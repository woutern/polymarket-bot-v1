"""LightGBM model trainer — trains per-pair binary classifiers.

Loads training data from DynamoDB, trains with time-ordered split,
calibrates with Platt + Isotonic, saves to S3, updates SSM.
"""

from __future__ import annotations

import io
import json
import logging
import math
import pickle
import time
from dataclasses import dataclass

import numpy as np

logger = logging.getLogger(__name__)

FEATURE_COLUMNS = [
    "move_pct_15s",  # price change in first 15s (known at entry)
    "realized_vol_5m", "vol_ratio", "body_ratio",
    "prev_window_direction", "prev_window_move_pct",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    # Signal-context features (describe our actual entry conditions)
    "signal_move_pct",    # abs(move) at entry — magnitude regardless of direction
    "signal_ask_price",   # yes_ask at window open
    "signal_seconds",     # seconds since open at first significant move
    "signal_ev",          # estimated EV at entry time
    # NOTE: move_pct_60s and move_pct_300s EXCLUDED — they are look-ahead features
]

PAIRS = ["BTC_5m", "BTC_15m", "ETH_5m", "ETH_15m", "SOL_5m", "SOL_15m"]


@dataclass
class TrainResult:
    pair: str
    n_train: int
    n_val: int
    val_brier: float
    val_auc: float
    baseline_brier: float
    deployed: bool
    s3_path: str = ""
    error: str = ""


def load_training_data(dynamo_table, asset: str, timeframe: str, limit: int = 5000) -> list[dict]:
    """Load training data from DynamoDB for a specific pair."""
    from boto3.dynamodb.conditions import Attr

    items = []
    scan_kwargs = {
        "FilterExpression": Attr("asset").eq(asset) & Attr("timeframe").eq(timeframe),
        "Limit": limit,
    }
    resp = dynamo_table.scan(**scan_kwargs)
    items.extend(resp.get("Items", []))
    while resp.get("LastEvaluatedKey") and len(items) < limit:
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        resp = dynamo_table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))

    # Sort by timestamp
    items.sort(key=lambda x: float(x.get("timestamp", 0)))
    return items[:limit]


# New features that default to 0 for historical data that lacks them
_OPTIONAL_FEATURES = {"signal_move_pct", "signal_ask_price", "signal_seconds", "signal_ev"}


