"""Latency arbitrage: detect Coinbase price move before Polymarket reprices.

The edge: Polymarket market makers take 200-800ms to reprice after a Coinbase
tick. If we detect a move and fire an order within that window, we buy the
cheap side before it gets repriced.

This is the primary alpha source — not prediction, but speed.
"""

from __future__ import annotations

import structlog

from polybot.models import Direction, OrderbookSnapshot, Signal, SignalSource

logger = structlog.get_logger()


def check_latency_arb(
    current_price: float,
    open_price: float,
    orderbook: OrderbookSnapshot,
    min_move_pct: float = 0.03,
    max_cheap_price: float = 0.65,
    min_profit_margin: float = 0.10,
    window_slug: str = "",
    asset: str = "BTC",
) -> Signal | None:
    """Check if Coinbase shows a move but Polymarket hasn't repriced yet.

    The key: if BTC is up 0.05% but YES is still trading at $0.40,
    the market hasn't caught up. Buy YES at $0.40, it resolves at $1.00.

    Args:
        current_price: Latest Coinbase price.
        open_price: Window open price.
        orderbook: Current Polymarket orderbook.
        min_move_pct: Minimum price move to consider (0.03% = very sensitive).
        max_cheap_price: Only buy if ask < this (don't buy at $0.90).
        min_profit_margin: Minimum expected profit per dollar (model_prob - market_price).
        window_slug: Current window slug.
        asset: Asset name.
    """
    if open_price <= 0:
        return None

    pct_move = (current_price - open_price) / open_price * 100

    if abs(pct_move) < min_move_pct:
        return None

    # Determine which side is cheap based on price direction
    if pct_move > 0:
        # BTC is UP → YES should win → is YES still cheap?
        direction = Direction.UP
        market_price = orderbook.yes_best_ask
        # Simple model: if BTC is up by min_move_pct, high chance it stays up
        # More move = more confidence
        model_prob = min(0.95, 0.60 + abs(pct_move) * 2.0)
    else:
        # BTC is DOWN → NO should win → is NO still cheap?
        direction = Direction.DOWN
        market_price = orderbook.no_best_ask
        model_prob = min(0.95, 0.60 + abs(pct_move) * 2.0)

    # Is the market still cheap? (hasn't repriced yet)
    if market_price > max_cheap_price:
        return None

    # Is there enough profit margin?
    profit_margin = model_prob - market_price
    if profit_margin < min_profit_margin:
        return None

    ev = profit_margin / market_price

    logger.info(
        "latency_arb_signal",
        asset=asset,
        direction=direction.value,
        pct_move=round(pct_move, 4),
        model_prob=round(model_prob, 4),
        market_price=round(market_price, 4),
        profit_margin=round(profit_margin, 4),
        ev=round(ev, 4),
        slug=window_slug,
    )

    return Signal(
        source=SignalSource.DIRECTIONAL,
        direction=direction,
        model_prob=model_prob,
        market_price=market_price,
        ev=ev,
        window_slug=window_slug,
        asset=asset,
    )
