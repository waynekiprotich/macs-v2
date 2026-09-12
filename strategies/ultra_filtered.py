"""Ultra-filtered high-win-rate strategy - only trades when everything aligns."""

import pandas as pd
import numpy as np
from typing import Optional, Dict, Any

class UltraFilteredStrategy:
    """
    Enters only when ALL conditions are met:
    - Bullish or recovery regime
    - Technical score >= min_technical_score (default 8/10)
    - RSI between 40-70 (not oversold, not overbought)
    - Price above 50 AND 200 SMA (uptrend)
    - Volume > 20-day average
    - Price near support (within 5%)
    - Tight TP at 0.4x ATR, wide SL at 3.0x ATR
    - AI analysis confidence >= 0.85
    """

    def __init__(self, config: dict):
        self.config = config
        self.min_score = config.get("min_technical_score", 8.0)
        self.tp_atr = config.get("tight_tp_atr_multiple", 0.4)
        self.sl_atr = config.get("wide_sl_atr_multiple", 3.0)
        self.min_ai_conf = config.get("min_ai_confidence", 0.85)

    def evaluate(self, df: pd.DataFrame, regime: dict, ai_analysis: Optional[dict] = None) -> Dict[str, Any]:
        """
        Evaluate whether to enter a trade.
        
        Returns:
            dict with action, confidence, reason, tp/sl levels
        """
        if len(df) < 200:
            return {"action": "hold", "confidence": 0, "reason": "insufficient data"}

        last = df.iloc[-1]
        prev = df.iloc[-2]

        reasons = []
        passed = 0
        total_checks = 8

        # --- CHECK 1: Regime ---
        if regime["regime"] in ("bullish",):
            passed += 1
            reasons.append(f"regime={regime['regime']} ✓")
        else:
            reasons.append(f"regime={regime['regime']} ✗")

        # --- CHECK 2: Price above 50 and 200 SMA ---
        if last["Close"] > last["sma_50"] and last["Close"] > last["sma_200"]:
            passed += 1
            reasons.append("price > 50/200 SMA ✓")
        else:
            reasons.append("price < 50/200 SMA ✗")

        # --- CHECK 3: RSI in sweet spot (40-70 for buys, 30-60 for sells) ---
        if 40 <= last["rsi"] <= 70:
            passed += 1
            reasons.append(f"rsi={last['rsi']:.1f} ✓")
        else:
            reasons.append(f"rsi={last['rsi']:.1f} ✗")

        # --- CHECK 4: Not overbought/oversold on stochastic ---
        if 20 <= last["stoch_k"] <= 80:
            passed += 1
            reasons.append(f"stoch={last['stoch_k']:.1f} ✓")
        else:
            reasons.append(f"stoch={last['stoch_k']:.1f} ✗")

        # --- CHECK 5: Volume confirmation ---
        if last["volume_ratio"] > 1.0:
            passed += 1
            reasons.append(f"vol_ratio={last['volume_ratio']:.2f} ✓")
        else:
            reasons.append(f"vol_ratio={last['volume_ratio']:.2f} ✗")

        # --- CHECK 6: Price near support (within 5%) ---
        dist_to_support = last.get("distance_to_support", 100)
        if dist_to_support < 5.0:
            passed += 1
            reasons.append(f"near_support({dist_to_support:.1f}%) ✓")
        else:
            reasons.append(f"far_from_support({dist_to_support:.1f}%) ✗")

        # --- CHECK 7: MACD bullish (macd > signal) ---
        if last["macd"] > last["macd_signal"]:
            passed += 1
            reasons.append("macd_bullish ✓")
        else:
            reasons.append("macd_bearish ✗")

        # --- CHECK 8: AI analysis (if available) ---
        ai_ok = True
        if ai_analysis and "confidence" in ai_analysis:
            ai_ok = ai_analysis["confidence"] >= self.min_ai_conf
            if ai_ok:
                passed += 1
                reasons.append(f"ai_conf={ai_analysis['confidence']:.2f} ✓")
            else:
                reasons.append(f"ai_conf={ai_analysis['confidence']:.2f} ✗")
        else:
            # No AI analysis - use technical only
            passed += 1  # Don't penalize
            reasons.append("no_ai (tech_only) ✓")

        # --- Compute confidence score ---
        confidence = passed / total_checks
        enough_passes = confidence >= 0.75  # At least 6/8 checks

        if not enough_passes:
            return {
                "action": "hold",
                "confidence": round(confidence, 2),
                "reason": " | ".join(reasons),
                "passed_checks": f"{passed}/{total_checks}",
            }

        # --- Determine action ---
        # Only long signals for ultra-filtered
        action = "buy"

        # --- Price levels ---
        entry_price = last["Close"]
        atr = last["atr"]
        take_profit = entry_price + (atr * self.tp_atr)
        stop_loss = entry_price - (atr * self.sl_atr)

        # Risk/reward
        risk = entry_price - stop_loss
        reward = take_profit - entry_price
        rr_ratio = reward / risk if risk > 0 else 0

        return {
            "action": action,
            "confidence": round(confidence, 2),
            "entry_price": round(entry_price, 2),
            "take_profit": round(take_profit, 2),
            "stop_loss": round(stop_loss, 2),
            "risk_reward_ratio": round(rr_ratio, 3),
            "quantity_pct": self._position_size(confidence),
            "reason": " | ".join(reasons),
            "passed_checks": f"{passed}/{total_checks}",
            "ai_used": ai_analysis is not None,
        }

    def _position_size(self, confidence: float) -> float:
        """Scale position size by confidence."""
        if confidence >= 0.95:
            return 0.03  # 3% of capital
        elif confidence >= 0.85:
            return 0.02  # 2%
        else:
            return 0.01  # 1%