"""AWS Bedrock (Claude Sonnet 4.6) AI signal layer.

Calls Bedrock once per entry zone (max 1 call/window) to get an AI-driven
probability adjustment on top of the Bayesian estimate.

The AI receives: asset, pct_move, seconds_remaining, yes_ask, no_ask, p_up_bayesian
and returns a JSON object with: {"p_up_ai": 0.65, "confidence": "medium", "reason": "..."}

This is additive — the final probability is a weighted blend:
  p_final = 0.7 * p_bayesian + 0.3 * p_ai
"""

from __future__ import annotations

import json
import logging
import time

logger = logging.getLogger(__name__)

# Model: cross-region inference in eu-west-1
_MODEL_ID = "eu.anthropic.claude-sonnet-4-6-20251001-v1:0"
_MAX_TOKENS = 120
_LAST_CALL: dict[str, float] = {}  # key → timestamp, rate-limit per key
_MIN_INTERVAL = 60.0  # at most once per 60s per asset key

_client = None


def _get_client():
    global _client
    if _client is None:
        try:
            import boto3
            import os
            profile = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None
            session = boto3.Session(profile_name=profile, region_name="eu-west-1")
            _client = session.client("bedrock-runtime")
        except Exception as e:
            logger.debug("bedrock_client_init_failed", extra={"error": str(e)})
    return _client


def get_ai_probability(
    asset: str,
    window_key: str,
    pct_move: float,
    seconds_remaining: float,
    yes_ask: float,
    no_ask: float,
    p_bayesian: float,
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
    prompt = (
        f"You are a crypto prediction market specialist. Analyze this Polymarket binary market:\n\n"
        f"Asset: {asset}/USD\n"
        f"Current price move from window open: {pct_move:+.3f}%\n"
        f"Direction: {direction}\n"
        f"Seconds remaining in 5/15-min window: {seconds_remaining:.0f}s\n"
        f"YES token ask price: {yes_ask:.3f} (market implies {yes_ask*100:.1f}% prob UP)\n"
        f"NO token ask price: {no_ask:.3f} (market implies {no_ask*100:.1f}% prob DOWN)\n"
        f"Bayesian model p(UP): {p_bayesian:.4f}\n\n"
        f"Resolution: UP if price at window close >= price at window open (Chainlink oracle).\n\n"
        f"Respond ONLY with valid JSON: "
        f'{{\"p_up\": <float 0-1>, \"confidence\": \"low|medium|high\", \"reason\": \"<10 words max>\"}}'
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
        logger.debug(
            "bedrock_signal",
            extra={
                "asset": asset,
                "p_ai": round(p_ai, 4),
                "confidence": parsed.get("confidence"),
                "reason": parsed.get("reason"),
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
