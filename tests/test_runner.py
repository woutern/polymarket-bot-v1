"""Tests for WindowRunner (core/runner.py).

Covers:
  - __post_init__: correct sub-component wiring
  - _predict: model_server=None fallback, uninitialised FB fallback, real path
  - _tick_loop: feature builder initialised on first Coinbase price
  - _commit: vol_history updated, prev_window set, result stored
  - Cross-window continuity via prev_window / vol_history
  - make_window_id: format
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polybot.core.runner import WindowRunner, make_window_id, _pair_to_asset
from polybot.ml.features import FeatureBuilder, PrevWindow
from polybot.core.controls import InMemoryControls
from polybot.storage.mm_store import InMemoryMMStore


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _mock_coinbase(price: float = 95000.0) -> MagicMock:
    cb = MagicMock()
    cb.get_price = MagicMock(return_value=price)
    cb.connect = AsyncMock()
    cb.close = AsyncMock()
    return cb


def _mock_feed() -> MagicMock:
    from polybot.strategy.base import MarketState
    feed = MagicMock()
    feed.connect = AsyncMock()
    feed.close = AsyncMock()
    feed.market_state = MagicMock(return_value=MarketState(
        seconds=30, yes_bid=0.52, no_bid=0.48, yes_ask=0.54, no_ask=0.50, prob_up=0.55
    ))
    return feed


def _runner(mode="paper", model_server=None, prev_window=None, vol_history=None, price=95000.0):
    cb = _mock_coinbase(price=price)
    runner = WindowRunner(
        pair="BTC_5M",
        yes_token_id="yes_tok",
        no_token_id="no_tok",
        window_id="BTC_5M_1234_abcd",
        window_open_ts=time.time() - 10,
        settings=MagicMock(),
        mode=mode,
        model_server=model_server,
        controls=InMemoryControls(),
        store=InMemoryMMStore(),
        prev_window=prev_window,
        vol_history=vol_history,
        coinbase_ws=cb,
    )
    runner.feed = _mock_feed()
    return runner


# ─── _pair_to_asset ───────────────────────────────────────────────────────────

class TestPairToAsset:
    def test_btc(self):
        assert _pair_to_asset("BTC_5M") == "BTC"

    def test_eth(self):
        assert _pair_to_asset("ETH_5M") == "ETH"

    def test_sol(self):
        assert _pair_to_asset("SOL_1H") == "SOL"

    def test_lowercase_normalised(self):
        assert _pair_to_asset("btc_5m") == "BTC"


# ─── make_window_id ──────────────────────────────────────────────────────────

class TestMakeWindowId:
    def test_contains_pair(self):
        wid = make_window_id("BTC_5M")
        assert wid.startswith("BTC_5M_")

    def test_unique(self):
        ids = {make_window_id("BTC_5M") for _ in range(50)}
        assert len(ids) == 50

    def test_timestamp_used(self):
        ts = 1_700_000_000.0
        wid = make_window_id("BTC_5M", ts=ts)
        assert "1700000000" in wid


# ─── __post_init__ wiring ────────────────────────────────────────────────────

class TestWindowRunnerInit:
    def test_paper_mode_uses_engine_client(self):
        runner = _runner(mode="paper")
        assert runner._order_client is runner.engine.client

    def test_asset_extracted_correctly(self):
        runner = _runner()
        assert runner._asset == "BTC"

    def test_feature_builder_created(self):
        runner = _runner()
        assert isinstance(runner._feature_builder, FeatureBuilder)

    def test_not_initialised_until_first_price(self):
        runner = _runner()
        assert runner._fb_initialised is False

    def test_injected_coinbase_not_owned(self):
        runner = _runner()
        assert runner._owns_coinbase is False

    def test_default_store_is_in_memory(self):
        runner = WindowRunner(
            pair="BTC_5M",
            yes_token_id="y",
            no_token_id="n",
            window_id="w",
            window_open_ts=time.time(),
            settings=MagicMock(),
            coinbase_ws=_mock_coinbase(),
        )
        assert isinstance(runner.store, InMemoryMMStore)

    def test_prev_window_passed_to_feature_builder(self):
        pw = PrevWindow(open_price=100.0, close_price=105.0)
        runner = _runner(prev_window=pw)
        assert runner._feature_builder.prev_window is pw

    def test_vol_history_shared(self):
        vh = deque([0.001], maxlen=20)
        runner = _runner(vol_history=vh)
        assert runner._feature_builder._vol_history is vh


# ─── _predict ────────────────────────────────────────────────────────────────

class TestPredict:
    def test_returns_half_when_no_model_server(self):
        runner = _runner(model_server=None)
        assert runner._predict(30) == 0.50

    def test_returns_half_when_fb_not_initialised(self):
        ms = MagicMock()
        ms.predict = MagicMock(return_value=0.70)
        runner = _runner(model_server=ms)
        assert runner._fb_initialised is False
        assert runner._predict(30) == 0.50

    def test_calls_model_server_after_init(self):
        ms = MagicMock()
        ms.predict = MagicMock(return_value=0.68)
        runner = _runner(model_server=ms, price=95000.0)

        # Simulate initialisation
        runner._feature_builder = FeatureBuilder(open_price=95000.0, window_open_ts=runner.window_open_ts)
        runner._fb_initialised = True

        result = runner._predict(30)
        assert ms.predict.called
        assert result == 0.68

    def test_falls_back_on_model_exception(self):
        ms = MagicMock()
        ms.predict = MagicMock(side_effect=Exception("model crashed"))
        runner = _runner(model_server=ms)
        runner._feature_builder = FeatureBuilder(open_price=95000.0, window_open_ts=runner.window_open_ts)
        runner._fb_initialised = True
        assert runner._predict(30) == 0.50

    def test_uses_yes_ask_from_feed(self):
        """_predict should use yes_ask as current_ask for features."""
        from polybot.strategy.base import MarketState
        ms = MagicMock()
        ms.predict = MagicMock(return_value=0.60)

        runner = _runner(model_server=ms)
        runner._feature_builder = FeatureBuilder(open_price=95000.0, window_open_ts=runner.window_open_ts)
        runner._fb_initialised = True

        # Feed returns yes_ask=0.72
        runner.feed.market_state = MagicMock(return_value=MarketState(
            seconds=30, yes_bid=0.70, no_bid=0.30, yes_ask=0.72, no_ask=0.32, prob_up=0.50
        ))

        runner._predict(30)
        call_kwargs = ms.predict.call_args
        features = call_kwargs[0][1]  # second positional arg
        assert features["signal_ask_price"] == pytest.approx(0.72, abs=0.01)


# ─── FeatureBuilder initialisation in tick loop ───────────────────────────────

class TestFeatureBuilderInitialisation:
    def test_first_price_reinitialises_fb(self):
        runner = _runner(price=95000.0)
        assert runner._fb_initialised is False

        # Simulate what _tick_loop does on first tick
        cb_price = runner.coinbase_ws.get_price(runner._asset)
        assert cb_price == 95000.0

        if not runner._fb_initialised:
            runner._feature_builder = FeatureBuilder(
                open_price=cb_price,
                window_open_ts=runner.window_open_ts,
                prev_window=runner.prev_window,
                vol_history=runner.vol_history,
            )
            runner._fb_initialised = True
        runner._feature_builder.on_price(cb_price, ts=time.time())

        assert runner._fb_initialised is True
        assert runner._feature_builder.open_price == 95000.0

    def test_zero_coinbase_price_skips_init(self):
        runner = _runner(price=0.0)
        cb_price = runner.coinbase_ws.get_price(runner._asset)
        if cb_price > 0:
            runner._fb_initialised = True  # should NOT happen
        assert runner._fb_initialised is False


# ─── _commit ─────────────────────────────────────────────────────────────────

class TestCommit:
    @pytest.mark.asyncio
    async def test_paper_commit_calls_engine_commit(self):
        runner = _runner(mode="paper")
        runner.engine.commit = MagicMock()
        await runner._commit()
        runner.engine.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_prev_window_set_after_commit(self):
        runner = _runner(price=95000.0)
        # Initialise FeatureBuilder
        runner._feature_builder = FeatureBuilder(open_price=95000.0, window_open_ts=runner.window_open_ts)
        runner._fb_initialised = True

        await runner._commit()
        # prev_window should be set from close price
        assert runner.prev_window is not None
        assert isinstance(runner.prev_window, PrevWindow)
        assert runner.prev_window.open_price == 95000.0
        assert runner.prev_window.close_price == 95000.0  # coinbase mock returns 95000

    @pytest.mark.asyncio
    async def test_prev_window_not_set_when_price_zero(self):
        runner = _runner(price=0.0)
        runner._feature_builder = FeatureBuilder(open_price=0.0, window_open_ts=runner.window_open_ts)
        runner._fb_initialised = True
        await runner._commit()
        assert runner.prev_window is None

    @pytest.mark.asyncio
    async def test_result_stored(self):
        runner = _runner()
        await runner._commit()
        assert runner.store.get_window(runner.window_id) is not None

    @pytest.mark.asyncio
    async def test_result_accessible_via_result_method(self):
        runner = _runner()
        await runner._commit()
        r = runner.result()
        assert r is not None


# ─── Cross-window vol_history propagation ────────────────────────────────────

class TestCrossWindowVolHistory:
    @pytest.mark.asyncio
    async def test_vol_history_updated_after_commit(self):
        vh = deque(maxlen=20)
        runner = _runner(vol_history=vh, price=95000.0)
        ts = runner.window_open_ts

        # Build one FeatureBuilder and feed all prices into it
        fb = FeatureBuilder(open_price=95000.0, window_open_ts=ts, vol_history=vh)
        for i in range(80):
            price = 95000.0 + (500.0 if i % 2 == 0 else -500.0)
            fb.on_price(price, ts=ts + i * 0.25)
        runner._feature_builder = fb
        runner._fb_initialised = True

        await runner._commit()
        assert len(vh) >= 1

    def test_vol_history_shared_across_two_runners(self):
        """Same deque instance used by both runners."""
        vh = deque(maxlen=20)
        r1 = _runner(vol_history=vh)
        r2 = _runner(vol_history=vh)
        assert r1._feature_builder._vol_history is vh
        assert r2._feature_builder._vol_history is vh


# ─── Kill switch stops tick loop ─────────────────────────────────────────────

class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_kill_switch_exits_loop(self):
        runner = _runner()
        runner.controls.kill_switch = True  # InMemoryControls — direct attribute
        await runner._tick_loop()
        # No exception — loop exited cleanly

    @pytest.mark.asyncio
    async def test_window_timer_300s_exits_loop(self):
        """When elapsed seconds >= 300, loop exits."""
        runner = _runner()
        # Set window_open_ts to 305 seconds ago
        runner.window_open_ts = time.time() - 305
        await runner._tick_loop()
        # Should exit without hanging


# ─── result() before run() ───────────────────────────────────────────────────

class TestResultBeforeRun:
    def test_result_none_before_run(self):
        runner = _runner()
        assert runner.result() is None


# ─── _predict edge cases ─────────────────────────────────────────────────────

class TestPredictEdgeCases:
    def test_yes_ask_zero_uses_fallback(self):
        """yes_ask=0 → current_ask falls back to 0.65."""
        from polybot.strategy.base import MarketState
        ms = MagicMock()
        ms.predict = MagicMock(return_value=0.60)

        runner = _runner(model_server=ms)
        runner._feature_builder = FeatureBuilder(open_price=95000.0, window_open_ts=runner.window_open_ts)
        runner._fb_initialised = True
        # yes_ask = 0 → should use 0.65 fallback
        runner.feed.market_state = MagicMock(return_value=MarketState(
            seconds=30, yes_bid=0.50, no_bid=0.50, yes_ask=0.0, no_ask=0.50, prob_up=0.50
        ))
        runner._predict(30)
        features = ms.predict.call_args[0][1]
        assert features["signal_ask_price"] == pytest.approx(0.65, abs=0.01)

    def test_feed_market_state_exception_returns_half(self):
        """If feed.market_state raises, _predict catches it and returns 0.50."""
        ms = MagicMock()
        ms.predict = MagicMock(return_value=0.70)
        runner = _runner(model_server=ms)
        runner._feature_builder = FeatureBuilder(open_price=95000.0, window_open_ts=runner.window_open_ts)
        runner._fb_initialised = True
        runner.feed.market_state = MagicMock(side_effect=Exception("feed exploded"))
        assert runner._predict(30) == 0.50


# ─── _execute_live ────────────────────────────────────────────────────────────

class TestExecuteLive:
    def _live_runner(self):
        runner = _runner()
        runner.mode = "live"
        client = MagicMock()
        client.post_buy = MagicMock(return_value="order_id_1")
        client.post_sell = MagicMock(return_value="order_id_2")
        runner._order_client = client
        return runner, client

    def _action(self, **kwargs):
        from polybot.strategy.base import StrategyAction
        defaults = dict(
            buy_up_shares=0, buy_up_price=0.0,
            buy_down_shares=0, buy_down_price=0.0,
            sell_up_shares=0, sell_up_price=0.0,
            sell_down_shares=0, sell_down_price=0.0,
            reason="test",
        )
        defaults.update(kwargs)
        return StrategyAction(**defaults)

    def test_buy_up_calls_post_buy_yes(self):
        runner, client = self._live_runner()
        action = self._action(buy_up_shares=10, buy_up_price=0.55)
        runner._execute_live(action)
        client.post_buy.assert_called_once_with("YES", 10, 0.55)

    def test_buy_down_calls_post_buy_no(self):
        runner, client = self._live_runner()
        action = self._action(buy_down_shares=8, buy_down_price=0.48)
        runner._execute_live(action)
        client.post_buy.assert_called_once_with("NO", 8, 0.48)

    def test_sell_up_calls_post_sell_yes(self):
        runner, client = self._live_runner()
        action = self._action(sell_up_shares=5, sell_up_price=0.70)
        runner._execute_live(action)
        client.post_sell.assert_called_once_with("YES", 5, 0.70)

    def test_sell_down_calls_post_sell_no(self):
        runner, client = self._live_runner()
        action = self._action(sell_down_shares=5, sell_down_price=0.30)
        runner._execute_live(action)
        client.post_sell.assert_called_once_with("NO", 5, 0.30)

    def test_zero_shares_skips_post_buy(self):
        runner, client = self._live_runner()
        action = self._action(buy_up_shares=0, buy_up_price=0.55)
        runner._execute_live(action)
        client.post_buy.assert_not_called()

    def test_zero_price_skips_post_buy(self):
        runner, client = self._live_runner()
        action = self._action(buy_up_shares=10, buy_up_price=0.0)
        runner._execute_live(action)
        client.post_buy.assert_not_called()

    def test_post_buy_returns_none_no_crash(self):
        runner, client = self._live_runner()
        client.post_buy = MagicMock(return_value=None)
        action = self._action(buy_up_shares=10, buy_up_price=0.55)
        runner._execute_live(action)  # must not raise

    def test_post_sell_returns_none_no_crash(self):
        runner, client = self._live_runner()
        client.post_sell = MagicMock(return_value=None)
        action = self._action(sell_up_shares=5, sell_up_price=0.70)
        runner._execute_live(action)  # must not raise


# ─── _sync_live_fills ─────────────────────────────────────────────────────────

class TestSyncLiveFills:
    def _make_order(self, status, token, side, filled_shares, price=0.55, filled_price=None, synced=False):
        o = MagicMock()
        o.status = status
        o.token = token
        o.side = side
        o.filled_shares = filled_shares
        o.price = price
        o.filled_price = filled_price
        o._synced = synced
        return o

    def _live_runner_with_orders(self, orders: dict):
        runner = _runner()
        runner.mode = "live"
        client = MagicMock()
        client.orders = orders
        runner._order_client = client
        return runner

    def test_terminal_buy_fill_applied_to_position(self):
        from polybot.execution.mm_live_client import _TERMINAL
        terminal = next(iter(_TERMINAL))
        order = self._make_order(terminal, "YES", "BUY", filled_shares=10, price=0.55)
        runner = self._live_runner_with_orders({"o1": order})
        runner.engine.position.buy = MagicMock()
        runner._sync_live_fills()
        runner.engine.position.buy.assert_called_once_with(True, 10, 0.55)

    def test_terminal_sell_fill_applied_to_position(self):
        from polybot.execution.mm_live_client import _TERMINAL
        terminal = next(iter(_TERMINAL))
        order = self._make_order(terminal, "YES", "SELL", filled_shares=5, price=0.70)
        runner = self._live_runner_with_orders({"o1": order})
        runner.engine.position.sell = MagicMock()
        runner._sync_live_fills()
        runner.engine.position.sell.assert_called_once_with(True, 5, 0.70)

    def test_non_terminal_order_skipped(self):
        order = self._make_order("OPEN", "YES", "BUY", filled_shares=10)
        runner = self._live_runner_with_orders({"o1": order})
        runner.engine.position.buy = MagicMock()
        runner._sync_live_fills()
        runner.engine.position.buy.assert_not_called()

    def test_zero_filled_shares_skipped(self):
        from polybot.execution.mm_live_client import _TERMINAL
        terminal = next(iter(_TERMINAL))
        order = self._make_order(terminal, "YES", "BUY", filled_shares=0)
        runner = self._live_runner_with_orders({"o1": order})
        runner.engine.position.buy = MagicMock()
        runner._sync_live_fills()
        runner.engine.position.buy.assert_not_called()

    def test_already_synced_order_not_double_counted(self):
        from polybot.execution.mm_live_client import _TERMINAL
        terminal = next(iter(_TERMINAL))
        order = self._make_order(terminal, "YES", "BUY", filled_shares=10, synced=True)
        runner = self._live_runner_with_orders({"o1": order})
        runner.engine.position.buy = MagicMock()
        runner._sync_live_fills()
        runner.engine.position.buy.assert_not_called()

    def test_filled_price_used_over_limit_price(self):
        from polybot.execution.mm_live_client import _TERMINAL
        terminal = next(iter(_TERMINAL))
        order = self._make_order(terminal, "NO", "BUY", filled_shares=8, price=0.48, filled_price=0.47)
        runner = self._live_runner_with_orders({"o1": order})
        runner.engine.position.buy = MagicMock()
        runner._sync_live_fills()
        runner.engine.position.buy.assert_called_once_with(False, 8, 0.47)  # filled_price wins

    def test_no_order_is_token_yes_maps_to_is_up_false(self):
        from polybot.execution.mm_live_client import _TERMINAL
        terminal = next(iter(_TERMINAL))
        order = self._make_order(terminal, "NO", "BUY", filled_shares=6, price=0.48)
        runner = self._live_runner_with_orders({"o1": order})
        runner.engine.position.buy = MagicMock()
        runner._sync_live_fills()
        runner.engine.position.buy.assert_called_once_with(False, 6, 0.48)


# ─── _commit live mode ────────────────────────────────────────────────────────

class TestCommitLiveMode:
    @pytest.mark.asyncio
    async def test_live_commit_calls_cancel_all(self):
        runner = _runner()
        runner.mode = "live"
        client = MagicMock()
        client.cancel_all = MagicMock(return_value=3)
        runner._order_client = client
        await runner._commit()
        client.cancel_all.assert_called_once()

    @pytest.mark.asyncio
    async def test_live_commit_does_not_call_engine_commit(self):
        runner = _runner()
        runner.mode = "live"
        client = MagicMock()
        client.cancel_all = MagicMock(return_value=0)
        runner._order_client = client
        runner.engine.commit = MagicMock()
        await runner._commit()
        runner.engine.commit.assert_not_called()


# ─── _owns_coinbase — internal creation ───────────────────────────────────────

class TestOwnsCoinbase:
    def test_creates_coinbase_ws_when_none_injected(self):
        runner = WindowRunner(
            pair="BTC_5M",
            yes_token_id="y",
            no_token_id="n",
            window_id="w",
            window_open_ts=time.time(),
            settings=MagicMock(),
            # coinbase_ws NOT injected
        )
        assert runner._owns_coinbase is True
        from polybot.feeds.coinbase_ws import CoinbaseWS
        assert isinstance(runner.coinbase_ws, CoinbaseWS)

    def test_injected_coinbase_not_owned(self):
        cb = _mock_coinbase()
        runner = WindowRunner(
            pair="BTC_5M",
            yes_token_id="y",
            no_token_id="n",
            window_id="w",
            window_open_ts=time.time(),
            settings=MagicMock(),
            coinbase_ws=cb,
        )
        assert runner._owns_coinbase is False
        assert runner.coinbase_ws is cb
