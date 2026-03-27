"""ModelServer — loads trained LightGBM models and serves predictions.

Loads models from S3 on startup, refreshes every 4 hours.
Falls back to 0.5 (neutral) if no model available.
"""

from __future__ import annotations

import os
import pickle
import time
from collections import deque

import numpy as np
import structlog

logger = structlog.get_logger()

PAIRS = [
    "BTC_5m",
    "ETH_5m",
    "SOL_5m",
    "XRP_5m",
    "BTC_15m",
    "ETH_15m",
    "SOL_15m",
    "BTC_1h",
    "ETH_1h",
    "SOL_1h",
    "XRP_1h",
]

# Adaptive threshold bounds — raised after consecutive losses at low confidence
_DEFAULT_GATE = 0.60
_MIN_GATE = 0.58
_MAX_GATE = 0.60
_MIN_HISTORY = 20  # predictions needed before adapting


class ModelServer:
    def __init__(self, region: str = "eu-west-1"):
        self._models: dict[str, dict] = {}  # pair → pipeline dict
        self._model_ages: dict[str, float] = {}
        self._region = region
        self._last_refresh = 0.0
        self._pred_history: dict[str, deque] = {}  # pair → last 100 predictions

    def load_models(self):
        """Load all available models from S3 via SSM paths."""
        try:
            profile = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None
            import boto3

            session = boto3.Session(profile_name=profile, region_name=self._region)
            ssm = session.client("ssm")
            s3 = session.client("s3")

            for pair in PAIRS:
                try:
                    resp = ssm.get_parameter(
                        Name=f"/polymarket/models/{pair}/latest_path"
                    )
                    s3_path = resp["Parameter"]["Value"]
                    if not s3_path.startswith("s3://"):
                        continue

                    # Parse s3://bucket/key
                    parts = s3_path.replace("s3://", "").split("/", 1)
                    bucket = parts[0]
                    key = parts[1]

                    obj = s3.get_object(Bucket=bucket, Key=key)
                    pipeline = pickle.loads(obj["Body"].read())
                    self._models[pair] = pipeline

                    # Get model age
                    try:
                        age_resp = ssm.get_parameter(
                            Name=f"/polymarket/models/{pair}/trained_at"
                        )
                        self._model_ages[pair] = float(age_resp["Parameter"]["Value"])
                    except Exception:
                        self._model_ages[pair] = time.time()

                    age_h = round((time.time() - self._model_ages[pair]) / 3600, 1)
                    logger.info(
                        "model_loaded", pair=pair, path=s3_path, age_hours=age_h
                    )
                except ssm.exceptions.ParameterNotFound:
                    logger.debug("no_model", pair=pair)
                except Exception as e:
                    logger.warning("model_load_failed", pair=pair, error=str(e)[:80])

            self._last_refresh = time.time()
        except Exception as e:
            logger.warning("model_server_init_failed", error=str(e)[:80])

    def predict(self, pair: str, features: dict) -> float:
        """Return calibrated probability. 0.5 if no model."""
        pipeline = self._models.get(pair)
        if pipeline is None:
            return 0.5

        try:
            model = pipeline["model"]
            feature_cols = pipeline["features"]
            X = np.array([[float(features.get(col, 0)) for col in feature_cols]])
            raw = model.predict(X)[0]

            # Apply calibration if available (older pipeline format)
            platt = pipeline.get("platt")
            isotonic = pipeline.get("isotonic")
            if platt and isotonic:
                platt_prob = platt.predict_proba(np.array([[raw]]))[0, 1]
                final = isotonic.predict([platt_prob])[0]
            else:
                # Raw LightGBM output is already a probability
                final = raw

            result = float(max(0.01, min(0.99, final)))
            # Track prediction for adaptive threshold
            if pair not in self._pred_history:
                self._pred_history[pair] = deque(maxlen=100)
            self._pred_history[pair].append(result)
            return result
        except Exception as e:
            logger.debug("predict_failed", pair=pair, error=str(e)[:60])
            return 0.5

    def get_model_age_hours(self, pair: str) -> float:
        trained_at = self._model_ages.get(pair, 0)
        if trained_at == 0:
            return 999.0
        return (time.time() - trained_at) / 3600

    def has_model(self, pair: str) -> bool:
        return pair in self._models

    def get_adaptive_threshold(self, pair: str) -> float:
        """Return lgbm_prob gate that adapts to model confidence level.

        If rolling mean < 0.55: model is underconfident → lower gate to 0.52
        If rolling mean > 0.65: model is well-trained → raise gate to 0.60
        Linear interpolation between.
        """
        history = self._pred_history.get(pair)
        if not history or len(history) < _MIN_HISTORY:
            return _DEFAULT_GATE

        rolling_mean = sum(history) / len(history)

        if rolling_mean < 0.55:
            threshold = _DEFAULT_GATE  # 0.52 — loose gate for undertrained model
        elif rolling_mean > 0.65:
            threshold = _MAX_GATE  # 0.60 — tight gate for confident model
        else:
            # Linear interpolation: 0.55→0.52, 0.65→0.60
            t = (rolling_mean - 0.55) / 0.10
            threshold = _DEFAULT_GATE + t * (_MAX_GATE - _DEFAULT_GATE)

        threshold = max(_MIN_GATE, min(_MAX_GATE, threshold))

        if len(history) % 50 == 0:  # log every 50 predictions
            logger.info(
                "adaptive_threshold",
                pair=pair,
                rolling_mean=round(rolling_mean, 4),
                threshold=round(threshold, 4),
                n_predictions=len(history),
            )

        return threshold

    def refresh_if_needed(self):
        """Reload models if >4 hours since last refresh."""
        if time.time() - self._last_refresh > 14400:
            self.load_models()
