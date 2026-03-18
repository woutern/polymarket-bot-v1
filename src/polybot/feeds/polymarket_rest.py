"""Polymarket REST API for market discovery and price history."""

from __future__ import annotations

import httpx
import structlog

from polybot.models import MarketInfo

logger = structlog.get_logger()

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"


async def search_markets(
    query: str = "btc-updown-5m",
    active: bool = True,
    limit: int = 10,
) -> list[MarketInfo]:
    """Search Gamma API for markets by slug pattern."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{GAMMA_URL}/events",
            params={"slug": query, "active": str(active).lower(), "limit": limit},
        )
        resp.raise_for_status()
        events = resp.json()

    markets = []
    for event in events:
        for mkt in event.get("markets", []):
            markets.append(
                MarketInfo(
                    condition_id=mkt.get("conditionId", ""),
                    question=mkt.get("question", ""),
                    slug=mkt.get("slug", ""),
                    yes_token_id=mkt.get("clobTokenIds", ["", ""])[0] if mkt.get("clobTokenIds") else "",
                    no_token_id=mkt.get("clobTokenIds", ["", ""])[1] if len(mkt.get("clobTokenIds", [])) > 1 else "",
                    end_date=mkt.get("endDate", ""),
                    active=mkt.get("active", False),
                )
            )
    return markets


async def get_market_by_condition(condition_id: str) -> dict | None:
    """Get market info from CLOB API by condition_id."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{CLOB_URL}/markets/{condition_id}")
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()


async def get_orderbook(token_id: str) -> dict:
    """Get orderbook for a specific token."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CLOB_URL}/book",
            params={"token_id": token_id},
        )
        resp.raise_for_status()
        return resp.json()


async def get_market_outcome(slug: str) -> tuple[str | None, str]:
    """Query Gamma API for the resolved outcome of a market.

    Returns:
        (winner, source) where winner is "YES" | "NO" | None (pending),
        and source is "polymarket_verified" | "pending".
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{GAMMA_URL}/markets",
            params={"slug": slug},
        )
        if resp.status_code != 200:
            return None, "pending"
        markets = resp.json()
        if not markets:
            return None, "pending"
        m = markets[0]
        prices = m.get("outcomePrices", [])
        # Gamma sometimes returns as JSON string "[\"0.99\",\"0.01\"]"
        if isinstance(prices, str):
            import json as _json
            prices = _json.loads(prices)
        if len(prices) >= 2:
            yes_price = float(prices[0])
            # Conclusive even before "closed" flag — Gamma API can lag
            if yes_price >= 0.99:
                return "YES", "polymarket_verified"
            if yes_price <= 0.01:
                return "NO", "polymarket_verified"
        if m.get("closed") and len(prices) >= 2:
            yes_price = float(prices[0])
            winner = "YES" if yes_price >= 0.5 else "NO"
            return winner, "polymarket_verified"
        return None, "pending"


async def get_prices_history(
    token_id: str,
    interval: str = "1m",
    fidelity: int = 1,
) -> list[dict]:
    """Get price history for a token."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CLOB_URL}/prices-history",
            params={
                "tokenID": token_id,
                "interval": interval,
                "fidelity": fidelity,
            },
        )
        resp.raise_for_status()
        return resp.json().get("history", [])
