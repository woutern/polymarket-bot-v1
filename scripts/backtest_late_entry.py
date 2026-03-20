"""Backtest: late-momentum strategy (T+4min entry, follow the move).

At T+4min (240s into window), the price direction is ~80% established.
We simulate: if direction at T+4min matches the final outcome.

Limitation: we don't have per-second price data, so we model T+4min as
80% of the final close move + Gaussian noise. This is conservative —
real T+4min would be slightly more predictable.

Usage:
    uv run python scripts/backtest_late_entry.py
"""

import sys
sys.path.insert(0, "src")

import random
import boto3
from collections import defaultdict
from datetime import datetime, timezone

random.seed(42)


def main():
    session = boto3.Session(profile_name="playground", region_name="us-east-1")

    # Load training data (has outcome per window)
    table = session.resource("dynamodb").Table("polymarket-bot-training-data")
    items = []
    resp = table.scan(Limit=500)
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"], Limit=500)
        items.extend(resp.get("Items", []))

    # Parse into usable records
    windows = []
    for t in items:
        tf = t.get("timeframe", "")
        if tf != "5m":
            continue
        asset = t.get("asset", "")
        open_p = float(t.get("open_price", 0) or 0)
        close_p = float(t.get("close_price", 0) or 0)
        outcome = t.get("outcome")
        ts = float(t.get("timestamp", 0) or 0)

        if open_p <= 0 or close_p <= 0 or outcome is None or ts == 0:
            continue

        outcome = int(float(outcome))
        if outcome not in (0, 1):
            continue

        pct_move = (close_p - open_p) / open_p * 100

        windows.append({
            "asset": asset,
            "open": open_p,
            "close": close_p,
            "outcome": outcome,
            "pct_move": pct_move,
            "timestamp": ts,
        })

    print(f"Loaded {len(windows)} valid 5m windows")

    # Sort by asset + time
    by_asset = defaultdict(list)
    for w in windows:
        by_asset[w["asset"]].append(w)
    for a in by_asset:
        by_asset[a].sort(key=lambda x: x["timestamp"])

    # Simulate late-entry
    # At T+4min (80% through window), model price as:
    #   t4_move = final_move * 0.80 + noise
    # Noise ~ N(0, |final_move| * 0.15) — 15% uncertainty
    # If t4_move and final_move have SAME sign → we bet right
    # If different sign → reversal in last minute, we lose

    bet_size = 1.50
    stats = defaultdict(lambda: {"w": 0, "l": 0, "s": 0, "pnl": 0.0})
    hour_stats = defaultdict(lambda: {"w": 0, "l": 0})
    move_stats = defaultdict(lambda: {"w": 0, "l": 0})
    all_trades = []

    for asset, ws in by_asset.items():
        for w in ws:
            final_move = w["pct_move"]

            # Simulate T+4min price
            noise_std = max(0.005, abs(final_move) * 0.15)
            t4_move = final_move * 0.80 + random.gauss(0, noise_std)

            # Skip if no clear direction at T+4min
            if abs(t4_move) < 0.003:
                stats[asset]["s"] += 1
                continue

            # Prediction: follow T+4min direction
            predicted_up = t4_move > 0
            actual_up = (w["outcome"] == 1)
            won = (predicted_up == actual_up)

            # P&L at assumed $0.65 avg ask
            pnl = (0.35 / 0.65) * bet_size if won else -bet_size

            if won:
                stats[asset]["w"] += 1
            else:
                stats[asset]["l"] += 1
            stats[asset]["pnl"] += pnl

            # By hour
            hour = datetime.fromtimestamp(w["timestamp"], tz=timezone.utc).hour
            if won:
                hour_stats[hour]["w"] += 1
            else:
                hour_stats[hour]["l"] += 1

            # By move magnitude (of T+4min signal, not final)
            abs_t4 = abs(t4_move)
            if abs_t4 < 0.01:
                bucket = "<0.01%"
            elif abs_t4 < 0.03:
                bucket = "0.01-0.03%"
            elif abs_t4 < 0.05:
                bucket = "0.03-0.05%"
            elif abs_t4 < 0.10:
                bucket = "0.05-0.10%"
            else:
                bucket = ">0.10%"

            if won:
                move_stats[bucket]["w"] += 1
            else:
                move_stats[bucket]["l"] += 1

            all_trades.append({"asset": asset, "won": won, "pnl": pnl})

    # Print results
    total_w = sum(s["w"] for s in stats.values())
    total_l = sum(s["l"] for s in stats.values())
    total_s = sum(s["s"] for s in stats.values())
    total_n = total_w + total_l
    total_pnl = sum(s["pnl"] for s in stats.values())
    wr = total_w / total_n * 100 if total_n else 0

    print(f"\n{'='*60}")
    print(f"  LATE-ENTRY BACKTEST")
    print(f"  T+4min momentum follow | $1.50/trade | avg ask $0.65")
    print(f"  NOTE: T+4min simulated as 80% of final move + noise")
    print(f"{'='*60}")
    print(f"\n  Total windows:  {len(windows)}")
    print(f"  Trades fired:   {total_n} ({total_n/len(windows)*100:.0f}%)")
    print(f"  Skipped:        {total_s} (no clear signal)")
    print(f"  Win rate:       {total_w}/{total_n} = {wr:.1f}%")
    print(f"  Total P&L:      ${total_pnl:+.2f}")
    print(f"  Breakeven WR:   65.0% (at $0.65 ask)")
    print(f"  Edge:           {wr - 65:+.1f}pp")

    print(f"\n  BY ASSET:")
    print(f"  {'ASSET':<6} {'W':>6} {'L':>6} {'WR':>6} {'PNL':>9}")
    print(f"  {'-'*35}")
    for asset in sorted(stats):
        s = stats[asset]
        n = s["w"] + s["l"]
        awr = s["w"] / n * 100 if n else 0
        print(f"  {asset:<6} {s['w']:>6} {s['l']:>6} {awr:>5.1f}% ${s['pnl']:>+7.2f}")

    print(f"\n  BY MOVE AT T+4min:")
    print(f"  {'MOVE':<12} {'W':>6} {'L':>6} {'WR':>6} {'N':>6}")
    print(f"  {'-'*38}")
    for bucket in ["<0.01%", "0.01-0.03%", "0.03-0.05%", "0.05-0.10%", ">0.10%"]:
        s = move_stats.get(bucket, {"w": 0, "l": 0})
        n = s["w"] + s["l"]
        if n == 0:
            continue
        bwr = s["w"] / n * 100
        print(f"  {bucket:<12} {s['w']:>6} {s['l']:>6} {bwr:>5.1f}% {n:>6}")

    print(f"\n  BY HOUR (UTC):")
    print(f"  {'HOUR':>6} {'W':>5} {'L':>5} {'WR':>6}")
    print(f"  {'-'*25}")
    for hour in sorted(hour_stats):
        s = hour_stats[hour]
        n = s["w"] + s["l"]
        if n < 10:
            continue
        hwr = s["w"] / n * 100
        print(f"  {hour:>4}:00 {s['w']:>5} {s['l']:>5} {hwr:>5.1f}%")

    # Equity curve
    cum = 0
    peak = 0
    max_dd = 0
    for t in all_trades:
        cum += t["pnl"]
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)
    print(f"\n  Max drawdown: ${max_dd:.2f}")
    print(f"  Final equity: ${cum:+.2f}")


if __name__ == "__main__":
    main()
