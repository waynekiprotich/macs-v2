"""The research backtester's accounting: fills, drift, costs, cash, CFD financing."""
import numpy as np
import pandas as pd
import pytest

from research.macro import backtest as bt

IDX = pd.bdate_range("2024-01-01", periods=6)


def _prices(closes, opens=None):
    c = pd.DataFrame({"A": closes}, index=IDX[:len(closes)])
    o = pd.DataFrame({"A": opens if opens is not None else closes}, index=c.index)
    return o, c


def _targets(rows, index):
    return pd.DataFrame({"A": rows}, index=index, dtype=float)


ZERO = pd.Series(0.0, index=IDX)


def test_buy_and_hold_earns_the_asset_return_after_the_first_open():
    o, c = _prices([100, 110, 121, 121], opens=[100, 105, 115, 121])
    res = bt.run(_targets([1, np.nan, np.nan, np.nan], c.index), o, c, ZERO, cost_bps=0)
    # Filled at the day-2 open (105): day 2 earns 110/105, then close-to-close.
    assert (1 + res.returns).prod() == pytest.approx(121 / 105)
    assert res.rebalances[0] == c.index[1]


def test_a_signal_never_earns_the_overnight_move_into_its_own_fill():
    o, c = _prices([100, 100, 100], opens=[100, 150, 100])
    res = bt.run(_targets([1, np.nan, np.nan], c.index), o, c, ZERO, cost_bps=0)
    assert res.returns.iloc[1] == pytest.approx(100 / 150 - 1)


def test_costs_are_charged_per_side_on_traded_notional():
    o, c = _prices([100, 100, 100, 100])
    res = bt.run(_targets([1, np.nan, 0, np.nan], c.index), o, c, ZERO, cost_bps=10)
    # The fee comes out of cash; the position is sized on equity before the fee.
    assert (1 + res.returns).prod() == pytest.approx(1 - 0.001 - 0.001, rel=1e-9)
    assert res.turnover.sum() == pytest.approx(2, rel=1e-3)


def test_idle_cash_earns_the_bill_rate_only_in_a_cash_account():
    o, c = _prices([100, 100, 100, 100])
    rf = pd.Series(0.001, index=c.index)
    flat = _targets([0, np.nan, np.nan, np.nan], c.index)
    assert (1 + bt.run(flat, o, c, rf, cost_bps=0).returns).prod() == pytest.approx(1.001 ** 3)
    assert (1 + bt.run(flat, o, c, rf, cost_bps=0, account="cfd").returns).prod() == pytest.approx(1.0)


def test_cfd_longs_pay_rate_plus_spread():
    o, c = _prices([100, 100, 100, 100])
    rf = pd.Series(0.0004, index=c.index)
    res = bt.run(_targets([1, np.nan, np.nan, np.nan], c.index), o, c, rf, cost_bps=0,
                 account="cfd", cfd_spread=0.0252)
    # Filled on day 2; financed on days 2-4.
    assert (1 + res.returns).prod() == pytest.approx(1 - 3 * (0.0004 + 0.0001), rel=1e-6)


def test_holdings_drift_between_rebalances():
    idx = IDX[:3]
    c = pd.DataFrame({"A": [100, 200, 200], "B": [100, 100, 100]}, index=idx, dtype=float)
    tg = pd.DataFrame({"A": [0.5, np.nan, np.nan], "B": [0.5, np.nan, np.nan]}, index=idx)
    res = bt.run(tg, c, c, pd.Series(0.0, index=idx), cost_bps=0, execution="close")
    assert res.weights.iloc[1]["A"] == pytest.approx(2 / 3)


def test_close_execution_fills_at_the_decision_close():
    o, c = _prices([100, 110, 121], opens=[100, 100, 121])
    res = bt.run(_targets([1, np.nan, np.nan], c.index), o, c, ZERO, cost_bps=0, execution="close")
    assert (1 + res.returns).prod() == pytest.approx(1.21)


def test_weights_on_an_unlisted_asset_are_refused():
    idx = IDX[:3]
    c = pd.DataFrame({"A": [np.nan, 100, 100]}, index=idx)
    with pytest.raises(ValueError):
        bt.run(pd.DataFrame({"A": [1.0, np.nan, np.nan]}, index=idx), c, c, pd.Series(0.0, index=idx),
               execution="close")


def test_metrics_of_a_constant_return():
    r = pd.Series(0.001, index=pd.bdate_range("2020-01-01", periods=252))
    m = bt.metrics(r, pd.Series(0.0, index=r.index))
    assert m["cagr"] == pytest.approx(1.001 ** 252 - 1)
    assert m["max_dd"] == 0
