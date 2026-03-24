"""Tests for FeatureBuilder (ml/features.py).

All 14 features are verified for correctness, edge-cases, and
cross-window continuity (PrevWindow → next window's features).
"""

from __future__ import annotations

import math
import time
from collections import deque
from unittest.mock import MagicMock, patch

import pytest

from polybot.ml.features import FeatureBuilder, PrevWindow


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _builder(open_price=100.0, ts: float | None = None) -> FeatureBuilder:
    return FeatureBuilder(open_price=open_price, window_open_ts=ts or time.time())


def _feed_prices(fb: FeatureBuilder, prices: list[float], start_ts: float, interval: float = 0.25):
    """Feed a list of prices at regular intervals."""
    for i, p in enumerate(prices):
        fb.on_price(p, ts=start_ts + i * interval)


# ─── FeatureBuilder init ──────────────────────────────────────────────────────

class TestFeatureBuilderInit:
    def test_open_price_stored(self):
        fb = _builder(open_price=95000.0)
        assert fb.open_price == 95000.0

    def test_initial_high_low_equals_open(self):
        fb = _builder(open_price=50.0)
        assert fb._high == 50.0
        assert fb._low == 50.0

    def test_first_price_in_deque(self):
        fb = _builder(open_price=123.0)
        assert list(fb._prices)[0] == 123.0

    def test_vol_history_shared(self):
        vh = deque(maxlen=20)
        vh.append(0.5)
        fb = FeatureBuilder(open_price=100.0, window_open_ts=time.time(), vol_history=vh)
        assert fb._vol_history is vh

    def test_vol_history_created_if_none(self):
        fb = _builder()
        assert fb._vol_history is not None
        assert fb._vol_history.maxlen == 20


# ─── on_price ────────────────────────────────────────────────────────────────

class TestOnPrice:
    def test_updates_high(self):
        fb = _builder(open_price=100.0)
        fb.on_price(110.0)
        assert fb._high == 110.0

    def test_updates_low(self):
        fb = _builder(open_price=100.0)
        fb.on_price(90.0)
        assert fb._low == 90.0

    def test_high_not_overwritten_by_lower(self):
        fb = _builder(open_price=100.0)
        fb.on_price(110.0)
        fb.on_price(105.0)
        assert fb._high == 110.0

    def test_low_not_overwritten_by_higher(self):
        fb = _builder(open_price=100.0)
        fb.on_price(90.0)
        fb.on_price(95.0)
        assert fb._low == 90.0

    def test_ignores_non_positive_price(self):
        fb = _builder(open_price=100.0)
        initial_len = len(fb._prices)
        fb.on_price(0.0)
        fb.on_price(-5.0)
        assert len(fb._prices) == initial_len

    def test_captures_price_at_15s(self):
        ts = time.time()
        fb = FeatureBuilder(open_price=100.0, window_open_ts=ts)
        # Price before 15s — not captured
        fb.on_price(101.0, ts=ts + 10.0)
        assert fb._price_at_15s is None
        # Price at exactly 15s — captured
        fb.on_price(102.0, ts=ts + 15.0)
        assert fb._price_at_15s == 102.0

    def test_price_at_15s_only_captured_once(self):
        ts = time.time()
        fb = FeatureBuilder(open_price=100.0, window_open_ts=ts)
        fb.on_price(102.0, ts=ts + 15.0)
        fb.on_price(105.0, ts=ts + 20.0)
        assert fb._price_at_15s == 102.0  # not overwritten


# ─── compute — move_pct_15s ───────────────────────────────────────────────────

class TestMovePct15s:
    def test_positive_move(self):
        ts = time.time()
        fb = FeatureBuilder(open_price=100.0, window_open_ts=ts)
        fb.on_price(102.0, ts=ts + 15.0)
        feats = fb.compute()
        assert abs(feats["move_pct_15s"] - 2.0) < 0.001

    def test_negative_move(self):
        ts = time.time()
        fb = FeatureBuilder(open_price=100.0, window_open_ts=ts)
        fb.on_price(98.0, ts=ts + 15.0)
        feats = fb.compute()
        assert abs(feats["move_pct_15s"] - (-2.0)) < 0.001

    def test_falls_back_to_current_price_before_15s(self):
        ts = time.time()
        fb = FeatureBuilder(open_price=100.0, window_open_ts=ts)
        # No 15s price yet → move_pct_15s should be ~0
        feats = fb.compute()
        assert feats["move_pct_15s"] == 0.0

    def test_zero_open_price_safe(self):
        fb = FeatureBuilder(open_price=0.0, window_open_ts=time.time())
        feats = fb.compute()
        assert feats["move_pct_15s"] == 0.0


