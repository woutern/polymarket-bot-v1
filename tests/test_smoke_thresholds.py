"""Tests for smoke test threshold guards."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from polybot.core.smoke_test import run_smoke_tests


def _make_settings(**overrides):
    s = MagicMock()
    s.max_market_price = 0.55
    s.min_ev_threshold = 0.08
    s.min_trade_usd = 1.0
    s.max_trade_usd = 5.0
    s.mode = "paper"
    s.polymarket_private_key = "0x" + "a" * 64
    s.polymarket_api_key = "test"
    s.asset_list = ["BTC"]
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


class TestThresholdGuards:
    async def test_correct_thresholds_pass(self):
        settings = _make_settings()
        result = await run_smoke_tests(settings)
        assert "max_market_price" in result.passed
        assert "min_ev_threshold" in result.passed
        assert "min_lgbm_prob" in result.passed

    async def test_high_max_market_price_fails(self):
        settings = _make_settings(max_market_price=0.60)
        result = await run_smoke_tests(settings)
        assert any("max_market_price" in f for f in result.failed)

    async def test_low_min_ev_fails(self):
        settings = _make_settings(min_ev_threshold=0.05)
        result = await run_smoke_tests(settings)
        assert any("min_ev_threshold" in f for f in result.failed)

    async def test_all_wrong_all_fail(self):
        settings = _make_settings(max_market_price=0.70, min_ev_threshold=0.03)
        result = await run_smoke_tests(settings)
        assert any("max_market_price" in f for f in result.failed)
        assert any("min_ev_threshold" in f for f in result.failed)

    async def test_boundary_values_pass(self):
        """Exactly 0.55, 0.08, and 0.60 should pass."""
        settings = _make_settings(max_market_price=0.55, min_ev_threshold=0.08)
        result = await run_smoke_tests(settings)
        assert "max_market_price" in result.passed
        assert "min_ev_threshold" in result.passed
        assert "min_lgbm_prob" in result.passed
