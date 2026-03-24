"""Feature builder for MarketMaker LightGBM model.

Computes the 14 features the model was trained on, from live price data
tracked throughout the window.

Features (matching training pipeline exactly):
    move_pct_15s        % price move in first 15s of window
    realized_vol_5m     annualized realized vol from price history
    vol_ratio           current vol / rolling vol average
    body_ratio          abs(current - open) / (high - low) in window
    prev_window_direction 1 if prev window went UP, -1 DOWN, 0 unknown
    prev_window_move_pct  % move of previous window
    hour_sin / hour_cos  cyclic UTC hour encoding
    dow_sin / dow_cos    cyclic day-of-week encoding
    signal_move_pct     abs(move_pct) at signal time
    signal_ask_price    winning side ask at signal time
    signal_seconds      seconds elapsed at signal time
    signal_ev           expected value at signal time (0 if unknown)

Usage:
    fb = FeatureBuilder(open_price=95000.0, window_open_ts=time.time())
    # Each Coinbase tick:
    fb.on_price(price=95050.0, ts=time.time())
    # When you want a prediction:
    features = fb.compute(current_ask=0.65, seconds=210)
    prob_up = model_server.predict("BTC_5m", features)
"""

from __future__ import annotations

import math
import time
from collections import deque
from datetime import datetime, timezone
from dataclasses import dataclass, field

from polybot.feeds.rtds_ws import compute_realized_vol


@dataclass
class PrevWindow:
    open_price: float = 0.0
    close_price: float = 0.0


class FeatureBuilder:
    """Accumulates per-window price data and computes model features on demand.

    One instance per active window. Feed Coinbase prices every tick.
    """

    def __init__(
        self,
        open_price: float,
        window_open_ts: float,
        prev_window: PrevWindow | None = None,
        vol_history: deque | None = None,
    ):
        self.open_price = open_price
        self.window_open_ts = window_open_ts
        self.prev_window = prev_window

        # Price history for vol computation (last 300 ticks @ 250ms = 75s)
        self._prices: deque[float] = deque(maxlen=1200)
        self._prices.append(open_price)

        # Window high/low for body_ratio
        self._high = open_price
        self._low = open_price

        # Price 15s into the window (captured once)
        self._price_at_15s: float | None = None

        # Rolling vol history across windows (for vol_ratio)
        # Caller should share the same deque across windows
        self._vol_history: deque[float] = vol_history if vol_history is not None else deque(maxlen=20)

    # ------------------------------------------------------------------
    # Feed
    # ------------------------------------------------------------------

    def on_price(self, price: float, ts: float | None = None) -> None:
        """Call every time a new Coinbase price arrives (~250ms)."""
        if price <= 0:
            return
        self._prices.append(price)
        if price > self._high:
            self._high = price
        if price < self._low:
            self._low = price

        # Capture price at T+15s (once)
        if self._price_at_15s is None:
            elapsed = (ts or time.time()) - self.window_open_ts
            if elapsed >= 15.0:
                self._price_at_15s = price

    # ------------------------------------------------------------------
    # Feature computation
    # ------------------------------------------------------------------

    def compute(
        self,
        current_ask: float = 0.65,
        seconds: int = 210,
    ) -> dict:
        """Return the 14-feature dict for model.predict()."""
        now_utc = datetime.now(timezone.utc)
        current_price = self._prices[-1] if self._prices else self.open_price

        # move_pct_15s — price move in first 15s (directional signal)
        price_15s = self._price_at_15s or current_price
        move_pct_15s = (
            (price_15s - self.open_price) / self.open_price * 100
            if self.open_price > 0 else 0.0
        )

        # realized_vol_5m
        vol = compute_realized_vol(list(self._prices), tick_interval_seconds=0.25)

        # vol_ratio — current vol vs rolling avg
        vol_avg = sum(self._vol_history) / len(self._vol_history) if self._vol_history else vol
        vol_ratio = vol / vol_avg if vol_avg > 0 else 1.0

        # body_ratio — candle body / full range
        hl_range = self._high - self._low
        body = abs(current_price - self.open_price)
        body_ratio = body / hl_range if hl_range > 0 else 0.5

        # prev window
        prev_dir = 0
        prev_move_pct = 0.0
        if self.prev_window and self.prev_window.open_price and self.prev_window.close_price:
            prev_dir = 1 if self.prev_window.close_price >= self.prev_window.open_price else -1
            prev_move_pct = (
                (self.prev_window.close_price - self.prev_window.open_price)
                / self.prev_window.open_price * 100
            )

        # cyclic time features
        hour_sin = math.sin(2 * math.pi * now_utc.hour / 24)
        hour_cos = math.cos(2 * math.pi * now_utc.hour / 24)
        dow_sin = math.sin(2 * math.pi * now_utc.weekday() / 7)
        dow_cos = math.cos(2 * math.pi * now_utc.weekday() / 7)

        # signal features — use current state at call time
        signal_move_pct = abs(move_pct_15s)
        signal_ask_price = current_ask
        signal_seconds = float(seconds)
        signal_ev = 0.0  # not computed in real-time (requires baseline rates)

        return {
            "move_pct_15s": round(move_pct_15s, 6),
            "realized_vol_5m": round(vol, 8),
            "vol_ratio": round(vol_ratio, 4),
            "body_ratio": round(body_ratio, 4),
            "prev_window_direction": prev_dir,
            "prev_window_move_pct": round(prev_move_pct, 6),
            "hour_sin": round(hour_sin, 6),
            "hour_cos": round(hour_cos, 6),
            "dow_sin": round(dow_sin, 6),
            "dow_cos": round(dow_cos, 6),
            "signal_move_pct": round(signal_move_pct, 6),
            "signal_ask_price": round(signal_ask_price, 4),
            "signal_seconds": round(signal_seconds, 1),
            "signal_ev": signal_ev,
        }

    def close(self, close_price: float) -> PrevWindow:
        """Call at window close. Returns a PrevWindow for the next window."""
        # Update vol history with this window's vol
        vol = compute_realized_vol(list(self._prices), tick_interval_seconds=0.25)
        if vol > 0:
            self._vol_history.append(vol)
        return PrevWindow(open_price=self.open_price, close_price=close_price)
