"""Force a test trade through the full live execution pipeline.

Bypasses signal guards (min_move, EV threshold, market efficiency) but uses
the real LiveTrader code path: sizing → create_order → post_order → DB → DynamoDB.

Usage:
    uv run python scripts/force_trade.py                    # BTC 5m, auto-pick best side
    uv run python scripts/force_trade.py --asset ETH        # ETH 5m
    uv run python scripts/force_trade.py --side YES         # Force YES (UP)
    uv run python scripts/force_trade.py --side NO          # Force NO (DOWN)
    uv run python scripts/force_trade.py --amount 1.50      # Custom dollar amount
    uv run python scripts/force_trade.py --dry-run          # Show what would happen
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

sys.path.insert(0, "src")

import httpx

from polybot.config import Settings
from polybot.models import Direction, Signal, SignalSource


def resolve_current_window(asset: str = "BTC", window_seconds: int = 300):
    """Get token IDs and orderbook for the current window."""
    now = int(time.time())
    aligned = now - (now % window_seconds)
    remaining = (aligned + window_seconds) - now
    tf = "5m"
    slug = f"{asset.lower()}-updown-{tf}-{aligned}"

    print(f"Window: {slug} | {remaining:.0f}s remaining")

    # Resolve market
    resp = httpx.get(
        "https://gamma-api.polymarket.com/events",
        params={"slug": slug, "limit": 1},
        timeout=15,
    )
    events = resp.json()
    if not events:
        print(f"ERROR: No market found for {slug}")
        sys.exit(1)

    mkt = events[0]["markets"][0]
    raw = mkt.get("clobTokenIds", [])
    if isinstance(raw, str):
        raw = json.loads(raw)
    yes_token = raw[0]
    no_token = raw[1]
    condition_id = mkt.get("conditionId", "")

    # Fetch orderbook
    yes_book = httpx.get("https://clob.polymarket.com/book", params={"token_id": yes_token}, timeout=15).json()
    no_book = httpx.get("https://clob.polymarket.com/book", params={"token_id": no_token}, timeout=15).json()

    yes_asks = yes_book.get("asks", [])
    no_asks = no_book.get("asks", [])
    yes_bids = yes_book.get("bids", [])
    no_bids = no_book.get("bids", [])

    yes_best_ask = min(float(a["price"]) for a in yes_asks) if yes_asks else None
    no_best_ask = min(float(a["price"]) for a in no_asks) if no_asks else None
    yes_best_bid = max(float(b["price"]) for b in yes_bids) if yes_bids else 0
    no_best_bid = max(float(b["price"]) for b in no_bids) if no_bids else 0

    return {
        "slug": slug,
        "remaining": remaining,
        "yes_token": yes_token,
        "no_token": no_token,
        "condition_id": condition_id,
        "yes_ask": yes_best_ask,
        "no_ask": no_best_ask,
        "yes_bid": yes_best_bid,
        "no_bid": no_best_bid,
    }


async def force_trade(asset: str, side: str | None, amount: float, dry_run: bool):
    settings = Settings()
    window = resolve_current_window(asset)

    if window["yes_ask"] is None and window["no_ask"] is None:
        print("ERROR: Empty orderbook — no asks on either side")
        sys.exit(1)

    # Auto-pick side: whichever has the cheaper ask (more upside)
    if side is None:
        if (window["yes_ask"] or 99) <= (window["no_ask"] or 99):
            side = "YES"
        else:
            side = "NO"
    side = side.upper()

    ask = window["yes_ask"] if side == "YES" else window["no_ask"]
    if ask is None or ask >= 1.0:
        print(f"ERROR: {side} ask is {ask} — can't trade")
        sys.exit(1)

    direction = Direction.UP if side == "YES" else Direction.DOWN
    token_id = window["yes_token"] if side == "YES" else window["no_token"]

    # Calculate shares
    price = round(ask, 2)
    shares = round(amount / price, 0)
    if shares < 1:
        shares = 1.0
    actual_cost = round(shares * price, 2)

    print(f"\n{'=' * 50}")
    print(f"FORCED TRADE:")
    print(f"  Asset:    {asset}")
    print(f"  Window:   {window['slug']}")
    print(f"  Side:     {side} ({'UP' if side == 'YES' else 'DOWN'})")
    print(f"  Price:    ${price}")
    print(f"  Shares:   {shares:.0f}")
    print(f"  Cost:     ${actual_cost}")
    print(f"  Time left: {window['remaining']:.0f}s")
    print(f"  Orderbook: YES={window['yes_ask']} NO={window['no_ask']}")
    print(f"{'=' * 50}")

    if dry_run:
        print("\n[DRY RUN] Would place this trade. Use without --dry-run to execute.")
        return

    # Build signal (forced — bypasses all guards)
    signal = Signal(
        source=SignalSource.DIRECTIONAL,
        direction=direction,
        model_prob=0.80,  # Forced
        market_price=price,
        ev=0.0,  # Forced trade, no real EV calc
        window_slug=window["slug"],
        asset=asset,
        p_bayesian=0.0,
        p_ai=None,
        pct_move=0.0,
        seconds_remaining=window["remaining"],
        yes_ask=window["yes_ask"] or 0,
        no_ask=window["no_ask"] or 0,
        yes_bid=window["yes_bid"],
        no_bid=window["no_bid"],
        open_price=0.0,
    )

    # Execute through LiveTrader
    from polybot.execution.live_trader import LiveTrader
    from polybot.risk.manager import RiskManager
    from polybot.storage.db import Database
    from polybot.storage.dynamo import DynamoStore

    # Execute order directly via py-clob-client (no local DB needed)
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, OrderArgs, OrderType, CreateOrderOptions

    creds = ApiCreds(
        api_key=settings.polymarket_api_key,
        api_secret=settings.polymarket_api_secret,
        api_passphrase=settings.polymarket_api_passphrase,
    )
    funder = settings.polymarket_funder or None
    sig_type = 2 if funder else 0
    client = ClobClient(
        host="https://clob.polymarket.com",
        chain_id=settings.polymarket_chain_id,
        key=settings.polymarket_private_key,
        creds=creds,
        signature_type=sig_type,
        funder=funder,
    )

    order_args = OrderArgs(
        token_id=token_id,
        price=price,
        size=shares,
        side="BUY",
    )
    options = CreateOrderOptions(tick_size="0.01", neg_risk=False)

    try:
        signed = client.create_order(order_args, options)
        resp = client.post_order(signed, OrderType.FOK)
        order_id = resp.get("orderID", "")
        success = resp.get("success", False)

        if not success:
            print(f"\nORDER NOT MATCHED: {resp.get('errorMsg', resp)}")
            return

        print(f"\nTRADE PLACED!")
        print(f"  Order ID: {order_id}")
        print(f"  Side: {side}")
        print(f"  Price: ${price}")
        print(f"  Shares: {shares:.0f}")
        print(f"  Cost: ${actual_cost}")

        # Write to DynamoDB (dashboard reads from here)
        dynamo = DynamoStore()
        dynamo.put_trade({
            "id": order_id,
            "timestamp": time.time(),
            "window_slug": window["slug"],
            "source": "directional",
            "direction": signal.direction.value,
            "side": side,
            "price": price,
            "size_usd": actual_cost,
            "fill_price": price,
            "pnl": None,
            "resolved": 0,
            "mode": "live",
            "asset": asset,
            "p_bayesian": 0.0,
            "p_ai": None,
            "p_final": 0.0,
            "pct_move": 0.0,
            "seconds_remaining": window["remaining"],
            "ev": 0.0,
            "outcome_source": "forced_test",
        })
        print(f"  Recorded in DynamoDB")

        # Auto-resolve after window closes
        remaining = window["remaining"]
        wait = remaining + 90  # wait for window close + 90s for Gamma API
        print(f"\n  Waiting {wait:.0f}s for resolution...")
        await asyncio.sleep(wait)

        # Resolve via Gamma API
        try:
            import json as _json
            slug = window["slug"]
            resp = httpx.get("https://gamma-api.polymarket.com/markets", params={"slug": slug}, timeout=10)
            m = resp.json()[0]
            if m.get("closed"):
                outcomes = _json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m.get("outcomes", [])
                prices = _json.loads(m["outcomePrices"]) if isinstance(m.get("outcomePrices"), str) else m.get("outcomePrices", [])
                outcome_map = dict(zip(outcomes, [float(p) for p in prices]))
                up_price = outcome_map.get("Up", 0)
                winner = "YES" if up_price >= 0.99 else ("NO" if up_price <= 0.01 else None)
                if winner:
                    correct = (side == winner)
                    pnl = round((actual_cost / price * (1 - price)) if correct else -actual_cost, 4)
                    from decimal import Decimal
                    dynamo._trades.update_item(
                        Key={"id": order_id},
                        UpdateExpression="SET resolved = :r, pnl = :p, polymarket_winner = :w, correct_prediction = :c, outcome_source = :s",
                        ExpressionAttributeValues={
                            ":r": 1, ":p": Decimal(str(pnl)), ":w": winner,
                            ":c": int(correct), ":s": "polymarket_verified",
                        },
                    )
                    emoji = "WIN ✅" if correct else "LOSS ❌"
                    print(f"  RESOLVED: {winner} → {emoji} (pnl=${pnl:+.2f})")
                else:
                    print(f"  Resolution ambiguous — check dashboard")
            else:
                print(f"  Market not closed yet — will be resolved by orphan resolver")
        except Exception as e:
            print(f"  Auto-resolve failed: {e} — will be resolved by orphan resolver")

    except Exception as e:
        print(f"\nTRADE FAILED: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Force a test trade through the full pipeline")
    parser.add_argument("--asset", default="BTC", choices=["BTC", "ETH", "SOL"])
    parser.add_argument("--side", default=None, choices=["YES", "NO", "yes", "no"])
    parser.add_argument("--amount", type=float, default=1.0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    asyncio.run(force_trade(args.asset, args.side, args.amount, args.dry_run))