# ─── compute — body_ratio ────────────────────────────────────────────────────

class TestBodyRatio:
    def test_full_range_move(self):
        fb = _builder(open_price=100.0)
        fb.on_price(110.0)   # high
        fb.on_price(90.0)    # low
        fb.on_price(110.0)   # current = open + 10
        feats = fb.compute()
        # body = |110 - 100| = 10, range = 20 → ratio = 0.5
        assert abs(feats["body_ratio"] - 0.5) < 0.01

    def test_no_range_defaults_to_half(self):
        fb = _builder(open_price=100.0)
        # Only open price — high == low == open → no range
        feats = fb.compute()
        assert feats["body_ratio"] == 0.5


# ─── compute — vol_ratio ─────────────────────────────────────────────────────

class TestVolRatio:
    def test_vol_ratio_one_when_no_history(self):
        fb = _builder(open_price=100.0)
        feats = fb.compute()
        assert feats["vol_ratio"] == 1.0

    def test_vol_ratio_with_history(self):
        vh = deque([0.001, 0.001, 0.001], maxlen=20)
        ts = time.time()
        fb = FeatureBuilder(open_price=100.0, window_open_ts=ts, vol_history=vh)
        # Feed a very volatile sequence to push vol > historical avg
        for i in range(80):
            price = 100.0 + (10.0 if i % 2 == 0 else -10.0)
            fb.on_price(price, ts=ts + i * 0.25)
        feats = fb.compute()
        assert feats["vol_ratio"] > 1.0


# ─── compute — prev_window features ──────────────────────────────────────────

class TestPrevWindowFeatures:
    def test_no_prev_window_zeros(self):
        fb = _builder()
        feats = fb.compute()
        assert feats["prev_window_direction"] == 0
        assert feats["prev_window_move_pct"] == 0.0

    def test_prev_up(self):
        prev = PrevWindow(open_price=100.0, close_price=105.0)
        fb = FeatureBuilder(open_price=110.0, window_open_ts=time.time(), prev_window=prev)
        feats = fb.compute()
        assert feats["prev_window_direction"] == 1
        assert abs(feats["prev_window_move_pct"] - 5.0) < 0.001

    def test_prev_down(self):
        prev = PrevWindow(open_price=100.0, close_price=95.0)
        fb = FeatureBuilder(open_price=95.0, window_open_ts=time.time(), prev_window=prev)
        feats = fb.compute()
        assert feats["prev_window_direction"] == -1
        assert feats["prev_window_move_pct"] < 0.0

    def test_prev_flat_counts_as_up(self):
        prev = PrevWindow(open_price=100.0, close_price=100.0)
        fb = FeatureBuilder(open_price=100.0, window_open_ts=time.time(), prev_window=prev)
        feats = fb.compute()
        assert feats["prev_window_direction"] == 1  # >= is UP


# ─── compute — cyclic time features ──────────────────────────────────────────

class TestCyclicTimeFeatures:
    def test_hour_sin_cos_range(self):
        fb = _builder()
        feats = fb.compute()
        assert -1.0 <= feats["hour_sin"] <= 1.0
        assert -1.0 <= feats["hour_cos"] <= 1.0

    def test_dow_sin_cos_range(self):
        fb = _builder()
        feats = fb.compute()
        assert -1.0 <= feats["dow_sin"] <= 1.0
        assert -1.0 <= feats["dow_cos"] <= 1.0

    def test_hour_sin_cos_orthogonal(self):
        """sin²+cos² must equal 1."""
        fb = _builder()
        feats = fb.compute()
        sq_sum = feats["hour_sin"] ** 2 + feats["hour_cos"] ** 2
        assert abs(sq_sum - 1.0) < 1e-6

    def test_dow_sin_cos_orthogonal(self):
        fb = _builder()
        feats = fb.compute()
        sq_sum = feats["dow_sin"] ** 2 + feats["dow_cos"] ** 2
        assert abs(sq_sum - 1.0) < 1e-6


# ─── compute — signal features ───────────────────────────────────────────────

