"""Tests for window tracker."""

import time
from unittest.mock import patch

from polybot.market.window_tracker import WindowState, WindowTracker
from polybot.models import Window


def test_slug_generation():
    # Timestamp divisible by 300
    assert Window.slug_for_ts(1710000000) == "btc-updown-5m-1710000000"
    # Not divisible — should align down
    assert Window.slug_for_ts(1710000123) == "btc-updown-5m-1710000000"


def test_window_tracker_open():
    tracker = WindowTracker(entry_seconds=10)
    state = tracker.tick(50000.0)
    assert state == WindowState.OPEN
    assert tracker.current is not None
    assert tracker.current.open_price == 50000.0


def test_window_tracker_pct_move():
    tracker = WindowTracker()
    tracker.tick(50000.0)
    pct = tracker.pct_move(50100.0)
    assert pct is not None
    assert abs(pct - 0.2) < 0.01  # 100/50000 * 100 = 0.2%


def test_window_direction_flat_is_up():
    """Flat/equal resolves as UP."""
    w = Window(open_ts=0, close_ts=300, open_price=100.0, close_price=100.0)
    assert w.resolved_direction.value == "up"


def test_window_direction_down():
    w = Window(open_ts=0, close_ts=300, open_price=100.0, close_price=99.9)
    assert w.resolved_direction.value == "down"


# ---------------------------------------------------------------------------
# 15-minute window tests
# ---------------------------------------------------------------------------


def test_slug_generation_15m():
    # Timestamp divisible by 900
    assert Window.slug_for_ts(1710000000, "BTC", 900) == "btc-updown-15m-1710000000"
    # Not divisible — should align down to nearest 900
    assert Window.slug_for_ts(1710000123, "BTC", 900) == "btc-updown-15m-1710000000"
    # 1710000899 still in same window
    assert Window.slug_for_ts(1710000899, "BTC", 900) == "btc-updown-15m-1710000000"
    # 1710000900 starts next window
    assert Window.slug_for_ts(1710000900, "BTC", 900) == "btc-updown-15m-1710000900"


def test_slug_generation_15m_eth():
    assert Window.slug_for_ts(1710000000, "ETH", 900) == "eth-updown-15m-1710000000"


def test_window_tracker_15m_open():
    tracker = WindowTracker(entry_seconds=30, asset="BTC", window_seconds=900)
    state = tracker.tick(50000.0)
    assert state == WindowState.OPEN
    assert tracker.current is not None
    assert tracker.current.open_price == 50000.0
    # Verify slug contains 15m
    assert "15m" in tracker.current.slug


def test_window_tracker_15m_close_ts():
    tracker = WindowTracker(entry_seconds=30, asset="BTC", window_seconds=900)
    tracker.tick(50000.0)
    assert tracker.current is not None
    assert tracker.current.close_ts - tracker.current.open_ts == 900


def test_window_tracker_5m_unchanged():
    """Existing 5-min tracker behaviour is unchanged."""
    tracker = WindowTracker(entry_seconds=10, asset="BTC", window_seconds=300)
    state = tracker.tick(50000.0)
    assert state == WindowState.OPEN
    assert tracker.current is not None
    assert tracker.current.close_ts - tracker.current.open_ts == 300
    assert "5m" in tracker.current.slug
