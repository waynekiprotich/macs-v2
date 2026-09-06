import pytest
import pandas as pd
import numpy as np
from core.indicators import compute_indicators
from core.regime import detect_regime

def test_indicators_missing_data():
    df = pd.DataFrame({'Close': [100.0]})
    res = compute_indicators(df)
    assert 'SMA_50' in res.columns

def test_regime_neutral():
    df = pd.DataFrame({
        'Close': [100, 100, 100],
        'SMA_50': [100, 100, 100],
        'SMA_200': [100, 100, 100],
        'ATR_14': [1.0, 1.0, 1.0],
        'ATR_SMA_20': [1.0, 1.0, 1.0]
    })
    res = detect_regime(df)
    assert res.iloc[-1]['Regime'] == 'neutral'

def test_volatility_rejection():
    # High ATR triggers HOLD
    atr_values = [1.0] * 19 + [5.0]
    df = pd.DataFrame({
        'Close': [100]*20,
        'SMA_50': [90]*20,
        'SMA_200': [80]*20,
        'ATR_14': atr_values,
        'Regime': ['bullish']*20
    })
    res = detect_regime(df)
    assert res.iloc[-1]['Is_Volatile'] == True
