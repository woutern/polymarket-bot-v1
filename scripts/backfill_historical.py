"""Historical data pipeline — combine training data + signals for full backtest.

Uses existing DynamoDB tables:
- polymarket-bot-training-data: outcomes (open/close/direction) per 5min window
- polymarket-bot-signals: ask prices (yes_ask/no_ask) at entry time

Matches by window_slug, enriches with ask-based features, stores in
historical_windows table, runs full analysis.

Usage:
    uv run python scripts/backfill_historical.py
"""

import sys
sys.path.insert(0, "src")

import boto3
import json
import math
import os
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal


def main():
    session = boto3.Session(profile_name="playground", region_name="eu-west-1")

    # Step 1: Load ALL training data
    print("Loading training data...")
    td_table = session.resource("dynamodb").Table("polymarket-bot-training-data")
    training = {}
    resp = td_table.scan()
    items = resp.get("Items", [])
    while "LastEvaluatedKey" in resp:
        resp = td_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))

    for i in items:
        if i.get("timeframe") != "5m":
            continue
        asset = i.get("asset", "")
        if asset not in ("BTC", "SOL"):
            continue
        wid = i.get("window_id", "")
        slug = wid.split("_", 2)[-1] if "_" in wid else wid
        if not slug:
            continue
        training[slug] = {
            "asset": asset,
            "open_price": float(i.get("open_price", 0)),
            "close_price": float(i.get("close_price", 0)),
            "outcome": int(float(i.get("outcome", 0))),  # 1=UP, 0=DOWN
            "pct_move": float(i.get("pct_move", 0)),
            "timestamp": float(i.get("timestamp", 0)),
        }

    print(f"  Training windows (BTC+SOL 5m): {len(training)}")

    # Step 2: Load ALL signals with ask data
    print("Loading signals...")
    sig_table = session.resource("dynamodb").Table("polymarket-bot-signals")
    sigs = []
    resp = sig_table.scan()
    sigs.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = sig_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        sigs.extend(resp.get("Items", []))

    # Group signals by slug, pick closest to 90s remaining (T+210s)
    by_slug = defaultdict(list)
    for s in sigs:
        if s.get("timeframe") != "5m":
            continue
        slug = s.get("window_slug", "")
        if not slug or not s.get("yes_ask") or not s.get("no_ask"):
            continue
        by_slug[slug].append(s)

    signals = {}
    for slug, sl in by_slug.items():
        best = min(sl, key=lambda s: abs(float(s.get("seconds_remaining", 999)) - 90))
        sr = float(best.get("seconds_remaining", 999))
        if sr > 200:
            continue  # Too far from T+210s
        signals[slug] = {
            "yes_ask": float(best.get("yes_ask", 0)),
            "no_ask": float(best.get("no_ask", 0)),
            "seconds_remaining": sr,
        }

    print(f"  Signals with asks: {len(signals)}")

    # Step 3: Match and enrich
    print("Matching training + signals...")
    windows = []
    for slug, td in training.items():
        if slug not in signals:
            continue
        sig = signals[slug]
        ya, na = sig["yes_ask"], sig["no_ask"]

        # Direction at T+210s
        if ya >= na:
            dominant_side = "YES"
            ask_at_210s = ya
        else:
            dominant_side = "NO"
            ask_at_210s = na

        # Skip if ask out of tradeable range
        if ask_at_210s < 0.50 or ask_at_210s > 0.95:
            continue

        ts = td["timestamp"]
        utc_hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
        dow = datetime.fromtimestamp(ts, tz=timezone.utc).weekday()
        weak_hours = (utc_hour < 9) or (utc_hour >= 21)

        # Outcome
        actual_up = td["outcome"] == 1
        predicted_up = dominant_side == "YES"
        won = predicted_up == actual_up

        # Sizing tiers
        ask_tier = "high" if ask_at_210s >= 0.68 else "low"
        size = 10.0 if ask_tier == "high" else 5.0
        shares = round(size / ask_at_210s)
        cost = round(shares * ask_at_210s, 2)
        pnl = round(shares * (1.0 - ask_at_210s), 2) if won else -cost

        windows.append({
            "slug": slug,
            "asset": td["asset"],
            "timestamp": ts,
            "open_price": td["open_price"],
            "close_price": td["close_price"],
            "pct_move": td["pct_move"],
            "outcome": td["outcome"],
            "yes_ask_210s": ya,
            "no_ask_210s": na,
            "dominant_side": dominant_side,
            "ask_at_210s": ask_at_210s,
            "utc_hour": utc_hour,
            "day_of_week": dow,
            "weak_hours": weak_hours,
            "ask_tier": ask_tier,
            "won": won,
            "hypothetical_size": size,
            "hypothetical_pnl": pnl,
            "seconds_remaining": sig["seconds_remaining"],
        })

    windows.sort(key=lambda x: x["timestamp"])
    print(f"  Matched windows: {len(windows)}")

    # Step 4: Store in DynamoDB
    print("Writing to historical_windows table...")
    hist_table = session.resource("dynamodb").Table("polymarket-bot-historical-windows")
    written = 0
    skipped = 0
    for w in windows:
        try:
            hist_table.put_item(
                Item={k: Decimal(str(v)) if isinstance(v, float) else v for k, v in {
                    "slug": w["slug"], "asset": w["asset"],
                    "timestamp": w["timestamp"],
                    "open_price": w["open_price"], "close_price": w["close_price"],
                    "pct_move": round(w["pct_move"], 6),
                    "outcome": w["outcome"],
                    "yes_ask_210s": round(w["yes_ask_210s"], 4),
                    "no_ask_210s": round(w["no_ask_210s"], 4),
                    "dominant_side": w["dominant_side"],
                    "ask_at_210s": round(w["ask_at_210s"], 4),
                    "utc_hour": w["utc_hour"], "day_of_week": w["day_of_week"],
                    "weak_hours": w["weak_hours"], "ask_tier": w["ask_tier"],
                    "won": w["won"],
                    "hypothetical_size": w["hypothetical_size"],
                    "hypothetical_pnl": round(w["hypothetical_pnl"], 2),
                    "source": "historical_backfill",
                }.items()},
                ConditionExpression="attribute_not_exists(slug)",
            )
            written += 1
        except Exception as e:
            if "ConditionalCheckFailedException" in type(e).__name__:
                skipped += 1
            else:
                skipped += 1
        if (written + skipped) % 100 == 0:
            print(f"  Progress: {written} written, {skipped} skipped")

    print(f"  Done: {written} written, {skipped} skipped")

    # Step 5: Full analysis
    print("\n" + "=" * 80)
    print("  HISTORICAL ANALYSIS")
    print("=" * 80)

    total_w = len([w for w in windows if w["won"]])
    total_pnl = sum(w["hypothetical_pnl"] for w in windows)
    print(f"\n  Total: {len(windows)} windows | {total_w}W/{len(windows)-total_w}L = {total_w/len(windows)*100:.1f}% WR | P&L: ${total_pnl:+.2f}")

    # Date range
    if windows:
        d1 = datetime.fromtimestamp(windows[0]["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d")
        d2 = datetime.fromtimestamp(windows[-1]["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d")
        print(f"  Date range: {d1} to {d2}")

    # 1. Win rate by ask bucket
    print(f"\n  1. WIN RATE BY ASK BUCKET:")
    print(f"  {'Bucket':<12} {'All':>20} {'BTC':>20} {'SOL':>20}")
    print(f"  {'-'*75}")
    buckets = [("$0.55-0.62", 0.55, 0.62), ("$0.62-0.68", 0.62, 0.68),
               ("$0.68-0.75", 0.68, 0.75), ("$0.75-0.82", 0.75, 0.82)]
    for label, lo, hi in buckets:
        for group_name, group in [("All", windows)] + [(a, [w for w in windows if w["asset"] == a]) for a in ["BTC", "SOL"]]:
            bt = [w for w in group if lo <= w["ask_at_210s"] < hi]
            if not bt:
                continue
            bw = len([w for w in bt if w["won"]])
            bpnl = sum(w["hypothetical_pnl"] for w in bt)
            if group_name == "All":
                print(f"  {label:<12} {bw}/{len(bt)}={bw/len(bt)*100:.0f}% ${bpnl:>+7.2f}", end="")
            else:
                if bt:
                    print(f"  {bw}/{len(bt)}={bw/len(bt)*100:.0f}% ${bpnl:>+7.2f}", end="")
                else:
                    print(f"  {'—':>20}", end="")
        print()

    # 2. Win rate by UTC hour
    print(f"\n  2. WIN RATE BY UTC HOUR:")
    print(f"  {'HR':>4} {'W':>4} {'L':>4} {'WR':>6} {'PNL':>9} {'Mode':<5}")
    by_hour = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
    for w in windows:
        h = w["utc_hour"]
        by_hour[h]["pnl"] += w["hypothetical_pnl"]
        if w["won"]:
            by_hour[h]["w"] += 1
        else:
            by_hour[h]["l"] += 1
    for h in range(24):
        d = by_hour.get(h)
        if not d or d["w"] + d["l"] == 0:
            continue
        n = d["w"] + d["l"]
        mode = "WEAK" if (h < 9 or h >= 21) else "PEAK"
        print(f"  {h:>4} {d['w']:>4} {d['l']:>4} {d['w']/n*100:>5.1f}% ${d['pnl']:>+7.2f} {mode}")

    # 3. Win rate by asset
    print(f"\n  3. WIN RATE BY ASSET:")
    for asset in ["BTC", "SOL"]:
        at = [w for w in windows if w["asset"] == asset]
        aw = len([w for w in at if w["won"]])
        apnl = sum(w["hypothetical_pnl"] for w in at)
        print(f"  {asset}: {len(at)} trades, {aw}W/{len(at)-aw}L = {aw/len(at)*100:.1f}% WR, P&L: ${apnl:+.2f}")

    # 4. With vs without time filter
    print(f"\n  4. TIME FILTER IMPACT:")
    with_filter = [w for w in windows if not (w["weak_hours"] and w["ask_at_210s"] < 0.65)]
    without = windows
    wf_w = len([w for w in with_filter if w["won"]])
    wf_pnl = sum(w["hypothetical_pnl"] for w in with_filter)
    wo_w = len([w for w in without if w["won"]])
    wo_pnl = sum(w["hypothetical_pnl"] for w in without)
    print(f"  Without filter: {len(without)} trades, {wo_w/len(without)*100:.1f}% WR, ${wo_pnl:+.2f}")
    print(f"  With filter:    {len(with_filter)} trades, {wf_w/len(with_filter)*100:.1f}% WR, ${wf_pnl:+.2f}")
    print(f"  Filter saved:   ${wf_pnl - wo_pnl:+.2f}")

    # 5. Win rate by day of week
    print(f"\n  5. WIN RATE BY DAY OF WEEK:")
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    by_dow = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0})
    for w in windows:
        d = w["day_of_week"]
        by_dow[d]["pnl"] += w["hypothetical_pnl"]
        if w["won"]:
            by_dow[d]["w"] += 1
        else:
            by_dow[d]["l"] += 1
    for d in range(7):
        dd = by_dow.get(d)
        if not dd or dd["w"] + dd["l"] == 0:
            continue
        n = dd["w"] + dd["l"]
        print(f"  {days[d]}: {n} trades, {dd['w']/n*100:.0f}% WR, ${dd['pnl']:+.2f}")

    # 6. Strategy comparison
    print(f"\n  6. STRATEGY COMPARISON:")
    print(f"  {'Strategy':<35} {'Trades':>7} {'WR':>6} {'PNL':>10}")
    print(f"  {'-'*60}")

    strategies = {
        "A) Current ($5/<$0.68, $10/>=$0.68)": windows,
        "B) High conviction only ($0.75+)": [w for w in windows if w["ask_at_210s"] >= 0.75],
        "C) BTC low only ($0.55-$0.62)": [w for w in windows if w["asset"] == "BTC" and 0.55 <= w["ask_at_210s"] < 0.62],
        "D) With time filter": with_filter,
    }
    for name, strat in strategies.items():
        if not strat:
            print(f"  {name:<35} {'0':>7}")
            continue
        sw = len([w for w in strat if w["won"]])
        sp = sum(w["hypothetical_pnl"] for w in strat)
        print(f"  {name:<35} {len(strat):>7} {sw/len(strat)*100:>5.1f}% ${sp:>+8.2f}")

    # Save to file
    os.makedirs("data", exist_ok=True)
    with open("data/historical_analysis.txt", "w") as f:
        f.write(f"Historical Analysis — {len(windows)} windows\n")
        f.write(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n")
        # Write all the analysis (simplified)
        f.write(f"\nTotal P&L: ${total_pnl:+.2f}\n")
        f.write(f"WR: {total_w/len(windows)*100:.1f}%\n")
        for name, strat in strategies.items():
            if strat:
                sw = len([w for w in strat if w["won"]])
                sp = sum(w["hypothetical_pnl"] for w in strat)
                f.write(f"{name}: {len(strat)} trades, {sw/len(strat)*100:.1f}% WR, ${sp:+.2f}\n")

    print(f"\n  Saved to data/historical_analysis.txt")


if __name__ == "__main__":
    main()
