"""Orderbook observer: log ask/bid snapshots at precise times after window open.

Runs for 2 hours, no trading. Captures orderbook state at T+2s, T+5s, T+10s,
T+15s, T+20s, T+30s, T+60s after each 5m window opens.

Output: CSV to stdout + DynamoDB polymarket-bot-observations table.
"""

import sys
sys.path.insert(0, "src")

import asyncio
import csv
import io
import json
import time
from collections import defaultdict
from datetime import datetime, timezone

import httpx
import structlog

from polybot.feeds.coinbase_ws import CoinbaseWS
from polybot.feeds.polymarket_rest import get_orderbook
from polybot.market.market_resolver import resolve_window
from polybot.models import Window

logger = structlog.get_logger()

ASSETS = ["BTC", "ETH", "SOL"]
WINDOW_SEC = 300
CHECKPOINTS = [2, 5, 10, 15, 20, 30, 60]  # seconds after window open
DURATION_HOURS = 2

# Store all observations
observations = []


async def get_snapshot(token_id: str) -> dict:
    """Fetch orderbook and extract best bid/ask."""
    try:
        book = await get_orderbook(token_id)
        asks = book.get("asks", [])
        bids = book.get("bids", [])
        best_ask = min(float(a["price"]) for a in asks) if asks else None
        best_bid = max(float(b["price"]) for b in bids) if bids else None
        spread = (best_ask - best_bid) if best_ask and best_bid else None
        return {"ask": best_ask, "bid": best_bid, "spread": spread, "n_asks": len(asks), "n_bids": len(bids)}
    except Exception as e:
        return {"ask": None, "bid": None, "spread": None, "n_asks": 0, "n_bids": 0, "error": str(e)}


async def observe_window(asset: str, window: Window, coinbase: CoinbaseWS):
    """Observe orderbook at each checkpoint after window open."""
    open_ts = window.open_ts
    open_price = coinbase.get_price(asset)
    if open_price <= 0:
        return

    for cp in CHECKPOINTS:
        # Wait until checkpoint time
        target_time = open_ts + cp
        now = time.time()
        wait = target_time - now
        if wait > 0:
            await asyncio.sleep(wait)
        elif wait < -5:
            # Missed this checkpoint by more than 5s, skip
            continue

        current_price = coinbase.get_price(asset)
        if current_price <= 0:
            continue

        pct_move = (current_price - open_price) / open_price * 100

        # Fetch both YES and NO orderbook
        if not window.yes_token_id:
            continue

        yes_snap = await get_snapshot(window.yes_token_id)
        no_snap = await get_snapshot(window.no_token_id) if window.no_token_id else {}

        obs = {
            "asset": asset,
            "timeframe": "5m",
            "window_slug": window.slug,
            "seconds_after_open": cp,
            "timestamp": time.time(),
            "open_price": round(open_price, 2),
            "current_price": round(current_price, 2),
            "pct_move": round(pct_move, 5),
            "yes_ask": yes_snap.get("ask"),
            "yes_bid": yes_snap.get("bid"),
            "yes_spread": yes_snap.get("spread"),
            "yes_n_asks": yes_snap.get("n_asks", 0),
            "no_ask": no_snap.get("ask"),
            "no_bid": no_snap.get("bid"),
            "no_spread": no_snap.get("spread"),
            "no_n_asks": no_snap.get("n_asks", 0),
        }
        observations.append(obs)

        # Determine which side is the "direction side"
        if pct_move > 0:
            dir_ask = yes_snap.get("ask")
            dir_side = "YES"
        elif pct_move < 0:
            dir_ask = no_snap.get("ask")
            dir_side = "NO"
        else:
            dir_ask = yes_snap.get("ask")
            dir_side = "YES"

        print(
            f"  T+{cp:>3}s {asset} move={pct_move:+.4f}% "
            f"YES={yes_snap.get('ask', '?'):>6} NO={no_snap.get('ask', '?'):>6} "
            f"dir={dir_side}@{dir_ask} spread_y={yes_snap.get('spread', '?')} spread_n={no_snap.get('spread', '?')}"
        )


