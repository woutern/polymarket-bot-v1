"""Window-aligned clock and T-minus countdown."""

from __future__ import annotations

import time

WINDOW_SECONDS = 300  # 5 minutes


def current_window_open() -> int:
    """Return the Unix timestamp of the current 5-min window open."""
    now = int(time.time())
    return now - (now % WINDOW_SECONDS)


def next_window_open() -> int:
    return current_window_open() + WINDOW_SECONDS


def seconds_until_close() -> float:
    """Seconds remaining in the current window."""
    return float(next_window_open()) - time.time()


def seconds_until_entry(entry_seconds: int = 10) -> float:
    """Seconds until T-entry_seconds (when we start looking for directional signals)."""
    return seconds_until_close() - entry_seconds


def window_slug(ts: int | None = None) -> str:
    """Generate market slug for a given (or current) window."""
    if ts is None:
        ts = current_window_open()
    aligned = ts - (ts % WINDOW_SECONDS)
    return f"btc-updown-5m-{aligned}"


def is_in_entry_zone(entry_seconds: int = 10) -> bool:
    """Are we in the last `entry_seconds` of the window?"""
    return seconds_until_close() <= entry_seconds


