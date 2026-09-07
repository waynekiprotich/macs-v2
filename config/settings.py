import os
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Dict, Any
from dotenv import load_dotenv
import logging

logger = logging.getLogger(__name__)

try:
    load_dotenv()
except Exception as e:
    logger.warning(f"Could not load .env file: {e}")

class Settings(BaseSettings):
    ALPACA_API_KEY: str = ""
    ALPACA_SECRET_KEY: str = ""
    ALPACA_PAPER: bool = True
    MACS_MODE: str = "PAPER"
    MACS_INTERVAL_MINUTES: int = 5
    MACS_MAX_CONSECUTIVE_LOSSES: int = 3
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
    DATABASE_URL: str = "sqlite:///macs.db"
    DISCORD_WEBHOOK_URL: str = ""

    HIGH_WIN_CONFIG: Dict[str, Any] = {
        "profit_target_pct": 0.05,
        "stop_loss_pct": 0.02,
        "trailing_stop_pct": 0.015,
        "max_risk_per_trade_pct": 0.01
    }

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

try:
    settings = Settings()
except Exception as e:
    logger.error(f"Error loading settings: {e}")
    raise
