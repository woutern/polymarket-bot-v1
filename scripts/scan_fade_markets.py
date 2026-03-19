"""Scan Polymarket for fade news opportunities — high Yes ask binary markets."""

import time
from datetime import datetime, timezone

import httpx

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

# Skip crypto price markets (our main bot trades these)
SKIP_KEYWORDS = [
    "btc", "eth", "sol", "bitcoin", "ethereum", "solana",
    "updown", "up or down", "higher or lower",
]


def main():
    print("Fetching active markets from Gamma API...")

    with httpx.Client(timeout=10) as client:
        resp = client.get(
            f"{GAMMA_URL}/markets",
            params={"active": "true", "closed": "false", "limit": 100,
                    "order": "startDate", "ascending": "false"},
        )
        resp.raise_for_status()
        markets = resp.json()

    print(f"Got {len(markets)} markets")

    results = []
    checked = 0

    with httpx.Client(timeout=10) as client:
        for m in markets:
            question = m.get("question", "")
            slug = m.get("slug", "")
            outcomes = m.get("outcomes", [])
            if isinstance(outcomes, str):
                import json
                outcomes = json.loads(outcomes)

            # Skip non-binary
            if len(outcomes) != 2:
                continue

            # Skip crypto price markets
            text = (question + " " + slug).lower()
            if any(kw in text for kw in SKIP_KEYWORDS):
                continue

            # Get token IDs
            token_ids = m.get("clobTokenIds", [])
            if isinstance(token_ids, str):
                import json
                token_ids = json.loads(token_ids)
            if len(token_ids) < 2:
                continue

            yes_token = token_ids[0]
            no_token = token_ids[1]
            checked += 1

            # Fetch orderbook
            try:
                resp = client.get(f"{CLOB_URL}/book", params={"token_id": yes_token})
                if resp.status_code != 200:
                    continue
                book = resp.json()
                asks = book.get("asks", [])
                if not asks:
                    continue
                yes_ask = float(asks[0].get("price", 0))
            except Exception:
                continue

            # Fetch No ask too
            no_ask = 0.0
            try:
                resp = client.get(f"{CLOB_URL}/book", params={"token_id": no_token})
                if resp.status_code == 200:
                    no_asks = resp.json().get("asks", [])
                    if no_asks:
                        no_ask = float(no_asks[0].get("price", 0))
            except Exception:
                pass

            if yes_ask < 0.90:
                time.sleep(0.1)
                continue

            results.append({
                "question": question,
                "slug": slug,
                "yes_ask": yes_ask,
                "no_ask": no_ask,
            })

            time.sleep(0.2)

    # Sort by yes_ask descending
    results.sort(key=lambda x: x["yes_ask"], reverse=True)

    # Format output
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    sep = "─" * 80

    lines = [
        "",
        "=== POLYMARKET FADE NEWS SCAN ===",
        f"Scanned at: {now}",
        f"Total markets checked: {checked}",
        f"Markets with Yes ask >= 0.90: {len(results)}",
        sep,
        f"{'Yes Ask':<9}│ {'No Ask':<8}│ Question",
        sep,
    ]

    for r in results:
        q = r["question"][:70]
        lines.append(f"${r['yes_ask']:<7.2f} │ ${r['no_ask']:<6.2f} │ {q}")

    lines.append(sep)
    lines.append("")

    output = "\n".join(lines)
    print(output)

    # Write to file
    import os
    os.makedirs("output", exist_ok=True)
    with open("output/fade_markets_scan.txt", "w") as f:
        f.write(output)
    print(f"Written to output/fade_markets_scan.txt")


if __name__ == "__main__":
    main()
