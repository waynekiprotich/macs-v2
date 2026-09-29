"""Shared, deterministic policy for demo execution and offline experiments."""
import hashlib
import json
import math
from decimal import Decimal, ROUND_DOWN

import pandas as pd

from config.settings import MarketRule, settings


def market_rule(symbol: str) -> MarketRule:
    # A command-line symbol can collect observations, but cannot approve trading.
    return settings.MACS_MARKET_RULES.get(symbol, MarketRule())


def experiment_config() -> dict:
    return {
        "experiment_id": settings.MACS_EXPERIMENT_ID,
        "execution_enabled": settings.MACS_EXECUTION_ENABLED,
        "mode": settings.MACS_MODE,
        "rules": {k: v.model_dump() for k, v in settings.MACS_MARKET_RULES.items()},
        "risk_per_trade": settings.MACS_RISK_PER_TRADE,
        "max_stake": settings.MACS_MAX_STAKE,
        "min_stake": settings.MACS_MIN_STAKE,
        "max_open_exposure": settings.MACS_MAX_OPEN_EXPOSURE,
        "max_open_trades": settings.MACS_MAX_OPEN_TRADES,
        "daily_loss_fraction": settings.MACS_DAILY_LOSS_FRACTION,
        "max_daily_loss": settings.MACS_MAX_DAILY_LOSS,
        "loss_streak_limit": settings.MACS_MAX_CONSECUTIVE_LOSSES,
        "cooldown_hours": settings.MACS_COOLDOWN_HOURS,
    }


def config_fingerprint() -> str:
    return hashlib.sha256(json.dumps(experiment_config(), sort_keys=True).encode()).hexdigest()[:16]


def stake_for_balance(balance: float) -> float:
    if not math.isfinite(balance) or balance <= 0:
        raise ValueError("A positive finite demo balance is required")
    amount = min(balance * settings.MACS_RISK_PER_TRADE, settings.MACS_MAX_STAKE)
    stake = float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_DOWN))
    if stake < settings.MACS_MIN_STAKE:
        raise ValueError("Risk budget is below the minimum stake; skip instead of rounding up")
    return stake


def hourly_trend(candles: pd.DataFrame) -> pd.Series:
    """Return trend at each 15m close using only complete, already closed hours.

    Input candle indexes are opening times. Missing quarters invalidate an hour;
    old hourly values are not carried across gaps or market closures.
    """
    groups = candles.resample("1h", label="right", closed="left")
    counts = groups["Close"].count()
    closes = groups["Close"].last().where(counts == 4)
    fast = closes.ewm(span=12, adjust=False, min_periods=26).mean()
    slow = closes.ewm(span=26, adjust=False, min_periods=26).mean()
    valid = closes.notna().rolling(26).sum() == 26
    trend = pd.Series("unknown", index=closes.index)
    trend.loc[valid & (fast > slow)] = "bullish"
    trend.loc[valid & (fast < slow)] = "bearish"
    at_close = (candles.index + pd.Timedelta(minutes=15)).floor("1h")
    return pd.Series(trend.reindex(at_close).fillna("unknown").to_numpy(), index=candles.index)


def signal_filter(rule: MarketRule, direction: str, hourly: str) -> str | None:
    if not rule.enabled:
        return "OBSERVATION_ONLY"
    if direction not in rule.directions:
        return "DIRECTION_BLOCKED"
    expected = "bullish" if direction == "BUY" else "bearish"
    if rule.hourly_confirmation and hourly != expected:
        return "HOURLY_FILTER"
    return None
