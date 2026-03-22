"""Main async event loop — multi-asset directional trading.

Strategy: Early entry (T+2s to T+15s) across BTC/ETH/SOL
on 5-minute windows with LightGBM + quality filters.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from dataclasses import dataclass, field
from functools import partial

import structlog

from polybot.config import Settings
from polybot.execution.live_trader import LiveTrader
from polybot.execution.paper_trader import PaperTrader
from polybot.feeds.coinbase_ws import CoinbaseWS
from polybot.feeds.polymarket_rest import get_orderbook
from polybot.market.market_resolver import resolve_window
from polybot.market.window_tracker import WindowState, WindowTracker
from polybot.models import Direction, OrderbookSnapshot, Window
from polybot.risk.manager import RiskManager
from polybot.storage.db import Database
from polybot.storage.dynamo import DynamoStore
from polybot.feeds.rtds_ws import RTDSClient, compute_oracle_probability, compute_realized_vol
from polybot.strategy.base_rate import BaseRateTable
from polybot.strategy.bayesian import BayesianUpdater
from polybot.strategy.directional import generate_directional_signal

logger = structlog.get_logger()

# Resolve the claim_winnings script path relative to this file so it works
# regardless of the working directory.
_SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts")

def _import_claim_all():
    """Lazily import claim_all from scripts/claim_winnings.py."""
    import importlib.util
    script_path = os.path.normpath(os.path.join(_SCRIPTS_DIR, "claim_winnings.py"))
    spec = importlib.util.spec_from_file_location("claim_winnings", script_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.claim_all

S3_BUCKET = "polymarket-bot-data-688567279867-euw1"

# Per-asset parquet config: asset symbol -> (local path, S3 key, /tmp fallback)
_ASSET_PARQUET: dict[str, tuple[str, str, str]] = {
    "BTC": ("data/candles/btc_usd_1min.parquet", "candles/btc_usd_1min.parquet", "/tmp/btc_usd_1min.parquet"),
    "ETH": ("data/candles/eth_usd_1min.parquet", "candles/eth_usd_1min.parquet", "/tmp/eth_usd_1min.parquet"),
    "SOL": ("data/candles/sol_usd_1min.parquet", "candles/sol_usd_1min.parquet", "/tmp/sol_usd_1min.parquet"),
}


@dataclass
class AssetState:
    """Per-asset tracking state."""

    asset: str
    tracker: WindowTracker
    bayesian: BayesianUpdater
    orderbook: OrderbookSnapshot = field(default_factory=OrderbookSnapshot)
    prev_open_ts: int | None = None
    prev_window: Window | None = None
    traded_this_window: bool = False
    orderbook_age: float = 0.0
    price_history: deque = field(default_factory=lambda: deque(maxlen=200))  # for realized vol
    vol_history: deque = field(default_factory=lambda: deque(maxlen=12))  # rolling vol for vol_ma_1h
    window_high: float = 0.0  # track high/low within first 15s for body_ratio
    window_low: float = float("inf")
    window_tick_count: int = 0
    prior_window_tick_counts: deque = field(default_factory=lambda: deque(maxlen=5))
    late_entry_evaluated: bool = False
    # Scan window state (T+210s to T+255s — find best entry price)
    scan_active: bool = False
    scan_best_ask: float | None = None
    scan_best_ask_ts: float | None = None
    scan_direction: str | None = None  # "up" or "down"
    scan_direction_flipped: bool = False
    scan_last_checked: float | None = None
    # Early entry state (T+14-18s, independent strategy)
    early_entry_evaluated: bool = False
    early_entry_traded: bool = False
    # Early entry position tracking (for checkpoints at T+60/120/180s)
    early_position: dict | None = None  # {slug, token_id, shares, entry_price, direction_up, side}
    early_checkpoints_done: set = field(default_factory=set)  # {60, 120, 180}
    # DCA + hedge tracking
    early_dca_orders: list = field(default_factory=list)   # [{order_id, side, price, size, filled}]
    early_hedge_order_id: str | None = None
    early_main_filled: float = 0.0    # total USD filled on main side
    early_hedge_filled: float = 0.0   # total USD filled on hedge side
    early_dca_done: set = field(default_factory=set)  # {15, 45, 90} — which DCA rounds fired
    # Per-side share tracking (for stop-and-rotate)
    early_up_shares: float = 0.0
    early_up_cost: float = 0.0
    early_down_shares: float = 0.0
    early_down_cost: float = 0.0
    early_rotate_done: set = field(default_factory=set)  # which 30s intervals posted cheap limits
    early_activity_log: list = field(default_factory=list)  # last 20 actions for dashboard
    early_accum_ticks: set = field(default_factory=set)   # which 3s ticks fired accumulation
    early_status_logged: set = field(default_factory=set)  # which 15s intervals logged status
    early_cheap_posted: float = 0.0   # total USD posted for cheap accumulation (budget cap)
    early_cheap_filled: float = 0.0   # total USD actually filled (fill-based budget cap)
    early_confirm_done: bool = False  # Phase 2 T+15s confirm fired for this window


# ── Binance long/short ratio bias (free, no API key) ─────────────────────────

_liq_cache: dict[str, tuple[float, float]] = {}  # symbol → (bias, timestamp)
_LIQ_CACHE_TTL = 60  # 1 min cache

_BINANCE_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT"}


async def fetch_liq_cluster_bias(symbol: str = "BTC") -> float:
    """Fetch Binance global long/short account ratio and compute bias.

    Uses the free public endpoint — no API key needed.
    Compares latest ratio to 5-period average to detect shifts.

    Positive = shorts crowded (bullish squeeze potential)
    Negative = longs crowded (bearish squeeze potential)
    Range: roughly -0.5 to +0.5
    Returns 0.0 on error.
    """
    cached = _liq_cache.get(symbol)
    if cached and time.time() - cached[1] < _LIQ_CACHE_TTL:
        return cached[0]

    try:
        import httpx
        binance_sym = _BINANCE_SYMBOLS.get(symbol, f"{symbol}USDT")
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(
                "https://fapi.binance.com/futures/data/globalLongShortAccountRatio",
                params={"symbol": binance_sym, "period": "5m", "limit": 5},
            )
            if resp.status_code != 200:
                return _liq_cache.get(symbol, (0.0, 0))[0]
            data = resp.json()
            if not data:
                return 0.0
            # Latest long/short ratio
            latest = data[-1]
            long_pct = float(latest.get("longAccount", 0.5))
            short_pct = float(latest.get("shortAccount", 0.5))
            # Bias: positive = more shorts (bullish), negative = more longs (bearish)
            # Normalize: at 50/50 → 0.0, at 60/40 long → -0.2, at 40/60 long → +0.2
            bias = short_pct - long_pct  # range: -1 to +1
            _liq_cache[symbol] = (bias, time.time())
            return bias
    except Exception:
        return _liq_cache.get(symbol, (0.0, 0))[0]


class TradingLoop:
    """Multi-asset, multi-timeframe directional trading bot.

    Single strategy: early entry (T+2s to T+15s) with LightGBM + filters.
    Uses Bayesian updater + historical base rates to estimate P(UP).
    Quarter-Kelly sizing capped at $1 per trade.
    """

    @staticmethod
    def _load_base_rate_for(asset: str) -> BaseRateTable:
        """Load historical base rate table for a single asset.

        Tries local path first, then S3 fallback.  Falls back to an empty
        table (returns 0.5 for all lookups) if no data is available.
        """
        table = BaseRateTable()
        cfg = _ASSET_PARQUET.get(asset, _ASSET_PARQUET["BTC"])
        local_path, s3_key, tmp_path = cfg

        for path in (local_path, tmp_path):
            if os.path.exists(path):
                table.load_from_parquet(path)
                logger.info("base_rates_loaded", asset=asset, source=path, bins=len(table.bins))
                return table

        try:
            import boto3
            s3 = boto3.client("s3", region_name="eu-west-1")
            s3.download_file(S3_BUCKET, s3_key, tmp_path)
            table.load_from_parquet(tmp_path)
            logger.info("base_rates_loaded", asset=asset, source="s3", bins=len(table.bins))
        except Exception as e:
            logger.warning("base_rates_load_failed", asset=asset, error=str(e))
        return table

    def __init__(self, settings: Settings):
        self.settings = settings
        enabled = settings.enabled_pairs
        # Unique assets needed for price feeds (add XRP for hourly markets)
        assets = sorted({a for a, _ in enabled} | {"XRP"})
        self.coinbase = CoinbaseWS(assets=assets)
        self.risk = RiskManager(
            bankroll=settings.bankroll,
            daily_loss_cap_pct=settings.daily_loss_cap_pct,
            max_position_pct=settings.max_position_pct,
            min_trade_usd=settings.min_trade_usd,
            max_trade_usd=settings.max_trade_usd,
        )
        self.db = Database()
        self.dynamo = DynamoStore()
        self.db.attach_dynamo(self.dynamo)

        # LightGBM model server
        from polybot.ml.server import ModelServer
        from polybot.ml.kpi_tracker import KPITracker
        self.model_server = ModelServer()
        self.kpi_tracker = KPITracker()
        # Macro features (fear/greed, funding, OI) — collected for future model retrains
        from polybot.features.macro_features import MacroFeatures
        self._macro = MacroFeatures()
        try:
            self.model_server.load_models()
        except Exception as e:
            logger.warning("model_server_load_failed", error=str(e))

        if settings.mode == "live":
            self.trader = LiveTrader(settings=settings, risk=self.risk, db=self.db)
            self.trader._dynamo = self.dynamo  # DynamoDB dedup
            # Load traded slugs from DynamoDB (last 24h) to survive restarts
            try:
                recent = self.dynamo.get_recent_trades(limit=200)
                for t in recent:
                    slug = t.get("window_slug", "")
                    if slug:
                        self.trader._traded_slugs.add(slug)
                logger.info("dedup_loaded", slugs=len(self.trader._traded_slugs))
            except Exception as e:
                logger.warning("dedup_load_failed", error=str(e)[:60])
        else:
            self.trader = PaperTrader(risk=self.risk, db=self.db)

        # Load a separate base rate table per asset so volatility profiles match
        base_rates: dict[str, BaseRateTable] = {
            asset: self._load_base_rate_for(asset) for asset in assets
        }

        # One AssetState per enabled pair (5m)
        self.asset_states: dict[str, AssetState] = {}
        for asset, dur in enabled:
            key = f"{asset}_5m"
            self.asset_states[key] = AssetState(
                asset=asset,
                tracker=WindowTracker(
                    entry_seconds=settings.directional_entry_seconds,
                    asset=asset,
                    window_seconds=dur,
                ),
                bayesian=BayesianUpdater(base_rates[asset]),
            )

        # Hourly states: BTC, ETH, SOL, XRP
        _hourly_assets = ["BTC", "ETH", "SOL", "XRP"]
        for asset in _hourly_assets:
            key = f"{asset}_1h"
            # Reuse base_rate for known assets, empty for XRP
            br = base_rates.get(asset, self._load_base_rate_for(assets[0]))
            self.asset_states[key] = AssetState(
                asset=asset,
                tracker=WindowTracker(
                    entry_seconds=60,
                    asset=asset,
                    window_seconds=3600,
                ),
                bayesian=BayesianUpdater(br),
            )

        logger.info("pairs_enabled", pairs=list(self.asset_states.keys()))

        # RTDS client for Chainlink oracle prices (include XRP for hourly markets)
        rtds_assets = list(dict.fromkeys(assets + ["XRP"]))
        self.rtds = RTDSClient(assets=rtds_assets)

        self._wallet_address: str = settings.polymarket_funder or ""
        self._last_claim_check: float = 0.0
        self._last_strategy_review: float = 0.0
        self._running = False
        self._start_time = time.time()
        self._last_heartbeat = 0.0
        self._last_verify_sweep = 0.0


    async def start(self):
        logger.info(
            "loop_starting",
            mode=self.settings.mode,
            bankroll=self.settings.bankroll,
            assets=list(self.asset_states.keys()),
            early_entry_enabled=self.settings.early_entry_enabled,
            early_entry_max_ask=self.settings.early_entry_max_ask,
            early_entry_lgbm=self.settings.early_entry_lgbm_threshold,
        )

        await self.db.connect()

        # Smoke test all dependencies before trading
        from polybot.core.smoke_test import run_smoke_tests
        smoke = await run_smoke_tests(self.settings)
        if smoke.failed:
            raise RuntimeError(f"Smoke test failed: {smoke.failed}")

        # Balance check at startup for bankroll sync
        if self._wallet_address:
            try:
                from polybot.market.balance_checker import BalanceChecker
                checker = BalanceChecker()
                balances = await checker.check(self._wallet_address)
                logger.info("wallet_balance", **balances)
                if self.settings.mode == "live":
                    polygon_usdc = balances.get("polygon_usdc", 0.0)
                    if polygon_usdc > 0 and abs(polygon_usdc - self.risk.bankroll) > 0.50:
                        self.risk.bankroll = polygon_usdc
                        logger.info("bankroll_updated_from_balance", bankroll=round(polygon_usdc, 2))
            except Exception as e:
                logger.warning("startup_balance_check_failed", error=str(e))

        # Resolve orphan trades from previous sessions
        await self._resolve_orphan_trades()

        self._running = True

        tasks = [
            asyncio.create_task(self.coinbase.connect(), name="coinbase_ws"),
            asyncio.create_task(self.rtds.connect(), name="rtds_ws"),
            asyncio.create_task(self._strategy_loop_resilient(), name="strategy_loop"),
            asyncio.create_task(self._shadow_tracker_loop(), name="shadow_tracker"),
        ]

        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            logger.info("loop_cancelled")
        except Exception as e:
            logger.error("gather_fatal_error", error=str(e), exc_info=True)
            raise
        finally:
            await self.stop()

    async def stop(self):
        self._running = False
        await self.coinbase.close()
        await self.rtds.close()
        await self.db.close()
        logger.info("loop_stopped")

    async def _strategy_loop_resilient(self):
        while self._running:
            try:
                await self._strategy_loop()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("strategy_loop_crashed", error=str(e), exc_info=True)
                if self._running:
                    logger.info("strategy_loop_restarting", delay_seconds=5)
                    await asyncio.sleep(5)

    async def _strategy_loop(self):
        # Wait for first price
        while self._running and all(
            self.coinbase.get_price(s.asset) == 0
            for s in self.asset_states.values()
        ):
            await asyncio.sleep(0.25)

        for key, state in self.asset_states.items():
            p = self.coinbase.get_price(state.asset)
            if p > 0:
                logger.info("price_feed_ready", key=key, asset=state.asset, price=p)

        _last_price_log = time.time()

        while self._running:
            any_price = False
            for key, state in self.asset_states.items():
                price = self.coinbase.get_price(state.asset)
                if price <= 0:
                    continue
                any_price = True
                try:
                    await self._tick_asset(state, price)
                except Exception as e:
                    logger.error("tick_asset_error", key=key, error=str(e), exc_info=True)

            # Warn if all prices have been zero for > 60 seconds
            if any_price:
                _last_price_log = time.time()
            elif time.time() - _last_price_log > 60:
                logger.warning("price_feed_stale_all_zero", seconds=round(time.time() - _last_price_log))
                _last_price_log = time.time()  # reset so we don't spam

            await asyncio.sleep(0.25)

            # Heartbeat every 60 seconds — log + touch file for Docker HEALTHCHECK
            if time.time() - self._last_heartbeat >= 60:
                self._last_heartbeat = time.time()
                uptime = round((time.time() - self._start_time) / 60, 1)
                logger.info("heartbeat", uptime_min=uptime, tasks=len(asyncio.all_tasks()))
                try:
                    open("/tmp/heartbeat", "w").write(str(time.time()))
                except Exception:
                    pass

            # Verify unresolved trades every 5 minutes
            if time.time() - self._last_verify_sweep >= 300:
                self._last_verify_sweep = time.time()
                asyncio.create_task(self._verify_sweep(), name="verify_sweep")

            # Refresh models every 4 hours (non-blocking)
            try:
                self.model_server.refresh_if_needed()
            except Exception as e:
                logger.warning("model_refresh_failed", error=str(e)[:60])

            # Auto-claim removed — Gnosis Safe proxy incompatible, claim manually via UI

    async def _tick_asset(self, state: AssetState, price: float):
        tracker = state.tracker
        window_state = tracker.tick(price)
        window = tracker.current
        if not window:
            return

        _sso = time.time() - window.open_ts
        logger.info("tick", asset=state.asset, seconds=int(_sso),
                    early_enabled=self.settings.early_entry_enabled,
                    has_position=bool(state.early_position),
                    early_traded=state.early_entry_traded,
                    early_evaluated=state.early_entry_evaluated)

        # Track price history for realized vol calculation
        state.price_history.append(price)
        # Track high/low for body_ratio
        if price > state.window_high:
            state.window_high = price
        if price < state.window_low:
            state.window_low = price

        # Update oracle lag (Coinbase vs Chainlink)
        oracle = self.rtds.get_state(state.asset)
        oracle.compute_lag(price)

        current_open_ts = window.open_ts

        if state.prev_open_ts is not None and current_open_ts != state.prev_open_ts:
            await self._on_window_close(state, price)
            await self._on_window_open(state, price)
        elif state.prev_open_ts is None:
            await self._on_window_open(state, price)

        # Late-entry scan window: T+210s start scan, T+240s execute (or early if cheap)
        seconds_since_open = time.time() - window.open_ts
        state.window_tick_count += 1

        # Write live state every 5s (async, non-blocking)
        if self.settings.early_entry_enabled and int(seconds_since_open) % 5 == 0:
            self._write_live_state_async(state, price, seconds_since_open)

        # Log progress every 60s for debugging
        if int(seconds_since_open) % 60 == 0 and int(seconds_since_open) > 0:
            logger.debug("tick_progress", asset=state.asset, seconds=int(seconds_since_open),
                         target=self.settings.late_entry_seconds, evaluated=state.late_entry_evaluated)

        # ── V2 EARLY ENTRY PLAYBOOK ──────────────────────────────────────────
        if self.settings.early_entry_enabled:
            win_secs = state.tracker.window_seconds
            # Cutoff: cancel unfilled + stop trading. 270s for 5m, 3540s for 1h.
            cutoff = win_secs - 30

            # PHASE 1: Open both sides at T+5–15s (wait for orderbook to form)
            if (not state.early_entry_traded and not state.early_position
                    and 5 <= seconds_since_open <= 15):
                await self._v2_open_position(state, price)

            # PHASE 2: Confirm direction at T+15-20s (once per window)
            if state.early_position and 15 <= seconds_since_open <= 20 and not state.early_confirm_done:
                state.early_confirm_done = True
                await self._v2_confirm(state, price)

            # PHASE 3: Accumulate every 3s from T+5 to T+cutoff + poll fills
            if state.early_position and 5 <= seconds_since_open <= cutoff:
                tick_3s = int(seconds_since_open // 3)
                if tick_3s not in state.early_accum_ticks:
                    state.early_accum_ticks.add(tick_3s)
                    await self._v2_accumulate_cheap(state, price, seconds_since_open)
                    await self._v2_poll_fills(state)

            # PHASE 4: Hard stop — expensive entries only, T+30 to T+240, down 25%+
            if state.early_position and 30 <= seconds_since_open <= 240:
                cp15 = int(seconds_since_open // 15) * 15
                if 30 <= cp15 <= 240 and cp15 not in state.early_checkpoints_done:
                    state.early_checkpoints_done.add(cp15)
                    await self._early_checkpoint(state, price, seconds_since_open, cp15)

            # Status log every 15s
            if state.early_position:
                st15 = int(seconds_since_open // 15)
                if st15 not in state.early_status_logged:
                    state.early_status_logged.add(st15)
                    self._log_v2_status(state, seconds_since_open)

            # Cancel unfilled at cutoff, then hold everything to resolution
            if state.early_position and seconds_since_open >= cutoff and cutoff not in state.early_checkpoints_done:
                state.early_checkpoints_done.add(cutoff)
                await self._early_cancel_unfilled(state)

        if not self.settings.early_entry_enabled and not state.late_entry_evaluated and not state.traded_this_window:
            await self._scan_tick(state, price, seconds_since_open)

        state.prev_open_ts = current_open_ts

    # _try_tier_a_entry removed (oracle dislocation never triggered in practice)
    # _on_entry_zone removed (replaced by _evaluate_scored_entry)

    # 128 lines removed (dead method)

    # 223 lines removed (dead method)

    async def _scan_tick(self, state: AssetState, price: float, seconds_since_open: float):
        """Scan window logic: T+210s–T+255s, find best entry price.

        Phase 1 (T+210s): Start scan — record direction and initial ask.
        Phase 2 (T+210s–T+240s): Every 3s, refresh orderbook. Track best ask.
            - If direction flips → abort (direction_unstable).
            - If ask <= $0.58 → enter immediately (cheap enough).
        Phase 3 (T+240s): Execute at best ask found during scan.
        """
        SCAN_START = self.settings.late_entry_seconds  # 210
        SCAN_END = SCAN_START + 30  # 240
        SCAN_DEADLINE = SCAN_START + 45  # 255 hard cutoff
        SCAN_INTERVAL = 3.0  # seconds between orderbook checks

        # Time-of-day + weekend liquidity filter
        from datetime import datetime, timezone
        _now_filter = datetime.now(timezone.utc)
        utc_hour = _now_filter.hour
        is_weekend = _now_filter.weekday() >= 5  # Sat=5, Sun=6
        weak_hours = (utc_hour < 9) or (utc_hour >= 21) or (utc_hour == 12)
        EARLY_ENTRY_ASK = 0.72 if is_weekend else (0.68 if weak_hours else 0.58)

        # Phase 1: Start scan at T+210s
        if seconds_since_open >= SCAN_START and not state.scan_active and not state.scan_direction_flipped:
            await self._refresh_orderbook(state)
            yes_ask = state.orderbook.yes_best_ask
            no_ask = state.orderbook.no_best_ask
            if yes_ask >= no_ask:
                direction = "up"
                current_ask = yes_ask
            else:
                direction = "down"
                current_ask = no_ask

            state.scan_active = True
            state.scan_direction = direction
            state.scan_best_ask = current_ask
            state.scan_best_ask_ts = time.time()
            state.scan_last_checked = time.time()

            logger.info("scan_started", asset=state.asset, direction=direction,
                        ask=round(current_ask, 3), seconds=round(seconds_since_open, 1))

            # Early entry: cheap enough, don't wait
            if 0.55 <= current_ask <= EARLY_ENTRY_ASK:
                logger.info("scan_early_entry", asset=state.asset, ask=round(current_ask, 3))
                await self._execute_scan_entry(state, price)
                return
            return

        # Phase 2: Scan in progress — check every 3s
        if state.scan_active and seconds_since_open < SCAN_END:
            now = time.time()
            if state.scan_last_checked and (now - state.scan_last_checked) < SCAN_INTERVAL:
                return  # too soon, wait

            await self._refresh_orderbook(state)
            state.scan_last_checked = now
            yes_ask = state.orderbook.yes_best_ask
            no_ask = state.orderbook.no_best_ask

            if yes_ask >= no_ask:
                direction = "up"
                current_ask = yes_ask
            else:
                direction = "down"
                current_ask = no_ask

            # Direction flip check
            if direction != state.scan_direction:
                state.scan_direction_flipped = True
                state.scan_active = False
                logger.info("scan_direction_flipped", asset=state.asset,
                            original=state.scan_direction, new=direction,
                            seconds=round(seconds_since_open, 1))
                # Log the skip to DynamoDB
                self._log_scan_signal(state, price, skip_reason="direction_unstable")
                state.late_entry_evaluated = True
                return

            # Track best (lowest) ask
            if current_ask < state.scan_best_ask:
                state.scan_best_ask = current_ask
                state.scan_best_ask_ts = now
                logger.debug("scan_better_ask", asset=state.asset,
                             ask=round(current_ask, 3), seconds=round(seconds_since_open, 1))

            # Early entry: cheap enough
            if 0.55 <= current_ask <= EARLY_ENTRY_ASK:
                state.scan_best_ask = current_ask
                state.scan_best_ask_ts = now
                logger.info("scan_early_entry", asset=state.asset, ask=round(current_ask, 3))
                await self._execute_scan_entry(state, price)
                return
            return

        # Phase 3: Scan deadline — execute at best ask found
        if state.scan_active and seconds_since_open >= SCAN_END:
            await self._execute_scan_entry(state, price)
            return

        # Hard deadline: if scan was aborted (direction flip) but we haven't marked evaluated
        if seconds_since_open >= SCAN_DEADLINE and not state.late_entry_evaluated:
            state.late_entry_evaluated = True

    def _log_scan_signal(self, state: AssetState, price: float, skip_reason: str = "", extra: dict | None = None):
        """Log a scan evaluation to DynamoDB signals table with full backtest data."""
        from datetime import datetime, timezone
        window = state.tracker.current
        if not window:
            return
        scan_duration = (time.time() - (state.scan_best_ask_ts or time.time()))
        try:
            record = {
                "window_slug": window.slug,
                "timestamp": time.time(),
                "asset": state.asset,
                "timeframe": "5m",
                "direction": state.scan_direction or "unknown",
                "pct_move": round(state.tracker.pct_move(price) or 0.0, 6),
                "market_price": round(state.scan_best_ask or 0, 4),
                "yes_ask": round(state.orderbook.yes_best_ask, 4),
                "no_ask": round(state.orderbook.no_best_ask, 4),
                "outcome": "skipped" if skip_reason else "traded",
                "rejection_reason": skip_reason,
                "strategy": "late_momentum_v3_scan",
                "seconds_remaining": round(window.seconds_remaining(), 1),
                "utc_hour": datetime.now(timezone.utc).hour,
                "scan_best_ask": round(state.scan_best_ask or 0, 4),
                "scan_duration_s": round(scan_duration, 1),
                "direction_flipped": state.scan_direction_flipped,
            }
            if extra:
                record.update(extra)
            self.dynamo.put_signal(record)
        except Exception:
            pass

    async def _execute_scan_entry(self, state: AssetState, price: float):
        """Execute trade at the best ask found during the scan window."""
        state.late_entry_evaluated = True
        state.scan_active = False

        window = state.tracker.current
        if not window:
            return

        # Use scan results
        direction_up = state.scan_direction == "up"
        current_ask = state.scan_best_ask or 0
        pct_move = state.tracker.pct_move(price) or 0.0
        remaining = window.seconds_remaining()

        # Per-asset adaptive ceilings
        max_ask = 0.82 if state.asset == "SOL" else 0.78

        # Time-of-day + weekend liquidity filter
        from datetime import datetime, timezone
        _now_exec = datetime.now(timezone.utc)
        utc_hour = _now_exec.hour
        is_weekend = _now_exec.weekday() >= 5  # Sat=5, Sun=6
        weak_hours = (utc_hour < 9) or (utc_hour >= 21) or (utc_hour == 12)
        min_ask = 0.70 if is_weekend else 0.65

        # Volatility filter — skip choppy markets
        vol_now = compute_realized_vol(list(state.price_history))
        vol_avg = sum(state.vol_history) / len(state.vol_history) if state.vol_history else vol_now
        choppy = vol_now > 2 * vol_avg if vol_avg > 0 else False

        is_peak = not weak_hours and not is_weekend

        scan_duration = time.time() - (state.scan_best_ask_ts or time.time())

        # LightGBM prediction (compute FIRST — gates everything)
        lgbm_prob = 0.0
        try:
            import math as _math
            from datetime import datetime as _dt2
            _now_utc = _dt2.now(timezone.utc)
            vol = compute_realized_vol(list(state.price_history))
            vol_ma = sum(state.vol_history) / len(state.vol_history) if state.vol_history else vol
            vol_ratio = vol / vol_ma if vol_ma > 0 else 1.0
            hl_range = state.window_high - state.window_low
            body = abs(price - (window.open_price or price))
            body_ratio = body / hl_range if hl_range > 0 else 0.5
            features = {
                "move_pct_15s": pct_move,
                "realized_vol_5m": vol,
                "vol_ratio": vol_ratio,
                "body_ratio": body_ratio,
                "prev_window_direction": (1 if state.prev_window and state.prev_window.close_price
                    and state.prev_window.open_price and state.prev_window.close_price >= state.prev_window.open_price else -1)
                    if state.prev_window else 0,
                "prev_window_move_pct": ((state.prev_window.close_price - state.prev_window.open_price)
                    / state.prev_window.open_price * 100) if state.prev_window and state.prev_window.open_price
                    and state.prev_window.close_price else 0,
                "hour_sin": _math.sin(2 * _math.pi * _now_utc.hour / 24),
                "hour_cos": _math.cos(2 * _math.pi * _now_utc.hour / 24),
                "dow_sin": _math.sin(2 * _math.pi * _now_utc.weekday() / 7),
                "dow_cos": _math.cos(2 * _math.pi * _now_utc.weekday() / 7),
                "signal_move_pct": abs(pct_move),
                "signal_ask_price": current_ask,
                "signal_seconds": 300 - remaining,
                "signal_ev": 0,
            }
            lgbm_prob = self.model_server.predict(f"{state.asset}_5m", features)
        except Exception:
            pass

        # Scenario C entry rules — lgbm gates first, ask ceiling relaxed for high conviction
        skip_reason = ""

        # 1. LightGBM gate (must pass before anything else)
        if lgbm_prob > 0 and lgbm_prob < 0.62:
            skip_reason = "lgbm_low"
        # 2. Absolute ask floor
        elif current_ask < 0.60:
            skip_reason = "ask_floor"
        # 3. Absolute ask ceiling
        elif current_ask > 0.95:
            skip_reason = "ask_ceiling"
        # 4. Standard guards
        elif not self.risk.can_trade():
            skip_reason = "circuit_breaker"
        elif choppy:
            skip_reason = "choppy_market"
        # 5. Time-of-day min ask
        elif current_ask < min_ask:
            skip_reason = "no_conviction"

        # Sizing — ask-based tiers with lgbm conviction gates
        size = 0
        if not skip_reason:
            if 0.82 <= current_ask <= 0.88 and lgbm_prob >= 0.70:
                size = 5.00  # High ask + high conviction
            elif 0.88 < current_ask <= 0.95 and lgbm_prob >= 0.80:
                size = 5.00  # Very high ask + very high conviction
            elif current_ask > max_ask:
                skip_reason = "fully_priced"  # Above per-asset max, lgbm not high enough
            elif current_ask >= 0.75 and is_peak:
                size = 10.00  # Normal high conviction, peak hours
            else:
                size = 5.00  # Default

        # BTC cross-asset move (for data collection)
        btc_move_pct = 0.0
        if state.asset != "BTC":
            btc_state = self.asset_states.get("BTC_5m")
            if btc_state and btc_state.tracker.current and btc_state.tracker.current.open_price:
                btc_price = self.coinbase.get_price("BTC")
                btc_open = btc_state.tracker.current.open_price
                if btc_open > 0:
                    btc_move_pct = (btc_price - btc_open) / btc_open * 100

        # Log evaluation
        logger.info(
            "late_entry_eval",
            asset=state.asset, slug=window.slug,
            direction="UP" if direction_up else "DOWN",
            current_ask=round(current_ask, 3),
            max_ask=max_ask, min_ask=min_ask,
            size=size,
            pct_move=round(pct_move, 4),
            seconds_remaining=round(remaining, 1),
            scan_duration_s=round(scan_duration, 1),
            direction_flipped=state.scan_direction_flipped,
            utc_hour=utc_hour, weak_hours=weak_hours,
            lgbm_prob=round(lgbm_prob, 4),
            btc_move_pct=round(btc_move_pct, 4),
            skip_reason=skip_reason or "TRADE",
        )

        # Log to DynamoDB with all backtest fields
        self._log_scan_signal(state, price, skip_reason=skip_reason,
                              extra={
                                  "utc_hour": utc_hour, "weak_hours": weak_hours,
                                  "open_price": round(window.open_price or 0, 4),
                                  "current_price": round(price, 4),
                                  "window_high": round(state.window_high, 4),
                                  "window_low": round(state.window_low, 4),
                                  "realized_vol": round(compute_realized_vol(list(state.price_history)), 6),
                                  "lgbm_prob": round(lgbm_prob, 4),
                                  "p_bayesian": round(state.bayesian.probability, 4),
                                  "btc_move_pct": round(btc_move_pct, 4),
                                  "size": size,
                                  "tier": "high" if current_ask >= 0.75 else "mid" if current_ask >= 0.65 else "low",
                              })

        if skip_reason:
            return

        # Build signal and execute
        from polybot.models import Direction, Signal, SignalSource
        direction = Direction.UP if direction_up else Direction.DOWN

        # Refresh orderbook one final time for accurate yes/no asks
        await self._refresh_orderbook(state)

        signal = Signal(
            source=SignalSource.DIRECTIONAL,
            direction=direction,
            model_prob=current_ask,
            market_price=current_ask,
            ev=0,
            window_slug=window.slug,
            asset=state.asset,
            p_bayesian=state.bayesian.probability,
            pct_move=pct_move,
            seconds_remaining=remaining,
            yes_ask=state.orderbook.yes_best_ask,
            no_ask=state.orderbook.no_best_ask,
            yes_bid=state.orderbook.yes_best_bid,
            no_bid=state.orderbook.no_best_bid,
            open_price=window.open_price or 0,
        )

        logger.info(
            "late_entry_trade",
            asset=state.asset, slug=window.slug,
            direction="UP" if direction_up else "DOWN",
            ask=round(current_ask, 3), size=round(size, 2),
            scan_duration_s=round(scan_duration, 1),
            strategy="late_momentum_v3_scan",
        )

        signal._late_entry_size = size
        t_start = time.time()
        signal_ms = (time.time() - t_start) * 1000
        await self._execute(signal, state, signal_ms, 0)
        state.traded_this_window = True

    async def _evaluate_scored_entry(self, state: AssetState, price: float):
        """Scored confirmation entry — previous strategy, kept for reference."""
        from polybot.strategy.scorer import compute_score

        window = state.tracker.current
        if not window:
            return

        pct_move = state.tracker.pct_move(price) or 0.0
        remaining = window.seconds_remaining()

        # BTC cross-asset signal
        btc_move = 0.0
        if state.asset != "BTC":
            btc_state = self.asset_states.get("BTC_5m")
            if btc_state and btc_state.tracker.current and btc_state.tracker.current.open_price:
                btc_price = self.coinbase.get_price("BTC")
                btc_open = btc_state.tracker.current.open_price
                if btc_open > 0:
                    btc_move = (btc_price - btc_open) / btc_open * 100

        await self._refresh_orderbook(state)
        current_ask = state.orderbook.yes_best_ask if pct_move >= 0 else state.orderbook.no_best_ask

        score = compute_score(
            ofi_at_2s=state.ofi_at_2s or 0,
            ofi_at_8s=state.ofi_at_8s or 0,
            price_at_2s=state.price_at_2s or price,
            price_at_8s=state.price_at_8s or price,
            open_price=window.open_price,
            btc_move_pct=btc_move,
            asset=state.asset,
            ask_at_open=state.ask_at_open or current_ask,
            ask_now=current_ask,
            window_volume=state.window_tick_count,
            avg_prior_volume=(sum(state.prior_window_tick_counts) / len(state.prior_window_tick_counts)
                              if state.prior_window_tick_counts else 0),
        )

        # LightGBM prediction
        pair = f"{state.asset}_5m"
        vol = compute_realized_vol(list(state.price_history))
        vol_ma = sum(state.vol_history) / len(state.vol_history) if state.vol_history else vol
        vol_ratio = vol / vol_ma if vol_ma > 0 else 1.0
        hl_range = state.window_high - state.window_low
        body = abs(price - (window.open_price or price))
        body_ratio = body / hl_range if hl_range > 0 else 0.5
        seconds_since_open = (window.close_ts - window.open_ts) - remaining
        import math as _math, datetime as _dt
        now_utc = _dt.datetime.now(_dt.timezone.utc)
        features = {
            "move_pct_15s": pct_move,
            "realized_vol_5m": vol,
            "vol_ratio": vol_ratio,
            "body_ratio": body_ratio,
            "prev_window_direction": (1 if state.prev_window and state.prev_window.close_price and state.prev_window.open_price and state.prev_window.close_price >= state.prev_window.open_price else -1) if state.prev_window else 0,
            "prev_window_move_pct": ((state.prev_window.close_price - state.prev_window.open_price) / state.prev_window.open_price * 100) if state.prev_window and state.prev_window.open_price and state.prev_window.close_price else 0,
            "hour_sin": _math.sin(2 * _math.pi * now_utc.hour / 24),
            "hour_cos": _math.cos(2 * _math.pi * now_utc.hour / 24),
            "dow_sin": _math.sin(2 * _math.pi * now_utc.weekday() / 7),
            "dow_cos": _math.cos(2 * _math.pi * now_utc.weekday() / 7),
            "signal_move_pct": abs(pct_move),
            "signal_ask_price": current_ask,
            "signal_seconds": seconds_since_open,
            "signal_ev": 0,
        }
        lgbm_prob = self.model_server.predict(pair, features)
        ev = lgbm_prob * (1 - current_ask) - (1 - lgbm_prob) * current_ask
        features["signal_ev"] = ev

        # Log score on every window
        entry_type = "skipped"
        skip_reason = ""

        logger.info(
            "window_score",
            asset=state.asset, slug=window.slug,
            score=score.total,
            ofi=score.ofi, no_rev=score.no_reversal,
            cross=score.cross_asset, pm=score.pm_pressure, vol=score.volume,
            lgbm_prob=round(lgbm_prob, 4),
            ask=round(current_ask, 3),
            pct_move=round(pct_move, 4),
            ev=round(ev, 4),
        )

        # Log to DynamoDB signals table
        try:
            self.dynamo.put_signal({
                "window_slug": window.slug,
                "timestamp": time.time(),
                "asset": state.asset,
                "timeframe": "5m",
                "score_total": score.total,
                "score_ofi": int(score.ofi),
                "score_no_reversal": int(score.no_reversal),
                "score_cross_asset": int(score.cross_asset),
                "score_polymarket_pressure": int(score.pm_pressure),
                "score_volume": int(score.volume),
                "direction": "up" if pct_move >= 0 else "down",
                "pct_move": round(pct_move, 6),
                "model_prob": round(lgbm_prob, 4),
                "market_price": round(current_ask, 4),
                "ev": round(ev, 4),
                "p_bayesian": round(state.bayesian.probability, 4),
                "seconds_remaining": round(remaining, 1),
                "yes_ask": round(state.orderbook.yes_best_ask, 4),
                "no_ask": round(state.orderbook.no_best_ask, 4),
                "current_price": round(price, 2),
                "open_price": round(window.open_price, 2) if window.open_price else 0,
            })
        except Exception:
            pass

        # HARD CEILING — applies to ALL entry paths (taker, maker, override)
        # This is the first check. Nothing trades above $0.55.
        if current_ask > self.settings.max_market_price:
            skip_reason = f"ask_{current_ask:.2f}_above_{self.settings.max_market_price}"
        # TIERED MOVE FILTER — small moves need stronger confirmation
        elif abs(pct_move) < 0.03:
            # Small move (0.015-0.03%): require BTC confirmation + higher lgbm
            if not btc_confirms:
                skip_reason = "small_move_no_btc_confirm"
            elif lgbm_prob < 0.68:
                skip_reason = "small_move_lgbm_low"
            elif ev < 0.05:
                skip_reason = "small_move_low_ev"
            else:
                entry_type = "override" if lgbm_prob >= 0.68 and current_ask <= 0.55 and ev >= 0.10 else "taker"
                logger.info("small_move_confirmed", asset=state.asset, slug=window.slug,
                            move=round(pct_move, 4), lgbm=round(lgbm_prob, 4), btc=btc_confirms)
        # Decision based on score — with hard filter override
        elif lgbm_prob >= 0.65 and current_ask <= 0.55 and current_ask > 0 and ev >= 0.10:
            entry_type = "override"
            logger.info("score_override", asset=state.asset, slug=window.slug,
                        score=score.total, lgbm=round(lgbm_prob, 4),
                        ask=round(current_ask, 3), ev=round(ev, 4))
        elif score.total >= 4:
            # HIGH CONVICTION: taker FOK
            if lgbm_prob < 0.60:
                skip_reason = "lgbm_low_taker"
            elif ev < self.settings.min_ev_threshold:
                skip_reason = "insufficient_ev"
            else:
                entry_type = "taker"
        elif score.total >= 2:
            # LOW CONVICTION: maker GTC at $0.48
            if lgbm_prob < 0.55:
                skip_reason = "lgbm_low_maker"
            elif ev < 0.05:
                skip_reason = "insufficient_ev_maker"
            else:
                entry_type = "maker"
        else:
            skip_reason = f"low_score_{score.total}"

        if entry_type == "skipped":
            logger.info("score_skip", asset=state.asset, slug=window.slug,
                        score=score.total, reason=skip_reason, lgbm=round(lgbm_prob, 4),
                        ask=round(current_ask, 3), ev=round(ev, 4))
            return

        # Build signal
        from polybot.models import Direction, Signal, SignalSource
        direction = Direction.UP if pct_move >= 0 else Direction.DOWN
        market_price = current_ask

        signal = Signal(
            source=SignalSource.DIRECTIONAL,
            direction=direction,
            model_prob=lgbm_prob,
            market_price=market_price,
            ev=ev,
            window_slug=window.slug,
            asset=state.asset,
            p_bayesian=state.bayesian.probability,
            pct_move=pct_move,
            seconds_remaining=remaining,
            yes_ask=state.orderbook.yes_best_ask,
            no_ask=state.orderbook.no_best_ask,
            yes_bid=state.orderbook.yes_best_bid,
            no_bid=state.orderbook.no_best_bid,
            open_price=window.open_price or 0,
        )

        if not self.risk.can_trade():
            logger.warning("score_blocked_circuit_breaker", asset=state.asset)
            return

        # Enforce $1.50 total bet ceiling
        size = min(self.risk.get_bet_size(lgbm_prob=lgbm_prob), 1.50)
        if size < 1.0:
            size = 1.0

        logger.info(
            "score_entry",
            asset=state.asset, slug=window.slug,
            entry_type=entry_type, score=score.total,
            ask=round(market_price, 3), size=round(size, 2),
            lgbm=round(lgbm_prob, 4), ev=round(ev, 4),
        )

        t_start = time.time()
        if not self.settings.scenario_c_enabled:
            logger.info("scenario_c_paused", asset=state.asset, slug=window.slug,
                        entry_type=entry_type, ask=round(market_price, 3), lgbm=round(lgbm_prob, 4))
            return
        if entry_type in ("taker", "override"):
            # FOK at market ask — override uses same execution as taker
            signal_ms = (time.time() - t_start) * 1000
            await self._execute(signal, state, signal_ms, 0)
            state.traded_this_window = True
        elif entry_type == "maker":
            # GTC at $0.48, cancel after 8s
            # For now, use FOK at current ask (GTC requires separate implementation)
            signal_ms = (time.time() - t_start) * 1000
            await self._execute(signal, state, signal_ms, 0)
            state.traded_this_window = True

    # ── V2 OPEN POSITION (at window open, T+0) ───────────────────────────────
    async def _v2_open_position(self, state: AssetState, price: float):
        """Post GTC limits on both sides immediately when window opens. No Gamma API fetch."""
        logger.info("v2_open_attempt", asset=state.asset, mode=self.settings.mode)
        if self.settings.mode != "live":
            return

        window = state.tracker.current
        if not window or not window.yes_token_id or not window.no_token_id:
            logger.info("v2_open_no_tokens", asset=state.asset,
                        slug=window.slug if window else "none",
                        yes_token=window.yes_token_id[:12] if window and window.yes_token_id else "",
                        no_token=window.no_token_id[:12] if window and window.no_token_id else "")
            return

        early_slug = f"early_{window.slug}"

        # Dedup
        if not hasattr(self, '_early_traded_slugs'):
            self._early_traded_slugs = set()
        if early_slug in self._early_traded_slugs:
            logger.info("v2_open_dedup", asset=state.asset, slug=early_slug)
            return
        self._early_traded_slugs.add(early_slug)

        # Determine direction via LGBM (neutral fallback if no model)
        import math as _math
        from datetime import datetime as _dt, timezone as _tz
        _now = _dt.now(_tz.utc)
        vol = compute_realized_vol(list(state.price_history))
        features = {
            "move_pct_15s": 0.0, "realized_vol_5m": vol,
            "vol_ratio": 1.0, "body_ratio": 0.5,
            "prev_window_direction": (1 if state.prev_window and state.prev_window.close_price
                and state.prev_window.open_price
                and state.prev_window.close_price >= state.prev_window.open_price else -1)
                if state.prev_window else 0,
            "prev_window_move_pct": ((state.prev_window.close_price - state.prev_window.open_price)
                / state.prev_window.open_price * 100) if state.prev_window
                and state.prev_window.open_price and state.prev_window.close_price else 0,
            "hour_sin": _math.sin(2 * _math.pi * _now.hour / 24),
            "hour_cos": _math.cos(2 * _math.pi * _now.hour / 24),
            "dow_sin": _math.sin(2 * _math.pi * _now.weekday() / 7),
            "dow_cos": _math.cos(2 * _math.pi * _now.weekday() / 7),
            "signal_move_pct": 0.0, "signal_ask_price": 0.50,
            "signal_seconds": 0, "signal_ev": 0,
        }
        try:
            lgbm_raw = self.model_server.predict(f"{state.asset}_5m", features)
        except Exception as model_err:
            logger.info("v2_open_model_fallback", asset=state.asset, error=str(model_err)[:60])
            lgbm_raw = 0.50
        direction_up = lgbm_raw >= 0.50

        main_token = window.yes_token_id if direction_up else window.no_token_id
        hedge_token = window.no_token_id if direction_up else window.yes_token_id
        main_bid = state.orderbook.yes_best_bid if direction_up else state.orderbook.no_best_bid
        hedge_bid = state.orderbook.no_best_bid if direction_up else state.orderbook.yes_best_bid
        # Budget: EARLY_ENTRY_MAX_BET / number of active 5m assets
        num_5m_assets = len([k for k in self.asset_states if "_1h" not in k])
        per_asset = self.settings.early_entry_max_bet / max(num_5m_assets, 1)
        # Open uses 25% of per-asset budget (K9: 22% at open, 78% accumulation).
        # LGBM confidence tiers drive main/hedge split within that 25%.
        # Remaining 75% goes to cheap accumulation — this is where edge is built.
        open_budget = round(per_asset * 0.25, 2)
        if lgbm_raw >= 0.60:
            main_frac, hedge_frac = 0.75, 0.25
        elif lgbm_raw >= 0.52:
            main_frac, hedge_frac = 0.65, 0.35
        else:
            main_frac, hedge_frac = 0.55, 0.45
        main_size = round(open_budget * main_frac, 2)
        hedge_size = round(open_budget * hedge_frac, 2)

        logger.info("v2_open_orderbook", asset=state.asset,
                    direction="UP" if direction_up else "DOWN",
                    yes_bid=round(state.orderbook.yes_best_bid, 3),
                    no_bid=round(state.orderbook.no_best_bid, 3),
                    yes_ask=round(state.orderbook.yes_best_ask, 3),
                    no_ask=round(state.orderbook.no_best_ask, 3))

        # Post GTC on both sides (order failures are caught individually)
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType, CreateOrderOptions
            from py_clob_client.order_builder.constants import BUY
            options = CreateOrderOptions(tick_size="0.01", neg_risk=False)
            for token, bid, sz, label in [
                (main_token, main_bid, main_size, "main"),
                (hedge_token, hedge_bid, hedge_size, "hedge"),
            ]:
                post_price = round(bid + 0.01, 2) if bid and bid > 0 else 0.49
                if post_price <= 0 or post_price >= 1.0:
                    continue
                shares = max(round(sz / post_price), 5)
                try:
                    logger.info("v2_open_placing", asset=state.asset, side=label,
                                token=token[:16], price=post_price, shares=shares, sz=sz)
                    args = OrderArgs(price=post_price, size=shares, side=BUY, token_id=token)
                    signed = self.trader.client.create_order(args, options)
                    resp = self.trader.client.post_order(signed, OrderType.GTC)
                    logger.info("v2_open_response", asset=state.asset, side=label, resp=str(resp)[:120])
                    oid = resp.get("orderID", "")
                    if oid:
                        state.early_dca_orders.append({"order_id": oid, "price": post_price, "size": sz, "side": label})
                        logger.info("v2_open_posted", asset=state.asset, slug=early_slug,
                                    side=label, price=post_price, size=sz, order_id=oid[:16],
                                    direction="UP" if direction_up else "DOWN")
                    else:
                        logger.warning("v2_open_no_order_id", asset=state.asset, side=label, resp=str(resp)[:200])
                except Exception as e:
                    logger.warning("v2_open_error", asset=state.asset, side=label, error=str(e)[:200])
        except Exception as e:
            logger.warning("v2_open_import_error", asset=state.asset, error=str(e)[:200])

        # Count open spend toward budget so accumulation respects the cap
        state.early_cheap_filled += main_size + hedge_size

        # Always set position so accumulation loop fires even if orders failed
        state.early_entry_traded = True
        hedge_entry_price = round((hedge_bid or 0.50) + 0.01, 2)
        state.early_position = {
            "slug": early_slug,
            "token_id": main_token,
            "hedge_token": hedge_token,
            "shares": max(round(main_size / 0.50), 5),
            "entry_price": round((main_bid or 0.50) + 0.01, 2),
            "hedge_entry_price": hedge_entry_price,
            "direction_up": direction_up,
            "side": "YES" if direction_up else "NO",
            "size": main_size,
        }
        logger.info("v2_open_complete", asset=state.asset, slug=early_slug,
                    direction="UP" if direction_up else "DOWN", lgbm=round(lgbm_raw, 3),
                    main=main_size, hedge=hedge_size, per_asset=per_asset)
        self._log_activity(state, f"OPEN {'UP' if direction_up else 'DOWN'}",
                           f"main=${main_size} hedge=${hedge_size} lgbm={lgbm_raw:.2f}")

    async def _v2_confirm(self, state: AssetState, price: float):
        """Phase 2: Re-run LGBM at T+15s with early price data.

        If direction confirmed: post 20% of per-asset budget more on main.
        If direction flipped: swap main/hedge labels so accumulation targets the right side.
        Skip if seconds_since_open > 20 (already handled by caller gate).
        """
        if self.settings.mode != "live":
            return
        pos = state.early_position
        if not pos:
            return
        window = state.tracker.current
        if not window or not window.open_price:
            return

        import math as _math
        from datetime import datetime as _dt, timezone as _tz
        _now = _dt.now(_tz.utc)
        pct_move = ((price - window.open_price) / window.open_price * 100) if window.open_price > 0 else 0
        vol = compute_realized_vol(list(state.price_history))
        vol_ma = sum(state.vol_history) / len(state.vol_history) if state.vol_history else vol
        features = {
            "move_pct_15s": pct_move, "realized_vol_5m": vol,
            "vol_ratio": vol / vol_ma if vol_ma > 0 else 1.0,
            "body_ratio": abs(pct_move) / 0.1 if pct_move != 0 else 0.5,
            "prev_window_direction": (1 if state.prev_window and state.prev_window.close_price
                and state.prev_window.open_price
                and state.prev_window.close_price >= state.prev_window.open_price else -1)
                if state.prev_window else 0,
            "prev_window_move_pct": ((state.prev_window.close_price - state.prev_window.open_price)
                / state.prev_window.open_price * 100) if state.prev_window
                and state.prev_window.open_price and state.prev_window.close_price else 0,
            "hour_sin": _math.sin(2 * _math.pi * _now.hour / 24),
            "hour_cos": _math.cos(2 * _math.pi * _now.hour / 24),
            "dow_sin": _math.sin(2 * _math.pi * _now.weekday() / 7),
            "dow_cos": _math.cos(2 * _math.pi * _now.weekday() / 7),
            "signal_move_pct": abs(pct_move), "signal_ask_price": pos["entry_price"],
            "signal_seconds": 15, "signal_ev": 0,
        }
        try:
            lgbm_raw = self.model_server.predict(f"{state.asset}_5m", features)
        except Exception as model_err:
            logger.info("v2_confirm_model_fallback", asset=state.asset, error=str(model_err)[:60])
            lgbm_raw = 0.50

        confirm_direction_up = lgbm_raw >= 0.50
        was_direction_up = pos["direction_up"]

        if confirm_direction_up != was_direction_up:
            # Direction flipped — swap main/hedge so accumulation targets winning side
            pos["direction_up"] = confirm_direction_up
            pos["token_id"], pos["hedge_token"] = pos["hedge_token"], pos["token_id"]
            pos["entry_price"], pos["hedge_entry_price"] = pos["hedge_entry_price"], pos["entry_price"]
            pos["side"] = "YES" if confirm_direction_up else "NO"
            logger.info("v2_confirm_flip", asset=state.asset, slug=pos["slug"],
                        new_direction="UP" if confirm_direction_up else "DOWN",
                        lgbm=round(lgbm_raw, 3), pct_move=round(pct_move, 3))
            self._log_activity(state, "CONFIRM FLIP",
                               f"{'UP' if confirm_direction_up else 'DOWN'} lgbm={lgbm_raw:.2f}")
            return

        # Direction confirmed — post 20% more on main side
        await self._refresh_orderbook(state)
        main_bid = state.orderbook.yes_best_bid if confirm_direction_up else state.orderbook.no_best_bid
        if not main_bid or main_bid <= 0:
            return
        num_5m_assets = len([k for k in self.asset_states if "_1h" not in k])
        per_asset = self.settings.early_entry_max_bet / max(num_5m_assets, 1)
        confirm_size = round(per_asset * 0.20, 2)  # $1 at $5/asset
        post_price = round(main_bid + 0.01, 2)
        if post_price <= 0 or post_price >= 1.0:
            return
        shares = max(round(confirm_size / post_price), 5)
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType, CreateOrderOptions
            from py_clob_client.order_builder.constants import BUY
            options = CreateOrderOptions(tick_size="0.01", neg_risk=False)
            args = OrderArgs(price=post_price, size=shares, side=BUY, token_id=pos["token_id"])
            signed = self.trader.client.create_order(args, options)
            resp = self.trader.client.post_order(signed, OrderType.GTC)
            oid = resp.get("orderID", "")
            if oid:
                state.early_cheap_filled += confirm_size
                state.early_dca_orders.append({"order_id": oid, "price": post_price,
                                                "size": confirm_size, "side": "main"})
                logger.info("v2_confirm_posted", asset=state.asset, slug=pos["slug"],
                            price=post_price, size=confirm_size, order_id=oid[:16],
                            lgbm=round(lgbm_raw, 3), pct_move=round(pct_move, 3))
                self._log_activity(state, "CONFIRM +main",
                                   f"${confirm_size} lgbm={lgbm_raw:.2f} move={pct_move:+.3f}%")
            else:
                logger.warning("v2_confirm_no_order_id", asset=state.asset, resp=str(resp)[:200])
        except Exception as e:
            logger.warning("v2_confirm_error", asset=state.asset, error=str(e)[:200])

    # ── V2 ACCUMULATE CHEAP + POLL FILLS ─────────────────────────────────
    async def _v2_accumulate_cheap(self, state: AssetState, price: float, seconds_since_open: float = 0.0):
        """Every 3s: post GTC ladders on BOTH sides below current bid.

        K9 4-tier ladder (from spec, 3,500 trades):
          bid <= 0.15 (lottery zone):    7 levels [0,1,2,3,4,5,6¢], $0.50 each
          bid 0.15-0.35 (cheap zone):    5 levels [0,1,2,4,6¢],      $0.35 each
          bid 0.35-0.60 (mid zone):      3 levels [0,2,5¢],           $0.25 each
          bid > 0.60 (winning side):     2 levels [0,3¢],             $0.50 each — only after T+60s

        Orders cost $0 until filled. Cap is on actual fills only.
        """
        pos = state.early_position
        if not pos or self.settings.mode != "live":
            return
        window = state.tracker.current
        if not window:
            return

        num_5m_assets = len([k for k in self.asset_states if "_1h" not in k])
        max_bet_per_asset = self.settings.early_entry_max_bet / max(num_5m_assets, 1)

        await self._refresh_orderbook(state)

        from py_clob_client.clob_types import CreateOrderOptions
        options = CreateOrderOptions(tick_size="0.01", neg_risk=False)

        order_tasks = []
        for side_up, token_id in [(True, window.yes_token_id), (False, window.no_token_id)]:
            if not token_id:
                continue
            bid = state.orderbook.yes_best_bid if side_up else state.orderbook.no_best_bid
            if not bid or bid <= 0:
                continue

            logger.info("v2_accum_side", asset=state.asset,
                        side="UP" if side_up else "DOWN",
                        bid=round(bid, 3),
                        filled=round(state.early_cheap_filled, 2),
                        max_bet=round(max_bet_per_asset, 2))

            # K9 4-tier ladder
            if bid <= 0.15:
                # Lottery zone: 7 levels, $0.50 each (14x+ return if wins)
                offsets = [0.00, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06]
                size = 0.50
            elif bid <= 0.35:
                # Cheap accumulation zone: 5 levels, $0.35 each
                offsets = [0.00, 0.01, 0.02, 0.04, 0.06]
                size = 0.35
            elif bid <= 0.60:
                # Mid / baseline zone: 3 levels, $0.25 each
                offsets = [0.00, 0.02, 0.05]
                size = 0.25
            else:
                # Winning side (bid > 0.60): 2 levels, $0.50 each, only after T+60s
                if seconds_since_open < 60:
                    logger.info("v2_accum_tier", asset=state.asset,
                                side="UP" if side_up else "DOWN",
                                bid=round(bid, 3), tier="winning_blocked_t60",
                                offsets="[]", num_orders=0)
                    continue
                offsets = [0.00, 0.03]
                size = 0.50

            logger.info("v2_accum_tier", asset=state.asset,
                        side="UP" if side_up else "DOWN",
                        bid=round(bid, 3),
                        tier="lottery" if bid <= 0.15 else "cheap" if bid <= 0.35
                        else "mid" if bid <= 0.60 else "winning",
                        offsets=str(offsets),
                        num_orders=len([o for o in offsets if round(bid - o, 2) >= 0.01]))

            for offset in offsets:
                post_price = round(bid - offset, 2)
                if post_price < 0.01 or post_price > 0.98:
                    continue
                # Skip if actual fills have hit the per-asset budget
                if state.early_cheap_filled + size > max_bet_per_asset:
                    continue
                shares = max(round(size / post_price), 5)
                order_tasks.append(
                    self._post_cheap_order(state, token_id, post_price, shares, size, side_up, options)
                )

        if order_tasks:
            await asyncio.gather(*order_tasks, return_exceptions=True)
            logger.info("v2_accumulate_tick", asset=state.asset,
                        orders=len(order_tasks), filled=round(state.early_cheap_filled, 2),
                        seconds=int(seconds_since_open), max_bet=max_bet_per_asset)

    async def _post_cheap_order(self, state: AssetState, token_id: str, post_price: float,
                                shares: int, size: float, side_up: bool, options) -> None:
        """Post one cheap limit order and track it. Full error logging."""
        from py_clob_client.clob_types import OrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY
        side = "UP" if side_up else "DOWN"
        try:
            args = OrderArgs(price=post_price, size=shares, side=BUY, token_id=token_id)
            signed = self.trader.client.create_order(args, options)
            resp = self.trader.client.post_order(signed, OrderType.GTC)
            oid = resp.get("orderID", "")
            if oid:
                state.early_cheap_posted += size
                state.early_dca_orders.append({"order_id": oid, "price": post_price, "size": size, "side": side})
                logger.info("v2_cheap_posted", asset=state.asset, side=side,
                            price=post_price, size=size, order_id=oid[:16])
            else:
                logger.warning("v2_cheap_no_order_id", asset=state.asset, side=side,
                               price=post_price, resp=str(resp)[:200])
        except Exception as e:
            logger.warning("v2_cheap_post_error", asset=state.asset, side=side,
                           price=post_price, error=str(e))

    async def _v2_poll_fills(self, state: AssetState):
        """Check fill status of all tracked GTC orders, update state totals."""
        if not state.early_dca_orders:
            return
        pos = state.early_position or {}
        direction_up = pos.get("direction_up", True)
        for order in state.early_dca_orders:
            if order.get("filled"):
                continue
            oid = order.get("order_id")
            if not oid:
                continue
            try:
                resp = self.trader.client.get_order(oid)
                status = resp.get("status", "") if isinstance(resp, dict) else ""
                logger.info("v2_poll_order", asset=state.asset, oid=oid[:16],
                            status=status, side=order.get("side", ""))
                if status in ("MATCHED", "FILLED"):
                    order["filled"] = True
                    sz = order.get("size", 0)
                    side = order.get("side", "")
                    price = order.get("price", 0)
                    state.early_cheap_filled += sz
                    # Track per-side shares — resolve "main"/"hedge" using direction
                    shares = round(sz / price) if price > 0 else 0
                    is_up = (side == "UP") or (side == "main" and direction_up) or (side == "hedge" and not direction_up)
                    if is_up:
                        state.early_up_shares += shares
                        state.early_up_cost += sz
                    else:
                        state.early_down_shares += shares
                        state.early_down_cost += sz
                    logger.info("v2_fill_detected", asset=state.asset, side=side,
                                is_up=is_up, price=price, size=sz, shares=shares)
                    self._log_activity(state, f"FILL {side} ${price:.2f}", f"${sz:.2f} ({shares} shares)")
            except Exception as e:
                logger.info("v2_poll_error", asset=state.asset, oid=oid[:16], error=str(e)[:80])

    async def _verify_early_polymarket(self, early_slug: str, market_slug: str, delay: int):
        """Verify early entry trades against Polymarket oracle after delay."""
        await asyncio.sleep(delay)
        try:
            from polybot.feeds.polymarket_rest import get_market_outcome
            for attempt in range(4):
                winner, source = await get_market_outcome(market_slug)
                if winner:
                    went_up = winner == "YES"
                    self._resolve_early_trades_polymarket(early_slug, went_up)
                    return
                await asyncio.sleep(60)
        except Exception as e:
            logger.warning("early_verify_polymarket_failed", slug=early_slug, error=str(e)[:60])

    def _resolve_early_trades_polymarket(self, early_slug: str, went_up: bool):
        """Update early trades with Polymarket-verified outcome."""
        try:
            if not self.dynamo or not self.dynamo._available:
                return
            from decimal import Decimal
            from boto3.dynamodb.conditions import Attr
            resp = self.dynamo._trades.scan(
                FilterExpression=Attr("window_slug").eq(early_slug),
            )
            all_items = resp.get("Items", [])
            has_exit = any(i.get("source") in ("early_exit", "early_hedge_exit") for i in all_items)
            for t in all_items:
                if t.get("source") in ("early_exit", "early_hedge_exit"):
                    continue  # Sell trade — P&L already recorded
                side = t.get("side", "")
                fill = float(t.get("fill_price", 0) or 0)
                size = float(t.get("size_usd", 0) or 0)
                if has_exit:
                    pnl = 0  # Position was sold — real P&L in the exit trade
                else:
                    won = (side == "YES" and went_up) or (side == "NO" and not went_up)
                    pnl = round((size / fill) - size, 2) if won and fill > 0 else round(-size, 2)
                self.dynamo._trades.update_item(
                    Key={"id": t["id"]},
                    UpdateExpression="SET resolved=:r, pnl=:p, outcome_source=:s",
                    ExpressionAttributeValues={
                        ":r": 1, ":p": Decimal(str(pnl)), ":s": "polymarket_verified",
                    },
                )
                logger.info("early_trade_verified", slug=early_slug, side=side, pnl=pnl, had_exit=has_exit)
        except Exception as e:
            logger.warning("early_verify_update_failed", error=str(e)[:60])

    def _resolve_early_trades(self, asset: str, window_slug: str, went_up: bool):
        """Resolve early entry trades in DynamoDB for a closed window."""
        try:
            if not self.dynamo or not self.dynamo._available:
                return
            from decimal import Decimal
            from boto3.dynamodb.conditions import Attr
            # Scan for unresolved early trades matching this window
            early_slug = f"early_{window_slug}"
            # Check if position was already sold (early_exit exists for this window)
            all_resp = self.dynamo._trades.scan(
                FilterExpression=Attr("window_slug").eq(early_slug),
            )
            all_items = all_resp.get("Items", [])
            has_exit = any(i.get("source") in ("early_exit", "early_hedge_exit") for i in all_items)

            unresolved = [t for t in all_items if int(t.get("resolved", 0)) == 0 and t.get("source") == "early_entry"]
            for t in unresolved:
                side = t.get("side", "")
                fill = float(t.get("fill_price", 0) or 0)
                size = float(t.get("size_usd", 0) or 0)

                if has_exit:
                    # Position was sold mid-window — P&L already recorded in the sell
                    pnl = 0
                    logger.info("early_trade_resolved_with_exit", slug=early_slug, side=side,
                                pnl=0, note="P&L in early_exit trade")
                else:
                    # Position held to resolution
                    won = (side == "YES" and went_up) or (side == "NO" and not went_up)
                    if won and fill > 0:
                        pnl = round((size / fill) - size, 2)
                    else:
                        pnl = round(-size, 2)
                    logger.info("early_trade_resolved", slug=early_slug, side=side,
                                won=won, pnl=pnl, source="coinbase")

                self.dynamo._trades.update_item(
                    Key={"id": t["id"]},
                    UpdateExpression="SET resolved=:r, pnl=:p, outcome_source=:s",
                    ExpressionAttributeValues={
                        ":r": 1,
                        ":p": Decimal(str(pnl)),
                        ":s": "coinbase_provisional",
                    },
                )
        except Exception as e:
            logger.warning("early_resolve_failed", slug=window_slug, error=str(e)[:80])

    async def _early_rotate_buy(self, state: AssetState, pos: dict, proceeds: float, ask: float, window):
        """After selling, buy back cheap on SAME side with the proceeds."""
        if self.settings.mode != "live" or not window:
            return
        token_id = pos["token_id"]
        if not token_id:
            return
        try:
            from py_clob_client.clob_types import OrderArgs, OrderType, CreateOrderOptions
            from py_clob_client.order_builder.constants import BUY
            options = CreateOrderOptions(tick_size="0.01", neg_risk=False)
            buy_price = round(ask, 2)
            shares = max(round(proceeds / buy_price), 5)
            args = OrderArgs(price=buy_price, size=shares, side=BUY, token_id=token_id)
            signed = self.trader.client.create_order(args, options)
            resp = self.trader.client.post_order(signed, OrderType.GTC)
            oid = resp.get("orderID", "")
            if oid:
                state.early_dca_orders.append({"order_id": oid, "price": buy_price, "size": proceeds, "side": "rotate"})
                logger.info("early_rotate_buy", asset=state.asset, slug=pos["slug"],
                            price=buy_price, proceeds=round(proceeds, 2), shares=shares,
                            potential_payout=round(shares * 1.0, 2), order_id=oid[:16])
            else:
                logger.warning("early_rotate_buy_failed", resp=str(resp)[:80])
        except Exception as e:
            logger.warning("early_rotate_buy_error", error=str(e)[:80])

    async def _early_cancel_unfilled(self, state: AssetState):
        """Cancel all unfilled GTC orders at T+cutoff. Hold filled shares to resolution."""
        cancelled = 0
        unfilled = [o for o in state.early_dca_orders if not o.get("filled")]
        for order in unfilled:
            oid = order.get("order_id")
            if oid:
                try:
                    self.trader.client.cancel(oid)
                    cancelled += 1
                except Exception as e:
                    logger.warning("early_cancel_error", asset=state.asset,
                                   oid=oid[:16], error=str(e)[:80])
        if state.early_hedge_order_id:
            try:
                self.trader.client.cancel(state.early_hedge_order_id)
                cancelled += 1
            except Exception as e:
                logger.warning("early_cancel_hedge_error", asset=state.asset, error=str(e)[:80])
            state.early_hedge_order_id = None

        state.early_dca_orders = [o for o in state.early_dca_orders if o.get("filled")]
        logger.info("early_cancel_unfilled", asset=state.asset,
                    slug=state.early_position["slug"] if state.early_position else "",
                    cancelled=cancelled, up_shares=int(state.early_up_shares),
                    down_shares=int(state.early_down_shares),
                    filled_usd=round(state.early_cheap_filled, 2))

    # ── EARLY ENTRY CHECKPOINTS ──────────────────────────────────────────
    async def _early_checkpoint(self, state: AssetState, price: float, seconds_since_open: float, checkpoint: int):
        """Phase 4: Hard stop for expensive entries. Conditions (ALL required):
        1. Entry price >= 40¢ (never touch cheap accumulation fills)
        2. Position down > 25% from entry price
        3. T+30 to T+240 (not too early, not in final 60s)
        Immediately rebuys same side cheap if ask <= 40¢.
        """
        pos = state.early_position
        if not pos:
            return

        # Gate 1: never stop-loss cheap entries
        if pos.get("entry_price", 0) < 0.40:
            return

        # Gate 3: T+240 hard cutoff — no selling in final 60s
        if seconds_since_open > 240:
            return

        await self._refresh_orderbook(state)
        main_bid = state.orderbook.yes_best_bid if pos["direction_up"] else state.orderbook.no_best_bid
        if not main_bid or main_bid <= 0:
            return

        # Gate 2: only sell if down more than 25%
        position_value_pct = ((main_bid - pos["entry_price"]) / pos["entry_price"] * 100) if pos["entry_price"] > 0 else 0

        logger.info("early_checkpoint", asset=state.asset, slug=pos["slug"], checkpoint=checkpoint,
                    direction="UP" if pos["direction_up"] else "DOWN",
                    entry_price=round(pos["entry_price"], 3), main_bid=round(main_bid, 3),
                    position_value_pct=round(position_value_pct, 1))
        self._log_activity(state, f"CHECK val={position_value_pct:+.0f}%",
                           "HOLD" if position_value_pct >= -25 else "SELL_STOP_25")

        if position_value_pct < -25:
            window = state.tracker.current
            sell_proceeds = await self._early_sell(state, pos, main_bid, "HARD_STOP_25")
            if sell_proceeds and sell_proceeds > 0.50 and window:
                main_ask = state.orderbook.yes_best_ask if pos["direction_up"] else state.orderbook.no_best_ask
                if main_ask and 0 < main_ask <= 0.40:
                    await self._early_rotate_buy(state, pos, sell_proceeds, main_ask, window)
                else:
                    logger.info("early_rotate_skip_expensive", slug=pos["slug"], ask=main_ask)

    async def _early_sell(self, state: AssetState, pos: dict, current_bid: float, reason: str) -> float:
        """Sell early entry position. Returns sell proceeds or 0."""
        if self.settings.mode != "live":
            logger.info("early_sell_paper", asset=state.asset, reason=reason)
            state.early_position = None
            return 0

        try:
            from py_clob_client.clob_types import OrderArgs, OrderType, CreateOrderOptions
            from py_clob_client.order_builder.constants import SELL
            options = CreateOrderOptions(tick_size="0.01", neg_risk=False)

            token_id = pos["token_id"]
            shares = pos["shares"]

            # Try FOK at current bid
            order_args = OrderArgs(price=current_bid, size=shares, side=SELL, token_id=token_id)
            signed = self.trader.client.create_order(order_args, options)
            try:
                resp = self.trader.client.post_order(signed, OrderType.FOK)
                order_id = resp.get("orderID", "")
            except Exception as sell_err:
                order_id = ""
                logger.warning("early_sell_fok_exception", slug=pos["slug"], error=str(sell_err))

            if not order_id:
                # FOK failed — try GTC at bid-1¢ then bid-2¢
                import asyncio as _aio2
                for offset in (0.01, 0.02):
                    gtc_price = max(round(current_bid - offset, 2), 0.01)
                    try:
                        gtc_args = OrderArgs(price=gtc_price, size=shares, side=SELL, token_id=token_id)
                        gtc_signed = self.trader.client.create_order(gtc_args, options)
                        gtc_resp = self.trader.client.post_order(gtc_signed, OrderType.GTC)
                        gtc_id = gtc_resp.get("orderID", "")
                        logger.info("early_sell_gtc_attempt", slug=pos["slug"],
                                    price=gtc_price, offset=offset, order_id=gtc_id or "failed")
                        if gtc_id:
                            for _ in range(5):  # 5s wait
                                await _aio2.sleep(1.0)
                                try:
                                    st = self.trader.client.get_order(gtc_id).get("status", "")
                                    if st in ("MATCHED", "FILLED"):
                                        order_id = gtc_id
                                        current_bid = gtc_price
                                        break
                                except Exception:
                                    break
                            if order_id:
                                break
                            try:
                                self.trader.client.cancel(gtc_id)
                            except Exception:
                                pass
                    except Exception as gtc_err:
                        logger.warning("early_sell_gtc_error", offset=offset, error=str(gtc_err))
                if not order_id:
                    logger.warning("early_sell_all_failed", slug=pos["slug"], bid=current_bid,
                                   reason=reason, shares=shares)

            if order_id:
                sell_proceeds = shares * current_bid
                pnl = sell_proceeds - pos["size"]
                logger.info("early_sell_filled", slug=pos["slug"], reason=reason,
                            bid=current_bid, pnl=round(pnl, 2), order_id=order_id[:16])
                self._log_activity(state, f"SELL {pos['side']} ${current_bid:.2f}", f"P&L ${pnl:+.2f} ({reason})")
                # Log to DynamoDB
                try:
                    from decimal import Decimal
                    import uuid
                    self.dynamo._trades.put_item(Item={
                        "id": str(uuid.uuid4()),
                        "window_slug": pos["slug"],
                        "asset": state.asset, "timeframe": "5m",
                        "side": "SELL", "source": "early_exit",
                        "fill_price": Decimal(str(round(current_bid, 4))),
                        "size_usd": Decimal(str(round(sell_proceeds, 2))),
                        "shares": Decimal(str(shares)),
                        "pnl": Decimal(str(round(pnl, 2))),
                        "timestamp": Decimal(str(round(time.time(), 3))),
                        "entry_type": reason, "resolved": 1,
                    })
                except Exception:
                    pass
                state.early_position = None
                return sell_proceeds

            # FOK failed — try GTC at bid+1¢
            logger.info("early_sell_fok_failed", slug=pos["slug"], trying_gtc=True)
            gtc_price = round(current_bid + 0.01, 2)
            gtc_args = OrderArgs(price=gtc_price, size=shares, side=SELL, token_id=token_id)
            gtc_signed = self.trader.client.create_order(gtc_args, options)
            gtc_resp = self.trader.client.post_order(gtc_signed, OrderType.GTC)
            gtc_id = gtc_resp.get("orderID", "")

            if gtc_id:
                import asyncio as _aio
                filled = False
                for _ in range(3):
                    await _aio.sleep(1.0)
                    try:
                        status = self.trader.client.get_order(gtc_id).get("status", "")
                        if status in ("MATCHED", "FILLED"):
                            filled = True
                            break
                    except Exception:
                        break
                if not filled:
                    try:
                        self.trader.client.cancel(gtc_id)
                    except Exception:
                        pass
                    logger.info("early_sell_gtc_timeout", slug=pos["slug"])
                    # Hold to resolution — don't panic
                    return
                # GTC filled
                sell_proceeds = shares * gtc_price
                pnl = sell_proceeds - pos["size"]
                logger.info("early_sell_gtc_filled", slug=pos["slug"], reason=reason, pnl=round(pnl, 2))
                state.early_position = None

        except Exception as e:
            logger.error("early_sell_error", error=str(e))
        return 0

    def _log_early_trade(self, state, window, side, fill_price, size, lgbm_prob, ev,
                         entry_type, limit_price, limit_filled, limit_wait_ms, order_id):
        """Log early entry trade to DynamoDB."""
        try:
            from decimal import Decimal
            dynamo = self.dynamo
            if not dynamo or not dynamo._available:
                return
            table = dynamo._trades
            early_slug = f"early_{window.slug}"
            import uuid
            table.put_item(Item={
                "id": str(uuid.uuid4()),
                "window_slug": early_slug,
                "asset": state.asset,
                "timeframe": "5m",
                "side": side,
                "fill_price": Decimal(str(round(fill_price, 4))),
                "size_usd": Decimal(str(round(size, 2))),
                "shares": Decimal(str(round(size / fill_price, 1))),
                "timestamp": Decimal(str(round(time.time(), 3))),
                "source": "early_entry",
                "strategy": "early_entry",
                "entry_type": entry_type,
                "limit_price": Decimal(str(round(limit_price, 4))) if limit_price else None,
                "limit_filled": limit_filled,
                "limit_wait_ms": Decimal(str(int(limit_wait_ms))),
                "model_prob": Decimal(str(round(lgbm_prob, 4))),
                "ev": Decimal(str(round(ev, 4))),
                "order_id": order_id or "",
                "resolved": 0,
            })
        except Exception as e:
            logger.warning("early_trade_log_failed", error=str(e)[:60])

    async def _on_window_open(self, state: AssetState, price: float):
        """Reset state for new window."""
        window = state.tracker.current
        if not window:
            return
        vol = compute_realized_vol(list(state.price_history))
        if vol > 0:
            state.vol_history.append(vol)
        state.prev_window = window
        state.traded_this_window = False
        state.window_high = price
        state.window_low = price
        state.prior_window_tick_counts.append(state.window_tick_count)
        state.window_tick_count = 0
        state.late_entry_evaluated = False
        state.scan_active = False
        state.scan_best_ask = None
        state.scan_best_ask_ts = None
        state.scan_direction = None
        state.scan_direction_flipped = False
        state.scan_last_checked = None
        state.early_entry_evaluated = False
        state.early_entry_traded = False
        state.early_position = None
        state.early_checkpoints_done = set()
        state.early_dca_orders = []
        state.early_hedge_order_id = None
        state.early_main_filled = 0.0
        state.early_hedge_filled = 0.0
        state.early_dca_done = set()
        state.early_up_shares = 0.0
        state.early_up_cost = 0.0
        state.early_down_shares = 0.0
        state.early_down_cost = 0.0
        state.early_rotate_done = set()
        state.early_activity_log = []
        state.early_accum_ticks = set()
        state.early_status_logged = set()
        state.early_cheap_posted = 0.0
        state.early_cheap_filled = 0.0
        state.early_confirm_done = False
        logger.info("window_opened", asset=state.asset, slug=window.slug, open_price=round(price, 2))
        state.bayesian.reset(price, 0.5)
        try:
            await resolve_window(window)
        except Exception as e:
            logger.error("market_resolve_failed", asset=state.asset, slug=window.slug, error=str(e))
        await self._refresh_orderbook(state)

    async def _on_window_close(self, state: AssetState, price: float):
        window = state.prev_window
        if not window:
            return

        if window.close_price is None:
            window.close_price = price

        went_up = window.resolved_direction == Direction.UP
        logger.info(
            "window_closed",
            asset=state.asset,
            slug=window.slug,
            open_price=window.open_price,
            close_price=window.close_price,
            direction="UP" if went_up else "DOWN",
        )

        # Provisional resolve using Coinbase (blue indicator) — will be overwritten
        # by Polymarket Chainlink oracle once confirmed
        await self.trader.resolve_window(window.slug, went_up)
        # Resolve early entry trades directly in DynamoDB (not in SQLite)
        self._resolve_early_trades(state.asset, window.slug, went_up)

        # Schedule authoritative Polymarket verification 90s after close
        slug = window.slug
        task = asyncio.create_task(self._verify_outcome_after_delay(slug, 90), name=f"verify_{slug}")
        # Verify early entry trades via Polymarket after delay (overwrites provisional)
        _early_slug = f"early_{slug}"
        early_task = asyncio.create_task(self._verify_early_polymarket(_early_slug, slug, 90), name=f"verify_early_{slug}")
        early_task.add_done_callback(lambda t: logger.error("verify_task_exception", error=str(t.exception())) if t.exception() else None)
        task.add_done_callback(lambda t: logger.error("verify_task_exception", error=str(t.exception())) if t.exception() else None)

        window_record = {
            "slug": window.slug,
            "open_ts": window.open_ts,
            "close_ts": window.close_ts,
            "open_price": window.open_price,
            "close_price": window.close_price,
            "direction": window.resolved_direction.value if window.resolved_direction else None,
            "condition_id": window.condition_id,
            "asset": state.asset,
        }
        await self.db.insert_window(window_record)
        try:
            self.dynamo.put_window(window_record)
        except Exception as e:
            logger.warning("dynamo_write_failed", error=str(e))

        # DATA_COLLECTION_MODE: log training data on every resolved window
        if os.getenv("DATA_COLLECTION_MODE", "").lower() == "true":
            try:
                tf = "5m"
                pct_move = 0.0
                if window.open_price and window.close_price and window.open_price > 0:
                    pct_move = (window.close_price - window.open_price) / window.open_price * 100
                outcome = 1 if went_up else 0
                realized_vol = compute_realized_vol(list(state.price_history))
                self.dynamo.put_training_data({
                    "window_id": f"{state.asset}_{tf}_{window.slug}",
                    "timestamp": time.time(),
                    "asset": state.asset,
                    "timeframe": tf,
                    "open_price": round(window.open_price, 2) if window.open_price else 0,
                    "close_price": round(window.close_price, 2) if window.close_price else 0,
                    "pct_move": round(pct_move, 6),
                    "outcome": outcome,
                    "direction": "up" if went_up else "down",
                    "yes_ask_at_open": round(state.orderbook.yes_best_ask, 4),
                    "no_ask_at_open": round(state.orderbook.no_best_ask, 4),
                    "yes_bid_at_open": round(state.orderbook.yes_best_bid, 4),
                    "no_bid_at_open": round(state.orderbook.no_best_bid, 4),
                    "p_bayesian": round(state.bayesian.probability, 4),
                    "realized_vol": round(realized_vol, 6),
                    "oracle_lag_pct": round(self.rtds.get_state(state.asset).oracle_lag_pct, 6),
                    # Signal-context features for LightGBM
                    "signal_move_pct": round(abs(pct_move), 6),
                    "signal_ask_price": round(state.orderbook.yes_best_ask, 4),
                    "signal_seconds": 0,  # filled by live collection
                    "signal_ev": 0,  # filled by live collection
                    # Orderbook microstructure features
                    "ofi_30s": round(self.coinbase.get_ofi_30s(state.asset), 6) if hasattr(self.coinbase, "get_ofi_30s") else 0,
                    "bid_ask_spread": round(self.coinbase.get_bid_ask_spread(state.asset), 6) if hasattr(self.coinbase, "get_bid_ask_spread") else 0,
                    "depth_imbalance": round(self.coinbase.get_depth_imbalance(state.asset), 6) if hasattr(self.coinbase, "get_depth_imbalance") else 0,
                    "trade_arrival_rate": round(self.coinbase.get_trade_arrival_rate(state.asset), 6) if hasattr(self.coinbase, "get_trade_arrival_rate") else 0,
                    "data_source": "live_with_orderbook" if hasattr(self.coinbase, "get_ofi_30s") else "live",
                    # New signal features
                    "liq_cluster_bias": round(_liq_cache.get("BTC", (0.0, 0))[0], 6),
                    "btc_confirms_direction": 0,  # late-entry strategy doesn't use BTC confirmation
                    # Macro features (collected for future model retrains)
                    **self._get_macro_features(),
                })
                logger.debug("training_data_logged", asset=state.asset, slug=window.slug, outcome=outcome)
            except Exception as e:
                logger.debug("training_data_failed", error=str(e))

        # Backfill outcome on early entry training data (logged at T+15s without outcome)
        try:
            outcome = 1 if went_up else 0
            early_window_id = f"early_{state.asset}_5m_{window.slug}"
            from decimal import Decimal as _Dec
            self.dynamo._training.update_item(
                Key={"window_id": early_window_id},
                UpdateExpression="SET outcome = :o, close_price = :cp, pct_move_final = :pm",
                ExpressionAttributeValues={
                    ":o": outcome,
                    ":cp": _Dec(str(round(window.close_price, 4))) if window.close_price else _Dec("0"),
                    ":pm": _Dec(str(round(
                        (window.close_price - window.open_price) / window.open_price * 100, 6
                    ))) if window.open_price and window.close_price else _Dec("0"),
                },
                ConditionExpression="attribute_exists(window_id)",
            )
            logger.debug("early_training_outcome_set", slug=window.slug, outcome=outcome)
        except self.dynamo._training.meta.client.exceptions.ConditionalCheckFailedException:
            pass  # No early entry training data for this window — normal
        except Exception as e:
            logger.debug("early_training_outcome_failed", error=str(e)[:60])

    async def _verify_outcome_after_delay(self, window_slug: str, delay_seconds: int = 90):
        """Wait 90s, then query Gamma API. Retry up to 5 times every 60s."""
        logger.info("resolution_scheduled", slug=window_slug, wait_seconds=delay_seconds)
        await asyncio.sleep(delay_seconds)

        for attempt in range(6):  # initial + 5 retries
            try:
                logger.info("resolution_checking", slug=window_slug, attempt=attempt + 1)
                from polybot.feeds.polymarket_rest import get_market_outcome
                # Strip early_ prefix for Polymarket API (market indexed by original slug)
                lookup_slug = window_slug.removeprefix("early_")
                winner, source = await get_market_outcome(lookup_slug)

                if winner is None:
                    if attempt < 5:
                        logger.info("resolution_pending", slug=window_slug, attempt=attempt + 1, next_retry_sec=60)
                        await asyncio.sleep(60)
                        continue
                    else:
                        logger.warning("resolution_exhausted", slug=window_slug, total_attempts=6)
                        return

                logger.info("resolution_winner_found", slug=window_slug, winner=winner, attempt=attempt + 1)

                # Check for manual sells via activity API — look for SELL trades
                # on the same condition_id AFTER our buy timestamp
                manual_sell_pnl: dict[str, float] = {}
                try:
                    import httpx as _hx
                    async with _hx.AsyncClient(timeout=10) as _ac:
                        _act_resp = await _ac.get(
                            "https://data-api.polymarket.com/activity",
                            params={"user": self._wallet_address, "limit": 100},
                        )
                        if _act_resp.status_code == 200:
                            for _a in _act_resp.json():
                                _cid = _a.get("conditionId", "")
                                _side = _a.get("side", "")
                                _type = _a.get("type", "")
                                _usdc = float(_a.get("usdcSize", 0) or 0)
                                # A manual sell shows as type=TRADE side=SELL
                                if _type == "TRADE" and _side == "SELL" and _cid:
                                    manual_sell_pnl[_cid] = manual_sell_pnl.get(_cid, 0) + _usdc
                                # A redeem also counts
                                elif _type == "REDEEM" and _cid:
                                    manual_sell_pnl[_cid] = manual_sell_pnl.get(_cid, 0) + _usdc
                except Exception:
                    pass

                # Winner confirmed — get trades from DynamoDB (NOT SQLite)
                trades = self.dynamo.get_trades_for_window(window_slug)
                for t in trades:
                    if t.get("resolved"):
                        continue  # already finalized
                    side = t.get("side", "")
                    correct = (side == winner)
                    fill_price = float(t.get("fill_price", 0) or 0)
                    size_usd = float(t.get("size_usd", 0) or 0)

                    # Check if user manually sold this position
                    _cid = t.get("condition_id", "") or ""
                    _slug_for_cid = t.get("window_slug", "")
                    sell_proceeds = manual_sell_pnl.get(_cid, 0)

                    if sell_proceeds > 0:
                        # User sold manually — P&L = sell proceeds - cost
                        pnl = sell_proceeds - size_usd
                        source = "manual_sell"
                        logger.info("manual_sell_detected", slug=window_slug, proceeds=round(sell_proceeds, 2), cost=round(size_usd, 2), pnl=round(pnl, 2))
                    elif correct and fill_price > 0:
                        shares = size_usd / fill_price
                        pnl = shares * (1.0 - fill_price)
                    else:
                        pnl = -size_usd

                    self.risk.record_trade(pnl)
                    # Update DynamoDB directly (NOT SQLite)
                    try:
                        self.dynamo.update_trade_resolved(t["id"], pnl, winner, correct, source)
                    except Exception as e:
                        logger.warning("resolution_dynamo_update_failed", id=t["id"], error=str(e)[:60])
                    # Also try SQLite as backup
                    try:
                        await self.db.update_trade_verified(
                            trade_id=t["id"], pnl=pnl,
                            polymarket_winner=winner, correct_prediction=correct,
                            outcome_source=source,
                        )
                    except Exception:
                        pass

                    logger.info(
                        "resolution_success",
                        id=t["id"],
                        slug=window_slug,
                        winner=winner,
                        our_side=side,
                        correct=correct,
                        pnl=round(pnl, 4),
                    )

                    # Update KPIs after resolution
                    try:
                        all_trades = self.dynamo.get_recent_trades(limit=200)
                        t["pnl"] = pnl  # update in-memory for KPI
                        t["resolved"] = 1
                        t["outcome_source"] = source
                        kpi = self.kpi_tracker.on_trade_resolved(t, all_trades)
                        if kpi.get("brier_skill_score") is not None:
                            logger.info(
                                "kpi_updated",
                                bss=kpi.get("brier_skill_score"),
                                wr_20=kpi.get("win_rate_last_20"),
                                sprt=kpi.get("sprt_status"),
                                separation=kpi.get("lgbm_separation"),
                            )
                    except Exception as e:
                        logger.debug(f"kpi_update_failed: {e}")

                return  # done

            except Exception as e:
                logger.warning("verify_failed", slug=window_slug, attempt=attempt, error=str(e))
                if attempt < 3:
                    await asyncio.sleep(60)

    async def _verify_sweep(self):
        """Periodic sweep: verify ALL unverified trades via Polymarket Gamma API.

        Scans the full trades table — not just 50 recent. Catches any trade
        that was resolved by Coinbase but not yet confirmed by Polymarket oracle.
        Also resolves trades still marked as unresolved (OPEN).
        """
        try:
            # Full table scan to find ALL unverified/unresolved
            _table = self.dynamo._trades
            if not _table:
                return
            all_trades = []
            resp = _table.scan()
            all_trades.extend(resp.get("Items", []))
            while "LastEvaluatedKey" in resp:
                resp = _table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
                all_trades.extend(resp.get("Items", []))

            to_verify = [t for t in all_trades if (
                # Coinbase-inferred but not Polymarket-verified
                (int(t.get("resolved", 0)) == 1
                 and t.get("outcome_source") != "polymarket_verified"
                 and t.get("outcome_source") != "manual_sell")
                # OR still unresolved (OPEN)
                or not int(t.get("resolved", 0))
            )]

            if not to_verify:
                return

            logger.info("verify_sweep_start", count=len(to_verify))
            from polybot.feeds.polymarket_rest import get_market_outcome
            fixed = 0
            for t in to_verify:
                slug = t.get("window_slug", "")
                if not slug:
                    continue
                # Strip early_ prefix for Polymarket API lookup (market indexed by original slug)
                lookup_slug = slug.removeprefix("early_")
                winner, source = await get_market_outcome(lookup_slug)
                if not winner:
                    continue
                side = t.get("side", "")
                correct = (side == winner)
                fill = float(t.get("fill_price", 0) or 0)
                size = float(t.get("size_usd", 0) or 0)
                pnl = round((size / fill) * (1 - fill), 2) if correct and fill > 0 else round(-size, 2)
                self.dynamo.update_trade_resolved(t["id"], pnl, winner, correct, source)
                fixed += 1

            if fixed:
                logger.info("verify_sweep_done", fixed=fixed)
        except Exception as e:
            logger.debug("verify_sweep_err", error=str(e)[:60])

    async def _resolve_orphan_trades(self):
        """On startup, resolve any trades left unresolved from previous sessions."""
        try:
            # Query DynamoDB (not SQLite — SQLite is empty in new containers)
            trades = self.dynamo.get_recent_trades(limit=100)
            orphans = [t for t in trades if not t.get("resolved") or str(t.get("resolved")) == "0"]
            if not orphans:
                logger.info("orphan_check_clean", count=0)
                return

            logger.info("orphan_check_found", count=len(orphans))
            from polybot.feeds.polymarket_rest import get_market_outcome

            for t in orphans:
                slug = t.get("window_slug", "")
                if not slug:
                    continue
                winner, source = await get_market_outcome(slug)
                if winner is None:
                    continue
                side = t.get("side", "")
                correct = (side == winner)
                fill = float(t.get("fill_price", 0) or 0)
                size = float(t.get("size_usd", 0) or 0)
                pnl = round((size / fill) * (1 - fill), 2) if correct and fill > 0 else round(-size, 2)

                self.risk.record_trade(pnl)
                # Update in DynamoDB directly (SQLite may be empty in new container)
                try:
                    self.dynamo.update_trade_resolved(t["id"], pnl, winner, correct, source)
                except Exception:
                    pass
                # Also try SQLite
                try:
                    await self.db.update_trade_verified(
                        trade_id=t["id"], pnl=pnl,
                        polymarket_winner=winner, correct_prediction=correct,
                        outcome_source=source,
                    )
                except Exception:
                    pass

                # Update KPIs
                try:
                    all_trades = self.dynamo.get_recent_trades(limit=200)
                    t_updated = dict(t)
                    t_updated["pnl"] = pnl
                    t_updated["resolved"] = 1
                    t_updated["outcome_source"] = source
                    self.kpi_tracker.on_trade_resolved(t_updated, all_trades)
                except Exception:
                    pass

                logger.info("orphan_resolved", id=t["id"], slug=slug, winner=winner, pnl=round(pnl, 2))
        except Exception as e:
            logger.warning("orphan_check_failed", error=str(e))

    async def _execute(self, signal, state: AssetState, signal_ms: float = 0, bedrock_ms: float = 0):
        if isinstance(self.trader, LiveTrader):
            window = state.tracker.current
            yes_id = window.yes_token_id if window else ""
            no_id = window.no_token_id if window else ""
            return await self.trader.execute(signal, yes_id, no_id, signal_ms=signal_ms, bedrock_ms=bedrock_ms)
        return await self.trader.execute(signal, signal_ms=signal_ms, bedrock_ms=bedrock_ms)

    async def _run_claim(self):
        """Run redeem.py in a subprocess so it never blocks the event loop."""
        try:
            import subprocess
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [".venv/bin/python", "scripts/redeem.py"],
                    capture_output=True, text=True, timeout=120,
                ),
            )
            if result.returncode == 0:
                logger.info("auto_claim_completed", output=result.stdout[-200:] if result.stdout else "")
            else:
                logger.warning("auto_claim_failed", stderr=result.stderr[-200:] if result.stderr else "")
        except Exception as e:
            logger.warning("auto_claim_failed", error=str(e)[:100])

    # 134 lines removed (dead method)

    def _log_v2_status(self, state: AssetState, seconds_since_open: float):
        """Log summary every 15s: shares, costs, combined avg, margin, order counts."""
        up_avg = (state.early_up_cost / state.early_up_shares) if state.early_up_shares > 0 else 0
        down_avg = (state.early_down_cost / state.early_down_shares) if state.early_down_shares > 0 else 0
        combined = up_avg + down_avg if up_avg > 0 and down_avg > 0 else 0
        margin = round((1 - combined) * 100, 1) if 0 < combined < 1 else 0
        open_orders = len([o for o in state.early_dca_orders if not o.get("filled")])
        filled_orders = len([o for o in state.early_dca_orders if o.get("filled")])
        fill_budget_remaining = round(max(self.settings.early_entry_max_bet - state.early_cheap_filled, 0), 2)
        logger.info(
            "v2_status",
            asset=state.asset,
            seconds=int(seconds_since_open),
            up_shares=int(state.early_up_shares),
            up_cost=round(state.early_up_cost, 2),
            up_avg=round(up_avg, 4),
            down_shares=int(state.early_down_shares),
            down_cost=round(state.early_down_cost, 2),
            down_avg=round(down_avg, 4),
            combined_avg=round(combined, 4),
            margin_pct=margin,
            orders_posted=len(state.early_dca_orders),
            orders_filled=filled_orders,
            fill_budget_remaining=fill_budget_remaining,
        )

    async def _shadow_tracker_loop(self):
        """Background: poll competitor wallet every 30s. Data collection only."""
        COMPETITOR = "0x63ce342161250d705dc0b16df89036c8e5f9ba9a"
        while self._running:
            try:
                await asyncio.sleep(30)
                import httpx as _hx
                async with _hx.AsyncClient(timeout=5) as c:
                    r = await c.get(
                        "https://data-api.polymarket.com/activity",
                        params={"user": COMPETITOR, "limit": 20},
                    )
                if r.status_code != 200:
                    continue
                activity = r.json()
                if not activity:
                    continue
                # Map to current 5m windows
                for key, state in list(self.asset_states.items()):
                    if "_1h" in key:
                        continue
                    window = state.tracker.current
                    if not window:
                        continue
                    slug = window.slug
                    his_up = [a for a in activity if slug in a.get("title", "") and "YES" in a.get("side", "")]
                    his_down = [a for a in activity if slug in a.get("title", "") and "NO" in a.get("side", "")]
                    if not his_up and not his_down:
                        continue
                    try:
                        import uuid
                        from decimal import Decimal as _D
                        pos = state.early_position
                        self.dynamo._trades.meta.client.put_item(
                            TableName="competitor-shadow",
                            Item={
                                "id": {"S": str(uuid.uuid4())},
                                "window_slug": {"S": slug},
                                "asset": {"S": state.asset},
                                "timestamp": {"N": str(round(time.time(), 1))},
                                "his_up_count": {"N": str(len(his_up))},
                                "his_down_count": {"N": str(len(his_down))},
                                "our_direction": {"S": ("UP" if pos["direction_up"] else "DOWN") if pos else ""},
                                "divergence": {"BOOL": (bool(his_up) and bool(pos) and not pos["direction_up"]) or
                                               (bool(his_down) and bool(pos) and pos["direction_up"])},
                            }
                        )
                        logger.info("shadow_tracked", asset=state.asset, slug=slug[-20:],
                                    his_up=len(his_up), his_down=len(his_down))
                    except Exception:
                        pass  # DynamoDB table may not exist yet — silent fail
            except Exception:
                pass  # Never slow down trading

    def _log_activity(self, state: AssetState, action: str, detail: str = ""):
        """Append action to rolling activity log for dashboard."""
        window = state.tracker.current
        secs = int(time.time() - window.open_ts) if window and window.open_ts else 0
        entry = f"T+{secs}s {action}"
        if detail:
            entry += f" {detail}"
        state.early_activity_log.append(entry)
        if len(state.early_activity_log) > 20:
            state.early_activity_log = state.early_activity_log[-20:]

    def _write_live_state_async(self, state: AssetState, price: float, seconds_since_open: float):
        """Write current state to DynamoDB for dashboard. Fire-and-forget."""
        try:
            if not self.dynamo or not self.dynamo._available:
                return
            from decimal import Decimal
            import boto3
            pos = state.early_position
            window = state.tracker.current

            # Determine phase
            if seconds_since_open < 0:
                phase = "WAITING"
            elif seconds_since_open < 15:
                phase = "PRE-POSITION"
            elif seconds_since_open < 30:
                phase = "CONFIRM"
            elif seconds_since_open < 270:
                phase = "ACCUMULATE"
            else:
                phase = "HOLD"

            # Compute combined avg
            up_avg = (state.early_up_cost / state.early_up_shares) if state.early_up_shares > 0 else 0
            down_avg = (state.early_down_cost / state.early_down_shares) if state.early_down_shares > 0 else 0
            combined = up_avg + down_avg if up_avg > 0 and down_avg > 0 else 0
            margin = round((1 - combined) * 100, 1) if 0 < combined < 1 else 0

            item = {
                "asset": state.asset,
                "timestamp": Decimal(str(round(time.time(), 1))),
                "price": Decimal(str(round(price, 2))),
                "seconds": Decimal(str(int(seconds_since_open))),
                "phase": phase,
                "slug": (window.slug if window else "") or "",
                "direction": ("UP" if pos["direction_up"] else "DOWN") if pos else "",
                "lgbm_prob": Decimal(str(round(self.model_server.predict(f"{state.asset}_5m", {}) if False else 0, 3))),
                "up_shares": Decimal(str(int(state.early_up_shares))),
                "up_cost": Decimal(str(round(state.early_up_cost, 2))),
                "up_avg": Decimal(str(round(up_avg, 4))),
                "down_shares": Decimal(str(int(state.early_down_shares))),
                "down_cost": Decimal(str(round(state.early_down_cost, 2))),
                "down_avg": Decimal(str(round(down_avg, 4))),
                "combined_avg": Decimal(str(round(combined, 4))),
                "margin": Decimal(str(margin)),
                "yes_ask": Decimal(str(round(state.orderbook.yes_best_ask, 4))),
                "yes_bid": Decimal(str(round(state.orderbook.yes_best_bid, 4))),
                "no_ask": Decimal(str(round(state.orderbook.no_best_ask, 4))),
                "no_bid": Decimal(str(round(state.orderbook.no_best_bid, 4))),
                "open_orders": Decimal(str(len([o for o in state.early_dca_orders if not o.get("filled")]))),
                "filled_orders": Decimal(str(len([o for o in state.early_dca_orders if o.get("filled")]))),
                "main_filled": Decimal(str(round(state.early_main_filled, 2))),
                "hedge_filled": Decimal(str(round(state.early_hedge_filled, 2))),
                "cheap_filled": Decimal(str(round(state.early_cheap_filled, 2))),
                "has_position": 1 if pos else 0,
                "activity": state.early_activity_log[-20:] if state.early_activity_log else [],
            }

            # Fire and forget
            profile = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None
            _live_table = boto3.Session(profile_name=profile, region_name="eu-west-1").resource("dynamodb").Table("polymarket-bot-live-state")
            _live_table.put_item(Item=item)
        except Exception:
            pass  # Never slow down trading

    def _get_macro_features(self) -> dict:
        """Get macro features for training data. Never fails — returns defaults if API down."""
        try:
            return {k: round(v, 6) if isinstance(v, float) else v for k, v in self._macro.get_all().items()}
        except Exception:
            return {}

    async def _refresh_orderbook(self, state: AssetState):
        window = state.tracker.current
        if not window or not window.yes_token_id:
            if window:
                logger.debug("orderbook_skip_no_token", asset=state.asset, slug=window.slug)
            return
        now = time.time()
        if now - state.orderbook_age < 1.0:  # Max 1 refresh/second
            return
        state.orderbook_age = now
        try:
            yes_book = await get_orderbook(window.yes_token_id)
            no_book = await get_orderbook(window.no_token_id) if window.no_token_id else {}

            snap = OrderbookSnapshot(timestamp=now)
            yes_asks = yes_book.get("asks", [])
            yes_bids = yes_book.get("bids", [])
            no_asks = no_book.get("asks", [])
            no_bids = no_book.get("bids", [])
            if not yes_asks and not no_asks:
                logger.warning(
                    "orderbook_empty",
                    asset=state.asset,
                    slug=window.slug,
                    yes_token_len=len(window.yes_token_id),
                    no_token_len=len(window.no_token_id) if window.no_token_id else 0,
                    yes_book_keys=list(yes_book.keys())[:5],
                )
            if yes_asks:
                snap.yes_best_ask = min(float(a["price"]) for a in yes_asks)
            if yes_bids:
                snap.yes_best_bid = max(float(b["price"]) for b in yes_bids)
                # Sum of top-3 bid sizes (sorted desc by price, take first 3)
                top3_yes = sorted(yes_bids, key=lambda b: float(b["price"]), reverse=True)[:3]
                snap.yes_bid_depth = sum(float(b.get("size", 0)) for b in top3_yes)
            if no_asks:
                snap.no_best_ask = min(float(a["price"]) for a in no_asks)
            if no_bids:
                snap.no_best_bid = max(float(b["price"]) for b in no_bids)
                top3_no = sorted(no_bids, key=lambda b: float(b["price"]), reverse=True)[:3]
                snap.no_bid_depth = sum(float(b.get("size", 0)) for b in top3_no)
            state.orderbook = snap
        except Exception as e:
            logger.error("orderbook_refresh_failed", asset=state.asset, error=str(e))
