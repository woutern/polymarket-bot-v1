#!/usr/bin/env python3
"""V3 Paper Trading Runner — BTC_15M paper mode with per-window learning.

Signals:
  - Polymarket orderbook WebSocket  → YES/NO bid/ask every tick
  - Coinbase WebSocket              → BTC price → 14 LightGBM features → prob_up
  - LightGBM model loaded from S3   → drives dump/neutral decision

What this does each window:
  1. Discovers BTC_15M window boundaries (same timing as live bot)
  2. Runs V3SimpleStrategy: open T+5s, rebalance T+270/450/630, commit T+750s
  3. After window: compute P&L from Coinbase direction, log everything
  4. Runs V3Learner after every N windows to auto-tune dump_threshold + rebalance_usd
  5. Repeats indefinitely — Ctrl+C or kill_switch to stop

Target: 60-70% win rate over the day. Learner iterates params until we get there.

Usage (ECS):
  PYTHONPATH=/app/src .venv/bin/python scripts/run_v3_paper.py

Usage (local):
  uv run python scripts/run_v3_paper.py [--budget 40]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import structlog

_WINDOW_SECONDS = 900  # BTC_15M = 15-minute windows
from polybot.core.controls import BotControls, InMemoryControls
from polybot.core.runner import WindowRunner, make_window_id
from polybot.feeds.coinbase_ws import CoinbaseWS
from polybot.market.market_resolver import resolve_window
from polybot.models import Window
from polybot.storage.mm_store import InMemoryMMStore
from polybot.strategy.v3_learner import V3Learner, V3WindowRecord
from polybot.strategy.v3_simple import V3Params, V3SimpleStrategy

# ── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging() -> None:
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.dev.ConsoleRenderer(colors=True),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("polybot.feeds").setLevel(logging.WARNING)
    logging.getLogger("polybot.core.runner").setLevel(logging.WARNING)
    logging.getLogger("polybot.core.engine").setLevel(logging.WARNING)

logger = logging.getLogger("v3_paper")

_MIN_SECONDS_TO_START = 120   # skip window if fewer seconds remain (15m needs more runway)

# ── Model loading ─────────────────────────────────────────────────────────────

def _load_model_server():
    try:
        from polybot.ml.server import ModelServer
        server = ModelServer()
        server.load_models()
        loaded = list(server._models.keys()) if hasattr(server, "_models") else []
        logger.info("model_loaded pairs=%s", loaded or "none")
        return server if loaded else None
    except Exception as exc:
        logger.warning("model_load_failed: %s — prob_up will be 0.50", str(exc)[:80])
        return None

# ── P&L computation ───────────────────────────────────────────────────────────

def _compute_pnl(result, went_up: bool | None) -> float:
    if went_up is None:
        return 0.0
    return (result.up_shares * 1.0 - result.net_cost) if went_up else (result.down_shares * 1.0 - result.net_cost)

# ── DynamoDB logging ──────────────────────────────────────────────────────────

_DDB_TABLE = "polymarket-bot-v2-windows"

def _ddb_log(item: dict) -> None:
    try:
        import boto3
        profile = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None
        session = boto3.Session(profile_name=profile, region_name="eu-west-1")
        session.resource("dynamodb").Table(_DDB_TABLE).put_item(Item=item)
    except Exception as exc:
        logger.debug("ddb_write_failed: %s", str(exc)[:80])

# ── Main runner ───────────────────────────────────────────────────────────────

class V3PaperRunner:
    """Standalone V3 paper trading loop. No monkey-patching, no MMLoop dependency."""

    def __init__(self, budget: float = 80.0, model_server=None, controls=None):
        self.params = V3Params(budget=budget)
        self.strategy = V3SimpleStrategy(self.params)
        self.learner = V3Learner(self.params)
        self.model_server = model_server
        self.controls = controls or InMemoryControls()
        self.store = InMemoryMMStore()
        self._stop = False
        self._window_count = 0
        self._prev_window = None
        from collections import deque
        self._vol_history = deque(maxlen=20)

    def stop(self) -> None:
        self._stop = True

    async def run(self) -> None:
        logger.info("=" * 62)
        logger.info("V3 Paper Trader — BTC_15M  budget=$%.0f/window", self.params.budget)
        logger.info("Signals: Polymarket orderbook WS + Coinbase WS + LightGBM")
        logger.info("Model: %s", "LightGBM loaded" if self.model_server else "0.50 fallback (no model)")
        logger.info("Dump threshold: prob > %.2f UP  /  < %.2f DOWN",
                    self.params.dump_threshold_up, self.params.dump_threshold_down)
        logger.info("Rebalance moments: T+%s seconds", "/".join(str(t) for t in self.params.rebalance_times))
        logger.info("=" * 62)

        coinbase_ws = CoinbaseWS(assets=["BTC"])
        coinbase_task = asyncio.create_task(coinbase_ws.connect())
        await asyncio.sleep(1.5)   # let first prices arrive

        try:
            await self._loop(coinbase_ws)
        finally:
            coinbase_task.cancel()
            try:
                await coinbase_task
            except asyncio.CancelledError:
                pass
            await coinbase_ws.close()

        stats = self.learner.full_stats()
        logger.info("=" * 62)
        logger.info("SESSION DONE: %s", stats)

    async def _loop(self, coinbase_ws: CoinbaseWS) -> None:
        from polybot.config import Settings
        settings = Settings()

        while not self._stop:
            if self.controls.kill_switch:
                logger.warning("v3_kill_switch — stopping")
                break
            if self.controls.pause_new_windows:
                await asyncio.sleep(10)
                continue

            # Window timing (15-minute windows)
            now = int(time.time())
            window_open_ts = now - (now % _WINDOW_SECONDS)
            window_close_ts = window_open_ts + _WINDOW_SECONDS
            seconds_left = window_close_ts - time.time()

            if seconds_left < _MIN_SECONDS_TO_START:
                wait = seconds_left + 1.0
                logger.info("v3_window_skip seconds_left=%.1f waiting=%.1fs", seconds_left, wait)
                await asyncio.sleep(wait)
                continue

            # Resolve Polymarket token IDs for this window
            window = Window(
                open_ts=window_open_ts,
                close_ts=window_close_ts,
                asset="BTC",
                slug=Window.slug_for_ts(window_open_ts, "BTC", _WINDOW_SECONDS),
            )
            try:
                window = await resolve_window(window)
            except Exception as exc:
                logger.error("v3_resolve_failed: %s", str(exc)[:80])
                await asyncio.sleep(5)
                continue

            if not window.yes_token_id or not window.no_token_id:
                logger.warning("v3_no_tokens slug=%s — skipping", window.slug)
                await asyncio.sleep(seconds_left + 1.0)
                continue

            # Capture open price before window starts
            open_price = coinbase_ws.get_price("BTC")

            # Reset strategy for new window
            self.strategy.reset()

            # Build and run window
            window_id = make_window_id("BTC_15M", ts=window_open_ts)
            runner = WindowRunner(
                pair="BTC_15M",
                yes_token_id=window.yes_token_id,
                no_token_id=window.no_token_id,
                window_id=window_id,
                window_open_ts=float(window_open_ts),
                settings=settings,
                mode="paper",
                model_server=self.model_server,
                controls=self.controls,
                store=self.store,
                prev_window=self._prev_window,
                vol_history=self._vol_history,
                coinbase_ws=coinbase_ws,
                strategy_override=self.strategy,
            )

            logger.info("v3_window_start #%d  slug=%s  seconds_left=%.0f",
                        self._window_count + 1, window.slug, seconds_left)

            await runner.run()
            self._window_count += 1

            if runner.prev_window is not None:
                self._prev_window = runner.prev_window

            # Capture close price + compute P&L
            close_price = coinbase_ws.get_price("BTC")
            went_up = (close_price >= open_price) if (open_price > 0 and close_price > 0) else None
            result = runner.result()

            if result:
                pnl = _compute_pnl(result, went_up)
                self._log_window(result, went_up, pnl, open_price, close_price, window_id)

                rec = V3WindowRecord(
                    window_id=window_id,
                    went_up=went_up,
                    net_cost=result.net_cost,
                    up_shares=result.up_shares,
                    down_shares=result.down_shares,
                    combined_avg=result.combined_avg,
                    rebalance_log=self.strategy.rebalance_log(),
                    pnl=pnl,
                )
                self.learner.record(rec)
                summary = self.learner.maybe_tune()
                if summary:
                    self._log_batch_summary(summary)

            # Heartbeat for ECS health check
            try:
                Path("/tmp/heartbeat").write_text(str(time.time()))
            except OSError:
                pass

            await asyncio.sleep(0.5)

    def _generate_analysis(self, result, went_up, pnl, rb_log, btc_move_pct) -> str:
        """Generate a plain-English analysis of what happened and why."""
        notes = []
        decisions = [rb["decision"] for rb in rb_log]
        dumps = [d for d in decisions if d.startswith("dump")]
        neutrals = [d for d in decisions if d.startswith("neutral")]
        probs = [rb["prob_up"] for rb in rb_log]
        avg_prob = sum(probs) / len(probs) if probs else 0.5
        up_sh, dn_sh = result.up_shares, result.down_shares
        net_cost = result.net_cost

        # Entry analysis
        if up_sh == 0 or dn_sh == 0:
            notes.append("One-sided position — open was skipped (spread too wide at T+0) so rebalances built only one side.")
        elif result.combined_avg > 0.95:
            notes.append(f"Dual position opened but avg={result.combined_avg:.3f} is near $1 — limited GP margin.")
        elif result.is_guaranteed_profit:
            notes.append(f"Guaranteed-profit position: avg={result.combined_avg:.3f} < $1.00 → guaranteed to win at resolution.")

        # Signal analysis
        if not dumps:
            if avg_prob > 0.55:
                notes.append(f"Signal was weakly bullish (avg prob={avg_prob:.2f}) but never crossed dump threshold {self.params.dump_threshold_up:.2f} — stayed neutral.")
            elif avg_prob < 0.45:
                notes.append(f"Signal was weakly bearish (avg prob={avg_prob:.2f}) but never crossed dump threshold {self.params.dump_threshold_down:.2f} — stayed neutral.")
            else:
                notes.append(f"Flat signal all window (avg prob={avg_prob:.2f}) — model sees no strong directional edge.")
        else:
            dump_types = ", ".join(dumps)
            notes.append(f"Signal triggered dump: {dump_types} — concentrated position.")

        # Outcome analysis
        if went_up is not None:
            move_str = f"BTC moved {'UP' if went_up else 'DOWN'} {abs(btc_move_pct):.2f}%"
            if pnl > 0:
                notes.append(f"{move_str} → WIN. Strategy correctly positioned.")
            elif result.is_guaranteed_profit and pnl < 0:
                # GP position that still lost — means one side massively outweighs
                notes.append(f"{move_str} → LOSS despite GP avg. Imbalanced sides ({up_sh}↑ {dn_sh}↓) mean excess shares on losing side dragged P&L negative.")
            elif not result.is_guaranteed_profit and pnl < 0:
                if up_sh == 0 or dn_sh == 0:
                    notes.append(f"{move_str} → LOSS. One-sided bet went wrong. Need dual open to hedge.")
                else:
                    notes.append(f"{move_str} → LOSS. avg={result.combined_avg:.3f} ≥ $1.00 means no GP safety net.")

        # What to change
        if up_sh == 0 or dn_sh == 0:
            notes.append("FIX NEEDED: Open is still blocked by wide spread. If this persists, raise entry_gate further or lower open_shares_per_side to reduce cost.")
        if net_cost < 20 and not (up_sh == 0 or dn_sh == 0):
            notes.append(f"Under-deployed: only ${net_cost:.0f} of ${self.params.budget:.0f} budget used. Increase rebalance_usd.")
        if result.is_guaranteed_profit and pnl > 0:
            notes.append("GP + WIN: ideal outcome. Params working. Keep current thresholds.")
        if not dumps and avg_prob > 0.58:
            notes.append(f"Prob consistently above 0.58 but dump threshold is {self.params.dump_threshold_up:.2f} — consider lowering threshold to ~0.62 if accuracy data supports it.")

        return " | ".join(notes)

    def _log_window(self, result, went_up, pnl, open_price, close_price, window_id):
        n = self._window_count
        rb_log = self.strategy.rebalance_log()
        decisions = [rb["decision"] for rb in rb_log if rb["decision"] != "none"]
        decision_str = " + ".join(decisions) if decisions else "NEUTRAL"
        prob_str = "  ".join(f"T+{rb['moment']}:{rb['prob_up']:.2f}" for rb in rb_log)
        direction_str = ("↑UP" if went_up else "↓DN") if went_up is not None else "?"
        pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        win_str = "WIN ✓" if pnl > 0 else ("LOSS ✗" if pnl < 0 else "BREAK")
        gp = "GP✓" if result.is_guaranteed_profit else "no-GP"
        btc_move_pct = ((close_price - open_price) / open_price * 100) if open_price > 0 else 0

        logger.info(
            "[W#%d] %-30s  %s  dir=%s  %s  net=$%.2f  %d↑ %d↓  avg=%.3f  %s",
            n, decision_str, prob_str, direction_str,
            win_str, result.net_cost,
            result.up_shares, result.down_shares, result.combined_avg, gp,
        )
        logger.info("  P&L: %s  BTC: $%.0f → $%.0f (%+.2f%%)",
                    pnl_str, open_price, close_price, btc_move_pct)

        stats = self.learner.full_stats()
        if stats:
            logger.info(
                "  Running: %d/%d wins (%.0f%%)  total_pnl=$%.2f  "
                "threshold=%.2f/%.2f  rebal_usd=$%.1f",
                stats["wins"], stats["total_windows"], stats["win_rate"] * 100,
                stats["total_pnl"],
                stats["current_dump_threshold_up"],
                stats["current_dump_threshold_down"],
                stats["current_rebalance_usd"],
            )

        # Compute derived metrics for dashboard
        dump_count = sum(1 for d in decisions if d.startswith("dump"))
        neutral_count = sum(1 for d in decisions if d.startswith("neutral"))
        probs = [rb["prob_up"] for rb in rb_log]
        rebalance_detail = [
            {"moment": rb["moment"], "prob": round(rb["prob_up"], 3), "decision": rb["decision"]}
            for rb in rb_log
        ]
        analysis = self._generate_analysis(result, went_up, pnl, rb_log, btc_move_pct)

        _ddb_log({
            "window_slug": window_id,
            "window_id": window_id,
            "strategy": "v3_paper",
            "timestamp": str(time.time()),
            "pair": "BTC_15M",
            "window_number": n,
            # Decision summary
            "decisions": decision_str,
            "rebalance_count": len(rb_log),
            "dump_count": dump_count,
            "neutral_count": neutral_count,
            "rebalance_detail": str(rebalance_detail),
            # Direction + P&L
            "went_up": str(went_up),
            "pnl": str(round(pnl, 4)),
            "outcome": "WIN" if pnl > 0 else ("LOSS" if pnl < 0 else "BREAKEVEN"),
            # Position
            "net_cost": str(round(result.net_cost, 4)),
            "budget_pct": str(round(result.net_cost / self.params.budget * 100, 1)),
            "up_shares": result.up_shares,
            "down_shares": result.down_shares,
            "combined_avg": str(round(result.combined_avg, 4)),
            "is_guaranteed_profit": result.is_guaranteed_profit,
            # BTC price
            "open_price": str(round(open_price, 2)),
            "close_price": str(round(close_price, 2)),
            "btc_move_pct": str(round(btc_move_pct, 3)),
            # Model signal
            "avg_prob_up": str(round(sum(probs) / len(probs), 3) if probs else "0.5"),
            "min_prob": str(round(min(probs), 3) if probs else "0.5"),
            "max_prob": str(round(max(probs), 3) if probs else "0.5"),
            # Learner params at time of window
            "dump_threshold_up": str(self.params.dump_threshold_up),
            "dump_threshold_down": str(self.params.dump_threshold_down),
            "rebalance_usd": str(self.params.rebalance_usd),
            "entry_gate": str(self.params.entry_gate),
            # Analysis
            "analysis": analysis,
        })

    def _log_batch_summary(self, summary: dict) -> None:
        logger.info("─" * 62)
        logger.info("BATCH #%d (%d windows) — TUNING PARAMS", summary["batch"], summary["windows"])
        logger.info("  Win rate:      %.1f%%  (target: 60-70%%)", summary["win_rate"] * 100)
        logger.info("  Total P&L:     $%.2f", summary["total_pnl"])
        if summary["dump_accuracy"] is not None:
            logger.info("  Dump accuracy: %.1f%%  (%d✓  %d✗)",
                        summary["dump_accuracy"] * 100,
                        summary["dump_correct"], summary["dump_wrong"])
        logger.info("  Dump rate:     %.1f%%", summary["dump_rate"] * 100)
        logger.info("  Avg combo_avg: %.3f", summary["avg_combined_avg"])
        logger.info("  → NEW params:  dump=%.2f/%.2f  rebal_usd=$%.1f",
                    self.params.dump_threshold_up, self.params.dump_threshold_down,
                    self.params.rebalance_usd)
        logger.info("─" * 62)

# ── Entry point ───────────────────────────────────────────────────────────────

def _parse_args():
    p = argparse.ArgumentParser(description="V3 BTC paper trader with learning loop")
    p.add_argument("--budget", type=float, default=80.0)
    return p.parse_args()


async def _main() -> None:
    _setup_logging()
    args = _parse_args()

    model_server = _load_model_server()

    # Use DynamoDB-backed controls in ECS (kill switch / pause work immediately)
    try:
        controls = BotControls()
        logger.info("controls: DynamoDB-backed (kill switch active)")
    except Exception:
        controls = InMemoryControls()
        logger.info("controls: in-memory (DynamoDB unavailable)")

    runner = V3PaperRunner(
        budget=args.budget,
        model_server=model_server,
        controls=controls,
    )

    def _sighandler(*_):
        logger.info("signal received — stopping after current window")
        runner.stop()

    signal.signal(signal.SIGINT, _sighandler)
    signal.signal(signal.SIGTERM, _sighandler)

    await runner.run()


if __name__ == "__main__":
    asyncio.run(_main())
