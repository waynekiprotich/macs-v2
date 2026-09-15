import contextlib
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import numpy as np
import pandas as pd
from sqlalchemy import func

from models.database import MarketSnapshot, PaperTrade, SessionLocal, SystemLog, engine
from .data_deriv import DerivDataProvider
from .indicators import compute_indicators
from .instance_lock import trading_lock
from .regime import detect_regime
from .risk_management import RiskManager
from . import technical_strategy

logger = logging.getLogger(__name__)

# fetch_data() defaults to 15m candles.
GRANULARITY_SECONDS = 900

# A BUY/SELL decided more than this many seconds after its candle closed is not
# traded: the entry would no longer be the close the signal was computed on.
MAX_SIGNAL_AGE_SECONDS = 120
# An OPEN contract expiring within this many seconds is waited for, so the
# cycle reconciles it instead of being blocked by it...
SETTLEMENT_WAIT_SECONDS = 60
# ...plus this long after expiry for Deriv to settle it.
SETTLEMENT_BUFFER_SECONDS = 10

# execute_trade() status -> signals.action_taken. Anything else is a failure
# in which nothing was bought.
EXECUTION_ACTIONS = {
    "success": "EXECUTED",
    "duplicate": "DUPLICATE_SKIPPED",
    "reconciliation_required": "RECONCILIATION_REQUIRED",
}


def _json_safe(value):
    """numpy/pandas scalar -> plain JSON value. NaN and inf become None:
    Postgres JSONB rejects them."""
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, (float, np.floating)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    return value


def _indicator_snapshot(row: pd.Series) -> dict:
    return {str(key): _json_safe(value) for key, value in row.items()}


