from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.exc import OperationalError

from core import risk_management
from core.risk_management import RiskManager
from models.database import PaperTrade, SessionLocal, TradeIntent

pytestmark = pytest.mark.usefixtures("clean_trading_tables")


def _add(*rows):
    db = SessionLocal()
    try:
        db.add_all(rows)
        db.commit()
    finally:
        db.close()


def _db_down():
    raise OperationalError("SELECT", {}, Exception("connection refused"))


def test_readable_clean_state_allows_trading():
    assert RiskManager().can_trade()["allowed"]


def test_database_error_blocks_trading_instead_of_assuming_a_clean_account(monkeypatch):
    monkeypatch.setattr(risk_management, "SessionLocal", _db_down)

    manager = RiskManager()
    status = manager.can_trade()

    assert not status["allowed"] and "Risk state unavailable" in status["reason"]
    assert manager.daily_pnl is None and manager.consecutive_losses is None


def test_failed_reload_blocks_after_an_earlier_good_load(monkeypatch):
    manager = RiskManager()
    assert manager.can_trade()["allowed"]

    monkeypatch.setattr(risk_management, "SessionLocal", _db_down)
    assert manager.reload() is False
    assert not manager.can_trade()["allowed"]


def test_stale_state_blocks_trading():
    manager = RiskManager()
    manager.state_loaded_at -= RiskManager.MAX_STATE_AGE + timedelta(seconds=1)

    status = manager.can_trade()
    assert not status["allowed"] and "stale" in status["reason"]


@pytest.mark.parametrize("status, blocks", [
    ("PENDING", True), ("AMBIGUOUS", True), ("UNRECORDED", True),
    ("FAILED", False), ("EXECUTED", False), ("RESOLVED", False),
])
def test_unresolved_trade_intents_block_trading(status, blocks):
    _add(TradeIntent(symbol="TEST_RISK", side="BUY", candle_time=datetime(2026, 9, 15, 11, tzinfo=timezone.utc),
                     stake=170.0, status=status))
    assert RiskManager().can_trade()["allowed"] is not blocks


@pytest.mark.parametrize("expires_in, blocks", [
    (timedelta(hours=-1), True),
    (timedelta(seconds=-5), True),    # just expired: Deriv may not have settled it yet
    (timedelta(seconds=10), True),    # inside the clock-skew margin
    (None, True),                     # no recorded expiry
    (timedelta(minutes=5), False),    # still running: no settled outcome to miss
], ids=["an hour past", "seconds past", "within margin", "no expiry", "running"])
def test_open_contract_at_or_past_expiry_blocks_trading(expires_in, blocks):
    expiry = None if expires_in is None else datetime.now(timezone.utc) + expires_in
    _add(PaperTrade(symbol="TEST_RISK", side="BUY", quantity=170.0, price=170.0, status="OPEN", contract_id="42",
                    expiry_time=expiry))

    status = RiskManager().can_trade()
    assert status["allowed"] is not blocks
    if blocks:
        assert "at or past expiry" in status["reason"]


def test_unavailable_state_is_recorded_as_a_risk_event(monkeypatch):
    manager = RiskManager()
    manager.state_loaded_at = None
    manager.load_error = "connection refused"
    manager.record_block()

    from models.database import RiskEvent
    db = SessionLocal()
    try:
        assert db.query(RiskEvent).filter_by(event_type="RISK_STATE_UNAVAILABLE").count() == 1
    finally:
        db.close()
