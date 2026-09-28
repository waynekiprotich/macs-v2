import os
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Dict, Any
from dotenv import load_dotenv
import logging

logger = logging.getLogger(__name__)


class MarketRule(BaseModel):
    """An explicitly approved demo experiment, never an inferred edge."""
    enabled: bool = False
    directions: list[str] = Field(default_factory=lambda: ["BUY"])
    min_conditions: int = Field(default=7, ge=1, le=8)
    min_payout: float = Field(default=0.80, ge=0, le=10, allow_inf_nan=False)
    duration_minutes: int = Field(default=15, ge=15, le=120)
    hourly_confirmation: bool = False
    cooldown_bars: int = Field(default=1, ge=0, le=96)

    @field_validator("directions")
    @classmethod
    def valid_directions(cls, value):
        if not value or any(v not in ("BUY", "SELL") for v in value):
            raise ValueError("directions must contain BUY and/or SELL")
        return list(dict.fromkeys(value))

    @field_validator("duration_minutes")
    @classmethod
    def aligned_duration(cls, value):
        if value % 15:
            raise ValueError("duration_minutes must be a multiple of 15")
        return value

try:
    load_dotenv()
except Exception as e:
    logger.warning(f"Could not load .env file: {e}")


def normalize_database_url(url: str) -> str:
    """Make a connection string pasted from Supabase usable by SQLAlchemy."""
    # SQLAlchemy 2.x rejects the postgres:// scheme some dashboards hand out.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    # Supabase is reached over the public internet: never send the password in cleartext.
    if url.startswith("postgresql") and "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"
    return url


class Settings(BaseSettings):
    # Disabled until the operator reviews the experiment and explicitly enables it.
    MACS_EXECUTION_ENABLED: bool = False
    MACS_EXPERIMENT_ID: str = "otc-buy-7of8-v1"
    MACS_MARKET_RULES: Dict[str, MarketRule] = {
        "OTC_DJI": MarketRule(enabled=True),
        "frxXAUUSD": MarketRule(enabled=False),
    }
    MACS_RISK_PER_TRADE: float = Field(default=0.0025, gt=0, le=0.01, allow_inf_nan=False)
    MACS_MAX_OPEN_EXPOSURE: float = Field(default=0.01, gt=0, le=0.01, allow_inf_nan=False)
    MACS_MAX_OPEN_TRADES: int = Field(default=1, ge=1, le=10)
    MACS_DAILY_LOSS_FRACTION: float = Field(default=0.02, gt=0, le=0.02, allow_inf_nan=False)
    MACS_MAX_DAILY_LOSS: float = Field(default=500.0, gt=0, allow_inf_nan=False)
    MACS_COOLDOWN_HOURS: float = Field(default=4.0, ge=0, allow_inf_nan=False)
    MACS_MIN_STAKE: float = Field(default=0.35, gt=0, allow_inf_nan=False)
    MACS_MAX_STAKE: float = Field(default=25.0, gt=0, allow_inf_nan=False)
    ALPACA_API_KEY: str = ""
    ALPACA_SECRET_KEY: str = ""
    ALPACA_PAPER: bool = True
    MACS_MODE: str = "PAPER"
    MACS_INTERVAL_MINUTES: int = 5
    MACS_MAX_CONSECUTIVE_LOSSES: int = Field(default=3, ge=1)
    MACS_MIN_CONFIDENCE_SCORE: float = 80.0
    # Pure technical strategy (core/technical_strategy.py): out of 8 symmetric
    # conditions, how many must agree before a BUY/SELL fires. Tune this with
    # `python cli.py backtest` — this is the single biggest lever on win rate
    # vs. trade frequency.
    MACS_MIN_CONDITIONS: int = 6
    # Contract duration sent to Deriv. This was hardcoded to 15m in
    # deriv_engine._execute_contract(); it lives here so the alert can report
    # the same number the contract actually used instead of a duplicated
    # literal. Changing it invalidates the backtest math in cli.py, which
    # assumes one 15m bar == one contract — re-run --duration-sweep first.
    MACS_CONTRACT_DURATION: int = 15
    MACS_CONTRACT_DURATION_UNIT: str = "m"
    # SQLite locally; Supabase Postgres in production. For Supabase use the
    # Session pooler URI (Project Settings -> Database -> Connection string):
    # user postgres.<project-ref>, host aws-0-<region>.pooler.supabase.com, port 5432.
    # The direct db.<project-ref>.supabase.co host is IPv6-only and can't be
    # reached from an IPv4-only network. Run `alembic upgrade head` against a
    # database before pointing the app at it.
    DATABASE_URL: str = "sqlite:///macs.db"
    # Postgres connection pool per process. Supabase's pooler caps clients per
    # project, and the daemon and API server each hold their own pool.
    DB_POOL_SIZE: int = 5
    DB_MAX_OVERFLOW: int = 5
    DISCORD_WEBHOOK_URL: str = ""

    HIGH_WIN_CONFIG: Dict[str, Any] = {
        "profit_target_pct": 0.05,
        "stop_loss_pct": 0.02,
        "trailing_stop_pct": 0.015,
        "max_risk_per_trade_pct": 0.01
    }

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @field_validator("DATABASE_URL")
    @classmethod
    def _normalize_database_url(cls, url: str) -> str:
        return normalize_database_url(url)

try:
    settings = Settings()
except Exception as e:
    logger.error(f"Error loading settings: {e}")
    raise