def _utc(ts) -> datetime:
    # Deriv epochs become naive UTC timestamps in data_deriv.py.
    ts = pd.Timestamp(ts)
    return (ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")).to_pydatetime()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _closed_bars(df: pd.DataFrame, granularity: int, now: datetime) -> pd.DataFrame:
    """Keep only bars that have closed. Deriv's ticks_history returns the
    still-forming bar last; a bar indexed by its open time has closed once
    open + granularity <= now."""
    if df.empty:
        return df
    opens = df.index.tz_localize("UTC") if df.index.tz is None else df.index.tz_convert("UTC")
    return df[opens + pd.Timedelta(seconds=granularity) <= pd.Timestamp(now)]


class TradingPipeline:
    def __init__(self, symbols: List[str], db_path: str = "trading.db"):
        self.symbols = symbols
        self.risk_manager = RiskManager(db_path=db_path)
        self.data_provider = DerivDataProvider()

    def run(self, execute: bool = True) -> dict:
        """Orchestrate data->indicators->regime->scoring->strategies->risk->execution,
        holding the single-instance trading lock for the whole cycle."""
        logger.info("Starting pipeline run...")
        with contextlib.ExitStack() as stack:
            try:
                acquired = stack.enter_context(trading_lock(engine))
            except Exception as e:
                logger.error(f"Could not check the trading lock; skipping this cycle: {e}")
                return self._blocked(f"Trading lock unavailable: {e}")
            if not acquired:
                logger.warning("Another MACS worker holds the trading lock; skipping this cycle.")
                return self._blocked("Another MACS worker is running")
            return self._run_locked(execute)

    def _blocked(self, reason: str) -> dict:
        return {s: {"action": "BLOCKED", "confidence": 0, "reason": reason} for s in self.symbols}

    def _wait_for_expiring_contracts(self) -> None:
        """Wait for OPEN contracts expiring within SETTLEMENT_WAIT_SECONDS to
        expire and settle, so this cycle can reconcile them. Later expiries are
        not waited for, and a failed read skips the wait: the risk check that
        follows still blocks on anything unsettled."""
        now = _now()
        db = SessionLocal()
        try:
            expiries = [_utc(expiry) for (expiry,) in db.query(PaperTrade.expiry_time).filter(
                PaperTrade.status == "OPEN", PaperTrade.expiry_time.isnot(None)
            ).all()]
        except Exception as e:
            logger.error(f"Could not read open contract expiries: {e}")
            return
        finally:
            db.close()

        buffer = timedelta(seconds=SETTLEMENT_BUFFER_SECONDS)
        horizon = now + timedelta(seconds=SETTLEMENT_WAIT_SECONDS)
        settles = [expiry + buffer for expiry in expiries if expiry <= horizon and expiry + buffer > now]
        if settles:
            wait = (max(settles) - now).total_seconds()
            logger.info(f"Waiting {wait:.0f}s for {len(settles)} contract(s) to expire and settle before reconciling.")
            _sleep(wait)

    def _run_locked(self, execute: bool) -> dict:
        outcomes = {s: {"action": "HOLD", "confidence": 0, "reason": "Not yet processed"} for s in self.symbols}

        # 1. Reconcile open contracts first, then read risk state, so contracts
        # that settled since the last cycle count toward this cycle's decisions.
        from execution.deriv_engine import DerivEngine
        self._wait_for_expiring_contracts()
        try:
            DerivEngine().reconcile_open_contracts()
        except Exception as e:
            logger.error(f"Failed to reconcile contracts: {e}")

        self.risk_manager.reload()
        risk_status = self.risk_manager.can_trade()
        if not risk_status['allowed']:
            logger.warning(f"Trading blocked by risk manager: {risk_status['reason']}")
            self.risk_manager.record_block()
            return self._blocked(risk_status['reason'])

        for symbol in self.symbols:
            logger.info(f"Processing symbol: {symbol}")

            df = self.data_provider.fetch_data(symbol)
            # Signals are evaluated on closed bars only: the forming bar's close is still moving.
            df = _closed_bars(df, GRANULARITY_SECONDS, _now())
            if df.empty:
                outcomes[symbol] = {"action": "NO DATA", "confidence": 0, "reason": "No closed candles returned from Deriv"}
                continue

            df = compute_indicators(df)
            df = detect_regime(df)
            df = technical_strategy.prepare(df)

            if df.empty:
                continue

            self._store_snapshots(symbol, df)

            from config.settings import settings
            min_conditions = getattr(settings, 'MACS_MIN_CONDITIONS', 6)

            latest = df.iloc[-1].copy()
            is_volatile = bool(latest.get('Is_Volatile', False))
            eval_result = technical_strategy.generate_signal(latest, min_conditions=min_conditions, is_volatile=is_volatile)

            signal = eval_result['signal']
            confidence = eval_result['confidence']
            signal_id = self._log_signal(symbol, latest, eval_result, min_conditions, is_volatile)
            latest['Confidence'] = confidence

            logger.info(
                f"{symbol} Latest Signal: {signal}, Confidence: {confidence:.1f}, "
                f"{eval_result['reason']}, Regime: {latest.get('Regime', 'Unknown')} "
                f"(Volatile: {is_volatile})"
            )
            outcomes[symbol] = {
                "action": signal,
                "confidence": confidence,
                "take_profit": eval_result.get('take_profit'),
                "stop_loss": eval_result.get('stop_loss'),
                "reason": eval_result['reason'],
            }

            if signal in ('BUY', 'SELL'):
                if signal_id is None:
                    # No signals row: the trade could be neither audited nor linked.
                    logger.error(f"Trade blocked for {symbol}: the signal could not be recorded")
                    outcomes[symbol]["action"] = "HOLD"
                    outcomes[symbol]["reason"] = "Signal not recorded; trade blocked"
                    continue
                signal_age = (_now() - _utc(latest.name)).total_seconds() - GRANULARITY_SECONDS
                if signal_age > MAX_SIGNAL_AGE_SECONDS:
                    logger.warning(
                        f"Trade skipped for {symbol}: its candle closed {signal_age:.0f}s ago "
                        f"(limit {MAX_SIGNAL_AGE_SECONDS}s)"
                    )
                    outcomes[symbol]["action"] = "STALE_SIGNAL"
                    outcomes[symbol]["reason"] = f"Candle closed {signal_age:.0f}s ago; not traded"
                    self._set_action_taken(signal_id, "STALE_SIGNAL")
                    continue
                # Re-read right before each trade: an earlier trade this cycle
                # may have left a contract that needs reconciliation.
                self.risk_manager.reload()
                risk_status = self.risk_manager.can_trade()
                if risk_status['allowed']:
                    if execute:
                        res = self.execute_trade(symbol, signal, latest, signal_id=signal_id)
                        action_taken = EXECUTION_ACTIONS.get(res.get("status"), "EXECUTION_FAILED")
                        if action_taken != "EXECUTED":
                            outcomes[symbol]["action"] = action_taken
                    else:
                        logger.info(f"[DRY RUN] Would execute {signal} for {symbol} at {latest.get('Close', 0):.2f}")
                        action_taken = "DRY_RUN"
                else:
                    logger.warning(f"Trade blocked for {symbol} right before execution: {risk_status['reason']}")
                    self.risk_manager.record_block()
                    outcomes[symbol]["action"] = "HOLD"
                    outcomes[symbol]["reason"] = f"Risk blocked: {risk_status['reason']}"
                    action_taken = "RISK_BLOCKED"
            else:
                action_taken = "VOLATILITY_SKIP" if is_volatile else "HOLD"
            self._set_action_taken(signal_id, action_taken)
            if action_taken == "RECONCILIATION_REQUIRED":
                logger.critical("Halting this cycle: a Deriv contract needs reconciliation before any further trade.")
                break

        logger.info("Pipeline run completed.")
        return outcomes

    def _store_snapshots(self, symbol: str, df: pd.DataFrame) -> None:
        """Persist bars not stored yet. run() passes closed bars only, so the
        forming bar is stored on a later cycle once it has closed. The first
        run backfills everything the fetch returned."""
        db = SessionLocal()
        try:
            latest_stored = db.query(func.max(MarketSnapshot.candle_time)).filter(
                MarketSnapshot.symbol == symbol, MarketSnapshot.granularity == GRANULARITY_SECONDS
            ).scalar()
            closed = df
            if latest_stored is not None:
                closed = closed[[_utc(ts) > _utc(latest_stored) for ts in closed.index]]
            db.add_all([
                MarketSnapshot(
                    symbol=symbol,
                    granularity=GRANULARITY_SECONDS,
                    candle_time=_utc(ts),
                    open=float(row['Open']),
                    high=float(row['High']),
                    low=float(row['Low']),
                    close=float(row['Close']),
                    source="deriv",
                    indicators=_indicator_snapshot(row),
                )
                for ts, row in closed.iterrows()
            ])
            db.commit()
        except Exception as e:
            logger.error(f"Failed to store market snapshots for {symbol}: {e}")
            db.rollback()
        finally:
            db.close()

    def _log_signal(self, symbol: str, latest: pd.Series, eval_result: dict,
                    min_conditions: int, is_volatile: bool) -> Optional[int]:
        db = SessionLocal()
        try:
            row = SystemLog(
                symbol=symbol,
                tech_score=float(eval_result['confidence']),
                ai_score=None,
                combined_confidence=float(eval_result['confidence']),
                regime=str(latest.get('Regime', 'Unknown')),
                is_volatile=is_volatile,
                signal=eval_result['signal'],
                error_warning=None,
                candle_time=_utc(latest.name),
                granularity=GRANULARITY_SECONDS,
                close_price=_json_safe(latest.get('Close')),
                bull_conditions=eval_result.get('bull_conditions'),
                bear_conditions=eval_result.get('bear_conditions'),
                min_conditions=min_conditions,
                take_profit=_json_safe(eval_result.get('take_profit')),
                stop_loss=_json_safe(eval_result.get('stop_loss')),
                reason=eval_result.get('reason'),
                strategy="technical_strategy",
                indicators=_indicator_snapshot(latest),
            )
            db.add(row)
            db.commit()
            return row.id
        except Exception as e:
            logger.error(f"Failed to write to signals: {e}")
            db.rollback()
            return None
        finally:
            db.close()

    def _set_action_taken(self, signal_id: Optional[int], action_taken: str) -> None:
        if signal_id is None:
            return
        db = SessionLocal()
        try:
            db.query(SystemLog).filter(SystemLog.id == signal_id).update({"action_taken": action_taken})
            db.commit()
        except Exception as e:
            logger.error(f"Failed to record action for signal {signal_id}: {e}")
            db.rollback()
        finally:
            db.close()

    def execute_trade(self, symbol: str, action: str, data_row, signal_id: Optional[int] = None) -> dict:
        """Execute trade using DerivEngine."""
        logger.info(f"EXECUTING {action} FOR {symbol} at price {data_row.get('Close', 0)}")
        from execution.deriv_engine import DerivEngine
        engine = DerivEngine()

        confidence = data_row.get('Confidence', 0.0)
        regime = data_row.get('Regime', 'unknown')
        reason = f"TechConfidence:{confidence:.1f} (pure technical, no AI)"

        res = engine.execute_signal(
            symbol=symbol,
            signal=action,
            quantity=170.0, # Stake $170
            price=data_row.get('Close', 0.0),
            reason=reason,
            tech_score=confidence,
            ai_score=None,
            confidence=confidence,
            regime=regime,
            signal_id=signal_id,
            candle_time=_utc(data_row.name),
        )
        logger.info(f"Execution Result: {res}")
        return res
