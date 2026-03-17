"""Arbitrage detector: YES_ask + NO_ask < 1.00 → locked profit."""

from __future__ import annotations

import structlog

from polybot.models import Direction, OrderbookSnapshot, Signal, SignalSource

logger = structlog.get_logger()


def check_arbitrage(orderbook: OrderbookSnapshot, window_slug: str = "") -> Signal | None:
    """Check if YES_ask + NO_ask < 1.00 (guaranteed profit).

    If so, return a signal to buy both sides.
    """
    total_cost = orderbook.yes_best_ask + orderbook.no_best_ask

    if total_cost >= 1.0:
        return None

    profit_per_dollar = 1.0 - total_cost
    ev = profit_per_dollar / total_cost  # Return on capital

    logger.info(
        "arbitrage_detected",
        yes_ask=orderbook.yes_best_ask,
        no_ask=orderbook.no_best_ask,
        total_cost=round(total_cost, 4),
        profit=round(profit_per_dollar, 4),
        ev=round(ev, 4),
        slug=window_slug,
    )

    # Return signal with YES direction (we're buying both sides anyway)
    return Signal(
        source=SignalSource.ARBITRAGE,
        direction=Direction.UP,  # Doesn't matter — we buy both
        model_prob=0.999,  # Near-certain — 1.0 breaks Kelly formula (p >= 1 guard)
        market_price=total_cost,
        ev=ev,
        window_slug=window_slug,
    )
