"""Historical base rate calculator.

Computes P(BTC finishes up | up X% at T-N seconds) from historical candle data.
This is the core edge: if the base rate is significantly above 50%, we have a
directional signal worth trading.
"""

from __future__ import annotations

from dataclasses import dataclass

import pyarrow.parquet as pq


@dataclass
class BaseRateBin:
    """A bin in the (pct_move, seconds_remaining) grid."""

    pct_move_min: float
    pct_move_max: float
    seconds_remaining: int
    total: int = 0
    up_count: int = 0

    @property
    def p_up(self) -> float:
        if self.total == 0:
            return 0.5
        return self.up_count / self.total


class BaseRateTable:
    """Lookup table for P(up | move, time_remaining).

    Built from historical 1-min BTC candles by simulating 5-min windows.
    """

    def __init__(self):
        self.bins: dict[tuple[float, int], BaseRateBin] = {}
        # Define bin edges for pct_move
        self.move_edges = [-1.0, -0.5, -0.3, -0.15, -0.05, 0.05, 0.15, 0.3, 0.5, 1.0]
        # Seconds remaining at which we compute base rates
        self.time_points = [60, 30, 20, 15, 10, 5]

    def build_from_candles(self, candles: list[dict]):
        """Build base rate table from 1-min candle data.

        Each candle dict has: start (unix ts), open, high, low, close, volume.
        Simulates 5-min windows by grouping into 300s-aligned blocks.
        """
        # Group candles into 5-min windows
        windows: dict[int, list[dict]] = {}
        for c in candles:
            window_ts = c["start"] - (c["start"] % 300)
            windows.setdefault(window_ts, []).append(c)

        # For each window, compute the result and intermediate states
        for window_ts, window_candles in windows.items():
            window_candles.sort(key=lambda x: x["start"])
            if len(window_candles) < 5:
                continue

            open_price = window_candles[0]["open"]
            close_price = window_candles[-1]["close"]
            # flat/equal = UP
            went_up = close_price >= open_price

            # For each time point, compute the intermediate price
            for secs_remaining in self.time_points:
                # Which candle corresponds to T-secs_remaining?
                elapsed = 300 - secs_remaining
                candle_idx = elapsed // 60
                if candle_idx >= len(window_candles):
                    continue

                # Use the OPEN of the candle we're in, not the close.
                # open == close of the previously completed candle, so we
                # never peek at the final window close price.
                intermediate_price = window_candles[candle_idx]["open"]
                pct_move = (intermediate_price - open_price) / open_price * 100

                # Find the bin
                bin_key = self._find_bin(pct_move, secs_remaining)
                if bin_key not in self.bins:
                    self.bins[bin_key] = BaseRateBin(
                        pct_move_min=bin_key[0],
                        pct_move_max=bin_key[0],  # Will be set properly
                        seconds_remaining=secs_remaining,
                    )
                self.bins[bin_key].total += 1
                if went_up:
                    self.bins[bin_key].up_count += 1

    def _find_bin(self, pct_move: float, secs_remaining: int) -> tuple[float, int]:
        """Find the bin for a given move and time remaining."""
        for i in range(len(self.move_edges) - 1):
            if self.move_edges[i] <= pct_move < self.move_edges[i + 1]:
                return (self.move_edges[i], secs_remaining)
        # Extreme moves: clamp to edges
        if pct_move < self.move_edges[0]:
            return (self.move_edges[0], secs_remaining)
        return (self.move_edges[-2], secs_remaining)

    def lookup(self, pct_move: float, seconds_remaining: int) -> float:
        """Look up P(up) for a given intermediate move and time remaining.

        Returns 0.5 if no data for that bin.
        """
        # Find closest time point
        closest_time = min(self.time_points, key=lambda t: abs(t - seconds_remaining))
        bin_key = self._find_bin(pct_move, closest_time)
        b = self.bins.get(bin_key)
        if b is None or b.total < 20:  # Minimum sample size
            return 0.5
        return b.p_up

    def load_from_parquet(self, path: str):
        """Load candle data from parquet and build table."""
        table = pq.read_table(path)
        candles = table.to_pylist()
        self.build_from_candles(candles)

    def summary(self) -> list[dict]:
        """Return a summary of all bins with sufficient data."""
        result = []
        for key, b in sorted(self.bins.items()):
            if b.total >= 20:
                result.append(
                    {
                        "pct_move_min": key[0],
                        "seconds_remaining": key[1],
                        "total": b.total,
                        "p_up": round(b.p_up, 4),
                    }
                )
        return result
