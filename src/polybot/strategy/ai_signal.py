"""AI-powered prediction signal using AWS Bedrock Claude Haiku.

Takes recent news headlines + price action and asks Claude Haiku
for a directional probability estimate. Responses are cached for
30 seconds to avoid excessive API calls.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import structlog

try:
    import boto3
    from botocore.config import Config as BotocoreConfig
except ImportError:
    boto3 = None  # type: ignore[assignment]
    BotocoreConfig = None  # type: ignore[assignment]

from polybot.feeds.news_feed import Headline
from polybot.models import Direction

logger = structlog.get_logger()

BEDROCK_MODEL_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
BEDROCK_REGION = "eu-west-1"
CACHE_TTL_SECONDS = 30
BEDROCK_TIMEOUT = 5

SYSTEM_PROMPT = (
    "You are a crypto market analyst. You will be given recent crypto news "
    "headlines and current price action data. Your job is to estimate the "
    "short-term direction of the asset price.\n\n"
    "You MUST respond with ONLY a valid JSON object, no other text:\n"
    '{"direction": "up" or "down", "confidence": 0.0-1.0, "reasoning": "brief explanation"}'
)

USER_PROMPT_TEMPLATE = (
    "Asset: {asset}\n"
    "Current price: ${current_price:,.2f}\n"
    "Price {minutes} minutes ago: ${reference_price:,.2f}\n"
    "Price change: {pct_change:+.3f}%\n"
    "Time remaining in window: {seconds_remaining:.0f} seconds\n\n"
    "Recent headlines:\n{headlines}\n\n"
    "Given these headlines and price action, what is the probability "
    "that {asset} will be HIGHER than its window open price when the "
    "5-minute window closes?\n\n"
    "Respond with ONLY a JSON object: "
    '{{"direction": "up"|"down", "confidence": 0.0-1.0, "reasoning": "..."}}'
)


@dataclass
class AISignalResult:
    """Result from an AI signal query."""

    direction: Direction
    confidence: float
    reasoning: str
    timestamp: float
    asset: str


class AISignalGenerator:
    """Generates trading signals using AWS Bedrock Claude Haiku.

    Usage:
        gen = AISignalGenerator()
        result = gen.get_signal(
            asset="BTC",
            headlines=[...],
            current_price=65000.0,
            reference_price=64900.0,
            seconds_remaining=45.0,
        )
    """

    def __init__(self):
        self._client = None
        self._cache: dict[str, tuple[AISignalResult, float]] = {}

    def _get_client(self):
        """Lazy-init the Bedrock client."""
        if self._client is None:
            config = BotocoreConfig(
                region_name=BEDROCK_REGION,
                read_timeout=BEDROCK_TIMEOUT,
                connect_timeout=BEDROCK_TIMEOUT,
                retries={"max_attempts": 1},
            )
            self._client = boto3.client("bedrock-runtime", config=config)
        return self._client

    def get_signal(
        self,
        asset: str,
        headlines: list[Headline],
        current_price: float,
        reference_price: float,
        seconds_remaining: float,
    ) -> AISignalResult | None:
        """Get an AI-powered directional signal.

        Returns None on any error (timeout, parse failure, etc.).
        Results are cached for CACHE_TTL_SECONDS.
        """
        cache_key = asset.upper()
        now = time.time()

        # Check cache
        if cache_key in self._cache:
            cached_result, cached_at = self._cache[cache_key]
            if (now - cached_at) < CACHE_TTL_SECONDS:
                logger.debug(
                    "ai_signal_cache_hit",
                    asset=asset,
                    age=round(now - cached_at, 1),
                )
                return cached_result

        try:
            result = self._invoke(
                asset=asset,
                headlines=headlines,
                current_price=current_price,
                reference_price=reference_price,
                seconds_remaining=seconds_remaining,
            )
            if result is not None:
                self._cache[cache_key] = (result, now)
            return result
        except Exception as e:
            logger.error("ai_signal_failed", asset=asset, error=str(e))
            return None

    def _invoke(
        self,
        asset: str,
        headlines: list[Headline],
        current_price: float,
        reference_price: float,
        seconds_remaining: float,
    ) -> AISignalResult | None:
        """Call Bedrock Claude Haiku and parse the response."""
        if reference_price <= 0:
            return None

        pct_change = (current_price - reference_price) / reference_price * 100
        minutes = round((300 - seconds_remaining) / 60, 1)

        # Format headlines
        if headlines:
            headline_text = "\n".join(
                f"- {h.title} (source: {h.source}, {h.age_seconds:.0f}s ago)"
                for h in headlines[:10]  # Limit to 10 most recent
            )
        else:
            headline_text = "- No recent headlines available"

        user_message = USER_PROMPT_TEMPLATE.format(
            asset=asset,
            current_price=current_price,
            reference_price=reference_price,
            pct_change=pct_change,
            minutes=minutes,
            seconds_remaining=seconds_remaining,
            headlines=headline_text,
        )

        client = self._get_client()

        body = json.dumps(
            {
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 256,
                "system": SYSTEM_PROMPT,
                "messages": [{"role": "user", "content": user_message}],
                "temperature": 0.2,
            }
        )

        logger.debug("ai_signal_invoking", asset=asset, model=BEDROCK_MODEL_ID)

        response = client.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            contentType="application/json",
            accept="application/json",
            body=body,
        )

        response_body = json.loads(response["body"].read())
        text = response_body["content"][0]["text"]

        return self._parse_response(text, asset)

    def _parse_response(self, text: str, asset: str) -> AISignalResult | None:
        """Parse the JSON response from Claude Haiku."""
        try:
            # Handle potential markdown wrapping
            cleaned = text.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                # Remove first and last lines (```json and ```)
                cleaned = "\n".join(lines[1:-1]).strip()

            data = json.loads(cleaned)

            direction_str = data.get("direction", "").lower()
            if direction_str not in ("up", "down"):
                logger.warning(
                    "ai_signal_invalid_direction",
                    direction=direction_str,
                    asset=asset,
                )
                return None

            direction = Direction.UP if direction_str == "up" else Direction.DOWN
            confidence = float(data.get("confidence", 0.0))
            confidence = max(0.0, min(1.0, confidence))
            reasoning = str(data.get("reasoning", ""))

            result = AISignalResult(
                direction=direction,
                confidence=confidence,
                reasoning=reasoning,
                timestamp=time.time(),
                asset=asset,
            )

            logger.info(
                "ai_signal_result",
                asset=asset,
                direction=direction.value,
                confidence=round(confidence, 3),
                reasoning=reasoning[:100],
            )

            return result

        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
            logger.warning(
                "ai_signal_parse_failed",
                asset=asset,
                error=str(e),
                raw_text=text[:200],
            )
            return None
