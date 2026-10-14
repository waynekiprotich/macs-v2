import time
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from config.settings import MarketRule, Settings, settings
from core import gold_london_mr as strat
from core import pipeline as pipeline_module
from core.pipeline import TradingPipeline
from core.risk_management import RiskManager
from execution.deriv_engine import DerivEngine
from models.database import MarketSnapshot, SessionLocal, SystemLog, TradeIntent
from research import lib as research_lib

SYMBOL = "frxXAUUSD"


def _bars(last_open: str, drift: float = 0.1, shock: float = -4.0, n: int = 900, seed: int = 5) -> pd.DataFrame:
    """Trending 5m path whose last three bars move `shock` each."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, 0.3, n)
    steps[-3:] = shock
    close = 2000 + np.cumsum(steps)
    open_ = np.r_[close[0], close[:-1]]
    index = pd.date_range(end=pd.Timestamp(last_open), periods=n, freq="5min")
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) + 0.1,
                         "Low": np.minimum(open_, close) - 0.1, "Close": close, "Volume": 1000.0}, index=index)


def _hourly(bars: pd.DataFrame) -> pd.DataFrame:
    """1h candles from the 5m path, including the still-forming last hour."""
    r = bars.resample("60min", label="left", closed="left")
    return pd.DataFrame({"Open": r["Open"].first(), "High": r["High"].max(), "Low": r["Low"].min(),
                         "Close": r["Close"].last()}).dropna()


def _mirror(bars: pd.DataFrame) -> pd.DataFrame:
    return pd.DataFrame({"Open": 4000 - bars["Open"], "High": 4000 - bars["Low"], "Low": 4000 - bars["High"],
                         "Close": 4000 - bars["Close"], "Volume": 1000.0}, index=bars.index)


def test_features_match_the_research_implementation():
    bars = _bars("2026-09-01 09:10", shock=0.0, n=3000)
    research = research_lib.indicator_frame(bars)
    ours = strat.features(bars)
    for col in ours.columns:
        np.testing.assert_allclose(ours[col].to_numpy(), research[col].to_numpy(), rtol=1e-10, equal_nan=True)
    hourly = _hourly(bars)
    np.testing.assert_allclose(strat.htf_slope(hourly).to_numpy(),
                               research_lib.indicator_frame(hourly)["slope_ema21"].to_numpy(), rtol=1e-10,
                               equal_nan=True)


def test_frozen_parameters_are_complete():
    p = strat.load_params()
    assert p["parts"] == ["dist_ema9", "rsi7", "stoch_k", "ret3", "pos20", "bb_pctb"]
    assert all(len(p["quantiles"][c]) == 1001 and np.all(np.diff(p["quantiles"][c]) >= 0) for c in p["parts"])
    assert p["score_low"] < 0 < p["score_high"]
    assert (p["session_utc"], p["bar_minutes"], p["duration_minutes"]) == ([6, 13], 5, 20)


@pytest.mark.parametrize("x,expected", [(-1.0, 0.0), (0.0, 0.0), (0.5, 0.125), (4.0, 1.0), (9.0, 1.0)])
def test_rank_is_the_fraction_below(x, expected):
    assert strat._rank(x, np.array([0.0, 1.0, 2.0, 3.0, 4.0])) == pytest.approx(expected)


def test_rank_of_a_repeated_value_is_its_lowest_position():
    assert strat._rank(1.0, np.array([0.0, 1.0, 1.0, 1.0, 2.0])) == pytest.approx(0.25)


def test_sharp_drop_in_an_hourly_uptrend_is_a_call():
    bars = _bars("2026-09-01 10:10")  # drop in the 10:00 hour, which is still forming at 10:15
    res = strat.generate_signal(bars, _hourly(bars))
    assert res["htf_slope"] > 0
    assert res["signal"] == "BUY", res["reason"]
    assert res["decision_time"] == pd.Timestamp("2026-09-01 10:15", tz="UTC")


def test_sharp_rally_in_an_hourly_downtrend_is_a_put():
    bars = _mirror(_bars("2026-09-01 10:10"))
    res = strat.generate_signal(bars, _hourly(bars))
    assert res["signal"] == "SELL", res["reason"]


def test_stretch_against_the_hourly_trend_holds():
    bars = _bars("2026-09-01 10:10", drift=-0.1)
    res = strat.generate_signal(bars, _hourly(bars))
    assert res["signal"] == "HOLD"
    assert "against the 1-hour trend" in res["reason"]


def test_outside_the_session_holds():
    bars = _bars("2026-09-01 14:10")
    res = strat.generate_signal(bars, _hourly(bars))
    assert res["signal"] == "HOLD" and res["reason"].startswith("Outside")


def test_a_forming_hourly_bar_is_ignored():
    bars = _bars("2026-09-01 10:10")
    hourly = _hourly(bars)
    closed_only = hourly[hourly.index + pd.Timedelta(hours=1) <= pd.Timestamp("2026-09-01 10:15")]
    crashed = hourly.copy()
    crashed.iloc[-1, crashed.columns.get_loc("Close")] -= 500  # the 10:00 hour has not closed
    assert strat.generate_signal(bars, crashed)["htf_slope"] == strat.generate_signal(bars, closed_only)["htf_slope"]


def test_too_little_history_holds():
    bars = _bars("2026-09-01 10:10", n=40)
    assert strat.generate_signal(bars, _hourly(bars))["signal"] == "HOLD"


GOLD_RULE = dict(enabled=True, directions=["BUY", "SELL"], bar_minutes=5, duration_minutes=20,
                 min_payout=0.73, cooldown_bars=0)


def test_market_rule_duration_must_fit_the_bar():
    assert MarketRule(**GOLD_RULE).duration_minutes == 20
    for bad in (dict(bar_minutes=5, duration_minutes=17), dict(bar_minutes=7, duration_minutes=21),
                dict(duration_minutes=20)):
        with pytest.raises(ValidationError):
            MarketRule(**bad)


def test_gold_strategy_needs_a_matching_gold_rule():
    good = Settings(MACS_STRATEGY="gold_london_mr", MACS_MARKET_RULES={SYMBOL: MarketRule(**GOLD_RULE)})
    assert good.cycle_seconds() == 300
    with pytest.raises(ValidationError):  # the default rules enable OTC_DJI on 15m bars
        Settings(MACS_STRATEGY="gold_london_mr")
    with pytest.raises(ValidationError):
        Settings(MACS_STRATEGY="gold_london_mr",
                 MACS_MARKET_RULES={SYMBOL: MarketRule(**{**GOLD_RULE, "duration_minutes": 15})})
    with pytest.raises(ValidationError):
        Settings(MACS_STRATEGY="technical", MACS_MARKET_RULES={SYMBOL: MarketRule(**GOLD_RULE)})
    with pytest.raises(ValidationError):
        Settings(MACS_STRATEGY="something_else")
    assert Settings().cycle_seconds() == 900


class FakeEngine:
    calls = []

    def reconcile_open_contracts(self):
        pass

    def execute_signal(self, **kwargs):
        FakeEngine.calls.append(kwargs)
        return {"status": "success"}


@pytest.fixture
def gold_pipeline(monkeypatch, clean_trading_tables):
    bars = _bars("2026-09-01 10:10")
    hourly = _hourly(bars)
    monkeypatch.setattr(settings, "MACS_STRATEGY", "gold_london_mr")
    monkeypatch.setattr(settings, "MACS_EXECUTION_ENABLED", True)
    monkeypatch.setattr(settings, "MACS_MODE", "PAPER")
    monkeypatch.setattr(settings, "MACS_MARKET_RULES", {SYMBOL: MarketRule(**GOLD_RULE)})
    requested = []

    def fetch(self, symbol, period="60d", interval="15m"):
        requested.append(interval)
        return {"5m": bars, "1h": hourly}[interval]

    monkeypatch.setattr(pipeline_module.DerivDataProvider, "fetch_data", fetch)
    monkeypatch.setattr("execution.deriv_engine.DerivEngine", FakeEngine)
    FakeEngine.calls = []

    def run(seconds_after_close: float):
        now = datetime(2026, 9, 1, 10, 15, tzinfo=timezone.utc) + timedelta(seconds=seconds_after_close)
        monkeypatch.setattr(pipeline_module, "_now", lambda: now)
        return TradingPipeline([SYMBOL]).run()

    run.requested = requested
    return run


def _latest_signal():
    with SessionLocal() as db:
        return db.query(SystemLog).filter_by(symbol=SYMBOL).order_by(SystemLog.id.desc()).first()


def test_gold_strategy_trades_its_own_candles_and_contract(gold_pipeline):
    assert gold_pipeline(10)[SYMBOL]["action"] == "BUY"
    assert gold_pipeline.requested == ["5m", "1h"]
    call = FakeEngine.calls[-1]
    assert (call["bar_seconds"], call["max_signal_age"]) == (300, 60)
    assert call["candle_time"] == datetime(2026, 9, 1, 10, 10, tzinfo=timezone.utc)
    sig = _latest_signal()
    assert (sig.granularity, sig.strategy, sig.action_taken) == (300, "gold_london_mr", "EXECUTED")
    assert sig.min_conditions is None and sig.indicators["Stretch_Score"] >= strat.load_params()["score_high"]
    with SessionLocal() as db:
        grains = {g for (g,) in db.query(MarketSnapshot.granularity).filter_by(symbol=SYMBOL).distinct()}
    assert grains == {300}


def test_gold_strategy_does_not_delay_entries_waiting_for_settlements(gold_pipeline, monkeypatch):
    waited = []
    monkeypatch.setattr(TradingPipeline, "_wait_for_expiring_contracts", lambda self: waited.append(True))
    gold_pipeline(10)
    assert waited == []


def test_gold_signal_older_than_a_minute_is_not_traded(gold_pipeline):
    assert gold_pipeline(90)[SYMBOL]["action"] == "STALE_SIGNAL"
    assert FakeEngine.calls == []


@pytest.mark.parametrize("late,bar,stale", [(30, 300, False), (90, 300, True), (100, 900, False), (130, 900, True)])
def test_engine_staleness_uses_the_bar_length(monkeypatch, late, bar, stale):
    close = datetime(2026, 9, 1, 10, 15, tzinfo=timezone.utc)
    monkeypatch.setattr("execution.deriv_engine._now", lambda: close + timedelta(seconds=late))
    context = {"candle_time": close - timedelta(seconds=bar), "bar_seconds": bar,
               "max_signal_age": 60 if bar == 300 else 120, "balance_at": time.monotonic()}
    assert DerivEngine._signal_stale(context) is stale


def test_five_minute_cycles_start_after_each_five_minute_close():
    from core.schedule import wait_for_cycle
    base = datetime(2026, 9, 1, 10, 0, tzinfo=timezone.utc).timestamp()
    clock = {"t": base + 8 * 60}  # 10:08, three minutes after the 10:05 close: too late for it
    slept = []

    def sleep(s):
        slept.append(s)
        clock["t"] += s

    boundary = wait_for_cycle(clock=lambda: clock["t"], sleep=sleep, granularity=300)
    assert boundary == base + 10 * 60
    assert clock["t"] == base + 10 * 60 + 10


@pytest.mark.usefixtures("clean_trading_tables")
def test_same_direction_cooldown_counts_the_strategy_bars():
    now = datetime.now(timezone.utc)
    with SessionLocal() as db:
        db.add(TradeIntent(symbol=SYMBOL, side="BUY", candle_time=now, stake=25, status="EXECUTED"))
        db.commit()
    check = RiskManager().check_order
    assert not check(25, 10000, SYMBOL, "BUY", now + timedelta(minutes=5), 1, bar_minutes=5)["allowed"]
    assert check(25, 10000, SYMBOL, "BUY", now + timedelta(minutes=10), 1, bar_minutes=5)["allowed"]
