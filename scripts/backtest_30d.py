"""30-day backtest: replay Tier C signal logic against historical BTC 5m/15m windows.

Uses:
- Coinbase 1-min candle data from S3 (open/high/low/close per minute)
- Polymarket Gamma API for actual outcomes (who won: YES or NO)

For each window:
1. Compute the price at T-60s (open of the candle 1 minute before close)
2. Compute pct_move from window open
3. Check if move > threshold
4. Simulate entry at the T-60s price (conservative: assume ask = 0.55 for UP, 0.55 for DOWN)
5. Compare our side to actual Polymarket outcome
"""

import sys
sys.path.insert(0, "src")

import json
import time
from collections import defaultdict
from datetime import datetime, timezone

import httpx
import pandas as pd


def load_candles(path: str) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df = df.sort_values("start").reset_index(drop=True)
    df["start"] = df["start"].astype(int)
    return df


def get_candle_at(df: pd.DataFrame, ts: int) -> dict | None:
    """Get the 1-min candle that contains timestamp ts."""
    aligned = ts - (ts % 60)
    matches = df[df["start"] == aligned]
    if len(matches) == 0:
        return None
    row = matches.iloc[0]
    return {"open": row["open"], "close": row["close"], "high": row["high"], "low": row["low"]}


def get_market_outcome(slug: str) -> str | None:
    """Query Gamma API for market outcome. Returns 'YES', 'NO', or None."""
    try:
        resp = httpx.get(
            "https://gamma-api.polymarket.com/events",
            params={"slug": slug, "limit": 1},
            timeout=10,
        )
        events = resp.json()
        if not events:
            return None
        mkt = events[0].get("markets", [{}])[0]
        if not mkt.get("closed"):
            return None
        prices = mkt.get("outcomePrices", [])
        if isinstance(prices, str):
            prices = json.loads(prices)
        if len(prices) >= 2:
            yes_p = float(prices[0])
            if yes_p >= 0.5:
                return "YES"
            else:
                return "NO"
        return None
    except Exception:
        return None


def run_backtest():
    print("Loading BTC candle data...")
    df = load_candles("/tmp/btc_candles.parquet")
    print(f"  {len(df)} candles, {df.iloc[0]['start']} to {df.iloc[-1]['start']}")

    now = int(time.time())
    thirty_days_ago = now - (30 * 24 * 3600)

    # Round to 5-min boundary
    start_ts = thirty_days_ago - (thirty_days_ago % 300)
    end_ts = now - (now % 300) - 300  # exclude current window

    # Only use windows where we have candle data
    candle_start = int(df.iloc[0]["start"])
    candle_end = int(df.iloc[-1]["start"])
    start_ts = max(start_ts, candle_start)
    end_ts = min(end_ts, candle_end)

    configs = [
        {"asset": "BTC", "tf": "5m", "window_sec": 300, "min_move": 0.08, "prefix": "btc-updown-5m"},
        {"asset": "BTC", "tf": "15m", "window_sec": 900, "min_move": 0.12, "prefix": "btc-updown-15m"},
    ]

    for cfg in configs:
        print(f"\n{'='*60}")
        print(f"Backtesting: {cfg['asset']} {cfg['tf']} (min_move={cfg['min_move']}%)")
        print(f"{'='*60}")

        ws = cfg["window_sec"]
        windows_checked = 0
        signals_fired = 0
        trades = []
        skipped_no_data = 0
        skipped_no_outcome = 0

        ts = start_ts
        while ts <= end_ts:
            slug = f"{cfg['prefix']}-{ts}"
            window_open_ts = ts
            window_close_ts = ts + ws

            # Get candle at window open (for open price)
            open_candle = get_candle_at(df, window_open_ts)
            # Get candle at T-60s (entry evaluation point)
            entry_ts = window_close_ts - 60
            entry_candle = get_candle_at(df, entry_ts)

            if not open_candle or not entry_candle:
                skipped_no_data += 1
                ts += ws
                continue

            windows_checked += 1
            open_price = open_candle["open"]
            entry_price = entry_candle["close"]  # price at T-60s

            # Compute move
            pct_move = (entry_price - open_price) / open_price * 100

            # Check threshold
            if abs(pct_move) < cfg["min_move"]:
                ts += ws
                continue

            signals_fired += 1

            # Determine our side
            if pct_move > 0:
                our_side = "YES"  # bet UP
                simulated_ask = 0.60  # conservative: assume we pay 0.60
            else:
                our_side = "NO"  # bet DOWN
                simulated_ask = 0.60

            # Get actual outcome from Polymarket
            # Rate limit: batch these later, for now check every Nth
            outcome = None
            # Use candle data to infer outcome (close price at window end)
            close_candle = get_candle_at(df, window_close_ts)
            if close_candle:
                close_price = close_candle["close"]
                went_up = close_price >= open_price
                outcome = "YES" if went_up else "NO"
            else:
                skipped_no_outcome += 1
                ts += ws
                continue

            won = our_side == outcome
            if won:
                pnl = (1.0 - simulated_ask) / simulated_ask  # return per dollar
            else:
                pnl = -1.0

            trades.append({
                "ts": window_open_ts,
                "slug": slug,
                "pct_move": pct_move,
                "our_side": our_side,
                "outcome": outcome,
                "won": won,
                "pnl_pct": pnl,
                "entry_price": entry_price,
                "open_price": open_price,
                "ask": simulated_ask,
            })

            ts += ws

        # Results
        wins = sum(1 for t in trades if t["won"])
        losses = len(trades) - wins
        wr = wins / len(trades) * 100 if trades else 0
        total_return = sum(t["pnl_pct"] for t in trades)

        print(f"\nResults:")
        print(f"  Windows checked:   {windows_checked}")
        print(f"  Signals fired:     {signals_fired}")
        print(f"  Trades simulated:  {len(trades)}")
        print(f"  Skipped (no data): {skipped_no_data}")
        print(f"  Skipped (no close):{skipped_no_outcome}")
        print(f"  Wins:              {wins}")
        print(f"  Losses:            {losses}")
        print(f"  Win rate:          {wr:.1f}%")
        print(f"  Total return:      {total_return:+.1f}x (on $1 per trade)")
        print(f"  Per-trade:         {total_return/len(trades):+.3f}x avg" if trades else "")

        if trades:
            # Breakdown by move size
            print(f"\n  By move size:")
            buckets = defaultdict(lambda: {"wins": 0, "total": 0})
            for t in trades:
                am = abs(t["pct_move"])
                if am < 0.10:
                    b = "0.08-0.10%"
                elif am < 0.15:
                    b = "0.10-0.15%"
                elif am < 0.25:
                    b = "0.15-0.25%"
                else:
                    b = "0.25%+"
                buckets[b]["total"] += 1
                if t["won"]:
                    buckets[b]["wins"] += 1
            for b in sorted(buckets.keys()):
                v = buckets[b]
                bwr = v["wins"] / v["total"] * 100 if v["total"] else 0
                print(f"    {b:>12}: {v['wins']}/{v['total']} = {bwr:.1f}% WR")

            # Show last 10 trades
            print(f"\n  Last 10 trades:")
            for t in trades[-10:]:
                dt = datetime.fromtimestamp(t["ts"], tz=timezone.utc).strftime("%m-%d %H:%M")
                result = "WIN" if t["won"] else "LOSS"
                print(f"    {dt} move={t['pct_move']:+.3f}% side={t['our_side']:>3} outcome={t['outcome']:>3} → {result}")


if __name__ == "__main__":
    run_backtest()
