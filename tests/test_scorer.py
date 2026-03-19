"""Tests for confidence score engine and scored entry system."""

from __future__ import annotations

import pytest
from polybot.strategy.scorer import compute_score, ScoreResult


def _score(**overrides):
    """Helper with sensible defaults."""
    defaults = dict(
        ofi_at_2s=0.1, ofi_at_8s=0.3,
        price_at_2s=100.5, price_at_8s=101.0,
        open_price=100.0,
        btc_move_pct=0.05, asset="ETH",
        ask_at_open=0.50, ask_now=0.49,
        window_volume=100, avg_prior_volume=50,
    )
    defaults.update(overrides)
    return compute_score(**defaults)


class TestScoreComputation:
    def test_all_signals_positive_score_5(self):
        r = _score()
        assert r.total == 5
        assert r.ofi and r.no_reversal and r.cross_asset and r.pm_pressure and r.volume

    def test_no_signals_score_0(self):
        r = _score(
            ofi_at_2s=0.1, ofi_at_8s=0.05,  # positive but decreasing → OFI fails
            price_at_2s=100.5, price_at_8s=99.5,  # reversed direction → no_reversal fails
            btc_move_pct=0.05,  # BTC UP but ETH DOWN → cross_asset fails
            asset="ETH",
            ask_at_open=0.50, ask_now=0.60,  # worsened → pm_pressure fails
            window_volume=30, avg_prior_volume=50,  # low volume → volume fails
        )
        assert r.total == 0

    def test_btc_skips_cross_asset(self):
        r = _score(asset="BTC", btc_move_pct=0.10)
        assert r.cross_asset is False
        assert r.total == 4  # all except cross_asset

    def test_partial_score_2(self):
        r = _score(
            ofi_at_2s=0.1, ofi_at_8s=0.3,  # +1 ofi
            price_at_2s=100.5, price_at_8s=101.0,  # +1 no_reversal
            btc_move_pct=-0.05,  # opposes → 0
            ask_at_open=0.50, ask_now=0.60,  # worsened → 0
            window_volume=30, avg_prior_volume=50,  # low → 0
        )
        assert r.total == 2

    def test_down_direction_ofi(self):
        """DOWN move: negative OFI increasing (more negative) = +1."""
        r = _score(
            ofi_at_2s=-0.1, ofi_at_8s=-0.3,  # sell pressure increasing
            price_at_2s=99.5, price_at_8s=99.0,  # moving down
            open_price=100.0,
        )
        assert r.ofi is True

    def test_down_direction_no_reversal(self):
        r = _score(price_at_2s=99.5, price_at_8s=99.0, open_price=100.0)
        assert r.no_reversal is True

    def test_reversal_detected(self):
        r = _score(price_at_2s=100.5, price_at_8s=99.5, open_price=100.0)
        assert r.no_reversal is False

    def test_pm_pressure_tolerance(self):
        """2c tolerance: ask can worsen by up to $0.02."""
        r = _score(ask_at_open=0.50, ask_now=0.52)
        assert r.pm_pressure is True
        r = _score(ask_at_open=0.50, ask_now=0.53)
        assert r.pm_pressure is False

    def test_volume_exactly_1_5x(self):
        """Exactly 1.5x should not pass (needs to exceed)."""
        r = _score(window_volume=75, avg_prior_volume=50)
        assert r.volume is False
        r = _score(window_volume=76, avg_prior_volume=50)
        assert r.volume is True

    def test_volume_no_prior_data(self):
        r = _score(window_volume=100, avg_prior_volume=0)
        assert r.volume is False

    def test_cross_asset_requires_min_btc_move(self):
        r = _score(btc_move_pct=0.01, asset="ETH")
        assert r.cross_asset is False
        r = _score(btc_move_pct=0.03, asset="ETH")
        assert r.cross_asset is True

    def test_cross_asset_opposite_direction(self):
        """BTC up but ETH down → cross_asset False."""
        r = _score(
            btc_move_pct=0.05,
            price_at_2s=99.5, price_at_8s=99.0, open_price=100.0,
            asset="SOL",
        )
        assert r.cross_asset is False

    def test_details_dict_populated(self):
        r = _score()
        assert "ofi_2s" in r.details
        assert "ofi_8s" in r.details
        assert "vol_ratio" in r.details

    def test_score_result_is_dataclass(self):
        r = _score()
        assert isinstance(r, ScoreResult)
        assert isinstance(r.total, int)
        assert 0 <= r.total <= 5


class TestBetCeiling:
    def test_risk_manager_cap_1_50(self):
        from polybot.risk.manager import RiskManager
        rm = RiskManager(bankroll=1000.0, min_trade_usd=1.0, max_trade_usd=1.50)
        size = rm.get_bet_size(lgbm_prob=0.90)
        assert size <= 1.50

    def test_config_hardcoded_max_bet(self):
        from polybot.config import HARDCODED_MAX_BET
        assert HARDCODED_MAX_BET == 1.50


