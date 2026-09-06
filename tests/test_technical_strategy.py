import pandas as pd
from core.technical_strategy import evaluate_row, generate_signal, TOTAL_CONDITIONS


def _base_row(**overrides):
    row = {
        'Close': 100.0, 'Regime': 'neutral',
        'EMA_12': 100.0, 'EMA_26': 100.0,
        'RSI_14': 50.0,
        'Stoch_k': 50.0, 'Stoch_d': 50.0,
        'MACD_diff': 0.0,
        'Support': 0.0, 'Resistance': 0.0,
        'Support_Touches': 0, 'Resistance_Touches': 0,
        'BB_upper': 110.0, 'BB_lower': 90.0, 'BB_mid': 100.0,
        'Bullish_Candle': False, 'Bearish_Candle': False,
        'ATR_14': 1.0,
    }
    row.update(overrides)
    return pd.Series(row)


def test_neutral_row_never_reaches_trade_threshold():
    """A truly flat row (RSI 50, MACD flat, no regime, no S/R touches) should
    never accumulate enough conditions on either side to fire a trade."""
    row = _base_row()
    result = evaluate_row(row)
    assert result['bull_conditions'] < 6
    assert result['bear_conditions'] < 6
    sig = generate_signal(row, min_conditions=6, is_volatile=False)
    assert sig['signal'] == 'HOLD'


def test_all_bullish_conditions_fire_buy():
    row = _base_row(
        Regime='bullish', EMA_12=105.0, EMA_26=100.0, RSI_14=55.0,
        Stoch_k=60.0, Stoch_d=50.0, MACD_diff=0.5,
        Support=95.0, Support_Touches=3, Close=95.2,
        BB_mid=90.0, BB_upper=110.0, Close_bb=None,
        Bullish_Candle=True,
    )
    row['Close'] = 95.2  # near support, also within BB_mid..BB_upper range check
    result = evaluate_row(row)
    assert result['bull_conditions'] >= 6
    sig = generate_signal(row, min_conditions=6, is_volatile=False)
    assert sig['signal'] == 'BUY'
    assert sig['take_profit'] > row['Close']
    assert sig['stop_loss'] < row['Close']


def test_all_bearish_conditions_fire_sell():
    row = _base_row(
        Regime='bearish', EMA_12=95.0, EMA_26=100.0, RSI_14=45.0,
        Stoch_k=40.0, Stoch_d=50.0, MACD_diff=-0.5,
        Resistance=105.0, Resistance_Touches=3,
        BB_mid=100.0, BB_lower=90.0,
        Bearish_Candle=True,
    )
    row['Close'] = 104.8  # near resistance, within BB_lower..BB_mid
    result = evaluate_row(row)
    assert result['bear_conditions'] >= 6
    sig = generate_signal(row, min_conditions=6, is_volatile=False)
    assert sig['signal'] == 'SELL'
    assert sig['take_profit'] < row['Close']
    assert sig['stop_loss'] > row['Close']


def test_volatility_filter_forces_hold_even_with_strong_signal():
    row = _base_row(
        Regime='bullish', EMA_12=105.0, EMA_26=100.0, RSI_14=55.0,
        Stoch_k=60.0, Stoch_d=50.0, MACD_diff=0.5,
        Support=95.0, Support_Touches=3, Close=95.2,
        BB_mid=90.0, BB_upper=110.0,
        Bullish_Candle=True,
    )
    sig = generate_signal(row, min_conditions=6, is_volatile=True)
    assert sig['signal'] == 'HOLD'


def test_fake_volume_cannot_influence_score():
    """Regression guard: nothing in evaluate_row should read a Volume column —
    Deriv mocks it to a constant, so any dependency on it is dead weight."""
    row = _base_row()
    row['Volume'] = 999999.0  # absurd value; must have zero effect
    baseline = _base_row()
    assert evaluate_row(row) == evaluate_row(baseline)


def test_total_conditions_matches_scoring():
    result = evaluate_row(_base_row(Regime='bullish'))
    assert result['bull_confidence'] == round(result['bull_conditions'] / TOTAL_CONDITIONS * 100, 1)
