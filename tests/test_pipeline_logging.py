from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from core import pipeline as pipeline_module
from core.instance_lock import trading_lock
from core.pipeline import TradingPipeline, _closed_bars
from models.database import MarketSnapshot, PaperTrade, RiskEvent, SessionLocal, SystemLog, engine

SYMBOL = "TEST_PIPE"
EVALUATED = []


def _candles(n: int = 400, last_open=None) -> pd.DataFrame:
    rng = np.random.default_rng(1)
    close = 100 + np.cumsum(rng.normal(0, 0.5, n))
    open_ = np.r_[close[0], close[:-1]]
    index = (pd.date_range(end=last_open, periods=n, freq="15min") if last_open is not None
             else pd.date_range("2026-09-01", periods=n, freq="15min"))
    return pd.DataFrame(
        {"Open": open_, "High": np.maximum(open_, close) + 0.2, "Low": np.minimum(open_, close) - 0.2,
         "Close": close, "Volume": 1000.0},
        index=index,
    )


class FakeEngine:
    calls = []

    def reconcile_open_contracts(self):
        pass

    def execute_signal(self, **kwargs):
        FakeEngine.calls.append(kwargs)
        return {"status": "success"}


@pytest.fixture
def run_pipeline(monkeypatch, clean_trading_tables):
    candles = _candles()
    monkeypatch.setattr(pipeline_module.DerivDataProvider, "fetch_data", lambda self, symbol: candles)
    monkeypatch.setattr("execution.deriv_engine.DerivEngine", FakeEngine)
    FakeEngine.calls = []
    EVALUATED.clear()

    def run(signal: str, symbols=(SYMBOL,)) -> dict:
        def generate_signal(row, **kw):
            EVALUATED.append(row.name)
            return {
                "signal": signal, "confidence": 75.0, "bull_conditions": 6, "bear_conditions": 1,
                "take_profit": None, "stop_loss": None, "reason": "test",
            }

        monkeypatch.setattr(pipeline_module.technical_strategy, "generate_signal", generate_signal)
        return TradingPipeline(list(symbols)).run(execute=True)

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


def test_closed_bars_are_stored_once(run_pipeline):
    run_pipeline("HOLD")
    db = SessionLocal()
    try:
        stored = db.query(MarketSnapshot).filter_by(symbol=SYMBOL).count()
        latest_bar = db.query(MarketSnapshot).filter_by(symbol=SYMBOL).order_by(MarketSnapshot.candle_time.desc()).first()
        evaluated_bar_time = _latest_signal(db).candle_time
    finally:
        db.close()
    assert stored > 0
    # Every bar passed in has closed, including the one the signal was evaluated on.
    assert latest_bar.candle_time == evaluated_bar_time

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


def test_closed_bars_drops_only_bars_that_have_not_closed():
    df = _candles(n=4, last_open=pd.Timestamp("2026-09-15 11:00"))

    mid_bar = _closed_bars(df, 900, datetime(2026, 9, 15, 11, 7, tzinfo=timezone.utc))
    at_close = _closed_bars(df, 900, datetime(2026, 9, 15, 11, 15, tzinfo=timezone.utc))

    assert mid_bar.index[-1] == pd.Timestamp("2026-09-15 10:45")
    assert at_close.index[-1] == pd.Timestamp("2026-09-15 11:00")


def test_signal_is_evaluated_on_the_last_closed_bar_not_the_forming_one(run_pipeline, monkeypatch):
    forming_open = pd.Timestamp("2026-09-15 11:00")
    last_closed = forming_open - pd.Timedelta(minutes=15)
    candles = _candles(last_open=forming_open)
    monkeypatch.setattr(pipeline_module.DerivDataProvider, "fetch_data", lambda self, symbol: candles)
    monkeypatch.setattr(pipeline_module, "_now", lambda: datetime(2026, 9, 15, 11, 7, tzinfo=timezone.utc))

    run_pipeline("BUY", symbols=("TEST_FORMING",))

    assert EVALUATED == [last_closed]
    assert FakeEngine.calls[-1]["candle_time"] == last_closed.tz_localize("UTC").to_pydatetime()
    db = SessionLocal()
    try:
        newest = db.query(MarketSnapshot).filter_by(symbol="TEST_FORMING").order_by(MarketSnapshot.candle_time.desc()).first()
    finally:
        db.close()
    assert pd.Timestamp(newest.candle_time) == last_closed


def test_unrecorded_signal_blocks_the_trade(run_pipeline, monkeypatch):
    monkeypatch.setattr(TradingPipeline, "_log_signal", lambda self, *args, **kwargs: None)

    outcome = run_pipeline("BUY")[SYMBOL]

    assert outcome["action"] == "HOLD" and "not recorded" in outcome["reason"]
    assert FakeEngine.calls == []


