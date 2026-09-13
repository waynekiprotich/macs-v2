"""
Walk-forward XGBoost training: P(win) for a CALL/PUT on a signal's side.

    python -m ml.train [--source snapshots|signals] [--symbol OTC_DJI] [--since 2026-01-01]
                       [--payout 0.80] [--promote] [--no-register]

1. Load labelled rows from ml.features, ordered by candle_time.
2. Walk forward: every test fold is scored by a model trained only on rows
   whose label had settled before the fold starts, so no training label
   overlaps the period it is scored on.
3. Score the out-of-sample predictions against honest baselines: the
   training base rate (log loss) and taking every row the rule produced
   (win rate, expectancy).
4. The decision rule is fixed, not tuned on the results: take a trade only
   when P(win) >= breakeven + DECISION_MARGIN, breakeven = 1 / (1 + payout).
   Tuning the threshold on the same predictions would flatter the model.
5. Gate: out-of-sample expectancy above zero over at least MIN_GATE_TRADES
   taken trades, and log loss below the base rate. Only a model that passes
   can be promoted to active.
6. Refit on every row, save the booster to ml/artifacts, and register it in
   model_versions (status candidate, or active with --promote).
"""
import argparse
import hashlib
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, Optional, Tuple

import numpy as np
import pandas as pd
import xgboost as xgb

from config.settings import settings
from core.performance import compute_metrics
from execution.deriv_engine import _duration_delta
from ml.features import FEATURE_COLUMNS, _utc, build_dataset, build_snapshot_dataset
from models.database import ModelVersion, SessionLocal

logger = logging.getLogger(__name__)

ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"

# Shallow, heavily regularised trees: the data is small and noisy, and the
# job is to find a weak edge without memorising noise.
PARAMS = {
    "objective": "binary:logistic",
    "eval_metric": "logloss",
    "max_depth": 3,
    "eta": 0.05,
    "subsample": 0.8,
    "colsample_bytree": 0.8,
    "min_child_weight": 5,
    "lambda": 1.0,
    "seed": 42,
}
NUM_BOOST_ROUND = 200

N_FOLDS = 4
MIN_TRAIN_ROWS = 200
MIN_GATE_TRADES = 100
DECISION_MARGIN = 0.02
# cli.py's backtest default, used only when no real payout has been recorded.
DEFAULT_PAYOUT = 0.85


class InsufficientData(Exception):
    pass


def target_name() -> str:
    return f"won_{settings.MACS_CONTRACT_DURATION}{settings.MACS_CONTRACT_DURATION_UNIT}_callput"


def _matrix(df: pd.DataFrame, labelled: bool) -> xgb.DMatrix:
    label = df["won"].astype(int) if labelled else None
    return xgb.DMatrix(df[FEATURE_COLUMNS].astype(float), label=label, feature_names=FEATURE_COLUMNS)


def fit(df: pd.DataFrame) -> xgb.Booster:
    return xgb.train(PARAMS, _matrix(df, labelled=True), num_boost_round=NUM_BOOST_ROUND)


def predict(booster: xgb.Booster, df: pd.DataFrame) -> np.ndarray:
    return booster.predict(_matrix(df, labelled=False))


def walk_forward_splits(times: pd.Series, n_folds: int, embargo: timedelta) -> Iterator[Tuple[pd.Index, pd.Index]]:
    """(train, test) index pairs. Test folds tile the later half of the time
    range. A row trains a fold only if its label window, candle_time to
    candle_time + embargo, closed by the time the fold starts."""
    start, end = times.min(), times.max()
    first_test = start + (end - start) / 2
    edges = [first_test + (end - first_test) * i / n_folds for i in range(n_folds + 1)]
    for i, (lo, hi) in enumerate(zip(edges, edges[1:])):
        in_fold = (times >= lo) & ((times <= hi) if i == n_folds - 1 else (times < hi))
        yield times[times + embargo <= lo].index, times[in_fold].index


def auc(y: np.ndarray, p: np.ndarray) -> float:
    n_pos = int(y.sum())
    n_neg = len(y) - n_pos
    if not n_pos or not n_neg:
        return float("nan")
    ranks = pd.Series(p).rank(method="average").to_numpy()
    return float((ranks[y == 1].sum() - n_pos * (n_pos + 1) / 2) / (n_pos * n_neg))


