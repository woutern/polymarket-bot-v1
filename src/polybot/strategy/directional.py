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
from polybot.strategy.bedrock_signal import blend_probabilities, get_ai_probability

logger = structlog.get_logger()


def generate_directional_signal(
    bayesian: BayesianUpdater,
    orderbook: OrderbookSnapshot,
    current_price: float,
    open_price: float,
    seconds_remaining: float,
    min_move_pct: float = 0.08,
    min_ev_threshold: float = 0.06,
    max_market_price: float = 0.75,
    window_slug: str = "",
    asset: str = "BTC",
    use_ai: bool = True,
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
        p_bayesian = bayesian.probability
        market_price = orderbook.yes_best_ask
    else:
        direction = Direction.DOWN
        p_bayesian = 1.0 - bayesian.probability
        market_price = orderbook.no_best_ask

    # AI signal: query Bedrock for additional probability estimate
    if use_ai:
        p_ai = get_ai_probability(
            asset=asset,
            window_key=window_slug,
            pct_move=pct_move,
            seconds_remaining=seconds_remaining,
            yes_ask=orderbook.yes_best_ask,
            no_ask=orderbook.no_best_ask,
            p_bayesian=p_bayesian,
        )
        model_prob = blend_probabilities(p_bayesian, p_ai)
        if p_ai is not None:
            logger.info(
                "bedrock_blend",
                asset=asset,
                p_bayesian=round(p_bayesian, 4),
                p_ai=round(p_ai, 4),
                p_final=round(model_prob, 4),
            )
    else:
        model_prob = p_bayesian

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

    # Require a realistic market price — orderbook not yet fetched defaults to 0.0
    # 0.20 floor: prices below this suggest uninitialized orderbook, not real edge
    if market_price < 0.20 or market_price >= 1:
        return None

    # OBI proxy veto: wide bid-ask spread signals reluctant buyers / high uncertainty.
    # True OBI (Cont, Kukanov & Stoikov 2014) needs volume; we approximate via price
    # spread. If spread > 0.15 on the relevant side, skip the trade.
    OBI_SPREAD_THRESHOLD = 0.15
    if direction == Direction.UP:
        yes_spread = orderbook.yes_best_ask - orderbook.yes_best_bid
        if yes_spread > OBI_SPREAD_THRESHOLD:
            logger.info(
                "directional_obi_veto",
                direction=direction.value,
                yes_spread=round(yes_spread, 4),
                obi_threshold=OBI_SPREAD_THRESHOLD,
                slug=window_slug,
                asset=asset,
            )
            return None
    else:  # Direction.DOWN
        no_spread = orderbook.no_best_ask - orderbook.no_best_bid
        if no_spread > OBI_SPREAD_THRESHOLD:
            logger.info(
                "directional_obi_veto",
                direction=direction.value,
                no_spread=round(no_spread, 4),
                obi_threshold=OBI_SPREAD_THRESHOLD,
                slug=window_slug,
                asset=asset,
            )
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
