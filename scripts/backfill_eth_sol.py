"""Backfill historical ETH and SOL 1-min candles from Coinbase.

Downloads data in chunks (300 candles per request = 5 hours)
and stores as parquet files alongside the existing BTC data.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from polybot.feeds.coinbase_rest import get_candles_paginated

DATA_DIR = Path("data/candles")

ASSETS = {
    "ETH-USD": DATA_DIR / "eth_usd_1min.parquet",
    "SOL-USD": DATA_DIR / "sol_usd_1min.parquet",
}


async def backfill_asset(product_id: str, out_path: Path, days: int):
    """Download `days` of 1-min candles for `product_id`."""
    end = int(time.time())
    start = end - (days * 86400)

    print(f"[{product_id}] Backfilling {days} days: {start} → {end}")

    candles = await get_candles_paginated(start, end, product_id=product_id)
    print(f"[{product_id}] Downloaded {len(candles)} candles")

    if not candles:
        print(f"[{product_id}] No data received — skipping.")
        return

    table = pa.table(
        {
            "start": [c["start"] for c in candles],
            "open": [c["open"] for c in candles],
            "high": [c["high"] for c in candles],
            "low": [c["low"] for c in candles],
            "close": [c["close"] for c in candles],
            "volume": [c["volume"] for c in candles],
        }
    )

    pq.write_table(table, out_path)
    print(f"[{product_id}] Saved to {out_path} ({len(candles)} rows)")


async def backfill(days: int = 90):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for product_id, out_path in ASSETS.items():
        await backfill_asset(product_id, out_path, days)


if __name__ == "__main__":
    import sys

    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    asyncio.run(backfill(days))
