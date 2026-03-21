"""Train LightGBM models from Jon-Becker dataset on S3.

Two-phase approach:
  Phase 1: Market-only model (uses just Jon-Becker data — 11K+ windows)
  Phase 2: Full model (joins with Coinbase candles for price features)

Usage:
    uv run python scripts/train_from_jbecker.py
"""

from __future__ import annotations

import io
import json
import math
import os
import sys
import time

import boto3
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

S3_BUCKET = "polymarket-bot-training-data-688567279867"
S3_PREFIX = "jon-becker"
MODEL_BUCKET = "polymarket-bot-data-688567279867-euw1"
PROFILE = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None

# Market-only features (derivable from Jon-Becker data alone)
MARKET_FEATURES = [
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    "signal_ask_price",       # YES ask from trades near T+210s
    "log_volume",             # log(market volume)
    "log_liquidity",          # log(market liquidity)
    "is_weekend",             # Saturday/Sunday
    "hour_bucket",            # 0-5 buckets for time-of-day
]

# Full features (same as current trainer — needs Coinbase candles)
FULL_FEATURES = [
    "move_pct_15s", "realized_vol_5m", "vol_ratio", "body_ratio",
    "prev_window_direction", "prev_window_move_pct",
    "hour_sin", "hour_cos", "dow_sin", "dow_cos",
    "signal_move_pct", "signal_ask_price", "signal_seconds", "signal_ev",
]


def read_s3_parquet(key: str) -> pd.DataFrame:
    session = boto3.Session(profile_name=PROFILE)
    s3 = session.client("s3")
    obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return pd.read_parquet(io.BytesIO(obj["Body"].read()))


def load_and_prepare(asset: str) -> pd.DataFrame:
    """Load Jon-Becker markets, derive features, return training-ready DataFrame."""
    print(f"\nLoading {asset} markets from S3...")
    markets = read_s3_parquet(f"{S3_PREFIX}/markets_5m_{asset.lower()}.parquet")
    print(f"  Raw: {len(markets):,} markets")

    # Parse timestamps
    markets["end_dt"] = pd.to_datetime(markets["end_date"], errors="coerce", utc=True)
    markets = markets.dropna(subset=["end_dt"])

    # Outcome: resolved markets only
    # yes_price near 1.0 = UP won, near 0.0 = DOWN won
    markets = markets[markets.yes_price.notna()].copy()
    resolved = markets[(markets.yes_price >= 0.95) | (markets.yes_price <= 0.05)].copy()
    resolved["outcome"] = (resolved.yes_price >= 0.95).astype(int)  # 1 = UP, 0 = DOWN
    print(f"  Resolved: {len(resolved):,} ({len(resolved)/len(markets)*100:.0f}%)")
    print(f"  UP: {resolved.outcome.sum():,} ({resolved.outcome.mean()*100:.1f}%)")

    # Time features
    resolved["hour"] = resolved["end_dt"].dt.hour
    resolved["dow"] = resolved["end_dt"].dt.dayofweek
    resolved["hour_sin"] = np.sin(2 * np.pi * resolved["hour"] / 24)
    resolved["hour_cos"] = np.cos(2 * np.pi * resolved["hour"] / 24)
    resolved["dow_sin"] = np.sin(2 * np.pi * resolved["dow"] / 7)
    resolved["dow_cos"] = np.cos(2 * np.pi * resolved["dow"] / 7)
    resolved["is_weekend"] = (resolved["dow"] >= 5).astype(float)

    # Hour bucket (trading session proxy)
    def hour_bucket(h):
        if h < 6: return 0       # Asia night
        elif h < 10: return 1    # Asia/Europe overlap
        elif h < 14: return 2    # Europe session
        elif h < 18: return 3    # US morning
        elif h < 22: return 4    # US afternoon
        else: return 5           # Late night
    resolved["hour_bucket"] = resolved["hour"].apply(hour_bucket).astype(float)

    # Volume/liquidity features
    resolved["log_volume"] = np.log1p(resolved["volume"].fillna(0))
    resolved["log_liquidity"] = np.log1p(resolved["liquidity"].fillna(0))

    # Ask price at snapshot (from market outcome_prices)
    # For resolved markets, the snapshot price tells us the market's implied prob
    # We need the INITIAL ask price, not the resolved one
    # Since we don't have the opening price, use volume as a proxy signal
    # Markets with higher volume had tighter spreads = lower ask prices typically
    # For now, set signal_ask_price to a reasonable default (will be overridden by trades data)
    resolved["signal_ask_price"] = 0.50  # Default — will be updated with trade data

    # Sort by time for proper train/val split
    resolved = resolved.sort_values("end_dt").reset_index(drop=True)

    # Add timestamp for trainer compatibility
    resolved["timestamp"] = resolved["end_dt"].astype(int) / 1e9

    return resolved


