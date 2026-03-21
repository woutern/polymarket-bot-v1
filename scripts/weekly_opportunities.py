"""Weekly opportunity scanner — markets resolving in 7-14 days.

Non-crypto, non-sports, volume > $5K, ask $0.65-$0.92.
Compact one-line output, grouped by topic.

Usage:
    uv run python scripts/weekly_opportunities.py
"""

import asyncio
import json
from datetime import datetime, timezone, timedelta
from collections import defaultdict

import httpx

GAMMA = "https://gamma-api.polymarket.com"
MIN_VOL = 5_000
MIN_ASK = 0.65
MAX_ASK = 0.92
SKIP_TAGS = {"crypto", "bitcoin", "ethereum", "solana", "xrp", "crypto-prices",
             "sports", "nba", "nfl", "nhl", "soccer", "basketball", "baseball",
             "esports", "tennis", "hockey", "golf", "march-madness", "ncaa"}


async def main():
    now = datetime.now(timezone.utc)
    end_min = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_max = (now + timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%SZ")

    print("Fetching events (7-14 days out)...")
    async with httpx.AsyncClient(timeout=30) as c:
        all_events = []
        offset = 0
        while offset < 1000:
            r = await c.get(f"{GAMMA}/events", params={
                "active": "true", "end_date_min": end_min, "end_date_max": end_max,
                "order": "volume", "ascending": "false", "limit": 50, "offset": offset,
            })
            if r.status_code != 200:
                break
            batch = r.json()
            if not batch:
                break
            all_events.extend(batch)
            if len(batch) < 50:
                break
            offset += 50

    print(f"Raw events: {len(all_events)}")

    # Parse and filter
    results = []
    for ev in all_events:
        tags = ev.get("tags") or []
        if isinstance(tags, str):
            try: tags = json.loads(tags)
            except: tags = []
        tag_slugs = {t.get("slug", "").lower() for t in tags if isinstance(t, dict)}

        # Skip crypto and sports
        if tag_slugs & SKIP_TAGS:
            continue

        # Get category label
        category = ""
        for t in tags:
            if isinstance(t, dict):
                label = t.get("label", "")
                if label and label not in ("All", "Earn 4%"):
                    category = label
                    break
        if not category:
            category = "Other"

        for m in ev.get("markets", []):
            vol = float(m.get("volume") or 0)
            if vol < MIN_VOL:
                continue

            prices = m.get("outcomePrices", [])
            if isinstance(prices, str):
                try: prices = json.loads(prices)
                except: continue
            if len(prices) < 2:
                continue

            yp, np = float(prices[0]), float(prices[1])
            yes_ok = MIN_ASK <= yp <= MAX_ASK
            no_ok = MIN_ASK <= np <= MAX_ASK
            if not yes_ok and not no_ok:
                continue

            if yes_ok and no_ok:
                side = "YES" if yp >= np else "NO"
                price = max(yp, np)
            elif yes_ok:
                side, price = "YES", yp
            else:
                side, price = "NO", np

            end_str = m.get("endDate") or ev.get("endDate") or ""
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                days_left = (end_dt - now).total_seconds() / 86400
            except:
                continue

            q = m.get("question") or ev.get("title", "?")
            results.append({
                "question": q,
                "category": category,
                "side": side,
                "price": price,
                "volume": vol,
                "days": days_left,
                "slug": m.get("slug", ""),
            })

    # Sort by volume, cap at 200
    results.sort(key=lambda x: x["volume"], reverse=True)
    results = results[:200]

    # Group by category
    by_cat = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)

    # Build output
    lines = []
    lines.append(f"WEEKLY OPPORTUNITIES — {now.strftime('%Y-%m-%d %H:%M CET')}")
    lines.append(f"Markets resolving in 7-14 days | Vol > $5K | Ask $0.65-$0.92")
    lines.append(f"Total: {len(results)} markets across {len(by_cat)} categories")
    lines.append("=" * 90)

    for cat in sorted(by_cat.keys(), key=lambda c: -sum(r["volume"] for r in by_cat[c])):
        markets = by_cat[cat]
        cat_vol = sum(r["volume"] for r in markets)
        lines.append(f"\n[{cat.upper()}] ({len(markets)} markets, ${cat_vol:,.0f} total volume)")
        for r in markets:
            q = r["question"][:60]
            vol = f"${r['volume']/1000:.0f}K" if r["volume"] >= 1000 else f"${r['volume']:.0f}"
            lines.append(f"  {q:<62} {r['side']:>3} ${r['price']:.2f}  {vol:>7}  {r['days']:.0f}d")

    output = "\n".join(lines)
    print(output)

    # Save to file
    import os
    os.makedirs("data", exist_ok=True)
    with open("data/weekly_opportunities.txt", "w") as f:
        f.write(output)
    print(f"\nSaved to data/weekly_opportunities.txt")


if __name__ == "__main__":
    asyncio.run(main())
