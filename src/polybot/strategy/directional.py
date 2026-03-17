"""Late-window directional signal generator.

Checks for price moves in the entry zone (T-60s to T-0s) and generates
signals when:
1. Price moved > threshold from window open
2. Market hasn't fully priced it in yet (ask < max_market_price)
3. EV > threshold based on model probability vs market price
"""

from __future__ import annotations

import structlog

from polybot.models import Direction, OrderbookSnapshot, Signal, SignalSource
from polybot.strategy.bayesian import BayesianUpdater

logger = structlog.get_logger()


def generate_directional_signal(
    bayesian: BayesianUpdater,
    orderbook: OrderbookSnapshot,
    current_price: float,
    open_price: float,
    seconds_remaining: float,
    min_move_pct: float = 0.08,
    min_ev_threshold: float = 0.05,
    max_market_price: float = 0.85,
    window_slug: str = "",
    asset: str = "BTC",
) -> Signal | None:
    """Generate a directional signal if conditions are met.

    Conditions:
    1. Price moved > min_move_pct from window open
    2. Market ask < max_market_price (market hasn't fully priced in)
    3. EV > min_ev_threshold
    """
    if open_price <= 0:
        return None

    pct_move = (current_price - open_price) / open_price * 100

    if abs(pct_move) < min_move_pct:
        return None

    # Determine direction from price movement
    if pct_move > 0:
        direction = Direction.UP
        model_prob = bayesian.probability
        market_price = orderbook.yes_best_ask
    else:
        direction = Direction.DOWN
        model_prob = 1.0 - bayesian.probability
        market_price = orderbook.no_best_ask

    # Market efficiency filter: if ask > max_market_price, market already priced it in
    if market_price > max_market_price:
        logger.info(
            "directional_market_efficient",
            direction=direction.value,
            market_price=round(market_price, 4),
            max_allowed=max_market_price,
            pct_move=round(pct_move, 4),
            seconds_remaining=round(seconds_remaining, 1),
            slug=window_slug,
            asset=asset,
        )
        return None

    if market_price <= 0 or market_price >= 1:
        return None

    ev = (model_prob - market_price) / market_price

    if ev < min_ev_threshold:
        logger.info(
            "directional_insufficient_ev",
            direction=direction.value,
            model_prob=round(model_prob, 4),
            market_price=round(market_price, 4),
            ev=round(ev, 4),
            pct_move=round(pct_move, 4),
            seconds_remaining=round(seconds_remaining, 1),
            slug=window_slug,
            asset=asset,
        )
        return None

    logger.info(
        "directional_signal",
        direction=direction.value,
        model_prob=round(model_prob, 4),
        market_price=round(market_price, 4),
        ev=round(ev, 4),
        pct_move=round(pct_move, 4),
        seconds_remaining=round(seconds_remaining, 1),
        slug=window_slug,
        asset=asset,
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
