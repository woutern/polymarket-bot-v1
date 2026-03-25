"""Tests for window tracker."""

import time
from unittest.mock import patch

import pytest

from polybot.market.window_tracker import WindowState, WindowTracker
from polybot.models import Window


# ---------------------------------------------------------------------------
# slug generation
# ---------------------------------------------------------------------------

def test_slug_generation():
    # Timestamp divisible by 300
    assert Window.slug_for_ts(1710000000) == "btc-updown-5m-1710000000"
    # Not divisible — should align down
    assert Window.slug_for_ts(1710000123) == "btc-updown-5m-1710000000"
    # Non-BTC assets must produce correct slugs (regression: resolver defaulted to BTC)
    assert Window.slug_for_ts(1710000000, asset="ETH") == "eth-updown-5m-1710000000"
    assert Window.slug_for_ts(1710000000, asset="SOL") == "sol-updown-5m-1710000000"


def test_resolve_window_uses_asset():
    """resolve_window must pass asset to slug_for_ts — not default to BTC."""
    from unittest.mock import AsyncMock, MagicMock, patch

    async def run():
        from polybot.market.market_resolver import resolve_window
        w = Window(open_ts=1710000000, close_ts=1710000300, asset="ETH")

        mock_resp = MagicMock()
        mock_resp.raise_for_status = lambda: None
        mock_resp.json.return_value = []  # no market — slug is set before HTTP call

        mock_get = AsyncMock(return_value=mock_resp)
        with patch("httpx.AsyncClient.get", mock_get):
            result = await resolve_window(w)

        # slug must be ETH, not BTC
        assert result.slug.startswith("eth-"), f"Expected eth- slug, got: {result.slug}"

    import asyncio
    asyncio.run(run())


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


def test_window_tracker_5m_unchanged():
    """Existing 5-min tracker behaviour is unchanged."""
    tracker = WindowTracker(entry_seconds=10, asset="BTC", window_seconds=300)
    state = tracker.tick(50000.0)
    assert state == WindowState.OPEN
    assert tracker.current is not None
    assert tracker.current.close_ts - tracker.current.open_ts == 300
    assert "5m" in tracker.current.slug


# ---------------------------------------------------------------------------
# State machine transitions (mocked time)
# ---------------------------------------------------------------------------

