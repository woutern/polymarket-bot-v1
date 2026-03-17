"""Backfill historical BTC 1-min candles from Coinbase.

Downloads data in chunks (300 candles per request = 5 hours)
and stores as parquet files.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from polybot.feeds.coinbase_rest import get_candles_paginated

DATA_DIR = Path("data/candles")


async def backfill(days: int = 90):
    """Download `days` of 1-min BTC-USD candles."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    end = int(time.time())
    start = end - (days * 86400)

    print(f"Backfilling {days} days of 1-min candles: {start} → {end}")

    candles = await get_candles_paginated(start, end)
    print(f"Downloaded {len(candles)} candles")

    if not candles:
        print("No data received!")
        return

    # Save as parquet
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

    out_path = DATA_DIR / "btc_usd_1min.parquet"
    pq.write_table(table, out_path)
    print(f"Saved to {out_path} ({len(candles)} rows)")


if __name__ == "__main__":
    import sys

    days = int(sys.argv[1]) if len(sys.argv) > 1 else 90
    asyncio.run(backfill(days))
