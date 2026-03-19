"""Scan Polymarket for news/politics fade opportunities."""

import json
import time

import httpx

GAMMA_URL = "https://gamma-api.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

POLITICS_KEYWORDS = [
    "president", "election", "vote", "congress", "senate race",
    "minister", "government", "policy", "fed rate", "interest rate",
    "ceasefire", "peace deal", "resign", "fired", "arrested", "indicted",
    "impeach", "treaty", "sanction", "tariff", "trade war", "trade deal",
    "invasion", "nuclear", "nato ", "white house", "supreme court",
    "referendum", "coup", "executive order", "legislation",
    "regime", "military action", "ground offensive",
    "trump", "biden", "zelensky", "putin", "xi jinping", "musk",
]

# Skip even if keyword matches — sports/crypto/entertainment
SKIP_PATTERNS = [
    "win on 2026", "win the 2025", "win the 2026", "win the 2027",
    "nba", "nfl", "mlb", "mls", "premier league", "champions league",
    "world cup", "ncaa", "f1 driver", "masters tournament",
    "price of bitcoin", "price of solana", "price of xrp", "price of ethereum",
    "bitcoin reach", "bitcoin dip", "ethereum dip", "solana",
    "crude oil", "tweets from", "post ", "elon musk post",
    "aliens", "jesus christ", "lebron james win the 2028",
    "cricket", "legends cricket", "islanders vs", "billikens",
    "howard bison", "earnings",
]


def _parse_json_field(val):
    if isinstance(val, str):
        return json.loads(val)
    return val or []


def _match_keyword(text: str) -> str | None:
    text = text.lower()
    for kw in POLITICS_KEYWORDS:
        if kw in text:
            return kw
    return None


def main():
    seen_conditions: set[str] = {}
    all_markets: list[dict] = []

    with httpx.Client(timeout=15) as client:
        # Source 1: markets endpoint
        print("Fetching markets...")
        resp = client.get(
            f"{GAMMA_URL}/markets",
            params={"active": "true", "closed": "false", "limit": 200,
                    "order": "volume24hr", "ascending": "false"},
        )
        resp.raise_for_status()
        for m in resp.json():
            cid = m.get("conditionId", "")
            if cid and cid not in seen_conditions:
                seen_conditions[cid] = True
                all_markets.append(m)

        # Source 2: events endpoint
        print("Fetching events...")
        resp = client.get(
            f"{GAMMA_URL}/events",
            params={"active": "true", "limit": 100,
                    "order": "startDate", "ascending": "false"},
        )
        resp.raise_for_status()
        for event in resp.json():
            for m in event.get("markets", []):
                cid = m.get("conditionId", "")
                if cid and cid not in seen_conditions:
                    seen_conditions[cid] = True
                    all_markets.append(m)

    print(f"Total unique markets: {len(all_markets)}")

    results = []
    checked = 0

    with httpx.Client(timeout=10) as client:
        for m in all_markets:
            question = m.get("question", "")
            slug = m.get("slug", "")
            outcomes = _parse_json_field(m.get("outcomes", []))

            if len(outcomes) != 2:
                continue

            text = question + " " + slug
            text_lower = text.lower()

            # Skip sports/crypto/entertainment even if keyword matches
            if any(p in text_lower for p in SKIP_PATTERNS):
                continue

            keyword = _match_keyword(text)
            if not keyword:
                continue

            token_ids = _parse_json_field(m.get("clobTokenIds", []))
            if len(token_ids) < 2:
                continue

            checked += 1

            # Fetch Yes orderbook
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

            if yes_ask < 0.80:
                time.sleep(0.1)
                continue

            # Fetch No orderbook
            no_ask = 0.0
            try:
                resp = client.get(f"{CLOB_URL}/book", params={"token_id": token_ids[1]})
                if resp.status_code == 200:
                    no_asks = resp.json().get("asks", [])
                    if no_asks:
                        no_ask = float(no_asks[0].get("price", 0))
            except Exception:
                pass

            results.append({
                "question": question,
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "keyword": keyword,
            })
            time.sleep(0.2)

    results.sort(key=lambda x: x["yes_ask"], reverse=True)

    sep = "─" * 90
    print(f"\n{'YES ASK':<9}{'NO ASK':<9}{'KEYWORD':<17}QUESTION")
    print(sep)
    for r in results:
        q = r["question"][:60]
        print(f"${r['yes_ask']:<8.2f}${r['no_ask']:<8.2f}{r['keyword']:<17}{q}")
    print(sep)
    print(f"\nNews/politics markets checked: {checked}")
    print(f"Markets with Yes ask >= $0.80: {len(results)}")


if __name__ == "__main__":
    main()
