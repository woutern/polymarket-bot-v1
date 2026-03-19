"""5-minute window state machine.

Tracks the lifecycle of each prediction window per asset:
  WAITING → OPEN → ENTRY_ZONE → CLOSED → RESOLVED
"""

from __future__ import annotations

import time
from enum import Enum

from polybot.core.clock import WINDOW_SECONDS, current_window_open
from polybot.models import Window


class WindowState(Enum):
    WAITING = "waiting"
    OPEN = "open"
    ENTRY_ZONE = "entry_zone"
    CLOSED = "closed"
    RESOLVED = "resolved"


class WindowTracker:
    def __init__(
        self,
        entry_seconds: int = 60,
        asset: str = "BTC",
        window_seconds: int = WINDOW_SECONDS,
    ):
        self.entry_seconds = entry_seconds
        self.asset = asset
        self.window_seconds = window_seconds
        self.current: Window | None = None
        self.state: WindowState = WindowState.WAITING
        self._last_open_ts: int = 0

    def tick(self, price: float) -> WindowState:
        """Call on every price tick. Returns the current state."""
        now = time.time()
        now_int = int(now)
        open_ts = now_int - (now_int % self.window_seconds)
        close_ts = open_ts + self.window_seconds
        remaining = close_ts - now

        # New window started
        if open_ts != self._last_open_ts:
            if self.current is not None and self.current.close_price is None:
                self.current.close_price = price

            self.current = Window(
                open_ts=open_ts,
                close_ts=close_ts,
                asset=self.asset,
                open_price=price,
                slug=Window.slug_for_ts(open_ts, self.asset, self.window_seconds),
            )
            self._last_open_ts = open_ts
            self.state = WindowState.OPEN
            return self.state

        if self.current is None:
            return WindowState.WAITING

        if remaining <= 0:
            if self.state != WindowState.CLOSED:
                self.current.close_price = price
            self.state = WindowState.CLOSED
        elif remaining <= self.entry_seconds:
            self.state = WindowState.ENTRY_ZONE
        else:
            self.state = WindowState.OPEN

        return self.state

    def pct_move(self, current_price: float) -> float | None:
        """Percentage move from window open price."""
        if self.current is None or self.current.open_price is None:
            return None
        return (current_price - self.current.open_price) / self.current.open_price * 100
