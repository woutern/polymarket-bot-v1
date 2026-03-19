"""Stress test: concurrent signals for same window must result in exactly 1 trade."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polybot.execution.live_trader import LiveTrader
from polybot.models import Direction, Signal, SignalSource


def _make_signal(slug="eth-updown-5m-test-123", asset="ETH"):
    return Signal(
        source=SignalSource.DIRECTIONAL,
        direction=Direction.UP,
        model_prob=0.70,
        market_price=0.55,
        ev=0.10,
        window_slug=slug,
        asset=asset,
        p_bayesian=0.70,
    )


def _make_live_trader():
    from polybot.config import Settings

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
    settings.max_trade_usd = 5.0

    risk = MagicMock()
    risk.can_trade.return_value = True
    risk.bankroll = 200.0
    risk.get_bet_size = lambda lgbm_prob=0.5: 2.0

    db = MagicMock()
    db.insert_trade = AsyncMock()

    with patch("polybot.execution.live_trader.ClobClient"):
        trader = LiveTrader(settings=settings, risk=risk, db=db)

    # Mock the order execution to succeed
    trader.client.create_order = MagicMock(return_value={"signed": "order"})
    trader.client.post_order = MagicMock(return_value={
        "orderID": "0xtest123",
        "success": True,
    })

    return trader


class TestDedupConcurrent:
    async def test_ten_concurrent_same_slug_only_one_executes(self):
        """10 concurrent signals for same slug → exactly 1 trade, 9 blocked."""
        trader = _make_live_trader()
        slug = "eth-updown-5m-test-concurrent"

        results = []

        async def try_trade():
            sig = _make_signal(slug=slug)
            result = await trader.execute(sig, "yes_token", "no_token")
            return "executed" if result is not None else "blocked"

        tasks = [try_trade() for _ in range(10)]
        results = await asyncio.gather(*tasks)

        executed = sum(1 for r in results if r == "executed")
        blocked = sum(1 for r in results if r == "blocked")

        assert executed == 1, f"Expected 1 execution, got {executed}"
        assert blocked == 9, f"Expected 9 blocked, got {blocked}"

    async def test_different_slugs_all_execute(self):
        """10 signals with different slugs → all execute."""
        trader = _make_live_trader()

        results = []

        async def try_trade(i):
            sig = _make_signal(slug=f"btc-updown-5m-test-{i}")
            result = await trader.execute(sig, "yes_token", "no_token")
            return "executed" if result is not None else "blocked"

        tasks = [try_trade(i) for i in range(10)]
        results = await asyncio.gather(*tasks)

        executed = sum(1 for r in results if r == "executed")
        assert executed == 10, f"Expected 10 executions, got {executed}"

    async def test_slug_in_memory_after_trade(self):
        """After trade executes, slug is in _traded_slugs set."""
        trader = _make_live_trader()
        slug = "btc-updown-5m-test-memory"
        sig = _make_signal(slug=slug)

        await trader.execute(sig, "yes_token", "no_token")
        assert slug in trader._traded_slugs

    async def test_dynamo_dedup_blocks(self):
        """If DynamoDB has existing trade for slug, block new trade."""
        trader = _make_live_trader()
        slug = "btc-updown-5m-test-dynamo"

        # Mock DynamoDB returning existing trade
        mock_dynamo = MagicMock()
        mock_dynamo.get_trades_for_window.return_value = [{"id": "existing"}]
        mock_dynamo.claim_slug.return_value = True
        trader._dynamo = mock_dynamo

        sig = _make_signal(slug=slug)
        result = await trader.execute(sig, "yes_token", "no_token")

        assert result is None  # blocked by DynamoDB dedup
        assert slug in trader._traded_slugs  # cached for next time

    async def test_btc_5m_and_15m_same_slug_one_executes(self):
        """BTC_5m and BTC_15m fire on same slug → only 1 trade."""
        trader = _make_live_trader()
        slug = "btc-updown-5m-test-cross-pair"

        # Simulate two pair trackers hitting same slug concurrently
        async def try_5m():
            sig = _make_signal(slug=slug, asset="BTC")
            return await trader.execute(sig, "yes_token", "no_token")

        async def try_15m():
            sig = _make_signal(slug=slug, asset="BTC")
            return await trader.execute(sig, "yes_token", "no_token")

        results = await asyncio.gather(try_5m(), try_15m())
        executed = sum(1 for r in results if r is not None)
        assert executed == 1, f"Expected 1 execution from cross-pair, got {executed}"

    async def test_dynamo_claim_blocks_second_container(self):
        """Atomic DynamoDB claim prevents second container from trading."""
        trader = _make_live_trader()
        slug = "btc-updown-5m-test-claim"

        # Mock DynamoDB: no existing trades, but claim fails (already claimed by other container)
        mock_dynamo = MagicMock()
        mock_dynamo.get_trades_for_window.return_value = []
        mock_dynamo.claim_slug.return_value = False  # another container claimed it
        trader._dynamo = mock_dynamo

        sig = _make_signal(slug=slug)
        result = await trader.execute(sig, "yes_token", "no_token")

        assert result is None  # blocked by dynamo claim
        assert slug in trader._traded_slugs

    async def test_dynamo_claim_succeeds_allows_trade(self):
        """When claim succeeds, trade executes normally."""
        trader = _make_live_trader()
        slug = "btc-updown-5m-test-claim-ok"

        mock_dynamo = MagicMock()
        mock_dynamo.get_trades_for_window.return_value = []
        mock_dynamo.claim_slug.return_value = True
        trader._dynamo = mock_dynamo

        sig = _make_signal(slug=slug)
        result = await trader.execute(sig, "yes_token", "no_token")

        assert result is not None  # trade executed
        mock_dynamo.claim_slug.assert_called_once_with(slug)
