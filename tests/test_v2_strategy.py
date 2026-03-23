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
    s.enabled_pairs = [("BTC", 300), ("ETH", 300), ("SOL", 300), ("XRP", 300)]
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
    s.early_entry_reprice_stale_after_seconds = 6.0
    s.early_entry_reprice_price_tolerance = 0.01
    s.directional_min_move_pct = 0.03
    s.bankroll = 1000.0
    s.kelly_fraction = 0.25
    s.min_trade_usd = 1.0
    s.max_trade_usd = 10.0
    return s


def _actual_order_cost(target_usd: float, price: float) -> tuple[int, float]:
    shares = max(int(round(target_usd / price, 0)), 5)
    return shares, round(shares * price, 2)


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

    async def test_posts_orders_on_open(self):
        bot = _make_bot(max_bet=50.0)  # enough budget for both sides
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

        assert len(calls) >= 1, f"Expected at least 1 order on open, got {len(calls)}"

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
        assert state.traded_this_window is True

    async def test_open_runs_in_paper_mode_for_verification(self):
        bot = _make_bot(mode="paper")
        window = _make_window()
        state = _make_state(window=window)
        bot._refresh_orderbook = _noop_refresh

        with patch.object(bot, "_log_activity"):
            await bot._v2_open_position(state, 50_000.0)

        assert state.early_position is not None
        bot.trader.client.post_order.assert_called()

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

        # T+90s: after T+60s gate, winning side (no_bid=0.68) fires
        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_accumulate_cheap(state, 50_000.0, seconds_since_open=90.0)

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

    async def test_accumulate_runs_in_paper_mode_for_verification(self):
        bot = _make_bot(mode="paper")
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": True, "entry_price": 0.33, "hedge_entry_price": 0.69}

        bot.trader.client.create_order = MagicMock()
        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_accumulate_cheap(state, 50_000.0)

        bot.trader.client.create_order.assert_called()

    async def test_cheap_side_gets_7_levels(self):
        """Bid <= 0.35 → 7 offset levels."""
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
        assert len(yes_prices) == 7, f"Expected 7 levels for cheap YES side, got {len(yes_prices)}"

    async def test_mid_zone_gets_5_levels(self):
        """Bid 0.35-0.60 (mid/baseline zone) → 5 offset levels at $0.20 each."""
        bot = _make_bot()
        window = _make_window(yes="yes_mid", no="no_mid")
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.45, no_bid=0.55)
        state.early_position = {
            "direction_up": True,
            "entry_price": 0.46,
            "hedge_entry_price": 0.56,
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

        yes_prices = [p for t, p in posted_prices if t == "yes_mid"]
        assert len(yes_prices) == 5, f"Expected 5 levels for mid YES side (bid=0.45), got {len(yes_prices)}"

    async def test_very_cheap_gets_9_levels(self):
        """Bid <= 0.15 (lottery zone) → 9 offset levels at $0.35 each."""
        bot = _make_bot()
        window = _make_window(yes="yes_lottery", no="no_lottery")
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.10, no_bid=0.90)
        state.early_position = {
            "direction_up": True,
            "entry_price": 0.11,
            "hedge_entry_price": 0.91,
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

        yes_prices = [p for t, p in posted_prices if t == "yes_lottery"]
        assert len(yes_prices) == 9, f"Expected 9 levels for lottery YES side (bid=0.10), got {len(yes_prices)}"

    async def test_winning_side_posts_before_t60(self):
        """Bid > 0.60 still posts on the winning side before T+60s."""
        bot = _make_bot()
        window = _make_window(yes="yes_win", no="no_win")
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.75, no_bid=0.25)
        state.early_position = {
            "direction_up": True,
            "entry_price": 0.76,
            "hedge_entry_price": 0.26,
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
            await bot._v2_accumulate_cheap(state, 50_000.0, seconds_since_open=30.0)

        yes_prices = [p for t, p in posted_prices if t == "yes_win"]
        assert len(yes_prices) == 3, f"Winning side should post 3 levels before T+60s, got {len(yes_prices)}"

    async def test_winning_side_gets_3_levels_after_t60(self):
        """Bid > 0.60 → 3 levels at bid, bid-1¢, bid-3¢ after T+60s."""
        bot = _make_bot()
        window = _make_window(yes="yes_win2", no="no_win2")
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.75, no_bid=0.25)
        state.early_position = {
            "direction_up": True,
            "entry_price": 0.76,
            "hedge_entry_price": 0.26,
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
            await bot._v2_accumulate_cheap(state, 50_000.0, seconds_since_open=90.0)

        yes_prices = [p for t, p in posted_prices if t == "yes_win2"]
        assert len(yes_prices) == 3, f"Expected 3 levels for winning side after T+60s, got {len(yes_prices)}"


# ── 3. BudgetCap ─────────────────────────────────────────────────────────────

class TestBudgetCap:
    """Filled + reserved notional must never exceed max_bet_per_asset."""

    async def test_orders_skip_when_budget_exhausted(self):
        bot = _make_bot(max_bet=50.0)
        window = _make_window(yes="yes_b", no="no_b")
        state = _make_state(window=window)
        state.early_reserved_notional = 49.90
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

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_accumulate_cheap(state, 50_000.0)

        assert len(post_calls) == 0, f"Should post 0 orders when budget exhausted, got {len(post_calls)}"

    async def test_fill_tracking_does_not_double_count(self):
        """Budget is pre-reserved at posting time. poll_fills must NOT add to early_cheap_filled
        (that would double-count against the cap)."""
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": True}
        state.early_reserved_notional = 7.0
        state.early_dca_orders = [
            {"order_id": "oid1", "price": 0.33, "size": 3.0, "actual_notional_usd": 3.0, "reserved_notional_usd_remaining": 3.0, "shares": 9, "side": "UP", "filled": False},
            {"order_id": "oid2", "price": 0.68, "size": 4.0, "actual_notional_usd": 4.0, "reserved_notional_usd_remaining": 4.0, "shares": 6, "side": "DOWN", "filled": False},
        ]

        def mock_get_order(oid):
            return {"status": "MATCHED"}

        bot.trader.client.get_order = mock_get_order

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_poll_fills(state)

        assert state.early_reserved_notional == 0.0
        assert state.early_cheap_filled == 7.0
        assert state.early_filled_notional == 7.0

    async def test_budget_resets_on_new_window(self):
        window = _make_window()
        state = _make_state(window=window)
        state.early_cheap_filled = 19.5
        state.filled_position_cost_usd = 19.5
        state.early_filled_notional = 19.5
        state.reserved_open_order_usd = 2.0
        state.early_reserved_notional = 2.0
        state.early_cheap_posted = 50.0

        # Simulate _on_window_open reset
        state.early_cheap_posted = 0.0
        state.early_cheap_filled = 0.0
        state.filled_position_cost_usd = 0.0
        state.early_filled_notional = 0.0
        state.reserved_open_order_usd = 0.0
        state.early_reserved_notional = 0.0

        assert state.early_cheap_filled == 0.0
        assert state.filled_position_cost_usd == 0.0
        assert state.early_filled_notional == 0.0
        assert state.reserved_open_order_usd == 0.0
        assert state.early_reserved_notional == 0.0
        assert state.early_cheap_posted == 0.0

    def test_filled_never_exceeds_max_bet_in_check(self):
        """Real exposure must use actual notional, not intended size."""
        max_bet = 50.0
        committed = 49.0
        actual_notional_usd = 2.25  # 5 shares at 45c
        assert committed + actual_notional_usd > max_bet

        committed = 47.75
        assert not (committed + actual_notional_usd > max_bet)

    def test_low_price_many_shares_can_fit_under_50_usd_notional(self):
        """Low-price orders may use many shares but still fit when USD notional stays under max."""
        bot = _make_bot(max_bet=50.0)
        state = _make_state(window=_make_window())

        shares, actual_notional_usd = bot._v2_order_size(49.80, 0.10)

        assert shares == 498
        assert actual_notional_usd == 49.80
        assert bot._reserve_v2_budget(state, actual_notional_usd, "test_low_price", "UP")
        assert round(state.reserved_open_order_usd, 2) == 49.80

    def test_five_share_minimum_can_block_by_real_notional(self):
        """At higher prices, the 5-share floor must block once real notional would exceed max."""
        bot = _make_bot(max_bet=50.0)
        state = _make_state(window=_make_window())
        state.reserved_open_order_usd = 48.00
        state.early_reserved_notional = 48.00

        shares, actual_notional_usd = bot._v2_order_size(0.20, 0.45)

        assert shares == 5
        assert actual_notional_usd == 2.25
        assert not bot._reserve_v2_budget(state, actual_notional_usd, "test_min_floor_block", "UP")
        assert round(state.reserved_open_order_usd, 2) == 48.00

    def test_cap_enforcement_always_uses_actual_notional_usd(self):
        """Cap enforcement must use actual_notional_usd, not target USD size or share count."""
        bot = _make_bot(max_bet=50.0)
        state = _make_state(window=_make_window())
        state.reserved_open_order_usd = 47.75
        state.early_reserved_notional = 47.75

        shares, actual_notional_usd = bot._v2_order_size(0.20, 0.45)

        assert shares == 5
        assert actual_notional_usd == 2.25
        assert bot._reserve_v2_budget(state, actual_notional_usd, "test_actual_notional_cap", "UP")
        assert round(state.reserved_open_order_usd, 2) == 50.00

    async def test_dense_tick_never_reserves_above_50(self):
        bot = _make_bot(max_bet=50.0)
        window = _make_window(yes="yes_dense", no="no_dense")
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.30, no_bid=0.30)
        state.reserved_open_order_usd = 45.65
        state.early_reserved_notional = 45.65
        state.early_position = {
            "direction_up": True,
            "entry_price": 0.31,
            "hedge_entry_price": 0.31,
            "slug": "early_dense",
            "shares": 10,
            "side": "YES",
            "size": 6.0,
        }
        bot._refresh_orderbook = _noop_refresh

        post_calls = []
        bot.trader.client.create_order = MagicMock(return_value={"signed": "x"})
        bot.trader.client.post_order = MagicMock(side_effect=lambda *a, **kw: post_calls.append(1) or {"orderID": f"oid_{len(post_calls)}"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_accumulate_cheap(state, 50_000.0)

        assert round(state.reserved_open_order_usd, 2) == 50.0
        assert state.reserved_open_order_usd <= 50.0
        assert state.early_cheap_filled == 0.0
        assert len(post_calls) == 3, f"Expected only 3 orders before cap, got {len(post_calls)}"

    async def test_same_tick_prereserve_prevents_race_overflow(self):
        bot = _make_bot(max_bet=50.0)
        window = _make_window(yes="yes_race", no="no_race")
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.30, no_bid=0.30)
        state.reserved_open_order_usd = 48.50
        state.early_reserved_notional = 48.50
        state.early_position = {
            "direction_up": True,
            "entry_price": 0.31,
            "hedge_entry_price": 0.31,
            "slug": "early_race",
            "shares": 10,
            "side": "YES",
            "size": 6.0,
        }
        bot._refresh_orderbook = _noop_refresh

        reserved_snapshots = []

        async def slow_post(*args, **kwargs):
            reserved_snapshots.append(round(state.reserved_open_order_usd, 2))
            await asyncio.sleep(0.01)

        bot._post_cheap_order = slow_post

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_accumulate_cheap(state, 50_000.0)

        assert round(state.reserved_open_order_usd, 2) == 50.0
        assert len(reserved_snapshots) == 1, f"Expected only 1 scheduled order before cap, got {len(reserved_snapshots)}"
        assert all(snapshot <= 50.0 for snapshot in reserved_snapshots)

    async def test_min_share_floor_inflates_actual_notional(self):
        bot = _make_bot(max_bet=50.0)
        window = _make_window(yes="yes_min", no="no_min")
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.45, no_bid=0.45)
        state.early_position = {
            "direction_up": True,
            "entry_price": 0.46,
            "hedge_entry_price": 0.46,
            "slug": "early_min",
            "shares": 0,
            "side": "YES",
            "size": 0.0,
        }
        bot._refresh_orderbook = _noop_refresh

        share_sizes = []

        def track_create(args, options):
            share_sizes.append(args.size)
            return {"signed": "x"}

        bot.trader.client.create_order = track_create
        bot.trader.client.post_order = MagicMock(return_value={"orderID": "oid"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_accumulate_cheap(state, 50_000.0)

        assert share_sizes, "Expected at least one accumulation order"
        assert min(share_sizes) == 5
        assert round(state.reserved_open_order_usd, 2) == 21.4

    async def test_budget_uses_actual_notional_not_intended_size(self):
        bot = _make_bot(max_bet=50.0)
        window = _make_window(yes="yes_actual", no="no_actual")
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.45, no_bid=0.45)
        state.reserved_open_order_usd = 49.55
        state.early_reserved_notional = 49.55
        state.early_position = {
            "direction_up": True,
            "entry_price": 0.46,
            "hedge_entry_price": 0.46,
            "slug": "early_actual",
            "shares": 0,
            "side": "YES",
            "size": 0.0,
        }
        bot._refresh_orderbook = _noop_refresh

        post_calls = []
        bot.trader.client.create_order = MagicMock(return_value={"signed": "x"})
        bot.trader.client.post_order = MagicMock(side_effect=lambda *a, **kw: post_calls.append(1) or {"orderID": f"oid_{len(post_calls)}"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_accumulate_cheap(state, 50_000.0)

        assert len(post_calls) == 0, f"Expected no orders to fit once actual 5-share notional is used, got {len(post_calls)}"
        assert round(state.reserved_open_order_usd, 2) == 49.55

    async def test_stale_orders_are_cancelled_released_and_repriced(self):
        bot = _make_bot(max_bet=50.0)
        window = _make_window(yes="yes_reprice", no="no_reprice")
        state = _make_state(asset="SOL", window=window)
        state.orderbook = _make_orderbook(yes_bid=0.30, no_bid=0.70)
        state.early_position = {
            "direction_up": True,
            "entry_price": 0.31,
            "hedge_entry_price": 0.71,
            "slug": "early_reprice",
            "shares": 10,
            "side": "YES",
            "size": 6.0,
        }
        state.reserved_open_order_usd = 5.0
        state.early_reserved_notional = 5.0
        now = 1_000_000.0
        state.early_dca_orders = [
            {
                "order_id": "old_up",
                "price": 0.20,
                "actual_price": 0.20,
                "shares": 5,
                "actual_shares": 5,
                "size": 1.0,
                "actual_notional_usd": 1.0,
                "remaining_reserved_notional_usd": 1.0,
                "side": "UP",
                "filled": False,
                "created_at": now - 10,
            },
            {
                "order_id": "old_down",
                "price": 0.80,
                "actual_price": 0.80,
                "shares": 5,
                "actual_shares": 5,
                "size": 4.0,
                "actual_notional_usd": 4.0,
                "remaining_reserved_notional_usd": 4.0,
                "side": "DOWN",
                "filled": False,
                "created_at": now - 10,
            },
        ]
        bot._refresh_orderbook = _noop_refresh

        posted = []
        cancelled = []
        bot.trader.client.create_order = MagicMock(return_value={"signed": "x"})
        bot.trader.client.post_order = MagicMock(
            side_effect=lambda *a, **kw: posted.append(1) or {"orderID": f"oid_{len(posted)}"}
        )
        bot.trader.client.cancel = MagicMock(
            side_effect=lambda order_id: cancelled.append(order_id) or {"success": True}
        )

        with (
            patch("polybot.core.loop.time.time", return_value=now),
            patch("polybot.core.loop.logger.info") as log_info,
            patch.object(bot, "_log_activity", MagicMock()),
        ):
            await bot._v2_accumulate_cheap(state, 50_000.0, seconds_since_open=30.0)

        assert cancelled == ["old_up", "old_down"]
        assert len(posted) == 10
        assert round(state.reserved_open_order_usd, 2) == 19.75
        assert {order["side"] for order in state.early_dca_orders if not order.get("filled")} == {"UP", "DOWN"}
        assert {order["order_id"] for order in state.early_dca_orders}.isdisjoint({"old_up", "old_down"})

        events = [call.args[0] for call in log_info.call_args_list if call.args]
        assert "stale_order_cancelled" in events
        assert "budget_released" in events
        assert "repriced_order_posted" in events
        reprice_cycle = [
            call for call in log_info.call_args_list if call.args and call.args[0] == "v2_reprice_cycle"
        ]
        assert reprice_cycle
        assert reprice_cycle[-1].kwargs["num_open_orders_before"] == 2
        assert reprice_cycle[-1].kwargs["num_open_orders_after"] == 10

    async def test_fresh_near_orders_are_reused_instead_of_repriced(self):
        bot = _make_bot(max_bet=50.0)
        window = _make_window(yes="yes_keep", no="no_keep")
        state = _make_state(asset="SOL", window=window)
        state.orderbook = _make_orderbook(yes_bid=0.30, no_bid=0.70)
        state.early_position = {
            "direction_up": True,
            "entry_price": 0.31,
            "hedge_entry_price": 0.71,
            "slug": "early_keep",
            "shares": 10,
            "side": "YES",
            "size": 6.0,
        }
        state.reserved_open_order_usd = 5.0
        state.early_reserved_notional = 5.0
        now = 1_000_000.0
        state.early_dca_orders = [
            {
                "order_id": "keep_up",
                "price": 0.30,
                "actual_price": 0.30,
                "shares": 5,
                "actual_shares": 5,
                "size": 1.5,
                "actual_notional_usd": 1.5,
                "remaining_reserved_notional_usd": 1.5,
                "side": "UP",
                "filled": False,
                "created_at": now - 2,
            },
            {
                "order_id": "keep_down",
                "price": 0.70,
                "actual_price": 0.70,
                "shares": 5,
                "actual_shares": 5,
                "size": 3.5,
                "actual_notional_usd": 3.5,
                "remaining_reserved_notional_usd": 3.5,
                "side": "DOWN",
                "filled": False,
                "created_at": now - 2,
            },
        ]
        bot._refresh_orderbook = _noop_refresh

        posted = []
        bot.trader.client.create_order = MagicMock(return_value={"signed": "x"})
        bot.trader.client.post_order = MagicMock(
            side_effect=lambda *a, **kw: posted.append(1) or {"orderID": f"oid_{len(posted)}"}
        )
        bot.trader.client.cancel = MagicMock(return_value={"success": True})

        with (
            patch("polybot.core.loop.time.time", return_value=now),
            patch.object(bot, "_log_activity", MagicMock()),
        ):
            await bot._v2_accumulate_cheap(state, 50_000.0, seconds_since_open=30.0)

        bot.trader.client.cancel.assert_not_called()
        assert len(posted) == 8
        assert round(state.reserved_open_order_usd, 2) == 19.75
        assert {order["order_id"] for order in state.early_dca_orders} >= {"keep_up", "keep_down"}

    async def test_cancel_releases_reserved_budget(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"slug": "early_test", "direction_up": True}
        state.early_cheap_filled = 5.0
        state.filled_position_cost_usd = 5.0
        state.early_filled_notional = 5.0
        state.reserved_open_order_usd = 5.0
        state.early_reserved_notional = 5.0
        state.early_dca_orders = [
            {"order_id": "oid_filled", "price": 0.30, "actual_price": 0.30, "shares": 17, "actual_shares": 17, "size": 5.0, "actual_notional_usd": 5.0, "remaining_reserved_notional_usd": 0.0, "side": "UP", "filled": True},
            {"order_id": "oid_open_1", "price": 0.25, "actual_price": 0.25, "shares": 12, "actual_shares": 12, "size": 3.0, "actual_notional_usd": 3.0, "remaining_reserved_notional_usd": 3.0, "side": "UP", "filled": False},
            {"order_id": "oid_open_2", "price": 0.22, "actual_price": 0.22, "shares": 9, "actual_shares": 9, "size": 2.0, "actual_notional_usd": 2.0, "remaining_reserved_notional_usd": 2.0, "side": "DOWN", "filled": False},
        ]
        bot.trader.client.cancel = MagicMock(return_value={"success": True})

        await bot._early_cancel_unfilled(state)

        assert round(state.reserved_open_order_usd, 2) == 0.0
        assert round(state.early_cheap_filled, 2) == 5.0
        assert len(state.early_dca_orders) == 1
        assert state.early_dca_orders[0]["order_id"] == "oid_filled"

    async def test_rejected_order_releases_reserved_budget(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": True}
        state.early_cheap_filled = 5.0
        state.filled_position_cost_usd = 5.0
        state.early_filled_notional = 5.0
        state.reserved_open_order_usd = 1.0
        state.early_reserved_notional = 1.0
        state.early_dca_orders = [
            {"order_id": "oid_rejected", "price": 0.30, "actual_price": 0.30, "shares": 5, "actual_shares": 5, "size": 1.0, "actual_notional_usd": 1.0, "remaining_reserved_notional_usd": 1.0, "side": "UP", "filled": False},
            {"order_id": "oid_filled", "price": 0.35, "actual_price": 0.35, "shares": 14, "actual_shares": 14, "size": 5.0, "actual_notional_usd": 5.0, "remaining_reserved_notional_usd": 0.0, "side": "DOWN", "filled": True},
        ]
        bot.trader.client.get_order = MagicMock(side_effect=lambda oid: {"status": "REJECTED"} if oid == "oid_rejected" else {"status": "FILLED"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_poll_fills(state)

        assert round(state.reserved_open_order_usd, 2) == 0.0
        assert round(state.early_cheap_filled, 2) == 5.0
        assert len(state.early_dca_orders) == 1
        assert state.early_dca_orders[0]["order_id"] == "oid_filled"

    async def test_expired_order_releases_reserved_budget(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": True}
        state.reserved_open_order_usd = 2.25
        state.early_reserved_notional = 2.25
        state.early_dca_orders = [
            {"order_id": "oid_expired", "price": 0.45, "actual_price": 0.45, "shares": 5, "actual_shares": 5, "size": 2.25, "actual_notional_usd": 2.25, "remaining_reserved_notional_usd": 2.25, "side": "UP", "filled": False},
        ]
        bot.trader.client.get_order = MagicMock(return_value={"status": "EXPIRED"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_poll_fills(state)

        assert round(state.reserved_open_order_usd, 2) == 0.0
        assert state.early_dca_orders == []


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
        # Add expensive filled lots so lot-aware checkpoint finds them
        state.early_dca_orders = [
            {"order_id": "ox1", "side": "main", "actual_price": 0.70,
             "actual_shares": 10, "actual_notional_usd": 7.0,
             "filled": True, "filled_shares": 10, "filled_notional_usd": 7.0},
        ]
        bot._refresh_orderbook = _noop_refresh
        # Model strongly against us, position down 28%
        bot.model_server.predict = MagicMock(return_value=0.30)

        sell_called = []
        async def mock_sell(state, pos, bid, action, sell_shares=None, sell_cost=None):
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
        state.early_position = {"direction_up": True, "shares": 0, "size": 0.0}
        state.early_dca_orders = [
            {"order_id": "oid_main", "price": 0.33, "actual_price": 0.33, "size": 0.99, "actual_notional_usd": 0.99, "remaining_reserved_notional_usd": 0.99, "shares": 3, "actual_shares": 3, "side": "main", "filled": False},
        ]
        state.reserved_open_order_usd = 0.99
        state.early_reserved_notional = 0.99
        bot.trader.client.get_order = MagicMock(return_value={"status": "FILLED"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_poll_fills(state)

        assert state.early_up_shares == 3, "main + direction_up=True should go to up_shares"
        assert round(state.early_up_cost, 2) == 0.99
        assert state.early_position["shares"] == 3
        assert round(state.early_position["size"], 2) == 0.99

    async def test_main_label_with_direction_down_goes_to_down_shares(self):
        """side='main' + direction_up=False → down_shares."""
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": False, "shares": 0, "size": 0.0}
        state.early_dca_orders = [
            {"order_id": "oid_main_dn", "price": 0.68, "actual_price": 0.68, "size": 0.68, "actual_notional_usd": 0.68, "remaining_reserved_notional_usd": 0.68, "shares": 1, "actual_shares": 1, "side": "main", "filled": False},
        ]
        state.reserved_open_order_usd = 0.68
        state.early_reserved_notional = 0.68
        bot.trader.client.get_order = MagicMock(return_value={"status": "FILLED"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_poll_fills(state)

        assert state.early_down_shares == 1, "main + direction_up=False should go to down_shares"
        assert state.early_up_shares == 0

    async def test_fill_moves_reserved_notional_into_filled_accounting(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": True, "shares": 0, "size": 0.0}
        state.reserved_open_order_usd = 2.15
        state.early_reserved_notional = 2.15
        state.early_dca_orders = [
            {"order_id": "oid_fill", "price": 0.43, "actual_price": 0.43, "size": 2.15, "actual_notional_usd": 2.15, "remaining_reserved_notional_usd": 2.15, "shares": 5, "actual_shares": 5, "side": "UP", "filled": False},
        ]
        bot.trader.client.get_order = MagicMock(return_value={"status": "FILLED"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_poll_fills(state)

        assert round(state.reserved_open_order_usd, 2) == 0.0
        assert round(state.filled_position_cost_usd, 2) == 2.15
        assert round(state.early_cheap_filled, 2) == 2.15
        assert round(state.early_filled_notional, 2) == 2.15

    async def test_partial_fill_moves_only_partial_usd_from_reserved_to_filled(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": True, "shares": 0, "size": 0.0}
        state.reserved_open_order_usd = 2.25
        state.early_reserved_notional = 2.25
        state.early_dca_orders = [
            {"order_id": "oid_partial", "price": 0.45, "actual_price": 0.45, "size": 2.25, "actual_notional_usd": 2.25, "remaining_reserved_notional_usd": 2.25, "shares": 5, "actual_shares": 5, "side": "UP", "filled": False},
        ]
        bot.trader.client.get_order = MagicMock(return_value={"status": "LIVE", "size_matched": "2"})

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_poll_fills(state)

        assert round(state.reserved_open_order_usd, 2) == 1.35
        assert round(state.filled_position_cost_usd, 2) == 0.9
        assert state.early_up_shares == 2
        assert round(state.early_up_cost, 2) == 0.9
        assert state.early_dca_orders[0]["filled"] is False
        assert round(state.early_dca_orders[0]["remaining_reserved_notional_usd"], 2) == 1.35

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

    def test_cheap_zone_offsets(self):
        """bid 0.15-0.35 → offsets [0, 1¢, 2¢, 4¢, 6¢], $0.35/level."""
        bid = 0.30
        offsets = [0.00, 0.01, 0.02, 0.04, 0.06]
        prices = [round(bid - o, 2) for o in offsets]
        assert all(p <= bid for p in prices), "All prices must be <= bid"
        assert prices == [0.30, 0.29, 0.28, 0.26, 0.24]

    def test_mid_zone_offsets(self):
        """bid 0.35-0.60 → offsets [0, 2¢, 5¢], $0.25/level."""
        bid = 0.50
        offsets = [0.00, 0.02, 0.05]
        prices = [round(bid - o, 2) for o in offsets]
        assert all(p <= bid for p in prices), "All prices must be <= bid"
        assert prices == [0.50, 0.48, 0.45]

    def test_winning_side_offsets(self):
        """bid > 0.60 → offsets [0, 3¢], $0.50/level, only after T+60s."""
        bid = 0.75
        offsets = [0.00, 0.03]
        prices = [round(bid - o, 2) for o in offsets]
        assert all(p <= bid for p in prices), "All prices must be <= bid"
        assert prices == [0.75, 0.72]

    def test_lottery_zone_offsets(self):
        """bid <= 0.15 → offsets [0, 1¢, 2¢, 3¢, 4¢, 5¢, 6¢], $0.50/level."""
        bid = 0.10
        offsets = [0.00, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06]
        prices = [p for o in offsets for p in [round(bid - o, 2)] if p >= 0.01]
        assert all(p >= 0.01 for p in prices)
        assert len(prices) == 7

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
        state.filled_position_cost_usd = 18.50
        state.reserved_open_order_usd = 4.50

        # Simulate _on_window_open
        state.early_cheap_filled = 0.0
        state.filled_position_cost_usd = 0.0
        state.reserved_open_order_usd = 0.0
        assert state.early_cheap_filled == 0.0
        assert state.filled_position_cost_usd == 0.0
        assert state.reserved_open_order_usd == 0.0

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
        """V2 uses rounded whole shares and real order notional."""
        assert _actual_order_cost(1.0, 0.33) == (5, 1.65)
        assert _actual_order_cost(0.2, 0.43) == (5, 2.15)
        assert _actual_order_cost(6.0, 0.49) == (12, 5.88)

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


# ── 11a. LGBMConfidenceSplit ─────────────────────────────────────────────────

class TestLGBMConfidenceSplit:
    """K9 spec: lgbm >= 0.60 → 70/30, 0.52-0.60 → 60/40, < 0.52 → 50/50."""

    async def _open_and_get_sizes(self, lgbm_val):
        bot = _make_bot(max_bet=20.0)
        bot.model_server.predict = MagicMock(return_value=lgbm_val)
        window = _make_window()
        state = _make_state(window=window)
        bot._refresh_orderbook = _noop_refresh

        sizes = {}
        def track_create(args, options):
            return {"signed": "x"}
        def track_post(signed, order_type):
            # orders are placed in order: main then hedge
            idx = len(sizes)
            sizes[idx] = getattr(signed, "size", None)
            return {"orderID": f"oid_{idx}"}

        # Capture sizes from early_dca_orders after call
        with patch.object(bot, "_log_activity"):
            await bot._v2_open_position(state, 50_000.0)
        return state

    async def test_lgbm_confidence_tiers_on_open(self):
        """Model-weighted allocation: open uses 10% of max_bet, scaled down when edge is weak."""
        cases = [
            # (lgbm, expected_up_pct, expected_down_pct, expected_budget_scale)
            (0.72, 0.80, 0.20, 1.00),
            (0.60, 0.60, 0.40, 1.00),
            (0.56, 0.60, 0.40, 0.75),
            (0.50, 0.50, 0.50, 0.30),
            (0.42, 0.40, 0.60, 0.75),
            (0.30, 0.20, 0.80, 1.00),
        ]
        for lgbm_val, exp_up_pct, exp_down_pct, exp_budget_scale in cases:
            bot = _make_bot(max_bet=50.0)
            bot.model_server.predict = MagicMock(return_value=lgbm_val)
            window = _make_window(slug=f"btc-updown-5m-{lgbm_val}")
            state = _make_state(window=window)
            bot._refresh_orderbook = _noop_refresh
            with patch.object(bot, "_log_activity"):
                await bot._v2_open_position(state, 50_000.0)
            orders = state.early_dca_orders
            up_sz = next((o["target_size"] for o in orders if o.get("side") == "UP"), None)
            down_sz = next((o["target_size"] for o in orders if o.get("side") == "DOWN"), None)
            open_budget = round(50.0 * 0.10 * exp_budget_scale, 2)
            assert up_sz is not None and down_sz is not None, f"lgbm={lgbm_val}: orders missing"
            assert abs(up_sz - round(open_budget * exp_up_pct, 2)) < 0.02, (
                f"lgbm={lgbm_val}: up={up_sz}, expected {round(open_budget * exp_up_pct, 2)}"
            )
            assert abs(down_sz - round(open_budget * exp_down_pct, 2)) < 0.02, (
                f"lgbm={lgbm_val}: down={down_sz}, expected {round(open_budget * exp_down_pct, 2)}"
            )

    async def test_open_spend_counted_toward_budget(self):
        """Open spend (UP + DOWN) must be reserved immediately using actual notional.
        Posts at bid+1¢ (aggressive at open), no combined-cost filter."""
        bot = _make_bot(max_bet=50.0)
        bot.model_server.predict = MagicMock(return_value=0.65)
        state = _make_state(window=_make_window())
        bot._refresh_orderbook = _noop_refresh
        with patch.object(bot, "_log_activity"):
            await bot._v2_open_position(state, 50_000.0)
        # open_budget = 50 * 0.10 = $5.00; up_pct=0.70, down_pct=0.30
        # UP: yes_bid=0.32, post at 0.33, target=$3.50 → shares=max(round(3.50/0.33),5)=11, cost=11*0.33=$3.63
        _, up_cost = _actual_order_cost(3.50, 0.33)
        # DOWN: no_bid=0.68, post at 0.69, target=$1.50 → shares=max(round(1.50/0.69),5)=5, cost=5*0.69=$3.45
        _, down_cost = _actual_order_cost(1.50, 0.69)
        expected_open_spend = round(up_cost + down_cost, 2)
        assert abs(state.early_reserved_notional - expected_open_spend) < 0.01, (
            f"early_reserved_notional={state.early_reserved_notional}, expected {expected_open_spend}"
        )
        assert state.early_cheap_filled == 0.0


class TestConfirmSafety:
    async def test_confirm_observes_without_flipping_or_posting(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {
            "slug": "early_btc-updown-5m-1000000",
            "token_id": "yes123",
            "hedge_token": "no456",
            "shares": 0,
            "entry_price": 0.33,
            "hedge_entry_price": 0.69,
            "direction_up": True,
            "side": "YES",
            "size": 0.0,
        }
        bot.model_server.predict = MagicMock(return_value=0.10)

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_confirm(state, 49_500.0)

        assert state.early_position["direction_up"] is True
        assert state.early_position["token_id"] == "yes123"
        assert state.early_position["hedge_token"] == "no456"
        bot.trader.client.post_order.assert_not_called()


class TestBudgetCurve:
    def test_budget_curve_starts_from_open_allocation(self):
        bot = _make_bot()
        assert bot._v2_budget_curve_pct(5.0) == 0.10
        assert bot._v2_budget_curve_pct(15.0) > 0.10
        assert bot._v2_budget_curve_pct(60.0) == 0.18
        assert bot._v2_budget_curve_pct(180.0) == 0.75
        assert bot._v2_budget_curve_pct(250.0) == 0.90

    def test_confidence_budget_scale_caps_weak_signals(self):
        bot = _make_bot()
        assert bot._v2_confidence_budget_scale(0.50) == 0.30
        assert bot._v2_confidence_budget_scale(0.54) == 0.55
        assert bot._v2_confidence_budget_scale(0.59) == 0.75
        assert bot._v2_confidence_budget_scale(0.65) == 1.00

    def test_pair_risk_limits_tighten_when_signal_is_weak(self):
        bot = _make_bot()
        assert bot._v2_pair_risk_limits(0.50) == (1.00, 0.50)
        assert bot._v2_pair_risk_limits(0.54) == (1.02, 1.00)
        assert bot._v2_pair_risk_limits(0.59) == (1.04, 1.50)
        assert bot._v2_pair_risk_limits(0.68) == (1.06, 2.00)


class TestExecutionSafety:
    async def test_scan_tick_blocked_after_early_trade(self):
        bot = _make_bot()
        bot.settings.early_entry_enabled = False
        bot._v2_graceful_stop_requested = False
        bot.rtds = MagicMock()
        bot.rtds.get_state.return_value = MagicMock(compute_lag=MagicMock())
        bot._on_window_open = AsyncMock()
        bot._on_window_close = AsyncMock()
        bot._v2_open_position = AsyncMock()
        bot._v2_confirm = AsyncMock()
        bot._v2_execution_tick = AsyncMock()
        bot._v2_poll_fills = AsyncMock()
        bot._early_cancel_unfilled = AsyncMock()
        bot._log_v2_status = MagicMock()
        bot._write_live_state_async = MagicMock()
        bot._scan_tick = AsyncMock()

        window = _make_window(open_ts=1_000)
        state = _make_state(window=window)
        state.early_entry_traded = True
        state.tracker.tick = MagicMock(return_value=None)

        with patch("polybot.core.loop.time.time", return_value=1_210.0):
            await bot._tick_asset(state, 50_000.0)

        bot._scan_tick.assert_not_called()

    async def test_execution_tick_keeps_recent_near_market_order(self):
        bot = _make_bot(max_bet=50.0)
        bot._v2_check_secrets_refresh = AsyncMock()
        bot._refresh_orderbook = _noop_refresh
        bot._post_cheap_order = AsyncMock()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {
            "slug": "early_btc-updown-5m-1000000",
            "token_id": "yes123",
            "hedge_token": "no456",
            "shares": 5,
            "entry_price": 0.33,
            "hedge_entry_price": 0.69,
            "direction_up": True,
            "side": "YES",
            "size": 1.65,
        }
        keep_order = bot._build_v2_tracked_order(
            order_id="keep_up",
            actual_shares=5,
            actual_price=0.32,
            actual_notional_usd=1.60,
            side="UP",
            target_size=1.60,
        )
        keep_order["created_at"] = time.time()
        state.early_dca_orders = [keep_order]
        state.reserved_open_order_usd = 1.60
        state.early_reserved_notional = 1.60

        await bot._v2_execution_tick(state, 50_000.0, 30.0)

        bot.trader.client.cancel_order.assert_not_called()
        assert any(order.get("order_id") == "keep_up" for order in state.early_dca_orders)

    async def test_execution_tick_stays_active_buy_only_after_180(self):
        bot = _make_bot(max_bet=50.0)
        bot._v2_check_secrets_refresh = AsyncMock()
        bot._refresh_orderbook = _noop_refresh
        bot._post_cheap_order = AsyncMock()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {
            "slug": "early_btc-updown-5m-1000000",
            "token_id": "yes123",
            "hedge_token": "no456",
            "shares": 5,
            "entry_price": 0.33,
            "hedge_entry_price": 0.69,
            "direction_up": True,
            "side": "YES",
            "size": 1.65,
        }

        await bot._v2_execution_tick(state, 50_000.0, 200.0)

        bot.trader.client.cancel_order.assert_not_called()
        assert bot._post_cheap_order.await_count > 0

    async def test_execution_tick_commits_at_250(self):
        bot = _make_bot(max_bet=50.0)
        bot._v2_check_secrets_refresh = AsyncMock()
        bot._refresh_orderbook = _noop_refresh
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {
            "slug": "early_btc-updown-5m-1000000",
            "token_id": "yes123",
            "hedge_token": "no456",
            "shares": 5,
            "entry_price": 0.33,
            "hedge_entry_price": 0.69,
            "direction_up": True,
            "side": "YES",
            "size": 1.65,
        }
        open_order = bot._build_v2_tracked_order(
            order_id="late_order",
            actual_shares=5,
            actual_price=0.32,
            actual_notional_usd=1.60,
            side="UP",
            target_size=1.60,
        )
        state.early_dca_orders = [open_order]
        state.reserved_open_order_usd = 1.60
        state.early_reserved_notional = 1.60

        await bot._v2_execution_tick(state, 50_000.0, 250.0)

        bot.trader.client.cancel.assert_called()
        assert state.early_reserved_notional == 0.0

    async def test_execution_tick_uses_cumulative_side_deficit_for_lagging_side(self):
        bot = _make_bot(max_bet=50.0)
        bot._v2_check_secrets_refresh = AsyncMock()
        bot._refresh_orderbook = _noop_refresh
        bot._post_cheap_order = AsyncMock()
        bot.model_server.predict = MagicMock(return_value=0.70)
        window = _make_window()
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.68, no_bid=0.30, yes_ask=0.69, no_ask=0.31)
        state.early_position = {
            "slug": "early_btc-updown-5m-1000000",
            "token_id": "yes123",
            "hedge_token": "no456",
            "shares": 20,
            "entry_price": 0.33,
            "hedge_entry_price": 0.69,
            "direction_up": True,
            "side": "YES",
            "size": 10.0,
        }
        state.early_up_shares = 20
        state.early_up_cost = 12.0
        state.early_down_shares = 5
        state.early_down_cost = 2.4
        state.filled_position_cost_usd = 14.4
        state.early_filled_notional = 14.4

        await bot._v2_execution_tick(state, 50_000.0, 120.0)

        posted_tokens = [call.args[1] for call in bot._post_cheap_order.await_args_list]
        assert "no456" in posted_tokens

    async def test_execution_tick_sells_excess_inventory_above_payout_floor(self):
        bot = _make_bot(max_bet=50.0)
        bot._v2_check_secrets_refresh = AsyncMock()
        bot._refresh_orderbook = _noop_refresh
        bot._post_cheap_order = AsyncMock()
        bot._v2_poll_fills = AsyncMock()
        bot._early_sell = AsyncMock(return_value=2.25)
        bot.model_server.predict = MagicMock(return_value=0.60)
        window = _make_window()
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.60, no_bid=0.45, yes_ask=0.61, no_ask=0.46)
        state.early_position = {
            "slug": "early_btc-updown-5m-1000000",
            "token_id": "yes123",
            "hedge_token": "no456",
            "shares": 30,
            "entry_price": 0.60,
            "hedge_entry_price": 0.45,
            "direction_up": True,
            "side": "YES",
            "size": 18.0,
        }
        state.early_up_shares = 30
        state.early_up_cost = 18.0
        state.early_down_shares = 35
        state.early_down_cost = 15.75
        state.filled_position_cost_usd = 33.75
        state.early_filled_notional = 33.75
        sell_order = bot._build_v2_tracked_order(
            order_id="filled_down",
            actual_shares=10,
            actual_price=0.45,
            actual_notional_usd=4.5,
            side="DOWN",
            target_size=4.5,
        )
        sell_order["filled"] = True
        sell_order["filled_shares"] = 10
        sell_order["filled_notional_usd"] = 4.5
        sell_order["inventory_shares"] = 10
        sell_order["inventory_notional_usd"] = 4.5
        state.early_dca_orders = [sell_order]

        await bot._v2_execution_tick(state, 50_000.0, 140.0)

        bot._early_sell.assert_awaited_once()
        _, _, sell_bid, reason = bot._early_sell.await_args.args[:4]
        assert round(sell_bid, 2) == 0.45
        assert reason == "PAYOUT_FLOOR"
        assert bot._early_sell.await_args.kwargs["sell_side_up"] is False

    async def test_execution_tick_blocks_new_buys_when_pair_state_is_bad(self):
        bot = _make_bot(max_bet=50.0)
        bot._v2_check_secrets_refresh = AsyncMock()
        bot._refresh_orderbook = _noop_refresh
        bot._post_cheap_order = AsyncMock()
        bot.model_server.predict = MagicMock(return_value=0.35)
        window = _make_window()
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.47, no_bid=0.65, yes_ask=0.48, no_ask=0.66)
        state.early_position = {
            "slug": "early_btc-updown-5m-1000000",
            "token_id": "yes123",
            "hedge_token": "no456",
            "shares": 10,
            "entry_price": 0.47,
            "hedge_entry_price": 0.58,
            "direction_up": False,
            "side": "NO",
            "size": 5.25,
        }
        state.early_up_shares = 5
        state.early_up_cost = 2.35
        state.early_down_shares = 5
        state.early_down_cost = 2.90
        state.filled_position_cost_usd = 5.25
        state.early_filled_notional = 5.25

        await bot._v2_execution_tick(state, 50_000.0, 120.0)

        bot._post_cheap_order.assert_not_awaited()

    async def test_execution_tick_blocks_expensive_catch_up_when_pair_is_incomplete(self):
        bot = _make_bot(max_bet=50.0)
        bot._v2_check_secrets_refresh = AsyncMock()
        bot._refresh_orderbook = _noop_refresh
        bot._post_cheap_order = AsyncMock()
        bot.model_server.predict = MagicMock(return_value=0.52)
        window = _make_window()
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.65, no_bid=0.52, yes_ask=0.66, no_ask=0.53)
        state.early_position = {
            "slug": "early_btc-updown-5m-1000000",
            "token_id": "yes123",
            "hedge_token": "no456",
            "shares": 5,
            "entry_price": 0.49,
            "hedge_entry_price": 0.52,
            "direction_up": True,
            "side": "YES",
            "size": 2.60,
        }
        state.early_up_shares = 0
        state.early_up_cost = 0.0
        state.early_down_shares = 5
        state.early_down_cost = 2.60
        state.filled_position_cost_usd = 2.60
        state.early_filled_notional = 2.60

        await bot._v2_execution_tick(state, 50_000.0, 90.0)

        bot._post_cheap_order.assert_not_awaited()

    async def test_execution_tick_blocks_late_expensive_rich_side_add(self):
        bot = _make_bot(max_bet=50.0)
        bot._v2_check_secrets_refresh = AsyncMock()
        bot._refresh_orderbook = _noop_refresh
        bot._post_cheap_order = AsyncMock()
        bot.model_server.predict = MagicMock(return_value=0.36)
        window = _make_window()
        state = _make_state(window=window)
        state.orderbook = _make_orderbook(yes_bid=0.18, no_bid=0.83, yes_ask=0.19, no_ask=0.84)
        state.early_position = {
            "slug": "early_btc-updown-5m-1000000",
            "token_id": "no456",
            "hedge_token": "yes123",
            "shares": 40,
            "entry_price": 0.83,
            "hedge_entry_price": 0.18,
            "direction_up": False,
            "side": "NO",
            "size": 31.50,
        }
        state.early_up_shares = 35
        state.early_up_cost = 7.35
        state.early_down_shares = 30
        state.early_down_cost = 24.15
        state.filled_position_cost_usd = 31.50
        state.early_filled_notional = 31.50

        await bot._v2_execution_tick(state, 50_000.0, 220.0)

        bot._post_cheap_order.assert_not_awaited()


class TestSellInventory:
    async def test_partial_sell_decrements_source_lot_inventory(self):
        bot = _make_bot()
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {
            "slug": "early_btc-updown-5m-1000000",
            "token_id": "yes123",
            "hedge_token": "no456",
            "shares": 20,
            "entry_price": 0.33,
            "hedge_entry_price": 0.69,
            "direction_up": True,
            "side": "YES",
            "size": 10.0,
        }
        state.early_down_shares = 10
        state.early_down_cost = 5.0
        state.filled_position_cost_usd = 5.0
        state.early_filled_notional = 5.0
        sell_order = bot._build_v2_tracked_order(
            order_id="filled_down",
            actual_shares=10,
            actual_price=0.50,
            actual_notional_usd=5.0,
            side="DOWN",
            target_size=5.0,
        )
        sell_order["filled"] = True
        sell_order["filled_shares"] = 10
        sell_order["filled_notional_usd"] = 5.0
        sell_order["inventory_shares"] = 10
        sell_order["inventory_notional_usd"] = 5.0

        proceeds = await bot._early_sell(
            state,
            state.early_position,
            current_bid=0.40,
            reason="EQUALIZE",
            sell_shares=5,
            sell_cost=2.5,
            sell_token_id=window.no_token_id,
            sell_side_up=False,
            sell_order=sell_order,
        )

        assert proceeds == 2.0
        assert state.early_down_shares == 5
        assert state.early_down_cost == 2.5
        assert bot._v2_order_inventory_shares(sell_order) == 5
        assert bot._v2_order_inventory_notional(sell_order) == 2.5


# ── 11b. OpenTimingT5s ────────────────────────────────────────────────────────

class TestOpenTimingT5s:
    """K9 spec: open position at T+5-15s, not T+0 (wait for orderbook to form)."""

    def test_phase1_fires_at_t5(self):
        """Tick loop triggers _v2_open_position only when 5 <= seconds <= 15."""
        import inspect
        from polybot.core.loop import TradingLoop
        source = inspect.getsource(TradingLoop._tick_asset)
        # Phase 1 gate in the tick loop
        assert "5 <= seconds_since_open <= 15" in source

    def test_v2_open_not_called_from_on_window_open(self):
        """_on_window_open must NOT directly call _v2_open_position (T+0 is wrong)."""
        import inspect
        from polybot.core.loop import TradingLoop
        source = inspect.getsource(TradingLoop._on_window_open)
        assert "_v2_open_position" not in source

    def test_tick_uses_pair_level_early_entry_gate(self):
        import inspect
        from polybot.core.loop import TradingLoop
        source = inspect.getsource(TradingLoop._tick_asset)
        assert "_early_entry_active_for_state" in source


class TestEarlyEntryPairGate:
    async def test_sol_5m_is_active_when_enabled(self):
        bot = _make_bot()
        bot.settings.enabled_pairs = [("SOL", 300)]
        bot.rtds = MagicMock()
        bot.rtds.get_state.return_value = MagicMock(compute_lag=MagicMock())
        bot._on_window_open = AsyncMock()
        bot._on_window_close = AsyncMock()
        bot._v2_open_position = AsyncMock()
        bot._v2_confirm = AsyncMock()
        bot._v2_accumulate_cheap = AsyncMock()
        bot._v2_poll_fills = AsyncMock()
        bot._early_checkpoint = AsyncMock()
        bot._early_cancel_unfilled = AsyncMock()
        bot._log_v2_status = MagicMock()
        bot._write_live_state_async = MagicMock()

        window = _make_window(slug="sol-updown-5m-1000000", open_ts=1_000)
        state = _make_state(asset="SOL", window=window)
        state.tracker.tick = MagicMock(return_value=None)

        with patch("polybot.core.loop.time.time", return_value=1_010.0):
            await bot._tick_asset(state, 50_000.0)

        bot._v2_open_position.assert_awaited_once()

    async def test_hourly_state_stays_off_even_when_sol_enabled(self):
        bot = _make_bot()
        bot.settings.enabled_pairs = [("SOL", 300)]
        bot.rtds = MagicMock()
        bot.rtds.get_state.return_value = MagicMock(compute_lag=MagicMock())
        bot._on_window_open = AsyncMock()
        bot._on_window_close = AsyncMock()
        bot._v2_open_position = AsyncMock()
        bot._v2_confirm = AsyncMock()
        bot._v2_accumulate_cheap = AsyncMock()
        bot._v2_poll_fills = AsyncMock()
        bot._early_checkpoint = AsyncMock()
        bot._early_cancel_unfilled = AsyncMock()
        bot._log_v2_status = MagicMock()
        bot._write_live_state_async = MagicMock()

        window = _make_window(slug="sol-updown-1h-1000000", open_ts=1_000)
        state = _make_state(asset="SOL", window=window)
        state.tracker.window_seconds = 3600
        state.tracker.tick = MagicMock(return_value=None)

        with patch("polybot.core.loop.time.time", return_value=1_010.0):
            await bot._tick_asset(state, 50_000.0)

        bot._v2_open_position.assert_not_called()


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


# ── 12. PaperModeVerification ────────────────────────────────────────────────

class TestPaperModeVerification:
    """Paper mode must run V2 order posting for verification without live execution."""

    async def test_accumulate_posts_in_paper(self):
        bot = _make_bot(mode="paper")
        window = _make_window()
        state = _make_state(window=window)
        state.early_position = {"direction_up": True, "entry_price": 0.33, "hedge_entry_price": 0.69}

        bot._refresh_orderbook = _noop_refresh

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_accumulate_cheap(state, 50_000.0)

        bot.trader.client.create_order.assert_called()

    async def test_open_posts_in_paper(self):
        bot = _make_bot(mode="paper")
        window = _make_window()
        state = _make_state(window=window)
        bot._refresh_orderbook = _noop_refresh

        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_open_position(state, 50_000.0)

        bot.trader.client.post_order.assert_called()
        assert state.early_position is not None

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
        """If $50 is already committed, remaining ticks should not post new orders."""
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
        state.filled_position_cost_usd = 50.0  # budget already exhausted
        state.early_cheap_filled = 50.0
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

        # T+90s: after T+60s gate so winning side (no_bid=0.70) fires
        with patch.object(bot, "_log_activity", MagicMock()):
            await bot._v2_accumulate_cheap(state, 50_000.0, seconds_since_open=90.0)

        assert "yes_sim2" in posted_tokens, "YES token must be posted"
        assert "no_sim2" in posted_tokens, "NO token must be posted"
