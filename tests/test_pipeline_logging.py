from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from core import pipeline as pipeline_module
from core.pipeline import TradingPipeline
from models.database import MarketSnapshot, RiskEvent, SessionLocal, SystemLog, init_db

SYMBOL = "TEST_PIPE"


def _candles(n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(1)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame(
        {"Open": open_, "High": np.maximum(open_, close) + 0.2, "Low": np.minimum(open_, close) - 0.2,
         "Close": close, "Volume": 1000.0},
        index=pd.date_range("2026-09-01", periods=n, freq="15min"),
    )


class FakeEngine:
    calls = []

    def reconcile_open_contracts(self):
        pass

    def execute_signal(self, **kwargs):
        FakeEngine.calls.append(kwargs)
        return {"status": "success"}


@pytest.fixture
def run_pipeline(monkeypatch):
    init_db()
    candles = _candles()
    monkeypatch.setattr(pipeline_module.DerivDataProvider, "fetch_data", lambda self, symbol: candles)
    monkeypatch.setattr("execution.deriv_engine.DerivEngine", FakeEngine)
    FakeEngine.calls = []

    def run(signal: str) -> dict:
        monkeypatch.setattr(pipeline_module.technical_strategy, "generate_signal", lambda row, **kw: {
            "signal": signal, "confidence": 75.0, "bull_conditions": 6, "bear_conditions": 1,
            "take_profit": None, "stop_loss": None, "reason": "test",
        })
        return TradingPipeline([SYMBOL]).run(execute=True)

    return run


def _latest_signal(db):
    return db.query(SystemLog).filter_by(symbol=SYMBOL).order_by(SystemLog.id.desc()).first()


def test_executed_signal_stores_features_and_links_the_trade(run_pipeline):
    assert run_pipeline("BUY")[SYMBOL]["action"] == "BUY"

    db = SessionLocal()
    try:
        sig = _latest_signal(db)
        assert sig.action_taken == "EXECUTED"
        assert (sig.granularity, sig.bull_conditions, sig.min_conditions, sig.strategy) == (900, 6, 6, "technical_strategy")
        assert sig.close_price == pytest.approx(sig.indicators["Close"])
        assert not any(isinstance(v, float) and v != v for v in sig.indicators.values())  # no NaN for JSONB
    finally:
        db.close()
    assert FakeEngine.calls[-1]["signal_id"] == sig.id


def test_closed_bars_are_stored_once_and_forming_bar_is_not(run_pipeline):
    run_pipeline("HOLD")
    db = SessionLocal()
    try:
        stored = db.query(MarketSnapshot).filter_by(symbol=SYMBOL).count()
        latest_bar = db.query(MarketSnapshot).filter_by(symbol=SYMBOL).order_by(MarketSnapshot.candle_time.desc()).first()
        forming_bar_time = _latest_signal(db).candle_time
    finally:
        db.close()
    assert stored > 0
    assert latest_bar.candle_time < forming_bar_time

    run_pipeline("HOLD")
    db = SessionLocal()
    try:
        assert db.query(MarketSnapshot).filter_by(symbol=SYMBOL).count() == stored
        assert _latest_signal(db).action_taken in ("HOLD", "VOLATILITY_SKIP")
    finally:
        db.close()


def test_circuit_breaker_is_recorded_once_per_cooldown(run_pipeline, monkeypatch):
    cooldown_until = datetime.now(timezone.utc) + timedelta(hours=3)

    def tripped(self):
        self.consecutive_losses = 3
        self.cooldown_until = cooldown_until

    monkeypatch.setattr(pipeline_module.RiskManager, "_load_state", tripped)
    assert run_pipeline("BUY")[SYMBOL]["action"] == "BLOCKED"
    run_pipeline("BUY")

    db = SessionLocal()
    try:
        events = db.query(RiskEvent).filter_by(event_type="CIRCUIT_BREAKER").all()
    finally:
        db.close()
    assert len(events) == 1
    assert (events[0].threshold, events[0].observed) == (3.0, 3.0)
    assert FakeEngine.calls == []