def test_contracts_are_reconciled_before_the_risk_state_that_gates_trading(run_pipeline, monkeypatch):
    order = []
    load_state = pipeline_module.RiskManager._load_state
    monkeypatch.setattr(FakeEngine, "reconcile_open_contracts", lambda self: order.append("reconcile"))

    def recording_load(self):
        order.append("load")
        load_state(self)

    monkeypatch.setattr(pipeline_module.RiskManager, "_load_state", recording_load)
    run_pipeline("BUY")

    # __init__ loads once; the load that gates trading follows reconciliation,
    # and the state is read again right before the trade.
    assert order == ["load", "reconcile", "load", "load"]
    assert len(FakeEngine.calls) == 1


def test_second_worker_is_blocked_while_the_trading_lock_is_held(run_pipeline):
    with trading_lock(engine) as acquired:
        assert acquired
        outcome = run_pipeline("BUY")[SYMBOL]

    assert outcome["action"] == "BLOCKED" and "Another MACS worker" in outcome["reason"]
    assert FakeEngine.calls == []
    assert run_pipeline("BUY")[SYMBOL]["action"] == "BUY"


def test_reconciliation_required_halts_the_rest_of_the_cycle(run_pipeline, monkeypatch):
    def unrecorded(self, **kwargs):
        FakeEngine.calls.append(kwargs)
        return {"status": "reconciliation_required", "contract_id": 555001}

    monkeypatch.setattr(FakeEngine, "execute_signal", unrecorded)

    outcomes = run_pipeline("BUY", symbols=(SYMBOL, "TEST_PIPE_2"))

    assert outcomes[SYMBOL]["action"] == "RECONCILIATION_REQUIRED"
    assert outcomes["TEST_PIPE_2"]["reason"] == "Not yet processed"
    assert len(FakeEngine.calls) == 1
    db = SessionLocal()
    try:
        assert _latest_signal(db).action_taken == "RECONCILIATION_REQUIRED"
    finally:
        db.close()


def _add_trade(**fields):
    db = SessionLocal()
    try:
        trade = PaperTrade(symbol="TEST_EXPIRY", side="BUY", quantity=170.0, price=170.0, **fields)
        db.add(trade)
        db.commit()
        return trade.contract_id
    finally:
        db.close()


def _settle(contract_id: str, pnl: float):
    db = SessionLocal()
    try:
        trade = db.query(PaperTrade).filter_by(contract_id=contract_id).one()
        trade.status, trade.pnl, trade.result = "CLOSED", pnl, "WON" if pnl > 0 else "LOST"
        db.commit()
    finally:
        db.close()


def _fail_reconciliation(self):
    raise RuntimeError("Reconciliation failed to get OTP.")


@pytest.mark.parametrize("reconcile", [_fail_reconciliation, lambda self: None],
                         ids=["reconciliation raises", "contract left OPEN"])
def test_expired_open_contract_that_is_not_reconciled_blocks_every_trade(run_pipeline, monkeypatch, reconcile):
    now = datetime.now(timezone.utc)
    _add_trade(status="OPEN", contract_id="900001", timestamp=now - timedelta(minutes=16),
               expiry_time=now - timedelta(seconds=30))
    monkeypatch.setattr(FakeEngine, "reconcile_open_contracts", reconcile)

    first = run_pipeline("BUY")[SYMBOL]
    second = run_pipeline("BUY")[SYMBOL]

    for outcome in (first, second):
        assert outcome["action"] == "BLOCKED" and "at or past expiry" in outcome["reason"]
    assert FakeEngine.calls == []


def test_expired_open_contract_that_reconciles_is_evaluated_normally(run_pipeline, monkeypatch):
    now = datetime.now(timezone.utc)
    contract = _add_trade(status="OPEN", contract_id="900002", timestamp=now - timedelta(minutes=16),
                          expiry_time=now - timedelta(seconds=30))
    monkeypatch.setattr(FakeEngine, "reconcile_open_contracts", lambda self: _settle(contract, 136.0))

    assert run_pipeline("BUY")[SYMBOL]["action"] == "BUY"
    assert len(FakeEngine.calls) == 1


def test_reconciled_loss_counts_toward_the_circuit_breaker_before_the_next_trade(run_pipeline, monkeypatch):
    now = datetime.now(timezone.utc)
    _add_trade(status="CLOSED", pnl=-100.0, result="LOST", contract_id="900003", timestamp=now - timedelta(minutes=46))
    _add_trade(status="CLOSED", pnl=-100.0, result="LOST", contract_id="900004", timestamp=now - timedelta(minutes=31))
    contract = _add_trade(status="OPEN", contract_id="900005", timestamp=now - timedelta(minutes=16),
                          expiry_time=now - timedelta(seconds=30))
    monkeypatch.setattr(FakeEngine, "reconcile_open_contracts", lambda self: _settle(contract, -100.0))

    outcome = run_pipeline("BUY")[SYMBOL]

    assert outcome["action"] == "BLOCKED" and "Circuit breaker" in outcome["reason"]
    assert FakeEngine.calls == []
