import os
import json
import asyncio
import requests
import websockets
import logging
from datetime import datetime, timezone, timedelta
from config.settings import settings
from execution.base import BaseEngine
from models.database import SessionLocal, PaperTrade
from core.notifications import send_discord_signal

logger = logging.getLogger(__name__)


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

class DerivEngine(BaseEngine):
    def __init__(self):
        super().__init__()
        self.token = os.environ.get('DERIV_API_TOKEN')
        self.app_id = os.environ.get('DERIV_APP_ID')
        self.account_id = "DOT90734760"

    async def _execute_contract(self, symbol: str, signal: str, quantity: float):
        headers = {
            'Authorization': f'Bearer {self.token}',
            'Deriv-App-ID': self.app_id,
            'Content-Type': 'application/json'
        }
        
        # 1. Fetch OTP
        resp = requests.post(f'https://api.derivws.com/trading/v1/options/accounts/{self.account_id}/otp', headers=headers)
        resp.raise_for_status()
        ws_url = resp.json()['data']['url']
        
        async with websockets.connect(ws_url) as ws:
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
                "underlying_symbol": symbol
            }
            await ws.send(json.dumps(proposal_req))
            response = json.loads(await ws.recv())

            if 'error' in response:
                logger.error(f"Deriv Proposal Error: {response['error']}")
                return None

            proposal = response['proposal']
            proposal_id = proposal['id']
            payout = proposal['payout']
            entry_spot = proposal.get('spot')
            logger.info(f"Deriv Proposal ID: {proposal_id}, Payout: {payout}")

            # 3. Buy Contract
            buy_req = {
                "buy": proposal_id,
                "price": quantity
            }
            await ws.send(json.dumps(buy_req))
            buy_response = json.loads(await ws.recv())
            
            if 'error' in buy_response:
                logger.error(f"Deriv Buy Error: {buy_response['error']}")
                return None
                
            buy = buy_response['buy']
            contract_id = buy['contract_id']
            buy_price = buy['buy_price']
            logger.info(f"Deriv Execution Success! Contract ID: {contract_id}, Price: {buy_price}")

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

            return {
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

    def execute_signal(self, symbol: str, signal: str, quantity: float, price: float, reason: str = "",
                       tech_score: float = None, ai_score: float = None, confidence: float = None, regime: str = None) -> dict:
        """
        Synchronous wrapper to execute a contract and log it.
        """
        if signal.upper() not in ('BUY', 'SELL'):
            return {"status": "ignored"}
            
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        result = loop.run_until_complete(self._execute_contract(symbol, signal, quantity))
        loop.close()
        
        if not result:
            return {"status": "error", "message": "Failed to purchase Deriv contract"}
            
        # Log to DB
        db = SessionLocal()
        try:
            trade = PaperTrade(
                symbol=symbol,
                side=signal.upper(),
                quantity=quantity,
                price=result['buy_price'], # Use the actual stake charged
                status="OPEN",  # Contract is open until reconciled
                reason=reason,
                contract_id=str(result['contract_id']),
                proposal_id=str(result.get('proposal_id', '')),
                payout=float(result['payout']) if result.get('payout') is not None else None,
                tech_score=tech_score,
                ai_score=ai_score,
                confidence=confidence,
                regime=regime
            )
            db.add(trade)
            db.commit()
            db.refresh(trade)
            
            t_score = tech_score if tech_score is not None else 0.0
            c_score = confidence if confidence is not None else 0.0
            r_str = regime if regime is not None else "unknown"
            
            # Send Discord Alert
            send_discord_signal(
                symbol=symbol,
                side=signal.upper(),
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
            
            return {"status": "success", "trade_id": trade.id, "contract_id": result['contract_id']}
        except Exception as e:
            logger.error(f"Failed to log Deriv trade: {e}")
            db.rollback()
            return {"status": "error"}
        finally:
            db.close()

    def get_positions(self) -> list:
        return []

    def get_account_summary(self) -> dict:
        return {"balance": 0.0}

    async def _reconcile_contract(self, ws, contract_id: str):
        req = {
            "proposal_open_contract": 1,
            "contract_id": int(contract_id)
        }
        await ws.send(json.dumps(req))
        resp = json.loads(await ws.recv())
        return resp.get("proposal_open_contract")

    def reconcile_open_contracts(self):
        """Finds OPEN contracts in DB, asks Deriv for status, and updates DB."""
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
            resp = requests.post(f'https://api.derivws.com/trading/v1/options/accounts/{self.account_id}/otp', headers=headers)
            if resp.status_code != 200:
                logger.error("Reconciliation failed to get OTP.")
                return
                
            ws_url = resp.json()['data']['url']
            
            async def run_recon():
                async with websockets.connect(ws_url) as ws:
                    from datetime import datetime, timezone
                    for trade in open_trades:
                        if not trade.contract_id:
                            continue
                        contract_info = await self._reconcile_contract(ws, trade.contract_id)
                        if not contract_info:
                            continue
                            
                        # is_sold == 1 or status in ('won', 'lost') means it's closed
                        if contract_info.get('is_sold') == 1 or contract_info.get('status') in ('won', 'lost'):
                            status_str = contract_info.get('status', 'unknown')
                            trade.status = "CLOSED"
                            trade.result = status_str.upper()
                            trade.payout = float(contract_info.get('sell_price', 0) or contract_info.get('payout', 0))
                            trade.pnl = float(contract_info.get('profit', 0))
                            trade.closed_timestamp = datetime.now(timezone.utc)
                            logger.info(f"Reconciled contract {trade.contract_id}: {trade.result} | PnL: {trade.pnl}")
            
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(run_recon())
            loop.close()
            
            db.commit()
        except Exception as e:
            logger.error(f"Reconciliation error: {e}")
            db.rollback()
        finally:
            db.close()
