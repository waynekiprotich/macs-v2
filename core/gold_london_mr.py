"""EXPERIMENTAL gold mean-reversion strategy, found in research/ (see
research/REPORT.md). Used only when MACS_STRATEGY=gold_london_mr, and only for
Deriv demo forward testing.

Evidence status: the 07-12 UTC primary version won 52.6% of 76 contracts on
the untouched test split (2026-07-18..09-29), below the 57.5% breakeven of a
0.74 payout. The 06-13 UTC window used here won 59.3% of 123 contracts
(95% CI 50.5-67.6%). That is not proof of an edge, and the pattern weakened
from June 2026 on. Treat live results as an experiment, not income.

Rule, evaluated once per closed 5-minute candle of frxXAUUSD:
  1. Stretch score: the mean of six train-ranked features (EMA9 distance,
     RSI7, stochastic %K, 3-bar return, 20-bar range position, Bollinger %B),
     sign-flipped so that high means stretched down (oversold).
  2. CALL when the score is at or above its 95th percentile, PUT at or
     below its 5th. The percentiles are frozen in gold_london_mr_params.json.
  3. Trade only in the direction of the last closed 1-hour EMA21 slope.
  4. Only decisions between 06:00 and 13:00 UTC.
  5. 20-minute Rise/Fall contract (a flat ~0.74 payout; below 20 minutes
     Deriv tilts gold payouts against mean-reversion entries).
Only closed candles are used. The caller must drop the forming candle.
"""
import json
import os
from functools import lru_cache
from typing import Any, Dict, Optional

import numpy as np
import pandas as pd

NAME = "gold_london_mr"
PARAMS_PATH = os.path.join(os.path.dirname(__file__), "gold_london_mr_params.json")
BAR_SECONDS = 300
HTF_SECONDS = 3600
# Research: each minute between the candle close and entry cost three to six
# points of win rate, so a late signal is dropped sooner than the 15m strategy's.
MAX_SIGNAL_AGE_SECONDS = 60


@lru_cache(maxsize=1)
def load_params(path: str = PARAMS_PATH) -> Dict[str, Any]:
    with open(path) as fh:
        params = json.load(fh)
    for col in params["parts"]:
        params["quantiles"][col] = np.asarray(params["quantiles"][col], dtype=float)
    return params


# Indicators. Kept identical to research/lib.py, which the parity test checks.

def _ema(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(span=n, adjust=False, min_periods=n).mean()


def _rma(x: pd.Series, n: int) -> pd.Series:
    return x.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def _atr(h: pd.Series, l: pd.Series, c: pd.Series, n: int = 14) -> pd.Series:
    pc = c.shift()
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return _rma(tr, n)


def _rsi(c: pd.Series, n: int) -> pd.Series:
    d = c.diff()
    up, down = _rma(d.clip(lower=0), n), _rma((-d).clip(lower=0), n)
    return 100 - 100 / (1 + up / down.replace(0, np.nan))


def features(bars: pd.DataFrame) -> pd.DataFrame:
    """The six stretch features for every closed 5-minute bar."""
    h, l, c = bars["High"], bars["Low"], bars["Close"]
    a14 = _atr(h, l, c, 14)
    vol = np.log(c).diff().rolling(50, min_periods=50).std()
    mid = c.rolling(20).mean()
    sd = c.rolling(20).std(ddof=0)
    lo14, hi14 = l.rolling(14).min(), h.rolling(14).max()
    hh20, ll20 = h.rolling(20).max(), l.rolling(20).min()
    return pd.DataFrame({
        "dist_ema9": (c - _ema(c, 9)) / a14,
        "rsi7": _rsi(c, 7),
        "stoch_k": 100 * (c - lo14) / (hi14 - lo14),
        "ret3": np.log(c / c.shift(3)) / (vol * np.sqrt(3)),
        "pos20": (c - ll20) / (hh20 - ll20).replace(0, np.nan),
        "bb_pctb": (c - (mid - 2 * sd)) / (4 * sd),
    }, index=bars.index)


def htf_slope(bars_1h: pd.DataFrame) -> pd.Series:
    """Sign-relevant 1-hour EMA21 slope over the last three closed hours."""
    c = bars_1h["Close"]
    e = _ema(c, 21)
    return (e - e.shift(3)) / _atr(bars_1h["High"], bars_1h["Low"], c, 14)


def _rank(x: float, grid: np.ndarray) -> float:
    """Fraction of the fitted distribution strictly below x."""
    k = len(grid)
    i = int(np.searchsorted(grid, x, side="left"))
    if i == 0:
        return 0.0
    if i >= k:
        return 1.0
    lo, hi = grid[i - 1], grid[i]
    frac = (x - lo) / (hi - lo) if hi > lo else 0.0
    return (i - 1 + frac) / (k - 1)


def stretch_score(row: pd.Series, params: Dict[str, Any]) -> Optional[float]:
    vals = [row.get(col) for col in params["parts"]]
    if any(v is None or not np.isfinite(v) for v in vals):
        return None
    ranks = [_rank(float(v), params["quantiles"][col]) for v, col in zip(vals, params["parts"])]
    return float(np.mean([-(r - 0.5) for r in ranks]) * 2)


def _utc_index(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    return index.tz_localize("UTC") if index.tz is None else index.tz_convert("UTC")


def generate_signal(bars_5m: pd.DataFrame, bars_1h: pd.DataFrame,
                    params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Signal for the latest closed 5-minute bar. Both frames must hold closed
    bars only, indexed by open time (naive UTC or tz-aware)."""
    params = params or load_params()
    hold = {"signal": "HOLD", "score": None, "confidence": 0.0, "htf_slope": None}
    if bars_5m.empty or len(bars_5m) < 60:
        return {**hold, "reason": "Not enough closed 5-minute candles"}
    bar_minutes = params["bar_minutes"]
    decision = _utc_index(bars_5m.index)[-1] + pd.Timedelta(minutes=bar_minutes)
    feats = features(bars_5m).iloc[-1]
    score = stretch_score(feats, params)
    out = {**hold, "decision_time": decision, "score": score, **{k: float(v) for k, v in feats.items()}}
    start, end = params["session_utc"]
    if not start <= decision.hour < end:
        return {**out, "reason": f"Outside {start:02d}:00-{end:02d}:00 UTC"}
    if score is None:
        return {**out, "reason": "Stretch features not available yet"}
    if score >= params["score_high"]:
        side = 1
    elif score <= params["score_low"]:
        side = -1
    else:
        return {**out, "reason": f"Stretch score {score:+.3f} inside "
                                 f"[{params['score_low']:+.3f}, {params['score_high']:+.3f}]"}
    # The 1-hour bar that closed at or before the decision time.
    h1 = bars_1h.copy()
    h1.index = _utc_index(h1.index)
    slope = htf_slope(h1)
    closed = slope[slope.index + pd.Timedelta(minutes=params["htf_minutes"]) <= decision].dropna()
    if closed.empty:
        return {**out, "reason": "No closed 1-hour trend available"}
    trend = float(closed.iloc[-1])
    out["htf_slope"] = trend
    if np.sign(trend) != side:
        return {**out, "reason": f"Stretch {'down' if side > 0 else 'up'} against the 1-hour trend"}
    # Percentile of the stretch, for logging. Not a win probability.
    out["confidence"] = round(100 * (0.5 + abs(score) / 2), 1)
    return {**out, "signal": "BUY" if side > 0 else "SELL",
            "reason": f"Stretch score {score:+.3f} beyond its 5% tail, with the 1-hour trend"}
