"""Tests for Kelly sizing."""

import pytest

from polybot.strategy.sizing import compute_size, kelly_fraction


# ---------------------------------------------------------------------------
# kelly_fraction
# ---------------------------------------------------------------------------

def test_kelly_positive_edge():
    # 60% chance of winning, even odds (b=1)
    f = kelly_fraction(0.6, 1.0, kelly_mult=1.0)
    assert abs(f - 0.2) < 0.01  # Full Kelly: (0.6*1 - 0.4)/1 = 0.2


def test_kelly_no_edge():
    f = kelly_fraction(0.5, 1.0, kelly_mult=1.0)
    assert f == 0.0


def test_kelly_negative_edge():
    f = kelly_fraction(0.4, 1.0, kelly_mult=1.0)
    assert f == 0.0


def test_quarter_kelly():
    f = kelly_fraction(0.6, 1.0, kelly_mult=0.25)
    assert abs(f - 0.05) < 0.01  # 0.2 * 0.25 = 0.05


def test_kelly_p_zero():
    """p=0: no edge, must return 0."""
    assert kelly_fraction(0.0, 1.0) == 0.0


def test_kelly_p_one():
    """p=1: would be infinite Kelly, but guard is p >= 1 → return 0."""
    assert kelly_fraction(1.0, 1.0) == 0.0


def test_kelly_b_zero():
    """b=0: no payout, must return 0 without division error."""
    assert kelly_fraction(0.9, 0.0) == 0.0


def test_kelly_b_negative():
    """Negative odds are nonsensical; guard is b <= 0."""
    assert kelly_fraction(0.9, -1.0) == 0.0


def test_kelly_returns_non_negative():
    """Result is always >= 0 even for marginal negative edge."""
    f = kelly_fraction(0.45, 1.0, kelly_mult=0.25)
    assert f >= 0.0


def test_kelly_mult_zero():
    """kelly_mult=0 collapses any fraction to 0."""
    f = kelly_fraction(0.9, 2.0, kelly_mult=0.0)
    assert f == 0.0


# ---------------------------------------------------------------------------
# compute_size
# ---------------------------------------------------------------------------

def test_compute_size_basic():
    # Model says 70% chance, market price 0.55
    size = compute_size(0.7, 0.55, bankroll=1000, kelly_mult=0.25, max_position_pct=0.01)
    assert 0 < size <= 10


def test_compute_size_zero_on_no_edge():
    size = compute_size(0.5, 0.55, bankroll=1000)
    assert size == 0.0


def test_compute_size_respects_cap():
    # Even with huge edge, should cap at max_position_pct
    size = compute_size(0.99, 0.10, bankroll=10000, max_position_pct=0.01)
    assert size <= 100.0


def test_compute_size_market_price_zero():
    """market_price=0 must return 0 (no division)."""
    size = compute_size(0.8, 0.0, bankroll=1000)
    assert size == 0.0


def test_compute_size_market_price_one():
    """market_price=1 means zero payout; guard is market_price >= 1."""
    size = compute_size(0.8, 1.0, bankroll=1000)
    assert size == 0.0


def test_compute_size_market_price_above_one():
    """market_price > 1 is invalid; must return 0."""
    size = compute_size(0.8, 1.5, bankroll=1000)
    assert size == 0.0


def test_compute_size_zero_bankroll():
    """Bankroll=0: Kelly fraction is fine but resulting size is 0."""
    size = compute_size(0.8, 0.5, bankroll=0.0)
    assert size == 0.0


def test_compute_size_negative_bankroll():
    """Negative bankroll: Kelly is non-negative but f*bankroll is negative → 0."""
    size = compute_size(0.8, 0.5, bankroll=-500.0)
    # f * negative bankroll = negative size, which is < 1.0 threshold
    assert size == 0.0


def test_compute_size_below_minimum_returns_zero():
    """Very tiny bankroll means size < $1 → returns 0."""
    size = compute_size(0.7, 0.55, bankroll=5.0)
    # 1% of 5 = 0.05, but capped at $10; 0.05 < 1.0 threshold
    assert size == 0.0


def test_compute_size_hard_cap_at_ten():
    """Size is always capped at $10."""
    size = compute_size(0.99, 0.01, bankroll=100_000, max_position_pct=1.0)
    assert size <= 10.0


def test_compute_size_model_prob_p_zero():
    """model_prob=0: kelly returns 0 → size 0."""
    size = compute_size(0.0, 0.5, bankroll=1000)
    assert size == 0.0


def test_compute_size_model_prob_p_one():
    """model_prob=1: kelly guard triggers → size 0."""
    size = compute_size(1.0, 0.5, bankroll=1000)
    assert size == 0.0


def test_compute_size_result_is_rounded():
    """Result should be rounded to 2 decimal places."""
    size = compute_size(0.7, 0.55, bankroll=1000)
    assert size == round(size, 2)