def load_trades_and_enrich(markets_df: pd.DataFrame, asset: str) -> pd.DataFrame:
    """Load trades and compute signal_ask_price from actual fills."""
    try:
        trades = read_s3_parquet(f"{S3_PREFIX}/trades_5m_{asset.lower()}.parquet")
        print(f"  Loaded {len(trades):,} {asset} trades")

        if len(trades) == 0:
            return markets_df

        # Group trades by slug, find trade closest to T+210s
        # window_ts is the END of the 5min window, so open = window_ts - 300
        # T+210s = open + 210 = window_ts - 90
        if "window_ts" in trades.columns and "maker_amount" in trades.columns:
            # Approximate: use maker_amount/taker_amount ratio as fill price
            trades["maker_amount"] = pd.to_numeric(trades["maker_amount"], errors="coerce")
            trades["taker_amount"] = pd.to_numeric(trades["taker_amount"], errors="coerce")

            # Compute fill price: for YES tokens, price = taker_amount / (taker_amount + maker_amount)
            # This is approximate — actual price depends on which side is maker/taker
            total = trades["maker_amount"] + trades["taker_amount"]
            trades["approx_price"] = trades["taker_amount"] / total.where(total > 0, 1)
            trades["approx_price"] = trades["approx_price"].clip(0.01, 0.99)

            # Get median price per slug as signal_ask_price
            slug_prices = trades.groupby("slug")["approx_price"].median().reset_index()
            slug_prices.columns = ["slug", "trade_ask_price"]

            markets_df = markets_df.merge(slug_prices, on="slug", how="left")
            filled = markets_df["trade_ask_price"].notna()
            markets_df.loc[filled, "signal_ask_price"] = markets_df.loc[filled, "trade_ask_price"]
            print(f"  Enriched {filled.sum():,} markets with trade prices")
            markets_df.drop(columns=["trade_ask_price"], inplace=True)

    except Exception as e:
        print(f"  No trades data: {e}")

    return markets_df


