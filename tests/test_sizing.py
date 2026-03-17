"""Tests for Kelly sizing."""

from polybot.strategy.sizing import compute_size, kelly_fraction


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


def test_compute_size_basic():
    # Model says 70% chance, market price 0.55
    # b = 0.45/0.55 = 0.818
    # Kelly = (0.7*0.818 - 0.3)/0.818 = 0.333
    # Quarter: 0.333 * 0.25 = 0.083
    # Capped at 1% of 1000 = 10
    size = compute_size(0.7, 0.55, bankroll=1000, kelly_mult=0.25, max_position_pct=0.01)
    assert 0 < size <= 10


def test_compute_size_zero_on_no_edge():
    size = compute_size(0.5, 0.55, bankroll=1000)
    assert size == 0.0


def test_compute_size_respects_cap():
    # Even with huge edge, should cap at max_position_pct
    size = compute_size(0.99, 0.10, bankroll=10000, max_position_pct=0.01)
    assert size <= 100.0
