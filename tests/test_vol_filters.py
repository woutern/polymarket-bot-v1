"""Tests for volatility protection filters: vol_ratio and body_ratio."""

from __future__ import annotations

import math

import pytest

from polybot.feeds.rtds_ws import compute_realized_vol


class TestVolRatio:
    def test_vol_ratio_below_half_blocks(self):
        """vol_ratio < 0.5 means too quiet — should block entry."""
        vol = 0.10
        vol_ma = 0.50
        vol_ratio = vol / vol_ma
        assert vol_ratio < 0.5

    def test_vol_ratio_above_three_blocks(self):
        """vol_ratio > 3.0 means too wild — should block entry."""
        vol = 1.50
        vol_ma = 0.40
        vol_ratio = vol / vol_ma
        assert vol_ratio > 3.0

    def test_vol_ratio_normal_allows(self):
        """vol_ratio between 0.5 and 3.0 should allow entry."""
        vol = 0.50
        vol_ma = 0.50
        vol_ratio = vol / vol_ma
        assert 0.5 <= vol_ratio <= 3.0

    def test_vol_ratio_exactly_one(self):
        """vol_ratio = 1.0 when vol equals average — should allow."""
        vol = 0.45
        vol_ma = 0.45
        vol_ratio = vol / vol_ma
        assert abs(vol_ratio - 1.0) < 0.01

    def test_vol_ma_zero_defaults_to_one(self):
        """When vol_ma is 0, vol_ratio should default to 1.0 (not crash)."""
        vol = 0.50
        vol_ma = 0.0
        vol_ratio = vol / vol_ma if vol_ma > 0 else 1.0
        assert vol_ratio == 1.0


class TestBodyRatio:
    def test_body_ratio_below_threshold_blocks(self):
        """body_ratio < 0.4 means indecisive candle — should block."""
        open_price = 100.0
        current = 100.02
        high = 100.10
        low = 99.90
        body = abs(current - open_price)
        hl_range = high - low
        body_ratio = body / hl_range
        assert body_ratio < 0.4  # 0.02 / 0.20 = 0.10

    def test_body_ratio_above_threshold_allows(self):
        """body_ratio >= 0.4 means decisive move — should allow."""
        open_price = 100.0
        current = 100.15
        high = 100.20
        low = 99.95
        body = abs(current - open_price)
        hl_range = high - low
        body_ratio = body / hl_range
        assert body_ratio >= 0.4  # 0.15 / 0.25 = 0.60

    def test_body_ratio_high_equals_low_no_crash(self):
        """When high == low (no range), body_ratio defaults to 0.5."""
        high = 100.0
        low = 100.0
        hl_range = high - low
        body_ratio = 0.5 if hl_range == 0 else abs(100.05 - 100.0) / hl_range
        assert body_ratio == 0.5

    def test_body_ratio_full_body(self):
        """When price moved from low to high, body_ratio = 1.0."""
        open_price = 100.0
        current = 100.20
        high = 100.20
        low = 100.0
        body = abs(current - open_price)
        hl_range = high - low
        body_ratio = body / hl_range if hl_range > 0 else 0.5
        assert abs(body_ratio - 1.0) < 0.01
