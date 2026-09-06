import logging
from typing import List
from .data_deriv import DerivDataProvider
from .indicators import compute_indicators
from .regime import detect_regime
from .risk_management import RiskManager
from . import technical_strategy

logger = logging.getLogger(__name__)

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

            from config.settings import settings
            min_conditions = getattr(settings, 'MACS_MIN_CONDITIONS', 6)

            latest = df.iloc[-1].copy()
            is_volatile = bool(latest.get('Is_Volatile', False))
            eval_result = technical_strategy.generate_signal(latest, min_conditions=min_conditions, is_volatile=is_volatile)

            signal = eval_result['signal']
            confidence = eval_result['confidence']
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

            from models.database import SessionLocal, SystemLog
            db = SessionLocal()
            try:
                sys_log = SystemLog(
                    symbol=symbol,
                    tech_score=float(confidence),
                    ai_score=None,
                    combined_confidence=float(confidence),
                    regime=str(latest.get('Regime', 'Unknown')),
                    is_volatile=1 if is_volatile else 0,
                    signal=signal,
                    error_warning=None
                )
                db.add(sys_log)
                db.commit()
            except Exception as e:
                logger.error(f"Failed to write to system_logs: {e}")
            finally:
                db.close()

            if signal in ('BUY', 'SELL'):
                risk_status = self.risk_manager.can_trade()
                if risk_status['allowed']:
                    if execute:
                        self.execute_trade(symbol, signal, latest)
                    else:
                        logger.info(f"[DRY RUN] Would execute {signal} for {symbol} at {latest.get('Close', 0):.2f}")
                else:
                    logger.warning(f"Trade blocked for {symbol} right before execution: {risk_status['reason']}")
                    outcomes[symbol]["action"] = "HOLD"
                    outcomes[symbol]["reason"] = f"Risk blocked: {risk_status['reason']}"
                    
        logger.info("Pipeline run completed.")
        return outcomes

    def execute_trade(self, symbol: str, action: str, data_row):
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
            quantity=10.0, # Stake $10
            price=data_row.get('Close', 0.0),
            reason=reason,
            tech_score=confidence,
            ai_score=None,
            confidence=confidence,
            regime=regime
        )
        logger.info(f"Execution Result: {res}")
