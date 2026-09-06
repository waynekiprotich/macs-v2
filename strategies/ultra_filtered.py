# DEPRECATED: this was the strategy the OLD backtest command tested, but it
# was never actually wired into core/pipeline.py's live path (which ran a
# separate, weaker 4-factor Tech_Score instead) — a split-brain bug where
# backtest results told you nothing about live behavior. Superseded by
# core/technical_strategy.py, which both live trading (pipeline.py) and
# `cli.py backtest` now share. Kept only because tests/test_strategies.py
# still imports it.
import pandas as pd
import numpy as np
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger(__name__)

class UltraFilteredStrategy:
    """
    Requires 7+ of 8 conditions to pass: regime, SMA alignment, RSI zone, stoch, volume, support, MACD, AI confidence.
    TP=0.4x ATR, SL=3x ATR.
    """
    def __init__(self, atr_period: int = 14, tp_multiplier: float = 0.4, sl_multiplier: float = 3.0):
        self.atr_period = atr_period
        self.tp_multiplier = tp_multiplier
        self.sl_multiplier = sl_multiplier

    def generate_signal(self, current_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Expects current_data to have:
        - close
        - regime (bull/bear)
        - sma_short, sma_long
        - rsi
        - stoch_k, stoch_d
        - volume, volume_sma
        - support_level
        - macd, macd_signal
        - ai_confidence
        - atr
        """
        try:
            conditions_met = 0
            
            close = current_data.get('close', 0.0)
            
            # 1. Regime
            if current_data.get('regime') == 'bull':
                conditions_met += 1
                
            # 2. SMA alignment
            if current_data.get('sma_short', 0.0) > current_data.get('sma_long', 0.0):
                conditions_met += 1
                
            # 3. RSI zone (not overbought)
            rsi = current_data.get('rsi', 0.0)
            if 40 <= rsi <= 70:
                conditions_met += 1
                
            # 4. Stoch
            stoch_k = current_data.get('stoch_k', 0.0)
            stoch_d = current_data.get('stoch_d', 0.0)
            if stoch_k > stoch_d and stoch_k < 80:
                conditions_met += 1
                
            # 5. Volume
            if current_data.get('volume', 0.0) > current_data.get('volume_sma', 0.0):
                conditions_met += 1
                
            # 6. Support
            if close > current_data.get('support_level', 0.0) * 0.99:
                conditions_met += 1
                
            # 7. MACD
            if current_data.get('macd', 0.0) > current_data.get('macd_signal', 0.0):
                conditions_met += 1
                
            # 8. AI confidence
            if current_data.get('ai_confidence', 0.0) > 0.7:
                conditions_met += 1
                
            signal_type = "NEUTRAL"
            tp = 0.0
            sl = 0.0
            
            if conditions_met >= 7:
                signal_type = "BUY"
                atr = current_data.get('atr', 0.0)
                tp = close + (atr * self.tp_multiplier)
                sl = close - (atr * self.sl_multiplier)
                
            return {
                "strategy": "UltraFiltered",
                "signal": signal_type,
                "take_profit": tp,
                "stop_loss": sl,
                "conditions_met": conditions_met,
                "metadata": current_data
            }
            
        except Exception as e:
            logger.error(f"Error generating signal in UltraFilteredStrategy: {e}")
            return {
                "strategy": "UltraFiltered",
                "signal": "ERROR",
                "error": str(e)
            }
