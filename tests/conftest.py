import os
import tempfile

import pytest

# settings reads DATABASE_URL once, at import. Point it at a throwaway SQLite
# file before any test imports the app, so the suite never writes to the live
# macs.db or to Supabase. A real env var wins over .env for both load_dotenv
# and pydantic-settings.
os.environ["DATABASE_URL"] = "sqlite:///" + os.path.join(tempfile.mkdtemp(prefix="macs-tests-"), "test.db")


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
