"""Tests for the directional signal generator."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from polybot.models import Direction, OrderbookSnapshot, SignalSource
from polybot.strategy.bayesian import BayesianUpdater
from polybot.strategy.base_rate import BaseRateBin, BaseRateTable
from polybot.strategy.directional import generate_directional_signal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bayesian(p_up: float = 0.75) -> BayesianUpdater:
    """Return a BayesianUpdater pre-reset so .probability == p_up (approx)."""
    table = BaseRateTable()
    b = BayesianUpdater(table)
    # Set log_odds directly for deterministic probability
    import math
    b.log_odds = math.log(p_up / (1 - p_up))
    b._initialized = True
    b._open_price = 100.0
    return b


def _make_orderbook(
    yes_ask: float = 0.50,
    no_ask: float = 0.50,
    yes_bid: float | None = None,
    no_bid: float | None = None,
) -> OrderbookSnapshot:
    # Default bids to ask - 0.02 so the spread is tight (0.02 < OBI threshold 0.15)
    # and existing signal tests are not inadvertently vetoed.
    return OrderbookSnapshot(
        yes_best_ask=yes_ask,
        yes_best_bid=yes_bid if yes_bid is not None else yes_ask - 0.02,
        no_best_ask=no_ask,
        no_best_bid=no_bid if no_bid is not None else no_ask - 0.02,
    )


# ---------------------------------------------------------------------------
# Guard conditions — should return None
# ---------------------------------------------------------------------------

class TestGuardConditions:
    def test_open_price_zero_returns_none(self):
        b = _make_bayesian()
        ob = _make_orderbook()
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.5,
            open_price=0.0,
            seconds_remaining=30,
        )
        assert result is None

    def test_open_price_negative_returns_none(self):
        b = _make_bayesian()
        ob = _make_orderbook()
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.5,
            open_price=-100.0,
            seconds_remaining=30,
        )
        assert result is None

    def test_move_below_min_threshold_returns_none(self):
        """A 0.01% move is below the default 0.08% threshold."""
        b = _make_bayesian()
        ob = _make_orderbook()
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.01,
            open_price=100.0,
            seconds_remaining=30,
            min_move_pct=0.08,
        )
        assert result is None

    def test_move_exactly_at_threshold_does_not_fire(self):
        """abs(pct_move) < min_move_pct — equal is NOT enough."""
        b = _make_bayesian(0.9)
        ob = _make_orderbook(yes_ask=0.40)
        # 0.08% move exactly equals min_move_pct=0.08 → abs < 0.08 is False BUT
        # the guard is < so exactly equal → signal CAN fire if other conditions met.
        # We test that just below does NOT fire.
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.079,  # 0.079% move
            open_price=100.0,
            seconds_remaining=30,
            min_move_pct=0.08,
        )
        assert result is None

    def test_market_already_priced_in_returns_none(self):
        """Ask > max_market_price means market already priced in the move."""
        b = _make_bayesian(0.9)
        ob = _make_orderbook(yes_ask=0.80)  # Above default max_market_price=0.75
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.5,
            open_price=100.0,
            seconds_remaining=30,
            min_move_pct=0.08,
            max_market_price=0.75,
        )
        assert result is None

    def test_market_price_below_floor_returns_none(self):
        """market_price < 0.20 triggers the floor guard (uninitialized orderbook)."""
        b = _make_bayesian(0.9)
        ob = _make_orderbook(yes_ask=0.10)
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.5,
            open_price=100.0,
            seconds_remaining=30,
            min_move_pct=0.08,
            max_market_price=0.75,
        )
        assert result is None

    def test_market_price_one_returns_none(self):
        """market_price=1 triggers the guard (market_price >= 1)."""
        b = _make_bayesian(0.9)
        ob = _make_orderbook(yes_ask=1.0)
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.5,
            open_price=100.0,
            seconds_remaining=30,
            min_move_pct=0.08,
            max_market_price=0.75,
        )
        assert result is None

    def test_ev_below_threshold_returns_none(self):
        """EV too low: model_prob ≈ market_price → ev ≈ 0."""
        b = _make_bayesian(0.55)  # model_prob ~ 0.55
        ob = _make_orderbook(yes_ask=0.54)  # ev = (0.55-0.54)/0.54 ≈ 0.019 < 0.06
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.5,
            open_price=100.0,
            seconds_remaining=30,
            min_move_pct=0.08,
            min_ev_threshold=0.06,
            max_market_price=0.75,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Signal fires — UP direction
# ---------------------------------------------------------------------------

class TestSignalFiresUp:
    def test_basic_up_signal(self):
        """All conditions met for an UP signal."""
        b = _make_bayesian(0.80)  # model_prob=0.80
        ob = _make_orderbook(yes_ask=0.50)  # ev = (0.80-0.50)/0.50 = 0.60 > 0.06
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.5,
            open_price=100.0,
            seconds_remaining=30,
            min_move_pct=0.08,
            min_ev_threshold=0.06,
            max_market_price=0.75,
        )
        assert result is not None
        assert result.direction == Direction.UP
        assert result.source == SignalSource.DIRECTIONAL

    def test_up_signal_ev_calculated_correctly(self):
        b = _make_bayesian(0.80)
        ob = _make_orderbook(yes_ask=0.50)
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.5,
            open_price=100.0,
            seconds_remaining=30,
        )
        # ev = (model_prob - market_price) / market_price
        expected_ev = (result.model_prob - 0.50) / 0.50
        assert abs(result.ev - expected_ev) < 1e-9

    def test_up_signal_model_prob_matches_bayesian(self):
        """model_prob in signal equals bayesian.probability for UP."""
        b = _make_bayesian(0.80)
        ob = _make_orderbook(yes_ask=0.50)
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.5,
            open_price=100.0,
            seconds_remaining=30,
        )
        assert result is not None
        assert abs(result.model_prob - b.probability) < 1e-9

    def test_up_signal_uses_yes_best_ask(self):
        b = _make_bayesian(0.80)
        ob = _make_orderbook(yes_ask=0.45, no_ask=0.99)
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.5,
            open_price=100.0,
            seconds_remaining=30,
        )
        assert result is not None
        assert result.market_price == 0.45

    def test_up_signal_slug_and_asset_passed_through(self):
        b = _make_bayesian(0.80)
        ob = _make_orderbook(yes_ask=0.50)
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.5,
            open_price=100.0,
            seconds_remaining=30,
            window_slug="btc-updown-5m-123",
            asset="BTC",
        )
        assert result is not None
        assert result.window_slug == "btc-updown-5m-123"
        assert result.asset == "BTC"


# ---------------------------------------------------------------------------
# Signal fires — DOWN direction
# ---------------------------------------------------------------------------

class TestSignalFiresDown:
    def test_basic_down_signal(self):
        """All conditions met for a DOWN signal."""
        # For DOWN: direction = DOWN, model_prob = 1 - bayesian.probability
        # bayesian.probability=0.20 → model_prob(DOWN) = 0.80
        b = _make_bayesian(0.20)
        ob = _make_orderbook(no_ask=0.50)  # ev = (0.80-0.50)/0.50 = 0.60 > 0.06
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=99.5,  # price fell → negative pct_move → DOWN
            open_price=100.0,
            seconds_remaining=30,
            min_move_pct=0.08,
            min_ev_threshold=0.06,
            max_market_price=0.75,
        )
        assert result is not None
        assert result.direction == Direction.DOWN

    def test_down_signal_uses_no_best_ask(self):
        b = _make_bayesian(0.20)
        ob = _make_orderbook(yes_ask=0.99, no_ask=0.45)
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=99.5,
            open_price=100.0,
            seconds_remaining=30,
        )
        assert result is not None
        assert result.market_price == 0.45

    def test_down_signal_model_prob_is_one_minus_p_up(self):
        b = _make_bayesian(0.20)
        ob = _make_orderbook(no_ask=0.50)
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=99.5,
            open_price=100.0,
            seconds_remaining=30,
        )
        assert result is not None
        assert abs(result.model_prob - (1.0 - b.probability)) < 1e-9

    def test_down_market_priced_in_returns_none(self):
        """no_best_ask > max_market_price → market already priced in downside."""
        b = _make_bayesian(0.20)
        ob = _make_orderbook(no_ask=0.80)
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=99.5,
            open_price=100.0,
            seconds_remaining=30,
            max_market_price=0.75,
        )
        assert result is None


# ---------------------------------------------------------------------------
# Custom thresholds
# ---------------------------------------------------------------------------

class TestCustomThresholds:
    def test_custom_min_move_pct(self):
        """Signal fires with a lower min_move_pct."""
        b = _make_bayesian(0.80)
        ob = _make_orderbook(yes_ask=0.50)
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.05,  # 0.05% move
            open_price=100.0,
            seconds_remaining=30,
            min_move_pct=0.04,  # lower threshold
        )
        assert result is not None

    def test_custom_max_market_price(self):
        """Stricter max_market_price suppresses signal."""
        b = _make_bayesian(0.80)
        ob = _make_orderbook(yes_ask=0.60)
        # Default max=0.75 would allow 0.60, but strict max=0.55 blocks it
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.5,
            open_price=100.0,
            seconds_remaining=30,
            max_market_price=0.55,
        )
        assert result is None

    def test_custom_min_ev_threshold(self):
        """Higher ev threshold suppresses a marginal signal."""
        b = _make_bayesian(0.62)  # model_prob ~ 0.62
        ob = _make_orderbook(yes_ask=0.55)  # ev = (0.62-0.55)/0.55 ≈ 0.127
        # With high threshold=0.20, this should be suppressed
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.5,
            open_price=100.0,
            seconds_remaining=30,
            min_ev_threshold=0.20,
        )
        assert result is None


# ---------------------------------------------------------------------------
# OBI proxy spread veto
# ---------------------------------------------------------------------------

class TestOBISpreadVeto:
    """Wide bid-ask spread on the directional side triggers veto (proxy OBI)."""

    def test_up_wide_yes_spread_vetoed(self):
        """UP signal vetoed when yes_best_ask - yes_best_bid > 0.15."""
        b = _make_bayesian(0.80)
        ob = _make_orderbook(yes_ask=0.50, yes_bid=0.30)  # spread = 0.20 > 0.15
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.5,
            open_price=100.0,
            seconds_remaining=30,
            use_ai=False,
        )
        assert result is None

    def test_up_spread_just_below_threshold_not_vetoed(self):
        """Spread of 0.14 (< 0.15 threshold) is not vetoed."""
        b = _make_bayesian(0.80)
        ob = _make_orderbook(yes_ask=0.50, yes_bid=0.36)  # spread = 0.14 < 0.15
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.5,
            open_price=100.0,
            seconds_remaining=30,
            use_ai=False,
        )
        assert result is not None
        assert result.direction == Direction.UP

    def test_up_tight_yes_spread_passes(self):
        """UP signal passes when yes spread is tight (< 0.15)."""
        b = _make_bayesian(0.80)
        ob = _make_orderbook(yes_ask=0.50, yes_bid=0.48)  # spread = 0.02
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=100.5,
            open_price=100.0,
            seconds_remaining=30,
            use_ai=False,
        )
        assert result is not None
        assert result.direction == Direction.UP

    def test_down_wide_no_spread_vetoed(self):
        """DOWN signal vetoed when no_best_ask - no_best_bid > 0.15."""
        b = _make_bayesian(0.20)
        ob = _make_orderbook(no_ask=0.50, no_bid=0.30)  # spread = 0.20 > 0.15
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=99.5,
            open_price=100.0,
            seconds_remaining=30,
            use_ai=False,
        )
        assert result is None

    def test_down_tight_no_spread_passes(self):
        """DOWN signal passes when no spread is tight (< 0.15)."""
        b = _make_bayesian(0.20)
        ob = _make_orderbook(no_ask=0.50, no_bid=0.48)  # spread = 0.02
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=99.5,
            open_price=100.0,
            seconds_remaining=30,
            use_ai=False,
        )
        assert result is not None
        assert result.direction == Direction.DOWN

    def test_up_wide_yes_spread_does_not_veto_down_direction(self):
        """A wide YES spread should NOT veto a DOWN signal (different side)."""
        b = _make_bayesian(0.20)
        # Wide YES spread but tight NO spread
        ob = _make_orderbook(yes_ask=0.50, yes_bid=0.20, no_ask=0.50, no_bid=0.48)
        result = generate_directional_signal(
            bayesian=b,
            orderbook=ob,
            current_price=99.5,
            open_price=100.0,
            seconds_remaining=30,
            use_ai=False,
        )
        assert result is not None
        assert result.direction == Direction.DOWN
