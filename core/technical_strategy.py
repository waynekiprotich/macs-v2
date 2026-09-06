"""
Unified technical strategy — pure price-action/indicator logic, no AI dependency.

This replaces the old split-brain setup where `scoring.py` computed a shallow
4-factor score for LIVE trading while `strategies/ultra_filtered.py`'s much
stricter 8-condition strategy was only ever exercised in the backtest command.
Backtest results told you nothing about what was actually trading live.

Also removes every condition that silently depended on Volume. Deriv synthetic
indices / forex feeds have no real volume — `data_deriv.py` hardcodes it to a
constant 1000.0 — so `volume > volume_sma` was comparing 1000 to 1000 and
never meaningfully contributing. It's replaced with a real candle-strength
condition (body-to-range ratio) instead.

Conditions are symmetric: every bullish check has a mirrored bearish check,
so SELL signals are a first-class citizen, not an afterthought.
"""
import pandas as pd
import numpy as np
import logging
from typing import Dict, Any

logger = logging.getLogger(__name__)

# Number of touches within TOUCH_TOLERANCE_PCT of a level, within the lookback
# window, required to call that level "real" support/resistance rather than
# a one-off wick.
SR_LOOKBACK = 20
TOUCH_TOLERANCE_PCT = 0.0015  # 0.15% — tune per instrument volatility
MIN_TOUCHES = 2

# How much of the candle's total range must be "body" (not wick) and how
# close the close must be to the extreme, to count as a strong directional
# candle. Replaces the old fake-volume condition.
MIN_BODY_RATIO = 0.55


def _support_resistance_strength(df: pd.DataFrame) -> pd.DataFrame:
    """
    Real pivot strength instead of the old `close > support * 0.99` guess:
    count how many times price has touched near the rolling support/resistance
    level in the lookback window. More touches = a level that actually matters.
    """
    df = df.copy()
    support = df['Low'].rolling(window=SR_LOOKBACK).min()
    resistance = df['High'].rolling(window=SR_LOOKBACK).max()

    def _touch_count(series_vals, level_vals, tol):
        counts = np.zeros(len(series_vals))
        for i in range(SR_LOOKBACK, len(series_vals)):
            window = series_vals[i - SR_LOOKBACK:i]
            level = level_vals[i]
            if level == 0 or np.isnan(level):
                continue
            touches = np.sum(np.abs(window - level) / level <= tol)
            counts[i] = touches
        return counts

    df['Support'] = support
    df['Resistance'] = resistance
    df['Support_Touches'] = _touch_count(df['Low'].values, support.values, TOUCH_TOLERANCE_PCT)
    df['Resistance_Touches'] = _touch_count(df['High'].values, resistance.values, TOUCH_TOLERANCE_PCT)
    return df


