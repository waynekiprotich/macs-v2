"""Market regime detection for MACS.

Classifies each bar as 'bullish', 'bearish', or 'neutral' using a
SMA-50/SMA-200 golden/death-cross rule. A separate boolean column
``Is_Volatile`` is set when the current ATR is more than 30% above its
20-bar rolling average, so downstream strategies can opt out of noisy
conditions without losing regime information.
"""

import pandas as pd
import logging

logger = logging.getLogger(__name__)

# Valid regime label strings returned by detect_regime.
REGIME_LABELS: tuple = ("bullish", "bearish", "neutral")

def detect_regime(df: pd.DataFrame) -> pd.DataFrame:
    """Detect market regime."""
    if df.empty or 'SMA_50' not in df.columns or 'SMA_200' not in df.columns or 'ATR_14' not in df.columns:
        logger.warning("Required indicators missing for regime detection")
        return df

    df = df.copy()
    try:
        df['Regime'] = 'neutral'
        
        # Trend conditions
        bullish_condition = (df['Close'] > df['SMA_50']) & (df['Close'] > df['SMA_200']) & (df['SMA_50'] > df['SMA_200'])
        bearish_condition = (df['Close'] < df['SMA_50']) & (df['Close'] < df['SMA_200']) & (df['SMA_50'] < df['SMA_200'])
        
        df.loc[bullish_condition, 'Regime'] = 'bullish'
        df.loc[bearish_condition, 'Regime'] = 'bearish'
        
        # Volatility condition (separate flag, so it doesn't mask the regime)
        avg_atr = df['ATR_14'].rolling(window=20).mean()
        df['Is_Volatile'] = df['ATR_14'] > (1.3 * avg_atr)
        
        df.dropna(inplace=True)
        return df
    except Exception as e:
        logger.error(f"Error detecting regime: {e}")
        return df
