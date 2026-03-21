"""One-shot scan: fetch all opportunity markets, sort by resolve time, output flat list."""

import asyncio
import json
import sys
import os
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx

GAMMA = "https://gamma-api.polymarket.com"
SKIP_SLUGS = {"5m", "15m", "updown"}
MIN_VOLUME = 1_000

WORKER_TAG_SLUGS = {
    "crypto": ["crypto", "crypto-prices", "bitcoin", "ethereum", "solana", "xrp", "bitcoin-prices",
               "ethereum-prices", "solana-prices", "xrp-prices", "cryptocurrency", "hit-price"],
    "finance": ["finance", "economics", "economy", "stocks", "earnings", "daily-close",
                "finance-updown", "tsla", "nvda", "nflx", "aapl", "meta"],
    "fed": ["fed", "fed-rates", "fomc", "federal-reserve", "jerome-powell", "fed-chair",
            "economic-policy"],
    "politics": ["politics", "us-politics", "trump", "elections", "government", "trump-approval",
                 "world-elections", "global-elections", "presidential-election"],
    "geopolitics": ["geopolitics", "iran", "world", "middle-east", "war", "ukraine", "russia",
                    "us-iran", "ukraine-peace-deal", "israel", "strait-of-hormuz",
                    "north-korea", "lebanon", "khamenei"],
    "elections": ["elections", "world-elections", "global-elections", "french-elections",
                  "german-elections", "slovenia-elections", "denmark-elections",
                  "peru-elections", "mayoral-elections", "special-elections"],
    "tech": ["tech", "ai", "technology", "ai-development", "openai", "open-ai", "big-tech",
             "gta-vi"],
    "weather": ["temperature", "weather", "daily", "precipitation"],
    "culture": ["culture", "entertainment", "awards", "mrbeast", "youtube",
                "prediction-markets", "recurring"],
}


async def fetch_tags():
    """Resolve tag slugs to IDs."""
    async with httpx.AsyncClient(timeout=15) as c:
        all_tags = []
        offset = 0
        while offset < 6000:
            r = await c.get(f"{GAMMA}/tags", params={"limit": 100, "offset": offset})
            batch = r.json() if r.status_code == 200 else []
            if not batch:
                break
            all_tags.extend(batch)
            if len(batch) < 100:
                break
            offset += 100
    return {t["slug"]: t["id"] for t in all_tags if "slug" in t and "id" in t}


async def fetch_worker_events(client, tag_ids, worker):
    events = []
    for tid in tag_ids:
        try:
            r = await client.get(f"{GAMMA}/events", params={
                "tag_id": tid, "active": "true", "closed": "false",
                "limit": 50, "offset": 0,
            })
            if r.status_code == 200:
                events.extend(r.json())
        except Exception:
            pass
    return worker, events


def parse_all(events, worker):
    now = datetime.now(timezone.utc)
    seen = set()
    out = []

    for ev in events:
        cat = ""
        for tag in (ev.get("tags") or []):
            if isinstance(tag, dict):
                cat = tag.get("label", "")
                break

        for m in ev.get("markets", []):
            slug = m.get("slug", "")
            cid = m.get("conditionId", "")
            if slug in seen or not cid:
                continue
            seen.add(slug)

            if any(s in slug.lower() for s in SKIP_SLUGS):
                continue
            vol = float(m.get("volume") or 0)
            if vol < MIN_VOLUME:
                continue

            prices = m.get("outcomePrices", [])
            if isinstance(prices, str):
                try:
                    prices = json.loads(prices)
                except Exception:
                    continue
            if len(prices) < 2:
                continue

            yp, np_ = float(prices[0]), float(prices[1])
            if yp >= np_ and 0.65 <= yp <= 0.95:
                side, price = "YES", yp
            elif 0.65 <= np_ <= 0.95:
                side, price = "NO", np_
            else:
                continue

            end_str = m.get("endDate") or ev.get("endDate") or ""
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                hrs = (end_dt - now).total_seconds() / 3600
            except Exception:
                continue
            if hrs < 0.5:
                continue

            # Tier
            if 0.85 <= price <= 0.95 and hrs <= 24:
                tier = 1
            elif (0.65 <= price < 0.85 and hrs <= 24) or (0.85 <= price <= 0.95 and 24 < hrs <= 48):
                tier = 2
            else:
                continue

            out.append({
                "question": m.get("question") or ev.get("title", "?"),
                "slug": slug,
                "side": side,
                "price": price,
                "volume": vol,
                "hours_left": hrs,
                "end_date": end_str,
                "tier": tier,
                "worker": worker,
                "category": cat,
            })

    return out


