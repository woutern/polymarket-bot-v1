"""Tests for the AI signal generator."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock, patch

import pytest

from polybot.feeds.news_feed import Headline
from polybot.models import Direction
from polybot.strategy.ai_signal import (
    AISignalGenerator,
    AISignalResult,
    CACHE_TTL_SECONDS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_headline(title: str = "BTC surges past $100k", source: str = "cryptopanic") -> Headline:
    return Headline(
        title=title,
        source=source,
        url="https://example.com",
        timestamp=time.time(),
        currencies=["BTC"],
    )


def _make_bedrock_response(direction: str = "up", confidence: float = 0.8, reasoning: str = "bullish momentum") -> MagicMock:
    """Create a mock Bedrock invoke_model response."""
    payload = json.dumps({"direction": direction, "confidence": confidence, "reasoning": reasoning})
    response_body = json.dumps({
        "content": [{"type": "text", "text": payload}],
        "model": "claude-haiku",
        "stop_reason": "end_turn",
    })
    mock_body = MagicMock()
    mock_body.read.return_value = response_body.encode()
    return {"body": mock_body}


# ---------------------------------------------------------------------------
# Parse response tests
# ---------------------------------------------------------------------------

class TestParseResponse:
    def setup_method(self):
        self.gen = AISignalGenerator()

    def test_valid_up_response(self):
        text = '{"direction": "up", "confidence": 0.85, "reasoning": "bullish"}'
        result = self.gen._parse_response(text, "BTC")
        assert result is not None
        assert result.direction == Direction.UP
        assert result.confidence == 0.85
        assert result.reasoning == "bullish"
        assert result.asset == "BTC"

    def test_valid_down_response(self):
        text = '{"direction": "down", "confidence": 0.7, "reasoning": "bearish news"}'
        result = self.gen._parse_response(text, "ETH")
        assert result is not None
        assert result.direction == Direction.DOWN
        assert result.confidence == 0.7
        assert result.asset == "ETH"

    def test_markdown_wrapped_response(self):
        text = '```json\n{"direction": "up", "confidence": 0.6, "reasoning": "ok"}\n```'
        result = self.gen._parse_response(text, "BTC")
        assert result is not None
        assert result.direction == Direction.UP

    def test_invalid_json(self):
        result = self.gen._parse_response("not json at all", "BTC")
        assert result is None

    def test_invalid_direction(self):
        text = '{"direction": "sideways", "confidence": 0.5, "reasoning": "unsure"}'
        result = self.gen._parse_response(text, "BTC")
        assert result is None

    def test_confidence_clamped(self):
        text = '{"direction": "up", "confidence": 1.5, "reasoning": "very bullish"}'
        result = self.gen._parse_response(text, "BTC")
        assert result is not None
        assert result.confidence == 1.0

    def test_confidence_clamped_low(self):
        text = '{"direction": "down", "confidence": -0.3, "reasoning": "confused"}'
        result = self.gen._parse_response(text, "BTC")
        assert result is not None
        assert result.confidence == 0.0

    def test_missing_fields(self):
        text = '{"direction": "up"}'
        result = self.gen._parse_response(text, "BTC")
        assert result is not None
        assert result.confidence == 0.0
        assert result.reasoning == ""


# ---------------------------------------------------------------------------
# Caching tests
# ---------------------------------------------------------------------------

class TestCaching:
    def setup_method(self):
        self.gen = AISignalGenerator()

    @patch.object(AISignalGenerator, "_invoke")
    def test_cache_hit(self, mock_invoke):
        result = AISignalResult(
            direction=Direction.UP,
            confidence=0.8,
            reasoning="test",
            timestamp=time.time(),
            asset="BTC",
        )
        mock_invoke.return_value = result

        headlines = [_make_headline()]

        # First call — should invoke
        r1 = self.gen.get_signal("BTC", headlines, 65000, 64900, 45)
        assert r1 is not None
        assert mock_invoke.call_count == 1

        # Second call — should use cache
        r2 = self.gen.get_signal("BTC", headlines, 65000, 64900, 40)
        assert r2 is not None
        assert mock_invoke.call_count == 1  # No additional call

    @patch.object(AISignalGenerator, "_invoke")
    def test_cache_miss_after_ttl(self, mock_invoke):
        result = AISignalResult(
            direction=Direction.UP,
            confidence=0.8,
            reasoning="test",
            timestamp=time.time(),
            asset="BTC",
        )
        mock_invoke.return_value = result

        headlines = [_make_headline()]

        # First call
        self.gen.get_signal("BTC", headlines, 65000, 64900, 45)
        assert mock_invoke.call_count == 1

        # Expire the cache
        cache_key = "BTC"
        cached_result, _ = self.gen._cache[cache_key]
        self.gen._cache[cache_key] = (cached_result, time.time() - CACHE_TTL_SECONDS - 1)

        # Second call — should invoke again
        self.gen.get_signal("BTC", headlines, 65000, 64900, 40)
        assert mock_invoke.call_count == 2

    @patch.object(AISignalGenerator, "_invoke")
    def test_different_assets_not_cached(self, mock_invoke):
        result_btc = AISignalResult(
            direction=Direction.UP, confidence=0.8,
            reasoning="btc", timestamp=time.time(), asset="BTC",
        )
        result_eth = AISignalResult(
            direction=Direction.DOWN, confidence=0.7,
            reasoning="eth", timestamp=time.time(), asset="ETH",
        )
        mock_invoke.side_effect = [result_btc, result_eth]

        headlines = [_make_headline()]

        r1 = self.gen.get_signal("BTC", headlines, 65000, 64900, 45)
        r2 = self.gen.get_signal("ETH", headlines, 3000, 2990, 45)
        assert r1.direction == Direction.UP
        assert r2.direction == Direction.DOWN
        assert mock_invoke.call_count == 2


# ---------------------------------------------------------------------------
# Bedrock integration (mocked)
# ---------------------------------------------------------------------------

class TestBedrockInvocation:
    @patch("polybot.strategy.ai_signal.boto3")
    def test_successful_invocation(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.invoke_model.return_value = _make_bedrock_response(
            direction="up", confidence=0.85, reasoning="bullish"
        )

        gen = AISignalGenerator()
        gen._client = mock_client

        headlines = [_make_headline()]
        result = gen.get_signal("BTC", headlines, 65000, 64900, 45)

        assert result is not None
        assert result.direction == Direction.UP
        assert result.confidence == 0.85
        mock_client.invoke_model.assert_called_once()

        # Check the model ID in the call
        call_kwargs = mock_client.invoke_model.call_args
        assert call_kwargs.kwargs["modelId"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"

    @patch("polybot.strategy.ai_signal.boto3")
    def test_bedrock_error_returns_none(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.invoke_model.side_effect = Exception("Bedrock timeout")

        gen = AISignalGenerator()
        gen._client = mock_client

        headlines = [_make_headline()]
        result = gen.get_signal("BTC", headlines, 65000, 64900, 45)

        assert result is None

    def test_zero_reference_price_returns_none(self):
        gen = AISignalGenerator()
        result = gen._invoke(
            asset="BTC",
            headlines=[_make_headline()],
            current_price=65000,
            reference_price=0,
            seconds_remaining=45,
        )
        assert result is None


# ---------------------------------------------------------------------------
# News headline tests
# ---------------------------------------------------------------------------

class TestHeadlineParsing:
    def test_headline_age(self):
        h = Headline(
            title="Test",
            source="test",
            timestamp=time.time() - 60,
        )
        assert 59 <= h.age_seconds <= 61

    def test_headline_currencies(self):
        h = Headline(
            title="BTC hits new ATH",
            source="test",
            timestamp=time.time(),
            currencies=["BTC"],
        )
        assert "BTC" in h.currencies

    def test_empty_headlines_handled(self):
        gen = AISignalGenerator()
        # Should format "No recent headlines" when list is empty
        # We test the prompt construction indirectly via _invoke
        # by ensuring it doesn't crash with empty headlines
        with patch.object(gen, "_get_client") as mock_get:
            mock_client = MagicMock()
            mock_get.return_value = mock_client
            mock_client.invoke_model.return_value = _make_bedrock_response()

            gen._client = mock_client
            result = gen.get_signal("BTC", [], 65000, 64900, 45)
            assert result is not None