def items_to_arrays(items: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """Convert DynamoDB items to feature matrix X and label array y.

    New signal-context features default to 0.0 for historical data
    that was collected before those features existed.
    """
    X_rows = []
    y_rows = []
    for item in items:
        features = []
        valid = True
        for col in FEATURE_COLUMNS:
            val = item.get(col)
            if val is None:
                if col in _OPTIONAL_FEATURES:
                    features.append(0.0)
                    continue
                valid = False
                break
            try:
                f = float(val)
                if math.isnan(f) or math.isinf(f):
                    valid = False
                    break
                features.append(f)
            except (ValueError, TypeError):
                valid = False
                break
        if not valid:
            continue
        outcome = item.get("outcome")
        if outcome is None:
            continue
        X_rows.append(features)
        y_rows.append(int(outcome))

    return np.array(X_rows), np.array(y_rows)


def train_pair(pair: str, items: list[dict], s3_client=None, ssm_client=None, s3_bucket: str = "") -> TrainResult:
    """Train a LightGBM model for one pair."""
    import lightgbm as lgb
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.linear_model import LogisticRegression
    from sklearn.isotonic import IsotonicRegression
    from sklearn.metrics import brier_score_loss, roc_auc_score

    asset, tf = pair.split("_")

    if len(items) < 500:
        return TrainResult(pair=pair, n_train=0, n_val=0, val_brier=1.0, val_auc=0.5,
                           baseline_brier=0.25, deployed=False, error=f"Only {len(items)} rows (<500)")

    X, y = items_to_arrays(items)
    if len(X) < 500:
        return TrainResult(pair=pair, n_train=0, n_val=0, val_brier=1.0, val_auc=0.5,
                           baseline_brier=0.25, deployed=False, error=f"Only {len(X)} valid rows")

    # Time-ordered split: 80% train, 20% val
    split_idx = int(len(X) * 0.8)
    X_train, X_val = X[:split_idx], X[split_idx:]
    y_train, y_val = y[:split_idx], y[split_idx:]

    # 5-minute embargo: drop train rows within 300s of val start
    if len(items) > split_idx:
        val_start_ts = float(items[split_idx].get("timestamp", 0))
        embargo_cutoff = val_start_ts - 300
        keep = []
        for i, item in enumerate(items[:split_idx]):
            if float(item.get("timestamp", 0)) < embargo_cutoff:
                keep.append(i)
        if keep:
            X_train = X_train[keep]
            y_train = y_train[keep]

    n_train = len(X_train)
    n_val = len(X_val)

    if n_train < 100 or n_val < 50:
        return TrainResult(pair=pair, n_train=n_train, n_val=n_val, val_brier=1.0, val_auc=0.5,
                           baseline_brier=0.25, deployed=False, error="Too few rows after split")

    # Signal-weighted: 3x weight on windows where |move_pct_15s| > 0.02%
    # These are the windows where our bot actually trades — model must learn these well
    move_col_idx = FEATURE_COLUMNS.index("move_pct_15s")
    sample_weights = np.where(np.abs(X_train[:, move_col_idx]) > 0.02, 3.0, 1.0)
    n_signal = int(np.sum(sample_weights > 1))
    logger.info(f"{pair}: {n_signal}/{n_train} signal windows get 3x weight")

    # Train LightGBM
    train_data = lgb.Dataset(X_train, label=y_train, weight=sample_weights, feature_name=FEATURE_COLUMNS)
    val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

    params = {
        "objective": "binary",
        "metric": ["binary_logloss", "auc"],
        "num_leaves": 31,
        "learning_rate": 0.05,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "min_child_samples": 20,
        "is_unbalance": True,  # handle outcome class imbalance
        "verbose": -1,
    }

    callbacks = [lgb.early_stopping(50, verbose=False), lgb.log_evaluation(0)]
    model = lgb.train(
        params, train_data,
        num_boost_round=500,
        valid_sets=[val_data],
        callbacks=callbacks,
    )

    # Raw predictions on val
    raw_probs = model.predict(X_val)

    # Platt scaling
    platt = LogisticRegression(C=1.0, solver="lbfgs", max_iter=1000)
    platt.fit(raw_probs.reshape(-1, 1), y_val)
    platt_probs = platt.predict_proba(raw_probs.reshape(-1, 1))[:, 1]

    # Isotonic regression
    isotonic = IsotonicRegression(out_of_bounds="clip")
    isotonic.fit(platt_probs, y_val)
    final_probs = isotonic.predict(platt_probs)

    # Evaluate
    val_brier = brier_score_loss(y_val, final_probs)
    try:
        val_auc = roc_auc_score(y_val, final_probs)
    except ValueError:
        val_auc = 0.5

    baseline_brier = brier_score_loss(y_val, np.full_like(y_val, 0.5, dtype=float))

    logger.info(f"{pair}: n_train={n_train} n_val={n_val} brier={val_brier:.4f} auc={val_auc:.4f} baseline={baseline_brier:.4f}")

    # Deploy gate
    if val_brier >= baseline_brier:
        return TrainResult(pair=pair, n_train=n_train, n_val=n_val, val_brier=val_brier,
                           val_auc=val_auc, baseline_brier=baseline_brier, deployed=False,
                           error="Model not better than baseline")

    # Save pipeline
    pipeline = {"model": model, "platt": platt, "isotonic": isotonic, "features": FEATURE_COLUMNS}
    s3_path = ""

    if s3_client and s3_bucket:
        ts = int(time.time())
        s3_key = f"models/{pair}_{ts}.pkl"
        buf = io.BytesIO()
        pickle.dump(pipeline, buf)
        buf.seek(0)
        s3_client.put_object(Bucket=s3_bucket, Key=s3_key, Body=buf.read())
        s3_path = f"s3://{s3_bucket}/{s3_key}"
        logger.info(f"{pair}: saved to {s3_path}")

        # Update SSM
        if ssm_client:
            ssm_client.put_parameter(Name=f"/polymarket/models/{pair}/latest_path", Value=s3_path, Type="String", Overwrite=True)
            ssm_client.put_parameter(Name=f"/polymarket/models/{pair}/val_brier", Value=str(round(val_brier, 6)), Type="String", Overwrite=True)
            ssm_client.put_parameter(Name=f"/polymarket/models/{pair}/val_auc", Value=str(round(val_auc, 6)), Type="String", Overwrite=True)
            ssm_client.put_parameter(Name=f"/polymarket/models/{pair}/trained_at", Value=str(ts), Type="String", Overwrite=True)

    return TrainResult(pair=pair, n_train=n_train, n_val=n_val, val_brier=val_brier,
                       val_auc=val_auc, baseline_brier=baseline_brier, deployed=True, s3_path=s3_path)


def train_all(region: str = "us-east-1", s3_bucket: str = "polymarket-bot-data-688567279867-use1") -> list[TrainResult]:
    """Train models for all 6 pairs."""
    import os
    import boto3

    profile = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None
    session = boto3.Session(profile_name=profile, region_name=region)
    ddb = session.resource("dynamodb")
    table = ddb.Table("polymarket-bot-training-data")
    s3 = session.client("s3")
    ssm = session.client("ssm")

    results = []
    for pair in PAIRS:
        asset, tf = pair.split("_")
        logger.info(f"Training {pair}...")
        items = load_training_data(table, asset, tf)
        logger.info(f"  Loaded {len(items)} rows")
        result = train_pair(pair, items, s3_client=s3, ssm_client=ssm, s3_bucket=s3_bucket)
        results.append(result)
        logger.info(f"  Result: brier={result.val_brier:.4f} auc={result.val_auc:.4f} deployed={result.deployed}")

    return results
