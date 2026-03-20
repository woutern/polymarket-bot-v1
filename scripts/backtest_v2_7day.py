"""Backtest: Late-entry v2 strategy over past 7 days.

Uses REAL data from DynamoDB:
  - training-data table: outcomes (open/close/direction) per 5m window
  - signals table: yes_ask/no_ask at various timestamps per window

Simulates the full v2 decision tree:
  - T+210s entry (pick signal closest to 90s remaining)
  - Follow market direction (buy whichever side has higher ask)
  - Guards: ask < $0.55 skip, ask > adaptive ceiling skip
  - Adaptive ceilings: SOL $0.82, BTC $0.78
  - Trailing-the-leader sizing: leader $20, follower $5, tied $10
  - P&L: win = shares * (1 - fill_price), loss = -size_usd

Usage:
    uv run python scripts/backtest_v2_7day.py
"""

import sys
sys.path.insert(0, "src")

import boto3
import time
import json
from collections import defaultdict
from datetime import datetime, timezone, timedelta
from decimal import Decimal


def load_data(days=7):
    """Pull training data + signals from DynamoDB."""
    session = boto3.Session(profile_name="playground", region_name="us-east-1")
    cutoff = time.time() - days * 86400

    # Training data (outcomes)
    print("Loading training data...")
    table = session.resource("dynamodb").Table("polymarket-bot-training-data")
    items = []
    resp = table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))

    training = {}
    for i in items:
        ts = float(i.get("timestamp", 0))
        if ts < cutoff or i.get("timeframe") != "5m":
            continue
        wid = i.get("window_id", "")
        asset = i.get("asset", "")
        # Build slug from window_id: e.g. "BTC_5m_btc-updown-5m-1773499500" -> "btc-updown-5m-1773499500"
        slug = wid.split("_", 2)[-1] if "_" in wid else wid
        if not slug:
            continue
        training[slug] = {
            "asset": asset,
            "open_price": float(i.get("open_price", 0)),
            "close_price": float(i.get("close_price", 0)),
            "outcome": int(float(i.get("outcome", 0))),  # 1=up, 0=down
            "pct_move": float(i.get("pct_move", 0)),
            "timestamp": ts,
        }

    print(f"  Training windows: {len(training)}")

    # Signals (ask prices)
    print("Loading signals...")
    sig_table = session.resource("dynamodb").Table("polymarket-bot-signals")
    sigs = []
    resp = sig_table.scan()
    sigs.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = sig_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        sigs.extend(resp.get("Items", []))

    # Group signals by window_slug, pick closest to 90s remaining
    by_slug = defaultdict(list)
    for s in sigs:
        ts = float(s.get("timestamp", 0))
        if ts < cutoff or s.get("timeframe") != "5m":
            continue
        slug = s.get("window_slug", "")
        if not slug or not s.get("yes_ask") or not s.get("no_ask"):
            continue
        by_slug[slug].append(s)

    signals = {}
    for slug, sig_list in by_slug.items():
        # Pick signal closest to 90s remaining (T+210s into 5min window)
        best = min(sig_list, key=lambda s: abs(float(s.get("seconds_remaining", 999)) - 90))
        sr = float(best.get("seconds_remaining", 999))
        # Accept signals within 30-150s remaining (reasonable proxy for T+210s)
        if sr < 30 or sr > 200:
            continue
        signals[slug] = {
            "yes_ask": float(best.get("yes_ask", 0)),
            "no_ask": float(best.get("no_ask", 0)),
            "direction": best.get("direction", ""),
            "seconds_remaining": sr,
            "market_price": float(best.get("market_price", 0)),
            "pct_move": float(best.get("pct_move", 0)),
            "asset": best.get("asset", ""),
        }

    print(f"  Signal windows (30-200s remaining): {len(signals)}")

    return training, signals


