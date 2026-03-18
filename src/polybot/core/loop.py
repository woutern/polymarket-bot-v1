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


class TradingLoop:
    """Multi-asset, multi-timeframe directional trading bot.

    Single strategy: late-window momentum entry (T-60s to T-15s).
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

        if settings.mode == "live":
            self.trader = LiveTrader(settings=settings, risk=self.risk, db=self.db)
        else:
            self.trader = PaperTrader(risk=self.risk, db=self.db)

        # Load a separate base rate table per asset so volatility profiles match
        base_rates: dict[str, BaseRateTable] = {
            asset: self._load_base_rate_for(asset) for asset in assets
        }

        # One AssetState per enabled pair
        self.asset_states: dict[str, AssetState] = {}
        for asset, dur in enabled:
            tf = "15m" if dur == 900 else "5m"
            key = f"{asset}_{tf}"
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

        self._wallet_address: str = settings.polymarket_funder or ""
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

        # Use per-asset × per-duration threshold (research-calibrated)
        min_move = self.settings.min_move_for(state.asset, state.tracker.window_seconds)

        signal = generate_directional_signal(
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

        # Only execute if orderbook was fetched recently (< 30s old) — stale = don't trade
        orderbook_fresh = (time.time() - state.orderbook_age) < 30.0
        if signal and self.risk.can_trade() and orderbook_fresh:
            await self._execute(signal, state)
            state.traded_this_window = True
        elif signal and not orderbook_fresh:
            logger.warning("signal_skipped_stale_orderbook", asset=state.asset, age=round(time.time() - state.orderbook_age, 1))

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

        # Schedule Polymarket outcome verification 30s after window close
        slug = window.slug
        asyncio.create_task(self._verify_outcome_after_delay(slug, 30), name=f"verify_{slug}")

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

    async def _verify_outcome_after_delay(self, window_slug: str, delay_seconds: int):
        """Wait, then query Polymarket for the authoritative outcome."""
        await asyncio.sleep(delay_seconds)
        if isinstance(self.trader, PaperTrader):
            await self.trader.verify_and_update(window_slug)
        elif isinstance(self.trader, LiveTrader):
            # LiveTrader already uses Gamma API in resolve_window — re-check for accuracy
            try:
                from polybot.feeds.polymarket_rest import get_market_outcome
                from polybot.storage.db import Database
                winner, source = await get_market_outcome(window_slug)
                if winner:
                    trades = await self.db.get_trades(window_slug=window_slug)
                    for t in trades:
                        if t.get("resolved"):
                            correct = (t.get("side", "") == winner)
                            await self.db.update_trade_outcome(
                                trade_id=t["id"],
                                polymarket_winner=winner,
                                correct_prediction=correct,
                                outcome_source=source,
                            )
            except Exception as e:
                logger.warning("live_verify_failed", slug=window_slug, error=str(e))

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
                tf = "15m" if "15m" in slug else "5m"
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
                        f"  BTC min_move: 0.08% | ETH: 0.10% | SOL: 0.14%\n"
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
