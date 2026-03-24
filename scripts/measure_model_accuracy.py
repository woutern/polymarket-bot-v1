"""Measure BTC_5m model accuracy across confidence buckets.

Usage:
    PYTHONPATH=src uv run python3 scripts/measure_model_accuracy.py
"""

import io
import math
import pickle
import sys

import numpy as np


def main():
    import boto3

    session = boto3.Session(profile_name="playground", region_name="eu-west-1")
    ssm = session.client("ssm")
    s3 = session.client("s3")

    pairs = ["BTC_5m", "ETH_5m", "SOL_5m"]

    for pair in pairs:
        print(f"\n{'=' * 60}")
        print(f"  {pair}")
        print(f"{'=' * 60}")

        # Load model
        try:
            path_resp = ssm.get_parameter(Name=f"/polymarket/models/{pair}/latest_path")
            s3_path = path_resp["Parameter"]["Value"]
        except Exception as e:
            print(f"  No model found: {e}")
            continue

        parts = s3_path.replace("s3://", "").split("/", 1)
        obj = s3.get_object(Bucket=parts[0], Key=parts[1])
        pipeline = pickle.loads(obj["Body"].read())
        model = pipeline["model"]
        platt = pipeline.get("platt")
        isotonic = pipeline.get("isotonic")
        features = pipeline["features"]

        # Get trained_at
        try:
            trained_resp = ssm.get_parameter(
                Name=f"/polymarket/models/{pair}/trained_at"
            )
            import time

            age_h = round(
                (time.time() - float(trained_resp["Parameter"]["Value"])) / 3600, 1
            )
            print(f"  Model age: {age_h}h")
        except Exception:
            pass

        auc_resp = ssm.get_parameter(Name=f"/polymarket/models/{pair}/val_auc")
        print(f"  Reported AUC: {auc_resp['Parameter']['Value']}")
        print(f"  Features: {features}")

        # Load training data
        asset, tf = pair.split("_")
        ddb = session.resource("dynamodb")
        table = ddb.Table("polymarket-bot-training-data")
        items = []
        resp = table.scan(
            FilterExpression="asset = :a AND timeframe = :t",
            ExpressionAttributeValues={":a": asset, ":t": tf},
        )
        items.extend(resp.get("Items", []))
        while resp.get("LastEvaluatedKey"):
            resp = table.scan(
                FilterExpression="asset = :a AND timeframe = :t",
                ExpressionAttributeValues={":a": asset, ":t": tf},
                ExclusiveStartKey=resp["LastEvaluatedKey"],
            )
            items.extend(resp.get("Items", []))

        print(f"  Total training rows: {len(items)}")

        # Build feature matrix
        optional = {
            "signal_move_pct",
            "signal_ask_price",
            "signal_seconds",
            "signal_ev",
        }
        X_rows, y_rows = [], []
        for item in items:
            feats = []
            valid = True
            for col in features:
                val = item.get(col)
                if val is None:
                    if col in optional:
                        feats.append(0.0)
                        continue
                    valid = False
                    break
                f = float(val)
                if math.isnan(f) or math.isinf(f):
                    valid = False
                    break
                feats.append(f)
            if not valid:
                continue
            outcome = item.get("outcome")
            if outcome is None:
                continue
            X_rows.append(feats)
            y_rows.append(int(outcome))

        X = np.array(X_rows)
        y = np.array(y_rows)
        print(f"  Valid prediction rows: {len(X)}")

        if len(X) < 100:
            print("  Not enough data for meaningful accuracy measurement")
            continue

        # Predict
        raw = model.predict(X)
        if platt and isotonic:
            platt_probs = platt.predict_proba(raw.reshape(-1, 1))[:, 1]
            probs = isotonic.predict(platt_probs)
        else:
            probs = raw
        probs = np.clip(probs, 0.01, 0.99)

        # Raw move_pct_15s direction accuracy (baseline)
        move_idx = (
            features.index("move_pct_15s") if "move_pct_15s" in features else None
        )
        if move_idx is not None:
            moves = X[:, move_idx]
            move_correct = ((moves > 0) & (y == 1)) | ((moves < 0) & (y == 0))
            strong_mask = np.abs(moves) > 0.02
            print(f"\n  --- Raw price-move baseline ---")
            print(
                f"  All moves: {move_correct.sum()}/{len(y)} = {move_correct.mean() * 100:.1f}%"
            )
            if strong_mask.sum() > 0:
                strong_correct = move_correct[strong_mask]
                print(
                    f"  Strong moves (|move|>0.02%): {strong_correct.sum()}/{strong_mask.sum()} = {strong_correct.mean() * 100:.1f}%"
                )

        # Model accuracy by confidence bucket
        print(f"\n  --- Model accuracy by confidence bucket ---")
        buckets = [
            (0.00, 0.30, "Very Strong DOWN (<0.30)"),
            (0.30, 0.40, "Strong DOWN (0.30-0.40)"),
            (0.40, 0.45, "Moderate DOWN (0.40-0.45)"),
            (0.45, 0.50, "Weak DOWN (0.45-0.50)"),
            (0.50, 0.55, "Weak UP (0.50-0.55)"),
            (0.55, 0.60, "Moderate UP (0.55-0.60)"),
            (0.60, 0.70, "Strong UP (0.60-0.70)"),
            (0.70, 1.01, "Very Strong UP (>0.70)"),
        ]
        for lo, hi, label in buckets:
            mask = (probs >= lo) & (probs < hi)
            if mask.sum() == 0:
                continue
            bucket_y = y[mask]
            predicted_up = probs[mask] >= 0.50
            actual_up = bucket_y == 1
            correct = (predicted_up == actual_up).sum()
            total = len(bucket_y)
            actual_rate = actual_up.mean() * 100
            print(
                f"  {label:35s} n={total:5d}  accuracy={correct / total * 100:.1f}%  actual_UP={actual_rate:.1f}%"
            )

        # Overall stats
        predicted_up = probs >= 0.50
        actual_up = y == 1
        overall = (predicted_up == actual_up).sum()
        print(f"\n  --- Overall ---")
        print(
            f"  Direction accuracy: {overall}/{len(y)} = {overall / len(y) * 100:.1f}%"
        )
        print(f"  Base rate UP: {y.mean() * 100:.1f}%")

        try:
            from sklearn.metrics import brier_score_loss, roc_auc_score

            print(f"  AUC (on full data): {roc_auc_score(y, probs):.4f}")
            print(f"  Brier score: {brier_score_loss(y, probs):.4f}")
            print(
                f"  Baseline Brier: {brier_score_loss(y, np.full_like(y, 0.5, dtype=float)):.4f}"
            )
        except Exception as e:
            print(f"  sklearn metrics error: {e}")

        # Prediction distribution
        print(f"\n  --- Prediction distribution ---")
        for lo, hi in [
            (0, 0.3),
            (0.3, 0.4),
            (0.4, 0.5),
            (0.5, 0.6),
            (0.6, 0.7),
            (0.7, 1.01),
        ]:
            n = ((probs >= lo) & (probs < hi)).sum()
            print(f"  {lo:.1f}-{hi:.1f}: {n:5d} ({n / len(probs) * 100:.1f}%)")

        # Calibration check: for each bucket, is predicted prob close to actual rate?
        print(f"\n  --- Calibration (predicted prob vs actual rate) ---")
        for lo, hi in [
            (0.0, 0.3),
            (0.3, 0.4),
            (0.4, 0.5),
            (0.5, 0.6),
            (0.6, 0.7),
            (0.7, 1.0),
        ]:
            mask = (probs >= lo) & (probs < hi)
            if mask.sum() < 10:
                continue
            avg_pred = probs[mask].mean()
            avg_actual = y[mask].mean()
            gap = abs(avg_pred - avg_actual)
            quality = "GOOD" if gap < 0.05 else ("OK" if gap < 0.10 else "BAD")
            print(
                f"  pred={avg_pred:.3f}  actual={avg_actual:.3f}  gap={gap:.3f}  [{quality}]  n={mask.sum()}"
            )

    print(f"\n{'=' * 60}")
    print("Done.")


if __name__ == "__main__":
    main()
