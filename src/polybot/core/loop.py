"""Main async event loop — multi-asset, multi-timeframe directional trading.

Strategy: Late-window directional (T-60s to T-15s) across BTC/ETH/SOL
on both 5-minute and 15-minute windows.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from functools import partial

import structlog

from polybot.config import Settings
from polybot.execution.live_trader import LiveTrader
from polybot.execution.paper_trader import PaperTrader
from polybot.feeds.coinbase_ws import CoinbaseWS
from polybot.feeds.polymarket_rest import get_orderbook
from polybot.market.balance_checker import BalanceChecker
from polybot.market.market_resolver import resolve_window
from polybot.market.window_tracker import WindowState, WindowTracker
from polybot.models import Direction, OrderbookSnapshot, Window
from polybot.risk.manager import RiskManager
from polybot.storage.db import Database
from polybot.storage.dynamo import DynamoStore
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

S3_BUCKET = "polymarket-bot-data-688567279867"
S3_KEY = "candles/btc_usd_1min.parquet"
LOCAL_PARQUET = "/tmp/btc_usd_1min.parquet"


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


class TradingLoop:
    """Multi-asset, multi-timeframe directional trading bot.

    Single strategy: late-window momentum entry (T-60s to T-15s).
    Uses Bayesian updater + historical base rates to estimate P(UP).
    Quarter-Kelly sizing capped at $1 per trade.
    """

    @staticmethod
    def _load_base_rates() -> BaseRateTable:
        table = BaseRateTable()
        local_paths = ["data/candles/btc_usd_1min.parquet", LOCAL_PARQUET]
        for path in local_paths:
            if os.path.exists(path):
                table.load_from_parquet(path)
                logger.info("base_rates_loaded", source=path, bins=len(table.bins))
                return table
        try:
            import boto3
            s3 = boto3.client("s3", region_name="eu-west-1")
            s3.download_file(S3_BUCKET, S3_KEY, LOCAL_PARQUET)
            table.load_from_parquet(LOCAL_PARQUET)
            logger.info("base_rates_loaded", source="s3", bins=len(table.bins))
        except Exception as e:
            logger.warning("base_rates_load_failed", error=str(e))
        return table

    def __init__(self, settings: Settings):
        self.settings = settings
        assets = settings.asset_list
        self.coinbase = CoinbaseWS(assets=assets)
        self.risk = RiskManager(
            bankroll=settings.bankroll,
            daily_loss_cap_pct=settings.daily_loss_cap_pct,
            max_position_pct=settings.max_position_pct,
        )
        self.db = Database()
        self.dynamo = DynamoStore()
        self.db.attach_dynamo(self.dynamo)

        if settings.mode == "live":
            self.trader = LiveTrader(settings=settings, risk=self.risk, db=self.db)
        else:
            self.trader = PaperTrader(risk=self.risk, db=self.db)

        base_rates = self._load_base_rates()

        # One AssetState per asset × duration combo
        self.asset_states: dict[str, AssetState] = {}
        for dur in settings.duration_list:
            for asset in assets:
                key = f"{asset}_{dur}" if dur != 300 else asset
                self.asset_states[key] = AssetState(
                    asset=asset,
                    tracker=WindowTracker(
                        entry_seconds=settings.directional_entry_seconds,
                        asset=asset,
                        window_seconds=dur,
                    ),
                    bayesian=BayesianUpdater(base_rates),
                )

        self.balance_checker = BalanceChecker()
        self._wallet_address: str = settings.polymarket_funder or ""
        self._last_balance_check: float = 0.0
        self._last_claim_check: float = 0.0
        self._last_strategy_review: float = 0.0
        self._running = False

    async def start(self):
        logger.info(
            "loop_starting",
            mode=self.settings.mode,
            bankroll=self.settings.bankroll,
            assets=list(self.asset_states.keys()),
        )

        await self.db.connect()

        if self._wallet_address and self.settings.mode == "live":
            try:
                balances = await self.balance_checker.check(self._wallet_address)
                logger.info("wallet_balance", **balances)
                polygon_usdc = balances.get("polygon_usdc", 0.0)
                if polygon_usdc > 0 and abs(polygon_usdc - self.risk.bankroll) > 0.50:
                    self.risk.bankroll = polygon_usdc
                    logger.info("bankroll_updated_from_balance", bankroll=round(polygon_usdc, 2))
            except Exception as e:
                logger.warning("startup_balance_check_failed", error=str(e))
        elif self._wallet_address:
            # Paper mode: check balance for display only, don't override bankroll
            try:
                balances = await self.balance_checker.check(self._wallet_address)
                logger.info("wallet_balance", **balances)
            except Exception as e:
                logger.warning("startup_balance_check_failed", error=str(e))

        self._running = True

        tasks = [
            asyncio.create_task(self.coinbase.connect(), name="coinbase_ws"),
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

        while self._running:
            for key, state in self.asset_states.items():
                price = self.coinbase.get_price(state.asset)
                if price <= 0:
                    continue
                try:
                    await self._tick_asset(state, price)
                except Exception as e:
                    logger.error("tick_asset_error", key=key, error=str(e), exc_info=True)

            await asyncio.sleep(0.25)

            # Periodic balance refresh every 5 minutes
            if self._wallet_address and (time.time() - self._last_balance_check) >= 300:
                self._last_balance_check = time.time()
                try:
                    balances = await self.balance_checker.check(self._wallet_address)
                    logger.info("wallet_balance", **balances)
                    if self.settings.mode == "live":
                        polygon_usdc = balances.get("polygon_usdc", 0.0)
                        if polygon_usdc > 0 and abs(polygon_usdc - self.risk.bankroll) > 0.50:
                            logger.info(
                                "bankroll_updated_from_balance",
                                old=round(self.risk.bankroll, 2),
                                new=round(polygon_usdc, 2),
                            )
                            self.risk.bankroll = polygon_usdc
                except Exception as e:
                    logger.warning("periodic_balance_check_failed", error=str(e))

            # Periodic auto-claim every 10 minutes — live mode only
            if (
                self.settings.mode == "live"
                and self.settings.polymarket_private_key
                and self.settings.polymarket_funder
                and (time.time() - self._last_claim_check) >= 600
            ):
                self._last_claim_check = time.time()
                asyncio.create_task(self._run_claim(), name="auto_claim")
                logger.info("auto_claim_triggered")

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

        current_open_ts = window.open_ts

        if state.prev_open_ts is not None and current_open_ts != state.prev_open_ts:
            await self._on_window_close(state, price)
            await self._on_window_open(state, price)
        elif state.prev_open_ts is None:
            await self._on_window_open(state, price)

        # Directional strategy — entry zone only
        if window_state == WindowState.ENTRY_ZONE and not state.traded_this_window:
            await self._on_entry_zone(state, price)

        state.prev_open_ts = current_open_ts

    async def _on_window_open(self, state: AssetState, price: float):
        window = state.tracker.current
        if not window:
            return
        state.prev_window = window
        state.traded_this_window = False
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

        # Hard cutoff: don't enter after T-15s (fill risk, blockchain latency)
        if remaining < 15:
            return

        pct_move = state.tracker.pct_move(price) or 0.0
        state.bayesian.update(price, remaining)

        await self._refresh_orderbook(state)

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
        )

        signal = generate_directional_signal(
            bayesian=state.bayesian,
            orderbook=state.orderbook,
            current_price=price,
            open_price=window.open_price,
            seconds_remaining=remaining,
            min_move_pct=self.settings.directional_min_move_pct,
            min_ev_threshold=self.settings.min_ev_threshold,
            max_market_price=self.settings.max_market_price,
            window_slug=window.slug,
            asset=state.asset,
        )

        if signal and self.risk.can_trade():
            await self._execute(signal, state)
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

    async def _execute(self, signal, state: AssetState):
        if isinstance(self.trader, LiveTrader):
            window = state.tracker.current
            yes_id = window.yes_token_id if window else ""
            no_id = window.no_token_id if window else ""
            return await self.trader.execute(signal, yes_id, no_id)
        return await self.trader.execute(signal)

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
        """Hourly learning loop: analyse recent trades, log strategy insights.

        Queries DynamoDB for the last 60 minutes of resolved trades and logs:
        - Win rate per asset
        - Average EV vs actual outcome
        - Best/worst performing asset × window combinations
        - Calls Bedrock for strategic commentary (paper/live)
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

            # Per-asset breakdown
            asset_stats: dict[str, dict] = {}
            for t in recent:
                a = str(t.get("asset", "BTC") or "BTC")
                if isinstance(a, dict):
                    a = a.get("S", "BTC")
                if a not in asset_stats:
                    asset_stats[a] = {"wins": 0, "total": 0, "pnl": 0.0}
                asset_stats[a]["total"] += 1
                p = float(t.get("pnl", 0) or 0)
                asset_stats[a]["pnl"] += p
                if p > 0:
                    asset_stats[a]["wins"] += 1

            logger.info(
                "strategy_review",
                period_hours=1,
                trades=total,
                wins=wins,
                win_rate=round(win_rate, 3),
                pnl=round(total_pnl, 4),
                bankroll=round(self.risk.bankroll, 2),
                per_asset={
                    a: {
                        "wr": round(v["wins"] / v["total"], 3) if v["total"] else 0,
                        "pnl": round(v["pnl"], 4),
                        "n": v["total"],
                    }
                    for a, v in asset_stats.items()
                },
            )

            # Call Bedrock for strategy commentary using recent performance
            try:
                from polybot.strategy.bedrock_signal import _get_client
                client = _get_client()
                if client:
                    import json
                    import boto3
                    asset_summary = "; ".join(
                        f"{a}: {v['wins']}/{v['total']} wins, P&L ${v['pnl']:.2f}"
                        for a, v in asset_stats.items()
                    )
                    prompt = (
                        f"You are a systematic trading advisor. Analyse this 1-hour performance for a "
                        f"Polymarket binary prediction bot (5m/15m UP/DOWN markets on BTC/ETH/SOL):\n\n"
                        f"Win rate: {win_rate:.1%} ({wins}/{total} trades)\n"
                        f"P&L: ${total_pnl:.4f}\n"
                        f"Per-asset: {asset_summary}\n"
                        f"Bankroll: ${self.risk.bankroll:.2f}\n\n"
                        f"In 2-3 sentences, what is working and what should change? "
                        f"Be specific about thresholds (EV, price move, entry timing)."
                    )
                    import json as _json
                    body = _json.dumps({
                        "anthropic_version": "bedrock-2023-05-31",
                        "max_tokens": 200,
                        "messages": [{"role": "user", "content": prompt}],
                    })
                    resp = client.invoke_model(
                        modelId="eu.anthropic.claude-sonnet-4-6-20251001-v1:0",
                        body=body,
                    )
                    result = _json.loads(resp["body"].read())
                    commentary = result["content"][0]["text"].strip()
                    logger.info("strategy_review_ai_commentary", commentary=commentary)
            except Exception as e:
                logger.debug("strategy_review_bedrock_failed", error=str(e))

        except Exception as e:
            logger.warning("strategy_review_failed", error=str(e))

    async def _refresh_orderbook(self, state: AssetState):
        window = state.tracker.current
        if not window or not window.yes_token_id:
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
            if yes_asks:
                snap.yes_best_ask = min(float(a["price"]) for a in yes_asks)
            if yes_bids:
                snap.yes_best_bid = max(float(b["price"]) for b in yes_bids)
            if no_asks:
                snap.no_best_ask = min(float(a["price"]) for a in no_asks)
            if no_bids:
                snap.no_best_bid = max(float(b["price"]) for b in no_bids)
            state.orderbook = snap
        except Exception as e:
            logger.error("orderbook_refresh_failed", asset=state.asset, error=str(e))
