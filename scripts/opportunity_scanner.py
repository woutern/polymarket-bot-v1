"""Opportunity scanner — pull markets from Gamma API and print filtered list.

Uses the events endpoint (has categories via tags) and flattens markets.
Filters:
  - YES ask $0.75–$0.95
  - Market volume > $5,000
  - Resolves within 7 days
  - Tag: Sports, Politics, Finance, Crypto (matched from event tags)

Usage:
    uv run python scripts/opportunity_scanner.py
"""

import asyncio
import json
from datetime import datetime, timezone, timedelta

import httpx

GAMMA_URL = "https://gamma-api.polymarket.com"
ALLOWED_TAGS = {"Sports", "Politics", "Finance", "Crypto", "Basketball", "Soccer",
                "NBA", "NFL", "NCAA", "Baseball", "Hockey", "Tennis",
                "Esports", "Economics", "Fed", "Interest Rates"}
# Map specific tags to broader categories for display
TAG_TO_CATEGORY = {
    "Sports": "Sports", "Basketball": "Sports", "Soccer": "Sports",
    "NBA": "Sports", "NFL": "Sports", "NCAA": "Sports", "Baseball": "Sports",
    "Hockey": "Sports", "Tennis": "Sports", "Esports": "Sports",
    "Politics": "Politics", "Elections": "Politics",
    "Finance": "Finance", "Economics": "Finance", "Fed": "Finance",
    "Interest Rates": "Finance",
    "Crypto": "Crypto", "Bitcoin": "Crypto", "Ethereum": "Crypto",
}
MIN_YES_PRICE = 0.75
MAX_YES_PRICE = 0.95
MIN_VOLUME = 5000


async def fetch_events(end_date_max: str, end_date_min: str) -> list[dict]:
    """Fetch active events from Gamma API."""
    async with httpx.AsyncClient(timeout=30) as client:
        all_events = []
        offset = 0
        while offset < 1000:
            resp = await client.get(
                f"{GAMMA_URL}/events",
                params={
                    "active": "true",
                    "end_date_max": end_date_max,
                    "end_date_min": end_date_min,
                    "order": "volume",
                    "ascending": "false",
                    "limit": 50,
                    "offset": offset,
                },
            )
            if resp.status_code != 200:
                print(f"API error: {resp.status_code}")
                break
            batch = resp.json()
            if not batch:
                break
            all_events.extend(batch)
            if len(batch) < 50:
                break
            offset += 50
    return all_events


def get_event_category(event: dict) -> str | None:
    """Extract category from event tags."""
    tags = event.get("tags") or []
    if isinstance(tags, str):
        try:
            tags = json.loads(tags)
        except Exception:
            return None

    for tag in tags:
        label = tag.get("label", "") if isinstance(tag, dict) else str(tag)
        if label in TAG_TO_CATEGORY:
            return TAG_TO_CATEGORY[label]
    return None


def parse_prices(raw) -> tuple[float, float]:
    """Parse outcomePrices → (yes_price, no_price)."""
    if not raw:
        return 0.0, 0.0
    try:
        prices = json.loads(raw) if isinstance(raw, str) else raw
        if len(prices) >= 2:
            return float(prices[0]), float(prices[1])
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return 0.0, 0.0


def flatten_and_filter(events: list[dict]) -> list[dict]:
    """Flatten events→markets and apply filters."""
    results = []
    now = datetime.now(timezone.utc)

    for event in events:
        category = get_event_category(event)
        if not category:
            continue

        event_title = event.get("title", "?")

        for m in event.get("markets", []):
            # Volume filter
            volume = float(m.get("volume") or 0)
            if volume < MIN_VOLUME:
                continue

            # Price filter — either YES or NO in range (NO in range = betting against)
            yes_price, no_price = parse_prices(m.get("outcomePrices"))
            yes_in_range = MIN_YES_PRICE <= yes_price <= MAX_YES_PRICE
            no_in_range = MIN_YES_PRICE <= no_price <= MAX_YES_PRICE
            if not yes_in_range and not no_in_range:
                continue
            # Pick the side that's in range; if both, pick the higher one
            if yes_in_range and no_in_range:
                side = "YES" if yes_price >= no_price else "NO"
                best_price = max(yes_price, no_price)
            elif yes_in_range:
                side = "YES"
                best_price = yes_price
            else:
                side = "NO"
                best_price = no_price

            # End date
            end_str = m.get("endDate") or event.get("endDate") or ""
            if not end_str:
                continue
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
            except Exception:
                continue

            hours_left = (end_dt - now).total_seconds() / 3600
            if hours_left < 0:
                continue

            question = m.get("question") or m.get("groupItemTitle") or event_title
            slug = m.get("slug", "")

            results.append({
                "question": question,
                "event": event_title,
                "category": category,
                "side": side,
                "price": best_price,
                "yes_price": yes_price,
                "no_price": no_price,
                "end_date": end_dt,
                "hours_left": hours_left,
                "volume": volume,
                "slug": slug,
            })

    results.sort(key=lambda x: x["volume"], reverse=True)
    return results


def format_time_left(hours: float) -> str:
    if hours < 1:
        return f"{max(1, int(hours * 60))}m"
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def print_table(title: str, markets: list[dict]):
    print(f"\n{'═' * 105}")
    print(f"  {title}")
    print(f"{'═' * 105}")

    if not markets:
        print("  No markets match filters.")
        return

    print(f"  {'Question':<48} {'Cat':<8} {'Side':>4} {'Price':>6} {'YES':>5} {'NO':>5} {'In':>7} {'Volume':>12}")
    print(f"  {'─' * 100}")

    for m in markets:
        q = m["question"]
        if len(q) > 47:
            q = q[:44] + "..."
        resolve = format_time_left(m["hours_left"])
        vol = f"${m['volume']:,.0f}"
        print(f"  {q:<48} {m['category']:<8} {m['side']:>4} ${m['price']:.2f} ${m['yes_price']:.2f} ${m['no_price']:.2f} {resolve:>7} {vol:>12}")

    print(f"\n  Total: {len(markets)} markets")

    from collections import Counter
    cats = Counter(m["category"] for m in markets)
    print(f"  By category: {dict(cats)}")


async def main():
    now = datetime.now(timezone.utc)
    end_min = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    end_7d = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("Fetching events from Gamma API...")
    events = await fetch_events(end_7d, end_min)
    print(f"Raw events: {len(events)}")

    total_markets = sum(len(e.get("markets", [])) for e in events)
    print(f"Total markets across events: {total_markets}")

    filtered = flatten_and_filter(events)

    urgent = [m for m in filtered if m["hours_left"] <= 24]
    later = [m for m in filtered if m["hours_left"] > 24]

    print_table(
        f"RESOLVING IN < 24 HOURS — YES $0.75–$0.95, Vol > $5K ({len(urgent)} markets)",
        urgent,
    )
    print_table(
        f"RESOLVING IN 1–7 DAYS — YES $0.75–$0.95, Vol > $5K ({len(later)} markets)",
        later,
    )


if __name__ == "__main__":
    asyncio.run(main())
