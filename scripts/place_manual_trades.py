"""Place manual trades on Polymarket markets by slug.

Fetches market data from Gamma API by slug, then places FOK orders via CLOB.

Usage:
    PYTHONPATH=src uv run python scripts/place_manual_trades.py
"""

import sys
sys.path.insert(0, "src")

import asyncio
import json
import time

import httpx
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    CreateOrderOptions,
    OrderArgs,
    OrderType,
)

from polybot.config import Settings

GAMMA_URL = "https://gamma-api.polymarket.com"

# (slug, side, size_usd, label)
TRADES = [
    # CRYPTO
    ("bitcoin-above-68k-on-march-21", "YES", 1.00, "BTC above $68K Mar 21"),
    ("xrp-above-1pt5-on-march-21", "NO", 1.00, "XRP above $1.50 Mar 21"),
    ("will-bitcoin-dip-to-69k-on-march-20", "NO", 1.00, "BTC dip to $69K Mar 20"),
    ("will-solana-dip-to-85-on-march-20", "NO", 1.00, "SOL dip to $85 Mar 20"),
    ("will-bitcoin-dip-to-66k-march-16-22", "NO", 1.00, "BTC dip to $66K wk"),
    ("will-ethereum-dip-to-2000-march-16-22", "NO", 1.00, "ETH dip to $2K wk"),
    # FINANCE
    ("tsla-close-above-370-on-march-20-2026", "NO", 1.00, "TSLA close >$370"),
    ("nya-up-or-down-on-march-20-2026", "NO", 1.00, "NYA Up/Down Mar 20"),
    # SPORTS
    ("nba-nyk-bkn-2026-03-20", "YES", 1.00, "Knicks vs Nets"),
    ("nba-bos-mem-2026-03-20", "YES", 1.00, "Celtics vs Grizzlies"),
    # POLITICS
    ("elon-musk-of-tweets-march-19-march-21-14", "NO", 1.00, "Musk 140-164 tweets"),
    # MULTI-DAY CRYPTO
    ("bitcoin-above-64k-on-march-21", "YES", 1.00, "BTC above $64K Mar 21"),
]

# These had different names/ranges than expected, or resolved:
# PLTR $152-$154 → actual market is "finish week above $152" (different structure)
# MSFT $370-$380 → actual market is "$360-$370" (wrong range)
# TCU vs Duke → matched a total line, not the winner market
# Angers/Udinese/Real Sociedad → negRisk=True (need special handling)
# Trump approval → negRisk=True

# negRisk markets (need neg_risk=True in order options)
NEG_RISK_TRADES = [
    ("fl1-rcl-ang-2026-03-20-ang", "NO", 1.00, "Angers SCO win"),
    ("sea-gen-udi-2026-03-20-udi", "NO", 1.00, "Udinese win"),
    ("lal-vil-rso-2026-03-20-rso", "NO", 1.00, "Real Sociedad win"),
    ("will-trumps-approval-rating-be-between-410-and-414-march-20-2026", "NO", 1.00, "Trump 41.0-41.4%"),
    ("will-trumps-approval-rating-be-between-405-and-409-march-20-2026", "YES", 1.00, "Trump 40.5-40.9%"),
]


def init_client() -> ClobClient:
    s = Settings()
    creds = ApiCreds(
        api_key=s.polymarket_api_key,
        api_secret=s.polymarket_api_secret,
        api_passphrase=s.polymarket_api_passphrase,
    )
    funder = s.polymarket_funder or None
    sig_type = 2 if funder else 0
    return ClobClient(
        host="https://clob.polymarket.com",
        chain_id=s.polymarket_chain_id,
        key=s.polymarket_private_key,
        creds=creds,
        signature_type=sig_type,
        funder=funder,
    )


