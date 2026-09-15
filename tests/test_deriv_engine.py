import asyncio
import json
import logging
from datetime import datetime, timezone
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest
from sqlalchemy.exc import OperationalError

from core import pipeline as pipeline_module
from core.pipeline import TradingPipeline
from core.risk_management import RiskManager
from execution import deriv_engine
from execution.deriv_engine import DerivEngine
from models.database import PaperTrade, SessionLocal, TradeIntent

CANDLE = datetime(2026, 9, 15, 11, 0, tzinfo=timezone.utc)
BUY_OK = {"buy": {"contract_id": 555001, "buy_price": 170.0}}


class FakeDeriv:
    """Deriv's websocket: records every request, answers the proposal, and
    answers the buy with a reply dict, or hangs when buy_reply is "hang"."""

    def __init__(self, buy_reply):
        self.buy_reply = buy_reply
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        if "proposal" in self.sent[-1]:
            return json.dumps({"proposal": {"id": "prop1", "payout": 306.0, "spot": 45000.0}})
        if self.buy_reply == "hang":
            await asyncio.sleep(5)
        return json.dumps(self.buy_reply)

    @property
    def buys(self):
        return [m for m in self.sent if "buy" in m]


@pytest.fixture
def deriv(monkeypatch, clean_trading_tables):
    http_calls = []

    def install(buy_reply):
        fake = FakeDeriv(buy_reply)
        otp = MagicMock(status_code=200)
        otp.json.return_value = {"data": {"url": "wss://fake"}}

        def post(*args, **kwargs):
            http_calls.append(kwargs)
            return otp

        monkeypatch.setattr(deriv_engine.requests, "post", post)
        monkeypatch.setattr(deriv_engine.websockets, "connect", lambda *a, **kw: fake)
        monkeypatch.setattr(deriv_engine, "send_discord_signal", lambda **kw: None)
        monkeypatch.setattr(deriv_engine, "send_heartbeat", lambda **kw: None)
        fake.http_calls = http_calls
        return fake

    return install


def _buy(candle_time=CANDLE):
    return DerivEngine().execute_signal("OTC_DJI", "BUY", 170.0, 45000.0, "test", candle_time=candle_time)


def _intents():
    db = SessionLocal()
    try:
        return db.query(TradeIntent).all()
    finally:
        db.close()


def _trades():
    db = SessionLocal()
    try:
        return db.query(PaperTrade).all()
    finally:
        db.close()


def _db_down(*args, **kwargs):
    raise OperationalError("INSERT INTO trades", {}, Exception("server closed the connection unexpectedly"))


def test_successful_buy_is_recorded_and_resolves_its_intent(deriv):
    fake = deriv(BUY_OK)

    result = _buy()

    assert result["status"] == "success" and result["contract_id"] == 555001
    [trade] = _trades()
    assert (trade.contract_id, trade.status, trade.price) == ("555001", "OPEN", 170.0)
    [intent] = _intents()
    assert (intent.status, intent.contract_id, intent.trade_id) == ("EXECUTED", "555001", trade.id)
    assert len(fake.buys) == 1
    assert RiskManager().can_trade()["allowed"]


def test_buy_that_cannot_be_recorded_blocks_trading_and_is_recorded_by_reconciliation(deriv, monkeypatch, caplog):
    fake = deriv(BUY_OK)
    record_trade = DerivEngine.__dict__["_record_trade"]  # the staticmethod itself, not the unwrapped function
    monkeypatch.setattr(DerivEngine, "_record_trade", staticmethod(_db_down))

    with caplog.at_level(logging.CRITICAL, logger="execution.deriv_engine"):
        result = _buy()

    assert result == {"status": "reconciliation_required", "intent_id": result["intent_id"], "contract_id": 555001}
    assert any("555001" in r.message and "RECONCILIATION REQUIRED" in r.message for r in caplog.records)
    assert _trades() == []
    [intent] = _intents()
    assert (intent.status, intent.contract_id, intent.details["price"]) == ("UNRECORDED", "555001", 170.0)
    blocked = RiskManager().can_trade()
    assert not blocked["allowed"] and "Reconciliation required" in blocked["reason"]

    # The database is back: reconciliation writes the row from the contract ID it
    # already has, without buying again.
    monkeypatch.setattr(DerivEngine, "_record_trade", record_trade)
    DerivEngine()._record_unrecorded_intents()

    [trade] = _trades()
    assert (trade.contract_id, trade.status, trade.symbol, trade.side) == ("555001", "OPEN", "OTC_DJI", "BUY")
    assert _intents()[0].status == "EXECUTED"
    assert len(fake.buys) == 1
    assert RiskManager().can_trade()["allowed"]


def test_database_fully_down_after_buy_keeps_contract_id_in_logs_and_trading_blocked(deriv, monkeypatch, caplog):
    deriv(BUY_OK)
    monkeypatch.setattr(DerivEngine, "_record_trade", staticmethod(_db_down))
    monkeypatch.setattr(DerivEngine, "_update_intent", staticmethod(lambda *a, **kw: False))

    with caplog.at_level(logging.CRITICAL, logger="execution.deriv_engine"):
        result = _buy()

    assert result["status"] == "reconciliation_required"
    assert any("Deriv contract 555001" in r.message for r in caplog.records)
    # The intent was committed before the buy, so it still blocks trading.
    [intent] = _intents()
    assert intent.status == "PENDING"
    assert not RiskManager().can_trade()["allowed"]