async def main():
    print(f"Orderbook Observer — logging for {DURATION_HOURS} hours")
    print(f"Checkpoints: {CHECKPOINTS} seconds after each 5m window open")
    print(f"Assets: {ASSETS}")
    print()

    coinbase = CoinbaseWS(assets=ASSETS)
    coinbase_task = asyncio.create_task(coinbase.connect())

    # Wait for price feed
    for _ in range(40):
        if any(coinbase.get_price(a) > 0 for a in ASSETS):
            break
        await asyncio.sleep(0.5)

    print(f"Price feed ready: BTC=${coinbase.get_price('BTC'):.0f} ETH=${coinbase.get_price('ETH'):.1f} SOL=${coinbase.get_price('SOL'):.2f}")
    print()

    start = time.time()
    end = start + DURATION_HOURS * 3600
    last_window_ts = {}

    try:
        while time.time() < end:
            now = time.time()
            now_int = int(now)
            window_open_ts = now_int - (now_int % WINDOW_SEC)

            for asset in ASSETS:
                key = f"{asset}_5m"
                if last_window_ts.get(key) == window_open_ts:
                    continue

                # New window just opened — resolve it and start observing
                seconds_in = now - window_open_ts
                if seconds_in > 1.5:  # Only catch windows within first 1.5s
                    last_window_ts[key] = window_open_ts
                    continue

                last_window_ts[key] = window_open_ts
                window = Window(
                    open_ts=window_open_ts,
                    close_ts=window_open_ts + WINDOW_SEC,
                    asset=asset,
                    open_price=coinbase.get_price(asset),
                    slug=Window.slug_for_ts(window_open_ts, asset, WINDOW_SEC),
                )

                # Resolve market (get token IDs)
                try:
                    await resolve_window(window)
                except Exception as e:
                    print(f"  RESOLVE FAILED {asset}: {e}")
                    continue

                if not window.yes_token_id:
                    print(f"  NO TOKEN {asset} {window.slug}")
                    continue

                elapsed = round(time.time() - start)
                remaining = round((end - time.time()) / 60)
                print(f"\n[{elapsed}s elapsed, {remaining}m remaining] Window: {window.slug}")

                # Fire observation task (non-blocking)
                asyncio.create_task(observe_window(asset, window, coinbase))

            await asyncio.sleep(0.25)

    except KeyboardInterrupt:
        print("\nStopped by user")
    finally:
        await coinbase.close()
        coinbase_task.cancel()

    # === REPORT ===
    print("\n" + "=" * 80)
    print(f"ORDERBOOK OBSERVATION REPORT — {len(observations)} snapshots")
    print("=" * 80)

    if not observations:
        print("No observations collected.")
        return

    # Group by checkpoint
    by_cp = defaultdict(list)
    for obs in observations:
        by_cp[obs["seconds_after_open"]].append(obs)

    print(f"\n{'CP':>5} {'N':>5} {'YES ask avg':>12} {'YES ask med':>12} {'NO ask avg':>12} {'<$0.65':>8} {'<$0.55':>8} {'min YES':>10} {'min NO':>10}")
    print("-" * 95)

    for cp in CHECKPOINTS:
        data = by_cp.get(cp, [])
        if not data:
            continue
        yes_asks = [o["yes_ask"] for o in data if o["yes_ask"] is not None]
        no_asks = [o["no_ask"] for o in data if o["no_ask"] is not None]
        if not yes_asks:
            continue

        ya_avg = sum(yes_asks) / len(yes_asks)
        ya_sorted = sorted(yes_asks)
        ya_med = ya_sorted[len(ya_sorted) // 2]
        na_avg = sum(no_asks) / len(no_asks) if no_asks else 0
        below_65 = sum(1 for a in yes_asks if a < 0.65)
        below_55 = sum(1 for a in yes_asks if a < 0.55)
        min_ya = min(yes_asks)
        min_na = min(no_asks) if no_asks else 0

        print(f"T+{cp:>3}s {len(data):>5} {ya_avg:>12.3f} {ya_med:>12.3f} {na_avg:>12.3f} {below_65:>7} ({below_65/len(data)*100:.0f}%) {below_55:>4} ({below_55/len(data)*100:.0f}%) {min_ya:>10.3f} {min_na:>10.3f}")

    # Find ANY observation where YES ask < 0.65
    cheap = [o for o in observations if o["yes_ask"] is not None and o["yes_ask"] < 0.65]
    print(f"\nObservations with YES ask < $0.65: {len(cheap)}")
    for o in cheap[:20]:
        print(f"  T+{o['seconds_after_open']}s {o['asset']} slug={o['window_slug'][-15:]} yes_ask={o['yes_ask']:.3f} no_ask={o.get('no_ask','?')} move={o['pct_move']:+.4f}%")

    # Also check NO ask < 0.65
    cheap_no = [o for o in observations if o["no_ask"] is not None and o["no_ask"] < 0.65]
    print(f"\nObservations with NO ask < $0.65: {len(cheap_no)}")
    for o in cheap_no[:20]:
        print(f"  T+{o['seconds_after_open']}s {o['asset']} slug={o['window_slug'][-15:]} no_ask={o['no_ask']:.3f} yes_ask={o.get('yes_ask','?')} move={o['pct_move']:+.4f}%")

    # Overall minimum asks observed
    all_yes = [o["yes_ask"] for o in observations if o["yes_ask"] is not None]
    all_no = [o["no_ask"] for o in observations if o["no_ask"] is not None]
    print(f"\nOverall minimum YES ask: ${min(all_yes):.3f}" if all_yes else "")
    print(f"Overall minimum NO ask: ${min(all_no):.3f}" if all_no else "")

    # Save CSV
    csv_path = "/tmp/orderbook_observations.csv"
    with open(csv_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=observations[0].keys())
        writer.writeheader()
        writer.writerows(observations)
    print(f"\nCSV saved to {csv_path}")


if __name__ == "__main__":
    asyncio.run(main())
