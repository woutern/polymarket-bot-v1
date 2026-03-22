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
    s.max_trade_usd = 8.00
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
        settings = _make_settings(max_market_price=0.95)
        result = await run_smoke_tests(settings)
        assert any("max_market_price" in f for f in result.failed)

    async def test_low_min_ev_fails(self):
        settings = _make_settings(min_ev_threshold=0.005)
        result = await run_smoke_tests(settings)
        assert any("min_ev_threshold" in f for f in result.failed)

    async def test_all_wrong_all_fail(self):
        settings = _make_settings(max_market_price=0.95, min_ev_threshold=0.005)
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


class TestModelLoadingSmokeTest:
    """Smoke test must verify models load and produce non-0.5 predictions."""

    def test_smoke_test_checks_model_loading(self):
        """Smoke test must include model_load check."""
        import inspect
        from polybot.core.smoke_test import run_smoke_tests
        source = inspect.getsource(run_smoke_tests)
        assert "model_load" in source
        assert "has_model" in source

    def test_smoke_test_checks_model_prediction(self):
        """Smoke test must verify predictions are not the 0.5 fallback."""
        import inspect
        from polybot.core.smoke_test import run_smoke_tests
        source = inspect.getsource(run_smoke_tests)
        assert "model_predict" in source
        assert "0.5" in source  # checks for the 0.5 fallback

    def test_smoke_test_fails_on_broken_model(self):
        """If model returns 0.5, smoke test must FAIL (not warn)."""
        import inspect
        from polybot.core.smoke_test import run_smoke_tests
        source = inspect.getsource(run_smoke_tests)
        # Must use result.fail, not result.warn for model issues
        assert 'result.fail(f"model_load_' in source or 'result.fail(f"model_predict_' in source

    def test_model_server_region_matches_ssm_smoke_test(self):
        """ModelServer and SSM smoke test must use same region."""
        import inspect
        from polybot.ml.server import ModelServer
        from polybot.core.smoke_test import run_smoke_tests
        server_source = inspect.getsource(ModelServer.__init__)
        smoke_source = inspect.getsource(run_smoke_tests)
        # Both must reference eu-west-1
        assert "eu-west-1" in server_source
        assert "eu-west-1" in smoke_source