def test_ambiguous_buy_is_never_retried_and_blocks_trading(deriv, monkeypatch):
    fake = deriv("hang")
    monkeypatch.setattr(deriv_engine, "WS_TIMEOUT", 0.05)

    result = _buy()

    assert result["status"] == "reconciliation_required"
    assert len(fake.buys) == 1
    [intent] = _intents()
    assert intent.status == "AMBIGUOUS"
    assert not RiskManager().can_trade()["allowed"]
    assert all(call.get("timeout") for call in fake.http_calls)

    # The same signal again is refused before anything reaches Deriv.
    assert _buy()["status"] == "duplicate"
    assert len(fake.buys) == 1


def test_same_symbol_candle_and_direction_buys_once(deriv):
    fake = deriv(BUY_OK)

    assert _buy()["status"] == "success"
    assert _buy()["status"] == "duplicate"

    assert len(fake.buys) == 1
    assert len(_trades()) == 1


def test_no_buy_when_the_intent_cannot_be_recorded(deriv, monkeypatch):
    fake = deriv(BUY_OK)
    monkeypatch.setattr(deriv_engine, "SessionLocal", _db_down)

    result = _buy()

    assert result["status"] == "error"
    assert fake.sent == [] and fake.http_calls == []


def test_deriv_refusal_buys_nothing_and_does_not_block(deriv):
    fake = deriv({"error": {"code": "InvalidPrice", "message": "Price moved"},
                  "msg_type": "buy", "echo_req": {"buy": "prop1", "price": 170.0}})

    assert _buy()["status"] == "error"

    assert len(fake.buys) == 1
    assert _trades() == []
    assert _intents()[0].status == "FAILED"
    assert RiskManager().can_trade()["allowed"]


def test_missing_candle_time_refuses_to_trade(deriv):
    fake = deriv(BUY_OK)
    assert _buy(candle_time=None)["status"] == "error"
    assert fake.sent == [] and _intents() == []


def _pipeline_always_buys(monkeypatch):
    """Real DerivEngine against FakeDeriv; closed candles; every bar signals BUY."""
    rng = np.random.default_rng(2)
    close = 100 + np.cumsum(rng.normal(0, 0.5, 400))
    open_ = np.r_[close[0], close[:-1]]
    candles = pd.DataFrame(
        {"Open": open_, "High": np.maximum(open_, close) + 0.2, "Low": np.minimum(open_, close) - 0.2,
         "Close": close, "Volume": 1000.0},
        index=pd.date_range("2026-09-01", periods=400, freq="15min"),
    )
    monkeypatch.setattr(pipeline_module.DerivDataProvider, "fetch_data", lambda self, symbol: candles)
    monkeypatch.setattr(DerivEngine, "reconcile_open_contracts", lambda self: None)
    monkeypatch.setattr(pipeline_module.technical_strategy, "generate_signal", lambda row, **kw: {
        "signal": "BUY", "confidence": 75.0, "bull_conditions": 6, "bear_conditions": 1,
        "take_profit": None, "stop_loss": None, "reason": "test",
    })


def test_rerunning_a_cycle_on_the_same_closed_candle_buys_once(deriv, monkeypatch):
    fake = deriv(BUY_OK)
    _pipeline_always_buys(monkeypatch)

    first = TradingPipeline(["TEST_RERUN"]).run(execute=True)
    second = TradingPipeline(["TEST_RERUN"]).run(execute=True)

    assert first["TEST_RERUN"]["action"] == "BUY"
    assert second["TEST_RERUN"]["action"] == "DUPLICATE_SKIPPED"
    assert len(fake.buys) == 1


UNRELATED_ERROR = {"code": "RateLimit", "message": "Rate limit reached"}


@pytest.mark.parametrize("reply", [
    {"error": UNRELATED_ERROR},
    {"error": UNRELATED_ERROR, "msg_type": "proposal", "echo_req": {"proposal": 1}},
    {"error": UNRELATED_ERROR, "msg_type": "buy", "echo_req": {"buy": "another-proposal", "price": 170.0}},
], ids=["no identity", "other message type", "other proposal"])
def test_error_not_identifiable_as_the_buy_reply_is_ambiguous_not_a_refusal(deriv, reply):
    fake = deriv(reply)

    result = _buy()

    assert result["status"] == "reconciliation_required"
    [intent] = _intents()
    assert intent.status == "AMBIGUOUS" and "not identifiable" in intent.error
    assert len(fake.buys) == 1
    assert not RiskManager().can_trade()["allowed"]
    assert _buy()["status"] == "duplicate"
    assert len(fake.buys) == 1


def test_unrelated_error_after_buy_stops_trading_this_cycle_and_the_next(deriv, monkeypatch):
    fake = deriv({"error": UNRELATED_ERROR, "msg_type": "proposal"})
    _pipeline_always_buys(monkeypatch)

    first = TradingPipeline(["TEST_AMB_1", "TEST_AMB_2"]).run(execute=True)
    second = TradingPipeline(["TEST_AMB_1", "TEST_AMB_2"]).run(execute=True)

    assert first["TEST_AMB_1"]["action"] == "RECONCILIATION_REQUIRED"
    assert first["TEST_AMB_2"]["reason"] == "Not yet processed"
    assert all(o["action"] == "BLOCKED" and "Reconciliation required" in o["reason"] for o in second.values())
    assert len(fake.buys) == 1
