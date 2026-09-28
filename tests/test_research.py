import json

import pandas as pd
import pytest

from config.settings import MarketRule
from core.research import load_export, metrics, simulate
from tests.test_backtest_binary import _prepared_df_with_one_signal


def test_research_has_no_outcomes_crossing_time_split():
    frame = _prepared_df_with_one_signal('BUY', 100, 101)
    rule = MarketRule(enabled=True, min_conditions=6)
    split = frame.index[211] + pd.Timedelta(minutes=15)
    assert len(simulate(frame, rule, .8)) == 1
    assert simulate(frame, rule, .8, end=split) == []
    assert simulate(frame, rule, .8, start=split) == []


def test_research_never_bridges_missing_candles():
    frame = _prepared_df_with_one_signal('BUY', 100, 101)
    frame.index = list(frame.index[:-1]) + [frame.index[-1]+pd.Timedelta(days=3)]
    assert simulate(frame, MarketRule(enabled=True, min_conditions=6), .8) == []


def test_research_observation_rule_cannot_create_trades():
    frame = _prepared_df_with_one_signal('BUY', 100, 101)
    assert simulate(frame, MarketRule(enabled=False), .8) == []


def test_research_is_conservative_about_assumed_payout():
    frame = _prepared_df_with_one_signal('BUY', 100, 101)
    assert simulate(frame, MarketRule(enabled=True, min_conditions=6), .79) == []


def test_metrics_do_not_treat_empty_sample_as_success():
    empty = metrics([])
    assert empty['win_rate_pct'] is None
    assert empty['expectancy'] is None
    assert empty['win_rate_95pct_iid_interval'] is None
    result = metrics([{'pnl': .8}, {'pnl': -1}])
    assert result['pnl'] == -.2
    assert result['profit_factor'] == .8
    assert result['max_drawdown'] == 1


def test_export_reader_handles_beekeeper_wrapping(tmp_path):
    path = tmp_path/'export.json'
    path.write_text(json.dumps([{'macs_complete_export': {'trades': [], 'signals': [], 'market_snapshots': []}}]))
    data, digest = load_export(path)
    assert data['trades'] == []
    assert len(digest) == 64
