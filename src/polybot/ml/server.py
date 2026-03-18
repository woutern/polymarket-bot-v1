"""ModelServer — loads trained LightGBM models and serves predictions.

Loads models from S3 on startup, refreshes every 4 hours.
Falls back to 0.5 (neutral) if no model available.
"""

from __future__ import annotations

import io
import logging
import os
import pickle
import time

import numpy as np

logger = logging.getLogger(__name__)

PAIRS = ["BTC_5m", "BTC_15m", "ETH_5m", "ETH_15m", "SOL_5m", "SOL_15m"]


class ModelServer:
    def __init__(self, region: str = "us-east-1"):
        self._models: dict[str, dict] = {}  # pair → pipeline dict
        self._model_ages: dict[str, float] = {}
        self._region = region
        self._last_refresh = 0.0

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
                    resp = ssm.get_parameter(Name=f"/polymarket/models/{pair}/latest_path")
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
                        age_resp = ssm.get_parameter(Name=f"/polymarket/models/{pair}/trained_at")
                        self._model_ages[pair] = float(age_resp["Parameter"]["Value"])
                    except Exception:
                        self._model_ages[pair] = time.time()

                    logger.info(f"model_loaded pair={pair} path={s3_path}")
                except ssm.exceptions.ParameterNotFound:
                    logger.debug(f"no_model pair={pair}")
                except Exception as e:
                    logger.warning(f"model_load_failed pair={pair} error={str(e)[:80]}")

            self._last_refresh = time.time()
        except Exception as e:
            logger.warning(f"model_server_init_failed error={str(e)[:80]}")

    def predict(self, pair: str, features: dict) -> float:
        """Return calibrated probability. 0.5 if no model."""
        pipeline = self._models.get(pair)
        if pipeline is None:
            return 0.5

        try:
            model = pipeline["model"]
            platt = pipeline["platt"]
            isotonic = pipeline["isotonic"]
            feature_cols = pipeline["features"]

            X = np.array([[float(features.get(col, 0)) for col in feature_cols]])
            raw = model.predict(X)[0]
            platt_prob = platt.predict_proba(np.array([[raw]]))[0, 1]
            final = isotonic.predict([platt_prob])[0]
            return float(max(0.01, min(0.99, final)))
        except Exception as e:
            logger.debug(f"predict_failed pair={pair} error={str(e)[:60]}")
            return 0.5

    def get_model_age_hours(self, pair: str) -> float:
        trained_at = self._model_ages.get(pair, 0)
        if trained_at == 0:
            return 999.0
        return (time.time() - trained_at) / 3600

    def has_model(self, pair: str) -> bool:
        return pair in self._models

    def refresh_if_needed(self):
        """Reload models if >4 hours since last refresh."""
        if time.time() - self._last_refresh > 14400:
            self.load_models()
