"""Resolve market slugs to condition_ids and token_ids via Gamma API."""

from __future__ import annotations

import json

import httpx
import structlog

from polybot.models import MarketInfo, Window, SLUG_PREFIXES

logger = structlog.get_logger()

GAMMA_URL = "https://gamma-api.polymarket.com"


async def resolve_window(window: Window) -> Window:
    """Look up a window's market on Gamma API and populate IDs.

    Searches by the slug pattern btc-updown-5m-{timestamp}.
    """
    slug = window.slug
    if not slug:
        slug = Window.slug_for_ts(window.open_ts)
        window.slug = slug

    async with httpx.AsyncClient(timeout=15) as client:
        # Search for the specific market
        resp = await client.get(
            f"{GAMMA_URL}/events",
            params={"slug": slug, "limit": 1},
        )
        resp.raise_for_status()
        events = resp.json()

    if not events:
        logger.warning("market_not_found", slug=slug)
        return window

    event = events[0]
    markets = event.get("markets", [])
    if not markets:
        logger.warning("market_no_markets", slug=slug)
        return window

    mkt = markets[0]
    window.condition_id = mkt.get("conditionId", "")
    raw = mkt.get("clobTokenIds", [])
    # Gamma sometimes returns token IDs as a JSON-encoded string "[...]"
    if isinstance(raw, str):
        raw = json.loads(raw)
    if len(raw) >= 2:
        window.yes_token_id = raw[0]
        window.no_token_id = raw[1]

    logger.info(
        "market_resolved",
        slug=slug,
        condition_id=window.condition_id,
        yes_token=window.yes_token_id[:8] if window.yes_token_id else "",
    )
    return window
