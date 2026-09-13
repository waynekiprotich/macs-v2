"""
Signals -> labelled ML feature rows.

compute_features() is the only place a model input is defined. Training
(build_dataset, below) and live scoring must both call it, so the model is
never served features computed differently from the ones it learned on.

Directional features are aligned to the trade's side: for a SELL the
indicator is mirrored, so "momentum with the trade" is positive for both
directions. That matches technical_strategy's symmetric conditions and lets
BUY and SELL rows train one model. `side` stays in as a feature so the model
can still learn a drift asymmetry between the two.

Usage:
    python -m ml.features --out features.csv [--since 2026-01-01] [--symbol OTC_DJI]
"""
import argparse
import logging
import math
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional

import pandas as pd

from config.settings import settings
from core import technical_strategy
from execution.deriv_engine import _duration_delta
from models.database import MarketSnapshot, PaperTrade, SessionLocal, SystemLog

logger = logging.getLogger(__name__)

FEATURE_COLUMNS = [
    "side", "trend_ema", "trend_sma", "dist_ema_12", "dist_sma_50", "macd_diff",
    "rsi", "stoch_k", "stoch_gap", "bb_position", "bb_width", "atr_pct",
    "body_ratio", "candle_with", "wick_against",
    "dist_backstop", "dist_obstacle", "backstop_touches", "obstacle_touches",
    "conditions_with", "conditions_against", "regime_with", "regime_against",
    "is_volatile", "hour_sin", "hour_cos", "day_of_week",
]

META_COLUMNS = [
    "signal_id", "trade_id", "symbol", "candle_time", "signal", "action_taken",
    "label_source",
    # Deriv's quoted return on a win. For expected-value thresholds only — it
    # isn't known until a proposal is fetched, so it can't be a model input.
    "payout_ratio",
    "won",
]


def _num(ind: Mapping[str, Any], key: str) -> float:
    try:
        return float(ind[key])
    except (KeyError, TypeError, ValueError):
        return math.nan


def _per(a: float, b: float) -> float:
    return a / b if b else math.nan


def compute_features(ind: Mapping[str, Any], side: str, candle_time: datetime) -> Dict[str, float]:
    """Model inputs for taking `side` ("BUY"/"SELL") on the bar described by
    `ind`: one row from compute_indicators -> detect_regime ->
    technical_strategy.prepare, as stored in signals.indicators.

    Prices are divided by ATR or Close so OTC_DJI (~40,000) and frxXAUUSD
    (~2,500) rows are on the same scale. Missing inputs give NaN, which
    XGBoost handles natively."""
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, got {side!r}")
    buy = side == "BUY"
    s = 1.0 if buy else -1.0

    close, atr = _num(ind, "Close"), _num(ind, "ATR_14")
    support, resistance = _num(ind, "Support"), _num(ind, "Resistance")
    bb_upper, bb_lower = _num(ind, "BB_upper"), _num(ind, "BB_lower")
    stoch_k = _num(ind, "Stoch_k")
    bb_position = _per(close - bb_lower, bb_upper - bb_lower)

    conditions = technical_strategy.evaluate_row(pd.Series(dict(ind)))
    bull, bear = conditions["bull_conditions"], conditions["bear_conditions"]
    regime = ind.get("Regime")
    hour = candle_time.hour + candle_time.minute / 60

    return {
        "side": s,
        "trend_ema": s * _per(_num(ind, "EMA_12") - _num(ind, "EMA_26"), atr),
        "trend_sma": s * _per(_num(ind, "SMA_50") - _num(ind, "SMA_200"), atr),
        "dist_ema_12": s * _per(close - _num(ind, "EMA_12"), atr),
        "dist_sma_50": s * _per(close - _num(ind, "SMA_50"), atr),
        "macd_diff": s * _per(_num(ind, "MACD_diff"), atr),
        "rsi": _num(ind, "RSI_14") if buy else 100 - _num(ind, "RSI_14"),
        "stoch_k": stoch_k if buy else 100 - stoch_k,
        "stoch_gap": s * (stoch_k - _num(ind, "Stoch_d")),
        "bb_position": bb_position if buy else 1 - bb_position,
        # Direction-free: volatility and candle shape.
        "bb_width": _per(bb_upper - bb_lower, close),
        "atr_pct": _per(atr, close),
        "body_ratio": _num(ind, "Body_Ratio"),
        "candle_with": float(bool(ind.get("Bullish_Candle" if buy else "Bearish_Candle"))),
        "wick_against": _num(ind, "Upper_Wick_Pct" if buy else "Lower_Wick_Pct"),
        # Backstop: the level behind the trade (support for a BUY). Obstacle: the one in front.
        "dist_backstop": _per(close - support, atr) if buy else _per(resistance - close, atr),
        "dist_obstacle": _per(resistance - close, atr) if buy else _per(close - support, atr),
        "backstop_touches": _num(ind, "Support_Touches" if buy else "Resistance_Touches"),
        "obstacle_touches": _num(ind, "Resistance_Touches" if buy else "Support_Touches"),
        "conditions_with": float(bull if buy else bear),
        "conditions_against": float(bear if buy else bull),
        "regime_with": float(regime == ("bullish" if buy else "bearish")),
        "regime_against": float(regime == ("bearish" if buy else "bullish")),
        "is_volatile": float(bool(ind.get("Is_Volatile"))),
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "day_of_week": float(candle_time.weekday()),
    }


