"""Tests for LiveTrader order placement — catches CLOB API edge cases."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polybot.models import Direction, Signal, SignalSource


def _make_signal(
    direction=Direction.UP,
    market_price=0.55,
    slug="btc-updown-5m-1000",
    asset="BTC",
):
    return Signal(
        source=SignalSource.DIRECTIONAL,
        direction=direction,
        model_prob=0.80,
        market_price=market_price,
        ev=0.45,
        window_slug=slug,
        asset=asset,
        p_bayesian=0.78,
        p_ai=0.82,
        # model_prob IS the final blended probability
        pct_move=0.12,
        seconds_remaining=25.0,
        yes_ask=market_price,
        no_ask=0.46,
        yes_bid=0.53,
        no_bid=0.44,
        open_price=50000.0,
    )


def _make_live_trader():
    """Create a LiveTrader with mocked CLOB client."""
    from polybot.execution.live_trader import LiveTrader

    settings = MagicMock()
    settings.polymarket_api_key = "test"
    settings.polymarket_api_secret = "test"
    settings.polymarket_api_passphrase = "test"
    settings.polymarket_private_key = "0x" + "a" * 64
    settings.polymarket_chain_id = 137
    settings.polymarket_funder = None
    settings.kelly_fraction = 0.25
    settings.max_position_pct = 0.01
    settings.min_trade_usd = 1.0
    settings.max_trade_usd = 1.0

    risk = MagicMock()
    risk.can_trade.return_value = True
    risk.bankroll = 43.0
    risk.max_position_pct = 0.01

    db = MagicMock()
    db.insert_trade = AsyncMock()

    with patch("polybot.execution.live_trader.ClobClient") as MockClient:
        trader = LiveTrader(settings=settings, risk=risk, db=db)

    return trader


class TestLiveTraderOrderCreation:
    async def test_uses_create_order_not_market_order(self):
        """create_order with tick_size='0.01' is used instead of create_market_order.

        This is critical: create_market_order calls get_tick_size which returns
        404 for short-lived 5/15-min markets, silently blocking all live trades.
        """
        trader = _make_live_trader()
        sig = _make_signal(market_price=0.55)

        # Mock create_order (not create_market_order)
        trader.client.create_order = MagicMock(return_value={"signed": "order"})
        trader.client.post_order = MagicMock(return_value={
            "orderID": "0xabc123",
            "success": True,
            "status": "matched",
        })

        result = await trader.execute(sig, "yes_token_123", "no_token_456")

        # create_order was called (not create_market_order)
        trader.client.create_order.assert_called_once()
        # Verify tick_size='0.01' was passed via options
        call_args = trader.client.create_order.call_args
        options = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("options")
        assert options.tick_size == "0.01"

    async def test_create_market_order_not_used(self):
        """create_market_order should never be called (it fails for 5-min markets)."""
        trader = _make_live_trader()
        sig = _make_signal(market_price=0.55)

        trader.client.create_order = MagicMock(return_value={"signed": "order"})
        trader.client.post_order = MagicMock(return_value={
            "orderID": "0xabc123",
            "success": True,
        })

        await trader.execute(sig, "yes_token_123", "no_token_456")
        # create_market_order should NOT have been called
        trader.client.create_market_order.assert_not_called()

    async def test_order_not_matched_returns_none(self):
        """If FOK order doesn't match, return None (no phantom trade recorded)."""
        trader = _make_live_trader()
        sig = _make_signal(market_price=0.55)

        trader.client.create_order = MagicMock(return_value={"signed": "order"})
        trader.client.post_order = MagicMock(return_value={
            "orderID": "",
            "success": False,
            "errorMsg": "no match",
        })

        result = await trader.execute(sig, "yes_token_123", "no_token_456")
        assert result is None
        # No trade should be recorded in DB
        trader.db.insert_trade.assert_not_awaited()

    async def test_exception_returns_none(self):
        """CLOB errors don't crash the bot."""
        trader = _make_live_trader()
        sig = _make_signal(market_price=0.55)

        trader.client.create_order = MagicMock(side_effect=Exception("network error"))

        result = await trader.execute(sig, "yes_token_123", "no_token_456")
        assert result is None

    async def test_up_signal_uses_yes_token(self):
        """UP direction buys YES token."""
        trader = _make_live_trader()
        sig = _make_signal(direction=Direction.UP, market_price=0.55)

        trader.client.create_order = MagicMock(return_value={"signed": "order"})
        trader.client.post_order = MagicMock(return_value={
            "orderID": "0x1",
            "success": True,
        })

        await trader.execute(sig, "yes_token_ABC", "no_token_DEF")
        order_args = trader.client.create_order.call_args[0][0]
        assert order_args.token_id == "yes_token_ABC"

    async def test_down_signal_uses_no_token(self):
        """DOWN direction buys NO token."""
        trader = _make_live_trader()
        sig = _make_signal(direction=Direction.DOWN, market_price=0.55)

        trader.client.create_order = MagicMock(return_value={"signed": "order"})
        trader.client.post_order = MagicMock(return_value={
            "orderID": "0x2",
            "success": True,
        })

        await trader.execute(sig, "yes_token_ABC", "no_token_DEF")
        order_args = trader.client.create_order.call_args[0][0]
        assert order_args.token_id == "no_token_DEF"

    async def test_signal_metadata_saved_to_db(self):
        """p_bayesian, p_ai, pct_move etc. are saved with the trade."""
        trader = _make_live_trader()
        sig = _make_signal(market_price=0.50)
        sig.p_bayesian = 0.78
        sig.p_ai = 0.82
        sig.pct_move = 0.15
        sig.ev = 0.60

        trader.client.create_order = MagicMock(return_value={"signed": "order"})
        trader.client.post_order = MagicMock(return_value={
            "orderID": "0xmeta",
            "success": True,
        })

        await trader.execute(sig, "yes_t", "no_t")
        call_kwargs = trader.db.insert_trade.call_args[0][0]
        assert call_kwargs["p_bayesian"] == 0.78
        assert call_kwargs["p_ai"] == 0.82
        assert call_kwargs["ev"] == 0.60
        assert call_kwargs["mode"] == "live"
        assert call_kwargs["asset"] == "BTC"

    async def test_price_zero_returns_none(self):
        """market_price=0 should not attempt an order."""
        trader = _make_live_trader()
        sig = _make_signal(market_price=0.0)

        result = await trader.execute(sig, "yes_t", "no_t")
        assert result is None

    async def test_shares_at_least_one(self):
        """Even if dollar amount / price < 1, buy at least 1 share."""
        trader = _make_live_trader()
        sig = _make_signal(market_price=0.50)  # $1 / 0.50 = 2 shares

        trader.client.create_order = MagicMock(return_value={"signed": "order"})
        trader.client.post_order = MagicMock(return_value={
            "orderID": "0xmin",
            "success": True,
        })

        await trader.execute(sig, "yes_t", "no_t")
        order_args = trader.client.create_order.call_args[0][0]
        assert order_args.size >= 1.0