def simulate_v2(training, signals):
    """Simulate the v2 strategy on historical data."""

    # Match windows that have BOTH training data (outcome) and signal data (asks)
    matched = []
    for slug in signals:
        if slug in training:
            t = training[slug]
            s = signals[slug]
            # Only BTC and SOL (ETH disabled in v2)
            if t["asset"] not in ("BTC", "SOL"):
                continue
            matched.append({**t, **s, "slug": slug})

    matched.sort(key=lambda x: x["timestamp"])
    print(f"\nMatched windows (BTC+SOL, with asks): {len(matched)}")

    # Group by window open_ts (5min aligned) for trailing-the-leader
    # Windows at the same timestamp across assets are concurrent
    by_ts = defaultdict(list)
    for m in matched:
        # Extract timestamp from slug: btc-updown-5m-XXXXX
        parts = m["slug"].rsplit("-", 1)
        window_ts = int(parts[-1]) if len(parts) == 2 and parts[-1].isdigit() else int(m["timestamp"])
        m["window_ts"] = window_ts
        by_ts[window_ts].append(m)

    # Simulate
    trades = []
    daily_pnl = defaultdict(float)
    skipped = defaultdict(int)
    total_windows = 0

    for window_ts in sorted(by_ts.keys()):
        windows = by_ts[window_ts]
        total_windows += len(windows)

        # Phase 1: evaluate all assets, determine asks
        evals = []
        for w in windows:
            yes_ask = w["yes_ask"]
            no_ask = w["no_ask"]

            # Direction: follow higher ask
            if yes_ask >= no_ask:
                direction_up = True
                current_ask = yes_ask
            else:
                direction_up = False
                current_ask = no_ask

            # Adaptive ceiling
            max_ask = 0.82 if w["asset"] == "SOL" else 0.78

            # Guards
            skip_reason = ""
            if current_ask < 0.55:
                skip_reason = "no_conviction"
            elif current_ask > max_ask:
                skip_reason = "fully_priced"

            evals.append({
                **w,
                "direction_up": direction_up,
                "current_ask": current_ask,
                "max_ask": max_ask,
                "skip_reason": skip_reason,
            })

        # Phase 2: trailing-the-leader sizing
        active_evals = [e for e in evals if not e["skip_reason"]]
        ask_by_asset = {e["asset"]: e["current_ask"] for e in active_evals}

        for e in evals:
            if e["skip_reason"]:
                skipped[e["skip_reason"]] += 1
                continue

            my_ask = e["current_ask"]
            other_asks = [v for k, v in ask_by_asset.items() if k != e["asset"]]
            best_other = max(other_asks) if other_asks else 0

            if best_other > 0 and abs(my_ask - best_other) <= 0.03:
                size = 10.00  # tied
                role = "tied"
            elif my_ask >= best_other:
                size = 20.00  # leader
                role = "leader"
            else:
                size = 5.00   # follower
                role = "follower"

            # Outcome
            actual_up = (e["outcome"] == 1)
            predicted_up = e["direction_up"]
            won = (predicted_up == actual_up)

            # P&L calculation (same as live_trader)
            fill_price = round(e["current_ask"], 2)
            if fill_price <= 0 or fill_price >= 1:
                skipped["bad_price"] += 1
                continue
            shares = round(size / fill_price, 0)
            if shares < 1:
                shares = 1.0
            actual_cost = round(shares * fill_price, 2)

            if won:
                pnl = round(shares * (1.0 - fill_price), 2)
            else:
                pnl = -actual_cost

            day = datetime.fromtimestamp(e["timestamp"], tz=timezone.utc).strftime("%Y-%m-%d")
            daily_pnl[day] += pnl

            trades.append({
                "timestamp": e["timestamp"],
                "day": day,
                "asset": e["asset"],
                "slug": e["slug"],
                "direction": "UP" if predicted_up else "DOWN",
                "actual": "UP" if actual_up else "DOWN",
                "won": won,
                "fill_price": fill_price,
                "size_usd": actual_cost,
                "shares": shares,
                "pnl": pnl,
                "role": role,
                "yes_ask": round(e["yes_ask"], 3),
                "no_ask": round(e["no_ask"], 3),
                "seconds_remaining": e["seconds_remaining"],
            })

    return trades, daily_pnl, skipped, total_windows


