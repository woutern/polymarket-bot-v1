"""MarketMaker window runner — async loop per active window.

Ties together:
  MMOrderbookWS    → live YES/NO bid/ask every message (~100-500ms)
  CoinbaseWS       → real BTC/ETH/SOL/XRP prices for feature computation
  FeatureBuilder   → accumulates Coinbase prices, builds 14 LightGBM features
  ModelServer      → LightGBM predict → prob_up each tick
  Engine           → strategy decisions each tick
  MMLiveClient     → real GTC orders on Polymarket CLOB
  MMStore          → DynamoDB logging (ticks, window, position)
  BotControls      → kill switch / pause flag

One WindowRunner per active trading window. Spawned by the top-level
bot process when a new 5-minute window opens.

Lifecycle:
  1. __init__     — set up feed + engine
  2. run()        — connect WebSockets, tick every second, commit at end
  3. result()     — return WindowResult for storage / dashboard

Paper mode (MODE=paper):
  - Uses MMPaperClient instead of MMLiveClient (no real orders)
  - Both WebSocket feeds still connect for real prices
  - Set mode="paper" in Settings or pass mode="paper" directly

Usage:
    runner = WindowRunner(
        pair="BTC_5M",
        yes_token_id=...,
        no_token_id=...,
        window_id=...,
        window_open_ts=...,  # Unix timestamp of window open
        settings=settings,
        mode="live",             # or "paper"
        model_server=server,     # ModelServer (optional; falls back to 0.50)
        controls=controls,
        store=store,
    )
    await runner.run()
    result = runner.result()
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field

from polybot.core.controls import BotControls, InMemoryControls
from polybot.core.engine import Engine, WindowResult
from polybot.execution.mm_paper_client import MMPaperClient
from polybot.feeds.coinbase_ws import CoinbaseWS
from polybot.feeds.mm_orderbook_ws import MMOrderbookWS
from polybot.ml.features import FeatureBuilder, PrevWindow
from polybot.storage.mm_store import InMemoryMMStore, MMStore
from polybot.strategy.profiles import get_profile

logger = logging.getLogger(__name__)

_TICK_INTERVAL = 1.0   # seconds between engine ticks
_WINDOW_SECONDS = 300  # 5-minute window duration

# Extract asset name from pair key, e.g. "BTC_5M" → "BTC"
def _pair_to_asset(pair: str) -> str:
    return pair.split("_")[0].upper()


@dataclass
class WindowRunner:
    """Async runner for a single 5-minute market-maker window.

    Args:
        pair:            Profile key, e.g. "BTC_5M"
        yes_token_id:    Polymarket YES token ID for this window
        no_token_id:     Polymarket NO token ID for this window
        window_id:       Unique ID for this window (used as DynamoDB PK)
        window_open_ts:  Unix timestamp when the window opened
        settings:        Bot settings (API keys, chain ID, mode)
        mode:            "paper" or "live"
        model_server:    ModelServer instance; falls back to 0.50 if None
        controls:        BotControls or InMemoryControls
        store:           MMStore or InMemoryMMStore
        prev_window:     PrevWindow from the preceding window (for features)
        vol_history:     Shared vol deque passed across windows (for vol_ratio)
        coinbase_ws:     Shared CoinbaseWS instance; created internally if None
    """

    pair: str
    yes_token_id: str
    no_token_id: str
    window_id: str
    window_open_ts: float
    settings: object
    mode: str = "paper"
    model_server: object = None
    controls: object = None
    store: object = None
    prev_window: object = None      # PrevWindow | None
    vol_history: object = None      # deque | None — shared across windows
    coinbase_ws: object = None      # CoinbaseWS | None — injected or auto-created

    def __post_init__(self):
        if self.controls is None:
            self.controls = InMemoryControls()
        if self.store is None:
            self.store = InMemoryMMStore()

        profile = get_profile(self.pair)
        self._asset = _pair_to_asset(self.pair)

        # Polymarket orderbook feed (YES/NO bid/ask)
        self.feed = MMOrderbookWS(
            yes_token_id=self.yes_token_id,
            no_token_id=self.no_token_id,
        )

        # Coinbase price feed for feature computation
        # Caller may inject a shared instance; if not, we create one per window
        self._owns_coinbase = self.coinbase_ws is None
        if self._owns_coinbase:
            self.coinbase_ws = CoinbaseWS(assets=[self._asset])

        # Feature builder — computes the 14 LightGBM features from live prices
        self._feature_builder = FeatureBuilder(
            open_price=0.0,          # updated on first Coinbase tick
            window_open_ts=self.window_open_ts,
            prev_window=self.prev_window,
            vol_history=self.vol_history,
        )
        self._fb_initialised = False  # True once first real price arrives

        # Engine (always uses paper client internally for position tracking)
        self.engine = Engine(
            pair=self.pair,
            mode=self.mode,
            controls=self.controls,
            profile=profile,
        )

        # Order client — swap paper/live
        if self.mode == "live":
            from polybot.execution.mm_live_client import MMLiveClient
            self._order_client = MMLiveClient(
                yes_token_id=self.yes_token_id,
                no_token_id=self.no_token_id,
                settings=self.settings,
            )
        else:
            # Paper mode: reuse the engine's internal paper client
            self._order_client = self.engine.client

        self._feed_task: asyncio.Task | None = None
        self._coinbase_task: asyncio.Task | None = None
        self._done = False
        self._result: WindowResult | None = None

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Connect feeds, tick every second, commit when window ends."""
        # Start Polymarket orderbook feed in background
        self._feed_task = asyncio.create_task(self.feed.connect())

        # Start Coinbase price feed (only if we own the instance)
        if self._owns_coinbase:
            self._coinbase_task = asyncio.create_task(self.coinbase_ws.connect())

        # Brief pause to let first snapshots arrive
        await asyncio.sleep(0.5)

        try:
            await self._tick_loop()
        finally:
            await self._commit()
            await self.feed.close()
            if self._owns_coinbase:
                await self.coinbase_ws.close()

            for task in (self._feed_task, self._coinbase_task):
                if task and not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        self._done = True
        logger.info(
            "window_runner_done window_id=%s pair=%s net_cost=%.2f combined_avg=%.4f is_gp=%s",
            self.window_id, self.pair,
            self.engine.position.net_cost,
            self.engine.position.combined_avg,
            self.engine.position.combined_avg < 1.0 and self.engine.position.payout_floor > 0,
        )

    async def _tick_loop(self) -> None:
        """Tick every second until the window ends or kill switch fires."""
        while True:
            # Kill switch check
            if self.controls.kill_switch:
                logger.warning("runner_kill_switch %s", self.window_id)
                break

            now = time.time()
            seconds = int(now - self.window_open_ts)

            # Window over
            if seconds >= _WINDOW_SECONDS:
                break

            # Feed latest Coinbase price into FeatureBuilder
            cb_price = self.coinbase_ws.get_price(self._asset)
            if cb_price > 0:
                if not self._fb_initialised:
                    # Re-initialise with the real open price on first tick
                    self._feature_builder = FeatureBuilder(
                        open_price=cb_price,
                        window_open_ts=self.window_open_ts,
                        prev_window=self.prev_window,
                        vol_history=self.vol_history,
                    )
                    self._fb_initialised = True
                self._feature_builder.on_price(cb_price, ts=now)

            # Build MarketState from live orderbook + model
            prob_up = self._predict(seconds)
            state = self.feed.market_state(seconds=seconds, prob_up=prob_up)

            # Engine tick → action
            action = self.engine.run_tick(state)

            # Verbose tick log — shows every decision when debug logging is on
            if logger.isEnabledFor(logging.DEBUG):
                pos = self.engine.position
                act_str = ""
                if action.buy_up_shares:
                    act_str += f" BUY_UP={action.buy_up_shares}@{action.buy_up_price:.3f}"
                if action.buy_down_shares:
                    act_str += f" BUY_DN={action.buy_down_shares}@{action.buy_down_price:.3f}"
                if action.sell_up_shares:
                    act_str += f" SELL_UP={action.sell_up_shares}@{action.sell_up_price:.3f}"
                if action.sell_down_shares:
                    act_str += f" SELL_DN={action.sell_down_shares}@{action.sell_down_price:.3f}"
                if not act_str:
                    act_str = f" HOLD reason={action.reason or '-'}"
                logger.debug(
                    "tick t=%d yes=%.3f/%.3f no=%.3f/%.3f up=%d@%.3f dn=%d@%.3f cost=%.2f%s",
                    seconds,
                    state.yes_bid, state.yes_ask,
                    state.no_bid, state.no_ask,
                    pos.up_shares, pos.up_avg,
                    pos.down_shares, pos.down_avg,
                    pos.net_cost,
                    act_str,
                )

            # Execute action via order client (live or paper)
            if self.mode == "live" and action.has_action():
                self._execute_live(action)

            # Sync fills from live client back into engine position
            if self.mode == "live":
                self._sync_live_fills()

            # Log tick to store
            result_so_far = self.engine.window_result()
            if result_so_far.tick_log:
                tick = result_so_far.tick_log[-1]
                self.store.put_tick(self.window_id, tick)

            # Update live position snapshot every 5 ticks
            if seconds % 5 == 0:
                snap = self.engine._position_snapshot()
                self.store.put_position(self.window_id, snap)

            # Sleep until next tick
            await asyncio.sleep(_TICK_INTERVAL)

    async def _commit(self) -> None:
        """Cancel open orders, close FeatureBuilder, store final window result."""
        # Cancel all open orders
        if self.mode == "live":
            cancelled = self._order_client.cancel_all()
            logger.info("runner_commit_cancelled count=%d window_id=%s", cancelled, self.window_id)
        else:
            self.engine.commit()

        # Close FeatureBuilder: updates vol_history for next window's vol_ratio
        if self._fb_initialised:
            close_price = self.coinbase_ws.get_price(self._asset)
            if close_price > 0:
                self.prev_window = self._feature_builder.close(close_price)

        # Store window result
        result = self.engine.window_result()
        self._result = result
        self.store.put_window(self.window_id, result)

    # ------------------------------------------------------------------
    # Live order execution
    # ------------------------------------------------------------------

    def _execute_live(self, action) -> None:
        """Translate StrategyAction into real CLOB orders."""
        from polybot.execution.mm_live_client import MMLiveClient
        client: MMLiveClient = self._order_client

        if action.buy_up_shares > 0 and action.buy_up_price > 0:
            oid = client.post_buy("YES", action.buy_up_shares, action.buy_up_price)
            if oid:
                logger.info("live_buy_up oid=%s shares=%d price=%.4f", oid, action.buy_up_shares, action.buy_up_price)

        if action.buy_down_shares > 0 and action.buy_down_price > 0:
            oid = client.post_buy("NO", action.buy_down_shares, action.buy_down_price)
            if oid:
                logger.info("live_buy_dn oid=%s shares=%d price=%.4f", oid, action.buy_down_shares, action.buy_down_price)

        if action.sell_up_shares > 0 and action.sell_up_price > 0:
            oid = client.post_sell("YES", action.sell_up_shares, action.sell_up_price)
            if oid:
                logger.info("live_sell_up oid=%s shares=%d price=%.4f", oid, action.sell_up_shares, action.sell_up_price)

        if action.sell_down_shares > 0 and action.sell_down_price > 0:
            oid = client.post_sell("NO", action.sell_down_shares, action.sell_down_price)
            if oid:
                logger.info("live_sell_dn oid=%s shares=%d price=%.4f", oid, action.sell_down_shares, action.sell_down_price)

    def _sync_live_fills(self) -> None:
        """Check live orders for fills and apply to engine position."""
        from polybot.execution.mm_live_client import MMLiveClient, _TERMINAL
        client: MMLiveClient = self._order_client

        for order in list(client.orders.values()):
            if order.status in _TERMINAL and order.filled_shares > 0:
                if not getattr(order, "_synced", False):
                    is_up = order.token == "YES"
                    if order.side == "BUY":
                        self.engine.position.buy(is_up, order.filled_shares, order.filled_price or order.price)
                    else:
                        self.engine.position.sell(is_up, order.filled_shares, order.filled_price or order.price)
                    order._synced = True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _elapsed_seconds(self) -> int:
        return int(time.time() - self.window_open_ts)

    def _predict(self, seconds: int) -> float:
        """Compute 14 LightGBM features and return prob_up. Falls back to 0.50."""
        if self.model_server is None:
            return 0.50
        if not self._fb_initialised:
            return 0.50
        try:
            # Current YES ask price from Polymarket orderbook
            state = self.feed.market_state(seconds=seconds)
            current_ask = state.yes_ask if state.yes_ask > 0 else 0.65

            features = self._feature_builder.compute(
                current_ask=current_ask,
                seconds=seconds,
            )
            prob = self.model_server.predict(self.pair, features)
            return prob
        except Exception as exc:
            logger.debug("runner_predict_failed %s", str(exc)[:60])
            return 0.50

    def result(self) -> WindowResult | None:
        """Return the final WindowResult (available after run() completes)."""
        return self._result


# ─── Convenience factory ────────────────────────────────────────────────────

def make_window_id(pair: str, ts: float | None = None) -> str:
    """Generate a unique window ID. Format: BTC_5M_<timestamp>_<short_uuid>"""
    ts = ts or time.time()
    short = uuid.uuid4().hex[:8]
    return f"{pair}_{int(ts)}_{short}"
