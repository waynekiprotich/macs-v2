"""
Regression coverage for the corrected backtest model in cli.py.

MACS actually buys a fixed 15-minute CALL/PUT binary option
(execution/deriv_engine.py: duration=15, duration_unit='m', no barrier).
An earlier version of this backtest simulated a TP/SL barrier walk that
doesn't correspond to any contract this system buys — take_profit/
stop_loss are computed by technical_strategy.generate_signal() for
logging only and are never read by execute_signal(). These tests pin
down the corrected binary payoff so it can't silently regress back to
modeling a product that doesn't exist here.
"""
import pandas as pd
from cli import _simulate, _trade_stats, _interval_to_minutes


def _prepared_df_with_one_signal(direction: str, entry_close: float, exit_close: float):
    """Build the minimal two-row frame needed for one BUY or one SELL signal
    at index 210 (the strategy's warm-up cutoff) that resolves one bar later."""
    n = 212
    base = {
        'Open': 100.0, 'High': 100.0, 'Low': 100.0, 'Close': 100.0, 'Volume': 1000.0,
        'Regime': 'neutral', 'EMA_12': 100.0, 'EMA_26': 100.0, 'RSI_14': 50.0,
        'Stoch_k': 50.0, 'Stoch_d': 50.0, 'MACD_diff': 0.0, 'ATR_14': 1.0,
        'Support': 0.0, 'Resistance': 0.0, 'Support_Touches': 0, 'Resistance_Touches': 0,
        'BB_upper': 110.0, 'BB_lower': 90.0, 'BB_mid': 100.0,
        'Bullish_Candle': False, 'Bearish_Candle': False, 'Is_Volatile': False,
    }
    rows = [dict(base) for _ in range(n)]

    if direction == 'BUY':
        rows[210].update({
            'Close': entry_close, 'Regime': 'bullish', 'EMA_12': 105.0, 'EMA_26': 100.0,
            'RSI_14': 55.0, 'Stoch_k': 60.0, 'Stoch_d': 50.0, 'MACD_diff': 0.5,
            'Support': entry_close * 0.998, 'Support_Touches': 3,
            'BB_mid': entry_close - 5, 'BB_upper': entry_close + 10,
            'Bullish_Candle': True,
        })
    else:
        rows[210].update({
            'Close': entry_close, 'Regime': 'bearish', 'EMA_12': 95.0, 'EMA_26': 100.0,
            'RSI_14': 45.0, 'Stoch_k': 40.0, 'Stoch_d': 50.0, 'MACD_diff': -0.5,
            'Resistance': entry_close * 1.002, 'Resistance_Touches': 3,
            'BB_mid': entry_close + 5, 'BB_lower': entry_close - 10,
            'Bearish_Candle': True,
        })
    rows[211]['Close'] = exit_close

    idx = pd.date_range('2026-01-01', periods=n, freq='15min')
    return pd.DataFrame(rows, index=idx)


def test_interval_to_minutes():
    assert _interval_to_minutes('15m') == 15
    assert _interval_to_minutes('5m') == 5
    assert _interval_to_minutes('1h') == 60


def test_buy_wins_when_price_rises_one_bar_later():
    df = _prepared_df_with_one_signal('BUY', entry_close=100.0, exit_close=101.0)
    trades = _simulate(df, min_conditions=6, payout_pct=0.85, expiry_bars=1)
    assert len(trades) == 1
    assert trades[0]['result'] == 'WIN'
    assert trades[0]['pnl'] == 0.85  # payout, not a distance-based P&L


def test_buy_loses_when_price_falls_one_bar_later():
    df = _prepared_df_with_one_signal('BUY', entry_close=100.0, exit_close=99.0)
    trades = _simulate(df, min_conditions=6, payout_pct=0.85, expiry_bars=1)
    assert len(trades) == 1
    assert trades[0]['result'] == 'LOSS'
    assert trades[0]['pnl'] == -1.0  # full stake lost, regardless of how far it moved


def test_sell_wins_when_price_falls_one_bar_later():
    df = _prepared_df_with_one_signal('SELL', entry_close=100.0, exit_close=99.0)
    trades = _simulate(df, min_conditions=6, payout_pct=0.9, expiry_bars=1)
    assert len(trades) == 1
    assert trades[0]['result'] == 'WIN'
    assert trades[0]['pnl'] == 0.9


def test_pnl_is_binary_not_distance_based():
    """A tiny move and a huge move in the winning direction must pay the
    exact same amount — this is a fixed-payout binary option, not a barrier
    contract with variable P&L per point moved."""
    df_small = _prepared_df_with_one_signal('BUY', entry_close=100.0, exit_close=100.01)
    df_big = _prepared_df_with_one_signal('BUY', entry_close=100.0, exit_close=150.0)
    small_trades = _simulate(df_small, min_conditions=6, payout_pct=0.85, expiry_bars=1)
    big_trades = _simulate(df_big, min_conditions=6, payout_pct=0.85, expiry_bars=1)
    assert small_trades[0]['pnl'] == big_trades[0]['pnl'] == 0.85


def test_trade_stats_breakeven_math():
    trades = [{'result': 'WIN', 'pnl': 0.85}] * 54 + [{'result': 'LOSS', 'pnl': -1.0}] * 46
    wins, losses, total, win_rate, total_pnl = _trade_stats(trades)
    assert total == 100
    assert win_rate == 54.0
    # 54 wins * 0.85 - 46 losses * 1.0 should be roughly breakeven near the
    # theoretical breakeven win rate of 1/(1+0.85) = 54.05%
    assert abs(total_pnl) < 2.0
