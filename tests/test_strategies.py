"""
Tests for the standalone strategy modules in strategies/.

NOTE: neither of these is wired into core/pipeline.py — the live decision
logic is core/technical_strategy.py (see tests/test_technical_strategy.py).
These are kept as self-contained alternates; if they're never adopted they
can be deleted along with their tests.
"""
import pytest
from strategies.mean_reversion import MeanReversionStrategy
from strategies.options_selling import OptionsSellingStrategy

def test_mean_reversion_strategy():
    strategy = MeanReversionStrategy()
    
    # Oversold at lower BB
    buy_data = {
        "close": 90.0,
        "rsi": 25.0,
        "bb_lower": 95.0,
        "bb_upper": 110.0
    }
    signal = strategy.generate_signal(buy_data)
    assert signal["signal"] == "BUY"
    
    # Overbought at upper BB
    sell_data = {
        "close": 115.0,
        "rsi": 75.0,
        "bb_lower": 95.0,
        "bb_upper": 110.0
    }
    signal = strategy.generate_signal(sell_data)
    assert signal["signal"] == "SELL"
    
    # Middle of range
    neutral_data = {
        "close": 100.0,
        "rsi": 50.0,
        "bb_lower": 90.0,
        "bb_upper": 110.0
    }
    signal = strategy.generate_signal(neutral_data)
    assert signal["signal"] == "NEUTRAL"

def test_options_selling_strategy():
    strategy = OptionsSellingStrategy(target_delta=0.15, target_dte=7)
    
    data = {
        "underlying_price": 100.0,
        "puts": [
            {"strike": 90.0, "delta": -0.16, "dte": 7, "bid": 1.0, "ask": 1.1},
            {"strike": 85.0, "delta": -0.05, "dte": 7, "bid": 0.5, "ask": 0.6}
        ]
    }
    
    signal = strategy.generate_signal(data)
    assert signal["signal"] == "SELL_PUT_SPREAD"
    assert signal["short_leg"]["strike"] == 90.0
    assert signal["long_leg"]["strike"] == 85.0

def test_options_selling_strategy_error_handling():
    strategy = OptionsSellingStrategy()
    
    # Missing puts data should be neutral due to no valid options
    signal = strategy.generate_signal({})
    assert signal["signal"] == "NEUTRAL"
