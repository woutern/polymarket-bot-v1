"""AWS Bedrock (Claude Sonnet 4.6) AI signal layer.

Calls Bedrock once per entry zone (max 1 call/20s per window) to get an AI-driven
probability adjustment on top of the Bayesian estimate.

The AI receives full context: asset, price action, market prices, base rates, Bayesian estimate.
Returns JSON: {"p_up": float, "confidence": 1-5, "key_factor": "<5 words"}

Final probability is a weighted blend:
  p_final = (1 - ai_weight) * p_bayesian + ai_weight * p_ai
"""

from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)

# Model: cross-region inference in eu-west-1
_MODEL_ID = "anthropic.claude-sonnet-4-6-20251001-v1:0"
_MAX_TOKENS = 150
_LAST_CALL: dict[str, float] = {}  # key → timestamp, rate-limit per key
_MIN_INTERVAL = 20.0  # at most once per 20s per window key (down from 60s)

_client = None


def _get_client():
    global _client
    if _client is None:
        try:
            import boto3
            import os
            profile = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None
            session = boto3.Session(profile_name=profile, region_name="us-east-1")
            _client = session.client("bedrock-runtime")
        except Exception as e:
            logger.debug("bedrock_client_init_failed", extra={"error": str(e)})
    return _client


def _momentum_description(pct_move: float, seconds_remaining: float) -> str:
    """Human-readable momentum label for the prompt."""
    abs_move = abs(pct_move)
    direction = "up" if pct_move > 0 else "down"
    if abs_move >= 0.5:
        strength = "strong"
    elif abs_move >= 0.2:
        strength = "moderate"
    else:
        strength = "mild"
    time_label = "late" if seconds_remaining < 30 else "mid" if seconds_remaining < 90 else "early"
    return f"{strength} {direction}move, {time_label}-window"


def _liquidity_description(spread: float) -> str:
    if spread < 0.03:
        return "tight — good liquidity"
    elif spread < 0.08:
        return "moderate"
    else:
        return "wide — thin book"


def get_ai_probability(
    asset: str,
    window_key: str,
    pct_move: float,
    seconds_remaining: float,
    yes_ask: float,
    no_ask: float,
    yes_bid: float,
    no_bid: float,
    p_bayesian: float,
    open_price: float,
    current_price: float,
) -> float | None:
    """Query Bedrock for an AI probability estimate.

    Returns adjusted p_up (0-1) or None if unavailable/rate-limited.
    Never raises — all errors return None silently.
    """
    now = time.time()
    last = _LAST_CALL.get(window_key, 0.0)
    if now - last < _MIN_INTERVAL:
        return None  # rate-limited
    _LAST_CALL[window_key] = now

    client = _get_client()
    if client is None:
        return None

    direction = "UP" if pct_move >= 0 else "DOWN"
    spread = yes_ask - yes_bid if direction == "UP" else no_ask - no_bid
    momentum_desc = _momentum_description(pct_move, seconds_remaining)
    liquidity_desc = _liquidity_description(spread)

    prompt = (
        f"Asset: {asset}/USD | Direction: {direction} | Seconds remaining: {seconds_remaining:.0f}s\n\n"
        f"PRICE ACTION:\n"
        f"  Move from window open: {pct_move:+.3f}% ({momentum_desc})\n"
        f"  Open: {open_price:.2f} | Current: {current_price:.2f}\n\n"
        f"MARKET PRICES (Polymarket):\n"
        f"  YES ask: {yes_ask:.3f} (implied {yes_ask*100:.1f}% prob UP)\n"
        f"  NO ask:  {no_ask:.3f}\n"
        f"  Spread:  {spread:.3f} — {liquidity_desc}\n\n"
        f"BAYESIAN MODEL:\n"
        f"  Current estimate: P(close_up) = {p_bayesian:.4f}\n"
        f"  Evidence: {seconds_remaining:.0f}s of {asset} price data in this window\n\n"
        f"Estimate P(close_up) using Chainlink resolution (not Coinbase). "
        f"Return JSON only: "
        f'{{\"p_up\": <float 0-1>, \"confidence\": <1-5>, \"key_factor\": \"<5 words max>\"}}'
    )

    try:
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": _MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
        })
        resp = client.invoke_model(modelId=_MODEL_ID, body=body)
        result = json.loads(resp["body"].read())
        text = result["content"][0]["text"].strip()
        parsed = json.loads(text)
        p_ai = float(parsed.get("p_up", p_bayesian))
        p_ai = max(0.01, min(0.99, p_ai))
        logger.info(
            "bedrock_signal",
            extra={
                "asset": asset,
                "p_ai": round(p_ai, 4),
                "p_bayesian": round(p_bayesian, 4),
                "confidence": parsed.get("confidence"),
                "key_factor": parsed.get("key_factor"),
                "window_key": window_key,
            },
        )
        return p_ai
    except Exception as e:
        logger.debug("bedrock_signal_failed", extra={"error": str(e)})
        return None


def blend_probabilities(p_bayesian: float, p_ai: float | None, ai_weight: float = 0.3) -> float:
    """Blend Bayesian and AI probabilities.

    Args:
        p_bayesian: Bayesian model probability (0-1).
        p_ai: AI model probability (0-1), or None to skip blending.
        ai_weight: Weight for AI signal (0.3 = 30% AI, 70% Bayesian).

    Returns:
        Blended probability.
    """
    if p_ai is None:
        return p_bayesian
    return (1 - ai_weight) * p_bayesian + ai_weight * p_ai