def _candle_strength(df: pd.DataFrame) -> pd.DataFrame:
    """Body-to-range ratio + close position, as a stand-in for missing volume."""
    df = df.copy()
    rng = (df['High'] - df['Low']).replace(0, np.nan)
    body = (df['Close'] - df['Open'])
    df['Body_Ratio'] = (body.abs() / rng).fillna(0)
    df['Bullish_Candle'] = (body > 0) & (df['Body_Ratio'] >= MIN_BODY_RATIO)
    df['Bearish_Candle'] = (body < 0) & (df['Body_Ratio'] >= MIN_BODY_RATIO)
    return df


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Add the extra columns this strategy needs on top of compute_indicators()."""
    if df.empty:
        return df
    df = _support_resistance_strength(df)
    df = _candle_strength(df)
    return df


TOTAL_CONDITIONS = 8


def evaluate_row(row: pd.Series) -> Dict[str, Any]:
    """
    Score a single row against 8 symmetric conditions. Returns conditions_met
    for both directions so the caller can pick whichever side clears the bar,
    plus a technical-only confidence (0-100) with no AI blending.
    """
    bull = 0
    bear = 0
    detail = {}

    close = row.get('Close', 0.0)

    # 1. Regime
    regime = row.get('Regime', 'neutral')
    detail['regime'] = regime
    if regime == 'bullish':
        bull += 1
    elif regime == 'bearish':
        bear += 1

    # 2. Trend alignment (EMA12 vs EMA26)
    ema12, ema26 = row.get('EMA_12', 0.0), row.get('EMA_26', 0.0)
    if ema12 > ema26:
        bull += 1
    elif ema12 < ema26:
        bear += 1

    # 3. RSI healthy zone (avoid buying overbought / selling oversold — leave room to run)
    rsi = row.get('RSI_14', 50.0)
    if 45 <= rsi <= 65:
        bull += 1
    if 35 <= rsi <= 55:
        bear += 1

    # 4. Stochastic direction, not already at an extreme
    k, d = row.get('Stoch_k', 0.0), row.get('Stoch_d', 0.0)
    if k > d and k < 80:
        bull += 1
    if k < d and k > 20:
        bear += 1

    # 5. MACD momentum
    macd_diff = row.get('MACD_diff', 0.0)
    if macd_diff > 0:
        bull += 1
    elif macd_diff < 0:
        bear += 1

    # 6. Support/Resistance — bouncing off a *proven* level, not just a number
    support = row.get('Support', 0.0)
    resistance = row.get('Resistance', 0.0)
    support_touches = row.get('Support_Touches', 0)
    resistance_touches = row.get('Resistance_Touches', 0)
    if support > 0 and support_touches >= MIN_TOUCHES and close <= support * (1 + TOUCH_TOLERANCE_PCT * 3):
        bull += 1
    if resistance > 0 and resistance_touches >= MIN_TOUCHES and close >= resistance * (1 - TOUCH_TOLERANCE_PCT * 3):
        bear += 1

    # 7. Bollinger position — room to move, not already pinned to a band
    bb_upper, bb_lower, bb_mid = row.get('BB_upper', 0.0), row.get('BB_lower', 0.0), row.get('BB_mid', 0.0)
    if bb_mid > 0 and bb_mid <= close < bb_upper:
        bull += 1
    if bb_mid > 0 and bb_lower < close <= bb_mid:
        bear += 1

    # 8. Candle strength (replaces the old fake-volume condition)
    if row.get('Bullish_Candle', False):
        bull += 1
    if row.get('Bearish_Candle', False):
        bear += 1

    detail['bull_conditions'] = bull
    detail['bear_conditions'] = bear
    detail['bull_confidence'] = round(bull / TOTAL_CONDITIONS * 100, 1)
    detail['bear_confidence'] = round(bear / TOTAL_CONDITIONS * 100, 1)
    return detail


def generate_signal(row: pd.Series, min_conditions: int = 6, is_volatile: bool = False) -> Dict[str, Any]:
    """
    Pure technical signal — no AI. `min_conditions` out of 8 must agree.
    Skips entirely during Is_Volatile (chop tends to fake out every indicator
    at once regardless of how many conditions "agree").
    """
    result = evaluate_row(row)
    atr = row.get('ATR_14', 0.0)
    close = row.get('Close', 0.0)

    signal = 'HOLD'
    confidence = max(result['bull_confidence'], result['bear_confidence'])
    tp = sl = None

    if is_volatile:
        return {**result, 'signal': 'HOLD', 'confidence': confidence,
                'take_profit': None, 'stop_loss': None,
                'reason': 'Skipped: volatility filter (Is_Volatile)'}

    if result['bull_conditions'] >= min_conditions and result['bull_conditions'] > result['bear_conditions']:
        signal = 'BUY'
        tp = close + (atr * 0.4)
        sl = close - (atr * 3.0)
    elif result['bear_conditions'] >= min_conditions and result['bear_conditions'] > result['bull_conditions']:
        signal = 'SELL'
        tp = close - (atr * 0.4)
        sl = close + (atr * 3.0)

    return {
        **result,
        'signal': signal,
        'confidence': confidence,
        'take_profit': tp,
        'stop_loss': sl,
        'reason': f"{result['bull_conditions']}/{TOTAL_CONDITIONS} bull, {result['bear_conditions']}/{TOTAL_CONDITIONS} bear conditions",
    }
