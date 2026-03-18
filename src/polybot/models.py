"""Core data models."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum


class Direction(Enum):
    UP = "up"
    DOWN = "down"


class SignalSource(Enum):
    DIRECTIONAL = "directional"


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
    # Signal tracking
    signals_fired: int = 0
    trades_executed: int = 0
    rejection_reason: str = ""
    polymarket_winner: str | None = None  # "YES" | "NO" once verified
    max_pct_move: float = 0.0  # Max price move seen in this window

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
    """Best bid/ask for YES and NO tokens, plus shallow bid depth."""

    yes_best_bid: float = 0.0
    yes_best_ask: float = 1.0
    no_best_bid: float = 0.0
    no_best_ask: float = 1.0
    timestamp: float = 0.0
    # Sum of top-3 bid sizes; used as a proxy for order-book depth / OBI.
    yes_bid_depth: float = 0.0
    no_bid_depth: float = 0.0


@dataclass
class Signal:
    """A trading signal from the directional strategy."""

    source: SignalSource
    direction: Direction
    model_prob: float        # Final blended probability
    market_price: float      # Ask price at entry
    ev: float                # (model_prob - market_price) / market_price
    size_usd: float = 0.0
    window_slug: str = ""
    asset: str = "BTC"
    # Component probabilities (for analysis / AI improvement)
    p_bayesian: float = 0.0          # Pure Bayesian component (before AI blend)
    p_ai: float | None = None        # AI component (None if Bedrock skipped)
    # Price context at signal time
    pct_move: float = 0.0            # % price move from window open
    seconds_remaining: float = 0.0  # How many seconds left in window
    yes_ask: float = 0.0             # YES token ask at signal time
    no_ask: float = 0.0              # NO token ask at signal time
    yes_bid: float = 0.0             # YES token bid (for spread calc)
    no_bid: float = 0.0              # NO token bid
    open_price: float = 0.0         # Window open price


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
    mode: str = "paper"
    # Signal metadata at entry (for analysis)
    p_bayesian: float = 0.0
    p_ai: float | None = None
    p_final: float = 0.0
    pct_move: float = 0.0
    seconds_remaining: float = 0.0
    ev: float = 0.0
    # Outcome verification (filled async after window closes)
    outcome_source: str = "coinbase_inferred"   # "coinbase_inferred" | "polymarket_verified"
    polymarket_winner: str | None = None         # "YES" | "NO" | None (pending)
    correct_prediction: bool | None = None       # Did our direction match Polymarket outcome?


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