def log_loss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def _outcomes(won: np.ndarray, payout: float) -> dict:
    """Win rate and expectancy per unit stake: +payout on a win, -1 on a loss."""
    if not len(won):
        return {"n": 0, "win_rate": None, "expectancy": None}
    return {
        "n": int(len(won)),
        "win_rate": round(float(won.mean()), 4),
        "expectancy": round(float(np.where(won == 1, payout, -1.0).mean()), 4),
    }


def evaluate(df: pd.DataFrame, payout: float, embargo: timedelta) -> dict:
    """Out-of-sample metrics from walk-forward folds. df must be sorted by candle_time."""
    folds, scored = [], []
    for train_idx, test_idx in walk_forward_splits(df["candle_time"], N_FOLDS, embargo):
        if len(train_idx) < MIN_TRAIN_ROWS or not len(test_idx):
            continue
        train, test = df.loc[train_idx], df.loc[test_idx].copy()
        test["proba"] = predict(fit(train), test)
        test["base_rate"] = train["won"].mean()
        scored.append(test)
        folds.append({
            "train_rows": int(len(train)),
            "test_rows": int(len(test)),
            "test_start": test["candle_time"].min().isoformat(),
            "test_end": test["candle_time"].max().isoformat(),
            "auc": round(auc(test["won"].to_numpy(), test["proba"].to_numpy()), 4),
        })
    if not scored:
        raise InsufficientData(f"No fold had {MIN_TRAIN_ROWS} training rows before it.")

    oos = pd.concat(scored)
    y, p = oos["won"].to_numpy(), oos["proba"].to_numpy()
    threshold = 1 / (1 + payout) + DECISION_MARGIN
    taken = oos[oos["proba"] >= threshold]

    metrics = {
        "payout": round(payout, 4),
        "decision_threshold": round(threshold, 4),
        "n_oos": int(len(oos)),
        "test_start": oos["candle_time"].min().isoformat(),
        "test_end": oos["candle_time"].max().isoformat(),
        "auc": round(auc(y, p), 4),
        "log_loss": round(log_loss(y, p), 4),
        "base_rate_log_loss": round(log_loss(y, oos["base_rate"].to_numpy()), 4),
        "brier": round(float(np.mean((p - y) ** 2)), 4),
        "every_row": _outcomes(y, payout),
        "model_taken": _outcomes(taken["won"].to_numpy(), payout),
        "folds": folds,
    }

    reasons = []
    if metrics["model_taken"]["n"] < MIN_GATE_TRADES:
        reasons.append(f"{metrics['model_taken']['n']} out-of-sample trades cleared the threshold; need {MIN_GATE_TRADES}")
    elif metrics["model_taken"]["expectancy"] <= 0:
        reasons.append(f"out-of-sample expectancy {metrics['model_taken']['expectancy']:+.4f} per stake is not positive")
    if metrics["log_loss"] >= metrics["base_rate_log_loss"]:
        reasons.append(f"log loss {metrics['log_loss']} does not beat the base rate's {metrics['base_rate_log_loss']}")
    metrics["gate"] = {"passed": not reasons, "reasons": reasons}
    return metrics