def print_report(trades, daily_pnl, skipped, total_windows):
    """Print comprehensive backtest report."""
    if not trades:
        print("\nNo trades to report!")
        return

    wins = [t for t in trades if t["won"]]
    losses = [t for t in trades if not t["won"]]
    total_pnl = sum(t["pnl"] for t in trades)
    wr = len(wins) / len(trades) * 100

    print(f"\n{'='*70}")
    print(f"  LATE-ENTRY V2 BACKTEST — PAST 7 DAYS")
    print(f"  Strategy: T+210s momentum follow + adaptive ceilings + leader sizing")
    print(f"  Assets: BTC + SOL only | Ceilings: BTC $0.78, SOL $0.82")
    print(f"  Sizing: Leader $20, Follower $5, Tied $10")
    print(f"{'='*70}")

    print(f"\n  SUMMARY")
    print(f"  {'─'*50}")
    print(f"  Total windows evaluated:  {total_windows}")
    print(f"  Trades executed:          {len(trades)}")
    print(f"  Skipped:                  {sum(skipped.values())}")
    for reason, count in sorted(skipped.items(), key=lambda x: -x[1]):
        print(f"    {reason}: {count}")
    print(f"  Win / Loss:               {len(wins)} / {len(losses)}")
    print(f"  Win rate:                 {wr:.1f}%")
    print(f"  Total P&L:                ${total_pnl:+.2f}")
    print(f"  Avg trade P&L:            ${total_pnl / len(trades):+.2f}")
    print(f"  Total wagered:            ${sum(t['size_usd'] for t in trades):.2f}")

    # By asset
    print(f"\n  BY ASSET")
    print(f"  {'─'*50}")
    print(f"  {'ASSET':<6} {'W':>5} {'L':>5} {'WR':>7} {'PNL':>10} {'AVG':>8} {'TRADES':>7}")
    for asset in ["BTC", "SOL"]:
        at = [t for t in trades if t["asset"] == asset]
        if not at:
            continue
        aw = len([t for t in at if t["won"]])
        al = len(at) - aw
        apnl = sum(t["pnl"] for t in at)
        awr = aw / len(at) * 100
        print(f"  {asset:<6} {aw:>5} {al:>5} {awr:>6.1f}% ${apnl:>+8.2f} ${apnl/len(at):>+6.2f} {len(at):>7}")

    # By role (leader/follower/tied)
    print(f"\n  BY SIZING ROLE")
    print(f"  {'─'*50}")
    print(f"  {'ROLE':<10} {'W':>5} {'L':>5} {'WR':>7} {'PNL':>10} {'SIZE':>7}")
    for role in ["leader", "follower", "tied"]:
        rt = [t for t in trades if t["role"] == role]
        if not rt:
            continue
        rw = len([t for t in rt if t["won"]])
        rl = len(rt) - rw
        rpnl = sum(t["pnl"] for t in rt)
        rwr = rw / len(rt) * 100
        avg_size = sum(t["size_usd"] for t in rt) / len(rt)
        print(f"  {role:<10} {rw:>5} {rl:>5} {rwr:>6.1f}% ${rpnl:>+8.2f} ${avg_size:>5.0f}")

    # By ask price bucket
    print(f"\n  BY FILL PRICE")
    print(f"  {'─'*50}")
    print(f"  {'PRICE':<12} {'W':>5} {'L':>5} {'WR':>7} {'PNL':>10} {'N':>5}")
    buckets = [
        ("$0.55-0.60", 0.55, 0.60),
        ("$0.60-0.65", 0.60, 0.65),
        ("$0.65-0.70", 0.65, 0.70),
        ("$0.70-0.75", 0.70, 0.75),
        ("$0.75-0.82", 0.75, 0.82),
    ]
    for label, lo, hi in buckets:
        bt = [t for t in trades if lo <= t["fill_price"] < hi]
        if not bt:
            continue
        bw = len([t for t in bt if t["won"]])
        bl = len(bt) - bw
        bpnl = sum(t["pnl"] for t in bt)
        bwr = bw / len(bt) * 100
        print(f"  {label:<12} {bw:>5} {bl:>5} {bwr:>6.1f}% ${bpnl:>+8.2f} {len(bt):>5}")

    # Daily P&L
    print(f"\n  DAILY P&L")
    print(f"  {'─'*50}")
    print(f"  {'DATE':<12} {'PNL':>10} {'TRADES':>8} {'WR':>7} {'CUM':>10}")
    cum = 0
    for day in sorted(daily_pnl.keys()):
        dt = [t for t in trades if t["day"] == day]
        dw = len([t for t in dt if t["won"]])
        dwr = dw / len(dt) * 100 if dt else 0
        dpnl = daily_pnl[day]
        cum += dpnl
        bar = "█" * max(1, int(abs(dpnl) / 5))
        sign = "+" if dpnl >= 0 else "-"
        print(f"  {day:<12} ${dpnl:>+8.2f}  {len(dt):>6}  {dwr:>5.1f}% ${cum:>+8.2f}  {'+' if dpnl >= 0 else ''}{bar}")

    # Equity curve stats
    cum = 0
    peak = 0
    max_dd = 0
    for t in trades:
        cum += t["pnl"]
        peak = max(peak, cum)
        dd = peak - cum
        max_dd = max(max_dd, dd)

    print(f"\n  RISK METRICS")
    print(f"  {'─'*50}")
    print(f"  Max drawdown:      ${max_dd:.2f}")
    print(f"  Peak equity:       ${peak:+.2f}")
    print(f"  Final equity:      ${cum:+.2f}")
    avg_win = sum(t["pnl"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl"] for t in losses) / len(losses) if losses else 0
    print(f"  Avg win:           ${avg_win:+.2f}")
    print(f"  Avg loss:          ${avg_loss:+.2f}")
    if avg_loss != 0:
        print(f"  Win/Loss ratio:    {abs(avg_win/avg_loss):.2f}")
    # Profit factor
    gross_profit = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    if gross_loss > 0:
        print(f"  Profit factor:     {gross_profit / gross_loss:.2f}")

    # By hour
    print(f"\n  BY HOUR (UTC)")
    print(f"  {'─'*50}")
    print(f"  {'HOUR':>6} {'W':>5} {'L':>5} {'WR':>7} {'PNL':>10}")
    hour_stats = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
    for t in trades:
        h = datetime.fromtimestamp(t["timestamp"], tz=timezone.utc).hour
        if t["won"]:
            hour_stats[h]["w"] += 1
        else:
            hour_stats[h]["l"] += 1
        hour_stats[h]["pnl"] += t["pnl"]
    for h in sorted(hour_stats.keys()):
        s = hour_stats[h]
        n = s["w"] + s["l"]
        if n < 3:
            continue
        hwr = s["w"] / n * 100
        print(f"  {h:>4}:00 {s['w']:>5} {s['l']:>5} {hwr:>6.1f}% ${s['pnl']:>+8.2f}")

    # Comparison: flat $10 sizing vs trailing-the-leader
    print(f"\n  COMPARISON: FLAT $10 vs TRAILING-THE-LEADER")
    print(f"  {'─'*50}")
    flat_pnl = 0
    for t in trades:
        fill = t["fill_price"]
        shares_flat = round(10.0 / fill, 0)
        if shares_flat < 1:
            shares_flat = 1
        cost_flat = round(shares_flat * fill, 2)
        if t["won"]:
            flat_pnl += round(shares_flat * (1.0 - fill), 2)
        else:
            flat_pnl -= cost_flat
    print(f"  Flat $10/trade P&L:     ${flat_pnl:+.2f}")
    print(f"  Leader sizing P&L:      ${total_pnl:+.2f}")
    print(f"  Sizing edge:            ${total_pnl - flat_pnl:+.2f}")

    # Worst trades
    print(f"\n  WORST 5 TRADES")
    print(f"  {'─'*50}")
    worst = sorted(trades, key=lambda t: t["pnl"])[:5]
    for t in worst:
        dt = datetime.fromtimestamp(t["timestamp"], tz=timezone.utc).strftime("%m/%d %H:%M")
        print(f"  {dt}  {t['asset']:<4} {t['direction']:<5} ask={t['fill_price']:.2f}  "
              f"${t['size_usd']:.0f} {t['role']:<8} PNL=${t['pnl']:+.2f}  {t['slug'][-15:]}")

    # Best trades
    print(f"\n  BEST 5 TRADES")
    print(f"  {'─'*50}")
    best = sorted(trades, key=lambda t: -t["pnl"])[:5]
    for t in best:
        dt = datetime.fromtimestamp(t["timestamp"], tz=timezone.utc).strftime("%m/%d %H:%M")
        print(f"  {dt}  {t['asset']:<4} {t['direction']:<5} ask={t['fill_price']:.2f}  "
              f"${t['size_usd']:.0f} {t['role']:<8} PNL=${t['pnl']:+.2f}  {t['slug'][-15:]}")


def main():
    training, signals = load_data(days=7)
    trades, daily_pnl, skipped, total_windows = simulate_v2(training, signals)
    print_report(trades, daily_pnl, skipped, total_windows)


if __name__ == "__main__":
    main()
