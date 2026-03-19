"""Fade News Monitor — scans Polymarket for politics/news fade opportunities.

Runs standalone. Polls every 5 minutes for markets where YES ask is in the
$0.80-$0.95 "hype zone". Logs to DynamoDB for dashboard tracking.
Does NOT trade — observation only.

Usage:
    uv run python scripts/fade_news_monitor.py          # single scan
    uv run python scripts/fade_news_monitor.py --loop    # continuous (5min)
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal

import httpx

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

# Only include genuine politics/news markets
POLITICS_KEYWORDS = [
    "president", "election", "vote", "congress", "senate race",
    "minister", "government", "policy", "fed rate", "interest rate",
    "ceasefire", "peace deal", "resign", "fired", "arrested", "indicted",
    "impeach", "treaty", "sanction", "tariff", "trade war", "trade deal",
    "invasion", "nuclear", "nato ", "white house", "supreme court",
    "referendum", "coup", "executive order", "legislation",
    "regime", "military action", "ground offensive",
    "trump", "biden", "zelensky", "putin",
]

# Skip even if keyword matches
SKIP_PATTERNS = [
    "win on 2026", "win the 2025", "win the 2026", "win the 2027",
    "nba", "nfl", "mlb", "mls", "premier league", "champions league",
    "world cup", "ncaa", "f1 driver", "masters tournament",
    "price of bitcoin", "price of solana", "price of xrp", "price of ethereum",
    "bitcoin reach", "bitcoin dip", "ethereum dip",
    "crude oil", "tweets from", "elon musk post",
    "aliens", "jesus christ", "cricket", "legends cricket",
    "earnings", "billikens", "howard bison",
]

# Fade zone: YES ask between these values
MIN_YES_ASK = 0.70
MAX_YES_ASK = 0.95


def _parse(val):
    if isinstance(val, str):
        return json.loads(val)
    return val or []


def _match_keyword(text: str) -> str | None:
    text = text.lower()
    for kw in POLITICS_KEYWORDS:
        if kw in text:
            return kw
    return None


def _get_dynamo_table():
    try:
        import boto3
        profile = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None
        session = boto3.Session(profile_name=profile, region_name="us-east-1")
        ddb = session.resource("dynamodb")
        table = ddb.Table("polymarket-bot-fade-news")
        # Test connectivity
        table.table_status
        return table
    except Exception as e:
        print(f"DynamoDB not available: {e}")
        return None


def scan_once() -> list[dict]:
    """Run one scan. Returns list of fade candidates."""
    seen = {}
    all_markets = []

    with httpx.Client(timeout=15) as client:
        # Fetch from markets endpoint (by volume)
        try:
            resp = client.get(
                f"{GAMMA_URL}/markets",
                params={"active": "true", "closed": "false", "limit": 200,
                        "order": "volume24hr", "ascending": "false"},
            )
            resp.raise_for_status()
            for m in resp.json():
                cid = m.get("conditionId", "")
                if cid and cid not in seen:
                    seen[cid] = True
                    all_markets.append(m)
        except Exception as e:
            print(f"Markets fetch failed: {e}")

        # Fetch from events endpoint
        try:
            resp = client.get(
                f"{GAMMA_URL}/events",
                params={"active": "true", "limit": 100,
                        "order": "startDate", "ascending": "false"},
            )
            resp.raise_for_status()
            for event in resp.json():
                for m in event.get("markets", []):
                    cid = m.get("conditionId", "")
                    if cid and cid not in seen:
                        seen[cid] = True
                        all_markets.append(m)
        except Exception as e:
            print(f"Events fetch failed: {e}")

    results = []
    checked = 0

    with httpx.Client(timeout=10) as client:
        for m in all_markets:
            question = m.get("question", "")
            slug = m.get("slug", "")
            outcomes = _parse(m.get("outcomes", []))
            condition_id = m.get("conditionId", "")
            end_date = m.get("endDate", "")

            if len(outcomes) != 2:
                continue

            text = (question + " " + slug).lower()
            if any(p in text for p in SKIP_PATTERNS):
                continue

            keyword = _match_keyword(question + " " + slug)
            if not keyword:
                continue

            token_ids = _parse(m.get("clobTokenIds", []))
            if len(token_ids) < 2:
                continue

            checked += 1

            # Fetch YES orderbook
            try:
                resp = client.get(f"{CLOB_URL}/book", params={"token_id": token_ids[0]})
                if resp.status_code != 200:
                    continue
                asks = resp.json().get("asks", [])
                if not asks:
                    continue
                yes_ask = float(asks[0].get("price", 0))
            except Exception:
                continue

            # Fetch NO orderbook
            no_ask = 0.0
            try:
                resp = client.get(f"{CLOB_URL}/book", params={"token_id": token_ids[1]})
                if resp.status_code == 200:
                    no_asks = resp.json().get("asks", [])
                    if no_asks:
                        no_ask = float(no_asks[0].get("price", 0))
            except Exception:
                pass

            # Only include fade zone
            if not (MIN_YES_ASK <= yes_ask <= MAX_YES_ASK):
                time.sleep(0.1)
                continue

            volume = float(m.get("volume", 0) or 0)

            results.append({
                "condition_id": condition_id,
                "question": question,
                "slug": slug,
                "keyword": keyword,
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "no_token_id": token_ids[1],
                "yes_token_id": token_ids[0],
                "volume": volume,
                "end_date": end_date,
                "scanned_at": time.time(),
            })
            time.sleep(0.15)

    results.sort(key=lambda x: x["yes_ask"], reverse=True)

    # Print table
    now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    print(f"\n[{now}] Scanned {checked} politics/news markets, {len(results)} in fade zone ($0.70-$0.95)")
    if results:
        sep = "─" * 95
        print(f"{'YES':>6} {'NO':>6} {'VOL':>8} {'KEYWORD':<17} QUESTION")
        print(sep)
        for r in results:
            q = r["question"][:55]
            vol = f"${r['volume']/1000:.0f}K" if r["volume"] >= 1000 else f"${r['volume']:.0f}"
            print(f"${r['yes_ask']:.2f}  ${r['no_ask']:.2f} {vol:>8} {r['keyword']:<17} {q}")
        print(sep)
    else:
        print("  No markets in fade zone right now.")

    return results


def save_to_dynamo(results: list[dict], table):
    """Save scan results to DynamoDB."""
    if not table or not results:
        return

    from decimal import Decimal

    for r in results:
        try:
            table.put_item(Item={
                "condition_id": r["condition_id"],
                "scanned_at": Decimal(str(round(r["scanned_at"], 3))),
                "question": r["question"],
                "slug": r["slug"],
                "keyword": r["keyword"],
                "yes_ask": Decimal(str(round(r["yes_ask"], 4))),
                "no_ask": Decimal(str(round(r["no_ask"], 4))),
                "volume": Decimal(str(round(r["volume"], 2))),
                "end_date": r["end_date"],
                "resolved": False,
                "outcome": None,
            })
        except Exception as e:
            print(f"  DynamoDB write failed: {e}")


def main():
    loop_mode = "--loop" in sys.argv
    table = _get_dynamo_table()

    if loop_mode:
        print("Fade News Monitor — continuous mode (every 5 min)")
        print("Press Ctrl+C to stop\n")
        while True:
            try:
                results = scan_once()
                save_to_dynamo(results, table)
                time.sleep(300)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
            except Exception as e:
                print(f"Error: {e}")
                time.sleep(60)
    else:
        results = scan_once()
        save_to_dynamo(results, table)


if __name__ == "__main__":
    main()
