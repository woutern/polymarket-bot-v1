"""Backfill historical Polymarket 5-min BTC market data."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from polybot.feeds.polymarket_rest import search_markets

DATA_DIR = Path("data/markets")


async def backfill():
    """Search and download available 5-min BTC market metadata."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    print("Searching for BTC 5-min markets on Polymarket...")
    markets = await search_markets(query="btc-updown-5m", active=False, limit=100)
    print(f"Found {len(markets)} markets")

    # Save as JSON
    out_path = DATA_DIR / "btc_5min_markets.json"
    data = []
    for m in markets:
        data.append(
            {
                "condition_id": m.condition_id,
                "question": m.question,
                "slug": m.slug,
                "yes_token_id": m.yes_token_id,
                "no_token_id": m.no_token_id,
                "end_date": m.end_date,
                "active": m.active,
            }
        )

    out_path.write_text(json.dumps(data, indent=2))
    print(f"Saved to {out_path}")


if __name__ == "__main__":
    asyncio.run(backfill())