async def get_market_by_slug(slug: str) -> dict | None:
    """Fetch market from Gamma API by exact slug."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(f"{GAMMA_URL}/markets", params={"slug": slug})
        if resp.status_code == 200:
            markets = resp.json()
            if markets:
                return markets[0]
    return None


def place_order(client: ClobClient, market: dict, side: str, size_usd: float, neg_risk: bool = False) -> dict:
    """Place a FOK order."""
    tokens = market.get("clobTokenIds", [])
    if isinstance(tokens, str):
        tokens = json.loads(tokens)
    prices = market.get("outcomePrices", [])
    if isinstance(prices, str):
        try:
            prices = json.loads(prices)
        except Exception:
            prices = []

    if side == "YES":
        token_id = tokens[0] if tokens else ""
        price = round(float(prices[0]) if prices else 0, 2)
    else:
        token_id = tokens[1] if len(tokens) > 1 else ""
        price = round(float(prices[1]) if len(prices) > 1 else 0, 2)

    if price <= 0 or price >= 1:
        return {"success": False, "error": f"Bad price: {price}", "price": price}
    if not token_id:
        return {"success": False, "error": "No token ID", "price": price}

    shares = round(size_usd / price, 0)
    if shares < 5:
        shares = 5  # Polymarket minimum
    actual_cost = round(shares * price, 2)

    order_args = OrderArgs(token_id=token_id, price=price, size=shares, side="BUY")
    options = CreateOrderOptions(tick_size="0.01", neg_risk=neg_risk)

    try:
        signed = client.create_order(order_args, options)
        resp = client.post_order(signed, OrderType.FOK)
        success = resp.get("success", False) if resp else False
        order_id = resp.get("orderID", "") if resp else ""
        error = resp.get("errorMsg", "") if resp else "no response"
        return {
            "success": success,
            "order_id": order_id,
            "price": price,
            "shares": shares,
            "cost": actual_cost,
            "error": error if not success else "",
        }
    except Exception as e:
        return {"success": False, "error": str(e)[:80], "price": price}


async def main():
    print("Initializing CLOB client...")
    client = init_client()

    all_trades = [(s, side, sz, label, False) for s, side, sz, label in TRADES]
    all_trades += [(s, side, sz, label, True) for s, side, sz, label in NEG_RISK_TRADES]

    results = []
    total_cost = 0
    successes = 0

    for i, (slug, side, size, label, neg_risk) in enumerate(all_trades, 1):
        print(f"\n[{i}/{len(all_trades)}] {label} (slug={slug[:40]})")
        market = await get_market_by_slug(slug)

        if not market:
            print(f"  ✗ Market not found for slug: {slug}")
            results.append({"n": i, "label": label, "side": side, "status": "NOT_FOUND", "price": 0, "cost": 0, "error": "Slug not found"})
            continue

        q = (market.get("question") or "?")[:55]
        print(f"  Found: {q}")

        result = place_order(client, market, side, size, neg_risk=neg_risk)

        if result["success"]:
            print(f"  ✓ FILLED: {result['shares']:.0f} shares @ ${result['price']:.2f} = ${result['cost']:.2f}")
            successes += 1
            total_cost += result["cost"]
            results.append({"n": i, "label": label, "side": side, "status": "FILLED", "price": result["price"], "cost": result["cost"], "error": ""})
        else:
            print(f"  ✗ FAILED: {result['error'][:60]}")
            results.append({"n": i, "label": label, "side": side, "status": "FAILED", "price": result["price"], "cost": 0, "error": result["error"][:40]})

        await asyncio.sleep(0.3)

    # Summary
    print(f"\n{'═' * 95}")
    print(f"  TRADE SUMMARY")
    print(f"{'═' * 95}")
    print(f"  {'#':>3} {'Market':<30} {'Side':>4} {'Price':>6} {'Cost':>7} {'Status':<8} {'Error'}")
    print(f"  {'─' * 90}")
    for r in results:
        print(f"  {r['n']:>3} {r['label']:<30} {r['side']:>4} ${r['price']:.2f} ${r['cost']:>5.2f} {r['status']:<8} {r['error']}")

    print(f"\n  Filled: {successes}/{len(all_trades)}")
    print(f"  Total cost: ${total_cost:.2f}")


if __name__ == "__main__":
    asyncio.run(main())