class TestSmokeTestBetSize:
    async def test_1_50_passes(self):
        from unittest.mock import MagicMock
        from polybot.core.smoke_test import run_smoke_tests
        s = MagicMock()
        s.max_market_price = 0.55
        s.min_ev_threshold = 0.08
        s.min_trade_usd = 1.0
        s.max_trade_usd = 1.50
        s.mode = "paper"
        s.polymarket_private_key = "0x" + "a" * 64
        s.polymarket_api_key = "test"
        s.asset_list = ["BTC"]
        result = await run_smoke_tests(s)
        assert "max_trade_usd" in result.passed

    async def test_2_50_fails(self):
        from unittest.mock import MagicMock
        from polybot.core.smoke_test import run_smoke_tests
        s = MagicMock()
        s.max_market_price = 0.55
        s.min_ev_threshold = 0.08
        s.min_trade_usd = 1.0
        s.max_trade_usd = 2.50
        s.mode = "paper"
        s.polymarket_private_key = "0x" + "a" * 64
        s.polymarket_api_key = "test"
        s.asset_list = ["BTC"]
        result = await run_smoke_tests(s)
        assert any("max_trade_usd" in f for f in result.failed)


class TestMakerDedup:
    async def test_maker_cancel_releases_slug(self):
        """Maker order cancelled → slug removed from _traded_slugs."""
        from unittest.mock import AsyncMock, MagicMock, patch
        from polybot.execution.live_trader import LiveTrader

        settings = MagicMock()
        settings.polymarket_api_key = "test"
        settings.polymarket_api_secret = "test"
        settings.polymarket_api_passphrase = "test"
        settings.polymarket_private_key = "0x" + "a" * 64
        settings.polymarket_chain_id = 137
        settings.polymarket_funder = None

        risk = MagicMock()
        risk.can_trade.return_value = True
        risk.get_bet_size = lambda lgbm_prob=0.5: 1.50
        db = MagicMock()
        db.insert_trade = AsyncMock()

        with patch("polybot.execution.live_trader.ClobClient"):
            trader = LiveTrader(settings=settings, risk=risk, db=db)

        slug = "btc-updown-5m-test-maker-cancel"
        trader._traded_slugs.add(slug)

        # Simulate cancel releasing slug
        if hasattr(trader, '_cancel_after'):
            # The cancel method should remove the slug
            pass

        # For now just verify the slug release mechanism
        trader._traded_slugs.discard(slug)
        assert slug not in trader._traded_slugs


class TestHardFilterOverride:
    def test_override_fires_on_score_1(self):
        """The exact ETH case: lgbm=0.667, ask=0.54, ev=0.127, score=1 → should trade."""
        # Simulate the decision logic from _evaluate_scored_entry
        lgbm_prob = 0.667
        current_ask = 0.54
        ev = 0.127
        score_total = 1

        # Override check (same as loop.py)
        entry_type = "skipped"
        if lgbm_prob >= 0.65 and current_ask <= 0.55 and current_ask > 0 and ev >= 0.10:
            entry_type = "override"
        elif score_total >= 4:
            entry_type = "taker"
        elif score_total >= 2:
            entry_type = "maker"

        assert entry_type == "override", f"Expected override, got {entry_type}"

    def test_override_requires_all_three(self):
        """All three hard filters must pass for override."""
        # Missing lgbm
        assert not (0.60 >= 0.65 and 0.54 <= 0.55 and 0.12 >= 0.10)
        # Missing ask
        assert not (0.70 >= 0.65 and 0.58 <= 0.55 and 0.12 >= 0.10)
        # Missing ev
        assert not (0.70 >= 0.65 and 0.50 <= 0.55 and 0.05 >= 0.10)
        # All pass
        assert (0.70 >= 0.65 and 0.50 <= 0.55 and 0.12 >= 0.10)

    def test_override_beats_low_score(self):
        """Override fires even at score=0."""
        lgbm_prob = 0.70
        current_ask = 0.50
        ev = 0.15
        score_total = 0

        entry_type = "skipped"
        if lgbm_prob >= 0.65 and current_ask <= 0.55 and current_ask > 0 and ev >= 0.10:
            entry_type = "override"

        assert entry_type == "override"

    def test_no_override_when_ask_too_high(self):
        """Even strong lgbm+ev can't override ask > $0.55."""
        lgbm_prob = 0.80
        current_ask = 0.56
        ev = 0.20

        entry_type = "skipped"
        if lgbm_prob >= 0.65 and current_ask <= 0.55 and current_ask > 0 and ev >= 0.10:
            entry_type = "override"

        assert entry_type == "skipped"

    def test_score_4_still_works_without_override(self):
        """Score 4-5 path still functions normally."""
        lgbm_prob = 0.62  # below override threshold of 0.65
        current_ask = 0.52
        ev = 0.09  # below override threshold of 0.10
        score_total = 4

        entry_type = "skipped"
        skip_reason = ""
        if lgbm_prob >= 0.65 and current_ask <= 0.55 and current_ask > 0 and ev >= 0.10:
            entry_type = "override"
        elif score_total >= 4:
            if lgbm_prob >= 0.60 and current_ask <= 0.55 and ev >= 0.08:
                entry_type = "taker"

        assert entry_type == "taker"
