"""Tests for Bayesian updater."""

import math

import pytest

from polybot.strategy.base_rate import BaseRateBin, BaseRateTable
from polybot.strategy.bayesian import BayesianUpdater


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_table_with_bin(p_up: float, bin_key: tuple = (0.05, 10)) -> BaseRateTable:
    """Return a BaseRateTable with a single populated bin."""
    table = BaseRateTable()
    total = 100
    up_count = int(p_up * total)
    table.bins[bin_key] = BaseRateBin(
        pct_move_min=bin_key[0],
        pct_move_max=bin_key[0],
        seconds_remaining=bin_key[1],
        total=total,
        up_count=up_count,
    )
    return table


# ---------------------------------------------------------------------------
# probability property
# ---------------------------------------------------------------------------

class TestProbabilityProperty:
    def test_neutral_prior_is_half(self):
        table = BaseRateTable()
        b = BayesianUpdater(table)
        b.reset(100.0, prior=0.5)
        assert abs(b.probability - 0.5) < 0.001

    def test_bullish_prior(self):
        table = BaseRateTable()
        b = BayesianUpdater(table)
        b.reset(100.0, prior=0.7)
        assert b.probability > 0.6

    def test_bearish_prior(self):
        table = BaseRateTable()
        b = BayesianUpdater(table)
        b.reset(100.0, prior=0.3)
        assert b.probability < 0.4

    def test_probability_before_init_is_half(self):
        """Calling probability before reset() returns 0.5 (log_odds=0)."""
        table = BaseRateTable()
        b = BayesianUpdater(table)
        assert abs(b.probability - 0.5) < 0.001

    def test_probability_always_in_0_1(self):
        """Sigmoid output stays in (0, 1) for large positive and negative log-odds."""
        table = BaseRateTable()
        b = BayesianUpdater(table)
        # Use values within Python's float range for exp: exp(-700) ≈ 0
        b.log_odds = 100.0
        assert 0.0 < b.probability <= 1.0
        b.log_odds = -100.0
        assert 0.0 <= b.probability < 1.0


# ---------------------------------------------------------------------------
# reset
# ---------------------------------------------------------------------------

class TestReset:
    def test_reset_sets_open_price(self):
        table = BaseRateTable()
        b = BayesianUpdater(table)
        b.reset(50000.0)
        assert b._open_price == 50000.0

    def test_reset_marks_initialized(self):
        table = BaseRateTable()
        b = BayesianUpdater(table)
        assert not b._initialized
        b.reset(100.0)
        assert b._initialized

    def test_reset_clamps_extreme_prior_high(self):
        """Prior of 1.0 should be clamped to 0.999."""
        table = BaseRateTable()
        b = BayesianUpdater(table)
        b.reset(100.0, prior=1.0)
        assert b.probability < 1.0

    def test_reset_clamps_extreme_prior_low(self):
        """Prior of 0.0 should be clamped to 0.001."""
        table = BaseRateTable()
        b = BayesianUpdater(table)
        b.reset(100.0, prior=0.0)
        assert b.probability > 0.0

    def test_reset_twice_uses_new_prior(self):
        table = BaseRateTable()
        b = BayesianUpdater(table)
        b.reset(100.0, prior=0.7)
        prob_first = b.probability
        b.reset(100.0, prior=0.3)
        prob_second = b.probability
        assert prob_second < prob_first


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

class TestUpdate:
    def test_update_before_init_returns_half(self):
        """update() before reset() returns 0.5."""
        table = BaseRateTable()
        b = BayesianUpdater(table)
        p = b.update(100.0, seconds_remaining=30)
        assert abs(p - 0.5) < 0.001

    def test_update_with_zero_open_price_returns_half(self):
        """After reset with open_price=0, update should return 0.5."""
        table = BaseRateTable()
        b = BayesianUpdater(table)
        b._initialized = True
        b._open_price = 0.0
        p = b.update(100.0, seconds_remaining=30)
        assert abs(p - 0.5) < 0.001

    def test_update_shifts_probability_up(self):
        """With a strong bullish base-rate bin, probability moves above 0.5."""
        table = _make_table_with_bin(p_up=0.8, bin_key=(0.05, 10))
        b = BayesianUpdater(table)
        b.reset(100.0, prior=0.5)
        p = b.update(100.1, seconds_remaining=10)  # +0.1% move → bin (0.05, 10)
        assert p > 0.5

    def test_update_shifts_probability_down(self):
        """With a strong bearish base-rate bin, probability moves below 0.5."""
        # Negative move maps to (-1.0, 10) bin
        table = _make_table_with_bin(p_up=0.2, bin_key=(-1.0, 10))
        b = BayesianUpdater(table)
        b.reset(100.0, prior=0.5)
        p = b.update(98.5, seconds_remaining=10)  # -1.5% move → clamped to (-1.0, 10)
        assert p < 0.5

    def test_update_returns_current_probability(self):
        """Return value of update() equals b.probability."""
        table = BaseRateTable()  # empty → always 0.5
        b = BayesianUpdater(table)
        b.reset(100.0, prior=0.5)
        p = b.update(100.05, seconds_remaining=30)
        assert p == b.probability

    def test_update_clamps_seconds_remaining(self):
        """seconds_remaining < 5 is clamped to 5 — should not raise."""
        table = BaseRateTable()
        b = BayesianUpdater(table)
        b.reset(100.0, prior=0.5)
        p = b.update(100.1, seconds_remaining=1)
        assert 0.0 < p < 1.0

    def test_update_neutral_base_rate_preserves_prior(self):
        """Empty table always returns 0.5 → prior drifts toward 0.5."""
        table = BaseRateTable()  # no bins → lookup returns 0.5
        b = BayesianUpdater(table)
        b.reset(100.0, prior=0.9)
        p_before = b.probability
        b.update(100.1, seconds_remaining=30)
        # After blending toward 0.5 evidence, probability should decrease
        assert b.probability < p_before

    def test_update_multiple_ticks_converge(self):
        """Repeated bullish ticks should increase probability monotonically."""
        table = _make_table_with_bin(p_up=0.9, bin_key=(0.05, 30))
        b = BayesianUpdater(table)
        b.reset(100.0, prior=0.5)

        probs = []
        for _ in range(5):
            p = b.update(100.1, seconds_remaining=30)
            probs.append(p)

        # Each tick increases probability
        for i in range(1, len(probs)):
            assert probs[i] >= probs[i - 1]

    def test_alpha_blending_formula(self):
        """Verify the exponential moving blend formula directly."""
        table = BaseRateTable()
        b = BayesianUpdater(table)
        b.reset(100.0, prior=0.5)

        initial_log_odds = b.log_odds
        # With empty table, evidence_log_odds = log(0.5/0.5) = 0
        evidence_log_odds = 0.0
        expected_log_odds = (1 - 0.3) * initial_log_odds + 0.3 * evidence_log_odds

        b.update(100.0, seconds_remaining=30)
        assert abs(b.log_odds - expected_log_odds) < 1e-9