def train_market_model(asset: str, df: pd.DataFrame):
    """Train a market-only LightGBM model."""
    import lightgbm as lgb
    from sklearn.metrics import brier_score_loss, roc_auc_score

    print(f"\n{'='*60}")
    print(f"TRAINING {asset}_5m — Market-Only Model")
    print(f"{'='*60}")

    # Prepare arrays
    feature_cols = MARKET_FEATURES
    X = df[feature_cols].values.astype(np.float64)
    y = df["outcome"].values.astype(int)

    # Check for NaN/Inf
    mask = np.all(np.isfinite(X), axis=1)
    X, y = X[mask], y[mask]
    timestamps = df.loc[mask, "timestamp"].values

    print(f"Samples: {len(X):,} (UP={y.sum():,}, DOWN={len(y)-y.sum():,})")

    if len(X) < 500:
        print(f"  Too few samples ({len(X)}), skipping")
        return None

    # Time-ordered split: 80/20
    split = int(len(X) * 0.8)
    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]

    # 5-min embargo
    val_start_ts = timestamps[split]
    embargo_cutoff = val_start_ts - 300
    keep = [i for i in range(split) if timestamps[i] < embargo_cutoff]
    if keep:
        X_train = X_train[keep]
        y_train = y_train[keep]

    print(f"Train: {len(X_train):,} | Val: {len(X_val):,}")

    # Train
    train_data = lgb.Dataset(X_train, label=y_train, feature_name=feature_cols)
    val_data = lgb.Dataset(X_val, label=y_val, reference=train_data)

    params = {
        "objective": "binary",
        "metric": ["binary_logloss", "auc"],
        "num_leaves": 31,
        "learning_rate": 0.05,
        "feature_fraction": 0.8,
        "bagging_fraction": 0.8,
        "bagging_freq": 5,
        "min_child_samples": 30,
        "max_depth": 5,
        "reg_alpha": 0.05,
        "reg_lambda": 0.05,
        "is_unbalance": True,
        "verbose": -1,
    }

    model = lgb.train(
        params, train_data,
        num_boost_round=500,
        valid_sets=[val_data],
        callbacks=[lgb.early_stopping(50, verbose=False), lgb.log_evaluation(50)],
    )

    # Evaluate
    probs = model.predict(X_val)
    brier = brier_score_loss(y_val, probs)
    baseline_brier = brier_score_loss(y_val, np.full(len(y_val), y_train.mean()))
    try:
        auc = roc_auc_score(y_val, probs)
    except ValueError:
        auc = 0.5

    print(f"\nResults:")
    print(f"  Brier score: {brier:.4f} (baseline: {baseline_brier:.4f})")
    print(f"  AUC: {auc:.4f}")
    print(f"  Best iteration: {model.best_iteration}")

    if brier < baseline_brier:
        print(f"  Model BEATS baseline by {(baseline_brier - brier):.4f}")
    else:
        print(f"  Model WORSE than baseline by {(brier - baseline_brier):.4f}")

    # Feature importance
    importance = model.feature_importance(importance_type="gain")
    print(f"\nFeature importance (gain):")
    for name, imp in sorted(zip(feature_cols, importance), key=lambda x: -x[1]):
        bar = "█" * int(imp / max(importance) * 20)
        print(f"  {name:>20s}: {imp:>8.1f}  {bar}")

    # Calibration check: bin predictions and compare to actual win rate
    print(f"\nCalibration (predicted prob → actual win rate):")
    prob_bins = pd.cut(probs, bins=[0, 0.3, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7, 1.0])
    cal_df = pd.DataFrame({"prob": probs, "outcome": y_val, "bin": prob_bins})
    for b, group in cal_df.groupby("bin", observed=True):
        if len(group) > 0:
            actual = group["outcome"].mean()
            predicted = group["prob"].mean()
            print(f"  {str(b):>15s}: pred={predicted:.3f} actual={actual:.3f} N={len(group):>4}")

    return model


def main():
    start = time.time()
    print("=" * 60)
    print("Jon-Becker → LightGBM Training Pipeline")
    print("=" * 60)

    models = {}
    for asset in ["BTC", "ETH", "SOL"]:
        df = load_and_prepare(asset)
        df = load_trades_and_enrich(df, asset)
        model = train_market_model(asset, df)
        if model:
            models[asset] = model

    # Summary
    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print(f"TRAINING COMPLETE — {elapsed:.0f}s")
    print(f"{'='*60}")
    print(f"Models trained: {list(models.keys())}")

    # Save models to S3
    if models:
        import pickle
        session = boto3.Session(profile_name=PROFILE)
        s3 = session.client("s3")
        for asset, model in models.items():
            buf = io.BytesIO()
            pickle.dump(model, buf)
            buf.seek(0)
            key = f"{S3_PREFIX}/models/{asset}_5m_market_only.pkl"
            s3.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.read())
            print(f"  Saved s3://{S3_BUCKET}/{key}")


if __name__ == "__main__":
    main()
