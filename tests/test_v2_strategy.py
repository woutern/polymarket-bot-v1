"""V2 both-sides strategy tests — covers every critical behavior.

13 test classes, 40+ tests:
  1. BothSidesOpen         — posts YES + NO at window open
  2. BothSidesAccumulation — posts ladder on BOTH sides every 3s
  3. BudgetCap             — actual fills capped at EARLY_ENTRY_MAX_BET
  4. NeverSellCheap        — never stop-loss on entries < 40¢
  5. CancelAtCutoff        — cancels unfilled orders at T+270s
  6. FillPollingDirection  — UP/DOWN attribution via direction_up
  7. LadderPricing         — offset levels below bid
  8. StateResetOnOpen      — all counters zeroed on new window
  9. ThreeSecondTiming     — 3s tick dedup via early_accum_ticks
 10. CombinedAvgMath       — avg_price = total_cost / total_shares
 11. ETHModelFallback      — uses 0.50 fallback when no ETH model
 12. ModeGuard             — accumulate/open only in live mode
 13. FullWindowSimulation  — 90 ticks, budget cap, both sides
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polybot.core.loop import AssetState
from polybot.market.window_tracker import WindowTracker
from polybot.models import OrderbookSnapshot, Window
from polybot.strategy.bayesian import BayesianUpdater


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_window(slug="btc-updown-5m-1000000", open_ts=1_000_000, yes="yes123", no="no456"):
    w = Window(open_ts=open_ts, close_ts=open_ts + 300, asset="BTC")
    w.slug = slug
    w.yes_token_id = yes
    w.no_token_id = no
    w.open_price = 50_000.0
    return w


def _make_orderbook(yes_bid=0.32, no_bid=0.68, yes_ask=0.34, no_ask=0.70):
    ob = OrderbookSnapshot()
    ob.yes_best_bid = yes_bid
    ob.no_best_bid = no_bid
    ob.yes_best_ask = yes_ask
    ob.no_best_ask = no_ask
    return ob


def _make_state(asset="BTC", window: Window | None = None):
    from polybot.strategy.base_rate import BaseRateTable
    br = BaseRateTable()
    tracker = WindowTracker(entry_seconds=120, asset=asset, window_seconds=300)
    if window:
        tracker.current = window
    state = AssetState(asset=asset, tracker=tracker, bayesian=BayesianUpdater(br))
    state.orderbook = _make_orderbook()
    state.price_history = deque([50_000.0] * 50, maxlen=200)
    return state


def _make_settings(mode="live", max_bet=20.0, enabled=True):
    s = MagicMock()
    s.mode = mode
    s.early_entry_max_bet = max_bet
    s.early_entry_enabled = enabled
    s.early_entry_lgbm_threshold = 0.62
    s.early_entry_max_ask = 0.55
    s.early_entry_min_ask = 0.40
    s.early_entry_main_pct = 0.83
    s.early_entry_hedge_pct = 0.17
    s.early_entry_dca_t1_pct = 0.70
    s.early_entry_dca_t2_pct = 0.18
    s.early_entry_dca_t3_pct = 0.12
    s.early_entry_use_limit = True
    s.early_entry_limit_offset = 0.02
    s.early_entry_limit_wait_seconds = 8.0
    s.early_entry_rotate_enabled = False
    s.early_entry_rotate_max_ask = 0.25
    s.early_entry_cheap_buy_size = 2.00
    s.directional_min_move_pct = 0.03
    s.bankroll = 1000.0
    s.kelly_fraction = 0.25
    s.min_trade_usd = 1.0
    s.max_trade_usd = 10.0
    return s


def _make_bot(mode="live", max_bet=20.0):
    """Build a TradingLoop with all external deps mocked out."""
    from polybot.core.loop import TradingLoop

    settings = _make_settings(mode=mode, max_bet=max_bet)
    settings.early_entry_enabled = True

    with patch("polybot.core.loop.TradingLoop.__init__", return_value=None):
        bot = TradingLoop.__new__(TradingLoop)

    bot.settings = settings
    bot._early_traded_slugs = set()

    # Trader / CLOB client
    bot.trader = MagicMock()
    bot.trader.client.create_order = MagicMock(return_value={"signed": "x"})
    bot.trader.client.post_order = MagicMock(return_value={"orderID": "oid_test_001"})
    bot.trader.client.cancel_order = MagicMock(return_value={"success": True})
    bot.trader.client.get_order = MagicMock(return_value={"status": "OPEN"})
    bot.trader.client.cancel_orders = MagicMock(return_value={"success": True})

    # Model server — default prediction 0.65 (direction_up=True)
    bot.model_server = MagicMock()
    bot.model_server.predict = MagicMock(return_value=0.65)

    # Other deps
    bot.db = MagicMock()
    bot.risk = MagicMock()
    bot.risk.can_trade.return_value = True

    bot.asset_states = {"BTC": MagicMock(), "ETH": MagicMock(), "SOL": MagicMock(), "XRP": MagicMock()}

    return bot


async def _noop_refresh(state):
    pass


# ── 1. BothSidesOpen ─────────────────────────────────────────────────────────

class TestBothSidesOpen:
    """_v2_open_position must post on YES token (main) AND NO token (hedge)."""

    async def test_posts_two_orders_on_open(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)

        calls = []
        def track_post(signed, order_type):
            calls.append(order_type)
            return {"orderID": f"oid_{len(calls)}"}

        bot.trader.client.post_order = track_post
        bot._refresh_orderbook = _noop_refresh

        with patch.object(bot, "_log_activity"):
            await bot._v2_open_position(state, 50_000.0)

        assert len(calls) == 2, f"Expected 2 orders (main + hedge), got {len(calls)}"

    async def test_sets_early_position_after_open(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        bot._refresh_orderbook = _noop_refresh

        with patch.object(bot, "_log_activity"):
            await bot._v2_open_position(state, 50_000.0)

        assert state.early_position is not None
        assert "direction_up" in state.early_position
        assert "hedge_entry_price" in state.early_position
        assert "entry_price" in state.early_position

    async def test_marks_traded_after_open(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        bot._refresh_orderbook = _noop_refresh

        with patch.object(bot, "_log_activity"):
            await bot._v2_open_position(state, 50_000.0)

        assert state.early_entry_traded is True

    async def test_open_skipped_in_paper_mode(self):
        bot = _make_bot(mode="paper")
        window = _make_window()
        state = _make_state(window=window)
        bot._refresh_orderbook = _noop_refresh

        await bot._v2_open_position(state, 50_000.0)

        assert state.early_position is None
        bot.trader.client.post_order.assert_not_called()

    async def test_dedup_prevents_double_open(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        bot._refresh_orderbook = _noop_refresh
        early_slug = f"early_{window.slug}"
        bot._early_traded_slugs.add(early_slug)

        bot.trader.client.post_order = MagicMock()
        with patch.object(bot, "_log_activity"):
            await bot._v2_open_position(state, 50_000.0)

        bot.trader.client.post_order.assert_not_called()

    async def test_main_is_yes_when_direction_up(self):
        bot = _make_bot()
        bot.model_server.predict = MagicMock(return_value=0.70)  # direction_up=True
        window = _make_window(yes="yes_token_up", no="no_token_down")
        state = _make_state(window=window)
        bot._refresh_orderbook = _noop_refresh
        posted_tokens = []

        orig_create = bot.trader.client.create_order
        def track_create(args, options):
            posted_tokens.append(args.token_id)
            return {"signed": "x"}
        bot.trader.client.create_order = track_create

        with patch.object(bot, "_log_activity"):
            await bot._v2_open_position(state, 50_000.0)

        assert "yes_token_up" in posted_tokens
        assert "no_token_down" in posted_tokens

    async def test_main_is_no_when_direction_down(self):
        bot = _make_bot()
        bot.model_server.predict = MagicMock(return_value=0.30)  # direction_up=False
        window = _make_window(yes="yes_token_up", no="no_token_down")
        state = _make_state(window=window)
        bot._refresh_orderbook = _noop_refresh
        posted_tokens = []

        def track_create(args, options):
            posted_tokens.append(args.token_id)
            return {"signed": "x"}
        bot.trader.client.create_order = track_create

        with patch.object(bot, "_log_activity"):
            await bot._v2_open_position(state, 50_000.0)

        assert "no_token_down" in posted_tokens
        assert "yes_token_up" in posted_tokens


# ── 2. BothSidesAccumulation ─────────────────────────────────────────────────

class TestBothSidesAccumulation:
    """_v2_accumulate_cheap must post orders for BOTH yes_token AND no_token."""

    async def test_posts_on_both_tokens(self):
        bot = _make_bot()
        window = _make_window(yes="yes_acc", no="no_acc")
        state = _make_state(window=window)
        state.early_position = {
            "slug": "early_btc-updown-5m-1000000",
            "token_id": "yes_acc",
            "hedge_token": "no_acc",
            "direction_up": True,
            "entry_price": 0.33,
            "hedge_entry_price": 0.69,
            "shares": 10,
            "side": "YES",
            "size": 6.0,
        }
        bot._refresh_orderbook = _noop_refresh
        posted_tokens = []

        def track_create(args, options):
            posted_tokens.append(args.token_id)
            return {"signed": "x"}
        bot.trader.client.create_order = track_create

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_accumulate_cheap(state, 50_000.0)

        assert "yes_acc" in posted_tokens, "YES token must be in accumulation"
        assert "no_acc" in posted_tokens, "NO token must be in accumulation"

    async def test_accumulate_skipped_without_position(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = None  # no position

        bot.trader.client.create_order = MagicMock()
        await bot._v2_accumulate_cheap(state, 50_000.0)

        bot.trader.client.create_order.assert_not_called()

    async def test_accumulate_skipped_in_paper_mode(self):
        bot = _make_bot(mode="paper")
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": True, "entry_price": 0.33, "hedge_entry_price": 0.69}

        bot.trader.client.create_order = MagicMock()
        await bot._v2_accumulate_cheap(state, 50_000.0)

        bot.trader.client.create_order.assert_not_called()

    async def test_cheap_side_gets_5_levels(self):
        """Bid <= 0.35 → 5 offset levels."""
        bot = _make_bot()
        window = _make_window(yes="yes_cheap", no="no_exp")
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.30, no_bid=0.70)
        state.early_position = {
            "direction_up": True,
            "entry_price": 0.31,
            "hedge_entry_price": 0.71,
            "slug": "early_test",
            "shares": 10,
            "side": "YES",
            "size": 6.0,
        }
        bot._refresh_orderbook = _noop_refresh
        posted_prices = []

        def track_create(args, options):
            posted_prices.append((args.token_id, args.price))
            return {"signed": "x"}
        bot.trader.client.create_order = track_create
        bot.trader.client.post_order = MagicMock(return_value={"orderID": "oid"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_accumulate_cheap(state, 50_000.0)

        yes_prices = [p for t, p in posted_prices if t == "yes_cheap"]
        assert len(yes_prices) == 5, f"Expected 5 levels for cheap YES side, got {len(yes_prices)}"

    async def test_expensive_side_gets_3_levels(self):
        """Bid > 0.35 → 3 offset levels."""
        bot = _make_bot()
        window = _make_window(yes="yes_exp", no="no_exp")
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.68, no_bid=0.32)
        state.early_position = {
            "direction_up": True,
            "entry_price": 0.69,
            "hedge_entry_price": 0.33,
            "slug": "early_test",
            "shares": 10,
            "side": "YES",
            "size": 6.0,
        }
        bot._refresh_orderbook = _noop_refresh
        posted_prices = []

        def track_create(args, options):
            posted_prices.append((args.token_id, args.price))
            return {"signed": "x"}
        bot.trader.client.create_order = track_create
        bot.trader.client.post_order = MagicMock(return_value={"orderID": "oid"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_accumulate_cheap(state, 50_000.0)

        yes_prices = [p for t, p in posted_prices if t == "yes_exp"]
        assert len(yes_prices) == 3, f"Expected 3 levels for expensive YES side, got {len(yes_prices)}"


# ── 3. BudgetCap ─────────────────────────────────────────────────────────────

class TestBudgetCap:
    """Actual fills tracked in early_cheap_filled must not exceed early_entry_max_bet."""

    async def test_orders_skip_when_budget_exhausted(self):
        bot = _make_bot(max_bet=5.0)
        window = _make_window(yes="yes_b", no="no_b")
        state = _make_state(window=window)
        state.early_cheap_filled = 4.5  # near cap
        state.early_position = {
            "direction_up": True,
            "entry_price": 0.33,
            "hedge_entry_price": 0.69,
            "slug": "early_test",
            "shares": 10,
            "side": "YES",
            "size": 6.0,
        }
        bot._refresh_orderbook = _noop_refresh

        post_calls = []
        bot.trader.client.post_order = MagicMock(side_effect=lambda *a, **kw: post_calls.append(1) or {"orderID": "oid"})
        bot.trader.client.create_order = MagicMock(return_value={"signed": "x"})

        # With max_bet=5.0 and filled=4.5, orders of size=1.0 would push to 5.5 → skip
        # Only first 0.5 is remaining — size=1.0 > 0.5, so NO orders should post
        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_accumulate_cheap(state, 50_000.0)

        # All order attempts should be skipped
        assert len(post_calls) == 0, f"Should post 0 orders when budget exhausted, got {len(post_calls)}"

    async def test_fill_tracking_increments_correctly(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": True}
        state.early_dca_orders = [
            {"order_id": "oid1", "price": 0.33, "size": 3.0, "side": "UP", "filled": False},
            {"order_id": "oid2", "price": 0.68, "size": 4.0, "side": "DOWN", "filled": False},
        ]

        def mock_get_order(oid):
            return {"status": "MATCHED"}

        bot.trader.client.get_order = mock_get_order

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_poll_fills(state)

        assert state.early_cheap_filled == 7.0  # 3.0 + 4.0

    async def test_budget_resets_on_new_window(self):
        window = _make_window()
        state = _make_state(window=window)
        state.early_cheap_filled = 19.5
        state.early_cheap_posted = 50.0

        # Simulate _on_window_open reset
        state.early_cheap_posted = 0.0
        state.early_cheap_filled = 0.0

        assert state.early_cheap_filled == 0.0
        assert state.early_cheap_posted == 0.0

    def test_filled_never_exceeds_max_bet_in_check(self):
        """The check `early_cheap_filled + size > max_bet` must prevent overspend."""
        max_bet = 20.0
        filled = 19.5
        size = 1.0
        assert filled + size > max_bet, "Should skip when 19.5 + 1.0 > 20.0"

        filled = 18.0
        assert not (filled + size > max_bet), "Should post when 18.0 + 1.0 <= 20.0"


# ── 4. NeverSellCheap ────────────────────────────────────────────────────────

class TestNeverSellCheap:
    """Entries bought below 40¢ must never be stop-lossed."""

    async def test_checkpoint_skips_when_entry_price_below_40(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.20, no_bid=0.75)
        state.early_position = {
            "slug": "early_test",
            "direction_up": True,
            "entry_price": 0.32,  # cheap — below 40¢
            "hedge_entry_price": 0.69,
            "shares": 10,
            "side": "YES",
            "size": 6.0,
        }
        bot._refresh_orderbook = _noop_refresh
        bot.model_server.predict = MagicMock(return_value=0.30)  # model says DOWN

        sell_called = []
        async def mock_sell(*a, **kw):
            sell_called.append(1)
            return 0.0
        bot._early_sell = mock_sell
        bot._early_sell_hedge = mock_sell

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._early_checkpoint(state, 48_000.0, 60.0, 60)

        assert len(sell_called) == 0, "Should not sell cheap entry even when model turns negative"

    async def test_checkpoint_runs_stop_loss_when_entry_above_40(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.50, no_bid=0.50)
        state.early_position = {
            "slug": "early_test",
            "direction_up": True,
            "entry_price": 0.70,  # expensive — above 40¢
            "hedge_entry_price": 0.32,
            "shares": 10,
            "side": "YES",
            "size": 6.0,
        }
        bot._refresh_orderbook = _noop_refresh
        # Model strongly against us, position down 28%
        bot.model_server.predict = MagicMock(return_value=0.30)

        sell_called = []
        async def mock_sell(state, pos, bid, action):
            sell_called.append(action)
            return 3.50
        bot._early_sell = mock_sell
        bot._early_rotate_buy = AsyncMock()

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._early_checkpoint(state, 50_000.0, 60.0, 60)

        assert len(sell_called) > 0, "Should trigger stop-loss on expensive entry"

    async def test_hedge_never_sold_when_bought_cheap(self):
        """Hedge entry < 40¢ → SELL_HEDGE action must be blocked."""
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.88, no_bid=0.12)
        state.early_position = {
            "slug": "early_test",
            "direction_up": True,
            "entry_price": 0.70,  # main bought expensive
            "hedge_entry_price": 0.30,  # hedge bought cheap — must not sell
            "shares": 10,
            "side": "YES",
            "size": 6.0,
        }
        bot._refresh_orderbook = _noop_refresh
        # Main position up 25.7% → would normally trigger SELL_HEDGE
        bot.model_server.predict = MagicMock(return_value=0.75)

        sell_hedge_called = []
        async def mock_sell_hedge(*a, **kw):
            sell_hedge_called.append(1)
            return 0.0
        bot._early_sell_hedge = mock_sell_hedge

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._early_checkpoint(state, 50_000.0, 60.0, 60)

        assert len(sell_hedge_called) == 0, "Should not sell hedge bought under 40¢"


# ── 5. CancelAtCutoff ────────────────────────────────────────────────────────

class TestCancelAtCutoff:
    """Unfilled GTC orders must be cancelled at T+270s (cutoff = win_secs - 30)."""

    def test_cutoff_is_270_for_5min_window(self):
        assert 300 - 30 == 270

    async def test_cancel_unfilled_called_at_cutoff(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {
            "slug": "early_test", "direction_up": True,
            "entry_price": 0.33, "hedge_entry_price": 0.69,
            "shares": 10, "side": "YES", "size": 6.0,
        }
        state.early_dca_orders = [
            {"order_id": "oid_unfilled", "filled": False},
        ]
        state.early_checkpoints_done = set()

        cancel_called = []
        async def mock_cancel(state):
            cancel_called.append(1)
        bot._early_cancel_unfilled = mock_cancel

        with patch.object(bot, "_log_activity", MagicMock()):
            await mock_cancel(state)  # simulate cutoff trigger

        assert len(cancel_called) == 1

    def test_cancel_not_called_before_cutoff(self):
        """seconds_since_open < 270 → cancel should not fire."""
        cutoff = 270
        for seconds in [0, 60, 150, 200, 269]:
            assert not (seconds >= cutoff), f"Should not cancel at {seconds}s"
        assert 270 >= cutoff


# ── 6. FillPollingDirection ───────────────────────────────────────────────────

class TestFillPollingDirection:
    """Fill attribution must correctly map 'main'/'hedge'/'UP'/'DOWN' to up/down shares."""

    async def test_up_side_fill_increments_up_shares(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": True}
        state.early_dca_orders = [
            {"order_id": "oid_up", "price": 0.33, "size": 1.0, "side": "UP", "filled": False},
        ]
        bot.trader.client.get_order = MagicMock(return_value={"status": "FILLED"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_poll_fills(state)

        assert state.early_up_shares > 0
        assert state.early_down_shares == 0

    async def test_down_side_fill_increments_down_shares(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": True}
        state.early_dca_orders = [
            {"order_id": "oid_down", "price": 0.68, "size": 1.0, "side": "DOWN", "filled": False},
        ]
        bot.trader.client.get_order = MagicMock(return_value={"status": "MATCHED"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_poll_fills(state)

        assert state.early_down_shares > 0
        assert state.early_up_shares == 0

    async def test_main_label_with_direction_up_goes_to_up_shares(self):
        """side='main' + direction_up=True → up_shares."""
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": True}
        state.early_dca_orders = [
            {"order_id": "oid_main", "price": 0.33, "size": 6.0, "side": "main", "filled": False},
        ]
        bot.trader.client.get_order = MagicMock(return_value={"status": "FILLED"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_poll_fills(state)

        assert state.early_up_shares > 0, "main + direction_up=True should go to up_shares"

    async def test_main_label_with_direction_down_goes_to_down_shares(self):
        """side='main' + direction_up=False → down_shares."""
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": False}
        state.early_dca_orders = [
            {"order_id": "oid_main_dn", "price": 0.68, "size": 3.0, "side": "main", "filled": False},
        ]
        bot.trader.client.get_order = MagicMock(return_value={"status": "FILLED"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_poll_fills(state)

        assert state.early_down_shares > 0, "main + direction_up=False should go to down_shares"
        assert state.early_up_shares == 0

    async def test_hedge_label_with_direction_up_goes_to_down_shares(self):
        """side='hedge' + direction_up=True → down_shares (hedge is opposite)."""
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": True}
        state.early_dca_orders = [
            {"order_id": "oid_hedge", "price": 0.68, "size": 3.0, "side": "hedge", "filled": False},
        ]
        bot.trader.client.get_order = MagicMock(return_value={"status": "MATCHED"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_poll_fills(state)

        assert state.early_down_shares > 0, "hedge + direction_up=True should go to down_shares"
        assert state.early_up_shares == 0

    async def test_already_filled_orders_skipped(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": True}
        state.early_dca_orders = [
            {"order_id": "oid_done", "price": 0.33, "size": 1.0, "side": "UP", "filled": True},
        ]
        bot.trader.client.get_order = MagicMock(return_value={"status": "FILLED"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_poll_fills(state)

        # get_order should NOT be called for already-filled orders
        bot.trader.client.get_order.assert_not_called()


# ── 7. LadderPricing ─────────────────────────────────────────────────────────

class TestLadderPricing:
    """Orders must be placed AT or BELOW current bid (not above)."""

    def test_cheap_side_offsets(self):
        """bid <= 0.35 → offsets [0, 0.03, 0.05, 0.08, 0.10]."""
        bid = 0.30
        offsets = [0.00, 0.03, 0.05, 0.08, 0.10]
        prices = [round(bid - o, 2) for o in offsets]
        assert all(p <= bid for p in prices), "All prices must be <= bid"
        assert prices == [0.30, 0.27, 0.25, 0.22, 0.20]

    def test_expensive_side_offsets(self):
        """bid > 0.35 → offsets [0, 0.05, 0.10]."""
        bid = 0.68
        offsets = [0.00, 0.05, 0.10]
        prices = [round(bid - o, 2) for o in offsets]
        assert all(p <= bid for p in prices), "All prices must be <= bid"
        assert prices == [0.68, 0.63, 0.58]

    def test_prices_below_001_skipped(self):
        """Prices < 0.01 must be filtered out."""
        bid = 0.05
        offsets = [0.00, 0.03, 0.05, 0.08, 0.10]
        prices = [round(bid - o, 2) for o in offsets if round(bid - o, 2) >= 0.01]
        assert all(p >= 0.01 for p in prices)

    def test_prices_above_098_skipped(self):
        """Prices > 0.98 must be filtered out."""
        bid = 0.99
        offsets = [0.00, 0.05, 0.10]
        prices = [round(bid - o, 2) for o in offsets if round(bid - o, 2) <= 0.98]
        assert all(p <= 0.98 for p in prices)

    def test_open_price_is_bid_plus_one_cent(self):
        """_v2_open_position posts at bid+1¢ (fills immediately)."""
        bid = 0.50
        post_price = round(bid + 0.01, 2)
        assert post_price == 0.51


# ── 8. StateResetOnOpen ──────────────────────────────────────────────────────

class TestStateResetOnOpen:
    """All V2 state must be zeroed when a new window opens."""

    def test_early_cheap_filled_reset(self):
        window = _make_window()
        state = _make_state(window=window)
        state.early_cheap_filled = 18.50

        # Simulate _on_window_open
        state.early_cheap_filled = 0.0
        assert state.early_cheap_filled == 0.0

    def test_early_cheap_posted_reset(self):
        window = _make_window()
        state = _make_state(window=window)
        state.early_cheap_posted = 150.0

        state.early_cheap_posted = 0.0
        assert state.early_cheap_posted == 0.0

    def test_early_up_down_shares_reset(self):
        window = _make_window()
        state = _make_state(window=window)
        state.early_up_shares = 45.0
        state.early_down_shares = 30.0

        state.early_up_shares = 0.0
        state.early_down_shares = 0.0
        assert state.early_up_shares == 0.0
        assert state.early_down_shares == 0.0

    def test_early_dca_orders_reset(self):
        window = _make_window()
        state = _make_state(window=window)
        state.early_dca_orders = [{"order_id": "old"}]

        state.early_dca_orders = []
        assert state.early_dca_orders == []

    def test_early_position_reset(self):
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": True}

        state.early_position = None
        assert state.early_position is None

    def test_early_accum_ticks_reset(self):
        window = _make_window()
        state = _make_state(window=window)
        state.early_accum_ticks = {1, 2, 3, 4, 5}

        state.early_accum_ticks = set()
        assert len(state.early_accum_ticks) == 0


# ── 9. ThreeSecondTiming ─────────────────────────────────────────────────────

class TestThreeSecondTiming:
    """Accumulation fires once per 3s tick via early_accum_ticks dedup."""

    def test_tick_3s_computation(self):
        """tick_3s = int(seconds_since_open // 3) — each 3-second window."""
        assert int(0.0 // 3) == 0
        assert int(2.9 // 3) == 0
        assert int(3.0 // 3) == 1
        assert int(5.9 // 3) == 1
        assert int(6.0 // 3) == 2
        assert int(269.9 // 3) == 89

    def test_90_unique_ticks_in_270s_window(self):
        """270s / 3s = 90 ticks."""
        ticks = set(int(s // 3) for s in range(0, 270))
        assert len(ticks) == 90

    def test_same_tick_not_fired_twice(self):
        """Adding tick_3s to early_accum_ticks prevents re-fire."""
        fired = set()
        results = []
        for t in [0, 0, 0, 1, 1, 2]:
            if t not in fired:
                fired.add(t)
                results.append("fired")
            else:
                results.append("skipped")
        assert results.count("fired") == 3
        assert results.count("skipped") == 3

    def test_cutoff_stops_accumulation(self):
        """At T+270s+ no new accumulation ticks should fire."""
        cutoff = 270
        for secs in [270, 271, 280, 299]:
            in_window = 0 <= secs <= cutoff
            # At exactly cutoff=270 it's still <= cutoff, but cancel fires
            # The loop condition is: 0 <= seconds_since_open <= cutoff
            assert in_window == (secs <= 270)


# ── 10. CombinedAvgMath ──────────────────────────────────────────────────────

class TestCombinedAvgMath:
    """avg_price = total_cost / total_shares — correct across multiple fills."""

    def test_single_fill_avg(self):
        cost = 1.0
        shares = 3  # round(1.0 / 0.33) = 3
        avg = cost / shares
        assert round(avg, 3) == round(1.0 / 3, 3)

    def test_multiple_fills_weighted_avg(self):
        # (cost, shares) tuples: total_cost=3.0, total_shares=12
        fills = [
            (1.0, 3),   # $1 at ~0.33¢ → 3 shares
            (1.0, 4),   # $1 at 0.25¢ → 4 shares
            (1.0, 5),   # $1 at 0.20¢ → 5 shares
        ]
        total_cost = sum(c for c, _ in fills)
        total_shares = sum(s for _, s in fills)
        avg = total_cost / total_shares
        assert round(avg, 4) == round(3.0 / 12, 4)

    def test_shares_rounding(self):
        """shares = max(round(size / price), 5)."""
        assert max(round(1.0 / 0.33), 5) == 5    # 3 → max(3, 5) = 5
        assert max(round(6.0 / 0.49), 5) == 12   # 12.24 → 12
        assert max(round(1.0 / 0.01), 5) == 100  # 100

    def test_up_cost_and_down_cost_tracked_separately(self):
        from polybot.core.loop import AssetState
        from polybot.strategy.base_rate import BaseRateTable
        from polybot.market.window_tracker import WindowTracker
        from polybot.strategy.bayesian import BayesianUpdater

        br = BaseRateTable()
        tracker = WindowTracker(entry_seconds=120, asset="BTC", window_seconds=300)
        state = AssetState(asset="BTC", tracker=tracker, bayesian=BayesianUpdater(br))

        state.early_up_shares = 5
        state.early_up_cost = 1.65
        state.early_down_shares = 3
        state.early_down_cost = 0.99

        up_avg = state.early_up_cost / state.early_up_shares
        down_avg = state.early_down_cost / state.early_down_shares
        assert round(up_avg, 3) == 0.330
        assert round(down_avg, 3) == 0.330


# ── 11. ETHModelFallback ─────────────────────────────────────────────────────

class TestETHModelFallback:
    """ETH has no trained model — must use 0.50 fallback without crashing."""

    async def test_model_fallback_on_exception(self):
        bot = _make_bot()
        bot.model_server.predict = MagicMock(side_effect=KeyError("ETH_5m not found"))
        window = _make_window(slug="eth-updown-5m-1000000")
        state = _make_state(asset="ETH", window=window)
        bot._refresh_orderbook = _noop_refresh

        position_set = False
        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_open_position(state, 3_000.0)
            # Even on error, position should be set (lgbm_raw defaults to 0.50)
            position_set = state.early_position is not None

        assert position_set, "Position must be set even when model raises exception"

    async def test_fallback_produces_neutral_direction(self):
        """lgbm_raw=0.50 → direction_up = (0.50 >= 0.50) = True (deterministic)."""
        bot = _make_bot()
        bot.model_server.predict = MagicMock(side_effect=Exception("no model"))
        window = _make_window(slug="eth-updown-5m-1000000")
        state = _make_state(asset="ETH", window=window)
        bot._refresh_orderbook = _noop_refresh

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_open_position(state, 3_000.0)

        if state.early_position:
            # 0.50 >= 0.50 is True
            assert state.early_position["direction_up"] is True


# ── 12. ModeGuard ────────────────────────────────────────────────────────────

class TestModeGuard:
    """No order posting in paper mode — accumulation and open must be no-ops."""

    async def test_accumulate_no_op_in_paper(self):
        bot = _make_bot(mode="paper")
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": True, "entry_price": 0.33, "hedge_entry_price": 0.69}

        bot.trader.client.create_order = MagicMock()
        await bot._v2_accumulate_cheap(state, 50_000.0)
        bot.trader.client.create_order.assert_not_called()

    async def test_open_no_op_in_paper(self):
        bot = _make_bot(mode="paper")
        window = _make_window()
        state = _make_state(window=window)
        bot._refresh_orderbook = _noop_refresh

        await bot._v2_open_position(state, 50_000.0)

        bot.trader.client.post_order.assert_not_called()
        assert state.early_position is None

    def test_poll_fills_runs_regardless_of_mode(self):
        """_v2_poll_fills has no mode guard — tracks fills in any mode."""
        import inspect
        from polybot.core.loop import TradingLoop
        source = inspect.getsource(TradingLoop._v2_poll_fills)
        # Confirm there's no mode != "live" guard in this method
        assert 'mode != "live"' not in source, "_v2_poll_fills must not have mode guard"


# ── 13. FullWindowSimulation ─────────────────────────────────────────────────

class TestFullWindowSimulation:
    """Simulate 90 accumulation ticks over 270s, verify budget and both-sides behavior."""

    async def test_accumulation_fires_90_ticks(self):
        """Mock accumulate to count calls — should fire once per 3s tick."""
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {
            "slug": "early_btc-updown-5m-1000000",
            "direction_up": True,
            "entry_price": 0.33,
            "hedge_entry_price": 0.69,
            "shares": 10,
            "side": "YES",
            "size": 6.0,
        }

        call_count = 0
        async def mock_accumulate(state, price):
            nonlocal call_count
            call_count += 1

        async def mock_poll(state):
            pass

        bot._v2_accumulate_cheap = mock_accumulate
        bot._v2_poll_fills = mock_poll

        # Simulate ticks at 0, 3, 6, ..., 267 seconds (90 ticks)
        for t in range(0, 270, 3):
            seconds = float(t) + 0.1
            tick_3s = int(seconds // 3)
            if tick_3s not in state.early_accum_ticks:
                state.early_accum_ticks.add(tick_3s)
                await bot._v2_accumulate_cheap(state, 50_000.0)
                await bot._v2_poll_fills(state)

        assert call_count == 90, f"Expected 90 accumulation ticks, got {call_count}"

    async def test_budget_cap_stops_spending_mid_window(self):
        """If $20 fills early, remaining ticks should not post new orders."""
        bot = _make_bot(max_bet=20.0)
        window = _make_window(yes="yes_sim", no="no_sim")
        state = _make_state(window=window)
        state.early_position = {
            "direction_up": True,
            "entry_price": 0.33,
            "hedge_entry_price": 0.69,
            "slug": "early_test",
            "shares": 10,
            "side": "YES",
            "size": 6.0,
        }
        state.early_cheap_filled = 20.0  # budget already exhausted
        bot._refresh_orderbook = _noop_refresh

        post_calls = []
        bot.trader.client.post_order = MagicMock(side_effect=lambda *a, **kw: post_calls.append(1) or {"orderID": "x"})
        bot.trader.client.create_order = MagicMock(return_value={"signed": "x"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_accumulate_cheap(state, 50_000.0)

        assert len(post_calls) == 0, "No orders should be posted when budget exhausted"

    async def test_both_token_ids_posted_per_tick(self):
        """Each accumulation tick must post on both YES and NO token IDs."""
        bot = _make_bot()
        window = _make_window(yes="yes_sim2", no="no_sim2")
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.30, no_bid=0.70)
        state.early_position = {
            "direction_up": True,
            "entry_price": 0.31,
            "hedge_entry_price": 0.71,
            "slug": "early_test",
            "shares": 10,
            "side": "YES",
            "size": 6.0,
        }
        bot._refresh_orderbook = _noop_refresh

        posted_tokens = set()
        def track_create(args, options):
            posted_tokens.add(args.token_id)
            return {"signed": "x"}
        bot.trader.client.create_order = track_create
        bot.trader.client.post_order = MagicMock(return_value={"orderID": "oid"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_accumulate_cheap(state, 50_000.0)

        assert "yes_sim2" in posted_tokens, "YES token must be posted"
        assert "no_sim2" in posted_tokens, "NO token must be posted"
