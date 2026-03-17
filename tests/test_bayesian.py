"""Tests for Bayesian updater."""

from polybot.strategy.base_rate import BaseRateTable
from polybot.strategy.bayesian import BayesianUpdater


def test_bayesian_neutral_prior():
    table = BaseRateTable()  # Empty — returns 0.5
    b = BayesianUpdater(table)
    b.reset(100.0, prior=0.5)
    assert abs(b.probability - 0.5) < 0.01


def test_bayesian_update_shifts_probability():
    """With a base rate table that says up is likely, probability should increase."""
    table = BaseRateTable()
    # Manually insert a strong bin
    table.bins[(0.05, 10)] = type(
        "Bin", (), {"total": 100, "up_count": 80, "p_up": 0.8}
    )()

    b = BayesianUpdater(table)
    b.reset(100.0, prior=0.5)
    p = b.update(100.1, seconds_remaining=10)  # +0.1% move
    assert p > 0.5, f"Expected P(up) > 0.5 after positive evidence, got {p}"


def test_bayesian_reset():
    table = BaseRateTable()
    b = BayesianUpdater(table)
    b.reset(100.0, prior=0.7)
    assert b.probability > 0.6
    b.reset(100.0, prior=0.3)
    assert b.probability < 0.4
