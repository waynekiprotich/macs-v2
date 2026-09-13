import hashlib
from datetime import timedelta

import numpy as np
import pandas as pd
import pytest
import xgboost as xgb

from ml.features import FEATURE_COLUMNS, META_COLUMNS
from ml.train import InsufficientData, train_and_register, walk_forward_splits
from models.database import ModelVersion, SessionLocal, init_db

FIFTEEN_MINUTES = timedelta(minutes=15)


def _dataset(n: int = 1600, learnable: bool = True, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(rng.normal(size=(n, len(FEATURE_COLUMNS))), columns=FEATURE_COLUMNS)
    p_win = 1 / (1 + np.exp(-3 * df["trend_ema"])) if learnable else np.full(n, 0.5)
    df["won"] = (rng.random(n) < p_win).astype(int)
    df["candle_time"] = pd.date_range("2026-01-01", periods=n, freq="15min", tz="UTC")
    for column in META_COLUMNS:
        if column not in df:
            df[column] = None
    df["symbol"], df["signal"] = "SYN", "BUY"
    return df


def test_training_labels_never_overlap_the_fold_they_score():
    times = pd.Series(pd.date_range("2026-01-01", periods=400, freq="15min", tz="UTC"))
    splits = list(walk_forward_splits(times, 4, FIFTEEN_MINUTES))
    assert len(splits) == 4
    for train, test in splits:
        assert len(test)
        assert (times[train] + FIFTEEN_MINUTES <= times[test].min()).all()
    assert sum(len(test) for _, test in splits) == len(times[times >= times.iloc[0] + (times.iloc[-1] - times.iloc[0]) / 2])


def test_learnable_edge_passes_the_gate_and_is_promoted(tmp_path):
    init_db()
    db = SessionLocal()
    try:
        result = train_and_register(db, _dataset(), payout=0.8, source="test", promote=True, artifact_dir=tmp_path)
        row = db.query(ModelVersion).filter_by(version=result["version"]).one()
    finally:
        db.close()

    assert row.status == "active" and row.promoted_at is not None
    assert row.metrics["gate"]["passed"] and row.metrics["auc"] > 0.7
    assert row.metrics["model_taken"]["expectancy"] > 0
    assert row.feature_names == FEATURE_COLUMNS
    assert hashlib.sha256(open(row.artifact_uri, "rb").read()).hexdigest() == row.artifact_sha256
    booster = xgb.Booster()
    booster.load_model(row.artifact_uri)
    assert booster.feature_names == FEATURE_COLUMNS


def test_noise_fails_the_gate_and_is_never_promoted(tmp_path):
    init_db()
    db = SessionLocal()
    try:
        result = train_and_register(db, _dataset(learnable=False, seed=1), payout=0.8, source="test",
                                    promote=True, artifact_dir=tmp_path)
        row = db.query(ModelVersion).filter_by(version=result["version"]).one()
    finally:
        db.close()

    assert not result["promoted"]
    assert row.status == "candidate"
    assert not row.metrics["gate"]["passed"] and row.notes.startswith("Gate failed")


def test_too_few_rows_is_refused(tmp_path):
    with pytest.raises(InsufficientData):
        train_and_register(None, _dataset(n=100), payout=0.8, source="test", artifact_dir=tmp_path)