async def main():
    print("Fetching tags...")
    slug_to_id = await fetch_tags()
    print(f"  {len(slug_to_id)} tags loaded\n")

    worker_tags = {}
    for worker, slugs in WORKER_TAG_SLUGS.items():
        worker_tags[worker] = [slug_to_id[s] for s in slugs if s in slug_to_id]

    print("Fetching markets from all 9 workers...")
    all_opps = []
    seen_slugs = set()

    async with httpx.AsyncClient(timeout=15) as client:
        tasks = [fetch_worker_events(client, ids, w) for w, ids in worker_tags.items()]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for result in results:
        if isinstance(result, Exception):
            continue
        worker, events = result
        opps = parse_all(events, worker)
        for o in opps:
            if o["slug"] not in seen_slugs:
                seen_slugs.add(o["slug"])
                all_opps.append(o)

    # Sort by resolve time (soonest first)
    all_opps.sort(key=lambda o: o["end_date"])

    now_cet = datetime.now(timezone(timedelta(hours=1)))
    print(f"Scan time: {now_cet.strftime('%Y-%m-%d %H:%M')} CET")
    print(f"Total opportunities: {len(all_opps)}")
    print(f"Tier 1 (auto-trade $5): {sum(1 for o in all_opps if o['tier'] == 1)}")
    print(f"Tier 2 (AI-checked $2.50): {sum(1 for o in all_opps if o['tier'] == 2)}")
    print()
    print("=" * 120)
    print(f"{'#':>3}  {'Tier':>4}  {'Resolves (CET)':>18}  {'Hrs':>5}  {'Side':>4}  {'Ask':>5}  {'Vol':>10}  {'Worker':>12}  Question")
    print("=" * 120)

    for i, o in enumerate(all_opps, 1):
        try:
            end_utc = datetime.fromisoformat(o["end_date"].replace("Z", "+00:00"))
            end_cet = end_utc + timedelta(hours=1)
            end_str = end_cet.strftime("%Y-%m-%d %H:%M")
        except Exception:
            end_str = o["end_date"][:16]

        q = o["question"]
        if len(q) > 55:
            q = q[:52] + "..."

        print(f"{i:>3}  T{o['tier']:>3}  {end_str:>18}  {o['hours_left']:>5.1f}  {o['side']:>4}  ${o['price']:.2f}  ${o['volume']:>9,.0f}  {o['worker']:>12}  {q}")

    # Save to file
    out_path = os.path.join(os.path.dirname(__file__), "..", "data", "opportunity_scan.txt")
    with open(out_path, "w") as f:
        f.write(f"Polymarket Opportunity Scan — {now_cet.strftime('%Y-%m-%d %H:%M')} CET\n")
        f.write(f"Total: {len(all_opps)} | Tier 1: {sum(1 for o in all_opps if o['tier'] == 1)} | Tier 2: {sum(1 for o in all_opps if o['tier'] == 2)}\n")
        f.write(f"Sorted by resolve time (soonest first)\n\n")
        f.write(f"{'#':>3}  {'Tier':>4}  {'Resolves (CET)':>18}  {'Hrs':>5}  {'Side':>4}  {'Ask':>5}  {'Vol':>10}  {'Worker':>12}  Question\n")
        f.write("=" * 120 + "\n")
        for i, o in enumerate(all_opps, 1):
            try:
                end_utc = datetime.fromisoformat(o["end_date"].replace("Z", "+00:00"))
                end_cet = end_utc + timedelta(hours=1)
                end_str = end_cet.strftime("%Y-%m-%d %H:%M")
            except Exception:
                end_str = o["end_date"][:16]
            q = o["question"]
            if len(q) > 55:
                q = q[:52] + "..."
            f.write(f"{i:>3}  T{o['tier']:>3}  {end_str:>18}  {o['hours_left']:>5.1f}  {o['side']:>4}  ${o['price']:.2f}  ${o['volume']:>9,.0f}  {o['worker']:>12}  {q}\n")

    print(f"\nSaved to {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
