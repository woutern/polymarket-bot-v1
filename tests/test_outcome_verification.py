"""Tests for outcome verification: Gamma API + DB + paper trader update."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polybot.execution.paper_trader import PaperTrader
from polybot.models import Direction, Signal, SignalSource


def _make_trader():
    risk = MagicMock()
    risk.can_trade.return_value = True
    risk.bankroll = 100.0
    risk.max_position_pct = 0.01
    risk.get_bet_size = lambda: 2.50
    risk.min_trade_usd = 1.0
    risk.max_trade_usd = 10.0
    db = MagicMock()
    db.insert_trade = AsyncMock()
    db.get_trades = AsyncMock()
    db.update_trade_outcome = AsyncMock()
    db.update_trade_verified = AsyncMock()
    return PaperTrader(risk=risk, db=db)


def _make_signal(slug="btc-5m-1000", market_price=0.60, direction=Direction.UP):
    return Signal(
        source=SignalSource.DIRECTIONAL,
        direction=direction,
        model_prob=0.80,
        market_price=market_price,
        ev=0.33,
        window_slug=slug,
        asset="BTC",
        p_bayesian=0.80,
        p_ai=None,
        pct_move=0.12,
        seconds_remaining=25.0,
        yes_ask=market_price,
        no_ask=0.41,
        yes_bid=0.58,
        no_bid=0.39,
        open_price=50000.0,
    )


# ---------------------------------------------------------------------------
# verify_and_update
# ---------------------------------------------------------------------------

class TestVerifyAndUpdate:
    async def test_yes_won_updates_correct_prediction_true(self):
        """When Polymarket says YES won and we bought YES, correct=True."""
        trader = _make_trader()
        trader.db.get_trades.return_value = [
            {"id": "abc1", "resolved": 0, "side": "YES", "fill_price": 0.50, "size_usd": 1.0, "pnl": 0.5},
        ]
        with patch(
            "polybot.execution.paper_trader.get_market_outcome",
            new=AsyncMock(return_value=("YES", "polymarket_verified")),
        ):
            await trader.verify_and_update("btc-5m-1000")

        trader.db.update_trade_verified.assert_awaited_once()
        call_kwargs = trader.db.update_trade_verified.call_args[1]
        assert call_kwargs["trade_id"] == "abc1"
        assert call_kwargs["polymarket_winner"] == "YES"
        assert call_kwargs["correct_prediction"] == True
        assert call_kwargs["pnl"] > 0  # win

    async def test_no_won_we_bought_yes_correct_false(self):
        """When Polymarket says NO won but we bought YES, correct=False."""
        trader = _make_trader()
        trader.db.get_trades.return_value = [
            {"id": "abc2", "resolved": 0, "side": "YES", "fill_price": 0.50, "size_usd": 1.0, "pnl": -1.0},
        ]
        with patch(
            "polybot.execution.paper_trader.get_market_outcome",
            new=AsyncMock(return_value=("NO", "polymarket_verified")),
        ):
            await trader.verify_and_update("btc-5m-1000")

        trader.db.update_trade_verified.assert_awaited_once()
        call_kwargs = trader.db.update_trade_verified.call_args[1]
        assert call_kwargs["trade_id"] == "abc2"
        assert call_kwargs["polymarket_winner"] == "NO"
        assert call_kwargs["correct_prediction"] == False
        assert call_kwargs["pnl"] < 0  # loss

    async def test_pending_outcome_skips_update(self):
        """If Gamma API returns None (pending), no DB update is made."""
        trader = _make_trader()
        trader.db.get_trades.return_value = [
            {"id": "abc3", "resolved": 1, "side": "YES"},
        ]
        with patch(
            "polybot.execution.paper_trader.get_market_outcome",
            new=AsyncMock(return_value=(None, "pending")),
        ):
            await trader.verify_and_update("btc-5m-1000")

        trader.db.update_trade_verified.assert_not_awaited()

    async def test_already_resolved_trade_not_reprocessed(self):
        """Already-resolved trades (resolved=1) are skipped — not re-verified."""
        trader = _make_trader()
        trader.db.get_trades.return_value = [
            {"id": "abc4", "resolved": 1, "side": "YES"},
        ]
        with patch(
            "polybot.execution.paper_trader.get_market_outcome",
            new=AsyncMock(return_value=("YES", "polymarket_verified")),
        ):
            await trader.verify_and_update("btc-5m-1000")

        trader.db.update_trade_verified.assert_not_awaited()

    async def test_api_exception_handled_silently(self):
        """If Gamma API raises, verify_and_update does not propagate the error."""
        trader = _make_trader()
        with patch(
            "polybot.execution.paper_trader.get_market_outcome",
            new=AsyncMock(side_effect=Exception("network error")),
        ):
            # Should not raise
            await trader.verify_and_update("btc-5m-1000")


# ---------------------------------------------------------------------------
# get_market_outcome (feeds/polymarket_rest.py)
# ---------------------------------------------------------------------------

class TestGetMarketOutcome:
    async def test_up_won(self):
        """Up price=1, Down price=0 → YES (Up) won."""
        from polybot.feeds.polymarket_rest import get_market_outcome
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"outcomes": '["Up", "Down"]', "outcomePrices": '["1", "0"]', "closed": True}]
        with patch("polybot.feeds.polymarket_rest.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = AsyncMock(return_value=mock_resp)
            winner, source = await get_market_outcome("btc-5m-1000")
        assert winner == "YES"
        assert source == "polymarket_verified"

    async def test_down_won(self):
        """Up price=0, Down price=1 → NO (Down) won."""
        from polybot.feeds.polymarket_rest import get_market_outcome
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"outcomes": '["Up", "Down"]', "outcomePrices": '["0", "1"]', "closed": True}]
        with patch("polybot.feeds.polymarket_rest.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = AsyncMock(return_value=mock_resp)
            winner, source = await get_market_outcome("btc-5m-1000")
        assert winner == "NO"
        assert source == "polymarket_verified"

    async def test_market_not_closed_returns_pending(self):
        """Not closed → pending regardless of prices."""
        from polybot.feeds.polymarket_rest import get_market_outcome
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"outcomes": '["Up", "Down"]', "outcomePrices": '["0.55", "0.45"]', "closed": False}]
        with patch("polybot.feeds.polymarket_rest.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = AsyncMock(return_value=mock_resp)
            winner, source = await get_market_outcome("btc-5m-1000")
        assert winner is None
        assert source == "pending"

    async def test_empty_response_returns_pending(self):
        """Empty markets list → pending."""
        from polybot.feeds.polymarket_rest import get_market_outcome
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = []
        with patch("polybot.feeds.polymarket_rest.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = AsyncMock(return_value=mock_resp)
            winner, source = await get_market_outcome("btc-5m-1000")
        assert winner is None
        assert source == "pending"

    async def test_http_error_returns_pending(self):
        """Non-200 status → pending."""
        from polybot.feeds.polymarket_rest import get_market_outcome
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        with patch("polybot.feeds.polymarket_rest.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = AsyncMock(return_value=mock_resp)
            winner, source = await get_market_outcome("btc-5m-1000")
        assert winner is None
        assert source == "pending"

    async def test_ambiguous_prices_returns_pending(self):
        """Closed but prices not conclusive (0.7/0.3) → pending (don't guess)."""
        from polybot.feeds.polymarket_rest import get_market_outcome
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"outcomes": '["Up", "Down"]', "outcomePrices": '["0.7", "0.3"]', "closed": True}]
        with patch("polybot.feeds.polymarket_rest.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=mock_client.return_value)
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.return_value.get = AsyncMock(return_value=mock_resp)
            winner, source = await get_market_outcome("btc-5m-1000")
        assert winner is None  # don't guess — wait for conclusive 0 or 1
        assert source == "pending"


# ---------------------------------------------------------------------------
# New Signal fields propagate through paper trader to DB
# ---------------------------------------------------------------------------

class TestSignalMetadataSaved:
    async def test_signal_metadata_written_to_db(self):
        """All new Signal fields (p_bayesian, p_ai, pct_move, etc.) are saved to DB."""
        trader = _make_trader()
        sig = _make_signal()
        sig.p_bayesian = 0.78
        sig.p_ai = 0.82
        sig.pct_move = 0.15
        sig.seconds_remaining = 22.0
        sig.ev = 0.33

        result = await trader.execute(sig)
        assert result is not None

        call_kwargs = trader.db.insert_trade.call_args[0][0]
        assert call_kwargs["p_bayesian"] == 0.78
        assert call_kwargs["p_ai"] == 0.82
        assert call_kwargs["p_final"] == sig.model_prob
        assert call_kwargs["pct_move"] == 0.15
        assert call_kwargs["seconds_remaining"] == 22.0
        assert call_kwargs["ev"] == 0.33
        assert call_kwargs["outcome_source"] == "coinbase_inferred"

    async def test_no_ai_p_ai_is_none(self):
        """When AI is not used, p_ai saved as None."""
        trader = _make_trader()
        sig = _make_signal()
        sig.p_ai = None
        result = await trader.execute(sig)
        assert result is not None
        call_kwargs = trader.db.insert_trade.call_args[0][0]
        assert call_kwargs["p_ai"] is None
