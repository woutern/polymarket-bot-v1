"""Tests for Gamma API resolution logic."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestOutcomeMapping:
    async def test_up_wins_when_up_price_one(self):
        from polybot.feeds.polymarket_rest import get_market_outcome
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"outcomes": '["Up", "Down"]', "outcomePrices": '["1", "0"]', "closed": True}]
        with patch("polybot.feeds.polymarket_rest.httpx.AsyncClient") as mc:
            mc.return_value.__aenter__ = AsyncMock(return_value=mc.return_value)
            mc.return_value.__aexit__ = AsyncMock(return_value=False)
            mc.return_value.get = AsyncMock(return_value=mock_resp)
            winner, src = await get_market_outcome("test")
        assert winner == "YES"
        assert src == "polymarket_verified"

    async def test_down_wins_when_up_price_zero(self):
        from polybot.feeds.polymarket_rest import get_market_outcome
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"outcomes": '["Up", "Down"]', "outcomePrices": '["0", "1"]', "closed": True}]
        with patch("polybot.feeds.polymarket_rest.httpx.AsyncClient") as mc:
            mc.return_value.__aenter__ = AsyncMock(return_value=mc.return_value)
            mc.return_value.__aexit__ = AsyncMock(return_value=False)
            mc.return_value.get = AsyncMock(return_value=mock_resp)
            winner, src = await get_market_outcome("test")
        assert winner == "NO"

    async def test_ambiguous_returns_pending(self):
        from polybot.feeds.polymarket_rest import get_market_outcome
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"outcomes": '["Up", "Down"]', "outcomePrices": '["0.6", "0.4"]', "closed": True}]
        with patch("polybot.feeds.polymarket_rest.httpx.AsyncClient") as mc:
            mc.return_value.__aenter__ = AsyncMock(return_value=mc.return_value)
            mc.return_value.__aexit__ = AsyncMock(return_value=False)
            mc.return_value.get = AsyncMock(return_value=mock_resp)
            winner, src = await get_market_outcome("test")
        assert winner is None
        assert src == "pending"

    async def test_not_closed_returns_pending(self):
        from polybot.feeds.polymarket_rest import get_market_outcome
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"outcomes": '["Up", "Down"]', "outcomePrices": '["1", "0"]', "closed": False}]
        with patch("polybot.feeds.polymarket_rest.httpx.AsyncClient") as mc:
            mc.return_value.__aenter__ = AsyncMock(return_value=mc.return_value)
            mc.return_value.__aexit__ = AsyncMock(return_value=False)
            mc.return_value.get = AsyncMock(return_value=mock_resp)
            winner, src = await get_market_outcome("test")
        assert winner is None

    async def test_missing_outcomes_returns_pending(self):
        from polybot.feeds.polymarket_rest import get_market_outcome
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"closed": True}]
        with patch("polybot.feeds.polymarket_rest.httpx.AsyncClient") as mc:
            mc.return_value.__aenter__ = AsyncMock(return_value=mc.return_value)
            mc.return_value.__aexit__ = AsyncMock(return_value=False)
            mc.return_value.get = AsyncMock(return_value=mock_resp)
            winner, src = await get_market_outcome("test")
        assert winner is None
