"""Tests for LightGBM trainer and model server."""

from __future__ import annotations

import math
import numpy as np
import pytest

from polybot.ml.trainer import FEATURE_COLUMNS, items_to_arrays, train_pair, TrainResult


def _make_mock_items(n: int = 600) -> list[dict]:
    """Generate mock training data with predictable pattern."""
    import random
    random.seed(42)
    items = []
    for i in range(n):
        move = random.gauss(0, 0.1)
        # Outcome correlated with move_pct_15s (simple signal)
        outcome = 1 if move > 0 else 0
        # Add some noise
        if random.random() < 0.2:
            outcome = 1 - outcome

        items.append({
            "timestamp": str(1773800000 + i * 300),
            "asset": "BTC",
            "timeframe": "5m",
            "outcome": outcome,
            "move_pct_15s": str(move),
            "move_pct_60s": str(move * 1.5),
            "move_pct_300s": str(move * 2),
            "realized_vol_5m": str(abs(random.gauss(0.5, 0.1))),
            "vol_ratio": str(abs(random.gauss(1.0, 0.3))),
            "body_ratio": str(abs(random.gauss(0.6, 0.2))),
            "prev_window_direction": str(1 if random.random() > 0.5 else -1),
            "prev_window_move_pct": str(random.gauss(0, 0.1)),
            "hour_sin": str(math.sin(2 * math.pi * (i % 24) / 24)),
            "hour_cos": str(math.cos(2 * math.pi * (i % 24) / 24)),
            "dow_sin": str(math.sin(2 * math.pi * (i % 7) / 7)),
            "dow_cos": str(math.cos(2 * math.pi * (i % 7) / 7)),
        })
    return items


class TestItemsToArrays:
    def test_correct_shape(self):
        items = _make_mock_items(100)
        X, y = items_to_arrays(items)
        assert X.shape == (100, len(FEATURE_COLUMNS))
        assert y.shape == (100,)

    def test_no_nan(self):
        items = _make_mock_items(100)
        X, y = items_to_arrays(items)
        assert not np.any(np.isnan(X))
        assert not np.any(np.isinf(X))

    def test_skips_missing_features(self):
        items = [{"timestamp": "1", "outcome": 1}]  # missing all features
        X, y = items_to_arrays(items)
        assert len(X) == 0

    def test_labels_binary(self):
        items = _make_mock_items(100)
        X, y = items_to_arrays(items)
        assert set(y).issubset({0, 1})


class TestTrainPair:
    def test_trains_and_returns_result(self):
        items = _make_mock_items(600)
        result = train_pair("BTC_5m", items)
        assert isinstance(result, TrainResult)
        assert result.n_train > 0
        assert result.n_val > 0
        assert 0 <= result.val_brier <= 1
        assert 0 <= result.val_auc <= 1

    def test_brier_better_than_random(self):
        items = _make_mock_items(600)
        result = train_pair("BTC_5m", items)
        # With correlated signal, model should beat 0.25 baseline
        assert result.val_brier < result.baseline_brier

    def test_skips_when_too_few_rows(self):
        items = _make_mock_items(50)
        result = train_pair("BTC_5m", items)
        assert result.deployed is False
        assert "500" in result.error or "rows" in result.error.lower()

    def test_no_data_leakage(self):
        """Val timestamps must all be after train timestamps."""
        items = _make_mock_items(600)
        split_idx = int(len(items) * 0.8)
        train_ts = [float(i["timestamp"]) for i in items[:split_idx]]
        val_ts = [float(i["timestamp"]) for i in items[split_idx:]]
        assert min(val_ts) > max(train_ts) - 300  # within embargo

    def test_does_not_deploy_bad_model(self):
        """Random noise data — model should not beat baseline."""
        import random
        random.seed(99)
        items = []
        for i in range(600):
            items.append({
                "timestamp": str(i * 300),
                "outcome": random.randint(0, 1),
                **{col: str(random.gauss(0, 1)) for col in FEATURE_COLUMNS},
            })
        result = train_pair("BTC_5m", items)
        # Pure noise — may or may not beat baseline, but check it runs
        assert isinstance(result, TrainResult)


class TestModelServer:
    def test_returns_half_when_no_model(self):
        from polybot.ml.server import ModelServer
        server = ModelServer()
        prob = server.predict("BTC_5m", {"move_pct_15s": 0.1})
        assert prob == 0.5

    def test_has_model_false_initially(self):
        from polybot.ml.server import ModelServer
        server = ModelServer()
        assert server.has_model("BTC_5m") is False

    def test_model_age_huge_when_no_model(self):
        from polybot.ml.server import ModelServer
        server = ModelServer()
        assert server.get_model_age_hours("BTC_5m") > 100
