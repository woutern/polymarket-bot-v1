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

S3_BUCKET = "polymarket-bot-data-688567279867-use1"

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
    window_open_price_at_5s: float | None = None  # BTC price at T+5s for cross-asset lead
    # Scored entry tracking
    price_at_2s: float | None = None
    price_at_8s: float | None = None
    ofi_at_2s: float | None = None
    ofi_at_8s: float | None = None
    ask_at_open: float | None = None
    window_tick_count: int = 0
    prior_window_tick_counts: deque = field(default_factory=lambda: deque(maxlen=5))
    score_evaluated: bool = False


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
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.download_file(S3_BUCKET, s3_key, tmp_path)
            table.load_from_parquet(tmp_path)
            logger.info("base_rates_loaded", asset=asset, source="s3", bins=len(table.bins))
        except Exception as e:
            logger.warning("base_rates_load_failed", asset=asset, error=str(e))
        return table

    def __init__(self, settings: Settings):
        self.settings = settings
        enabled = settings.enabled_pairs
        # Unique assets needed for price feeds
        assets = sorted({a for a, _ in enabled})
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

        # One AssetState per enabled pair
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

        logger.info("pairs_enabled", pairs=list(self.asset_states.keys()))

        # RTDS client for Chainlink oracle prices
        self.rtds = RTDSClient(assets=assets)

        self._wallet_address: str = settings.polymarket_funder or ""
        self._last_claim_check: float = 0.0
        self._last_strategy_review: float = 0.0
        self._running = False
        self._start_time = time.time()
        self._last_heartbeat = 0.0

    async def start(self):
        logger.info(
            "loop_starting",
            mode=self.settings.mode,
            bankroll=self.settings.bankroll,
            assets=list(self.asset_states.keys()),
        )

        await self.db.connect()

        # Smoke test all dependencies before trading
        from polybot.core.smoke_test import run_smoke_tests
        smoke = await run_smoke_tests(self.settings)
        if smoke.failed:
            raise RuntimeError(f"Smoke test failed: {smoke.failed}")

        # Balance check at startup only (no longer in hot loop)
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

            # Refresh models every 4 hours (non-blocking)
            try:
                self.model_server.refresh_if_needed()
            except Exception as e:
                logger.warning("model_refresh_failed", error=str(e)[:60])

            # Hourly strategy review — learn from recent trades, log insights
            if (time.time() - self._last_strategy_review) >= 3600:
                self._last_strategy_review = time.time()
                asyncio.create_task(self._strategy_review(), name="strategy_review")

    async def _tick_asset(self, state: AssetState, price: float):
        tracker = state.tracker
        window_state = tracker.tick(price)
        window = tracker.current
        if not window:
            return

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

        # Tier A: Oracle dislocation entry — fires immediately, any time in window
        remaining = window.seconds_remaining()
        if (
            oracle.dislocation
            and not state.traded_this_window
            and remaining > 30
            and window.open_price
            and window.open_price > 0
        ):
            await self._try_tier_a_entry(state, price, window, oracle, remaining)

        # Scored entry: capture data at T+2s and T+8s, evaluate at T+12s
        seconds_since_open = time.time() - window.open_ts
        state.window_tick_count += 1

        if state.price_at_2s is None and seconds_since_open >= 2.0:
            state.price_at_2s = price
            state.ofi_at_2s = self.coinbase.get_ofi_30s(state.asset) if hasattr(self.coinbase, 'get_ofi_30s') else 0.0
            await self._refresh_orderbook(state)
            pct_move = state.tracker.pct_move(price) or 0.0
            state.ask_at_open = state.orderbook.yes_best_ask if pct_move >= 0 else state.orderbook.no_best_ask

        if state.window_open_price_at_5s is None and seconds_since_open >= 5.0:
            state.window_open_price_at_5s = self.coinbase.get_price(state.asset)

        if state.price_at_8s is None and seconds_since_open >= 8.0:
            state.price_at_8s = price
            state.ofi_at_8s = self.coinbase.get_ofi_30s(state.asset) if hasattr(self.coinbase, 'get_ofi_30s') else 0.0

        # At T+12s: compute score and decide entry (replaces old _on_entry_zone)
        if (
            seconds_since_open >= 12.0
            and not state.score_evaluated
            and not state.traded_this_window
            and window.open_price
            and window.open_price > 0
        ):
            state.score_evaluated = True
            await self._evaluate_scored_entry(state, price)

        state.prev_open_ts = current_open_ts

    async def _try_tier_a_entry(self, state: AssetState, price: float, window, oracle, remaining: float):
        """Tier A: trade immediately on oracle dislocation when edge > 5%."""
        realized_vol = compute_realized_vol(list(state.price_history))
        if realized_vol <= 0:
            return

        oracle_prob = compute_oracle_probability(
            spot_price=price,
            strike=window.open_price,
            realized_vol=realized_vol,
            seconds_remaining=remaining,
        )

        await self._refresh_orderbook(state)
        yes_ask = state.orderbook.yes_best_ask

        if oracle_prob >= 0.5:
            edge = oracle_prob - yes_ask
            direction = "up"
            market_price = yes_ask
        else:
            edge = (1 - oracle_prob) - state.orderbook.no_best_ask
            direction = "down"
            market_price = state.orderbook.no_best_ask

        if edge < 0.05 or market_price < 0.20 or market_price >= 0.95:
            return

        logger.info(
            "tier_a_signal",
            asset=state.asset,
            slug=window.slug,
            oracle_lag_pct=round(oracle.oracle_lag_pct, 5),
            oracle_prob=round(oracle_prob, 4),
            yes_ask=round(yes_ask, 4),
            edge=round(edge, 4),
            direction=direction,
            t_minus=round(remaining, 1),
            realized_vol=round(realized_vol, 4),
        )

        from polybot.models import Direction, Signal, SignalSource
        signal = Signal(
            source=SignalSource.DIRECTIONAL,
            direction=Direction.UP if direction == "up" else Direction.DOWN,
            model_prob=oracle_prob if direction == "up" else (1 - oracle_prob),
            market_price=market_price,
            ev=edge / market_price if market_price > 0 else 0,
            window_slug=window.slug,
            asset=state.asset,
            p_bayesian=state.bayesian.probability,
            p_ai=None,
            pct_move=(price - window.open_price) / window.open_price * 100 if window.open_price else 0,
            seconds_remaining=remaining,
            yes_ask=state.orderbook.yes_best_ask,
            no_ask=state.orderbook.no_best_ask,
            yes_bid=state.orderbook.yes_best_bid,
            no_bid=state.orderbook.no_best_bid,
            open_price=window.open_price or 0,
        )

        if self.risk.can_trade():
            t0 = time.time()
            from polybot.strategy.bedrock_signal import get_last_latency
            await self._execute(signal, state, (time.time() - t0) * 1000, 0)
            state.traded_this_window = True

            # Log oracle fields
            try:
                self.dynamo.put_signal({
                    "window_slug": window.slug,
                    "timestamp": time.time(),
                    "asset": state.asset,
                    "timeframe": "5m",
                    "outcome": "executed",
                    "rejection_reason": "",
                    "direction": direction,
                    "pct_move": round(signal.pct_move, 6),
                    "model_prob": round(signal.model_prob, 4),
                    "market_price": round(market_price, 4),
                    "ev": round(signal.ev, 4),
                    "p_bayesian": round(state.bayesian.probability, 4),
                    "seconds_remaining": round(remaining, 1),
                    "yes_ask": round(state.orderbook.yes_best_ask, 4),
                    "no_ask": round(state.orderbook.no_best_ask, 4),
                    "current_price": round(price, 2),
                    "open_price": round(window.open_price, 2) if window.open_price else 0,
                    "entry_tier": "A",
                    "oracle_lag_pct": round(oracle.oracle_lag_pct, 6),
                    "oracle_lag_ms": round(oracle.oracle_lag_ms, 1),
                    "oracle_prob": round(oracle_prob, 4),
                })
            except Exception:
                pass

    async def _on_window_open(self, state: AssetState, price: float):
        window = state.tracker.current
        if not window:
            return
        # Store vol from closing window before resetting
        vol = compute_realized_vol(list(state.price_history))
        if vol > 0:
            state.vol_history.append(vol)

        state.prev_window = window
        state.traded_this_window = False
        state.window_high = price
        state.window_low = price
        state.window_open_price_at_5s = None
        # Reset scored entry fields
        state.prior_window_tick_counts.append(state.window_tick_count)
        state.window_tick_count = 0
        state.price_at_2s = None
        state.price_at_8s = None
        state.ofi_at_2s = None
        state.ofi_at_8s = None
        state.ask_at_open = None
        state.score_evaluated = False
        logger.info("window_opened", asset=state.asset, slug=window.slug, open_price=round(price, 2))
        state.bayesian.reset(price, 0.5)

        try:
            await resolve_window(window)
        except Exception as e:
            logger.error("market_resolve_failed", asset=state.asset, slug=window.slug, error=str(e))

        await self._refresh_orderbook(state)

    async def _on_entry_zone(self, state: AssetState, price: float):
        window = state.tracker.current
        if not window or not window.open_price:
            return

        remaining = window.seconds_remaining()

        # No hard time cutoff — time filtering done in _tick_asset (T+2s to T+15s)

        pct_move = state.tracker.pct_move(price) or 0.0
        state.bayesian.update(price, remaining)

        tf = "5m"
        seconds_since_open = time.time() - window.open_ts

        # Compute all filter values upfront for logging
        vol = compute_realized_vol(list(state.price_history))
        vol_ma = sum(state.vol_history) / len(state.vol_history) if state.vol_history else vol
        vol_ratio = vol / vol_ma if vol_ma > 0 else 1.0
        hl_range = state.window_high - state.window_low
        body = abs(price - (window.open_price or price))
        body_ratio = body / hl_range if hl_range > 0 else 0.5
        min_move = self.settings.min_move_for(state.asset, state.tracker.window_seconds)

        # Change 2: Lower threshold when previous window confirms + calm volatility
        prev_agrees = False
        if state.prev_window and state.prev_window.open_price and state.prev_window.close_price:
            prev_up = state.prev_window.close_price >= state.prev_window.open_price
            prev_agrees = (prev_up == (pct_move > 0))
        if prev_agrees and 0.8 <= vol_ratio <= 1.5 and tf == "5m":
            min_move = min(min_move, 0.015)  # lower to 0.015% for high-certainty signals

        # FILTER 1: Momentum strength
        if abs(pct_move) < min_move:
            logger.info("signal_rejected", asset=state.asset, timeframe=tf,
                        seconds_since_open=round(seconds_since_open, 1),
                        move_pct=round(pct_move, 4), vol_ratio=round(vol_ratio, 3),
                        body_ratio=round(body_ratio, 3), reason="move_too_small",
                        threshold=min_move)
            return

        # FILTER 3: Previous window continuation
        if state.prev_window and state.prev_window.open_price and state.prev_window.close_price:
            prev_up = state.prev_window.close_price >= state.prev_window.open_price
            current_up = pct_move > 0
            if prev_up != current_up:
                logger.info("signal_rejected", asset=state.asset, timeframe=tf,
                            seconds_since_open=round(seconds_since_open, 1),
                            move_pct=round(pct_move, 4), vol_ratio=round(vol_ratio, 3),
                            body_ratio=round(body_ratio, 3), reason="prev_window_disagree")
                return

        # FILTER 2: Volatility ratio
        if vol_ratio < 0.5:
            logger.info("signal_rejected", asset=state.asset, timeframe=tf,
                        seconds_since_open=round(seconds_since_open, 1),
                        move_pct=round(pct_move, 4), vol_ratio=round(vol_ratio, 3),
                        body_ratio=round(body_ratio, 3), reason="vol_too_low")
            return
        if vol_ratio > 3.0:
            logger.info("signal_rejected", asset=state.asset, timeframe=tf,
                        seconds_since_open=round(seconds_since_open, 1),
                        move_pct=round(pct_move, 4), vol_ratio=round(vol_ratio, 3),
                        body_ratio=round(body_ratio, 3), reason="vol_too_high")
            return

        # FILTER 5: Body ratio
        if body_ratio < 0.4:
            logger.info("signal_rejected", asset=state.asset, timeframe=tf,
                        seconds_since_open=round(seconds_since_open, 1),
                        move_pct=round(pct_move, 4), vol_ratio=round(vol_ratio, 3),
                        body_ratio=round(body_ratio, 3), reason="body_too_small")
            return

        await self._refresh_orderbook(state)

        # FILTER 4: Spread — skip if bid-ask spread > $0.10
        if pct_move > 0:
            spread = state.orderbook.yes_best_ask - state.orderbook.yes_best_bid
        else:
            spread = state.orderbook.no_best_ask - state.orderbook.no_best_bid
        if spread > 0.10:
            logger.debug("filter_wide_spread", asset=state.asset, spread=round(spread, 4))
            return

        # Change 3: BTC cross-asset lead for ETH/SOL
        btc_confirms = False
        btc_lead_move = 0.0
        if state.asset in ("ETH", "SOL"):
            # Find BTC_5m state to get BTC price move in first 5s
            btc_key = "BTC_5m"
            btc_state = self.asset_states.get(btc_key)
            if btc_state and btc_state.window_open_price_at_5s and btc_state.tracker.current:
                btc_open = btc_state.tracker.current.open_price
                if btc_open and btc_open > 0:
                    btc_now = self.coinbase.get_price("BTC")
                    btc_lead_move = (btc_now - btc_open) / btc_open * 100
                    # BTC confirms if same direction as ETH/SOL AND move > 0.02%
                    btc_confirms = abs(btc_lead_move) > 0.02 and (btc_lead_move > 0) == (pct_move > 0)

        # Change 1: CoinGlass liquidation bias (cached, async)
        try:
            liq_bias = await fetch_liq_cluster_bias(state.asset if state.asset == "BTC" else "BTC")
        except Exception:
            liq_bias = 0.0

        logger.info(
            "entry_zone",
            asset=state.asset,
            slug=window.slug,
            price=round(price, 2),
            pct_move=round(pct_move, 4),
            p_up=round(state.bayesian.probability, 4),
            t_minus=round(remaining, 1),
            yes_ask=round(state.orderbook.yes_best_ask, 4),
            no_ask=round(state.orderbook.no_best_ask, 4),
            prev_window_agrees=True,
            spread=round(spread, 4),
            vol_ratio=round(vol_ratio, 3),
            body_ratio=round(body_ratio, 3),
            liq_cluster_bias=round(liq_bias, 4),
            btc_confirms=btc_confirms,
            btc_lead_move=round(btc_lead_move, 4),
            min_move_applied=min_move,
        )

        # min_move already computed above (with high-certainty lowering applied)

        t_signal_start = time.time()
        evaluation = generate_directional_signal(
            bayesian=state.bayesian,
            orderbook=state.orderbook,
            current_price=price,
            open_price=window.open_price,
            seconds_remaining=remaining,
            min_move_pct=min_move,
            min_ev_threshold=self.settings.min_ev_threshold,
            max_market_price=self.settings.max_market_price,
            window_slug=window.slug,
            asset=state.asset,
        )
        signal = evaluation.signal

        # LightGBM prediction (logged alongside existing signal, not blocking yet)
        pair = f"{state.asset}_5m"
        seconds_since_open = (window.close_ts - window.open_ts) - remaining
        features = {
            "move_pct_15s": pct_move,
            "realized_vol_5m": vol,
            "vol_ratio": vol_ratio,
            "body_ratio": body_ratio,
            "prev_window_direction": (1 if state.prev_window and state.prev_window.close_price and state.prev_window.open_price and state.prev_window.close_price >= state.prev_window.open_price else -1) if state.prev_window else 0,
            "prev_window_move_pct": ((state.prev_window.close_price - state.prev_window.open_price) / state.prev_window.open_price * 100) if state.prev_window and state.prev_window.open_price and state.prev_window.close_price else 0,
            "hour_sin": __import__("math").sin(2 * __import__("math").pi * __import__("datetime").datetime.now(__import__("datetime").timezone.utc).hour / 24),
            "hour_cos": __import__("math").cos(2 * __import__("math").pi * __import__("datetime").datetime.now(__import__("datetime").timezone.utc).hour / 24),
            "dow_sin": __import__("math").sin(2 * __import__("math").pi * __import__("datetime").datetime.now(__import__("datetime").timezone.utc).weekday() / 7),
            "dow_cos": __import__("math").cos(2 * __import__("math").pi * __import__("datetime").datetime.now(__import__("datetime").timezone.utc).weekday() / 7),
            # Signal-context features
            "signal_move_pct": abs(pct_move),
            "signal_ask_price": state.orderbook.yes_best_ask,
            "signal_seconds": seconds_since_open,
            "signal_ev": evaluation.ev if evaluation.ev else 0,
        }
        lgbm_prob = self.model_server.predict(pair, features)
        model_has = self.model_server.has_model(pair)

        # Log model prediction with new features
        if model_has:
            logger.info(
                "lgbm_prediction",
                asset=state.asset,
                pair=pair,
                lgbm_prob=round(lgbm_prob, 4),
                model_age_h=round(self.model_server.get_model_age_hours(pair), 1),
                pct_move=round(pct_move, 4),
                liq_bias=round(liq_bias, 4),
                btc_confirms=btc_confirms,
                btc_lead_move=round(btc_lead_move, 4),
            )

        # If model loaded and confident, use adaptive threshold as filter
        if model_has and signal is not None:
            threshold = self.model_server.get_adaptive_threshold(pair)
            if lgbm_prob < threshold:
                logger.info("lgbm_low_confidence", pair=pair, prob=round(lgbm_prob, 4), threshold=round(threshold, 4))
                signal = None  # block trade — model not confident

        # Log every evaluation to DynamoDB (throttle: 1 per window per asset)
        if not state.traded_this_window:
            try:
                self.dynamo.put_signal({
                    "window_slug": evaluation.window_slug,
                    "timestamp": time.time(),
                    "asset": evaluation.asset,
                    "timeframe": evaluation.timeframe,
                    "outcome": evaluation.outcome,
                    "rejection_reason": evaluation.rejection_reason or "",
                    "direction": evaluation.direction or "",
                    "pct_move": round(evaluation.pct_move, 6),
                    "model_prob": round(evaluation.model_prob, 4) if evaluation.model_prob else 0,
                    "market_price": round(evaluation.market_price, 4) if evaluation.market_price else 0,
                    "ev": round(evaluation.ev, 4) if evaluation.ev else 0,
                    "p_bayesian": round(evaluation.p_bayesian, 4) if evaluation.p_bayesian else 0,
                    "seconds_remaining": round(remaining, 1),
                    "yes_ask": round(evaluation.yes_ask, 4),
                    "no_ask": round(evaluation.no_ask, 4),
                    "current_price": round(price, 2),
                    "open_price": round(window.open_price, 2) if window.open_price else 0,
                })
            except Exception:
                pass  # signal logging is best-effort

        # Only execute if orderbook was fetched recently (< 30s old) — stale = don't trade
        orderbook_fresh = (time.time() - state.orderbook_age) < 30.0
        if signal and self.risk.can_trade() and orderbook_fresh:
            signal_ms = (time.time() - t_signal_start) * 1000
            from polybot.strategy.bedrock_signal import get_last_latency
            bedrock_ms = get_last_latency(window.slug)
            await self._execute(signal, state, signal_ms, bedrock_ms)
            state.traded_this_window = True
        elif signal and not orderbook_fresh:
            logger.warning("signal_skipped_stale_orderbook", asset=state.asset, age=round(time.time() - state.orderbook_age, 1))

    async def _evaluate_scored_entry(self, state: AssetState, price: float):
        """Scored confirmation entry — replaces old single-trigger entry zone."""
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

        # Decision based on score — with hard filter override
        # OVERRIDE: strong hard filters bypass score entirely
        if lgbm_prob >= 0.65 and current_ask <= 0.55 and current_ask > 0 and ev >= 0.10:
            entry_type = "override"
            logger.info("score_override", asset=state.asset, slug=window.slug,
                        score=score.total, lgbm=round(lgbm_prob, 4),
                        ask=round(current_ask, 3), ev=round(ev, 4))
        elif score.total >= 4:
            # HIGH CONVICTION: taker FOK
            if lgbm_prob < 0.60:
                skip_reason = "lgbm_low_taker"
            elif current_ask > 0.55:
                skip_reason = "ask_above_0.55"
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

        await self.trader.resolve_window(window.slug, went_up)

        # Schedule Polymarket outcome verification 90s after window close (retry 3x at 60s)
        slug = window.slug
        task = asyncio.create_task(self._verify_outcome_after_delay(slug, 90), name=f"verify_{slug}")
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
                    "btc_confirms_direction": 1 if (state.asset != "BTC" and state.window_open_price_at_5s) else 0,
                })
                logger.debug("training_data_logged", asset=state.asset, slug=window.slug, outcome=outcome)
            except Exception as e:
                logger.debug("training_data_failed", error=str(e))

    async def _verify_outcome_after_delay(self, window_slug: str, delay_seconds: int = 90):
        """Wait 90s, then query Gamma API. Retry up to 5 times every 60s."""
        logger.info("resolution_scheduled", slug=window_slug, wait_seconds=delay_seconds)
        await asyncio.sleep(delay_seconds)

        for attempt in range(6):  # initial + 5 retries
            try:
                logger.info("resolution_checking", slug=window_slug, attempt=attempt + 1)
                from polybot.feeds.polymarket_rest import get_market_outcome
                winner, source = await get_market_outcome(window_slug)

                if winner is None:
                    if attempt < 5:
                        logger.info("resolution_pending", slug=window_slug, attempt=attempt + 1, next_retry_sec=60)
                        await asyncio.sleep(60)
                        continue
                    else:
                        logger.warning("resolution_exhausted", slug=window_slug, total_attempts=6)
                        return

                logger.info("resolution_winner_found", slug=window_slug, winner=winner, attempt=attempt + 1)

                # Winner confirmed — get trades from DynamoDB (NOT SQLite)
                trades = self.dynamo.get_trades_for_window(window_slug)
                for t in trades:
                    if t.get("resolved"):
                        continue  # already finalized
                    side = t.get("side", "")
                    correct = (side == winner)
                    fill_price = float(t.get("fill_price", 0) or 0)
                    size_usd = float(t.get("size_usd", 0) or 0)

                    if correct and fill_price > 0:
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
        """Run claim_all in a thread so it never blocks the event loop."""
        try:
            claim_all = _import_claim_all()
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, partial(claim_all, self.settings))
            logger.info("auto_claim_completed")
        except Exception as e:
            logger.warning("auto_claim_failed", error=str(e))

    async def _strategy_review(self):
        """Hourly learning loop: analyse recent trades, call Bedrock for actionable suggestions.

        Queries DynamoDB for the last hour of resolved trades and:
        - Breaks down win rate per asset × timeframe
        - Tracks AI vs Bayesian-only accuracy
        - Asks Bedrock for one specific parameter change to test
        """
        try:
            trades = self.dynamo.get_recent_trades(limit=200)
            if not trades:
                logger.info("strategy_review_no_trades")
                return

            now = time.time()
            cutoff = now - 3600
            recent = [
                t for t in trades
                if float(t.get("timestamp", 0) or 0) >= cutoff
                and t.get("resolved")
            ]

            if not recent:
                logger.info("strategy_review_no_recent_resolved", total_in_db=len(trades))
                return

            wins = sum(1 for t in recent if float(t.get("pnl", 0) or 0) > 0)
            total = len(recent)
            total_pnl = sum(float(t.get("pnl", 0) or 0) for t in recent)
            win_rate = wins / total if total else 0

            # Count rejection reasons from signals
            signals_fired = sum(int(t.get("signals_fired", 0) or 0) for t in recent)
            trades_executed = len(recent)

            # Per-asset × timeframe breakdown
            asset_stats: dict[str, dict] = {}
            for t in recent:
                a = str(t.get("asset", "BTC") or "BTC")
                if isinstance(a, dict):
                    a = a.get("S", "BTC")
                slug = str(t.get("window_slug", "") or "")
                tf = "5m"
                key = f"{a} {tf}"
                if key not in asset_stats:
                    asset_stats[key] = {"wins": 0, "total": 0, "pnl": 0.0}
                asset_stats[key]["total"] += 1
                p = float(t.get("pnl", 0) or 0)
                asset_stats[key]["pnl"] += p
                if p > 0:
                    asset_stats[key]["wins"] += 1

            # AI vs Bayesian accuracy
            ai_trades = [t for t in recent if t.get("p_ai") is not None]
            bayesian_only = [t for t in recent if t.get("p_ai") is None]
            ai_wr = (sum(1 for t in ai_trades if float(t.get("pnl", 0) or 0) > 0) / len(ai_trades)
                     if ai_trades else None)
            bayes_wr = (sum(1 for t in bayesian_only if float(t.get("pnl", 0) or 0) > 0) / len(bayesian_only)
                        if bayesian_only else None)

            avg_ev = (sum(float(t.get("ev", 0) or 0) for t in recent) / total) if total else 0

            logger.info(
                "strategy_review",
                period_hours=1,
                trades=total,
                wins=wins,
                win_rate=round(win_rate, 3),
                pnl=round(total_pnl, 4),
                bankroll=round(self.risk.bankroll, 2),
                avg_ev=round(avg_ev, 4),
                ai_trades=len(ai_trades),
                ai_wr=round(ai_wr, 3) if ai_wr is not None else None,
                bayesian_only_wr=round(bayes_wr, 3) if bayes_wr is not None else None,
                per_segment={
                    k: {
                        "wr": round(v["wins"] / v["total"], 3) if v["total"] else 0,
                        "pnl": round(v["pnl"], 4),
                        "n": v["total"],
                    }
                    for k, v in asset_stats.items()
                },
            )

            # Bedrock strategy review — actionable parameter suggestion
            try:
                from polybot.strategy.bedrock_signal import _get_client
                client = _get_client()
                if client:
                    import json
                    segment_summary = "\n".join(
                        f"  {k}: {v['wins']}/{v['total']} wins ({v['wins']/v['total']*100:.0f}% WR), P&L ${v['pnl']:.2f}"
                        for k, v in asset_stats.items() if v["total"] > 0
                    )
                    ai_section = ""
                    if ai_wr is not None and bayes_wr is not None:
                        ai_section = (
                            f"\nAI CONTRIBUTION:\n"
                            f"  AI-assisted trades: {len(ai_trades)}/{total} | WR: {ai_wr:.1%}\n"
                            f"  Bayesian-only trades: {len(bayesian_only)}/{total} | WR: {bayes_wr:.1%}\n"
                        )
                    prompt = (
                        f"You are analyzing a systematic trading strategy on Polymarket 5/15-min markets.\n\n"
                        f"LAST HOUR PERFORMANCE:\n"
                        f"  Trades: {total} | Wins: {wins} | Win rate: {win_rate:.1%}\n"
                        f"  P&L: ${total_pnl:.4f} | Avg EV: {avg_ev:.1%}\n"
                        f"  Bankroll: ${self.risk.bankroll:.2f}\n\n"
                        f"BY SEGMENT:\n{segment_summary}\n"
                        f"{ai_section}\n"
                        f"CURRENT PARAMETERS:\n"
                        f"  min_move: 0.02% | max_ask: 0.60 | lgbm_threshold: 0.55\n"
                        f"  max_market_price: 0.75 | min_ev: 0.06 | obi_spread: 0.15\n\n"
                        f"Provide: (1) one specific parameter change to test next hour, "
                        f"(2) whether AI is adding value vs Bayesian-only, "
                        f"(3) one signal pattern to investigate. Be specific and concise."
                    )
                    body = json.dumps({
                        "anthropic_version": "bedrock-2023-05-31",
                        "max_tokens": 300,
                        "messages": [{"role": "user", "content": prompt}],
                    })
                    resp = client.invoke_model(
                        modelId="anthropic.claude-sonnet-4-6-20251001-v1:0",
                        body=body,
                    )
                    result = json.loads(resp["body"].read())
                    commentary = result["content"][0]["text"].strip()
                    logger.info("strategy_review_ai_commentary", commentary=commentary)
            except Exception as e:
                logger.debug("strategy_review_bedrock_failed", error=str(e))

        except Exception as e:
            logger.warning("strategy_review_failed", error=str(e))

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
