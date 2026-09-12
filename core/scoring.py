"""Technical + AI scoring for MACS."""

import pandas as pd
import numpy as np
from typing import Optional, Dict, Any

def technical_score(df: pd.DataFrame, regime: dict) -> float:
    """
    Score the technical setup from 0.0 to 10.0.
    Higher = more favorable conditions.
    """
    if len(df) < 50:
        return 0.0

    last = df.iloc[-1]
    score = 5.0  # Start neutral

    # Trend alignment (+2.0 max)
    if last["Close"] > last["sma_50"]:
        score += 0.5
    if last["Close"] > last["sma_200"]:
        score += 0.5
    if last["sma_20"] > last["sma_50"]:
        score += 0.5
    if last["sma_50"] > last["sma_200"]:
        score += 0.5

    # RSI scoring (+1.5 max)
    if 40 <= last["rsi"] <= 60:
        score += 1.5
    elif 35 <= last["rsi"] <= 65:
        score += 0.75
    elif last["rsi"] < 30 or last["rsi"] > 70:
        score -= 1.0

    # MACD (+1.0 max)
    if last["macd"] > last["macd_signal"]:
        score += 0.5
        if last["macd_diff"] > 0:
            score += 0.5
    else:
        score -= 0.5

    # Volume (+0.5 max)
    if last["volume_ratio"] > 1.2:
        score += 0.5
    elif last["volume_ratio"] > 1.0:
        score += 0.25

    # BB position (+0.5 max)
    if 0.3 <= last["bb_position"] <= 0.7:
        score += 0.5
    elif 0.2 <= last["bb_position"] <= 0.8:
        score += 0.25

    # Regime bonus (+0.5 max)
    if regime.get("regime") == "bullish":
        score += 0.5

    return round(min(max(score, 0.0), 10.0), 1)


def ai_analysis(df: pd.DataFrame, gemini_client, symbol: str) -> Dict[str, Any]:
    """
    Use Gemini to analyze recent price action.
    Returns confidence score and reasoning.
    """
    try:
        recent = df.tail(30)
        price_data = recent[["Date", "Open", "High", "Low", "Close", "Volume"]].to_dict("records")

        prompt = f"""You are a professional trading analyst. Analyze {symbol} price action over the last 30 bars.

Price data (last 30 periods):
{price_data}

Provide a JSON response exactly like this:
{{
    "direction": "bullish|bearish|neutral",
    "confidence": 0.0-1.0,
    "reasoning": "brief 1-sentence technical reasoning"
}}

Be conservative. Only rate bullish if the trend is clearly up."""

        model = gemini_client.GenerativeModel("gemini-2.0-flash")
        response = model.generate_content(prompt)

        import json
        import re
        text = response.text
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            return {
                "direction": result.get("direction", "neutral"),
                "confidence": float(result.get("confidence", 0.5)),
                "reasoning": result.get("reasoning", ""),
            }
    except Exception as e:
        pass

    return {"direction": "neutral", "confidence": 0.5, "reasoning": "AI analysis failed"}