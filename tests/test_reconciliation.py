import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from execution import deriv_engine
from execution.deriv_engine import DerivEngine
from models.database import PaperTrade, SessionLocal

FIXTURES = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.usefixtures("clean_trading_tables")


def _reply(name):
    """A proposal_open_contract reply recorded from the Options API (demo account)."""
    return json.loads((FIXTURES / f"deriv_proposal_open_contract_{name}.json").read_text())


class FakeDeriv:
    """Answers proposal_open_contract with a recorded reply, addressed to the
    request's req_id. Any other kind of request fails the test."""

    def __init__(self, replies):
        self.replies = replies
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def send(self, data):
        request = json.loads(data)
        assert set(request) == {"proposal_open_contract", "contract_id", "req_id"}, request
        self.sent.append(request)

    async def recv(self):
        request = self.sent[-1]
        reply = self.replies[request["contract_id"]]
        if callable(reply):
            return json.dumps(reply(request))
        return json.dumps(dict(reply, echo_req=request, req_id=request["req_id"]))


@pytest.fixture
def deriv(monkeypatch):
    def install(replies):
        fake = FakeDeriv(replies)
        otp = MagicMock(status_code=200)
        otp.json.return_value = {"data": {"url": "wss://fake"}}
        monkeypatch.setattr(deriv_engine.requests, "post", lambda *a, **kw: otp)
        monkeypatch.setattr(deriv_engine.websockets, "connect", lambda *a, **kw: fake)
        return fake

    return install


def _open_trade(info, proposal_spot=4283.74):
    """An OPEN row as execute_signal writes it: payout and quoted_payout hold
    the quote, entry_spot the proposal-time spot."""
    quote = float(info["payout"])
    db = SessionLocal()
    try:
        db.add(PaperTrade(symbol="frxXAUUSD", side="BUY", quantity=170.0, price=170.0, status="OPEN",
                          contract_id=str(info["contract_id"]), payout=quote, quoted_payout=quote,
                          entry_spot=proposal_spot, contract_type="CALL", duration=15, duration_unit="m"))
        db.commit()
    finally:
        db.close()


def _trade(contract_id):
    db = SessionLocal()
    try:
        return db.query(PaperTrade).filter_by(contract_id=str(contract_id)).one()
    finally:
        db.close()


@pytest.mark.parametrize("name, result, numbers", [
    ("won", "WON", dict(pnl=116.59, sell_price=286.59, payout=286.59, quoted_payout=286.59,
                        entry_spot=4283.78, exit_spot=4289.93)),
    ("lost", "LOST", dict(pnl=-170.0, sell_price=0.0, payout=295.76, quoted_payout=295.76,
                          entry_spot=4291.95, exit_spot=4289.36)),
])
def test_settled_contract_records_outcome_spots_and_keeps_the_payout(deriv, name, result, numbers):
    reply = _reply(name)
    info = reply["proposal_open_contract"]
    _open_trade(info)
    fake = deriv({info["contract_id"]: reply})

    DerivEngine().reconcile_open_contracts()

    trade = _trade(info["contract_id"])
    assert (trade.status, trade.result) == ("CLOSED", result)
    assert {key: getattr(trade, key) for key in numbers} == pytest.approx(numbers)
    assert trade.closed_timestamp is not None
    assert [r["req_id"] for r in fake.sent] == [1000]


def test_exit_spot_stays_null_when_deriv_does_not_report_one(deriv):
    reply = _reply("won")
    info = reply["proposal_open_contract"]
    for key in ("exit_spot", "exit_spot_time"):
        info.pop(key)
    _open_trade(info)
    deriv({info["contract_id"]: reply})

    DerivEngine().reconcile_open_contracts()

    trade = _trade(info["contract_id"])
    assert trade.status == "CLOSED" and trade.exit_spot is None


def test_older_v3_field_names_are_still_read(deriv):
    reply = _reply("lost")
    info = reply["proposal_open_contract"]
    info["exit_tick"] = float(info.pop("exit_spot"))
    info["entry_tick"] = float(info.pop("entry_spot"))
    _open_trade(info)
    deriv({info["contract_id"]: reply})

    DerivEngine().reconcile_open_contracts()

    trade = _trade(info["contract_id"])
    assert (trade.exit_spot, trade.entry_spot) == (4289.36, 4291.95)


def test_settled_reply_without_a_profit_figure_leaves_the_contract_open(deriv):
    reply = _reply("won")
    info = reply["proposal_open_contract"]
    info.pop("profit")
    _open_trade(info)
    deriv({info["contract_id"]: reply})

    DerivEngine().reconcile_open_contracts()

    trade = _trade(info["contract_id"])
    assert (trade.status, trade.pnl) == ("OPEN", None)


def test_reply_to_a_different_request_is_not_used_to_settle(deriv):
    reply = _reply("won")
    info = reply["proposal_open_contract"]
    _open_trade(info)
    deriv({info["contract_id"]: lambda request: dict(reply, echo_req=request, req_id=request["req_id"] + 1)})

    DerivEngine().reconcile_open_contracts()

    assert _trade(info["contract_id"]).status == "OPEN"


def test_error_reply_leaves_the_contract_open(deriv):
    reply = _reply("won")
    info = reply["proposal_open_contract"]
    _open_trade(info)
    deriv({info["contract_id"]: lambda request: {
        "echo_req": request, "msg_type": "proposal_open_contract", "req_id": request["req_id"],
        "error": {"code": "RateLimit", "message": "Rate limit reached"},
    }})

    DerivEngine().reconcile_open_contracts()

    assert _trade(info["contract_id"]).status == "OPEN"
