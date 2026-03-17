"""Coinbase REST API for historical candle data."""

from __future__ import annotations

import asyncio
import time

import httpx

# Public endpoint — no auth needed
CANDLES_URL = "https://api.coinbase.com/api/v3/brokerage/market/products/BTC-USD/candles"

# Coinbase granularity options
GRANULARITY_1MIN = "ONE_MINUTE"
GRANULARITY_5MIN = "FIVE_MINUTE"


async def get_candles(
    start: int,
    end: int,
    granularity: str = GRANULARITY_1MIN,
    client: httpx.AsyncClient | None = None,
) -> list[dict]:
    """Fetch candles from Coinbase. Max 300 per request.

    Returns list of dicts with keys: start, low, high, open, close, volume.
    Sorted ascending by time.
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=30)

    try:
        resp = await client.get(
            CANDLES_URL,
            params={
                "start": str(start),
                "end": str(end),
                "granularity": granularity,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        candles = data.get("candles", [])
        # Convert string fields to float, sort ascending
        result = []
        for c in candles:
            result.append(
                {
                    "start": int(c["start"]),
                    "low": float(c["low"]),
                    "high": float(c["high"]),
                    "open": float(c["open"]),
                    "close": float(c["close"]),
                    "volume": float(c["volume"]),
                }
            )
        result.sort(key=lambda x: x["start"])
        return result
    finally:
        if own_client:
            await client.aclose()


async def get_candles_paginated(
    start: int,
    end: int,
    granularity: str = GRANULARITY_1MIN,
    max_per_request: int = 300,
) -> list[dict]:
    """Paginate through Coinbase candles API (max 300 per request).

    For 1-min candles: 300 candles = 5 hours per request.
    """
    interval_seconds = 60 if granularity == GRANULARITY_1MIN else 300
    chunk_seconds = max_per_request * interval_seconds

    all_candles: list[dict] = []
    async with httpx.AsyncClient(timeout=30) as client:
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + chunk_seconds, end)
            candles = await get_candles(cursor, chunk_end, granularity, client)
            all_candles.extend(candles)
            cursor = chunk_end
            # Be nice to the API
            await asyncio.sleep(0.2)

    # Deduplicate by start timestamp
    seen = set()
    deduped = []
    for c in all_candles:
        if c["start"] not in seen:
            seen.add(c["start"])
            deduped.append(c)
    deduped.sort(key=lambda x: x["start"])
    return deduped