def label_from_prices(side: str, entry: Optional[float], exit_price: Optional[float]) -> Optional[int]:
    """1 if a CALL (BUY) / PUT (SELL) from entry to exit wins. A tie loses:
    Deriv's contract terms require strictly higher / strictly lower."""
    if entry is None or exit_price is None or math.isnan(entry) or math.isnan(exit_price):
        return None
    return int(exit_price > entry) if side == "BUY" else int(exit_price < entry)


def label_from_trade(trade: PaperTrade) -> Optional[int]:
    if trade.result in ("WON", "LOST"):
        return int(trade.result == "WON")
    if trade.status == "CLOSED" and trade.pnl is not None:
        return int(trade.pnl > 0)
    return None


def _utc(dt: datetime) -> datetime:
    # SQLite returns naive datetimes, Postgres aware ones; both hold UTC.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def build_dataset(session, since: Optional[datetime] = None, symbol: Optional[str] = None) -> pd.DataFrame:
    """One labelled row per BUY/SELL signal that has an indicator snapshot,
    ordered by candle_time (walk-forward splits depend on that order).

    Label source, best first:
      trade  — the contract bought on this signal settled: exact outcome.
      candle — not executed (dry run, risk-blocked) or still open: replayed
               from market_snapshots, entry = signal close, exit = close of
               the bar one contract duration later. Approximate: the live
               signal is taken on a still-forming bar and Deriv settles on ticks.
    Signals with neither are dropped.
    """
    signals = (
        session.query(SystemLog, PaperTrade)
        .outerjoin(PaperTrade, PaperTrade.signal_id == SystemLog.id)
        .filter(SystemLog.signal.in_(("BUY", "SELL")))
        .filter(SystemLog.indicators.isnot(None), SystemLog.candle_time.isnot(None))
    )
    snapshots = session.query(
        MarketSnapshot.symbol, MarketSnapshot.granularity, MarketSnapshot.candle_time, MarketSnapshot.close
    )
    if since:
        signals = signals.filter(SystemLog.candle_time >= since)
        snapshots = snapshots.filter(MarketSnapshot.candle_time >= since)
    if symbol:
        signals = signals.filter(SystemLog.symbol == symbol)
        snapshots = snapshots.filter(MarketSnapshot.symbol == symbol)

    closes = {(sym, granularity, _utc(ct)): close for sym, granularity, ct, close in snapshots}
    duration = _duration_delta(settings.MACS_CONTRACT_DURATION, settings.MACS_CONTRACT_DURATION_UNIT)

    rows, dropped = [], 0
    for sig, trade in signals.order_by(SystemLog.candle_time).all():
        candle_time = _utc(sig.candle_time)
        won, source = (label_from_trade(trade), "trade") if trade else (None, None)
        if won is None:
            exit_close = closes.get((sig.symbol, sig.granularity, candle_time + duration))
            won, source = label_from_prices(sig.signal, sig.close_price, exit_close), "candle"
        if won is None:
            dropped += 1
            continue

        payout_ratio = None
        if trade and trade.quoted_payout and trade.price:
            payout_ratio = trade.quoted_payout / trade.price - 1

        rows.append({
            "signal_id": sig.id,
            "trade_id": trade.id if trade else None,
            "symbol": sig.symbol,
            "candle_time": candle_time,
            "signal": sig.signal,
            "action_taken": sig.action_taken,
            "label_source": source,
            "payout_ratio": payout_ratio,
            "won": won,
            **compute_features(sig.indicators, sig.signal, candle_time),
        })

    if dropped:
        logger.info(f"Dropped {dropped} signal(s) with no settled trade and no exit candle to label them.")
    return pd.DataFrame(rows, columns=META_COLUMNS + FEATURE_COLUMNS)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Export labelled ML feature rows from the signals table.")
    parser.add_argument("--out", default="features.csv")
    parser.add_argument("--since", type=lambda s: _utc(datetime.fromisoformat(s)), help="ISO date, UTC (e.g. 2026-01-01)")
    parser.add_argument("--symbol")
    args = parser.parse_args(argv)

    session = SessionLocal()
    try:
        df = build_dataset(session, since=args.since, symbol=args.symbol)
    finally:
        session.close()

    df.to_csv(args.out, index=False)
    print(f"{len(df)} rows -> {args.out}")
    if len(df):
        print(df["label_source"].value_counts().to_string())
        print(f"Win rate: {df['won'].mean():.1%}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
