import math
from datetime import datetime, timedelta, timezone

import pytest

from ml.features import FEATURE_COLUMNS, build_dataset, build_snapshot_dataset, compute_features, label_from_prices
from models.database import MarketSnapshot, PaperTrade, SessionLocal, SystemLog, init_db

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)

IND = {
    "Close": 101.0, "ATR_14": 2.0, "EMA_12": 100.0, "EMA_26": 99.0, "SMA_50": 98.0, "SMA_200": 95.0,
    "MACD_diff": 0.5, "RSI_14": 60.0, "Stoch_k": 70.0, "Stoch_d": 65.0,
    "BB_upper": 104.0, "BB_lower": 96.0, "BB_mid": 100.0,
    "Body_Ratio": 0.7, "Upper_Wick_Pct": 0.1, "Lower_Wick_Pct": 0.2,
    "Support": 97.0, "Resistance": 105.0, "Support_Touches": 3, "Resistance_Touches": 1,
    "Bullish_Candle": True, "Bearish_Candle": False, "Regime": "bullish", "Is_Volatile": False,
}


def test_features_cover_every_column_in_order():
    assert list(compute_features(IND, "BUY", T0)) == FEATURE_COLUMNS


def test_sell_mirrors_directional_features():
    buy, sell = compute_features(IND, "BUY", T0), compute_features(IND, "SELL", T0)
    assert (buy["trend_ema"], sell["trend_ema"]) == (0.5, -0.5)
    assert sell["rsi"] == 100 - buy["rsi"]
    assert sell["bb_position"] == 1 - buy["bb_position"]
    assert (buy["regime_with"], sell["regime_against"]) == (1.0, 1.0)
    assert (buy["dist_backstop"], sell["dist_obstacle"]) == (2.0, 2.0)
    assert buy["atr_pct"] == sell["atr_pct"]


def test_zero_atr_gives_nan_instead_of_crashing():
    assert math.isnan(compute_features({**IND, "ATR_14": 0.0}, "BUY", T0)["trend_ema"])


def test_price_tie_loses():
    assert label_from_prices("BUY", 100.0, 100.0) == 0
    assert label_from_prices("SELL", 100.0, 100.0) == 0
    assert label_from_prices("SELL", 100.0, 99.0) == 1
    assert label_from_prices("BUY", 100.0, None) is None


def test_build_dataset_labels_from_trade_then_candles():
    init_db()
    db = SessionLocal()
    try:
        executed = SystemLog(symbol="TEST_FEAT", signal="BUY", candle_time=T0, granularity=900,
                             close_price=101.0, indicators=IND, action_taken="EXECUTED")
        dry_run = SystemLog(symbol="TEST_FEAT", signal="SELL", candle_time=T0 + timedelta(minutes=15),
                            granularity=900, close_price=101.0, indicators=IND, action_taken="DRY_RUN")
        unlabelled = SystemLog(symbol="TEST_FEAT", signal="BUY", candle_time=T0 + timedelta(hours=5),
                               granularity=900, close_price=101.0, indicators=IND, action_taken="DRY_RUN")
        hold = SystemLog(symbol="TEST_FEAT", signal="HOLD", candle_time=T0, granularity=900, indicators=IND)
        db.add_all([executed, dry_run, unlabelled, hold])
        db.flush()
        db.add(PaperTrade(symbol="TEST_FEAT", side="BUY", quantity=170.0, price=170.0, status="CLOSED",
                          result="LOST", pnl=-170.0, signal_id=executed.id, quoted_payout=314.5))
        # Exit bar for the SELL, one 15m contract after its candle, closes below entry.
        db.add(MarketSnapshot(symbol="TEST_FEAT", granularity=900, candle_time=T0 + timedelta(minutes=30),
                              open=101.0, high=101.0, low=99.0, close=100.0, source="test"))
        db.commit()

        df = build_dataset(db, symbol="TEST_FEAT")
    finally:
        db.close()

    assert list(df["label_source"]) == ["trade", "candle"]
    assert list(df["won"]) == [0, 1]
    assert df["payout_ratio"].iloc[0] == pytest.approx(0.85)


def test_build_dataset_keeps_one_row_per_bar_preferring_the_traded_signal():
    init_db()
    db = SessionLocal()
    try:
        bar = T0 + timedelta(days=1)
        repeats = [
            SystemLog(symbol="TEST_DUP", signal="BUY", candle_time=bar, granularity=900,
                      close_price=101.0, indicators=IND, action_taken=action)
            for action in ("RISK_BLOCKED", "EXECUTED", "RISK_BLOCKED")
        ]
        db.add_all(repeats)
        db.flush()
        traded_id = repeats[1].id
        db.add(PaperTrade(symbol="TEST_DUP", side="BUY", quantity=170.0, price=170.0, status="CLOSED",
                          result="WON", pnl=136.0, signal_id=traded_id))
        db.commit()

        df = build_dataset(db, symbol="TEST_DUP")
    finally:
        db.close()

    assert list(df["signal_id"]) == [traded_id]
    assert list(df["label_source"]) == ["trade"]


def test_snapshot_dataset_replays_the_rule_on_every_bar_with_an_exit():
    init_db()
    db = SessionLocal()
    try:
        # IND agrees with 7 bull conditions and 0 bear, so every bar replays as a BUY.
        for minutes, close in ((0, 100.0), (15, 101.0), (30, 100.5)):
            db.add(MarketSnapshot(symbol="TEST_SNAP", granularity=900, candle_time=T0 + timedelta(minutes=minutes),
                                  open=close, high=close, low=close, close=close, source="test", indicators=IND))
        db.commit()

        df = build_snapshot_dataset(db, symbol="TEST_SNAP")
    finally:
        db.close()

    assert list(df["signal"]) == ["BUY", "BUY"]
    assert list(df["won"]) == [1, 0]  # the last bar has no exit bar yet
    assert set(df["label_source"]) == {"snapshot"}
