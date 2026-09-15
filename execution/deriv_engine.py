import os
import json
import asyncio
import requests
import websockets
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy.exc import IntegrityError
from config.settings import settings
from execution.base import BaseEngine
from models.database import SessionLocal, PaperTrade, TradeIntent
from core.notifications import send_discord_signal, send_heartbeat

logger = logging.getLogger(__name__)

# Seconds. Every network wait on the trading path is bounded, so a hung
# connection ends the cycle instead of stalling the worker.
HTTP_TIMEOUT = 10
WS_TIMEOUT = 15
# req_id for the two requests on a buy connection. Deriv echoes req_id on the
# reply, which is how a reply is tied to the request it answers.
PROPOSAL_REQ_ID = 1
BUY_REQ_ID = 2


class AmbiguousBuyError(Exception):
    """The buy request may have reached Deriv, but no usable response came
    back, so a contract may exist. Never retry: reconcile instead."""

    def __init__(self, message: str, contract_id=None):
        super().__init__(message)
        self.contract_id = contract_id


def _duration_delta(amount: int, unit: str) -> timedelta:
    """Deriv duration units -> timedelta. Ticks are ~2s on synthetics but are
    not time-based contracts, so they're approximated only for display."""
    unit = str(unit).lower()
    if unit == "d":
        return timedelta(days=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "s":
        return timedelta(seconds=amount)
    if unit == "t":
        return timedelta(seconds=amount * 2)
    return timedelta(minutes=amount)


def _float_or_none(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_float(info: dict, *keys):
    for key in keys:
        value = _float_or_none(info.get(key))
        if value is not None:
            return value
    return None


def _jsonable(fields: dict) -> dict:
    return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in fields.items()}


def _trade_fields_from_intent(intent) -> dict:
    """The trades row for an UNRECORDED intent: what execute_signal would
    have written, or a minimal row when an operator supplied only the
    contract ID. Reconciliation fills in the outcome."""
    fields = dict(intent.details or {})
    for key in ("entry_time", "expiry_time"):
        if isinstance(fields.get(key), str):
            fields[key] = datetime.fromisoformat(fields[key])
    fields.update(
        symbol=intent.symbol, side=intent.side, contract_id=str(intent.contract_id),
        signal_id=intent.signal_id, status="OPEN",
    )
    fields.setdefault("quantity", intent.stake)
    fields.setdefault("price", intent.stake)
    fields.setdefault("broker", "deriv")
    fields.setdefault("contract_type", "CALL" if intent.side == "BUY" else "PUT")
    fields.setdefault("reason", "Recorded by reconciliation after the trade write failed")
    if fields.get("entry_time"):
        fields.setdefault("timestamp", fields["entry_time"])
    return fields


class DerivEngine(BaseEngine):
    def __init__(self):
        super().__init__()
        self.token = os.environ.get('DERIV_API_TOKEN')
        self.app_id = os.environ.get('DERIV_APP_ID')
        self.account_id = "DOT90734760"

    async def _execute_contract(self, symbol: str, signal: str, quantity: float):
        """Buy one contract. Returns the buy details, or None when Deriv
        explicitly refused (nothing was bought). Raises AmbiguousBuyError
        when the buy was sent but its outcome is unknown; any other exception
        means the buy was never sent."""
        buy_sent = False
        contract_id = None
        result = None
        try:
            headers = {
                'Authorization': f'Bearer {self.token}',
                'Deriv-App-ID': self.app_id,
                'Content-Type': 'application/json'
            }

            # 1. Fetch OTP
            resp = requests.post(
                f'https://api.derivws.com/trading/v1/options/accounts/{self.account_id}/otp',
                headers=headers, timeout=HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            ws_url = resp.json()['data']['url']

            async with websockets.connect(ws_url, open_timeout=WS_TIMEOUT, close_timeout=5) as ws:
                contract_type = "CALL" if signal.upper() == "BUY" else "PUT"
                duration = settings.MACS_CONTRACT_DURATION
                duration_unit = settings.MACS_CONTRACT_DURATION_UNIT

                # 2. Get Proposal
                proposal_req = {
                    "proposal": 1,
                    "amount": quantity,
                    "basis": "stake",
                    "contract_type": contract_type,
                    "currency": "USD",
                    "duration": duration,
                    "duration_unit": duration_unit,
                    "underlying_symbol": symbol,
                    "req_id": PROPOSAL_REQ_ID,
                }
                await asyncio.wait_for(ws.send(json.dumps(proposal_req)), WS_TIMEOUT)
                response = json.loads(await asyncio.wait_for(ws.recv(), WS_TIMEOUT))

                # Nothing has been bought yet, so an unmatched reply only ends this attempt.
                if response.get('req_id') != PROPOSAL_REQ_ID or response.get('msg_type') != 'proposal':
                    raise ValueError(f"reply not identifiable as the proposal response: {str(response)[:300]}")
                if 'error' in response:
                    logger.error(f"Deriv Proposal Error: {response['error']}")
                    return None

                proposal = response['proposal']
                proposal_id = proposal['id']
                payout = proposal['payout']
                entry_spot = proposal.get('spot')
                logger.info(f"Deriv Proposal ID: {proposal_id}, Payout: {payout}")

                # 3. Buy Contract. From here on a contract may exist.
                buy_req = {
                    "buy": proposal_id,
                    "price": quantity,
                    "req_id": BUY_REQ_ID,
                }
                buy_sent = True
                await asyncio.wait_for(ws.send(json.dumps(buy_req)), WS_TIMEOUT)
                buy_response = json.loads(await asyncio.wait_for(ws.recv(), WS_TIMEOUT))

                # Only a reply that identifies itself as the answer to this buy
                # (its req_id, msg_type buy, this proposal echoed back) says
                # anything about the buy. Anything else leaves the outcome unknown;
                # a contract ID it carries is kept so the contract can be recorded.
                is_buy_reply = (
                    buy_response.get('req_id') == BUY_REQ_ID
                    and buy_response.get('msg_type') == 'buy'
                    and (buy_response.get('echo_req') or {}).get('buy') == proposal_id
                )
                if not is_buy_reply:
                    receipt = buy_response.get('buy')
                    if isinstance(receipt, dict):
                        contract_id = receipt.get('contract_id')
                    raise ValueError(f"reply not identifiable as the buy response: {str(buy_response)[:300]}")
                if 'error' in buy_response:
                    logger.error(f"Deriv Buy Error: {buy_response['error']}")
                    return None

                buy = buy_response['buy']
                contract_id = buy['contract_id']
                logger.info(f"Deriv Execution Success! Contract ID: {contract_id}")
                buy_price = buy['buy_price']
                logger.info(f"Deriv contract {contract_id} price: {buy_price}")

                # When the contract starts and settles. Deriv returns start_time as
                # an epoch; if this endpoint omits it we fall back to now, which is
                # correct to within the round-trip of the buy call. Expiry is
                # derived from our own duration rather than read back, so the alert
                # can always state a settle time even on a sparse response.
                start_epoch = buy.get('start_time') or buy.get('purchase_time')
                entry_time = (
                    datetime.fromtimestamp(start_epoch, tz=timezone.utc)
                    if start_epoch else datetime.now(timezone.utc)
                )
                expiry_time = entry_time + _duration_delta(duration, duration_unit)

                result = {
                    "contract_id": contract_id,
                    "proposal_id": proposal_id,
                    "buy_price": buy_price,
                    "contract_type": contract_type,
                    "duration": duration,
                    "duration_unit": duration_unit,
                    "entry_time": entry_time,
                    "expiry_time": expiry_time,
                    "entry_spot": entry_spot,
                    # Deriv's plain-English contract terms, e.g. "Win payout if
                    # Gold/USD is strictly lower than entry spot at 15 minutes
                    # after contract start time."
                    "longcode": buy.get('longcode'),
                    # Deriv's quoted payout for a winning contract. Captured at buy
                    # time because it's the single number that decides whether this
                    # system can be profitable at all: breakeven win rate is
                    # stake/payout, so a 0.85 ratio needs 54.1% accuracy. It was
                    # previously logged and discarded, which left cli.py's
                    # --payout backtest flag as an unverifiable guess.
                    "payout": payout,
                }
        except Exception as e:
            if result is not None:
                logger.error(f"Deriv contract {result['contract_id']} was bought; ignoring an error after the buy: {e}")
                return result
            if buy_sent:
                raise AmbiguousBuyError(f"{type(e).__name__}: {e}", contract_id=contract_id) from e
            raise
        return result

    def execute_signal(self, symbol: str, signal: str, quantity: float, price: float, reason: str = "",
                       tech_score: float = None, ai_score: float = None, confidence: float = None, regime: str = None,
                       signal_id: int = None, candle_time: datetime = None) -> dict:
        """
        Claim a trade intent, buy one contract, record it. Result status:
        - success: bought and recorded.
        - ignored: not a BUY/SELL signal.
        - duplicate: this symbol, candle and direction already has an intent; nothing bought.
        - error: nothing was bought.
        - reconciliation_required: a contract may exist, or does exist, without a
          trades row. Trading stays blocked until it is recorded or ruled out.
        A buy is never retried: a database rollback can't undo a contract Deriv sold.
        """
        side = signal.upper()
        if side not in ('BUY', 'SELL'):
            return {"status": "ignored"}
        if candle_time is None:
            logger.error(f"Refusing to trade {symbol}: no candle time to key the trade intent on")
            return {"status": "error", "message": "missing candle_time"}

        claim = self._claim_intent(symbol, side, candle_time, quantity, signal_id)
        if claim["status"] != "claimed":
            return claim
        intent_id = claim["intent_id"]

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(self._execute_contract(symbol, signal, quantity))
        except AmbiguousBuyError as e:
            contract = e.contract_id or "unknown"
            logger.critical(
                f"RECONCILIATION REQUIRED: BUY {symbol} {side} (trade intent {intent_id}) was sent to Deriv "
                f"but its outcome is unknown ({e}). Contract ID: {contract}. Not retrying; trading is blocked."
            )
            if e.contract_id:
                self._update_intent(intent_id, status="UNRECORDED", contract_id=str(e.contract_id), error=str(e))
            else:
                self._update_intent(intent_id, status="AMBIGUOUS", error=str(e))
            self._alert(f"RECONCILIATION REQUIRED: {symbol} {side} buy outcome unknown, contract {contract}, intent {intent_id}")
            return {"status": "reconciliation_required", "intent_id": intent_id, "contract_id": e.contract_id}
        except Exception as e:
            logger.error(f"Deriv buy for {symbol} failed before the buy was sent: {e}")
            self._update_intent(intent_id, status="FAILED", error=str(e))
            return {"status": "error", "message": str(e)}
        finally:
            loop.close()

        if not result:
            self._update_intent(intent_id, status="FAILED", error="Deriv refused the proposal or buy")
            return {"status": "error", "message": "Failed to purchase Deriv contract"}

        contract_id = str(result['contract_id'])
        trade_fields = dict(
            symbol=symbol,
            side=side,
            quantity=quantity,
            price=result['buy_price'], # Use the actual stake charged
            status="OPEN",  # Contract is open until reconciled
            reason=reason,
            contract_id=contract_id,
            proposal_id=str(result.get('proposal_id', '')),
            payout=_float_or_none(result.get('payout')),
            tech_score=tech_score,
            ai_score=ai_score,
            confidence=confidence,
            regime=regime,
            signal_id=signal_id,
            broker="deriv",
            mode=settings.MACS_MODE,
            contract_type=result.get('contract_type'),
            duration=result.get('duration'),
            duration_unit=result.get('duration_unit'),
            entry_time=result.get('entry_time'),
            expiry_time=result.get('expiry_time'),
            entry_spot=_float_or_none(result.get('entry_spot')),
            quoted_payout=_float_or_none(result.get('payout')),
        )
        try:
            trade_id = self._record_trade(intent_id, trade_fields)
        except Exception as e:
            logger.critical(
                f"RECONCILIATION REQUIRED: Deriv contract {contract_id} ({symbol} {side}, stake {result['buy_price']}, "
                f"trade intent {intent_id}) was bought but could not be recorded: {e}. Trading is blocked."
            )
            self._update_intent(intent_id, status="UNRECORDED", contract_id=contract_id, error=str(e),
                                details=_jsonable(trade_fields))
            self._alert(f"RECONCILIATION REQUIRED: contract {contract_id} ({symbol} {side}) bought but not recorded")
            return {"status": "reconciliation_required", "intent_id": intent_id, "contract_id": result['contract_id']}

        t_score = tech_score if tech_score is not None else 0.0
        c_score = confidence if confidence is not None else 0.0
        r_str = regime if regime is not None else "unknown"

        # Send Discord Alert. The trade is already recorded, so an alert failure
        # must not make it look failed.
        try:
            send_discord_signal(
                symbol=symbol,
                side=side,
                price=result['buy_price'],
                strategy="Deriv Engine",
                ai_score=ai_score,
                notes=f"Tech:{t_score:.1f} | Conf:{c_score:.1f} | Reg:{r_str} | ID:{result['contract_id']}",
                contract={
                    "contract_type": result.get('contract_type'),
                    "duration": result.get('duration'),
                    "duration_unit": result.get('duration_unit'),
                    "entry_time": result.get('entry_time'),
                    "expiry_time": result.get('expiry_time'),
                    "entry_spot": result.get('entry_spot'),
                    "stake": result['buy_price'],
                    "payout": result.get('payout'),
                    "longcode": result.get('longcode'),
                },
            )
        except Exception as e:
            logger.error(f"Failed to send trade alert for contract {contract_id}: {e}")

        return {"status": "success", "trade_id": trade_id, "contract_id": result['contract_id']}

    @staticmethod
    def _claim_intent(symbol: str, side: str, candle_time: datetime, stake: float, signal_id) -> dict:
        """Commit a PENDING intent before buying. The unique (symbol,
        candle_time, side) constraint turns a second attempt into a duplicate."""
        db = None
        try:
            db = SessionLocal()
            intent = TradeIntent(symbol=symbol, side=side, candle_time=candle_time, stake=stake,
                                 signal_id=signal_id, status="PENDING")
            db.add(intent)
            db.commit()
            return {"status": "claimed", "intent_id": intent.id}
        except IntegrityError as e:
            db.rollback()
            exists = db.query(TradeIntent.id).filter(
                TradeIntent.symbol == symbol, TradeIntent.candle_time == candle_time, TradeIntent.side == side
            ).first()
            if exists:
                logger.warning(f"Duplicate trade skipped: {symbol} {side} on candle {candle_time} already has trade intent {exists.id}")
                return {"status": "duplicate", "intent_id": exists.id}
            logger.error(f"Could not record trade intent for {symbol}; not buying: {e}")
            return {"status": "error", "message": f"trade intent not recorded: {e}"}
        except Exception as e:
            if db is not None:
                db.rollback()
            logger.error(f"Could not record trade intent for {symbol}; not buying: {e}")
            return {"status": "error", "message": f"trade intent not recorded: {e}"}
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _update_intent(intent_id: int, **fields) -> bool:
        db = None
        try:
            db = SessionLocal()
            fields["updated_at"] = datetime.now(timezone.utc)
            db.query(TradeIntent).filter(TradeIntent.id == intent_id).update(fields)
            db.commit()
            return True
        except Exception as e:
            if db is not None:
                db.rollback()
            logger.critical(
                f"Could not mark trade intent {intent_id} {fields.get('status')} "
                f"(contract {fields.get('contract_id') or 'unknown'}): {e}. It stays unresolved and trading stays blocked."
            )
            return False
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _record_trade(intent_id: int, trade_fields: dict) -> int:
        """Write the trades row and mark the intent EXECUTED in one
        transaction. Reuses an existing row for the same contract, so a
        contract is never recorded twice."""
        db = None
        try:
            db = SessionLocal()
            trade = db.query(PaperTrade).filter(PaperTrade.contract_id == trade_fields["contract_id"]).first()
            if trade is None:
                trade = PaperTrade(**trade_fields)
                db.add(trade)
                db.flush()
            db.query(TradeIntent).filter(TradeIntent.id == intent_id).update({
                "status": "EXECUTED",
                "contract_id": trade_fields["contract_id"],
                "trade_id": trade.id,
                "error": None,
                "updated_at": datetime.now(timezone.utc),
            })
            db.commit()
            return trade.id
        except Exception:
            if db is not None:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _alert(message: str) -> None:
        try:
            send_heartbeat(status=message)
        except Exception as e:
            logger.error(f"Failed to send reconciliation alert: {e}")

    def get_positions(self) -> list:
        return []

    def get_account_summary(self) -> dict:
        return {"balance": 0.0}

    async def _reconcile_contract(self, ws, contract_id: str, req_id: int):
        req = {
            "proposal_open_contract": 1,
            "contract_id": int(contract_id),
            "req_id": req_id,
        }
        await asyncio.wait_for(ws.send(json.dumps(req)), WS_TIMEOUT)
        resp = json.loads(await asyncio.wait_for(ws.recv(), WS_TIMEOUT))
        if resp.get("req_id") != req_id:
            logger.error(f"Reconciliation reply with req_id {resp.get('req_id')} while waiting for {req_id}; leaving contract {contract_id} OPEN")
            return None
        if "error" in resp:
            logger.error(f"Reconciliation error for contract {contract_id}: {resp['error']}")
            return None
        info = resp.get("proposal_open_contract")
        # Matched by req_id above and by contract ID here: never settle a trade from another contract's reply.
        if info and str(info.get("contract_id")) != str(contract_id):
            logger.error(f"Reconciliation reply for contract {info.get('contract_id')} while asking about {contract_id}; skipping")
            return None
        return info

    def _record_unrecorded_intents(self):
        """Write the trades row for contracts that were bought but never
        recorded. The contract ID is already known, so nothing is bought
        again; the row goes in OPEN and the reconciliation below settles it."""
        db = SessionLocal()
        try:
            pending = [
                (intent.id, intent.contract_id, _trade_fields_from_intent(intent))
                for intent in db.query(TradeIntent).filter(
                    TradeIntent.status == "UNRECORDED", TradeIntent.contract_id.isnot(None)
                ).all()
            ]
        except Exception as e:
            logger.error(f"Could not read unrecorded trade intents: {e}")
            return
        finally:
            db.close()

        for intent_id, contract_id, fields in pending:
            try:
                trade_id = self._record_trade(intent_id, fields)
                logger.warning(f"Recorded previously unrecorded Deriv contract {contract_id} as trade {trade_id}")
            except Exception as e:
                logger.critical(f"RECONCILIATION REQUIRED: contract {contract_id} (trade intent {intent_id}) is still not recorded: {e}")

    def reconcile_open_contracts(self):
        """Records unrecorded contracts, then finds OPEN contracts in DB, asks Deriv for status, and updates DB."""
        self._record_unrecorded_intents()
        db = SessionLocal()
        try:
            open_trades = db.query(PaperTrade).filter(PaperTrade.status == "OPEN").all()
            if not open_trades:
                return

            logger.info(f"Reconciling {len(open_trades)} OPEN contracts...")

            headers = {
                'Authorization': f'Bearer {self.token}',
                'Deriv-App-ID': self.app_id,
                'Content-Type': 'application/json'
            }
            resp = requests.post(
                f'https://api.derivws.com/trading/v1/options/accounts/{self.account_id}/otp',
                headers=headers, timeout=HTTP_TIMEOUT,
            )
            if resp.status_code != 200:
                logger.error("Reconciliation failed to get OTP.")
                return

            ws_url = resp.json()['data']['url']

            async def run_recon():
                async with websockets.connect(ws_url, open_timeout=WS_TIMEOUT, close_timeout=5) as ws:
                    from datetime import datetime, timezone
                    for req_id, trade in enumerate(open_trades, start=1000):
                        if not trade.contract_id:
                            continue
                        contract_info = await self._reconcile_contract(ws, trade.contract_id, req_id)
                        if not contract_info:
                            continue

                        # is_sold == 1 or status in ('won', 'lost') means it's closed
                        if contract_info.get('is_sold') == 1 or contract_info.get('status') in ('won', 'lost'):
                            profit = _float_or_none(contract_info.get('profit'))
                            if profit is None:
                                logger.error(f"Contract {trade.contract_id} settled without a profit figure; leaving it OPEN")
                                continue
                            status_str = contract_info.get('status', 'unknown')
                            trade.status = "CLOSED"
                            trade.result = status_str.upper()
                            # payout is what the contract pays on a win, the same won or
                            # lost; what it settled for is sell_price, the result is pnl.
                            trade.payout = _float_or_none(contract_info.get('payout')) or trade.payout
                            trade.pnl = profit
                            trade.sell_price = _float_or_none(contract_info.get('sell_price'))
                            # The Options API reports entry_spot/exit_spot; entry_tick,
                            # exit_tick and sell_spot are the older v3 names. Deriv's
                            # entry spot replaces the proposal-time spot stored at buy.
                            trade.exit_spot = _first_float(contract_info, 'exit_spot', 'exit_tick', 'sell_spot')
                            trade.entry_spot = _first_float(contract_info, 'entry_spot', 'entry_tick') or trade.entry_spot
                            trade.closed_timestamp = datetime.now(timezone.utc)
                            logger.info(f"Reconciled contract {trade.contract_id}: {trade.result} | PnL: {trade.pnl}")

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(run_recon())
            finally:
                loop.close()

            db.commit()
        except Exception as e:
            logger.error(f"Reconciliation error: {e}")
            db.rollback()
        finally:
            db.close()
