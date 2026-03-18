"""Late-window directional signal generator.

Checks for price moves in the entry zone (T-60s to T-0s) and generates
signals when:
1. Price moved > threshold from window open
2. Market hasn't fully priced it in yet (ask < max_market_price)
3. EV > threshold based on model probability vs market price

Returns SignalEvaluation for every evaluation — both fired signals and
rejections (with reason) — so the dashboard can show what was skipped.
"""

from __future__ import annotations

import structlog

from polybot.models import Direction, OrderbookSnapshot, Signal, SignalEvaluation, SignalSource
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
) -> SignalEvaluation:
    """Evaluate whether to generate a directional signal.

    Always returns a SignalEvaluation — either with a signal (trade) or
    with a rejection_reason (skipped). This allows the dashboard to show
    both fired and rejected signals.
    """
    tf = "15m" if "15m" in window_slug else "5m"

    # Base evaluation context (filled progressively)
    base = dict(
        asset=asset,
        window_slug=window_slug,
        timeframe=tf,
        seconds_remaining=seconds_remaining,
        open_price=open_price,
        current_price=current_price,
        yes_ask=orderbook.yes_best_ask,
        no_ask=orderbook.no_best_ask,
    )

    if open_price <= 0:
        return SignalEvaluation(signal=None, rejection_reason="invalid_open_price", **base)

    pct_move = (current_price - open_price) / open_price * 100
    base["pct_move"] = pct_move

    if abs(pct_move) < min_move_pct:
        return SignalEvaluation(signal=None, rejection_reason="min_move", **base)

    # Determine direction from price movement
    if pct_move > 0:
        direction = Direction.UP
        p_bayesian = bayesian.probability
        market_price = orderbook.yes_best_ask
    else:
        direction = Direction.DOWN
        p_bayesian = 1.0 - bayesian.probability
        market_price = orderbook.no_best_ask

    base["direction"] = direction.value
    base["p_bayesian"] = p_bayesian
    base["market_price"] = market_price

    # AI signal: query Bedrock for additional probability estimate
    p_ai: float | None = None
    if use_ai:
        p_ai = get_ai_probability(
            asset=asset,
            window_key=window_slug,
            pct_move=pct_move,
            seconds_remaining=seconds_remaining,
            yes_ask=orderbook.yes_best_ask,
            no_ask=orderbook.no_best_ask,
            yes_bid=orderbook.yes_best_bid,
            no_bid=orderbook.no_best_bid,
            p_bayesian=p_bayesian,
            open_price=open_price,
            current_price=current_price,
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
            logger.debug("bedrock_skipped_rate_limited", asset=asset, slug=window_slug)
    else:
        model_prob = p_bayesian

    base["p_ai"] = p_ai
    base["model_prob"] = model_prob

    # Market efficiency filter
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
        return SignalEvaluation(signal=None, rejection_reason="market_efficient", **base)

    # Price floor guard
    if market_price < 0.20 or market_price >= 1:
        return SignalEvaluation(signal=None, rejection_reason="unrealistic_price", **base)

    # OBI proxy veto
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
            return SignalEvaluation(signal=None, rejection_reason="obi_veto", **base)
    else:
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
            return SignalEvaluation(signal=None, rejection_reason="obi_veto", **base)

    ev = (model_prob - market_price) / market_price
    base["ev"] = ev

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
        return SignalEvaluation(signal=None, rejection_reason="insufficient_ev", **base)

    # All guards passed — fire signal
    logger.info(
        "directional_signal",
        direction=direction.value,
        model_prob=round(model_prob, 4),
        market_price=round(market_price, 4),
        ev=round(ev, 4),
        pct_move=round(pct_move, 4),
        seconds_remaining=round(seconds_remaining, 1),
        p_bayesian=round(p_bayesian, 4),
        p_ai=round(p_ai, 4) if p_ai is not None else None,
        slug=window_slug,
        asset=asset,
    )

    signal = Signal(
        source=SignalSource.DIRECTIONAL,
        direction=direction,
        model_prob=model_prob,
        market_price=market_price,
        ev=ev,
        window_slug=window_slug,
        asset=asset,
        p_bayesian=p_bayesian,
        p_ai=p_ai,
        pct_move=pct_move,
        seconds_remaining=seconds_remaining,
        yes_ask=orderbook.yes_best_ask,
        no_ask=orderbook.no_best_ask,
        yes_bid=orderbook.yes_best_bid,
        no_bid=orderbook.no_best_bid,
        open_price=open_price,
    )

    return SignalEvaluation(signal=signal, rejection_reason=None, **base)