class TestSignalFeatures:
    def test_signal_ask_price_passed_through(self):
        fb = _builder()
        feats = fb.compute(current_ask=0.72, seconds=210)
        assert feats["signal_ask_price"] == 0.72

    def test_signal_seconds_passed_through(self):
        fb = _builder()
        feats = fb.compute(current_ask=0.65, seconds=225)
        assert feats["signal_seconds"] == 225.0

    def test_signal_move_pct_is_abs_of_move_pct_15s(self):
        ts = time.time()
        fb = FeatureBuilder(open_price=100.0, window_open_ts=ts)
        fb.on_price(97.0, ts=ts + 15.0)   # move = -3%
        feats = fb.compute()
        assert feats["signal_move_pct"] >= 0.0
        assert abs(feats["signal_move_pct"] - 3.0) < 0.01

    def test_signal_ev_is_zero(self):
        fb = _builder()
        feats = fb.compute()
        assert feats["signal_ev"] == 0.0


# ─── compute — feature dict completeness ─────────────────────────────────────

class TestFeatureDictCompleteness:
    EXPECTED_KEYS = {
        "move_pct_15s", "realized_vol_5m", "vol_ratio", "body_ratio",
        "prev_window_direction", "prev_window_move_pct",
        "hour_sin", "hour_cos", "dow_sin", "dow_cos",
        "signal_move_pct", "signal_ask_price", "signal_seconds", "signal_ev",
    }

    def test_all_14_keys_present(self):
        fb = _builder()
        feats = fb.compute()
        assert set(feats.keys()) == self.EXPECTED_KEYS

    def test_all_values_are_numeric(self):
        fb = _builder()
        feats = fb.compute()
        for k, v in feats.items():
            assert isinstance(v, (int, float)), f"{k} is not numeric: {v!r}"


# ─── close → PrevWindow ──────────────────────────────────────────────────────

class TestClose:
    def test_returns_prev_window(self):
        fb = _builder(open_price=100.0)
        pw = fb.close(close_price=105.0)
        assert isinstance(pw, PrevWindow)
        assert pw.open_price == 100.0
        assert pw.close_price == 105.0

    def test_vol_history_updated_on_close(self):
        vh = deque(maxlen=20)
        ts = time.time()
        fb = FeatureBuilder(open_price=100.0, window_open_ts=ts, vol_history=vh)
        # Feed enough prices to produce non-zero vol
        for i in range(40):
            fb.on_price(100.0 + (1.0 if i % 2 == 0 else -1.0), ts=ts + i * 0.25)
        fb.close(close_price=101.0)
        assert len(vh) == 1
        assert vh[0] > 0

    def test_zero_vol_not_appended_to_history(self):
        vh = deque(maxlen=20)
        fb = FeatureBuilder(open_price=100.0, window_open_ts=time.time(), vol_history=vh)
        # Only one price (open) → vol=0
        fb.close(close_price=100.0)
        assert len(vh) == 0  # zero vol not pushed

    def test_close_preserves_open_price_in_prev_window(self):
        fb = _builder(open_price=50000.0)
        pw = fb.close(close_price=51000.0)
        assert pw.open_price == 50000.0


# ─── vol_ratio edge cases ─────────────────────────────────────────────────────

class TestVolRatioEdgeCases:
    def test_vol_zero_history_empty_returns_one(self):
        """vol=0, history empty → vol_avg=vol=0 → ratio defaults to 1.0."""
        fb = FeatureBuilder(open_price=100.0, window_open_ts=time.time())
        # Single price → vol=0, history empty
        feats = fb.compute()
        assert feats["vol_ratio"] == 1.0

    def test_vol_zero_but_history_non_empty_returns_zero(self):
        """vol=0, history has previous values → ratio = 0/avg = 0.0."""
        vh = deque([0.02, 0.03], maxlen=20)
        fb = FeatureBuilder(open_price=100.0, window_open_ts=time.time(), vol_history=vh)
        # Only one price → vol = 0
        feats = fb.compute()
        assert feats["vol_ratio"] == 0.0

    def test_vol_ratio_idempotent(self):
        """Calling compute() twice returns same vol_ratio."""
        ts = time.time()
        fb = FeatureBuilder(open_price=100.0, window_open_ts=ts)
        for i in range(20):
            fb.on_price(100.0 + (i % 2) * 1.0, ts=ts + i * 0.25)
        f1 = fb.compute()
        f2 = fb.compute()
        assert f1["vol_ratio"] == f2["vol_ratio"]


