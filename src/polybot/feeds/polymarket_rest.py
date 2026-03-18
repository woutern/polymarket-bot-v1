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

    Uses outcomes + outcomePrices mapping:
      outcomes: ['Up', 'Down'], outcomePrices: ['1', '0'] → Up won → YES
      outcomes: ['Up', 'Down'], outcomePrices: ['0', '1'] → Down won → NO

    Returns:
        (winner, source) where winner is "YES" | "NO" | None (pending),
        and source is "polymarket_verified" | "pending".
    """
    import json as _json

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
        if not m.get("closed"):
            return None, "pending"

        outcomes = m.get("outcomes", [])
        if isinstance(outcomes, str):
            outcomes = _json.loads(outcomes)
        prices = m.get("outcomePrices", [])
        if isinstance(prices, str):
            prices = _json.loads(prices)

        if len(outcomes) < 2 or len(prices) < 2:
            return None, "pending"

        # Map outcome names to their payout prices
        outcome_map = dict(zip(outcomes, prices))
        up_price = float(outcome_map.get("Up", 0))

        # Only trust conclusive results (0 or 1)
        if up_price >= 0.99:
            return "YES", "polymarket_verified"  # Up won
        elif up_price <= 0.01:
            return "NO", "polymarket_verified"  # Down won
        else:
            # Ambiguous — not yet resolved
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
