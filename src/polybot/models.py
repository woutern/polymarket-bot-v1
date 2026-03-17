"""Core data models."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Direction(Enum):
    UP = "up"
    DOWN = "down"


class SignalSource(Enum):
    ARBITRAGE = "arbitrage"
    DIRECTIONAL = "directional"
    WALLET = "wallet"


SLUG_PREFIXES = {
    "BTC": "btc-updown-5m",
    "ETH": "eth-updown-5m",
    "SOL": "sol-updown-5m",
    "BTC_15M": "btc-updown-15m",
    "ETH_15M": "eth-updown-15m",
    "SOL_15M": "sol-updown-15m",
}


@dataclass
class Window:
    """A single 5-minute prediction window."""

    open_ts: int  # Unix timestamp, divisible by 300
    close_ts: int  # open_ts + 300
    asset: str = "BTC"
    open_price: float | None = None
    close_price: float | None = None
    slug: str = ""
    condition_id: str = ""
    yes_token_id: str = ""
    no_token_id: str = ""

    @property
    def resolved_direction(self) -> Direction | None:
        if self.open_price is None or self.close_price is None:
            return None
        return Direction.UP if self.close_price >= self.open_price else Direction.DOWN

    def seconds_remaining(self) -> float:
        return max(0.0, self.close_ts - time.time())

    @staticmethod
    def slug_for_ts(ts: int, asset: str = "BTC", window_seconds: int = 300) -> str:
        aligned = ts - (ts % window_seconds)
        if window_seconds == 900:
            key = f"{asset.upper()}_15M"
            default_prefix = f"{asset.lower()}-updown-15m"
        else:
            key = asset.upper()
            default_prefix = f"{asset.lower()}-updown-5m"
        prefix = SLUG_PREFIXES.get(key, default_prefix)
        return f"{prefix}-{aligned}"


@dataclass
class OrderbookSnapshot:
    """Best bid/ask for YES and NO tokens."""

    yes_best_bid: float = 0.0
    yes_best_ask: float = 1.0
    no_best_bid: float = 0.0
    no_best_ask: float = 1.0
    timestamp: float = 0.0


@dataclass
class Signal:
    """A trading signal from a strategy."""

    source: SignalSource
    direction: Direction
    model_prob: float
    market_price: float
    ev: float
    size_usd: float = 0.0
    window_slug: str = ""
    asset: str = "BTC"


@dataclass
class TradeRecord:
    """A completed or pending trade."""

    id: str = ""
    timestamp: float = 0.0
    window_slug: str = ""
    asset: str = "BTC"
    source: str = ""
    direction: str = ""
    side: str = ""
    price: float = 0.0
    size_usd: float = 0.0
    fill_price: float | None = None
    pnl: float | None = None
    resolved: bool = False


@dataclass
class MarketInfo:
    """Polymarket market metadata from Gamma API."""

    condition_id: str = ""
    question: str = ""
    slug: str = ""
    yes_token_id: str = ""
    no_token_id: str = ""
    end_date: str = ""
    active: bool = False


@dataclass
class DailyStats:
    """Daily performance tracking."""

    date: str = ""
    trades: int = 0
    wins: int = 0
    losses: int = 0
    gross_pnl: float = 0.0
    fees: float = 0.0
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    bankroll_start: float = 0.0
    bankroll_end: float = 0.0