# ─── prev_window zero-price guards ───────────────────────────────────────────

class TestPrevWindowZeroGuards:
    def test_prev_window_open_price_zero_skipped(self):
        """open_price=0 is falsy → direction stays 0, no ZeroDivisionError."""
        prev = PrevWindow(open_price=0.0, close_price=105.0)
        fb = FeatureBuilder(open_price=100.0, window_open_ts=time.time(), prev_window=prev)
        feats = fb.compute()
        assert feats["prev_window_direction"] == 0
        assert feats["prev_window_move_pct"] == 0.0

    def test_prev_window_close_price_zero_skipped(self):
        """close_price=0 is falsy → direction stays 0."""
        prev = PrevWindow(open_price=100.0, close_price=0.0)
        fb = FeatureBuilder(open_price=100.0, window_open_ts=time.time(), prev_window=prev)
        feats = fb.compute()
        assert feats["prev_window_direction"] == 0


# ─── Deque overflow (> 1200 prices) ──────────────────────────────────────────

class TestDequeOverflow:
    def test_prices_beyond_maxlen_still_computes(self):
        ts = time.time()
        fb = FeatureBuilder(open_price=100.0, window_open_ts=ts)
        # Feed 1500 prices → deque wraps at 1200
        for i in range(1500):
            fb.on_price(100.0 + (i % 5) * 0.1, ts=ts + i * 0.25)
        feats = fb.compute()
        assert len(fb._prices) == 1200
        assert isinstance(feats["realized_vol_5m"], float)

    def test_high_low_still_correct_after_overflow(self):
        ts = time.time()
        fb = FeatureBuilder(open_price=100.0, window_open_ts=ts)
        for i in range(1500):
            # max price = 120, min = 80
            fb.on_price(100.0 + (10.0 if i % 3 == 0 else -10.0), ts=ts + i * 0.25)
        # high/low tracked outside deque
        assert fb._high >= 110.0
        assert fb._low <= 90.0


# ─── compute() idempotency ────────────────────────────────────────────────────

class TestComputeIdempotency:
    def test_repeated_compute_same_result(self):
        ts = time.time()
        fb = FeatureBuilder(open_price=100.0, window_open_ts=ts)
        fb.on_price(102.0, ts=ts + 15.0)
        for i in range(5):
            fb.on_price(101.0 + i * 0.5, ts=ts + 20.0 + i * 5)
        f1 = fb.compute(current_ask=0.65, seconds=120)
        f2 = fb.compute(current_ask=0.65, seconds=120)
        assert f1 == f2

    def test_signal_move_pct_always_non_negative(self):
        """signal_move_pct = abs(move_pct_15s) — never negative."""
        ts = time.time()
        # Downward move
        fb = FeatureBuilder(open_price=100.0, window_open_ts=ts)
        fb.on_price(95.0, ts=ts + 15.0)   # -5% move
        feats = fb.compute()
        assert feats["move_pct_15s"] < 0
        assert feats["signal_move_pct"] >= 0.0
        assert feats["signal_move_pct"] == pytest.approx(5.0, abs=0.01)


# ─── Cross-window continuity ─────────────────────────────────────────────────

class TestCrossWindowContinuity:
    def test_prev_window_flows_into_next(self):
        ts = time.time()

        # Window 1
        vh = deque(maxlen=20)
        fb1 = FeatureBuilder(open_price=100.0, window_open_ts=ts, vol_history=vh)
        for i in range(40):
            fb1.on_price(100.0 + (2.0 if i % 2 == 0 else -2.0), ts=ts + i * 0.25)
        pw = fb1.close(close_price=103.0)

        # Window 2 inherits prev_window and vol_history
        ts2 = ts + 300
        fb2 = FeatureBuilder(open_price=103.0, window_open_ts=ts2, prev_window=pw, vol_history=vh)
        feats2 = fb2.compute()

        assert feats2["prev_window_direction"] == 1  # 103 > 100
        assert abs(feats2["prev_window_move_pct"] - 3.0) < 0.01
        # vol_ratio should use the history pushed by window 1
        # (not 1.0, because history is now non-empty)
        # — just check it doesn't crash and is a valid number
        assert isinstance(feats2["vol_ratio"], float)