def train_and_register(session, df: pd.DataFrame, payout: float, source: str, promote: bool = False,
                       register: bool = True, artifact_dir: Path = ARTIFACT_DIR) -> dict:
    if len(df) < 2 * MIN_TRAIN_ROWS:
        raise InsufficientData(
            f"{len(df)} labelled rows; need at least {2 * MIN_TRAIN_ROWS}. market_snapshots gains one bar per "
            "symbol every 15 minutes, and the pipeline's first run backfills what Deriv returns."
        )
    df = df.sort_values("candle_time").reset_index(drop=True)
    embargo = _duration_delta(settings.MACS_CONTRACT_DURATION, settings.MACS_CONTRACT_DURATION_UNIT)
    metrics = evaluate(df, payout, embargo)

    now = datetime.now(timezone.utc)
    version = f"xgb-{now:%Y%m%dT%H%M%S%fZ}"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact = artifact_dir / f"{version}.json"
    fit(df).save_model(artifact)
    sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()

    promoted = promote and metrics["gate"]["passed"]
    result = {"version": version, "artifact": str(artifact), "promoted": promoted, "metrics": metrics}
    if not register:
        return result

    if promoted:
        session.query(ModelVersion).filter_by(target=target_name(), status="active").update(
            {"status": "retired", "retired_at": now}
        )
    session.add(ModelVersion(
        version=version,
        model_type="xgboost",
        target=target_name(),
        status="active" if promoted else "candidate",
        feature_names=list(FEATURE_COLUMNS),
        hyperparams={"params": PARAMS, "num_boost_round": NUM_BOOST_ROUND, "source": source,
                     "decision_margin": DECISION_MARGIN},
        metrics=metrics,
        decision_threshold=metrics["decision_threshold"],
        train_start=df["candle_time"].min().to_pydatetime(),
        train_end=df["candle_time"].max().to_pydatetime(),
        test_start=datetime.fromisoformat(metrics["test_start"]),
        test_end=datetime.fromisoformat(metrics["test_end"]),
        n_train=int(len(df)),
        n_test=metrics["n_oos"],
        artifact_uri=str(artifact),
        artifact_sha256=sha256,
        promoted_at=now if promoted else None,
        notes=None if metrics["gate"]["passed"] else "Gate failed: " + "; ".join(metrics["gate"]["reasons"]),
    ))
    session.commit()
    return result


def _print_report(result: dict, promote: bool) -> None:
    m = result["metrics"]
    print(f"\nModel {result['version']}  ({m['n_oos']} out-of-sample rows, {m['test_start'][:10]} to {m['test_end'][:10]})")
    print(f"  AUC {m['auc']}   log loss {m['log_loss']} vs base rate {m['base_rate_log_loss']}   Brier {m['brier']}")
    print(f"  Payout {m['payout']:.2%}, so breakeven {1 / (1 + m['payout']):.1%}; model takes trades at P(win) >= {m['decision_threshold']:.1%}")
    for label, key in (("Every row (rule alone)", "every_row"), ("Model-filtered", "model_taken")):
        o = m[key]
        detail = f"win rate {o['win_rate']:.1%}, expectancy {o['expectancy']:+.4f}/stake" if o["n"] else "no trades"
        print(f"  {label:<24} {o['n']:>5} trades, {detail}")
    if m["gate"]["passed"]:
        print("  Gate: PASSED" + (" and promoted to active" if result["promoted"] else " (run with --promote to activate)"))
    else:
        print("  Gate: FAILED" + (", so not promoted" if promote else ""))
        for reason in m["gate"]["reasons"]:
            print(f"    - {reason}")
    print(f"  Artifact: {result['artifact']}")


def main(argv=None):
    parser = argparse.ArgumentParser(description="Walk-forward XGBoost training for CALL/PUT win probability.")
    parser.add_argument("--source", choices=("snapshots", "signals"), default="snapshots",
                        help="snapshots: replay the rule over every stored bar (default). signals: live signals only.")
    parser.add_argument("--symbol")
    parser.add_argument("--since", type=lambda s: _utc(datetime.fromisoformat(s)), help="ISO date, UTC")
    parser.add_argument("--payout", type=float, help="Payout ratio on a win (0.80 = 80%%). Default: measured from trades.")
    parser.add_argument("--promote", action="store_true", help="Make this the active model if it passes the gate.")
    parser.add_argument("--no-register", action="store_true", help="Evaluate and save the artifact without a model_versions row.")
    args = parser.parse_args(argv)

    payout: Optional[float] = args.payout
    if payout is None:
        payout = compute_metrics().get("avg_payout_ratio")
        if payout is None:
            payout = DEFAULT_PAYOUT
            print(f"No recorded payouts yet; assuming {DEFAULT_PAYOUT:.0%}. Pass --payout to override.")

    builder = build_snapshot_dataset if args.source == "snapshots" else build_dataset
    session = SessionLocal()
    try:
        df = builder(session, since=args.since, symbol=args.symbol)
        print(f"{len(df)} labelled rows from {args.source}")
        result = train_and_register(session, df, payout, args.source, promote=args.promote,
                                    register=not args.no_register)
    except InsufficientData as e:
        raise SystemExit(f"Not enough data to train: {e}")
    finally:
        session.close()
    _print_report(result, args.promote)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
