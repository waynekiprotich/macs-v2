"""Technical indicator computations for MACS strategies.

All functions operate on a DataFrame with OHLCV columns (Open, High, Low,
Close, Volume). They return a *copy* of the input with new columns appended;
the caller's original frame is never mutated.
"""

import pandas as pd
import numpy as np
from ta.trend import SMAIndicator, EMAIndicator, MACD
from ta.momentum import RSIIndicator, StochasticOscillator
from ta.volatility import BollingerBands, AverageTrueRange
from ta.volume import VolumeWeightedAveragePrice
import logging

logger = logging.getLogger(__name__)

# Minimum number of rows required before any indicator makes sense.
MIN_ROWS: int = 200


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Compute various technical indicators."""
    if df.empty:
        logger.warning("Empty DataFrame passed to compute_indicators")
        return df

    df = df.copy()
    
    try:
        # SMAs
        df['SMA_20'] = SMAIndicator(close=df['Close'], window=20).sma_indicator()
        df['SMA_50'] = SMAIndicator(close=df['Close'], window=50).sma_indicator()
        df['SMA_200'] = SMAIndicator(close=df['Close'], window=200).sma_indicator()

        # EMAs
        df['EMA_12'] = EMAIndicator(close=df['Close'], window=12).ema_indicator()
        df['EMA_26'] = EMAIndicator(close=df['Close'], window=26).ema_indicator()

        # MACD
        macd = MACD(close=df['Close'], window_slow=26, window_fast=12, window_sign=9)
        df['MACD_line'] = macd.macd()
        df['MACD_signal'] = macd.macd_signal()
        df['MACD_diff'] = macd.macd_diff()

        # RSI
        df['RSI_14'] = RSIIndicator(close=df['Close'], window=14).rsi()

        # Bollinger Bands
        bb = BollingerBands(close=df['Close'], window=20, window_dev=2)
        df['BB_upper'] = bb.bollinger_hband()
        df['BB_lower'] = bb.bollinger_lband()
        df['BB_mid'] = bb.bollinger_mavg()

        # ATR
        atr = AverageTrueRange(high=df['High'], low=df['Low'], close=df['Close'], window=14)
        df['ATR_14'] = atr.average_true_range()

        # Volume SMA
        df['Volume_SMA_20'] = df['Volume'].rolling(window=20).mean()

        # Stochastics
        stoch = StochasticOscillator(high=df['High'], low=df['Low'], close=df['Close'], window=14, smooth_window=3)
        df['Stoch_k'] = stoch.stoch()
        df['Stoch_d'] = stoch.stoch_signal()

        # Wick percentages
        df['Total_Range'] = df['High'] - df['Low']
        df['Total_Range'] = df['Total_Range'].replace(0, np.nan)
        
        df['Upper_Wick'] = df['High'] - df[['Open', 'Close']].max(axis=1)
        df['Lower_Wick'] = df[['Open', 'Close']].min(axis=1) - df['Low']
        
        df['Upper_Wick_Pct'] = df['Upper_Wick'] / df['Total_Range']
        df['Lower_Wick_Pct'] = df['Lower_Wick'] / df['Total_Range']
        
        # Support/Resistance
        df['Support'] = df['Low'].rolling(window=20).min()
        df['Resistance'] = df['High'].rolling(window=20).max()

        df.dropna(inplace=True)
        
        return df
    except Exception as e:
        logger.error(f"Error computing indicators: {e}")
        return df
