"""MMLoop — top-level window discovery + runner spawning for the MarketMaker bot.

Runs one WindowRunner per 5-minute window, chaining prev_window and vol_history
between consecutive windows. Shared CoinbaseWS is kept alive across windows.

Lifecycle:
    1. Wait until the current window has enough time left (>= 10s from now)
    2. Resolve YES/NO token IDs from Gamma API
    3. Spawn WindowRunner in paper (or live) mode
    4. When runner finishes, chain prev_window + vol_history to next window
    5. Wait for next window boundary and repeat

Usage:
    loop = MMLoop(pair="BTC_5M", mode="paper", budget_override=20.0)
    await loop.run()
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from collections import deque

from polybot.core.clock import current_window_open, WINDOW_SECONDS
from polybot.core.controls import InMemoryControls
from polybot.core.runner import WindowRunner, make_window_id
from polybot.feeds.coinbase_ws import CoinbaseWS
from polybot.market.market_resolver import resolve_window
from polybot.models import Window
from polybot.storage.mm_store import InMemoryMMStore

logger = logging.getLogger(__name__)

# If fewer than this many seconds remain in the current window, skip it and wait
# for the next one. We need at least a few seconds to resolve the market + connect.
_MIN_SECONDS_TO_START = 60  # need at least 60s to trade meaningfully


def _pair_to_asset(pair: str) -> str:
    return pair.split("_")[0].upper()


class MMLoop:
    """Discovers and runs MarketMaker windows sequentially.

    Args:
        pair:            Profile key, e.g. "BTC_5M"
        mode:            "paper" or "live"
        budget_override: Override profile budget (e.g. 20.0 for a small test)
        model_server:    ModelServer instance; falls back to 0.50 if None
        controls:        BotControls or InMemoryControls; defaults to InMemoryControls
        store:           MMStore; defaults to InMemoryMMStore
        max_windows:     Stop after this many windows (None = run forever)
    """

    def __init__(
        self,
        pair: str = "BTC_5M",
        mode: str = "paper",
        budget_override: float | None = None,
        model_server: object = None,
        controls: object = None,
        store: object = None,
        max_windows: int | None = None,
        settings: object = None,
    ):
        self.pair = pair
        self.mode = mode
        self.budget_override = budget_override
        self.model_server = model_server
        self.controls = controls or InMemoryControls()
        self.store = store or InMemoryMMStore()
        self.max_windows = max_windows
        self.settings = settings or _MinimalSettings()

        self._asset = _pair_to_asset(pair)
        self._coinbase_ws: CoinbaseWS | None = None
        self._coinbase_task: asyncio.Task | None = None

        # Cross-window continuity
        self._prev_window = None           # PrevWindow | None
        self._vol_history: deque = deque(maxlen=20)

        self._windows_run = 0
        self._stop = False
        self._results = []

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self) -> list:
        """Start the MM loop. Returns list of WindowResults when done."""
        logger.info("mm_loop_start pair=%s mode=%s budget_override=%s", self.pair, self.mode, self.budget_override)

        # Start shared Coinbase feed once — reused across all windows
        self._coinbase_ws = CoinbaseWS(assets=[self._asset])
        self._coinbase_task = asyncio.create_task(self._coinbase_ws.connect())

        # Brief warm-up so first price arrives before first window
        await asyncio.sleep(1.0)

        try:
            await self._loop()
        finally:
            if self._coinbase_task and not self._coinbase_task.done():
                self._coinbase_task.cancel()
                try:
                    await self._coinbase_task
                except asyncio.CancelledError:
                    pass
            await self._coinbase_ws.close()

        logger.info("mm_loop_done windows_run=%d", self._windows_run)
        return self._results

    # ------------------------------------------------------------------
    # Inner loop
    # ------------------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stop:
            # Kill switch
            if self.controls.kill_switch:
                logger.warning("mm_loop_kill_switch")
                break

            # Max windows
            if self.max_windows is not None and self._windows_run >= self.max_windows:
                logger.info("mm_loop_max_windows reached=%d", self._windows_run)
                break

            # Pause flag — wait and check again
            if self.controls.pause_new_windows:
                logger.info("mm_loop_paused waiting 10s")
                await asyncio.sleep(10)
                continue

            # Determine current window timing
            window_open_ts = current_window_open()
            window_close_ts = window_open_ts + WINDOW_SECONDS
            seconds_left = window_close_ts - time.time()

            if seconds_left < _MIN_SECONDS_TO_START:
                # Too close to the end — wait for the next window
                wait = seconds_left + 1.0
                logger.info("mm_loop_skip_window seconds_left=%.1f waiting=%.1f", seconds_left, wait)
                await asyncio.sleep(wait)
                continue

            # Resolve token IDs for this window
            window = Window(
                open_ts=window_open_ts,
                close_ts=window_close_ts,
                asset=self._asset,
            )
            try:
                window = await resolve_window(window)
            except Exception as exc:
                logger.error("mm_loop_resolve_failed: %s — skipping window", str(exc)[:80])
                await asyncio.sleep(5)
                continue

            if not window.yes_token_id or not window.no_token_id:
                logger.warning("mm_loop_no_tokens slug=%s — skipping window", window.slug)
                # Wait until current window ends, then try next
                await asyncio.sleep(seconds_left + 1.0)
                continue

            # Apply budget override to profile
            if self.budget_override is not None:
                self._apply_budget_override()

            # Build runner
            window_id = make_window_id(self.pair, ts=window_open_ts)
            runner = WindowRunner(
                pair=self.pair,
                yes_token_id=window.yes_token_id,
                no_token_id=window.no_token_id,
                window_id=window_id,
                window_open_ts=float(window_open_ts),
                settings=self.settings,
                mode=self.mode,
                model_server=self.model_server,
                controls=self.controls,
                store=self.store,
                prev_window=self._prev_window,
                vol_history=self._vol_history,
                coinbase_ws=self._coinbase_ws,
            )

            logger.info(
                "mm_loop_window_start window_id=%s slug=%s seconds_left=%.1f",
                window_id, window.slug, seconds_left,
            )

            # Run window to completion
            await runner.run()
            self._windows_run += 1

            # Chain continuity data to next window
            if runner.prev_window is not None:
                self._prev_window = runner.prev_window

            result = runner.result()
            if result:
                self._results.append(result)
                _log_result(result, window_id)

            # Heartbeat for ECS health check
            try:
                with open("/tmp/heartbeat", "w") as _hb:
                    _hb.write(str(time.time()))
            except OSError:
                pass

            # Brief pause before re-checking — the top-of-loop seconds_left guard
            # will enter the current window if enough time remains, or skip to next.
            await asyncio.sleep(0.5)

    # ------------------------------------------------------------------
    # Budget override
    # ------------------------------------------------------------------

    def _apply_budget_override(self) -> None:
        """Temporarily patch the profile budget for this loop's duration."""
        from polybot.strategy.profiles import get_profile
        profile = get_profile(self.pair)
        if profile.budget != self.budget_override:
            logger.info("mm_loop_budget_override %.2f → %.2f", profile.budget, self.budget_override)
            profile.budget = self.budget_override

    def stop(self) -> None:
        """Signal the loop to stop after the current window finishes."""
        self._stop = True


# ------------------------------------------------------------------
# Minimal settings stub for local use
# ------------------------------------------------------------------

class _MinimalSettings:
    """No-op settings object for local paper mode (no Polymarket creds needed)."""
    polymarket_private_key = ""
    polymarket_api_key = ""
    polymarket_api_secret = ""
    polymarket_api_passphrase = ""
    polymarket_chain_id = 137
    polymarket_funder = ""
    mode = "paper"
    pairs = "BTC_5M"


# ------------------------------------------------------------------
# Result logging
# ------------------------------------------------------------------

def _log_result(result, window_id: str) -> None:
    logger.info(
        "mm_window_result window_id=%s up_shares=%d dn_shares=%d "
        "net_cost=%.2f combined_avg=%.4f payout_floor=%d profitable=%s",
        window_id,
        result.up_shares,
        result.down_shares,
        result.net_cost,
        result.combined_avg,
        result.payout_floor,
        result.is_guaranteed_profit,
    )
