"""Latency monitor — measures data pipeline lag to optimize AWS placement.

Tracks:
- Coinbase WS tick-to-strategy latency
- Polymarket orderbook fetch round-trip time
- End-to-end signal detection time (price tick → order decision)

On AWS eu-west-1 (Ireland):
- Coinbase WS: advanced-trade-ws.coinbase.com → ~50-80ms from eu-west-1
- Polymarket CLOB: clob.polymarket.com → ~120-180ms (US-East)
- Gamma API: gamma-api.polymarket.com → ~100-150ms
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()


@dataclass
class LatencySample:
    source: str
    latency_ms: float
    timestamp: float = field(default_factory=time.time)


class LatencyMonitor:
    """Rolling window latency tracker for each data source."""

    def __init__(self, window_size: int = 100):
        self._samples: dict[str, deque[float]] = {}
        self._window = window_size
        self._last_report: float = 0.0

    def record(self, source: str, latency_ms: float) -> None:
        if source not in self._samples:
            self._samples[source] = deque(maxlen=self._window)
        self._samples[source].append(latency_ms)

        # Log summary every 5 minutes
        if time.time() - self._last_report > 300:
            self._last_report = time.time()
            self.log_summary()

    def p50(self, source: str) -> float:
        samples = list(self._samples.get(source, []))
        if not samples:
            return 0.0
        return sorted(samples)[len(samples) // 2]

    def p95(self, source: str) -> float:
        samples = list(self._samples.get(source, []))
        if not samples:
            return 0.0
        return sorted(samples)[int(len(samples) * 0.95)]

    def log_summary(self) -> None:
        for source, samples in self._samples.items():
            if not samples:
                continue
            s = list(samples)
            logger.info(
                "latency_summary",
                source=source,
                p50_ms=round(sorted(s)[len(s) // 2], 1),
                p95_ms=round(sorted(s)[int(len(s) * 0.95)], 1),
                avg_ms=round(sum(s) / len(s), 1),
                samples=len(s),
            )


# Global singleton used throughout the app
monitor = LatencyMonitor()
