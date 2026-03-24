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

import structlog

from polybot.config import Settings
from polybot.execution.live_trader import LiveTrader
from polybot.execution.paper_trader import PaperTrader
from polybot.feeds.coinbase_ws import CoinbaseWS
from polybot.feeds.polymarket_rest import get_orderbook
from polybot.feeds.rtds_ws import (
    RTDSClient,
    compute_realized_vol,
)
from polybot.market.market_resolver import resolve_window
from polybot.market.window_tracker import WindowTracker
from polybot.models import Direction, OrderbookSnapshot, Window
from polybot.risk.manager import RiskManager
from polybot.storage.db import Database
from polybot.storage.dynamo import DynamoStore
from polybot.strategy.base_rate import BaseRateTable
from polybot.strategy.bayesian import BayesianUpdater

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
    "BTC": (
        "data/candles/btc_usd_1min.parquet",
        "candles/btc_usd_1min.parquet",
        "/tmp/btc_usd_1min.parquet",
    ),
    "ETH": (
        "data/candles/eth_usd_1min.parquet",
        "candles/eth_usd_1min.parquet",
        "/tmp/eth_usd_1min.parquet",
    ),
    "SOL": (
        "data/candles/sol_usd_1min.parquet",
        "candles/sol_usd_1min.parquet",
        "/tmp/sol_usd_1min.parquet",
    ),
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
    price_history: deque = field(
        default_factory=lambda: deque(maxlen=200)
    )  # for realized vol
    vol_history: deque = field(
        default_factory=lambda: deque(maxlen=12)
    )  # rolling vol for vol_ma_1h
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
    early_position: dict | None = (
        None  # {slug, token_id, shares, entry_price, direction_up, side}
    )
    early_checkpoints_done: set = field(default_factory=set)  # {60, 120, 180}
    # DCA + hedge tracking
    early_dca_orders: list = field(
        default_factory=list
    )  # [{order_id, side, price, size, filled}]
    early_hedge_order_id: str | None = None
    early_main_filled: float = 0.0  # total USD filled on main side
    early_hedge_filled: float = 0.0  # total USD filled on hedge side
    early_dca_done: set = field(
        default_factory=set
    )  # {15, 45, 90} — which DCA rounds fired
    # Per-side share tracking (for stop-and-rotate)
    early_up_shares: float = 0.0
    early_up_cost: float = 0.0
    early_down_shares: float = 0.0
    early_down_cost: float = 0.0
    early_rotate_done: set = field(
        default_factory=set
    )  # which 30s intervals posted cheap limits
    early_activity_log: list = field(
        default_factory=list
    )  # last 20 actions for dashboard
    early_accum_ticks: set = field(
        default_factory=set
    )  # which 3s ticks fired accumulation
    early_status_logged: set = field(
        default_factory=set
    )  # which 15s intervals logged status
    early_cheap_posted: float = 0.0  # cumulative USD successfully posted this window
    early_cheap_filled: float = 0.0  # compatibility alias for filled_position_cost_usd
    reserved_open_order_usd: float = 0.0  # reserved USD on open early-entry orders
    filled_position_cost_usd: float = 0.0  # actual filled USD on early-entry orders
    early_reserved_notional: float = (
        0.0  # compatibility alias for reserved_open_order_usd
    )
    early_filled_notional: float = (
        0.0  # compatibility alias for filled_position_cost_usd
    )
    early_confirm_done: bool = False  # Phase 2 T+15s confirm fired for this window
    early_last_fill_ts: float = 0.0  # epoch time of last fill (for no-fill kicker)
    v2_last_sell_ts: float = 0.0  # epoch time of last sell (60s cooldown)
    v2_last_sell_side_up: bool | None = None  # which side was last sold
    v2_last_sell_price_up: float = 0.0  # price we last sold UP at
    v2_last_sell_price_down: float = 0.0  # price we last sold DOWN at
    v2_last_rescue_ts: float = 0.0  # epoch time of last incomplete-pair rescue repost


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
                logger.info(
                    "base_rates_loaded", asset=asset, source=path, bins=len(table.bins)
                )
                return table

        try:
            import boto3

            s3 = boto3.client("s3", region_name="eu-west-1")
            s3.download_file(S3_BUCKET, s3_key, tmp_path)
            table.load_from_parquet(tmp_path)
            logger.info(
                "base_rates_loaded", asset=asset, source="s3", bins=len(table.bins)
            )
        except Exception as e:
            logger.warning("base_rates_load_failed", asset=asset, error=str(e))
        return table

    def __init__(self, settings: Settings):
        self.settings = settings
        enabled = settings.enabled_pairs
        watched = list(getattr(settings, "watch_pair_list", []) or [])
        tracked_5m: list[tuple[str, int]] = []
        seen_pairs: set[tuple[str, int]] = set()
        for pair in enabled + watched:
            asset, dur = pair
            if dur != 300:
                continue
            if pair in seen_pairs:
                continue
            tracked_5m.append(pair)
            seen_pairs.add(pair)
        # Unique assets needed for price feeds.
        # Always include BTC/ETH/SOL/XRP so 1h watch-only states still get ticks.
        assets = sorted({a for a, _ in tracked_5m} | {"BTC", "ETH", "SOL", "XRP"})
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
        from polybot.ml.kpi_tracker import KPITracker
        from polybot.ml.server import ModelServer

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

        # One AssetState per tracked 5m pair. Trading remains gated by enabled_pairs.
        self.asset_states: dict[str, AssetState] = {}
        for asset, dur in tracked_5m:
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

    @staticmethod
    def _timeframe_key(window_seconds: int) -> str:
        mapping = {300: "5m", 900: "15m", 3600: "1h"}
        return mapping.get(window_seconds, f"{window_seconds}s")

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
                    if (
                        polygon_usdc > 0
                        and abs(polygon_usdc - self.risk.bankroll) > 0.50
                    ):
                        self.risk.bankroll = polygon_usdc
                        logger.info(
                            "bankroll_updated_from_balance",
                            bankroll=round(polygon_usdc, 2),
                        )
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
        # Cancel all open GTC orders before shutdown (prevent orphaned orders)
        logger.info("stopping_cancel_orders")
        cancel_count = 0
        for asset_key, state in getattr(self, "asset_states", {}).items():
            for order in getattr(state, "early_dca_orders", []):
                oid = order.get("order_id", "")
                if oid and not order.get("closed") and not order.get("filled"):
                    try:
                        if hasattr(self, "trader") and hasattr(self.trader, "client"):
                            self.trader.client.cancel(oid)
                            order["closed"] = True
                            cancel_count += 1
                            logger.info(
                                "shutdown_cancel", asset=asset_key, oid=oid[:16]
                            )
                    except Exception as e:
                        logger.warning(
                            "shutdown_cancel_fail", oid=oid[:16], err=str(e)[:80]
                        )
        logger.info("shutdown_cancelled_orders", count=cancel_count)
        await self.coinbase.close()
        await self.rtds.close()
        await self.db.close()
        logger.info("loop_stopped")

    def _v2_max_bet_per_asset(self) -> float:
        # V2 per-asset budget from early_entry_max_bet setting (Secrets Manager)
        return getattr(self.settings, "early_entry_max_bet", 25.0) or 25.0

    def _v2_reserved_open_order_usd(self, state: AssetState) -> float:
        return round(
            max(state.reserved_open_order_usd, state.early_reserved_notional), 2
        )

    def _set_v2_reserved_open_order_usd(self, state: AssetState, amount: float) -> None:
        reserved = round(max(amount, 0.0), 2)
        state.reserved_open_order_usd = reserved
        state.early_reserved_notional = reserved

    def _v2_filled_position_cost_usd(self, state: AssetState) -> float:
        return round(
            max(
                state.filled_position_cost_usd,
                state.early_filled_notional,
                state.early_cheap_filled,
            ),
            2,
        )

    def _set_v2_filled_position_cost_usd(
        self, state: AssetState, amount: float
    ) -> None:
        filled = round(max(amount, 0.0), 2)
        state.filled_position_cost_usd = filled
        state.early_filled_notional = filled
        state.early_cheap_filled = filled

    def _v2_filled_notional(self, state: AssetState) -> float:
        return self._v2_filled_position_cost_usd(state)

    def _set_v2_filled_notional(self, state: AssetState, amount: float) -> None:
        self._set_v2_filled_position_cost_usd(state, amount)

    def _v2_current_total_notional(self, state: AssetState) -> float:
        return round(
            self._v2_filled_position_cost_usd(state)
            + self._v2_reserved_open_order_usd(state),
            2,
        )

    def _v2_min_order_shares(self) -> int:
        return 5

    def _v2_order_size(self, target_usd: float, price: float) -> tuple[int, float]:
        """Convert target USD into executable whole shares and actual order notional."""
        if target_usd <= 0 or price <= 0 or price >= 1:
            return 0, 0.0
        shares = max(int(round(target_usd / price, 0)), self._v2_min_order_shares())
        actual_notional_usd = round(shares * price, 2)
        return shares, actual_notional_usd

    def _v2_float(self, value) -> float:
        try:
            return float(value or 0)
        except (TypeError, ValueError):
            return 0.0

    def _v2_order_actual_shares(self, order: dict) -> int:
        return int(order.get("actual_shares", order.get("shares", 0)) or 0)

    def _v2_order_actual_price(self, order: dict) -> float:
        return self._v2_float(order.get("actual_price", order.get("price", 0)))

    def _v2_order_actual_notional(self, order: dict) -> float:
        return round(
            self._v2_float(order.get("actual_notional_usd", order.get("size", 0))), 2
        )

    def _v2_order_inventory_shares(self, order: dict) -> int:
        return int(order.get("inventory_shares", order.get("filled_shares", 0)) or 0)

    def _v2_order_inventory_notional(self, order: dict) -> float:
        return round(
            self._v2_float(
                order.get("inventory_notional_usd", order.get("filled_notional_usd", 0))
            ),
            2,
        )

    def _set_v2_order_inventory(
        self, order: dict, shares: int, notional: float
    ) -> None:
        order["inventory_shares"] = max(int(shares), 0)
        order["inventory_notional_usd"] = round(max(notional, 0.0), 2)

    def _v2_order_reserved_remaining(self, order: dict) -> float:
        return round(
            self._v2_float(
                order.get(
                    "remaining_reserved_notional_usd",
                    order.get(
                        "reserved_notional_usd_remaining",
                        self._v2_order_actual_notional(order),
                    ),
                )
            ),
            2,
        )

    def _set_v2_order_reserved_remaining(self, order: dict, amount: float) -> None:
        remaining = round(max(amount, 0.0), 2)
        order["remaining_reserved_notional_usd"] = remaining
        order["reserved_notional_usd_remaining"] = remaining

    def _build_v2_tracked_order(
        self,
        order_id: str,
        actual_shares: int,
        actual_price: float,
        actual_notional_usd: float,
        side: str,
        target_size: float | None = None,
    ) -> dict:
        return {
            "order_id": order_id,
            "shares": actual_shares,
            "price": actual_price,
            "size": actual_notional_usd,
            "actual_shares": actual_shares,
            "actual_price": actual_price,
            "actual_notional_usd": actual_notional_usd,
            "remaining_reserved_notional_usd": actual_notional_usd,
            "reserved_notional_usd_remaining": actual_notional_usd,
            "filled_shares": 0,
            "filled_notional_usd": 0.0,
            "inventory_shares": 0,
            "inventory_notional_usd": 0.0,
            "target_size": target_size,
            "side": side,
            "budget_reserved": True,
            "created_at": time.time(),
        }

    def _v2_fill_progress(self, order: dict, resp: dict) -> tuple[int, float, bool]:
        actual_shares = self._v2_order_actual_shares(order)
        actual_price = self._v2_order_actual_price(order)
        actual_notional_usd = self._v2_order_actual_notional(order)
        prev_filled_shares = int(order.get("filled_shares", 0) or 0)
        prev_filled_notional = round(
            self._v2_float(order.get("filled_notional_usd", 0)), 2
        )
        status = str(resp.get("status", "") if isinstance(resp, dict) else "").upper()

        if status in ("MATCHED", "FILLED"):
            return actual_shares, actual_notional_usd, True

        size_matched = self._v2_float(
            resp.get("size_matched", 0) if isinstance(resp, dict) else 0
        )
        total_filled_shares = (
            int(round(size_matched)) if size_matched > 0 else prev_filled_shares
        )
        total_filled_shares = max(
            min(total_filled_shares, actual_shares), prev_filled_shares
        )
        total_filled_notional = (
            round(total_filled_shares * actual_price, 2)
            if actual_price > 0
            else prev_filled_notional
        )
        total_filled_notional = max(
            min(total_filled_notional, actual_notional_usd), prev_filled_notional
        )
        is_complete = (
            total_filled_notional >= actual_notional_usd - 1e-9
            if actual_notional_usd > 0
            else False
        )
        return total_filled_shares, total_filled_notional, is_complete

    def _sync_v2_position_from_fills(self, state: AssetState) -> None:
        """Keep the sellable main position aligned with actual filled shares/cost."""
        pos = state.early_position
        if not pos:
            return
        if pos.get("direction_up", True):
            pos["shares"] = int(round(state.early_up_shares))
            pos["size"] = round(state.early_up_cost, 2)
        else:
            pos["shares"] = int(round(state.early_down_shares))
            pos["size"] = round(state.early_down_cost, 2)

    def _early_entry_active_for_state(self, state: AssetState) -> bool:
        if not self.settings.early_entry_enabled:
            return False
        if state.tracker.window_seconds != 300:
            return False
        enabled_pairs = set(getattr(self.settings, "enabled_pairs", []) or [])
        return (state.asset, state.tracker.window_seconds) in enabled_pairs

    def _v2_order_execution_enabled(self) -> bool:
        return self.settings.mode in ("live", "paper") and hasattr(
            self.trader, "client"
        )

    def _v2_get_order_status(self, state: AssetState, order: dict) -> dict:
        oid = order.get("order_id", "")
        if not oid or not hasattr(self.trader, "client"):
            return {"status": "UNKNOWN"}

        resp = self.trader.client.get_order(oid)
        if self.settings.mode != "paper":
            return resp

        status = str(resp.get("status", "") if isinstance(resp, dict) else "").upper()
        if status in (
            "MATCHED",
            "FILLED",
            "CANCELED",
            "CANCELLED",
            "REJECTED",
            "EXPIRED",
        ):
            return resp

        direction = self._v2_order_direction(state, order)
        if direction is True:
            best_ask = self._v2_float(state.orderbook.yes_best_ask)
        elif direction is False:
            best_ask = self._v2_float(state.orderbook.no_best_ask)
        else:
            best_ask = 0.0

        actual_price = self._v2_order_actual_price(order)
        if best_ask > 0 and actual_price >= best_ask - 1e-9:
            actual_shares = self._v2_order_actual_shares(order)
            if hasattr(self.trader.client, "mark_filled"):
                self.trader.client.mark_filled(oid, actual_shares)
            return {"status": "FILLED", "size_matched": str(actual_shares)}

        return resp if isinstance(resp, dict) else {"status": "LIVE"}

    def _v2_remaining_budget(self, state: AssetState) -> float:
        max_bet_per_asset = self._v2_max_bet_per_asset()
        return round(
            max(max_bet_per_asset - self._v2_current_total_notional(state), 0.0), 2
        )

    def _v2_order_direction(self, state: AssetState, order: dict) -> bool | None:
        pos = state.early_position or {}
        direction_up = pos.get("direction_up", True)
        side = order.get("side", "")
        if side in ("UP", "YES"):
            return True
        if side in ("DOWN", "NO"):
            return False
        if side == "main":
            return direction_up
        if side == "hedge":
            return not direction_up
        if side == "rotate":
            return direction_up
        return None

    def _v2_setting_float(self, name: str, default: float) -> float:
        value = getattr(self.settings, name, default)
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        return default

    def _v2_open_budget_pct(self) -> float:
        return 0.10

    def _v2_sell_start_seconds(self) -> float:
        return 45.0

    def _v2_bad_pair_sell_start_seconds(self) -> float:
        return 20.0

    def _v2_buy_only_start_seconds(self) -> float:
        return 180.0

    def _v2_commit_start_seconds(self) -> float:
        return 250.0

    def _v2_sell_cooldown_seconds(self) -> float:
        return 30.0

    def _v2_rescue_retry_seconds(self) -> float:
        return 15.0

    def _v2_salvage_start_seconds(self) -> float:
        return 90.0

    def _v2_orphan_salvage_min_bid(self) -> float:
        return 0.30

    def _v2_allocation_split(self, prob_up: float) -> tuple[float, float]:
        if prob_up >= 0.70:
            return 0.80, 0.20
        if prob_up >= 0.62:
            return 0.70, 0.30
        if prob_up >= 0.56:
            return 0.60, 0.40
        if prob_up > 0.44:
            return 0.50, 0.50
        if prob_up > 0.38:
            return 0.40, 0.60
        if prob_up > 0.30:
            return 0.30, 0.70
        return 0.20, 0.80

    def _v2_confidence_budget_scale(self, prob_up: float) -> float:
        """Spend less when the model edge is weak; neutral windows should stay small."""
        edge = round(abs(prob_up - 0.50), 3)
        if edge < 0.03:
            return 0.35
        if edge < 0.06:
            return 0.65
        if edge < 0.10:
            return 0.85
        if edge >= 0.20:
            return 1.10
        return 1.00

    def _v2_strong_favored_budget_boost(
        self,
        prob_up: float,
        usable: float,
        up_budget: float,
        down_budget: float,
        up_bid: float,
        down_bid: float,
        up_shares: int,
        down_shares: int,
    ) -> tuple[float, float]:
        edge = round(abs(prob_up - 0.50), 3)
        if edge < 0.10 or usable <= 0:
            return up_budget, down_budget

        favored_up = prob_up >= 0.50
        favored_budget = up_budget if favored_up else down_budget
        unfavored_budget = down_budget if favored_up else up_budget
        favored_bid = up_bid if favored_up else down_bid
        favored_shares = up_shares if favored_up else down_shares
        unfavored_shares = down_shares if favored_up else up_shares

        if favored_bid <= 0:
            return up_budget, down_budget

        # Do not keep biasing the same side if it is already clearly ahead in shares.
        if favored_shares > unfavored_shares + 5:
            return up_budget, down_budget

        min_orders = 2 if edge >= 0.20 else 1
        target_favored_budget = round(5 * favored_bid * min_orders, 2)
        target_favored_budget = min(target_favored_budget, usable)
        if favored_budget >= target_favored_budget - 1e-9:
            return up_budget, down_budget

        transfer = min(
            unfavored_budget, round(target_favored_budget - favored_budget, 2)
        )
        if transfer <= 0:
            return up_budget, down_budget

        if favored_up:
            return round(up_budget + transfer, 2), round(down_budget - transfer, 2)
        return round(up_budget - transfer, 2), round(down_budget + transfer, 2)

    def _v2_pair_risk_limits(self, prob_up: float) -> tuple[float, float]:
        """Pair guardrails once both sides exist."""
        edge = round(abs(prob_up - 0.50), 3)
        if edge < 0.03:
            return 1.00, 0.50
        if edge < 0.06:
            return 1.02, 1.00
        if edge < 0.10:
            return 1.04, 1.50
        return 1.06, 2.00

    def _v2_expensive_side_price_cap(self, seconds_since_open: float) -> float:
        """Tighten the maximum acceptable rich-side price later in the window.

        Core rule: never buy above 55c on either side.  In a binary market
        where UP + DOWN ≈ $1.00, anything above 50c is the expensive side.
        We allow up to 55c to handle the open phase where both sides are
        near 50c, but after that we should be selling the expensive side,
        not buying it.
        """
        if seconds_since_open >= 120:
            return 0.50
        if seconds_since_open >= 60:
            return 0.52
        return 0.55

    def _v2_incomplete_pair_rescue_price(
        self, bid: float, ask: float, hard_cap: float
    ) -> float:
        """Use a more aggressive missing-side repost than the normal passive ladder."""
        if ask > 0:
            price = round(min(ask, hard_cap), 2)
            if price >= 0.01:
                return price
        if bid > 0:
            price = round(min(bid + 0.01, hard_cap), 2)
            if price >= 0.01:
                return price
        return 0.0

    def _v2_expected_position_ev(
        self,
        prob_up: float,
        up_shares: int,
        down_shares: int,
        net_cost: float,
    ) -> float:
        expected_payout = (prob_up * up_shares) + ((1.0 - prob_up) * down_shares)
        return round(expected_payout - net_cost, 2)

    def _v2_projected_pair_metrics(
        self,
        state: AssetState,
        prob_up: float,
        current_total_notional: float,
        side_up: bool,
        shares: int,
        notional: float,
    ) -> dict[str, float | int]:
        new_up_shares = int(state.early_up_shares) + (shares if side_up else 0)
        new_down_shares = int(state.early_down_shares) + (shares if not side_up else 0)
        new_up_cost = float(state.early_up_cost) + (notional if side_up else 0.0)
        new_down_cost = float(state.early_down_cost) + (
            notional if not side_up else 0.0
        )
        new_up_avg = (new_up_cost / new_up_shares) if new_up_shares > 0 else 0.0
        new_down_avg = (new_down_cost / new_down_shares) if new_down_shares > 0 else 0.0
        new_combined = (
            (new_up_avg + new_down_avg)
            if (new_up_shares > 0 and new_down_shares > 0)
            else 0.0
        )
        payout_floor = min(new_up_shares, new_down_shares)
        projected_total = round(current_total_notional + notional, 2)
        cost_above_floor = max(round(projected_total - payout_floor, 2), 0.0)
        expected_ev = self._v2_expected_position_ev(
            prob_up=prob_up,
            up_shares=new_up_shares,
            down_shares=new_down_shares,
            net_cost=projected_total,
        )
        return {
            "combined_avg": round(new_combined, 4),
            "payout_floor": payout_floor,
            "cost_above_floor": round(cost_above_floor, 2),
            "expected_ev": expected_ev,
        }

    def _v2_projected_sell_metrics(
        self,
        state: AssetState,
        *,
        prob_up: float,
        current_total_notional: float,
        side_up: bool,
        shares: int,
        proceeds: float,
    ) -> dict[str, float | int]:
        current_up_shares = int(state.early_up_shares)
        current_down_shares = int(state.early_down_shares)
        current_up_cost = float(state.early_up_cost)
        current_down_cost = float(state.early_down_cost)

        sell_from_shares = current_up_shares if side_up else current_down_shares
        sell_from_cost = current_up_cost if side_up else current_down_cost
        if sell_from_shares <= 0 or shares <= 0 or shares > sell_from_shares:
            return {
                "combined_avg": 9.99,
                "payout_floor": 0,
                "cost_above_floor": 9.99,
                "expected_ev": -9.99,
            }

        unit_cost = sell_from_cost / sell_from_shares if sell_from_shares > 0 else 0.0
        removed_cost = round(unit_cost * shares, 2)
        new_up_shares = current_up_shares - shares if side_up else current_up_shares
        new_down_shares = (
            current_down_shares - shares if not side_up else current_down_shares
        )
        new_up_cost = (
            max(round(current_up_cost - removed_cost, 2), 0.0)
            if side_up
            else current_up_cost
        )
        new_down_cost = (
            max(round(current_down_cost - removed_cost, 2), 0.0)
            if not side_up
            else current_down_cost
        )
        new_up_avg = (new_up_cost / new_up_shares) if new_up_shares > 0 else 0.0
        new_down_avg = (new_down_cost / new_down_shares) if new_down_shares > 0 else 0.0
        new_combined = (
            (new_up_avg + new_down_avg)
            if (new_up_shares > 0 and new_down_shares > 0)
            else 0.0
        )
        payout_floor = min(new_up_shares, new_down_shares)
        projected_total = max(round(current_total_notional - proceeds, 2), 0.0)
        cost_above_floor = max(round(projected_total - payout_floor, 2), 0.0)
        expected_ev = self._v2_expected_position_ev(
            prob_up=prob_up,
            up_shares=new_up_shares,
            down_shares=new_down_shares,
            net_cost=projected_total,
        )
        return {
            "combined_avg": round(new_combined, 4),
            "payout_floor": payout_floor,
            "cost_above_floor": round(cost_above_floor, 2),
            "expected_ev": expected_ev,
        }

    def _v2_orphan_pair_state(self, state: AssetState) -> tuple[bool, bool | None]:
        up_shares = int(state.early_up_shares)
        down_shares = int(state.early_down_shares)
        if up_shares >= 5 and down_shares <= 0:
            return True, False
        if down_shares >= 5 and up_shares <= 0:
            return True, True
        return False, None

    def _v2_should_salvage_orphan(
        self,
        *,
        prob_up: float,
        seconds_since_open: float,
        orphan_side_up: bool,
        total_shares: int,
        current_bid: float,
        projected: dict[str, float | int],
        max_combined_avg: float,
        max_cost_above_floor: float,
    ) -> bool:
        projected_combined = float(projected["combined_avg"])
        projected_cost_above_floor = float(projected["cost_above_floor"])
        projected_ev = float(projected["expected_ev"])
        edge = abs(prob_up - 0.50)
        favored_side_up = prob_up >= 0.50
        projected_bad = (
            projected_combined > max_combined_avg + 1e-9
            or projected_cost_above_floor > max_cost_above_floor + 1e-9
            or projected_ev < -0.01
        )
        if current_bid < self._v2_orphan_salvage_min_bid():
            return False
        if total_shares > 10:
            return False
        if edge >= 0.08 and orphan_side_up == favored_side_up:
            return False
        normal_salvage_due = seconds_since_open >= self._v2_salvage_start_seconds()
        early_strong_flip_salvage = (
            total_shares <= 5
            and edge >= 0.10
            and orphan_side_up != favored_side_up
            and seconds_since_open >= 20.0
        )
        if not (normal_salvage_due or early_strong_flip_salvage):
            return False
        return projected_bad

    def _v2_rescue_worth_completing(
        self,
        *,
        prob_up: float,
        projected: dict[str, float | int],
        max_combined_avg: float,
        max_cost_above_floor: float,
    ) -> bool:
        """Only rescue an orphan if completing the pair is still economically sane."""
        projected_combined = float(projected["combined_avg"])
        projected_cost_above_floor = float(projected["cost_above_floor"])
        projected_ev = float(projected["expected_ev"])
        edge = round(abs(prob_up - 0.50), 3)
        ev_floor = -0.01 if edge >= 0.10 else 0.0
        combined_buffer = 0.01 if edge >= 0.10 else 0.0
        return (
            projected_combined <= max_combined_avg + combined_buffer + 1e-9
            and projected_cost_above_floor <= max_cost_above_floor + 1e-9
            and projected_ev >= ev_floor
        )

    def _v2_bad_pair_recycle_active(
        self,
        *,
        current_combined_avg: float,
        current_cost_above_floor: float,
        current_position_ev: float,
        max_combined_avg: float,
        max_cost_above_floor: float,
    ) -> bool:
        """Detect complete pairs that are bad enough to justify trimming a rich side."""
        return (
            current_combined_avg > max_combined_avg + 0.02
            or current_cost_above_floor > max_cost_above_floor + 0.15
            or current_position_ev < -0.05
        )

    def _v2_budget_curve_pct(self, seconds_since_open: float) -> float:
        """Small open, main deployment mid-window, tight late-window add-on."""
        open_pct = self._v2_open_budget_pct()
        if seconds_since_open <= 5:
            return open_pct
        if seconds_since_open <= 60:
            progress = (seconds_since_open - 5.0) / 55.0
            return round(open_pct + (0.12 * progress), 4)  # 10% → 22%
        if seconds_since_open <= 180:
            progress = (seconds_since_open - 60.0) / 120.0
            return round(0.22 + (0.60 * progress), 4)  # 22% → 82%
        if seconds_since_open <= 250:
            progress = (seconds_since_open - 180.0) / 70.0
            return round(0.82 + (0.10 * progress), 4)  # 82% → 92%
        return 0.92

    def _v2_reprice_stale_after_seconds(self) -> float:
        value = self._v2_setting_float("early_entry_reprice_stale_after_seconds", 6.0)
        return value if value > 0 else 6.0

    def _v2_reprice_price_tolerance(self) -> float:
        value = self._v2_setting_float("early_entry_reprice_price_tolerance", 0.01)
        return max(value, 0.0)

    def _v2_open_orders(self, state: AssetState) -> list[dict]:
        return [
            order
            for order in state.early_dca_orders
            if not order.get("filled") and not order.get("closed")
        ]

    def _v2_normalized_order_side(self, state: AssetState, order: dict) -> str:
        direction = self._v2_order_direction(state, order)
        if direction is True:
            return "UP"
        if direction is False:
            return "DOWN"
        return str(order.get("side", "")).upper()

    def _v2_accum_plan(self, bid: float) -> tuple[list[float], float, str]:
        if bid <= 0.15:
            return (
                [0.00, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08],
                0.35,
                "lottery",
            )
        if bid <= 0.35:
            return [0.00, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06], 0.25, "cheap"
        if bid <= 0.60:
            return [0.00, 0.01, 0.02, 0.03, 0.05], 0.20, "mid"
        return [0.00, 0.01, 0.03], 0.15, "winning"

    def _v2_accum_specs(self, bid: float) -> tuple[str, list[dict]]:
        offsets, target_size, tier = self._v2_accum_plan(bid)
        specs: list[dict] = []
        seen_prices: set[float] = set()
        for offset in offsets:
            post_price = round(bid - offset, 2)
            if post_price < 0.01 or post_price > 0.98 or post_price in seen_prices:
                continue
            shares, actual_notional_usd = self._v2_order_size(target_size, post_price)
            if shares <= 0 or actual_notional_usd <= 0:
                continue
            seen_prices.add(post_price)
            specs.append(
                {
                    "post_price": post_price,
                    "shares": shares,
                    "actual_notional_usd": actual_notional_usd,
                    "target_size": target_size,
                }
            )
        return tier, specs

    def _v2_cancel_client(self):
        if not hasattr(self.trader, "client"):
            return None
        if hasattr(self.trader.client, "cancel"):
            return self.trader.client.cancel
        if hasattr(self.trader.client, "cancel_order"):
            return self.trader.client.cancel_order
        return None

    async def _v2_recycle_stale_orders(
        self,
        state: AssetState,
        desired_prices_by_side: dict[str, list[float]],
    ) -> tuple[dict[str, set[float]], int]:
        kept_prices_by_side: dict[str, set[float]] = {"UP": set(), "DOWN": set()}
        stale_after = self._v2_reprice_stale_after_seconds()
        tolerance = self._v2_reprice_price_tolerance()
        cancel_client = self._v2_cancel_client()
        cancelled = 0
        now = time.time()

        for order in list(self._v2_open_orders(state)):
            side = self._v2_normalized_order_side(state, order)
            if side not in ("UP", "DOWN"):
                continue

            desired_prices = desired_prices_by_side.get(side, [])
            actual_price = round(self._v2_order_actual_price(order), 2)
            created_at_raw = order.get("created_at", now)
            created_at = (
                created_at_raw if isinstance(created_at_raw, (int, float)) else now
            )
            order_age_seconds = max(now - float(created_at), 0.0)

            nearest_desired_price = actual_price
            price_gap = 0.0
            near_market = True
            if desired_prices:
                nearest_desired_price = min(
                    desired_prices, key=lambda price: abs(price - actual_price)
                )
                price_gap = round(abs(nearest_desired_price - actual_price), 2)
                near_market = price_gap <= tolerance + 1e-9
            else:
                near_market = False

            stale_age = order_age_seconds >= stale_after
            stale_price = not near_market
            unlikely_to_fill = stale_price
            is_stale = stale_age and (stale_price or not desired_prices)

            if not is_stale:
                kept_prices_by_side[side].add(actual_price)
                continue

            oid = order.get("order_id", "")
            try:
                if cancel_client and oid:
                    cancel_client(oid)
                release_notional = self._v2_order_reserved_remaining(order)
                if release_notional > 0 and not order.get("budget_released"):
                    self._release_v2_budget(
                        state, release_notional, "stale_cancel", side, oid
                    )
                self._set_v2_order_reserved_remaining(order, 0.0)
                order["budget_released"] = True
                order["closed"] = True
                cancelled += 1
                logger.info(
                    "stale_order_cancelled",
                    asset=state.asset,
                    side=side,
                    order_id=oid[:16],
                    actual_price=actual_price,
                    nearest_desired_price=nearest_desired_price,
                    price_gap=price_gap,
                    order_age_seconds=round(order_age_seconds, 2),
                    stale_after_seconds=round(stale_after, 2),
                    no_longer_near_market=stale_price,
                    unlikely_to_fill=unlikely_to_fill,
                    released_reserved_usd=round(release_notional, 2),
                    reserved_open_order_usd=self._v2_reserved_open_order_usd(state),
                    filled_position_cost_usd=self._v2_filled_position_cost_usd(state),
                )
            except Exception as e:
                logger.warning(
                    "stale_order_cancel_error",
                    asset=state.asset,
                    side=side,
                    order_id=oid[:16],
                    error=str(e)[:120],
                )

        state.early_dca_orders = [
            order for order in state.early_dca_orders if not order.get("closed")
        ]
        return kept_prices_by_side, cancelled

    def _v2_cancel_open_orders_for_side(
        self, state: AssetState, side: str, context: str
    ) -> int:
        cancel_client = self._v2_cancel_client()
        cancelled = 0
        for order in list(self._v2_open_orders(state)):
            if self._v2_normalized_order_side(state, order) != side:
                continue
            oid = order.get("order_id", "")
            try:
                if cancel_client and oid:
                    cancel_client(oid)
                release_notional = self._v2_order_reserved_remaining(order)
                if release_notional > 0 and not order.get("budget_released"):
                    self._release_v2_budget(state, release_notional, context, side, oid)
                self._set_v2_order_reserved_remaining(order, 0.0)
                order["budget_released"] = True
                order["closed"] = True
                cancelled += 1
                logger.info(
                    "v2_side_orders_cancelled",
                    asset=state.asset,
                    side=side,
                    context=context,
                    order_id=oid[:16],
                )
            except Exception as e:
                logger.warning(
                    "v2_side_order_cancel_error",
                    asset=state.asset,
                    side=side,
                    context=context,
                    order_id=oid[:16],
                    error=str(e)[:120],
                )
        state.early_dca_orders = [
            order for order in state.early_dca_orders if not order.get("closed")
        ]
        return cancelled

    def _v2_window_metrics(self, state: AssetState) -> dict[str, float | int | bool]:
        tracked_orders = state.early_dca_orders
        filled_orders = [
            o
            for o in tracked_orders
            if o.get("filled") or self._v2_float(o.get("filled_notional_usd", 0)) > 0
        ]
        cheap_buy_count = len(tracked_orders)
        cheap_buy_usd = round(
            sum(
                float(o.get("actual_notional_usd", o.get("size", 0)) or 0)
                for o in tracked_orders
            ),
            2,
        )
        under_25_count = sum(
            1 for o in tracked_orders if float(o.get("price", 1) or 1) < 0.25
        )
        percent_buys_under_0_25 = (
            round((under_25_count / cheap_buy_count) * 100, 1)
            if cheap_buy_count
            else 0.0
        )
        directions = [
            self._v2_order_direction(state, order) for order in tracked_orders
        ]
        filled_directions = [
            self._v2_order_direction(state, order) for order in filled_orders
        ]
        reserved_open_order_usd = self._v2_reserved_open_order_usd(state)
        filled_position_cost_usd = self._v2_filled_position_cost_usd(state)
        return {
            "cheap_buy_count": cheap_buy_count,
            "cheap_buy_usd": cheap_buy_usd,
            "percent_buys_under_0_25": percent_buys_under_0_25,
            "trades_per_window": len(filled_orders),
            "both_sides_posted": any(direction is True for direction in directions)
            and any(direction is False for direction in directions),
            "both_sides_filled": any(
                direction is True for direction in filled_directions
            )
            and any(direction is False for direction in filled_directions),
            "reserved_open_order_usd": reserved_open_order_usd,
            "filled_position_cost_usd": filled_position_cost_usd,
            "current_reserved_budget": reserved_open_order_usd,
            "current_filled_budget": filled_position_cost_usd,
            "current_total_budget": self._v2_current_total_notional(state),
            "remaining_budget": self._v2_remaining_budget(state),
            "max_bet_per_asset": self._v2_max_bet_per_asset(),
        }

    def _v2_side_committed_usd(self, state: AssetState, side_up: bool) -> float:
        filled = state.early_up_cost if side_up else state.early_down_cost
        reserved = 0.0
        for order in self._v2_open_orders(state):
            if self._v2_order_direction(state, order) is side_up:
                reserved += self._v2_order_reserved_remaining(order)
        return round(filled + reserved, 2)

    def _v2_side_hold_value(self, prob_up: float, side_up: bool) -> float:
        return prob_up if side_up else (1.0 - prob_up)

    def _reserve_v2_budget(
        self, state: AssetState, actual_notional_usd: float, context: str, side: str
    ) -> bool:
        max_bet_per_asset = self._v2_max_bet_per_asset()
        current_reserved = self._v2_reserved_open_order_usd(state)
        current_filled = self._v2_filled_position_cost_usd(state)
        current_total = round(current_reserved + current_filled, 2)
        remaining_budget = round(max(max_bet_per_asset - current_total, 0.0), 2)
        if current_total + actual_notional_usd > max_bet_per_asset + 1e-9:
            logger.info(
                "v2_budget_blocked",
                asset=state.asset,
                side=side,
                context=context,
                actual_notional_usd=round(actual_notional_usd, 2),
                max_bet_per_asset=max_bet_per_asset,
                reserved_open_order_usd=current_reserved,
                filled_position_cost_usd=current_filled,
                current_total_notional_usd=current_total,
                remaining_budget=remaining_budget,
            )
            return False
        self._set_v2_reserved_open_order_usd(
            state, current_reserved + actual_notional_usd
        )
        logger.info(
            "v2_budget_reserved",
            asset=state.asset,
            side=side,
            context=context,
            actual_notional_usd=round(actual_notional_usd, 2),
            max_bet_per_asset=max_bet_per_asset,
            reserved_open_order_usd=self._v2_reserved_open_order_usd(state),
            filled_position_cost_usd=current_filled,
            current_total_notional_usd=self._v2_current_total_notional(state),
            remaining_budget=self._v2_remaining_budget(state),
        )
        return True

    def _move_v2_reserved_to_filled(
        self,
        state: AssetState,
        actual_notional_usd: float,
        context: str,
        side: str,
        order_id: str = "",
    ) -> None:
        max_bet_per_asset = self._v2_max_bet_per_asset()
        self._set_v2_reserved_open_order_usd(
            state, self._v2_reserved_open_order_usd(state) - actual_notional_usd
        )
        self._set_v2_filled_position_cost_usd(
            state, self._v2_filled_position_cost_usd(state) + actual_notional_usd
        )
        logger.info(
            "v2_budget_filled",
            asset=state.asset,
            side=side,
            context=context,
            actual_notional_usd=round(actual_notional_usd, 2),
            order_id=order_id[:16] if order_id else "",
            max_bet_per_asset=max_bet_per_asset,
            reserved_open_order_usd=self._v2_reserved_open_order_usd(state),
            filled_position_cost_usd=self._v2_filled_position_cost_usd(state),
            current_total_notional_usd=self._v2_current_total_notional(state),
            remaining_budget=self._v2_remaining_budget(state),
        )

    def _release_v2_budget(
        self,
        state: AssetState,
        actual_notional_usd: float,
        context: str,
        side: str,
        order_id: str = "",
    ) -> None:
        max_bet_per_asset = self._v2_max_bet_per_asset()
        self._set_v2_reserved_open_order_usd(
            state, self._v2_reserved_open_order_usd(state) - actual_notional_usd
        )
        logger.info(
            "v2_budget_released",
            asset=state.asset,
            side=side,
            context=context,
            actual_notional_usd=round(actual_notional_usd, 2),
            order_id=order_id[:16] if order_id else "",
            max_bet_per_asset=max_bet_per_asset,
            reserved_open_order_usd=self._v2_reserved_open_order_usd(state),
            filled_position_cost_usd=self._v2_filled_position_cost_usd(state),
            current_total_notional_usd=self._v2_current_total_notional(state),
            remaining_budget=self._v2_remaining_budget(state),
        )
        logger.info(
            "budget_released",
            asset=state.asset,
            side=side,
            context=context,
            actual_notional_usd=round(actual_notional_usd, 2),
            order_id=order_id[:16] if order_id else "",
            reserved_open_order_usd=self._v2_reserved_open_order_usd(state),
            filled_position_cost_usd=self._v2_filled_position_cost_usd(state),
        )

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
            self.coinbase.get_price(s.asset) == 0 for s in self.asset_states.values()
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
                    logger.error(
                        "tick_asset_error", key=key, error=str(e), exc_info=True
                    )

            # Warn if all prices have been zero for > 60 seconds
            if any_price:
                _last_price_log = time.time()
            elif time.time() - _last_price_log > 60:
                logger.warning(
                    "price_feed_stale_all_zero",
                    seconds=round(time.time() - _last_price_log),
                )
                _last_price_log = time.time()  # reset so we don't spam

            await asyncio.sleep(0.25)

            # Heartbeat every 60 seconds — log + touch file for Docker HEALTHCHECK
            if time.time() - self._last_heartbeat >= 60:
                self._last_heartbeat = time.time()
                uptime = round((time.time() - self._start_time) / 60, 1)
                logger.info(
                    "heartbeat", uptime_min=uptime, tasks=len(asyncio.all_tasks())
                )
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

        early_entry_active = self._early_entry_active_for_state(state)
        _sso = time.time() - window.open_ts
        logger.info(
            "tick",
            asset=state.asset,
            seconds=int(_sso),
            early_enabled=early_entry_active,
            early_master_enabled=self.settings.early_entry_enabled,
            has_position=bool(state.early_position),
            early_traded=state.early_entry_traded,
            early_evaluated=state.early_entry_evaluated,
        )

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
        if early_entry_active and int(seconds_since_open) % 5 == 0:
            self._write_live_state_async(state, price, seconds_since_open)

        # Log progress every 60s for debugging
        if int(seconds_since_open) % 60 == 0 and int(seconds_since_open) > 0:
            logger.debug(
                "tick_progress",
                asset=state.asset,
                seconds=int(seconds_since_open),
                target=self.settings.late_entry_seconds,
                evaluated=state.late_entry_evaluated,
            )

        # ── V2 EARLY ENTRY PLAYBOOK ──────────────────────────────────────────
        # Two gates:
        # 1. Opening new positions requires early_entry_active (can be disabled via secrets)
        # 2. Managing existing positions only requires state.early_position (survives disable)
        has_active_position = bool(state.early_position)

        if early_entry_active or has_active_position:
            win_secs = state.tracker.window_seconds
            # Cutoff: cancel unfilled + stop trading. 270s for 5m, 3540s for 1h.
            cutoff = win_secs - 30

            # SAFETY: Skip this window if we joined late (prevents stacking on restart)
            if (
                not state.early_entry_traded
                and not state.early_position
                and seconds_since_open > 15
            ):
                if int(seconds_since_open) % 60 == 0:
                    logger.info(
                        "v2_skipped_late_join",
                        asset=state.asset,
                        seconds=int(seconds_since_open),
                    )
                # Don't open position — wait for next window
                pass  # fall through to end of early_entry_active block
            # PHASE 1: Open both sides at T+5–15s (requires early_entry_active — new positions only)
            elif (
                early_entry_active
                and not state.early_entry_traded
                and not state.early_position
                and 5 <= seconds_since_open <= 15
            ):
                await self._v2_open_position(state, price)

            # PHASE 2: Confirm direction at T+15-20s (once per window)
            if (
                state.early_position
                and 15 <= seconds_since_open <= 20
                and not state.early_confirm_done
            ):
                state.early_confirm_done = True
                await self._v2_confirm(state, price)

            # PHASE 3: Unified execution tick every 1s from T+5 to T+cutoff
            if state.early_position and 5 <= seconds_since_open <= cutoff:
                tick_1s = int(seconds_since_open)
                if tick_1s not in state.early_accum_ticks:
                    state.early_accum_ticks.add(tick_1s)
                    await self._v2_poll_fills(state)
                    await self._v2_execution_tick(state, price, seconds_since_open)
                    await self._v2_poll_fills(state)

            # PHASE 4: Disabled — unified _v2_execution_tick handles loser sells.
            # _early_checkpoint was selling winner-side lots on intra-window dips,
            # conflicting with the directional execution engine.
            # if state.early_position and 30 <= seconds_since_open <= 240:
            #     cp15 = int(seconds_since_open // 15) * 15
            #     if 30 <= cp15 <= 240 and cp15 not in state.early_checkpoints_done:
            #         state.early_checkpoints_done.add(cp15)
            #         await self._early_checkpoint(state, price, seconds_since_open, cp15)

            # Status log every 15s
            if state.early_position:
                st15 = int(seconds_since_open // 15)
                if st15 not in state.early_status_logged:
                    state.early_status_logged.add(st15)
                    self._log_v2_status(state, seconds_since_open)

            # Cancel unfilled at cutoff, then hold everything to resolution
            if (
                state.early_position
                and seconds_since_open >= cutoff
                and cutoff not in state.early_checkpoints_done
            ):
                state.early_checkpoints_done.add(cutoff)
                await self._early_cancel_unfilled(state)

        if (
            not self.settings.early_entry_enabled
            and not getattr(self, "_v2_graceful_stop_requested", False)
            and not state.late_entry_evaluated
            and not state.traded_this_window
            and not state.early_entry_traded
            and not state.early_position
        ):
            await self._scan_tick(state, price, seconds_since_open)

        state.prev_open_ts = current_open_ts

    # _try_tier_a_entry removed (oracle dislocation never triggered in practice)
    # _on_entry_zone removed (replaced by _evaluate_scored_entry)

    # 128 lines removed (dead method)

    # 223 lines removed (dead method)

    async def _scan_tick(
        self, state: AssetState, price: float, seconds_since_open: float
    ):
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
        if (
            seconds_since_open >= SCAN_START
            and not state.scan_active
            and not state.scan_direction_flipped
        ):
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

            logger.info(
                "scan_started",
                asset=state.asset,
                direction=direction,
                ask=round(current_ask, 3),
                seconds=round(seconds_since_open, 1),
            )

            # Early entry: cheap enough, don't wait
            if 0.55 <= current_ask <= EARLY_ENTRY_ASK:
                logger.info(
                    "scan_early_entry", asset=state.asset, ask=round(current_ask, 3)
                )
                await self._execute_scan_entry(state, price)
                return
            return

        # Phase 2: Scan in progress — check every 3s
        if state.scan_active and seconds_since_open < SCAN_END:
            now = time.time()
            if (
                state.scan_last_checked
                and (now - state.scan_last_checked) < SCAN_INTERVAL
            ):
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
                logger.info(
                    "scan_direction_flipped",
                    asset=state.asset,
                    original=state.scan_direction,
                    new=direction,
                    seconds=round(seconds_since_open, 1),
                )
                # Log the skip to DynamoDB
                self._log_scan_signal(state, price, skip_reason="direction_unstable")
                state.late_entry_evaluated = True
                return

            # Track best (lowest) ask
            if current_ask < state.scan_best_ask:
                state.scan_best_ask = current_ask
                state.scan_best_ask_ts = now
                logger.debug(
                    "scan_better_ask",
                    asset=state.asset,
                    ask=round(current_ask, 3),
                    seconds=round(seconds_since_open, 1),
                )

            # Early entry: cheap enough
            if 0.55 <= current_ask <= EARLY_ENTRY_ASK:
                state.scan_best_ask = current_ask
                state.scan_best_ask_ts = now
                logger.info(
                    "scan_early_entry", asset=state.asset, ask=round(current_ask, 3)
                )
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

    def _log_scan_signal(
        self,
        state: AssetState,
        price: float,
        skip_reason: str = "",
        extra: dict | None = None,
    ):
        """Log a scan evaluation to DynamoDB signals table with full backtest data."""
        from datetime import datetime, timezone

        window = state.tracker.current
        if not window:
            return
        scan_duration = time.time() - (state.scan_best_ask_ts or time.time())
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
        vol_avg = (
            sum(state.vol_history) / len(state.vol_history)
            if state.vol_history
            else vol_now
        )
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
            vol_ma = (
                sum(state.vol_history) / len(state.vol_history)
                if state.vol_history
                else vol
            )
            vol_ratio = vol / vol_ma if vol_ma > 0 else 1.0
            hl_range = state.window_high - state.window_low
            body = abs(price - (window.open_price or price))
            body_ratio = body / hl_range if hl_range > 0 else 0.5
            features = {
                "move_pct_15s": pct_move,
                "realized_vol_5m": vol,
                "vol_ratio": vol_ratio,
                "body_ratio": body_ratio,
                "prev_window_direction": (
                    1
                    if state.prev_window
                    and state.prev_window.close_price
                    and state.prev_window.open_price
                    and state.prev_window.close_price >= state.prev_window.open_price
                    else -1
                )
                if state.prev_window
                else 0,
                "prev_window_move_pct": (
                    (state.prev_window.close_price - state.prev_window.open_price)
                    / state.prev_window.open_price
                    * 100
                )
                if state.prev_window
                and state.prev_window.open_price
                and state.prev_window.close_price
                else 0,
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
                skip_reason = (
                    "fully_priced"  # Above per-asset max, lgbm not high enough
                )
            elif current_ask >= 0.75 and is_peak:
                size = 10.00  # Normal high conviction, peak hours
            else:
                size = 5.00  # Default

        # BTC cross-asset move (for data collection)
        btc_move_pct = 0.0
        if state.asset != "BTC":
            btc_state = self.asset_states.get("BTC_5m")
            if (
                btc_state
                and btc_state.tracker.current
                and btc_state.tracker.current.open_price
            ):
                btc_price = self.coinbase.get_price("BTC")
                btc_open = btc_state.tracker.current.open_price
                if btc_open > 0:
                    btc_move_pct = (btc_price - btc_open) / btc_open * 100

        # Log evaluation
        logger.info(
            "late_entry_eval",
            asset=state.asset,
            slug=window.slug,
            direction="UP" if direction_up else "DOWN",
            current_ask=round(current_ask, 3),
            max_ask=max_ask,
            min_ask=min_ask,
            size=size,
            pct_move=round(pct_move, 4),
            seconds_remaining=round(remaining, 1),
            scan_duration_s=round(scan_duration, 1),
            direction_flipped=state.scan_direction_flipped,
            utc_hour=utc_hour,
            weak_hours=weak_hours,
            lgbm_prob=round(lgbm_prob, 4),
            btc_move_pct=round(btc_move_pct, 4),
            skip_reason=skip_reason or "TRADE",
        )

        # Log to DynamoDB with all backtest fields
        self._log_scan_signal(
            state,
            price,
            skip_reason=skip_reason,
            extra={
                "utc_hour": utc_hour,
                "weak_hours": weak_hours,
                "open_price": round(window.open_price or 0, 4),
                "current_price": round(price, 4),
                "window_high": round(state.window_high, 4),
                "window_low": round(state.window_low, 4),
                "realized_vol": round(
                    compute_realized_vol(list(state.price_history)), 6
                ),
                "lgbm_prob": round(lgbm_prob, 4),
                "p_bayesian": round(state.bayesian.probability, 4),
                "btc_move_pct": round(btc_move_pct, 4),
                "size": size,
                "tier": "high"
                if current_ask >= 0.75
                else "mid"
                if current_ask >= 0.65
                else "low",
            },
        )

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
            asset=state.asset,
            slug=window.slug,
            direction="UP" if direction_up else "DOWN",
            ask=round(current_ask, 3),
            size=round(size, 2),
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
            if (
                btc_state
                and btc_state.tracker.current
                and btc_state.tracker.current.open_price
            ):
                btc_price = self.coinbase.get_price("BTC")
                btc_open = btc_state.tracker.current.open_price
                if btc_open > 0:
                    btc_move = (btc_price - btc_open) / btc_open * 100

        btc_confirms = (pct_move >= 0 and btc_move > 0.01) or (
            pct_move < 0 and btc_move < -0.01
        )

        await self._refresh_orderbook(state)
        current_ask = (
            state.orderbook.yes_best_ask
            if pct_move >= 0
            else state.orderbook.no_best_ask
        )

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
            avg_prior_volume=(
                sum(state.prior_window_tick_counts)
                / len(state.prior_window_tick_counts)
                if state.prior_window_tick_counts
                else 0
            ),
        )

        # LightGBM prediction
        pair = f"{state.asset}_5m"
        vol = compute_realized_vol(list(state.price_history))
        vol_ma = (
            sum(state.vol_history) / len(state.vol_history)
            if state.vol_history
            else vol
        )
        vol_ratio = vol / vol_ma if vol_ma > 0 else 1.0
        hl_range = state.window_high - state.window_low
        body = abs(price - (window.open_price or price))
        body_ratio = body / hl_range if hl_range > 0 else 0.5
        seconds_since_open = (window.close_ts - window.open_ts) - remaining
        import datetime as _dt
        import math as _math

        now_utc = _dt.datetime.now(_dt.timezone.utc)
        features = {
            "move_pct_15s": pct_move,
            "realized_vol_5m": vol,
            "vol_ratio": vol_ratio,
            "body_ratio": body_ratio,
            "prev_window_direction": (
                1
                if state.prev_window
                and state.prev_window.close_price
                and state.prev_window.open_price
                and state.prev_window.close_price >= state.prev_window.open_price
                else -1
            )
            if state.prev_window
            else 0,
            "prev_window_move_pct": (
                (state.prev_window.close_price - state.prev_window.open_price)
                / state.prev_window.open_price
                * 100
            )
            if state.prev_window
            and state.prev_window.open_price
            and state.prev_window.close_price
            else 0,
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
            asset=state.asset,
            slug=window.slug,
            score=score.total,
            ofi=score.ofi,
            no_rev=score.no_reversal,
            cross=score.cross_asset,
            pm=score.pm_pressure,
            vol=score.volume,
            lgbm_prob=round(lgbm_prob, 4),
            ask=round(current_ask, 3),
            pct_move=round(pct_move, 4),
            ev=round(ev, 4),
        )

        # Log to DynamoDB signals table
        try:
            self.dynamo.put_signal(
                {
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
                    "open_price": round(window.open_price, 2)
                    if window.open_price
                    else 0,
                }
            )
        except Exception:
            pass

        # HARD CEILING — applies to ALL entry paths (taker, maker, override)
        # This is the first check. Nothing trades above $0.55.
        if current_ask > self.settings.max_market_price:
            skip_reason = (
                f"ask_{current_ask:.2f}_above_{self.settings.max_market_price}"
            )
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
                entry_type = (
                    "override"
                    if lgbm_prob >= 0.68 and current_ask <= 0.55 and ev >= 0.10
                    else "taker"
                )
                logger.info(
                    "small_move_confirmed",
                    asset=state.asset,
                    slug=window.slug,
                    move=round(pct_move, 4),
                    lgbm=round(lgbm_prob, 4),
                    btc=btc_confirms,
                )
        # Decision based on score — with hard filter override
        elif (
            lgbm_prob >= 0.65 and current_ask <= 0.55 and current_ask > 0 and ev >= 0.10
        ):
            entry_type = "override"
            logger.info(
                "score_override",
                asset=state.asset,
                slug=window.slug,
                score=score.total,
                lgbm=round(lgbm_prob, 4),
                ask=round(current_ask, 3),
                ev=round(ev, 4),
            )
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
            logger.info(
                "score_skip",
                asset=state.asset,
                slug=window.slug,
                score=score.total,
                reason=skip_reason,
                lgbm=round(lgbm_prob, 4),
                ask=round(current_ask, 3),
                ev=round(ev, 4),
            )
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
            asset=state.asset,
            slug=window.slug,
            entry_type=entry_type,
            score=score.total,
            ask=round(market_price, 3),
            size=round(size, 2),
            lgbm=round(lgbm_prob, 4),
            ev=round(ev, 4),
        )

        t_start = time.time()
        if not self.settings.scenario_c_enabled:
            logger.info(
                "scenario_c_paused",
                asset=state.asset,
                slug=window.slug,
                entry_type=entry_type,
                ask=round(market_price, 3),
                lgbm=round(lgbm_prob, 4),
            )
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
        if not self._v2_order_execution_enabled():
            return

        window = state.tracker.current
        if not window or not window.yes_token_id or not window.no_token_id:
            logger.info(
                "v2_open_no_tokens",
                asset=state.asset,
                slug=window.slug if window else "none",
                yes_token=window.yes_token_id[:12]
                if window and window.yes_token_id
                else "",
                no_token=window.no_token_id[:12]
                if window and window.no_token_id
                else "",
            )
            return

        early_slug = f"early_{window.slug}"

        # Dedup
        if not hasattr(self, "_early_traded_slugs"):
            self._early_traded_slugs = set()
        if early_slug in self._early_traded_slugs:
            logger.info("v2_open_dedup", asset=state.asset, slug=early_slug)
            return
        self._early_traded_slugs.add(early_slug)

        # Determine direction via LGBM (neutral fallback if no model)
        import math as _math
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        _now = _dt.now(_tz.utc)
        vol = compute_realized_vol(list(state.price_history))
        features = {
            "move_pct_15s": 0.0,
            "realized_vol_5m": vol,
            "vol_ratio": 1.0,
            "body_ratio": 0.5,
            "prev_window_direction": (
                1
                if state.prev_window
                and state.prev_window.close_price
                and state.prev_window.open_price
                and state.prev_window.close_price >= state.prev_window.open_price
                else -1
            )
            if state.prev_window
            else 0,
            "prev_window_move_pct": (
                (state.prev_window.close_price - state.prev_window.open_price)
                / state.prev_window.open_price
                * 100
            )
            if state.prev_window
            and state.prev_window.open_price
            and state.prev_window.close_price
            else 0,
            "hour_sin": _math.sin(2 * _math.pi * _now.hour / 24),
            "hour_cos": _math.cos(2 * _math.pi * _now.hour / 24),
            "dow_sin": _math.sin(2 * _math.pi * _now.weekday() / 7),
            "dow_cos": _math.cos(2 * _math.pi * _now.weekday() / 7),
            "signal_move_pct": 0.0,
            "signal_ask_price": 0.50,
            "signal_seconds": 0,
            "signal_ev": 0,
        }
        try:
            lgbm_raw = self.model_server.predict(f"{state.asset}_5m", features)
        except Exception as model_err:
            logger.info(
                "v2_open_model_fallback", asset=state.asset, error=str(model_err)[:60]
            )
            lgbm_raw = 0.50
        direction_up = lgbm_raw >= 0.50

        yes_bid = state.orderbook.yes_best_bid
        no_bid = state.orderbook.no_best_bid
        HARD_CAP_PRICE = 0.55

        # Model-weighted allocation for open phase
        up_pct, down_pct = self._v2_allocation_split(lgbm_raw)
        budget_scale = self._v2_confidence_budget_scale(lgbm_raw)

        # Open uses a small starter allocation. Most capital deploys mid-window.
        max_bet = self._v2_max_bet_per_asset()
        open_budget = round(max_bet * self._v2_open_budget_pct() * budget_scale, 2)
        up_size = round(open_budget * up_pct, 2)
        down_size = round(open_budget * down_pct, 2)

        logger.info(
            "v2_open_orderbook",
            asset=state.asset,
            direction="UP" if direction_up else "DOWN",
            lgbm=round(lgbm_raw, 3),
            up_pct=up_pct,
            down_pct=down_pct,
            budget_scale=round(budget_scale, 2),
            yes_bid=round(yes_bid, 3),
            no_bid=round(no_bid, 3),
            yes_ask=round(state.orderbook.yes_best_ask, 3),
            no_ask=round(state.orderbook.no_best_ask, 3),
        )

        # Post GTC on both sides immediately.
        try:
            from py_clob_client.clob_types import (
                CreateOrderOptions,
                OrderArgs,
                OrderType,
            )
            from py_clob_client.order_builder.constants import BUY

            options = CreateOrderOptions(tick_size="0.01", neg_risk=False)

            for token, bid, sz, label, side_up in [
                (window.yes_token_id, yes_bid, up_size, "UP", True),
                (window.no_token_id, no_bid, down_size, "DOWN", False),
            ]:
                if not token or not bid or bid <= 0:
                    continue
                post_price = round(
                    bid + 0.01, 2
                )  # post at bid+1¢ (aggressive, want fills at open)
                if post_price <= 0 or post_price > HARD_CAP_PRICE:
                    continue
                shares, actual_notional_usd = self._v2_order_size(sz, post_price)
                if shares <= 0 or actual_notional_usd <= 0:
                    continue
                # No combined filter at open — both sides need fills ASAP
                # Hard cap ($0.90) is already checked above
                if not self._reserve_v2_budget(
                    state, actual_notional_usd, "open", label
                ):
                    break
                try:
                    logger.info(
                        "v2_open_placing",
                        asset=state.asset,
                        side=label,
                        token=token[:16],
                        price=post_price,
                        shares=shares,
                        sz=actual_notional_usd,
                        target_size=sz,
                    )
                    args = OrderArgs(
                        price=post_price, size=shares, side=BUY, token_id=token
                    )
                    signed = self.trader.client.create_order(args, options)
                    resp = self.trader.client.post_order(signed, OrderType.GTC)
                    logger.info(
                        "v2_open_response",
                        asset=state.asset,
                        side=label,
                        resp=str(resp)[:120],
                    )
                    oid = resp.get("orderID", "")
                    if oid:
                        state.early_dca_orders.append(
                            self._build_v2_tracked_order(
                                order_id=oid,
                                actual_shares=shares,
                                actual_price=post_price,
                                actual_notional_usd=actual_notional_usd,
                                target_size=sz,
                                side=label,
                            )
                        )
                        logger.info(
                            "v2_open_posted",
                            asset=state.asset,
                            slug=early_slug,
                            side=label,
                            actual_price=post_price,
                            actual_notional_usd=actual_notional_usd,
                            target_size=sz,
                            actual_shares=shares,
                            order_id=oid[:16],
                            direction="UP" if direction_up else "DOWN",
                        )
                    else:
                        self._release_v2_budget(
                            state, actual_notional_usd, "open_no_order_id", label
                        )
                        logger.warning(
                            "v2_open_no_order_id",
                            asset=state.asset,
                            side=label,
                            resp=str(resp)[:200],
                        )
                except Exception as e:
                    self._release_v2_budget(
                        state, actual_notional_usd, "open_post_error", label
                    )
                    logger.warning(
                        "v2_open_error",
                        asset=state.asset,
                        side=label,
                        error=str(e)[:200],
                    )
        except Exception as e:
            logger.warning(
                "v2_open_import_error", asset=state.asset, error=str(e)[:200]
            )

        # Always set position so accumulation loop fires even if orders failed
        state.early_entry_traded = True
        state.traded_this_window = True
        main_token = window.yes_token_id if direction_up else window.no_token_id
        hedge_token = window.no_token_id if direction_up else window.yes_token_id
        main_bid = yes_bid if direction_up else no_bid
        hedge_bid = no_bid if direction_up else yes_bid
        state.early_position = {
            "slug": early_slug,
            "token_id": main_token,
            "hedge_token": hedge_token,
            "shares": 0,
            "entry_price": round(main_bid or 0.50, 2),
            "hedge_entry_price": round(hedge_bid or 0.50, 2),
            "direction_up": direction_up,
            "side": "YES" if direction_up else "NO",
            "size": 0.0,
        }
        logger.info(
            "v2_open_complete",
            asset=state.asset,
            slug=early_slug,
            direction="UP" if direction_up else "DOWN",
            lgbm=round(lgbm_raw, 3),
            budget_scale=round(budget_scale, 2),
            up_size=up_size,
            down_size=down_size,
            max_bet=max_bet,
        )
        self._log_activity(
            state,
            f"OPEN {'UP' if direction_up else 'DOWN'}",
            f"up=${up_size} down=${down_size} lgbm={lgbm_raw:.2f}",
        )

    async def _v2_confirm(self, state: AssetState, price: float):
        """Observe the T+15 model update without mutating live inventory.

        The execution tick owns allocation and order placement. Confirm is now
        telemetry-only so it can't reintroduce one-sided risk or extra burst buys.
        """
        pos = state.early_position
        if not pos:
            return
        window = state.tracker.current
        if not window or not window.open_price:
            return

        import math as _math
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        _now = _dt.now(_tz.utc)
        pct_move = (
            ((price - window.open_price) / window.open_price * 100)
            if window.open_price > 0
            else 0
        )
        vol = compute_realized_vol(list(state.price_history))
        vol_ma = (
            sum(state.vol_history) / len(state.vol_history)
            if state.vol_history
            else vol
        )
        features = {
            "move_pct_15s": pct_move,
            "realized_vol_5m": vol,
            "vol_ratio": vol / vol_ma if vol_ma > 0 else 1.0,
            "body_ratio": abs(pct_move) / 0.1 if pct_move != 0 else 0.5,
            "prev_window_direction": (
                1
                if state.prev_window
                and state.prev_window.close_price
                and state.prev_window.open_price
                and state.prev_window.close_price >= state.prev_window.open_price
                else -1
            )
            if state.prev_window
            else 0,
            "prev_window_move_pct": (
                (state.prev_window.close_price - state.prev_window.open_price)
                / state.prev_window.open_price
                * 100
            )
            if state.prev_window
            and state.prev_window.open_price
            and state.prev_window.close_price
            else 0,
            "hour_sin": _math.sin(2 * _math.pi * _now.hour / 24),
            "hour_cos": _math.cos(2 * _math.pi * _now.hour / 24),
            "dow_sin": _math.sin(2 * _math.pi * _now.weekday() / 7),
            "dow_cos": _math.cos(2 * _math.pi * _now.weekday() / 7),
            "signal_move_pct": abs(pct_move),
            "signal_ask_price": pos["entry_price"],
            "signal_seconds": 15,
            "signal_ev": 0,
        }
        try:
            lgbm_raw = self.model_server.predict(f"{state.asset}_5m", features)
        except Exception as model_err:
            logger.info(
                "v2_confirm_model_fallback",
                asset=state.asset,
                error=str(model_err)[:60],
            )
            lgbm_raw = 0.50

        confirm_direction_up = lgbm_raw >= 0.50
        direction_up = pos.get("direction_up", True)
        logger.info(
            "v2_confirm_observed",
            asset=state.asset,
            slug=pos["slug"],
            current_direction="UP" if direction_up else "DOWN",
            confirm_direction="UP" if confirm_direction_up else "DOWN",
            confirmed=confirm_direction_up == direction_up,
            lgbm=round(lgbm_raw, 3),
            pct_move=round(pct_move, 3),
        )
        self._log_activity(
            state,
            "CONFIRM OBSERVE",
            f"{'UP' if confirm_direction_up else 'DOWN'} lgbm={lgbm_raw:.2f} move={pct_move:+.3f}%",
        )

    # ── V2 ACCUMULATE CHEAP + POLL FILLS ─────────────────────────────────
    async def _v2_accumulate_cheap(
        self, state: AssetState, price: float, seconds_since_open: float = 0.0
    ):
        """Every 3s: post GTC ladders on BOTH sides below current bid.

        Dense both-sides ladder:
          bid <= 0.15 (lottery zone):    9 levels [0-8¢],             $0.35 each
          bid 0.15-0.35 (cheap zone):    7 levels [0-6¢],             $0.25 each
          bid 0.35-0.60 (mid zone):      5 levels [0,1,2,3,5¢],       $0.20 each
          bid > 0.60 (winning side):     3 levels [0,1,3¢],           $0.15 each

        Budget cap is strict per window per asset and includes filled positions
        plus reserved open orders.
        """
        pos = state.early_position
        if not pos or not self._v2_order_execution_enabled():
            return
        window = state.tracker.current
        if not window:
            return

        max_bet_per_asset = self._v2_max_bet_per_asset()

        # Hard cap: accumulation may only deploy 40% of budget.
        # Remaining 60% is reserved for directional rebalance activity.
        MAX_INITIAL_DEPLOY = 0.40
        if (
            self._v2_filled_position_cost_usd(state)
            >= MAX_INITIAL_DEPLOY * max_bet_per_asset
        ):
            return

        await self._refresh_orderbook(state)

        from py_clob_client.clob_types import CreateOrderOptions

        options = CreateOrderOptions(tick_size="0.01", neg_risk=False)

        order_tasks = []
        tick_sides_posted: set[str] = set()
        tick_order_count = 0
        tick_order_usd = 0.0
        tick_under_25 = 0
        num_open_orders_before = len(self._v2_open_orders(state))
        desired_specs_by_side: dict[str, list[dict]] = {"UP": [], "DOWN": []}
        for side_up, token_id in [
            (True, window.yes_token_id),
            (False, window.no_token_id),
        ]:
            if not token_id:
                continue
            bid = (
                state.orderbook.yes_best_bid if side_up else state.orderbook.no_best_bid
            )
            if not bid or bid <= 0:
                continue
            side = "UP" if side_up else "DOWN"
            tier, desired_specs = self._v2_accum_specs(bid)
            desired_specs_by_side[side] = desired_specs

            logger.info(
                "v2_accum_side",
                asset=state.asset,
                side=side,
                bid=round(bid, 3),
                reserved_open_order_usd=self._v2_reserved_open_order_usd(state),
                filled_position_cost_usd=self._v2_filled_position_cost_usd(state),
                current_total_notional_usd=self._v2_current_total_notional(state),
                remaining_budget=self._v2_remaining_budget(state),
                max_bet_per_asset=round(max_bet_per_asset, 2),
            )

            logger.info(
                "v2_accum_tier",
                asset=state.asset,
                side=side,
                bid=round(bid, 3),
                tier=tier,
                offsets=str(
                    [round(bid - spec["post_price"], 2) for spec in desired_specs]
                ),
                num_orders=len(desired_specs),
            )

        desired_prices_by_side = {
            side: [spec["post_price"] for spec in specs]
            for side, specs in desired_specs_by_side.items()
        }
        (
            kept_prices_by_side,
            stale_orders_cancelled,
        ) = await self._v2_recycle_stale_orders(
            state,
            desired_prices_by_side,
        )

        for side_up, token_id in [
            (True, window.yes_token_id),
            (False, window.no_token_id),
        ]:
            if not token_id:
                continue
            side = "UP" if side_up else "DOWN"
            for spec in desired_specs_by_side[side]:
                if spec["post_price"] in kept_prices_by_side[side]:
                    continue
                actual_notional_usd = spec["actual_notional_usd"]
                if not self._reserve_v2_budget(
                    state,
                    actual_notional_usd,
                    "accumulate",
                    "UP" if side_up else "DOWN",
                ):
                    continue  # skip this spec, try cheaper ones
                tick_sides_posted.add(side)
                tick_order_count += 1
                tick_order_usd += actual_notional_usd
                if spec["post_price"] < 0.25:
                    tick_under_25 += 1
                order_tasks.append(
                    self._post_cheap_order(
                        state,
                        token_id,
                        spec["post_price"],
                        spec["shares"],
                        actual_notional_usd,
                        side_up,
                        options,
                        target_size=spec["target_size"],
                    )
                )
                kept_prices_by_side[side].add(spec["post_price"])

        if order_tasks:
            await asyncio.gather(*order_tasks, return_exceptions=True)
        window_metrics = self._v2_window_metrics(state)
        num_open_orders_after = len(self._v2_open_orders(state))
        open_sides_after = {
            self._v2_normalized_order_side(state, order)
            for order in self._v2_open_orders(state)
        }
        logger.info(
            "v2_reprice_cycle",
            asset=state.asset,
            seconds=int(seconds_since_open),
            stale_orders_cancelled=stale_orders_cancelled,
            repriced_orders_posted=len(order_tasks),
            num_open_orders_before=num_open_orders_before,
            num_open_orders_after=num_open_orders_after,
        )
        logger.info(
            "v2_accumulate_tick",
            asset=state.asset,
            orders=len(order_tasks),
            seconds=int(seconds_since_open),
            cheap_buy_count=tick_order_count,
            cheap_buy_usd=round(tick_order_usd, 2),
            percent_buys_under_0_25=round((tick_under_25 / tick_order_count) * 100, 1)
            if tick_order_count
            else 0.0,
            trades_per_window=window_metrics["trades_per_window"],
            both_sides_posted=("UP" in open_sides_after and "DOWN" in open_sides_after),
            both_sides_filled=window_metrics["both_sides_filled"],
            reserved_open_order_usd=window_metrics["reserved_open_order_usd"],
            filled_position_cost_usd=window_metrics["filled_position_cost_usd"],
            current_reserved_budget=window_metrics["current_reserved_budget"],
            remaining_budget=window_metrics["remaining_budget"],
            max_bet_per_asset=window_metrics["max_bet_per_asset"],
        )

    async def _post_cheap_order(
        self,
        state: AssetState,
        token_id: str,
        post_price: float,
        shares: int,
        size: float,
        side_up: bool,
        options,
        target_size: float | None = None,
    ) -> None:
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
                state.early_dca_orders.append(
                    self._build_v2_tracked_order(
                        order_id=oid,
                        actual_shares=shares,
                        actual_price=post_price,
                        actual_notional_usd=size,
                        target_size=target_size,
                        side=side,
                    )
                )
                logger.info(
                    "v2_cheap_posted",
                    asset=state.asset,
                    side=side,
                    actual_price=post_price,
                    actual_notional_usd=size,
                    target_size=target_size,
                    actual_shares=shares,
                    order_id=oid[:16],
                    reserved_open_order_usd=self._v2_reserved_open_order_usd(state),
                    filled_position_cost_usd=self._v2_filled_position_cost_usd(state),
                    remaining_budget=self._v2_remaining_budget(state),
                )
                logger.info(
                    "repriced_order_posted",
                    asset=state.asset,
                    side=side,
                    actual_price=post_price,
                    actual_notional_usd=size,
                    target_size=target_size,
                    actual_shares=shares,
                    order_id=oid[:16],
                    reserved_open_order_usd=self._v2_reserved_open_order_usd(state),
                    filled_position_cost_usd=self._v2_filled_position_cost_usd(state),
                )
            else:
                self._release_v2_budget(state, size, "cheap_no_order_id", side)
                logger.warning(
                    "v2_cheap_no_order_id",
                    asset=state.asset,
                    side=side,
                    price=post_price,
                    resp=str(resp)[:200],
                )
        except Exception as e:
            self._release_v2_budget(state, size, "cheap_post_error", side)
            logger.warning(
                "v2_cheap_post_error",
                asset=state.asset,
                side=side,
                price=post_price,
                error=str(e),
            )

    # ── V2 UNIFIED EXECUTION TICK ────────────────────────────────────────────

    async def _v2_execution_tick(
        self, state: AssetState, price: float, seconds_since_open: float
    ):
        """5m execution split into open, main deploy, buy-only late phase, and hold.

        Phases:
        1. T+5-60: small open + light follow-up
        2. T+60-180: main deployment + limited sell-and-recover
        3. T+180-250: buy-only, frozen split, no sells
        4. T+250+: commit/hold
        """
        pos = state.early_position
        if not pos or not self._v2_order_execution_enabled():
            return
        window = state.tracker.current
        if not window or not window.yes_token_id or not window.no_token_id:
            return

        # Check for graceful stop (secrets refresh)
        await self._v2_check_secrets_refresh()

        await self._refresh_orderbook(state)

        yes_bid = self._v2_float(state.orderbook.yes_best_bid)
        no_bid = self._v2_float(state.orderbook.no_best_bid)
        if yes_bid <= 0 and no_bid <= 0:
            return

        max_bet = self._v2_max_bet_per_asset()
        HARD_CAP_PRICE = 0.55

        # ── 1. MODEL → ALLOCATION SPLIT ───────────────────────────────────
        import math as _math
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        _now = _dt.now(_tz.utc)
        vol = compute_realized_vol(list(state.price_history))
        window_open = window.open_price or price
        move_pct = ((price - window_open) / window_open * 100) if window_open > 0 else 0

        features = {
            "move_pct_15s": move_pct,
            "realized_vol_5m": vol,
            "vol_ratio": 1.0,
            "body_ratio": 0.5,
            "prev_window_direction": (
                1
                if state.prev_window
                and state.prev_window.close_price
                and state.prev_window.open_price
                and state.prev_window.close_price >= state.prev_window.open_price
                else -1
            )
            if state.prev_window
            else 0,
            "prev_window_move_pct": (
                (state.prev_window.close_price - state.prev_window.open_price)
                / state.prev_window.open_price
                * 100
            )
            if state.prev_window
            and state.prev_window.open_price
            and state.prev_window.close_price
            else 0,
            "hour_sin": _math.sin(2 * _math.pi * _now.hour / 24),
            "hour_cos": _math.cos(2 * _math.pi * _now.hour / 24),
            "dow_sin": _math.sin(2 * _math.pi * _now.weekday() / 7),
            "dow_cos": _math.cos(2 * _math.pi * _now.weekday() / 7),
            "signal_move_pct": move_pct,
            "signal_ask_price": state.orderbook.yes_best_ask if yes_bid > 0 else 0.50,
            "signal_seconds": seconds_since_open,
            "signal_ev": 0,
        }
        try:
            prob_up = self.model_server.predict(f"{state.asset}_5m", features)
        except Exception:
            prob_up = 0.50

        # Model-weighted allocation split + spend cap when the signal is weak.
        up_pct, down_pct = self._v2_allocation_split(prob_up)
        budget_scale = self._v2_confidence_budget_scale(prob_up)

        buy_only_start = self._v2_buy_only_start_seconds()
        commit_start = self._v2_commit_start_seconds()

        # Direction commitment at T+60: lock the allocation split so the bot
        # stops chasing model flips mid-window.  Before T+60 the model can
        # update the split freely.  At T+60 the current split is frozen and
        # reused for the rest of the main phase + buy-only phase.
        DIRECTION_LOCK_SECONDS = 60.0
        if seconds_since_open >= DIRECTION_LOCK_SECONDS:
            if "locked_up_pct" not in pos:
                # First tick after lock point — freeze current allocation
                pos["locked_prob_up"] = prob_up
                pos["locked_up_pct"] = up_pct
                pos["locked_down_pct"] = down_pct
                pos["locked_budget_scale"] = budget_scale
                logger.info(
                    "v2_direction_locked",
                    asset=state.asset,
                    seconds=int(seconds_since_open),
                    prob_up=round(prob_up, 3),
                    up_pct=up_pct,
                    down_pct=down_pct,
                    budget_scale=round(budget_scale, 2),
                )
            else:
                # After lock: use frozen values, ignore model flips
                prob_up = pos.get("locked_prob_up", prob_up)
                up_pct = pos.get("locked_up_pct", up_pct)
                down_pct = pos.get("locked_down_pct", down_pct)
                budget_scale = pos.get("locked_budget_scale", budget_scale)

        if seconds_since_open >= buy_only_start:
            if "late_phase_up_pct" not in pos:
                pos["late_phase_prob_up"] = prob_up
                pos["late_phase_up_pct"] = up_pct
                pos["late_phase_down_pct"] = down_pct
                pos["late_phase_budget_scale"] = budget_scale
                logger.info(
                    "v2_buy_only_mode_entered",
                    asset=state.asset,
                    seconds=int(seconds_since_open),
                    prob_up=round(prob_up, 3),
                    up_pct=up_pct,
                    down_pct=down_pct,
                    budget_scale=round(budget_scale, 2),
                )
            else:
                prob_up = pos.get("late_phase_prob_up", prob_up)
                up_pct = pos.get("late_phase_up_pct", up_pct)
                down_pct = pos.get("late_phase_down_pct", down_pct)
                budget_scale = pos.get("late_phase_budget_scale", budget_scale)

        # ── 2a. LATE-WINDOW DUMP (T+210 to T+250) ────────────────────────
        # Sell near-worthless losing shares before commit. Even 3c per share
        # is better than $0 at resolution.
        LATE_DUMP_START = 210.0
        if LATE_DUMP_START <= seconds_since_open < commit_start:
            last_sell_ts = getattr(state, "v2_last_sell_ts", 0.0)
            dump_cooldown_ok = (
                (time.time() - last_sell_ts) >= 15.0 if last_sell_ts > 0 else True
            )
            if dump_cooldown_ok:
                for side_up, shares, bid in [
                    (True, int(state.early_up_shares), yes_bid),
                    (False, int(state.early_down_shares), no_bid),
                ]:
                    if shares < 5 or bid <= 0 or bid >= 0.10:
                        continue  # not worthless, or no shares
                    # Find a lot to sell
                    for order in state.early_dca_orders:
                        if not order.get("filled"):
                            continue
                        side = self._v2_normalized_order_side(state, order)
                        if (side == "UP") != side_up:
                            continue
                        inv_shares = self._v2_order_inventory_shares(order)
                        if inv_shares < 5:
                            continue
                        sell_shares = min(inv_shares, 5)
                        inv_notional = self._v2_order_inventory_notional(order)
                        unit_cost = inv_notional / inv_shares if inv_shares > 0 else 0.0
                        sell_cost = round(sell_shares * unit_cost, 2)
                        sell_token = (
                            window.yes_token_id if side_up else window.no_token_id
                        )
                        proceeds = await self._early_sell(
                            state,
                            pos,
                            bid,
                            "LATE_DUMP",
                            sell_shares=sell_shares,
                            sell_cost=sell_cost,
                            sell_token_id=sell_token,
                            sell_side_up=side_up,
                            sell_order=order,
                        )
                        if proceeds and proceeds > 0:
                            state.v2_last_sell_ts = time.time()
                            state.v2_last_sell_side_up = side_up
                            if side_up:
                                state.v2_last_sell_price_up = bid
                            else:
                                state.v2_last_sell_price_down = bid
                            logger.info(
                                "v2_late_dump",
                                asset=state.asset,
                                side="UP" if side_up else "DOWN",
                                shares=sell_shares,
                                bid=round(bid, 3),
                                proceeds=round(proceeds, 2),
                                seconds=int(seconds_since_open),
                            )
                            await self._v2_poll_fills(state)
                        break  # one sell per tick max

        # ── 2b. COMMITMENT CHECK (T >= 250) ───────────────────────────────
        if seconds_since_open >= commit_start:
            cancel_fn = self._v2_cancel_client()
            cancelled = 0
            for order in list(self._v2_open_orders(state)):
                oid = order.get("order_id", "")
                try:
                    if cancel_fn and oid:
                        cancel_fn(oid)
                    release = self._v2_order_reserved_remaining(order)
                    if release > 0 and not order.get("budget_released"):
                        side = self._v2_normalized_order_side(state, order)
                        self._release_v2_budget(
                            state, release, "commit_cancel", side, oid
                        )
                    self._set_v2_order_reserved_remaining(order, 0.0)
                    order["budget_released"] = True
                    order["closed"] = True
                    cancelled += 1
                except Exception:
                    pass
            state.early_dca_orders = [
                o for o in state.early_dca_orders if not o.get("closed")
            ]

            up_avg = (
                (state.early_up_cost / state.early_up_shares)
                if state.early_up_shares > 0
                else 0
            )
            dn_avg = (
                (state.early_down_cost / state.early_down_shares)
                if state.early_down_shares > 0
                else 0
            )
            combined = (
                (up_avg + dn_avg)
                if (state.early_up_shares > 0 and state.early_down_shares > 0)
                else 0
            )

            if seconds_since_open % 15 < 1:
                logger.info(
                    "v2_committed",
                    asset=state.asset,
                    seconds=int(seconds_since_open),
                    prob_up=round(prob_up, 3),
                    up_shares=int(state.early_up_shares),
                    down_shares=int(state.early_down_shares),
                    up_avg=round(up_avg, 3),
                    down_avg=round(dn_avg, 3),
                    combined_avg=round(combined, 3),
                    cancelled=cancelled,
                )
            return

        # ── 3. BUDGET CURVE (smooth ramp from the open allocation) ───────
        max_deploy_pct = self._v2_budget_curve_pct(seconds_since_open)
        max_deploy_usd = min(max_bet, max_bet * max_deploy_pct * budget_scale)
        current_filled = self._v2_filled_position_cost_usd(state)
        current_total_notional = self._v2_current_total_notional(state)
        budget_curve_remaining = max(max_deploy_usd - current_filled, 0)

        desired_prices_by_side: dict[str, list[float]] = {"UP": [], "DOWN": []}

        late_buy_only = seconds_since_open >= buy_only_start
        base_offsets = [0.01, 0.03] if late_buy_only else [0.00, 0.01, 0.02]
        hard_cap_skipped = 0
        for side_up, token_id, bid, side_budget, pct in [
            (True, window.yes_token_id, yes_bid, 0.0, up_pct),
            (False, window.no_token_id, no_bid, 0.0, down_pct),
        ]:
            if not token_id or bid <= 0:
                continue
            if bid > HARD_CAP_PRICE:
                hard_cap_skipped += 1
                continue
            side = "UP" if side_up else "DOWN"
            levels = 3 if pct >= 0.70 else (2 if pct >= 0.50 else 1)
            if late_buy_only:
                levels = min(levels, len(base_offsets))
            desired_prices: list[float] = []
            for offset in base_offsets[:levels]:
                post_price = round(bid - offset, 2)
                if 0.01 <= post_price <= HARD_CAP_PRICE:
                    desired_prices.append(post_price)
                elif post_price > HARD_CAP_PRICE:
                    hard_cap_skipped += 1
            desired_prices_by_side[side] = desired_prices

        # ── 4. RECYCLE STALE OPEN ORDERS ──────────────────────────────────
        (
            kept_prices_by_side,
            stale_orders_cancelled,
        ) = await self._v2_recycle_stale_orders(
            state,
            desired_prices_by_side,
        )

        # ── 5. ACCUMULATE BOTH SIDES (model-weighted, cumulative side targets) ──
        remaining = self._v2_remaining_budget(state)
        usable = min(remaining, budget_curve_remaining)
        target_up_usd = round(max_deploy_usd * up_pct, 2)
        target_down_usd = round(max_deploy_usd * down_pct, 2)
        current_up_committed = self._v2_side_committed_usd(state, True)
        current_down_committed = self._v2_side_committed_usd(state, False)
        up_budget = max(round(target_up_usd - current_up_committed, 2), 0.0)
        down_budget = max(round(target_down_usd - current_down_committed, 2), 0.0)
        deficit_total = round(up_budget + down_budget, 2)
        if deficit_total > usable > 0:
            scale = usable / deficit_total
            up_budget = round(up_budget * scale, 2)
            down_budget = round(down_budget * scale, 2)
        up_budget, down_budget = self._v2_strong_favored_budget_boost(
            prob_up=prob_up,
            usable=usable,
            up_budget=up_budget,
            down_budget=down_budget,
            up_bid=yes_bid,
            down_bid=no_bid,
            up_shares=int(state.early_up_shares),
            down_shares=int(state.early_down_shares),
        )

        current_up_avg = (
            (state.early_up_cost / state.early_up_shares)
            if state.early_up_shares > 0
            else 0.0
        )
        current_down_avg = (
            (state.early_down_cost / state.early_down_shares)
            if state.early_down_shares > 0
            else 0.0
        )
        current_combined_avg = (
            current_up_avg + current_down_avg
            if (state.early_up_shares > 0 and state.early_down_shares > 0)
            else 0.0
        )
        current_payout_floor = min(
            int(state.early_up_shares), int(state.early_down_shares)
        )
        current_cost_above_floor = max(
            round(current_total_notional - current_payout_floor, 2), 0.0
        )
        current_position_ev = self._v2_expected_position_ev(
            prob_up=prob_up,
            up_shares=int(state.early_up_shares),
            down_shares=int(state.early_down_shares),
            net_cost=current_total_notional,
        )
        max_combined_avg, max_cost_above_floor = self._v2_pair_risk_limits(prob_up)
        has_orphan, missing_side_up = self._v2_orphan_pair_state(state)

        from py_clob_client.clob_types import CreateOrderOptions

        options = CreateOrderOptions(tick_size="0.01", neg_risk=False)
        base_shares = 5

        posted_up = 0
        posted_down = 0
        pair_guard_skipped = 0

        for side_up, token_id, bid, side_budget, pct in [
            (True, window.yes_token_id, yes_bid, up_budget, up_pct),
            (False, window.no_token_id, no_bid, down_budget, down_pct),
        ]:
            if not token_id or bid <= 0:
                continue
            if bid > HARD_CAP_PRICE:
                continue
            side = "UP" if side_up else "DOWN"
            levels = 3 if pct >= 0.70 else (2 if pct >= 0.50 else 1)
            if late_buy_only:
                levels = min(levels, len(base_offsets))

            for offset in base_offsets[:levels]:
                post_price = round(bid - offset, 2)
                if post_price < 0.01 or post_price > HARD_CAP_PRICE:
                    if post_price > HARD_CAP_PRICE:
                        hard_cap_skipped += 1
                    continue
                if post_price in kept_prices_by_side.get(side, set()):
                    continue

                notional = round(base_shares * post_price, 2)
                if notional > side_budget:
                    continue

                # Anti-churn: don't buy a side above the price we last sold it at.
                # This prevents the buy-sell-buy-sell loop where we buy DOWN at 58c,
                # sell at 49c, then immediately buy again at 56c.
                # Only buy back CHEAPER than what we sold for.
                last_sell_price = (
                    getattr(state, "v2_last_sell_price_up", 0.0)
                    if side_up
                    else getattr(state, "v2_last_sell_price_down", 0.0)
                )
                if last_sell_price > 0 and post_price >= last_sell_price:
                    pair_guard_skipped += 1
                    continue

                side_shares = (
                    int(state.early_up_shares)
                    if side_up
                    else int(state.early_down_shares)
                )
                other_shares = (
                    int(state.early_down_shares)
                    if side_up
                    else int(state.early_up_shares)
                )
                lagging_side = side_shares < other_shares
                other_avg = current_down_avg if side_up else current_up_avg
                expensive_side_cap = self._v2_expensive_side_price_cap(
                    seconds_since_open
                )
                if (
                    other_avg > 0
                    and post_price > other_avg
                    and post_price > expensive_side_cap
                ):
                    pair_guard_skipped += 1
                    continue
                projected = self._v2_projected_pair_metrics(
                    state,
                    prob_up=prob_up,
                    current_total_notional=current_total_notional,
                    side_up=side_up,
                    shares=base_shares,
                    notional=notional,
                )
                projected_combined = float(projected["combined_avg"])
                projected_cost_above_floor = float(projected["cost_above_floor"])
                projected_pair_exists = int(projected["payout_floor"]) >= 5
                projected_position_ev = float(projected["expected_ev"])

                # If only one side has filled, freeze additional buys on that side until
                # the missing side catches up at an acceptable starter-pair price.
                if current_payout_floor <= 0 and side_shares >= 5 and other_shares <= 0:
                    pair_guard_skipped += 1
                    continue

                # As soon as a candidate buy would complete the pair, enforce the same
                # pair-quality guardrails. This blocks "catch-up" fills like buying the
                # missing side at 0.65 against an existing 0.52 fill in a weak-signal window.
                if projected_pair_exists:
                    edge = abs(prob_up - 0.50)
                    favored_side = (prob_up >= 0.50 and side_up) or (
                        prob_up < 0.50 and not side_up
                    )

                    # Balance cap: favored side can't exceed 75% of total shares.
                    # This prevents 96/4 positions — we want directional lean, not
                    # all-in one-sided bets.  K9 ends up 45-55% balanced.
                    total_shares_now = int(state.early_up_shares) + int(
                        state.early_down_shares
                    )
                    if total_shares_now >= 10 and favored_side:
                        favored_shares = (
                            side_shares + base_shares
                        )  # projected after this buy
                        favored_pct = favored_shares / (total_shares_now + base_shares)
                        if favored_pct > 0.75:
                            pair_guard_skipped += 1
                            continue

                    # Favored side with model edge: use loose guard only
                    if favored_side and edge >= 0.08:
                        # Still check: don't buy if projected EV is deeply negative
                        if projected_position_ev < -1.0:
                            pair_guard_skipped += 1
                            continue
                    else:
                        # Unfavored / weak-signal side gets guard — but looser
                        # when under-represented (needs to catch up for balance)
                        under_represented = side_shares < other_shares
                        if under_represented:
                            # Loose guard: let the hedge side catch up
                            projected_within_limits = projected_position_ev >= -0.50
                        else:
                            projected_within_limits = (
                                projected_combined <= max_combined_avg + 0.02
                                and projected_cost_above_floor
                                <= max_cost_above_floor + 0.50
                                and projected_position_ev >= -0.10
                            )
                        if not projected_within_limits:
                            pair_guard_skipped += 1
                            continue

                if not self._reserve_v2_budget(
                    state, notional, "exec_" + side.lower(), side
                ):
                    continue
                try:
                    await self._post_cheap_order(
                        state,
                        token_id,
                        post_price,
                        base_shares,
                        notional,
                        side_up,
                        options,
                        target_size=notional,
                    )
                    if side_up:
                        posted_up += 1
                    else:
                        posted_down += 1
                    side_budget -= notional
                    kept_prices_by_side.setdefault(side, set()).add(post_price)
                except Exception:
                    self._release_v2_budget(state, notional, "exec_post_error", side)

        # ── 5b. INCOMPLETE-PAIR RESCUE / SALVAGE ───────────────────────────
        if (
            has_orphan
            and missing_side_up is not None
            and seconds_since_open < buy_only_start
        ):
            missing_side = "UP" if missing_side_up else "DOWN"
            filled_side = "DOWN" if missing_side_up else "UP"
            salvage_side_up = not missing_side_up
            missing_token = (
                window.yes_token_id if missing_side_up else window.no_token_id
            )
            missing_bid = yes_bid if missing_side_up else no_bid
            missing_ask = self._v2_float(
                state.orderbook.yes_best_ask
                if missing_side_up
                else state.orderbook.no_best_ask
            )
            filled_bid = no_bid if missing_side_up else yes_bid
            total_orphan_shares = int(state.early_up_shares) + int(
                state.early_down_shares
            )
            last_sell_ts = getattr(state, "v2_last_sell_ts", 0.0)
            salvage_cooldown_ok = (
                (time.time() - last_sell_ts) >= self._v2_sell_cooldown_seconds()
                if last_sell_ts > 0
                else True
            )
            rescue_price = self._v2_incomplete_pair_rescue_price(
                missing_bid, missing_ask, HARD_CAP_PRICE
            )
            rescue_notional = (
                round(base_shares * rescue_price, 2) if rescue_price > 0 else 0.0
            )
            projected_rescue = (
                self._v2_projected_pair_metrics(
                    state,
                    prob_up=prob_up,
                    current_total_notional=current_total_notional,
                    side_up=missing_side_up,
                    shares=base_shares,
                    notional=rescue_notional,
                )
                if rescue_notional > 0
                else {
                    "combined_avg": 9.99,
                    "cost_above_floor": 9.99,
                    "expected_ev": -9.99,
                }
            )
            rescue_due = (
                seconds_since_open >= 15.0
                and (time.time() - getattr(state, "v2_last_rescue_ts", 0.0))
                >= self._v2_rescue_retry_seconds()
            )
            rescue_allowed = (
                rescue_due
                and rescue_price >= 0.01
                and rescue_price <= HARD_CAP_PRICE
                and current_total_notional + rescue_notional <= max_bet + 1e-9
                and self._v2_rescue_worth_completing(
                    prob_up=prob_up,
                    projected=projected_rescue,
                    max_combined_avg=max_combined_avg,
                    max_cost_above_floor=max_cost_above_floor,
                )
                and not self._v2_should_salvage_orphan(
                    prob_up=prob_up,
                    seconds_since_open=seconds_since_open,
                    orphan_side_up=salvage_side_up,
                    total_shares=total_orphan_shares,
                    current_bid=filled_bid,
                    projected=projected_rescue,
                    max_combined_avg=max_combined_avg,
                    max_cost_above_floor=max_cost_above_floor,
                )
            )

            if rescue_allowed and missing_token:
                self._v2_cancel_open_orders_for_side(
                    state, missing_side, "incomplete_pair_rescue"
                )
                if self._reserve_v2_budget(
                    state,
                    rescue_notional,
                    f"rescue_{missing_side.lower()}",
                    missing_side,
                ):
                    try:
                        await self._post_cheap_order(
                            state,
                            missing_token,
                            rescue_price,
                            base_shares,
                            rescue_notional,
                            missing_side_up,
                            options,
                            target_size=rescue_notional,
                        )
                        state.v2_last_rescue_ts = time.time()
                        kept_prices_by_side.setdefault(missing_side, set()).add(
                            rescue_price
                        )
                        if missing_side_up:
                            posted_up += 1
                        else:
                            posted_down += 1
                        logger.info(
                            "v2_incomplete_pair_rescue",
                            asset=state.asset,
                            seconds=int(seconds_since_open),
                            missing_side=missing_side,
                            rescue_price=round(rescue_price, 2),
                            rescue_notional=round(rescue_notional, 2),
                            projected_combined=round(
                                float(projected_rescue["combined_avg"]), 3
                            ),
                            projected_cost_above_floor=round(
                                float(projected_rescue["cost_above_floor"]), 2
                            ),
                            projected_ev=round(
                                float(projected_rescue["expected_ev"]), 2
                            ),
                        )
                    except Exception:
                        self._release_v2_budget(
                            state, rescue_notional, "rescue_post_error", missing_side
                        )
            elif salvage_cooldown_ok and self._v2_should_salvage_orphan(
                prob_up=prob_up,
                seconds_since_open=seconds_since_open,
                orphan_side_up=salvage_side_up,
                total_shares=total_orphan_shares,
                current_bid=filled_bid,
                projected=projected_rescue,
                max_combined_avg=max_combined_avg,
                max_cost_above_floor=max_cost_above_floor,
            ):
                salvage_order = None
                salvage_cost = 0.0
                salvage_shares = 0
                for order in state.early_dca_orders:
                    if not order.get("filled"):
                        continue
                    side = self._v2_normalized_order_side(state, order)
                    if (side == "UP") != salvage_side_up:
                        continue
                    inventory_shares = self._v2_order_inventory_shares(order)
                    if inventory_shares < 5:
                        continue
                    salvage_order = order
                    salvage_shares = min(inventory_shares, 5)
                    inventory_notional = self._v2_order_inventory_notional(order)
                    salvage_cost = (
                        round(
                            (inventory_notional / inventory_shares) * salvage_shares, 2
                        )
                        if inventory_shares > 0
                        else 0.0
                    )
                    break
                if salvage_order and salvage_shares >= 5:
                    proceeds = await self._early_sell(
                        state,
                        pos,
                        filled_bid,
                        "ORPHAN_SALVAGE",
                        sell_shares=salvage_shares,
                        sell_cost=salvage_cost,
                        sell_token_id=window.yes_token_id
                        if salvage_side_up
                        else window.no_token_id,
                        sell_side_up=salvage_side_up,
                        sell_order=salvage_order,
                    )
                    if proceeds and proceeds > 0:
                        sell_fired = True
                        sell_reason = "ORPHAN_SALVAGE"
                        state.v2_last_sell_ts = time.time()
                        state.v2_last_sell_side_up = salvage_side_up
                        if salvage_side_up:
                            state.v2_last_sell_price_up = filled_bid
                        else:
                            state.v2_last_sell_price_down = filled_bid
                        await self._v2_poll_fills(state)

        # ── 6. SELL-AND-RECOVER (T+60 to T+180 only) ────────────────────
        # Sell excess inventory above the payout floor when the market bid is
        # richer than the model-implied hold value for that side.
        sell_fired = locals().get("sell_fired", False)
        sell_reason = locals().get("sell_reason", "")
        up_avg = (
            (state.early_up_cost / state.early_up_shares)
            if state.early_up_shares > 0
            else 0
        )
        dn_avg = (
            (state.early_down_cost / state.early_down_shares)
            if state.early_down_shares > 0
            else 0
        )
        combined_avg = (
            (up_avg + dn_avg)
            if (state.early_up_shares > 0 and state.early_down_shares > 0)
            else 0
        )
        payout_floor = min(int(state.early_up_shares), int(state.early_down_shares))
        cost_above_floor = max(round(current_filled - payout_floor, 2), 0.0)

        last_sell_ts = getattr(state, "v2_last_sell_ts", 0.0)
        sell_cooldown_ok = (
            (time.time() - last_sell_ts) >= self._v2_sell_cooldown_seconds()
            if last_sell_ts > 0
            else True
        )

        bad_pair_now = payout_floor >= 5 and self._v2_bad_pair_recycle_active(
            current_combined_avg=current_combined_avg,
            current_cost_above_floor=current_cost_above_floor,
            current_position_ev=current_position_ev,
            max_combined_avg=max_combined_avg,
            max_cost_above_floor=max_cost_above_floor,
        )
        normal_sell_window = (
            self._v2_sell_start_seconds() <= seconds_since_open < buy_only_start
        )
        bad_pair_sell_window = (
            self._v2_bad_pair_sell_start_seconds()
            <= seconds_since_open
            < buy_only_start
        )

        if sell_cooldown_ok and (
            normal_sell_window or (bad_pair_now and bad_pair_sell_window)
        ):
            sell_candidates: list[dict] = []
            if normal_sell_window:
                for side_up, shares, bid in [
                    (True, int(state.early_up_shares), yes_bid),
                    (False, int(state.early_down_shares), no_bid),
                ]:
                    excess_shares = max(shares - payout_floor, 0)
                    if excess_shares < 5 or shares - 5 < 10 or bid <= 0:
                        continue
                    hold_value = self._v2_side_hold_value(prob_up, side_up)
                    edge_over_hold = round(bid - hold_value, 3)
                    if edge_over_hold < 0.005:
                        continue
                    sell_candidates.append(
                        {
                            "side_up": side_up,
                            "side": "UP" if side_up else "DOWN",
                            "bid": bid,
                            "excess_shares": excess_shares,
                            "edge_over_hold": edge_over_hold,
                            "reason": "PAYOUT_FLOOR",
                        }
                    )

            # UNFAVORED_RICH: sell the expensive unfavored side when model is confident
            # This is the K9 "sell losers to fund winners" mechanic
            if not sell_candidates and normal_sell_window:
                edge = abs(prob_up - 0.50)
                if edge >= 0.10:  # model is at least 60/40 confident
                    favored_up = prob_up >= 0.50
                    for side_up, shares, bid, avg in [
                        (True, int(state.early_up_shares), yes_bid, current_up_avg),
                        (False, int(state.early_down_shares), no_bid, current_down_avg),
                    ]:
                        # Only sell the UNFAVORED side
                        if side_up == favored_up:
                            continue
                        # Must have shares and decent bid
                        if shares < 5 or bid < 0.10:
                            continue
                        # Only sell if this side's avg is expensive (>0.55)
                        if avg < 0.55:
                            continue
                        # Keep at least 5 shares as hedge
                        if shares - 5 < 5 and shares > 5:
                            continue
                        sell_candidates.append(
                            {
                                "side_up": side_up,
                                "side": "UP" if side_up else "DOWN",
                                "bid": bid,
                                "excess_shares": shares,
                                "edge_over_hold": round(
                                    bid - self._v2_side_hold_value(prob_up, side_up), 3
                                ),
                                "reason": "UNFAVORED_RICH",
                            }
                        )

            if (
                not sell_candidates
                and bad_pair_now
                and current_position_ev < 0.20
                and current_combined_avg > 1.01
            ):
                total_shares_now = int(state.early_up_shares) + int(
                    state.early_down_shares
                )
                for side_up, shares, bid in [
                    (True, int(state.early_up_shares), yes_bid),
                    (False, int(state.early_down_shares), no_bid),
                ]:
                    if shares < 5 or bid < self._v2_orphan_salvage_min_bid():
                        continue
                    # Allow breaking a small bad pair (e.g. 5/5) if EV is negative.
                    # Only protect the hedge on larger positions.
                    if total_shares_now > 20 and shares - 5 < 5:
                        continue
                    projected_after_sell = self._v2_projected_sell_metrics(
                        state,
                        prob_up=prob_up,
                        current_total_notional=current_total_notional,
                        side_up=side_up,
                        shares=5,
                        proceeds=round(bid * 5, 2),
                    )
                    projected_cost_above_floor = float(
                        projected_after_sell["cost_above_floor"]
                    )
                    projected_position_ev = float(projected_after_sell["expected_ev"])
                    cost_improvement = round(
                        current_cost_above_floor - projected_cost_above_floor, 2
                    )
                    ev_improvement = round(
                        projected_position_ev - current_position_ev, 2
                    )
                    # Loosen threshold: any positive improvement counts for bad pairs
                    if cost_improvement < 0.01 and ev_improvement < 0.01:
                        continue
                    sell_candidates.append(
                        {
                            "side_up": side_up,
                            "side": "UP" if side_up else "DOWN",
                            "bid": bid,
                            "excess_shares": shares,
                            "edge_over_hold": round(
                                bid - self._v2_side_hold_value(prob_up, side_up), 3
                            ),
                            "cost_improvement": cost_improvement,
                            "ev_improvement": ev_improvement,
                            "reason": "BAD_PAIR",
                        }
                    )

            if sell_candidates:
                # For BAD_PAIR: prefer selling the side with higher avg price
                # (the expensive side pushing combined above 1.00).
                # For PAYOUT_FLOOR: prefer selling excess with best edge_over_hold.
                def _sell_sort_key(item):
                    if item.get("reason") == "BAD_PAIR":
                        # Prefer the side with higher avg — that's the one hurting the pair
                        side_avg = (
                            current_up_avg if item["side_up"] else current_down_avg
                        )
                        return (
                            side_avg,  # higher avg = sell this one first
                            item.get("ev_improvement", 0.0),
                            item.get("cost_improvement", 0.0),
                        )
                    return (
                        item.get("ev_improvement", item["edge_over_hold"]),
                        item.get("cost_improvement", 0.0),
                        item["excess_shares"],
                        item["edge_over_hold"],
                    )

                sell_candidates.sort(key=_sell_sort_key, reverse=True)
                sell_side = sell_candidates[0]
                sell_side_up = sell_side["side_up"]
                sell_bid = sell_side["bid"]

                sell_lots = []
                for order in state.early_dca_orders:
                    if not order.get("filled"):
                        continue
                    side = self._v2_normalized_order_side(state, order)
                    if (side == "UP") != sell_side_up:
                        continue
                    inventory_shares = self._v2_order_inventory_shares(order)
                    if inventory_shares < 5:
                        continue
                    inventory_notional = self._v2_order_inventory_notional(order)
                    sell_shares = min(inventory_shares, 5)
                    unit_cost = (
                        inventory_notional / inventory_shares
                        if inventory_shares > 0 and inventory_notional > 0
                        else self._v2_order_actual_price(order)
                    )
                    sell_lots.append(
                        {
                            "order": order,
                            "shares": sell_shares,
                            "price": self._v2_order_actual_price(order),
                            "notional": round(sell_shares * unit_cost, 2),
                        }
                    )

                if sell_lots:
                    sell_lots.sort(key=lambda x: -x["price"])
                    lot = sell_lots[0]
                    sell_token = (
                        window.yes_token_id if sell_side_up else window.no_token_id
                    )
                    sell_reason = str(sell_side.get("reason", "PAYOUT_FLOOR"))
                    proceeds = await self._early_sell(
                        state,
                        pos,
                        sell_bid,
                        sell_reason,
                        sell_shares=lot["shares"],
                        sell_cost=lot["notional"],
                        sell_token_id=sell_token,
                        sell_side_up=sell_side_up,
                        sell_order=lot["order"],
                    )
                    if proceeds and proceeds > 0:
                        sell_fired = True
                        state.v2_last_sell_ts = time.time()
                        state.v2_last_sell_side_up = sell_side_up
                        if sell_side_up:
                            state.v2_last_sell_price_up = sell_bid
                        else:
                            state.v2_last_sell_price_down = sell_bid
                        await self._v2_poll_fills(state)

        # ── 7. LOG ────────────────────────────────────────────────────────
        net_cost = self._v2_filled_position_cost_usd(state)
        total_shares = int(state.early_up_shares + state.early_down_shares)
        logger.info(
            "v2_execution_tick",
            asset=state.asset,
            seconds=int(seconds_since_open),
            prob_up=round(prob_up, 3),
            up_pct=up_pct,
            down_pct=down_pct,
            posted_up=posted_up,
            posted_down=posted_down,
            stale_orders_cancelled=stale_orders_cancelled,
            hard_cap_skipped=hard_cap_skipped,
            pair_guard_skipped=pair_guard_skipped,
            up_shares=int(state.early_up_shares),
            down_shares=int(state.early_down_shares),
            up_avg=round(up_avg, 3),
            down_avg=round(dn_avg, 3),
            combined_avg=round(combined_avg, 3),
            payout_floor=payout_floor,
            cost_above_floor=round(cost_above_floor, 2),
            net_cost=round(net_cost, 2),
            total_shares=total_shares,
            sell_fired=sell_fired,
            sell_reason=sell_reason,
            filled_usd=round(net_cost, 2),
            remaining_budget=round(self._v2_remaining_budget(state), 2),
            budget_curve_pct=round(max_deploy_pct, 3),
            budget_scale=round(budget_scale, 2),
        )

    # ── V2 SECRETS REFRESH ────────────────────────────────────────────────

    async def _v2_check_secrets_refresh(self):
        """Re-read EARLY_ENTRY_ENABLED from Secrets Manager every 60s for graceful stop."""
        now = time.time()
        if not hasattr(self, "_v2_graceful_stop_requested"):
            self._v2_graceful_stop_requested = False
        if not hasattr(self, "_last_secrets_check"):
            self._last_secrets_check = 0.0
        if now - self._last_secrets_check < 60:
            return
        self._last_secrets_check = now
        try:
            import json as _json

            import boto3

            client = boto3.client("secretsmanager", region_name="eu-west-1")
            raw = client.get_secret_value(SecretId="polymarket-bot-env")
            secrets = _json.loads(raw["SecretString"])
            new_enabled = secrets.get("EARLY_ENTRY_ENABLED", "true").lower() == "true"
            if new_enabled != self.settings.early_entry_enabled:
                logger.info("v2_secrets_toggled_early_entry", enabled=new_enabled)
            self.settings.early_entry_enabled = new_enabled
            self._v2_graceful_stop_requested = not new_enabled
        except Exception as e:
            logger.warning("v2_secrets_refresh_failed", error=str(e)[:120])

    # ── V2 REBALANCE ENGINE (legacy — no longer called from tick) ──────────

    async def _v2_rebalance_cycle(
        self, state: AssetState, price: float, seconds_since_open: float
    ):
        """Continuous rebalance: adjust UP/DOWN exposure toward model-driven target.

        Runs every 3s tick after accumulation. Three steps:
        1. Sell partial expensive lots on overweight side (if imbalance > 10%)
        2. Buy on underweight side (reuse accumulation specs)
        3. Ensure at least 1 active order per side
        """
        pos = state.early_position
        if not pos or not self._v2_order_execution_enabled():
            return
        window = state.tracker.current
        if not window:
            return

        # No rebalancing in first 30s — let accumulation build initial position
        if seconds_since_open < 30:
            return

        await self._refresh_orderbook(state)

        # ── 1. READ STATE ──────────────────────────────────────────────────
        up_shares = state.early_up_shares
        down_shares = state.early_down_shares
        yes_bid = self._v2_float(state.orderbook.yes_best_bid)
        no_bid = self._v2_float(state.orderbook.no_best_bid)

        up_value = up_shares * yes_bid if yes_bid > 0 else 0
        down_value = down_shares * no_bid if no_bid > 0 else 0
        total_value = up_value + down_value

        if total_value < 1.0:
            return  # not enough position to rebalance

        current_up_ratio = up_value / total_value

        # ── 2. MODEL INPUT ─────────────────────────────────────────────────
        import math as _math
        from datetime import datetime as _dt
        from datetime import timezone as _tz

        _now = _dt.now(_tz.utc)
        vol = compute_realized_vol(list(state.price_history))
        window_open = window.open_price or price
        move_pct = ((price - window_open) / window_open * 100) if window_open > 0 else 0

        features = {
            "move_pct_15s": move_pct,
            "realized_vol_5m": vol,
            "vol_ratio": 1.0,
            "body_ratio": 0.5,
            "prev_window_direction": (
                1
                if state.prev_window
                and state.prev_window.close_price
                and state.prev_window.open_price
                and state.prev_window.close_price >= state.prev_window.open_price
                else -1
            )
            if state.prev_window
            else 0,
            "prev_window_move_pct": (
                (state.prev_window.close_price - state.prev_window.open_price)
                / state.prev_window.open_price
                * 100
            )
            if state.prev_window
            and state.prev_window.open_price
            and state.prev_window.close_price
            else 0,
            "hour_sin": _math.sin(2 * _math.pi * _now.hour / 24),
            "hour_cos": _math.cos(2 * _math.pi * _now.hour / 24),
            "dow_sin": _math.sin(2 * _math.pi * _now.weekday() / 7),
            "dow_cos": _math.cos(2 * _math.pi * _now.weekday() / 7),
            "signal_move_pct": move_pct,
            "signal_ask_price": state.orderbook.yes_best_ask if yes_bid > 0 else 0.50,
            "signal_seconds": seconds_since_open,
            "signal_ev": 0,
        }
        try:
            prob_up = self.model_server.predict(f"{state.asset}_5m", features)
        except Exception:
            prob_up = 0.50

        # ── 3. TARGET EXPOSURE ─────────────────────────────────────────────
        if prob_up > 0.55:
            target_up = 0.65
        elif prob_up < 0.45:
            target_up = 0.35
        else:
            target_up = 0.50
        target_down = 1.0 - target_up

        delta_up = current_up_ratio - target_up
        imbalance_threshold = 0.05

        logger.info(
            "v2_rebalance_state",
            asset=state.asset,
            seconds=int(seconds_since_open),
            prob_up=round(prob_up, 3),
            target_up=target_up,
            current_up_ratio=round(current_up_ratio, 3),
            delta_up=round(delta_up, 3),
            up_value=round(up_value, 2),
            down_value=round(down_value, 2),
            up_shares=int(up_shares),
            down_shares=int(down_shares),
            remaining_budget=self._v2_remaining_budget(state),
        )

        # ── 4. SELL OVERWEIGHT SIDE ────────────────────────────────────────
        sold_usd = 0.0
        if abs(delta_up) > imbalance_threshold:
            overweight_up = delta_up > 0
            sold_usd = await self._v2_rebalance_sell_overweight(
                state,
                pos,
                overweight_up,
                delta_up,
                total_value,
                seconds_since_open,
            )
            if sold_usd > 0:
                await self._v2_poll_fills(state)

        # ── 5. DUAL-SIDED LADDER QUOTE ────────────────────────────────────
        await self._v2_quote_dual_ladder(
            state, prob_up, delta_up, yes_bid, no_bid, seconds_since_open
        )

    async def _v2_rebalance_sell_overweight(
        self,
        state: AssetState,
        pos: dict,
        overweight_up: bool,
        delta: float,
        total_value: float,
        seconds_since_open: float,
    ) -> float:
        """Sell partial expensive lots on the overweight side. Returns USD sold."""
        direction_up = pos.get("direction_up", True)
        overweight_side = "UP" if overweight_up else "DOWN"

        # Determine current bid for the overweight side (needed for no-loss check)
        if overweight_up:
            current_bid = self._v2_float(state.orderbook.yes_best_bid)
        else:
            current_bid = self._v2_float(state.orderbook.no_best_bid)

        if current_bid <= 0:
            return 0.0

        # Collect sellable lots: filled, actual_price >= 0.40, on overweight side,
        # AND current_bid >= entry price (no-loss guard)
        sellable = []
        skipped_loss = 0
        for order in state.early_dca_orders:
            if not order.get("filled"):
                continue
            actual_price = self._v2_order_actual_price(order)
            if actual_price < 0.40:
                continue  # NEVER sell cheap lots
            side = self._v2_normalized_order_side(state, order)
            if side != overweight_side:
                continue
            filled_shares = int(order.get("filled_shares", 0) or 0)
            if filled_shares <= 0:
                continue
            # No-loss guard: only sell if current bid >= entry price
            if current_bid < actual_price:
                skipped_loss += 1
                continue
            sellable.append(
                {
                    "shares": filled_shares,
                    "price": actual_price,
                    "notional": round(filled_shares * actual_price, 2),
                }
            )
        if skipped_loss > 0:
            logger.info(
                "v2_rebalance_skipped_loss_lots",
                asset=state.asset,
                side=overweight_side,
                skipped=skipped_loss,
                bid=round(current_bid, 2),
            )

        if not sellable:
            logger.info(
                "v2_rebalance_no_sellable",
                asset=state.asset,
                overweight_side=overweight_side,
            )
            return 0.0

        # Sort expensive-first (sell most expensive lots first)
        sellable.sort(key=lambda x: -x["price"])

        # Dynamic sell fraction: proportional to imbalance, capped at 50%
        sell_fraction = min(0.50, abs(delta))
        excess_usd = abs(delta) * total_value
        sell_target = excess_usd * sell_fraction

        sell_shares = 0
        sell_cost = 0.0
        for lot in sellable:
            if sell_cost >= sell_target:
                break
            take_shares = lot["shares"]
            take_cost = lot["notional"]
            # Partial lot: only take what we need
            if sell_cost + take_cost > sell_target and lot["price"] > 0:
                needed = sell_target - sell_cost
                take_shares = max(int(needed / lot["price"]), 5)  # Polymarket min 5
                if take_shares > lot["shares"]:
                    take_shares = lot["shares"]
                take_cost = round(take_shares * lot["price"], 2)
            sell_shares += take_shares
            sell_cost += take_cost

        if sell_shares < 5:
            logger.info(
                "v2_rebalance_sell_too_small",
                asset=state.asset,
                sell_shares=sell_shares,
                min_shares=5,
            )
            return 0.0

        logger.info(
            "v2_rebalance_sell",
            asset=state.asset,
            side=overweight_side,
            sell_shares=sell_shares,
            sell_cost=round(sell_cost, 2),
            excess_usd=round(excess_usd, 2),
            sell_target=round(sell_target, 2),
            bid=round(current_bid, 2),
        )

        # Pass the correct token_id and side for the overweight side
        window = state.tracker.current
        if window and overweight_up:
            sell_token_id = window.yes_token_id
        elif window and not overweight_up:
            sell_token_id = window.no_token_id
        else:
            sell_token_id = None

        proceeds = await self._early_sell(
            state,
            pos,
            current_bid,
            "REBALANCE",
            sell_shares=sell_shares,
            sell_cost=sell_cost,
            sell_token_id=sell_token_id,
            sell_side_up=overweight_up,
        )
        return proceeds or 0.0

    async def _v2_quote_dual_ladder(
        self,
        state: AssetState,
        prob_up: float,
        delta_up: float,
        yes_bid: float,
        no_bid: float,
        seconds_since_open: float,
    ):
        """Direction-biased ladder quoting with budget preservation.

        Every 1s tick:
        1. Cancel all existing open orders (rebuild fresh)
        2. Determine directional regime from model + price
        3. Post winner side first (priority), hedge side capped
        4. No-fill kicker on winner side only if dry
        """
        window = state.tracker.current
        if not window:
            return
        remaining = self._v2_remaining_budget(state)
        if remaining < 0.05:
            return

        # ── 1. CANCEL ALL OPEN ORDERS ──────────────────────────────────────
        cancel_fn = self._v2_cancel_client()
        for order in list(self._v2_open_orders(state)):
            oid = order.get("order_id", "")
            try:
                if cancel_fn and oid:
                    cancel_fn(oid)
                release_notional = self._v2_order_reserved_remaining(order)
                if release_notional > 0 and not order.get("budget_released"):
                    side = self._v2_normalized_order_side(state, order)
                    self._release_v2_budget(
                        state, release_notional, "quote_rebuild", side, oid
                    )
                self._set_v2_order_reserved_remaining(order, 0.0)
                order["budget_released"] = True
                order["closed"] = True
            except Exception as e:
                logger.warning("v2_quote_cancel_error", oid=oid[:16], error=str(e)[:80])
        state.early_dca_orders = [
            o for o in state.early_dca_orders if not o.get("closed")
        ]

        remaining = self._v2_remaining_budget(state)  # refresh after cancels

        # ── 2. DIRECTIONAL REGIME ──────────────────────────────────────────
        window_open = window.open_price or 0
        current_price = (
            state.tracker.current_price
            if hasattr(state.tracker, "current_price")
            else 0
        )
        # Use last known price from price_history if tracker doesn't expose it
        if not current_price and state.price_history:
            current_price = state.price_history[-1]
        price_confirms_up = (
            current_price > window_open if (window_open and current_price) else False
        )
        price_confirms_down = (
            current_price < window_open if (window_open and current_price) else False
        )

        # 5 regimes
        if prob_up > 0.55 and price_confirms_up:
            regime = "STRONG_UP"
            winner_up = True
            winner_levels = 3
            hedge_levels = 1
            hedge_budget_cap = 0.10  # max 10% of remaining for hedge
        elif prob_up > 0.55:
            regime = "WEAK_UP"
            winner_up = True
            winner_levels = 2
            hedge_levels = 1
            hedge_budget_cap = 0.15
        elif prob_up < 0.45 and price_confirms_down:
            regime = "STRONG_DOWN"
            winner_up = False
            winner_levels = 3
            hedge_levels = 1
            hedge_budget_cap = 0.10
        elif prob_up < 0.45:
            regime = "WEAK_DOWN"
            winner_up = False
            winner_levels = 2
            hedge_levels = 1
            hedge_budget_cap = 0.15
        else:
            regime = "NEUTRAL"
            winner_up = True  # arbitrary for neutral
            winner_levels = 2
            hedge_levels = 2
            hedge_budget_cap = 0.40

        base_shares = 5
        base_offsets = [0.00, 0.01, 0.02]
        from py_clob_client.clob_types import CreateOrderOptions

        options = CreateOrderOptions(tick_size="0.01", neg_risk=False)

        posted_up = 0
        posted_down = 0
        loser_skipped = False
        hedge_budget_used = 0.0
        hedge_budget_limit = remaining * hedge_budget_cap

        # ── 3. POST WINNER SIDE FIRST ──────────────────────────────────────
        winner_token = window.yes_token_id if winner_up else window.no_token_id
        winner_bid = yes_bid if winner_up else no_bid
        winner_side = "UP" if winner_up else "DOWN"
        hedge_token = window.no_token_id if winner_up else window.yes_token_id
        hedge_bid = no_bid if winner_up else yes_bid
        hedge_side = "DOWN" if winner_up else "UP"

        for phase, token_id, bid, side_up, side, max_levels, is_hedge in [
            (
                "winner",
                winner_token,
                winner_bid,
                winner_up,
                winner_side,
                winner_levels,
                False,
            ),
            (
                "hedge",
                hedge_token,
                hedge_bid,
                not winner_up,
                hedge_side,
                hedge_levels,
                True,
            ),
        ]:
            if not token_id or bid <= 0:
                continue

            offsets = base_offsets[:max_levels]
            for offset in offsets:
                post_price = round(bid - offset, 2)
                if post_price < 0.01 or post_price > 0.98:
                    continue

                actual_notional = round(base_shares * post_price, 2)

                # Hedge budget cap
                if (
                    is_hedge
                    and hedge_budget_used + actual_notional > hedge_budget_limit
                ):
                    loser_skipped = True
                    continue

                if not self._reserve_v2_budget(
                    state, actual_notional, "quote_" + phase, side
                ):
                    continue

                try:
                    await self._post_cheap_order(
                        state,
                        token_id,
                        post_price,
                        base_shares,
                        actual_notional,
                        side_up,
                        options,
                        target_size=actual_notional,
                    )
                    if side_up:
                        posted_up += 1
                    else:
                        posted_down += 1
                    if is_hedge:
                        hedge_budget_used += actual_notional
                except Exception as e:
                    self._release_v2_budget(
                        state, actual_notional, "quote_post_error", side
                    )

        # ── 4. LOG ────────────────────────────────────────────────────────
        logger.info(
            "v2_quote_dual_ladder",
            asset=state.asset,
            seconds=int(seconds_since_open),
            posted_up=posted_up,
            posted_down=posted_down,
            regime=regime,
            prob_up=round(prob_up, 3),
            delta_up=round(delta_up, 3),
            price_confirms="UP"
            if price_confirms_up
            else ("DOWN" if price_confirms_down else "NONE"),
            loser_skipped=loser_skipped,
            hedge_budget_used=round(hedge_budget_used, 2),
            hedge_budget_limit=round(hedge_budget_limit, 2),
            remaining_budget=self._v2_remaining_budget(state),
        )

        # ── 5. NO-FILL KICKER (winner side only) ─────────────────────────
        now = time.time()
        last_fill = state.early_last_fill_ts
        no_fill_seconds = (now - last_fill) if last_fill > 0 else seconds_since_open
        if no_fill_seconds >= 12.0:
            remaining_now = self._v2_remaining_budget(state)
            if remaining_now >= 0.05 and winner_token and winner_bid > 0:
                ask = self._v2_float(
                    state.orderbook.yes_best_ask
                    if winner_up
                    else state.orderbook.no_best_ask
                )
                if 0 < ask < 1.0:
                    kicker_price = round(ask - 0.01, 2)
                    if 0.01 <= kicker_price <= 0.98:
                        kicker_notional = round(base_shares * kicker_price, 2)
                        if self._reserve_v2_budget(
                            state, kicker_notional, "no_fill_kicker", winner_side
                        ):
                            try:
                                await self._post_cheap_order(
                                    state,
                                    winner_token,
                                    kicker_price,
                                    base_shares,
                                    kicker_notional,
                                    winner_up,
                                    options,
                                    target_size=kicker_notional,
                                )
                                logger.info(
                                    "v2_no_fill_kicker",
                                    asset=state.asset,
                                    side=winner_side,
                                    price=kicker_price,
                                    no_fill_seconds=round(no_fill_seconds, 1),
                                    seconds=int(seconds_since_open),
                                )
                            except Exception as e:
                                self._release_v2_budget(
                                    state, kicker_notional, "kicker_error", winner_side
                                )

    async def _v2_rebalance_ensure_active(
        self, state: AssetState, seconds_since_open: float
    ):
        """Ensure at least 1 active order per side if budget allows."""
        window = state.tracker.current
        if not window:
            return
        remaining = self._v2_remaining_budget(state)
        if remaining < 0.05:
            return

        from py_clob_client.clob_types import CreateOrderOptions

        options = CreateOrderOptions(tick_size="0.01", neg_risk=False)

        open_orders = self._v2_open_orders(state)
        for side_up, token_id in [
            (True, window.yes_token_id),
            (False, window.no_token_id),
        ]:
            if not token_id:
                continue
            side = "UP" if side_up else "DOWN"
            side_open = [
                o
                for o in open_orders
                if self._v2_normalized_order_side(state, o) == side
            ]
            if side_open:
                continue  # already have active orders on this side

            bid = self._v2_float(
                state.orderbook.yes_best_bid if side_up else state.orderbook.no_best_bid
            )
            if bid <= 0:
                continue

            # Post 1 order at bid-1¢
            post_price = round(bid - 0.01, 2)
            if post_price < 0.01 or post_price > 0.98:
                continue
            _, specs = self._v2_accum_specs(bid)
            if not specs:
                continue
            spec = specs[0]  # first spec (at bid)
            if not self._reserve_v2_budget(
                state, spec["actual_notional_usd"], "ensure_active", side
            ):
                # Try cheapest spec
                for s in reversed(specs):
                    if self._reserve_v2_budget(
                        state, s["actual_notional_usd"], "ensure_active", side
                    ):
                        spec = s
                        break
                else:
                    continue

            await self._post_cheap_order(
                state,
                token_id,
                spec["post_price"],
                spec["shares"],
                spec["actual_notional_usd"],
                side_up,
                options,
                target_size=spec["target_size"],
            )
            logger.info(
                "v2_rebalance_ensure_posted",
                asset=state.asset,
                side=side,
                price=spec["post_price"],
                shares=spec["shares"],
                seconds=int(seconds_since_open),
            )

    async def _v2_poll_fills(self, state: AssetState):
        """Check fill status of all tracked GTC orders, update state totals."""
        if not state.early_dca_orders:
            return
        pos = state.early_position or {}
        direction_up = pos.get("direction_up", True)
        filled_any = False
        for order in state.early_dca_orders:
            if order.get("filled"):
                continue
            oid = order.get("order_id")
            if not oid:
                continue
            try:
                resp = self._v2_get_order_status(state, order)
                status = str(
                    resp.get("status", "") if isinstance(resp, dict) else ""
                ).upper()
                logger.info(
                    "v2_poll_order",
                    asset=state.asset,
                    oid=oid[:16],
                    status=status,
                    side=order.get("side", ""),
                )
                actual_shares = self._v2_order_actual_shares(order)
                actual_price = self._v2_order_actual_price(order)
                actual_notional_usd = self._v2_order_actual_notional(order)
                total_filled_shares, total_filled_notional, is_complete = (
                    self._v2_fill_progress(
                        order, resp if isinstance(resp, dict) else {}
                    )
                )
                prev_filled_shares = int(order.get("filled_shares", 0) or 0)
                prev_filled_notional = round(
                    self._v2_float(order.get("filled_notional_usd", 0)), 2
                )
                delta_shares = max(total_filled_shares - prev_filled_shares, 0)
                delta_notional = round(
                    max(total_filled_notional - prev_filled_notional, 0.0), 2
                )
                if delta_notional > 0:
                    side = order.get("side", "")
                    reserved_remaining = self._v2_order_reserved_remaining(order)
                    move_notional = round(min(reserved_remaining, delta_notional), 2)
                    if move_notional > 0:
                        self._move_v2_reserved_to_filled(
                            state, move_notional, "fill", side, oid
                        )
                    self._set_v2_order_reserved_remaining(
                        order, reserved_remaining - move_notional
                    )
                    order["filled_notional_usd"] = round(total_filled_notional, 2)
                    order["filled_shares"] = total_filled_shares
                    if delta_shares <= 0 and actual_price > 0:
                        delta_shares = int(round(move_notional / actual_price))
                    self._set_v2_order_inventory(
                        order,
                        self._v2_order_inventory_shares(order) + delta_shares,
                        self._v2_order_inventory_notional(order) + move_notional,
                    )
                    is_up = (
                        (side == "UP")
                        or (side == "main" and direction_up)
                        or (side == "hedge" and not direction_up)
                    )
                    if is_up:
                        state.early_up_shares += delta_shares
                        state.early_up_cost += move_notional
                    else:
                        state.early_down_shares += delta_shares
                        state.early_down_cost += move_notional
                    filled_any = True
                    state.early_last_fill_ts = time.time()
                    order["partially_filled"] = not is_complete
                    logger.info(
                        "v2_fill_detected",
                        asset=state.asset,
                        side=side,
                        is_up=is_up,
                        actual_price=actual_price,
                        actual_notional_usd=move_notional,
                        actual_shares=delta_shares,
                        total_filled_notional_usd=round(total_filled_notional, 2),
                        total_filled_shares=total_filled_shares,
                        remaining_reserved_notional_usd=self._v2_order_reserved_remaining(
                            order
                        ),
                        fill_status="full" if is_complete else "partial",
                    )
                    self._log_activity(
                        state,
                        f"FILL {side} ${actual_price:.2f}",
                        f"${move_notional:.2f} ({delta_shares} shares)",
                    )
                if is_complete:
                    order["filled"] = True
                    order["partially_filled"] = False
                    order["filled_shares"] = actual_shares
                    order["filled_notional_usd"] = actual_notional_usd
                    if (
                        self._v2_order_inventory_shares(order) <= 0
                        and actual_shares > 0
                    ):
                        self._set_v2_order_inventory(
                            order, actual_shares, actual_notional_usd
                        )
                    self._set_v2_order_reserved_remaining(order, 0.0)
                elif status in ("CANCELED", "CANCELLED", "REJECTED", "EXPIRED"):
                    if not order.get("budget_released"):
                        release_notional = self._v2_order_reserved_remaining(order)
                        if release_notional > 0:
                            self._release_v2_budget(
                                state,
                                release_notional,
                                "poll_terminal",
                                order.get("side", ""),
                                oid,
                            )
                        self._set_v2_order_reserved_remaining(order, 0.0)
                        order["budget_released"] = True
                    order["closed"] = True
                    logger.info(
                        "v2_order_terminal",
                        asset=state.asset,
                        oid=oid[:16],
                        status=status,
                        side=order.get("side", ""),
                    )
            except Exception as e:
                logger.info(
                    "v2_poll_error", asset=state.asset, oid=oid[:16], error=str(e)[:80]
                )
        state.early_dca_orders = [
            o for o in state.early_dca_orders if not o.get("closed")
        ]
        if filled_any:
            self._sync_v2_position_from_fills(state)

    async def _verify_early_polymarket(
        self, early_slug: str, market_slug: str, delay: int
    ):
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
            logger.warning(
                "early_verify_polymarket_failed", slug=early_slug, error=str(e)[:60]
            )

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
            has_exit = any(
                i.get("source") in ("early_exit", "early_hedge_exit") for i in all_items
            )
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
                    pnl = (
                        round((size / fill) - size, 2)
                        if won and fill > 0
                        else round(-size, 2)
                    )
                self.dynamo._trades.update_item(
                    Key={"id": t["id"]},
                    UpdateExpression="SET resolved=:r, pnl=:p, outcome_source=:s",
                    ExpressionAttributeValues={
                        ":r": 1,
                        ":p": Decimal(str(pnl)),
                        ":s": "polymarket_verified",
                    },
                )
                logger.info(
                    "early_trade_verified",
                    slug=early_slug,
                    side=side,
                    pnl=pnl,
                    had_exit=has_exit,
                )
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
            has_exit = any(
                i.get("source") in ("early_exit", "early_hedge_exit") for i in all_items
            )

            unresolved = [
                t
                for t in all_items
                if int(t.get("resolved", 0)) == 0 and t.get("source") == "early_entry"
            ]
            for t in unresolved:
                side = t.get("side", "")
                fill = float(t.get("fill_price", 0) or 0)
                size = float(t.get("size_usd", 0) or 0)

                if has_exit:
                    # Position was sold mid-window — P&L already recorded in the sell
                    pnl = 0
                    logger.info(
                        "early_trade_resolved_with_exit",
                        slug=early_slug,
                        side=side,
                        pnl=0,
                        note="P&L in early_exit trade",
                    )
                else:
                    # Position held to resolution
                    won = (side == "YES" and went_up) or (side == "NO" and not went_up)
                    if won and fill > 0:
                        pnl = round((size / fill) - size, 2)
                    else:
                        pnl = round(-size, 2)
                    logger.info(
                        "early_trade_resolved",
                        slug=early_slug,
                        side=side,
                        won=won,
                        pnl=pnl,
                        source="coinbase",
                    )

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

    async def _early_rotate_buy(
        self, state: AssetState, pos: dict, proceeds: float, ask: float, window
    ):
        """After selling, buy back cheap on SAME side with the proceeds."""
        if self.settings.mode != "live" or not window:
            return
        token_id = pos["token_id"]
        if not token_id:
            return
        actual_notional_usd = 0.0
        try:
            from py_clob_client.clob_types import (
                CreateOrderOptions,
                OrderArgs,
                OrderType,
            )
            from py_clob_client.order_builder.constants import BUY

            options = CreateOrderOptions(tick_size="0.01", neg_risk=False)
            buy_price = round(ask, 2)
            shares, actual_notional_usd = self._v2_order_size(proceeds, buy_price)
            if shares <= 0 or actual_notional_usd <= 0:
                return
            if not self._reserve_v2_budget(
                state, actual_notional_usd, "rotate_buy", "rotate"
            ):
                return
            args = OrderArgs(price=buy_price, size=shares, side=BUY, token_id=token_id)
            signed = self.trader.client.create_order(args, options)
            resp = self.trader.client.post_order(signed, OrderType.GTC)
            oid = resp.get("orderID", "")
            if oid:
                state.early_dca_orders.append(
                    self._build_v2_tracked_order(
                        order_id=oid,
                        actual_shares=shares,
                        actual_price=buy_price,
                        actual_notional_usd=actual_notional_usd,
                        target_size=proceeds,
                        side="rotate",
                    )
                )
                logger.info(
                    "early_rotate_buy",
                    asset=state.asset,
                    slug=pos["slug"],
                    actual_price=buy_price,
                    actual_notional_usd=round(actual_notional_usd, 2),
                    target_size=round(proceeds, 2),
                    actual_shares=shares,
                    potential_payout=round(shares * 1.0, 2),
                    order_id=oid[:16],
                )
            else:
                self._release_v2_budget(
                    state, actual_notional_usd, "rotate_no_order_id", "rotate"
                )
                logger.warning("early_rotate_buy_failed", resp=str(resp)[:80])
        except Exception as e:
            self._release_v2_budget(
                state, actual_notional_usd, "rotate_post_error", "rotate"
            )
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
                    if not order.get("budget_released"):
                        release_notional = self._v2_order_reserved_remaining(order)
                        if release_notional > 0:
                            self._release_v2_budget(
                                state,
                                release_notional,
                                "cancel",
                                order.get("side", ""),
                                oid,
                            )
                        self._set_v2_order_reserved_remaining(order, 0.0)
                        order["budget_released"] = True
                except Exception as e:
                    logger.warning(
                        "early_cancel_error",
                        asset=state.asset,
                        oid=oid[:16],
                        error=str(e)[:80],
                    )
        if state.early_hedge_order_id:
            try:
                self.trader.client.cancel(state.early_hedge_order_id)
                cancelled += 1
            except Exception as e:
                logger.warning(
                    "early_cancel_hedge_error", asset=state.asset, error=str(e)[:80]
                )
            state.early_hedge_order_id = None

        state.early_dca_orders = [o for o in state.early_dca_orders if o.get("filled")]
        logger.info(
            "early_cancel_unfilled",
            asset=state.asset,
            slug=state.early_position["slug"] if state.early_position else "",
            cancelled=cancelled,
            up_shares=int(state.early_up_shares),
            down_shares=int(state.early_down_shares),
            filled_position_cost_usd=self._v2_filled_position_cost_usd(state),
            reserved_open_order_usd=self._v2_reserved_open_order_usd(state),
        )

    # ── EARLY ENTRY CHECKPOINTS ──────────────────────────────────────────
    async def _early_checkpoint(
        self,
        state: AssetState,
        price: float,
        seconds_since_open: float,
        checkpoint: int,
    ):
        """Phase 4: Hard stop for expensive entries. Conditions (ALL required):
        1. Entry price >= 40¢ (never touch cheap accumulation fills)
        2. Position down > 25% from entry price
        3. T+30 to T+240 (not too early, not in final 60s)
        Immediately rebuys same side cheap if ask <= 40¢.
        """
        pos = state.early_position
        if not pos:
            return

        # Gate 3: T+240 hard cutoff — no selling in final 60s
        if seconds_since_open > 240:
            return

        # Lot-aware: only consider filled orders with actual_price >= 0.40 on main side
        direction_up = pos.get("direction_up", True)
        expensive_lots = []
        for order in state.early_dca_orders:
            if not order.get("filled"):
                continue
            actual_price = self._v2_order_actual_price(order)
            if actual_price < 0.40:
                continue
            # Check if this order is on the main side
            side = self._v2_normalized_order_side(state, order)
            is_main = (side == "UP" and direction_up) or (
                side == "DOWN" and not direction_up
            )
            if not is_main:
                continue
            filled_shares = int(order.get("filled_shares", 0) or 0)
            if filled_shares <= 0:
                continue
            expensive_lots.append(
                {
                    "shares": filled_shares,
                    "price": actual_price,
                    "notional": round(filled_shares * actual_price, 2),
                }
            )

        if not expensive_lots:
            return  # no expensive lots to sell — protect cheap fills

        expensive_shares = sum(lot["shares"] for lot in expensive_lots)
        expensive_cost = sum(lot["notional"] for lot in expensive_lots)
        expensive_avg = (
            round(expensive_cost / expensive_shares, 4) if expensive_shares > 0 else 0
        )

        await self._refresh_orderbook(state)
        main_bid = (
            state.orderbook.yes_best_bid
            if direction_up
            else state.orderbook.no_best_bid
        )
        if not main_bid or main_bid <= 0:
            return

        # Gate: only sell if expensive lots are down more than 25%
        position_value_pct = (
            ((main_bid - expensive_avg) / expensive_avg * 100)
            if expensive_avg > 0
            else 0
        )

        logger.info(
            "early_checkpoint",
            asset=state.asset,
            slug=pos["slug"],
            checkpoint=checkpoint,
            direction="UP" if direction_up else "DOWN",
            entry_price=round(expensive_avg, 3),
            main_bid=round(main_bid, 3),
            position_value_pct=round(position_value_pct, 1),
            expensive_lots=len(expensive_lots),
            expensive_shares=expensive_shares,
            total_shares=pos.get("shares", 0),
        )
        self._log_activity(
            state,
            f"CHECK val={position_value_pct:+.0f}%",
            "HOLD" if position_value_pct >= -25 else "SELL_STOP_25",
        )

        if position_value_pct < -25:
            window = state.tracker.current
            sell_proceeds = await self._early_sell(
                state,
                pos,
                main_bid,
                "HARD_STOP_25",
                sell_shares=expensive_shares,
                sell_cost=expensive_cost,
            )
            if sell_proceeds and sell_proceeds > 0.50 and window:
                main_ask = (
                    state.orderbook.yes_best_ask
                    if direction_up
                    else state.orderbook.no_best_ask
                )
                if main_ask and 0 < main_ask <= 0.40:
                    await self._early_rotate_buy(
                        state, pos, sell_proceeds, main_ask, window
                    )
                else:
                    logger.info(
                        "early_rotate_skip_expensive", slug=pos["slug"], ask=main_ask
                    )

    def _v2_apply_sell_fill(
        self,
        state: AssetState,
        sold_up: bool,
        shares: int,
        cost_basis: float,
        sell_order: dict | None = None,
    ) -> None:
        if sold_up:
            state.early_up_shares = max(state.early_up_shares - shares, 0)
            state.early_up_cost = max(state.early_up_cost - cost_basis, 0)
        else:
            state.early_down_shares = max(state.early_down_shares - shares, 0)
            state.early_down_cost = max(state.early_down_cost - cost_basis, 0)

        if sell_order is not None:
            remaining_shares = max(
                self._v2_order_inventory_shares(sell_order) - shares, 0
            )
            remaining_notional = max(
                self._v2_order_inventory_notional(sell_order) - cost_basis, 0.0
            )
            self._set_v2_order_inventory(
                sell_order, remaining_shares, remaining_notional
            )
            if remaining_shares == 0:
                sell_order["inventory_depleted"] = True

        self._set_v2_filled_position_cost_usd(
            state, max(self._v2_filled_position_cost_usd(state) - cost_basis, 0)
        )
        self._sync_v2_position_from_fills(state)
        logger.info(
            "v2_sell_inventory_updated",
            asset=state.asset,
            sold_side="UP" if sold_up else "DOWN",
            sold_shares=shares,
            sold_cost=round(cost_basis, 2),
            up_shares=int(state.early_up_shares),
            down_shares=int(state.early_down_shares),
            filled_usd=self._v2_filled_position_cost_usd(state),
        )

    async def _early_sell(
        self,
        state: AssetState,
        pos: dict,
        current_bid: float,
        reason: str,
        sell_shares: int | None = None,
        sell_cost: float | None = None,
        sell_token_id: str | None = None,
        sell_side_up: bool | None = None,
        sell_order: dict | None = None,
    ) -> float:
        """Sell early entry position. Returns sell proceeds or 0.

        If sell_shares is provided, sell only that many shares (lot-aware).
        Otherwise falls back to pos["shares"] (legacy aggregate).
        """
        inventory_aware = (
            sell_shares is not None
            or sell_side_up is not None
            or sell_order is not None
        )
        is_partial = inventory_aware or (
            sell_shares is not None and sell_shares < (pos.get("shares", 0) or 0)
        )
        if self.settings.mode != "live":
            logger.info(
                "early_sell_paper", asset=state.asset, reason=reason, partial=is_partial
            )
            if inventory_aware:
                sold_up = (
                    sell_side_up
                    if sell_side_up is not None
                    else pos.get("direction_up", True)
                )
                shares = (
                    sell_shares if sell_shares is not None else pos.get("shares", 0)
                )
                cost_basis = sell_cost if sell_cost is not None else pos.get("size", 0)
                self._v2_apply_sell_fill(state, sold_up, shares, cost_basis, sell_order)
                if (
                    int(state.early_up_shares) <= 0
                    and int(state.early_down_shares) <= 0
                ):
                    state.early_position = None
            elif not is_partial:
                state.early_position = None
            return 0

        try:
            from py_clob_client.clob_types import (
                CreateOrderOptions,
                OrderArgs,
                OrderType,
            )
            from py_clob_client.order_builder.constants import SELL

            options = CreateOrderOptions(tick_size="0.01", neg_risk=False)

            token_id = sell_token_id if sell_token_id is not None else pos["token_id"]
            shares = sell_shares if sell_shares is not None else pos["shares"]

            # Try FOK at current bid
            order_args = OrderArgs(
                price=current_bid, size=shares, side=SELL, token_id=token_id
            )
            signed = self.trader.client.create_order(order_args, options)
            try:
                resp = self.trader.client.post_order(signed, OrderType.FOK)
                order_id = resp.get("orderID", "")
            except Exception as sell_err:
                order_id = ""
                logger.warning(
                    "early_sell_fok_exception", slug=pos["slug"], error=str(sell_err)
                )

            if not order_id:
                # FOK failed — try GTC at bid-1¢ then bid-2¢
                import asyncio as _aio2

                for offset in (0.01, 0.02):
                    gtc_price = max(round(current_bid - offset, 2), 0.01)
                    try:
                        gtc_args = OrderArgs(
                            price=gtc_price, size=shares, side=SELL, token_id=token_id
                        )
                        gtc_signed = self.trader.client.create_order(gtc_args, options)
                        gtc_resp = self.trader.client.post_order(
                            gtc_signed, OrderType.GTC
                        )
                        gtc_id = gtc_resp.get("orderID", "")
                        logger.info(
                            "early_sell_gtc_attempt",
                            slug=pos["slug"],
                            price=gtc_price,
                            offset=offset,
                            order_id=gtc_id or "failed",
                        )
                        if gtc_id:
                            for _ in range(5):  # 5s wait
                                await _aio2.sleep(1.0)
                                try:
                                    st = self.trader.client.get_order(gtc_id).get(
                                        "status", ""
                                    )
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
                        logger.warning(
                            "early_sell_gtc_error", offset=offset, error=str(gtc_err)
                        )
                if not order_id:
                    logger.warning(
                        "early_sell_all_failed",
                        slug=pos["slug"],
                        bid=current_bid,
                        reason=reason,
                        shares=shares,
                    )

            if order_id:
                sell_proceeds = shares * current_bid
                cost_basis = sell_cost if sell_cost is not None else pos["size"]
                pnl = sell_proceeds - cost_basis
                sold_up = (
                    sell_side_up
                    if sell_side_up is not None
                    else pos.get("direction_up", True)
                )
                sold_label = "YES" if sold_up else "NO"
                logger.info(
                    "early_sell_filled",
                    slug=pos["slug"],
                    reason=reason,
                    bid=current_bid,
                    pnl=round(pnl, 2),
                    order_id=order_id[:16],
                    shares_sold=shares,
                    cost_basis=round(cost_basis, 2),
                )
                self._log_activity(
                    state,
                    f"SELL {sold_label} ${current_bid:.2f}",
                    f"P&L ${pnl:+.2f} ({reason})",
                )
                # Log to DynamoDB
                try:
                    import uuid
                    from decimal import Decimal

                    self.dynamo._trades.put_item(
                        Item={
                            "id": str(uuid.uuid4()),
                            "window_slug": pos["slug"],
                            "asset": state.asset,
                            "timeframe": "5m",
                            "side": "SELL",
                            "source": "early_exit",
                            "fill_price": Decimal(str(round(current_bid, 4))),
                            "size_usd": Decimal(str(round(sell_proceeds, 2))),
                            "shares": Decimal(str(shares)),
                            "pnl": Decimal(str(round(pnl, 2))),
                            "timestamp": Decimal(str(round(time.time(), 3))),
                            "entry_type": reason,
                            "resolved": 1,
                        }
                    )
                except Exception:
                    pass
                # Update internal inventory to match executed reality
                if inventory_aware:
                    self._v2_apply_sell_fill(
                        state, sold_up, shares, cost_basis, sell_order
                    )
                    if (
                        int(state.early_up_shares) <= 0
                        and int(state.early_down_shares) <= 0
                    ):
                        state.early_position = None
                else:
                    state.early_position = None
                return sell_proceeds

            # FOK failed — try GTC at bid+1¢
            logger.info("early_sell_fok_failed", slug=pos["slug"], trying_gtc=True)
            gtc_price = round(current_bid + 0.01, 2)
            gtc_args = OrderArgs(
                price=gtc_price, size=shares, side=SELL, token_id=token_id
            )
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
                cost_basis_gtc = sell_cost if sell_cost is not None else pos["size"]
                pnl = sell_proceeds - cost_basis_gtc
                sold_up = (
                    sell_side_up
                    if sell_side_up is not None
                    else pos.get("direction_up", True)
                )
                sold_label = "YES" if sold_up else "NO"
                logger.info(
                    "early_sell_gtc_filled",
                    slug=pos["slug"],
                    reason=reason,
                    pnl=round(pnl, 2),
                    shares_sold=shares,
                    cost_basis=round(cost_basis_gtc, 2),
                )
                self._log_activity(
                    state,
                    f"SELL {sold_label} ${gtc_price:.2f}",
                    f"P&L ${pnl:+.2f} ({reason})",
                )
                # Update internal inventory
                if inventory_aware:
                    self._v2_apply_sell_fill(
                        state, sold_up, shares, cost_basis_gtc, sell_order
                    )
                    if (
                        int(state.early_up_shares) <= 0
                        and int(state.early_down_shares) <= 0
                    ):
                        state.early_position = None
                else:
                    state.early_position = None
                return sell_proceeds

        except Exception as e:
            logger.error("early_sell_error", error=str(e))
        return 0

    def _log_early_trade(
        self,
        state,
        window,
        side,
        fill_price,
        size,
        lgbm_prob,
        ev,
        entry_type,
        limit_price,
        limit_filled,
        limit_wait_ms,
        order_id,
    ):
        """Log early entry trade to DynamoDB."""
        try:
            from decimal import Decimal

            dynamo = self.dynamo
            if not dynamo or not dynamo._available:
                return
            table = dynamo._trades
            early_slug = f"early_{window.slug}"
            import uuid

            table.put_item(
                Item={
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
                    "limit_price": Decimal(str(round(limit_price, 4)))
                    if limit_price
                    else None,
                    "limit_filled": limit_filled,
                    "limit_wait_ms": Decimal(str(int(limit_wait_ms))),
                    "model_prob": Decimal(str(round(lgbm_prob, 4))),
                    "ev": Decimal(str(round(ev, 4))),
                    "order_id": order_id or "",
                    "resolved": 0,
                }
            )
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
        state.reserved_open_order_usd = 0.0
        state.filled_position_cost_usd = 0.0
        state.early_reserved_notional = 0.0
        state.early_filled_notional = 0.0
        state.early_confirm_done = False
        state.early_last_fill_ts = 0.0
        state.v2_last_sell_ts = 0.0
        state.v2_last_sell_side_up = None
        state.v2_last_sell_price_up = 0.0
        state.v2_last_sell_price_down = 0.0
        state.v2_last_rescue_ts = 0.0
        logger.info(
            "window_opened",
            asset=state.asset,
            slug=window.slug,
            open_price=round(price, 2),
        )
        state.bayesian.reset(price, 0.5)
        try:
            await resolve_window(window)
        except Exception as e:
            logger.error(
                "market_resolve_failed",
                asset=state.asset,
                slug=window.slug,
                error=str(e),
            )
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
        task = asyncio.create_task(
            self._verify_outcome_after_delay(slug, 90), name=f"verify_{slug}"
        )
        # Verify early entry trades via Polymarket after delay (overwrites provisional)
        _early_slug = f"early_{slug}"
        early_task = asyncio.create_task(
            self._verify_early_polymarket(_early_slug, slug, 90),
            name=f"verify_early_{slug}",
        )
        early_task.add_done_callback(
            lambda t: (
                logger.error("verify_task_exception", error=str(t.exception()))
                if t.exception()
                else None
            )
        )
        task.add_done_callback(
            lambda t: (
                logger.error("verify_task_exception", error=str(t.exception()))
                if t.exception()
                else None
            )
        )

        window_record = {
            "slug": window.slug,
            "open_ts": window.open_ts,
            "close_ts": window.close_ts,
            "open_price": window.open_price,
            "close_price": window.close_price,
            "direction": window.resolved_direction.value
            if window.resolved_direction
            else None,
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
                tf = self._timeframe_key(state.tracker.window_seconds)
                pct_move = 0.0
                if window.open_price and window.close_price and window.open_price > 0:
                    pct_move = (
                        (window.close_price - window.open_price)
                        / window.open_price
                        * 100
                    )
                outcome = 1 if went_up else 0
                realized_vol = compute_realized_vol(list(state.price_history))
                self.dynamo.put_training_data(
                    {
                        "window_id": f"{state.asset}_{tf}_{window.slug}",
                        "timestamp": time.time(),
                        "asset": state.asset,
                        "timeframe": tf,
                        "open_price": round(window.open_price, 2)
                        if window.open_price
                        else 0,
                        "close_price": round(window.close_price, 2)
                        if window.close_price
                        else 0,
                        "pct_move": round(pct_move, 6),
                        "outcome": outcome,
                        "direction": "up" if went_up else "down",
                        "yes_ask_at_open": round(state.orderbook.yes_best_ask, 4),
                        "no_ask_at_open": round(state.orderbook.no_best_ask, 4),
                        "yes_bid_at_open": round(state.orderbook.yes_best_bid, 4),
                        "no_bid_at_open": round(state.orderbook.no_best_bid, 4),
                        "p_bayesian": round(state.bayesian.probability, 4),
                        "realized_vol": round(realized_vol, 6),
                        "oracle_lag_pct": round(
                            self.rtds.get_state(state.asset).oracle_lag_pct, 6
                        ),
                        # Signal-context features for LightGBM
                        "signal_move_pct": round(abs(pct_move), 6),
                        "signal_ask_price": round(state.orderbook.yes_best_ask, 4),
                        "signal_seconds": 0,  # filled by live collection
                        "signal_ev": 0,  # filled by live collection
                        # Orderbook microstructure features
                        "ofi_30s": round(self.coinbase.get_ofi_30s(state.asset), 6)
                        if hasattr(self.coinbase, "get_ofi_30s")
                        else 0,
                        "bid_ask_spread": round(
                            self.coinbase.get_bid_ask_spread(state.asset), 6
                        )
                        if hasattr(self.coinbase, "get_bid_ask_spread")
                        else 0,
                        "depth_imbalance": round(
                            self.coinbase.get_depth_imbalance(state.asset), 6
                        )
                        if hasattr(self.coinbase, "get_depth_imbalance")
                        else 0,
                        "trade_arrival_rate": round(
                            self.coinbase.get_trade_arrival_rate(state.asset), 6
                        )
                        if hasattr(self.coinbase, "get_trade_arrival_rate")
                        else 0,
                        "data_source": "live_with_orderbook"
                        if hasattr(self.coinbase, "get_ofi_30s")
                        else "live",
                        # New signal features
                        "liq_cluster_bias": round(
                            _liq_cache.get("BTC", (0.0, 0))[0], 6
                        ),
                        "btc_confirms_direction": 0,  # late-entry strategy doesn't use BTC confirmation
                        # Macro features (collected for future model retrains)
                        **self._get_macro_features(),
                    }
                )
                logger.debug(
                    "training_data_logged",
                    asset=state.asset,
                    slug=window.slug,
                    outcome=outcome,
                )
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
                    ":cp": _Dec(str(round(window.close_price, 4)))
                    if window.close_price
                    else _Dec("0"),
                    ":pm": _Dec(
                        str(
                            round(
                                (window.close_price - window.open_price)
                                / window.open_price
                                * 100,
                                6,
                            )
                        )
                    )
                    if window.open_price and window.close_price
                    else _Dec("0"),
                },
                ConditionExpression="attribute_exists(window_id)",
            )
            logger.debug(
                "early_training_outcome_set", slug=window.slug, outcome=outcome
            )
        except (
            self.dynamo._training.meta.client.exceptions.ConditionalCheckFailedException
        ):
            pass  # No early entry training data for this window — normal
        except Exception as e:
            logger.debug("early_training_outcome_failed", error=str(e)[:60])

    async def _verify_outcome_after_delay(
        self, window_slug: str, delay_seconds: int = 90
    ):
        """Wait 90s, then query Gamma API. Retry up to 5 times every 60s."""
        logger.info(
            "resolution_scheduled", slug=window_slug, wait_seconds=delay_seconds
        )
        await asyncio.sleep(delay_seconds)

        for attempt in range(6):  # initial + 5 retries
            try:
                logger.info(
                    "resolution_checking", slug=window_slug, attempt=attempt + 1
                )
                from polybot.feeds.polymarket_rest import get_market_outcome

                # Strip early_ prefix for Polymarket API (market indexed by original slug)
                lookup_slug = window_slug.removeprefix("early_")
                winner, source = await get_market_outcome(lookup_slug)

                if winner is None:
                    if attempt < 5:
                        logger.info(
                            "resolution_pending",
                            slug=window_slug,
                            attempt=attempt + 1,
                            next_retry_sec=60,
                        )
                        await asyncio.sleep(60)
                        continue
                    else:
                        logger.warning(
                            "resolution_exhausted", slug=window_slug, total_attempts=6
                        )
                        return

                logger.info(
                    "resolution_winner_found",
                    slug=window_slug,
                    winner=winner,
                    attempt=attempt + 1,
                )

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
                                    manual_sell_pnl[_cid] = (
                                        manual_sell_pnl.get(_cid, 0) + _usdc
                                    )
                                # A redeem also counts
                                elif _type == "REDEEM" and _cid:
                                    manual_sell_pnl[_cid] = (
                                        manual_sell_pnl.get(_cid, 0) + _usdc
                                    )
                except Exception:
                    pass

                # Winner confirmed — get trades from DynamoDB (NOT SQLite)
                trades = self.dynamo.get_trades_for_window(window_slug)
                for t in trades:
                    if t.get("resolved"):
                        continue  # already finalized
                    side = t.get("side", "")
                    correct = side == winner
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
                        logger.info(
                            "manual_sell_detected",
                            slug=window_slug,
                            proceeds=round(sell_proceeds, 2),
                            cost=round(size_usd, 2),
                            pnl=round(pnl, 2),
                        )
                    elif correct and fill_price > 0:
                        shares = size_usd / fill_price
                        pnl = shares * (1.0 - fill_price)
                    else:
                        pnl = -size_usd

                    self.risk.record_trade(pnl)
                    # Update DynamoDB directly (NOT SQLite)
                    try:
                        self.dynamo.update_trade_resolved(
                            t["id"], pnl, winner, correct, source
                        )
                    except Exception as e:
                        logger.warning(
                            "resolution_dynamo_update_failed",
                            id=t["id"],
                            error=str(e)[:60],
                        )
                    # Also try SQLite as backup
                    try:
                        await self.db.update_trade_verified(
                            trade_id=t["id"],
                            pnl=pnl,
                            polymarket_winner=winner,
                            correct_prediction=correct,
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
                logger.warning(
                    "verify_failed", slug=window_slug, attempt=attempt, error=str(e)
                )
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

            to_verify = [
                t
                for t in all_trades
                if (
                    # Coinbase-inferred but not Polymarket-verified
                    (
                        int(t.get("resolved", 0)) == 1
                        and t.get("outcome_source") != "polymarket_verified"
                        and t.get("outcome_source") != "manual_sell"
                    )
                    # OR still unresolved (OPEN)
                    or not int(t.get("resolved", 0))
                )
            ]

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
                correct = side == winner
                fill = float(t.get("fill_price", 0) or 0)
                size = float(t.get("size_usd", 0) or 0)
                pnl = (
                    round((size / fill) * (1 - fill), 2)
                    if correct and fill > 0
                    else round(-size, 2)
                )
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
            orphans = [
                t
                for t in trades
                if not t.get("resolved") or str(t.get("resolved")) == "0"
            ]
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
                correct = side == winner
                fill = float(t.get("fill_price", 0) or 0)
                size = float(t.get("size_usd", 0) or 0)
                pnl = (
                    round((size / fill) * (1 - fill), 2)
                    if correct and fill > 0
                    else round(-size, 2)
                )

                self.risk.record_trade(pnl)
                # Update in DynamoDB directly (SQLite may be empty in new container)
                try:
                    self.dynamo.update_trade_resolved(
                        t["id"], pnl, winner, correct, source
                    )
                except Exception:
                    pass
                # Also try SQLite
                try:
                    await self.db.update_trade_verified(
                        trade_id=t["id"],
                        pnl=pnl,
                        polymarket_winner=winner,
                        correct_prediction=correct,
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

                logger.info(
                    "orphan_resolved",
                    id=t["id"],
                    slug=slug,
                    winner=winner,
                    pnl=round(pnl, 2),
                )
        except Exception as e:
            logger.warning("orphan_check_failed", error=str(e))

    async def _execute(
        self, signal, state: AssetState, signal_ms: float = 0, bedrock_ms: float = 0
    ):
        if isinstance(self.trader, LiveTrader):
            window = state.tracker.current
            yes_id = window.yes_token_id if window else ""
            no_id = window.no_token_id if window else ""
            return await self.trader.execute(
                signal, yes_id, no_id, signal_ms=signal_ms, bedrock_ms=bedrock_ms
            )
        return await self.trader.execute(
            signal, signal_ms=signal_ms, bedrock_ms=bedrock_ms
        )

    async def _run_claim(self):
        """Run redeem.py in a subprocess so it never blocks the event loop."""
        try:
            import subprocess

            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                lambda: subprocess.run(
                    [".venv/bin/python", "scripts/redeem.py"],
                    capture_output=True,
                    text=True,
                    timeout=120,
                ),
            )
            if result.returncode == 0:
                logger.info(
                    "auto_claim_completed",
                    output=result.stdout[-200:] if result.stdout else "",
                )
            else:
                logger.warning(
                    "auto_claim_failed",
                    stderr=result.stderr[-200:] if result.stderr else "",
                )
        except Exception as e:
            logger.warning("auto_claim_failed", error=str(e)[:100])

    # 134 lines removed (dead method)

    def _log_v2_status(self, state: AssetState, seconds_since_open: float):
        """Log summary every 15s: shares, costs, combined avg, margin, order counts."""
        up_avg = (
            (state.early_up_cost / state.early_up_shares)
            if state.early_up_shares > 0
            else 0
        )
        down_avg = (
            (state.early_down_cost / state.early_down_shares)
            if state.early_down_shares > 0
            else 0
        )
        combined = up_avg + down_avg if up_avg > 0 and down_avg > 0 else 0
        margin = round((1 - combined) * 100, 1) if 0 < combined < 1 else 0
        open_orders = len([o for o in state.early_dca_orders if not o.get("filled")])
        filled_orders = len([o for o in state.early_dca_orders if o.get("filled")])
        window_metrics = self._v2_window_metrics(state)
        fill_budget_remaining = window_metrics["remaining_budget"]
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
            cheap_buy_count=window_metrics["cheap_buy_count"],
            cheap_buy_usd=window_metrics["cheap_buy_usd"],
            percent_buys_under_0_25=window_metrics["percent_buys_under_0_25"],
            trades_per_window=window_metrics["trades_per_window"],
            both_sides_posted=window_metrics["both_sides_posted"],
            both_sides_filled=window_metrics["both_sides_filled"],
            reserved_open_order_usd=window_metrics["reserved_open_order_usd"],
            filled_position_cost_usd=window_metrics["filled_position_cost_usd"],
            current_filled_budget=window_metrics["current_filled_budget"],
            current_reserved_budget=window_metrics["current_reserved_budget"],
            current_total_budget=window_metrics["current_total_budget"],
            remaining_budget=window_metrics["remaining_budget"],
            max_bet_per_asset=window_metrics["max_bet_per_asset"],
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
                    his_up = [
                        a
                        for a in activity
                        if slug in a.get("title", "") and "YES" in a.get("side", "")
                    ]
                    his_down = [
                        a
                        for a in activity
                        if slug in a.get("title", "") and "NO" in a.get("side", "")
                    ]
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
                                "our_direction": {
                                    "S": ("UP" if pos["direction_up"] else "DOWN")
                                    if pos
                                    else ""
                                },
                                "divergence": {
                                    "BOOL": (
                                        bool(his_up)
                                        and bool(pos)
                                        and not pos["direction_up"]
                                    )
                                    or (
                                        bool(his_down)
                                        and bool(pos)
                                        and pos["direction_up"]
                                    )
                                },
                            },
                        )
                        logger.info(
                            "shadow_tracked",
                            asset=state.asset,
                            slug=slug[-20:],
                            his_up=len(his_up),
                            his_down=len(his_down),
                        )
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

    def _write_live_state_async(
        self, state: AssetState, price: float, seconds_since_open: float
    ):
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
            up_avg = (
                (state.early_up_cost / state.early_up_shares)
                if state.early_up_shares > 0
                else 0
            )
            down_avg = (
                (state.early_down_cost / state.early_down_shares)
                if state.early_down_shares > 0
                else 0
            )
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
                "lgbm_prob": Decimal(
                    str(
                        round(
                            self.model_server.predict(f"{state.asset}_5m", {})
                            if False
                            else 0,
                            3,
                        )
                    )
                ),
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
                "open_orders": Decimal(
                    str(len([o for o in state.early_dca_orders if not o.get("filled")]))
                ),
                "filled_orders": Decimal(
                    str(len([o for o in state.early_dca_orders if o.get("filled")]))
                ),
                "main_filled": Decimal(str(round(state.early_main_filled, 2))),
                "hedge_filled": Decimal(str(round(state.early_hedge_filled, 2))),
                "cheap_filled": Decimal(str(self._v2_filled_notional(state))),
                "cheap_reserved": Decimal(str(self._v2_reserved_open_order_usd(state))),
                "cheap_committed": Decimal(str(self._v2_current_total_notional(state))),
                "reserved_open_order_usd": Decimal(
                    str(self._v2_reserved_open_order_usd(state))
                ),
                "filled_position_cost_usd": Decimal(
                    str(self._v2_filled_position_cost_usd(state))
                ),
                "remaining_budget": Decimal(str(self._v2_remaining_budget(state))),
                "max_bet_per_asset": Decimal(str(self._v2_max_bet_per_asset())),
                "has_position": 1 if pos else 0,
                "activity": state.early_activity_log[-20:]
                if state.early_activity_log
                else [],
            }

            # Fire and forget
            profile = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None
            _live_table = (
                boto3.Session(profile_name=profile, region_name="eu-west-1")
                .resource("dynamodb")
                .Table("polymarket-bot-live-state")
            )
            _live_table.put_item(Item=item)
        except Exception:
            pass  # Never slow down trading

    def _get_macro_features(self) -> dict:
        """Get macro features for training data. Never fails — returns defaults if API down."""
        try:
            return {
                k: round(v, 6) if isinstance(v, float) else v
                for k, v in self._macro.get_all().items()
            }
        except Exception:
            return {}

    async def _refresh_orderbook(self, state: AssetState):
        window = state.tracker.current
        if not window or not window.yes_token_id:
            if window:
                logger.debug(
                    "orderbook_skip_no_token", asset=state.asset, slug=window.slug
                )
            return
        now = time.time()
        if now - state.orderbook_age < 1.0:  # Max 1 refresh/second
            return
        state.orderbook_age = now
        try:
            yes_book = await get_orderbook(window.yes_token_id)
            no_book = (
                await get_orderbook(window.no_token_id) if window.no_token_id else {}
            )

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
                top3_yes = sorted(
                    yes_bids, key=lambda b: float(b["price"]), reverse=True
                )[:3]
                snap.yes_bid_depth = sum(float(b.get("size", 0)) for b in top3_yes)
            if no_asks:
                snap.no_best_ask = min(float(a["price"]) for a in no_asks)
            if no_bids:
                snap.no_best_bid = max(float(b["price"]) for b in no_bids)
                top3_no = sorted(
                    no_bids, key=lambda b: float(b["price"]), reverse=True
                )[:3]
                snap.no_bid_depth = sum(float(b.get("size", 0)) for b in top3_no)
            state.orderbook = snap
        except Exception as e:
            logger.error("orderbook_refresh_failed", asset=state.asset, error=str(e))
