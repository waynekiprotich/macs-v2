"""Market data fetching utilities for MACS.

Thin wrappers around yfinance that return cleaned DataFrames.  All
functions drop NaN rows before returning so callers can assume a
contiguous, valid OHLCV series.
"""

import yfinance as yf
import pandas as pd
import logging

logger = logging.getLogger(__name__)


def fetch_data(symbol: str, period: str = "60d", interval: str = "15m") -> pd.DataFrame:
    """Fetch OHLCV history for *symbol* via yfinance.

    Args:
        symbol:   Yahoo Finance ticker (e.g. ``"BTC-USD"``).
        period:   Lookback window accepted by yfinance (default ``"60d"``).
        interval: Bar size accepted by yfinance (default ``"15m"``).

    Returns:
        A DataFrame with columns Open/High/Low/Close/Volume, NaN rows
        dropped.  Returns an empty DataFrame on error.
    """
    logger.info(f"Fetching data for {symbol} with period {period} and interval {interval}")
    try:
        ticker = yf.Ticker(symbol)
        df = ticker.history(period=period, interval=interval)
        if df.empty:
            logger.warning(f"No data returned for {symbol}")
            return df
        
        df.dropna(inplace=True)
        return df
    except Exception as e:
        logger.error(f"Error fetching data for {symbol}: {e}")
        return pd.DataFrame()
