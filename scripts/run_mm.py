#!/usr/bin/env python3
"""Local entry point for the MarketMaker bot (paper or live).

Paper mode (default): real Polymarket orderbook + real Coinbase price feeds,
but all orders are simulated in-memory. No real money at risk.

Usage:
    # Paper mode — watch real markets, simulate orders:
    uv run python scripts/run_mm.py

    # Paper mode with model predictions (needs AWS access):
    uv run python scripts/run_mm.py --model

    # Live mode — REAL MONEY. Only after paper mode confirms sane:
    uv run python scripts/run_mm.py --live --budget 20

    # Stop after N windows (useful for tests):
    uv run python scripts/run_mm.py --windows 2
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

# Add src/ to path so we can import polybot without installing
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import structlog

from polybot.config import Settings
from polybot.core.mm_loop import MMLoop


def _configure_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
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
        level=level,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Silence noisy libraries
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("botocore").setLevel(logging.WARNING)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MarketMaker bot — paper or live")
    p.add_argument("--pair", default="BTC_5M", help="Pair to trade (default: BTC_5M)")
    p.add_argument("--live", action="store_true", help="Enable live trading (real money)")
    p.add_argument("--budget", type=float, default=20.0, help="USD budget per window (default: 20)")
    p.add_argument("--model", action="store_true", help="Load LightGBM model from S3 (needs AWS)")
    p.add_argument("--windows", type=int, default=None, help="Stop after N windows (default: forever)")
    p.add_argument("--verbose", action="store_true", help="Debug logging")
    p.add_argument("--yes", action="store_true", help="Skip live trading confirmation (for container/CI use)")
    return p.parse_args()


def _load_model_server() -> object:
    """Try to load ModelServer from S3 via SSM. Returns None on failure."""
    try:
        from polybot.ml.server import ModelServer
        server = ModelServer()
        server.load_models()
        loaded = [p for p in server._models]
        if loaded:
            print(f"[model] Loaded: {loaded}")
        else:
            print("[model] No models loaded — falling back to 0.50")
        return server
    except Exception as exc:
        print(f"[model] Failed to load — falling back to 0.50: {exc}")
        return None


async def _main() -> None:
    args = _parse_args()
    _configure_logging(verbose=args.verbose)

    mode = "live" if args.live else "paper"

    if mode == "live" and not args.yes:
        print()
        print("=" * 55)
        print("  WARNING: LIVE MODE — REAL MONEY AT RISK")
        print(f"  Pair: {args.pair}  Budget: ${args.budget}/window")
        print("=" * 55)
        confirm = input("  Type 'yes' to confirm: ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            return
        print()

    model_server = _load_model_server() if args.model else None

    # Live mode uses DynamoDB-backed controls (kill_switch + pause_new_windows).
    # Paper mode uses in-memory controls (no AWS needed).
    controls = None
    if mode == "live":
        try:
            from polybot.core.controls import BotControls
            controls = BotControls()
            print(f"  Controls: DynamoDB (polymarket-bot-controls)")
        except Exception as exc:
            print(f"  Controls: DynamoDB unavailable ({exc!s:.60}) — using in-memory")

    print()
    print("=" * 55)
    print(f"  MarketMaker Bot — {mode.upper()}")
    print(f"  Pair:    {args.pair}")
    print(f"  Budget:  ${args.budget}/window")
    print(f"  Model:   {'live LightGBM' if model_server else '0.50 fallback'}")
    print(f"  Windows: {args.windows or 'unlimited'}")
    print("=" * 55)
    print("  Ctrl+C to stop after current window")
    print()

    settings = Settings()

    loop_obj = MMLoop(
        pair=args.pair,
        mode=mode,
        budget_override=args.budget,
        model_server=model_server,
        max_windows=args.windows,
        controls=controls,
        settings=settings,
    )

    # Handle Ctrl+C gracefully — stop after current window
    loop = asyncio.get_event_loop()

    def _sigint_handler(*_):
        print("\n[signal] Stopping after current window...")
        loop_obj.stop()

    signal.signal(signal.SIGINT, _sigint_handler)
    signal.signal(signal.SIGTERM, _sigint_handler)

    results = await loop_obj.run()

    # Summary
    print()
    print("=" * 55)
    print(f"  Session Summary — {len(results)} window(s)")
    print("=" * 55)
    total_cost = 0.0
    total_floor = 0
    for i, r in enumerate(results, 1):
        flag = "PROFITABLE" if r.is_guaranteed_profit else "not profitable"
        print(
            f"  [{i}] up={r.up_shares:3d}  dn={r.down_shares:3d}  "
            f"cost=${r.net_cost:5.2f}  avg={r.combined_avg:.4f}  "
            f"floor={r.payout_floor:3d}  → {flag}"
        )
        total_cost += r.net_cost
        total_floor += r.payout_floor
    print(f"  Total cost: ${total_cost:.2f}  Total floor: {total_floor}")
    print("=" * 55)


if __name__ == "__main__":
    asyncio.run(_main())
