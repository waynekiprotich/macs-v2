# DEPRECATED: no longer called by core/pipeline.py — superseded by
# core/technical_strategy.generate_signal(), which is symmetric (BUY+SELL)
# and doesn't depend on the shallow Tech_Score from core/scoring.py.
# Kept only because test_volatility.py still imports it.
import logging
import pandas as pd

logger = logging.getLogger(__name__)

def combine_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Weight and combine various signals to generate a final trading signal."""
    if df.empty or 'Tech_Score' not in df.columns:
        logger.warning("Missing required data for signal combination")
        return df
        
    df = df.copy()
    try:
        df['Signal'] = 'HOLD'
        
        # We need to evaluate the combined score (Tech + AI)
        # But df['AI_Score'] is added in pipeline.py for the latest row, not here for the whole dataframe.
        # Actually, let's just do Tech_Score logic here and refine it in pipeline.py, or better, 
        # move the combine_signals logic to handle the new scale.
        buy_condition = (df['Tech_Score'] >= 70.0) & (df['Regime'] == 'bullish')
        sell_condition = (df['Tech_Score'] <= 30.0) & (df['Regime'] == 'bearish')
        
        df.loc[buy_condition, 'Signal'] = 'BUY'
        df.loc[sell_condition, 'Signal'] = 'SELL'
        
        # Override to HOLD if highly volatile (chop/noise)
        df.loc[df['Is_Volatile'] == True, 'Signal'] = 'HOLD'
        
        return df
    except Exception as e:
        logger.error(f"Error combining signals: {e}")
        return df
