import logging
import math
from datetime import datetime
from typing import List, Optional

import numpy as np
import pandas as pd
from sqlalchemy import func

from models.database import MarketSnapshot, SessionLocal, SystemLog
from .data_deriv import DerivDataProvider
from .indicators import compute_indicators
from .regime import detect_regime
from .risk_management import RiskManager
from . import technical_strategy

logger = logging.getLogger(__name__)

# fetch_data() defaults to 15m candles.
GRANULARITY_SECONDS = 900


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


class TradingPipeline:
    def __init__(self, symbols: List[str], db_path: str = "trading.db"):
        self.symbols = symbols
        self.risk_manager = RiskManager(db_path=db_path)
        self.data_provider = DerivDataProvider()

    def run(self, execute: bool = True) -> dict:
        """Orchestrate data->indicators->regime->scoring->strategies->risk->execution"""
        logger.info("Starting pipeline run...")
        outcomes = {s: {"action": "HOLD", "confidence": 0, "reason": "Not yet processed"} for s in self.symbols}

        # 1. Reconcile open contracts first
        from execution.deriv_engine import DerivEngine
        try:
            DerivEngine().reconcile_open_contracts()
        except Exception as e:
            logger.error(f"Failed to reconcile contracts: {e}")

        risk_status = self.risk_manager.can_trade()
        if not risk_status['allowed']:
            logger.warning(f"Trading blocked by risk manager: {risk_status['reason']}")
            self.risk_manager.record_block()
            outcomes = {s: {"action": "BLOCKED", "confidence": 0, "reason": risk_status['reason']} for s in self.symbols}
            return outcomes

        for symbol in self.symbols:
            logger.info(f"Processing symbol: {symbol}")

            df = self.data_provider.fetch_data(symbol)
            if df.empty:
                outcomes[symbol] = {"action": "NO DATA", "confidence": 0, "reason": "No candles returned from Deriv"}
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
                risk_status = self.risk_manager.can_trade()
                if risk_status['allowed']:
                    if execute:
                        res = self.execute_trade(symbol, signal, latest, signal_id=signal_id)
                        action_taken = "EXECUTED" if res.get("status") == "success" else "EXECUTION_FAILED"
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

        logger.info("Pipeline run completed.")
        return outcomes

    def _store_snapshots(self, symbol: str, df: pd.DataFrame) -> None:
        """Persist closed bars not stored yet. The last bar is still forming,
        so it's stored on a later cycle once it has closed. The first run
        backfills everything the fetch returned."""
        db = SessionLocal()
        try:
            latest_stored = db.query(func.max(MarketSnapshot.candle_time)).filter(
                MarketSnapshot.symbol == symbol, MarketSnapshot.granularity == GRANULARITY_SECONDS
            ).scalar()
            closed = df.iloc[:-1]
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
        )
        logger.info(f"Execution Result: {res}")
        return res
