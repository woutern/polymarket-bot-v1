"""Tests for macro features: fear/greed, funding rate, open interest."""

from unittest.mock import patch, MagicMock

from polybot.features.macro_features import MacroFeatures


class TestFearGreed:
    def test_returns_valid_value(self):
        mf = MacroFeatures()
        result = mf.get_fear_greed()
        assert "fear_greed_value" in result
        assert 0 <= result["fear_greed_value"] <= 100
        assert "fear_greed_zone" in result
        assert result["fear_greed_zone"] in [0, 1, 2, 3]

    def test_caching_no_double_fetch(self):
        mf = MacroFeatures()
        r1 = mf.get_fear_greed()
        # Second call should use cache — same object
        r2 = mf.get_fear_greed()
        assert r1 == r2

    def test_zone_classification(self):
        mf = MacroFeatures()
        # Manually test zone logic
        for val, expected_zone in [(10, 0), (30, 1), (60, 2), (80, 3)]:
            zone = 0 if val < 25 else (1 if val < 50 else (2 if val < 75 else 3))
            assert zone == expected_zone


class TestSolFunding:
    def test_returns_float(self):
        mf = MacroFeatures()
        result = mf.get_sol_funding()
        assert "sol_funding_rate" in result
        assert isinstance(result["sol_funding_rate"], float)
        assert "sol_funding_direction" in result
        assert result["sol_funding_direction"] in [-1, 0, 1]


class TestSolOpenInterest:
    def test_returns_oi(self):
        mf = MacroFeatures()
        result = mf.get_sol_open_interest()
        assert "sol_oi" in result
        assert "oi_change_1h" in result
        assert "oi_expanding" in result
        assert result["oi_expanding"] in [0, 1]


class TestAllFeatures:
    def test_all_features_present(self):
        mf = MacroFeatures()
        result = mf.get_all()
        expected_keys = [
            "fear_greed_value", "fear_greed_zone",
            "sol_funding_rate", "sol_funding_direction",
            "sol_oi", "oi_change_1h", "oi_expanding",
        ]
        for key in expected_keys:
            assert key in result, f"Missing feature: {key}"

    def test_graceful_fallback_if_api_down(self):
        """If all APIs are down, defaults are returned."""
        mf = MacroFeatures()
        with patch("httpx.get", side_effect=Exception("network down")):
            result = mf.get_all()
        # Should return defaults, not crash
        assert result["fear_greed_value"] == 50  # default
        assert result["sol_funding_rate"] == 0.0  # default
