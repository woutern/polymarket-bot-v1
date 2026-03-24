"""Shared types for strategy layer.

MarketState — what the strategy sees each tick.
StrategyAction — what the strategy wants to do each tick.

These are pure data classes. No logic, no side effects.
The engine reads StrategyAction and executes the orders.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MarketState:
    """Current market snapshot passed to strategy every tick."""

    seconds: int = 0          # seconds since window open
    yes_bid: float = 0.50     # best bid on YES (UP) side
    no_bid: float = 0.50      # best bid on NO (DOWN) side
    yes_ask: float = 0.51     # best ask on YES side
    no_ask: float = 0.51      # best ask on NO side
    prob_up: float = 0.50     # model prediction: P(BTC goes up)

    @property
    def spread(self) -> float:
        """Absolute difference between yes_bid and no_bid."""
        return round(abs(self.yes_bid - self.no_bid), 4)

    @property
    def market_up(self) -> bool:
        """True if market thinks UP is winning."""
        return self.yes_bid > self.no_bid


@dataclass
class StrategyAction:
    """What the strategy wants to do on this tick.

    Engine reads this and posts/cancels orders accordingly.
    Zero values mean no action on that side.
    """

    # Buys — post limit orders at these prices
    buy_up_shares: int = 0
    buy_up_price: float = 0.0
    buy_down_shares: int = 0
    buy_down_price: float = 0.0

    # Sells — post limit orders at these prices
    sell_up_shares: int = 0
    sell_up_price: float = 0.0
    sell_down_shares: int = 0
    sell_down_price: float = 0.0

    # Debug: why this action was taken
    reason: str = ""

    def has_action(self) -> bool:
        """True if this tick produces any order."""
        return any([
            self.buy_up_shares,
            self.buy_down_shares,
            self.sell_up_shares,
            self.sell_down_shares,
        ])
