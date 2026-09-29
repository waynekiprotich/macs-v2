import os
import tempfile

import pytest

# settings reads DATABASE_URL once, at import. Point it at a throwaway SQLite
# file before any test imports the app, so the suite never writes to the live
# macs.db or to Supabase. A real env var wins over .env for both load_dotenv
# and pydantic-settings.
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="macs-tests-"), "test.db")


@pytest.fixture(autouse=True)
def no_external_services(monkeypatch):
    """Tests may mock transports explicitly; never reach accounts or webhooks."""
    import requests
    import websockets

    def blocked(*args, **kwargs):
        raise AssertionError("External network access is forbidden in tests")

    monkeypatch.setattr(requests.sessions.Session, "request", blocked)
    monkeypatch.setattr(websockets, "connect", blocked)


@pytest.fixture
def legacy_execution_config(monkeypatch):
    """Original six-condition fixtures test plumbing, not the candidate defaults."""
    from config.settings import settings, MarketRule
    symbols = ["OTC_DJI", "TEST_PIPE", "TEST_PIPE_2", "TEST_FORMING", "TEST_RERUN", "TEST_AMB_1", "TEST_AMB_2"]
    monkeypatch.setattr(settings, "MACS_EXECUTION_ENABLED", True)
    monkeypatch.setattr(settings, "MACS_MODE", "PAPER")
    monkeypatch.setattr(settings, "MACS_MAX_STAKE", 170.0)
    monkeypatch.setattr(settings, "MACS_MARKET_RULES", {
        s: MarketRule(enabled=True, directions=["BUY", "SELL"], min_conditions=6, cooldown_bars=0)
        for s in symbols
    })


@pytest.fixture
def clean_trading_tables():
    """Trade intents and OPEN trades block RiskManager for every test sharing
    this database, so tests that create them start and end with none."""
    from models.database import PaperTrade, SessionLocal, TradeIntent, init_db

    init_db()

    def clear():
        db = SessionLocal()
        try:
            db.query(TradeIntent).delete()
            db.query(PaperTrade).delete()
            db.commit()
        finally:
            db.close()

    clear()
    yield
    clear()