class TestWindowStateMachine:
    """Test WAITING → OPEN → ENTRY_ZONE → CLOSED transitions."""

    def _make_tracker(self, window_seconds: int = 300, entry_seconds: int = 60) -> WindowTracker:
        return WindowTracker(entry_seconds=entry_seconds, asset="BTC", window_seconds=window_seconds)

    def test_initial_state_is_waiting(self):
        tracker = WindowTracker()
        assert tracker.state == WindowState.WAITING

    def test_first_tick_opens_window(self):
        """First tick always opens a new window (open_ts != 0)."""
        tracker = self._make_tracker()
        state = tracker.tick(50000.0)
        assert state == WindowState.OPEN
        assert tracker.current is not None

    def test_open_state_mid_window(self):
        """When plenty of time remains, state is OPEN."""
        tracker = self._make_tracker(window_seconds=300, entry_seconds=60)
        # Use a 300-aligned open_ts: 1_000_200 % 300 == 0
        open_ts = 1_000_200
        fake_now = open_ts + 100  # 100s into the window, 200s remaining

        with patch("polybot.market.window_tracker.time.time", return_value=float(fake_now)):
            state = tracker.tick(50000.0)

        assert state == WindowState.OPEN

    def test_entry_zone_near_end(self):
        """When remaining ≤ entry_seconds, state transitions to ENTRY_ZONE."""
        tracker = self._make_tracker(window_seconds=300, entry_seconds=60)
        open_ts = 1_000_200  # 300-aligned
        # First tick to open the window (1s in)
        with patch("polybot.market.window_tracker.time.time", return_value=float(open_ts + 1)):
            tracker.tick(50000.0)

        # Second tick: 250s into window → 50s remaining < 60s → ENTRY_ZONE
        fake_now = open_ts + 250
        with patch("polybot.market.window_tracker.time.time", return_value=float(fake_now)):
            state = tracker.tick(50100.0)

        assert state == WindowState.ENTRY_ZONE

    def test_closed_state_past_end(self):
        """When the window closes, the next tick opens a new window and sets the
        previous window's close_price. The new window itself starts as OPEN."""
        tracker = self._make_tracker(window_seconds=300, entry_seconds=60)
        open_ts = 1_000_200  # 300-aligned

        with patch("polybot.market.window_tracker.time.time", return_value=float(open_ts + 1)):
            tracker.tick(50000.0)

        prev_window = tracker.current
        assert prev_window.close_price is None

        # Jump to the next window (open_ts + 300 is the new window's open)
        next_open_ts = open_ts + 300
        fake_now = next_open_ts + 1
        with patch("polybot.market.window_tracker.time.time", return_value=float(fake_now)):
            state = tracker.tick(51000.0)

        # A new window opened → state is OPEN for the new window
        assert state == WindowState.OPEN
        # The previous window's close_price was set to the price at transition
        assert prev_window.close_price == 51000.0
        # The new window is now current and has the correct open price
        assert tracker.current.open_price == 51000.0

    def test_close_price_set_on_window_transition(self):
        """close_price of the previous window is set on the first tick of the new window,
        and is not overwritten by subsequent ticks of the same new window."""
        tracker = self._make_tracker(window_seconds=300, entry_seconds=60)
        open_ts = 1_000_200  # 300-aligned

        with patch("polybot.market.window_tracker.time.time", return_value=float(open_ts + 1)):
            tracker.tick(50000.0)

        window_1 = tracker.current
        next_open_ts = open_ts + 300

        # First tick of window 2 sets window_1.close_price = 51000
        with patch("polybot.market.window_tracker.time.time", return_value=float(next_open_ts + 1)):
            tracker.tick(51000.0)

        assert window_1.close_price == 51000.0
        window_2 = tracker.current

        # Second tick of window 2 — window_1.close_price must not change
        window_2_close = window_2.close_price  # None until window_2 closes
        with patch("polybot.market.window_tracker.time.time", return_value=float(next_open_ts + 50)):
            tracker.tick(52000.0)

        assert window_1.close_price == 51000.0  # unchanged

    def test_new_window_carries_forward_close_price(self):
        """When a new window starts and the previous had no close_price, it is set."""
        tracker = self._make_tracker(window_seconds=300, entry_seconds=60)
        open_ts = 1_000_200  # 300-aligned

        with patch("polybot.market.window_tracker.time.time", return_value=float(open_ts + 1)):
            tracker.tick(50000.0)

        prev_window = tracker.current
        assert prev_window.close_price is None

        # Jump to a new window (next 300-aligned boundary)
        new_open_ts = open_ts + 300
        with patch("polybot.market.window_tracker.time.time", return_value=float(new_open_ts + 1)):
            tracker.tick(51000.0)

        # The previous window's close_price should be filled with the price at transition
        assert prev_window.close_price == 51000.0

    def test_open_price_preserved_across_ticks(self):
        """open_price of current window does not change on subsequent ticks."""
        tracker = self._make_tracker(window_seconds=300, entry_seconds=60)
        open_ts = 1_000_200  # 300-aligned

        with patch("polybot.market.window_tracker.time.time", return_value=float(open_ts + 1)):
            tracker.tick(50000.0)

        open_price = tracker.current.open_price

        with patch("polybot.market.window_tracker.time.time", return_value=float(open_ts + 50)):
            tracker.tick(51000.0)

        assert tracker.current.open_price == open_price

    def test_pct_move_none_before_first_tick(self):
        """pct_move returns None when no window is open yet."""
        tracker = self._make_tracker()
        assert tracker.pct_move(50000.0) is None

    def test_pct_move_zero_open_price(self):
        """pct_move when open_price is None returns None."""
        tracker = self._make_tracker()
        tracker.current = Window(open_ts=0, close_ts=300, open_price=None)
        assert tracker.pct_move(50000.0) is None

    def test_pct_move_positive(self):
        tracker = self._make_tracker()
        tracker.tick(50000.0)
        pct = tracker.pct_move(50500.0)
        assert pct is not None
        assert abs(pct - 1.0) < 0.001  # (500/50000)*100 = 1%

    def test_pct_move_negative(self):
        tracker = self._make_tracker()
        tracker.tick(50000.0)
        pct = tracker.pct_move(49000.0)
        assert pct is not None
        assert abs(pct - (-2.0)) < 0.001  # (-1000/50000)*100 = -2%

    def test_waiting_state_when_current_is_none(self):
        """If open_ts matches but current is None, returns WAITING."""
        tracker = self._make_tracker(window_seconds=300, entry_seconds=60)
        open_ts = 1_000_200  # 300-aligned

        # Tick once to set _last_open_ts, then manually clear current
        with patch("polybot.market.window_tracker.time.time", return_value=float(open_ts + 1)):
            tracker.tick(50000.0)

        # _last_open_ts was set to open_ts during first tick
        tracker.current = None

        # Tick again within the same window — open_ts matches, current is None → WAITING
        with patch("polybot.market.window_tracker.time.time", return_value=float(open_ts + 50)):
            state = tracker.tick(50100.0)

        assert state == WindowState.WAITING
