"""Backtest alternative sizing strategies on resolved trades.

Reads all resolved trades from DynamoDB and simulates:
  a) Current: ask-based flat sizing ($5/$7.50/$10)
  b) Kelly: fractional Kelly based on lgbm_prob
  c) LightGBM-gated: only trade when lgbm_prob > 0.60, flat $10
  d) Combined: lgbm_prob > 0.60 AND ask-based sizing

Usage:
    uv run python scripts/backtest_sizing.py
"""

import sys
sys.path.insert(0, "src")

import boto3
import math
from collections import defaultdict
from datetime import datetime, timezone


def main():
    session = boto3.Session(profile_name="playground", region_name="eu-west-1")

    # Load all resolved trades
    table = session.resource("dynamodb").Table("polymarket-bot-trades")
    items = []
    resp = table.scan()
    items.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        items.extend(resp.get("Items", []))

    resolved = [t for t in items
                if int(t.get("resolved", 0)) == 1
                and t.get("outcome_source") == "polymarket_verified"
                and t.get("asset") in ("BTC", "SOL")]
    resolved.sort(key=lambda x: float(x.get("timestamp", 0)))

    print(f"Resolved verified trades: {len(resolved)}")

    # Load signals for lgbm_prob data
    sig_table = session.resource("dynamodb").Table("polymarket-bot-signals")
    sigs = []
    resp = sig_table.scan()
    sigs.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = sig_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        sigs.extend(resp.get("Items", []))

    # Map signal data by window_slug for lgbm_prob lookup
    sig_by_slug = {}
    for s in sigs:
        slug = s.get("window_slug", "")
        lgbm = float(s.get("lgbm_prob", 0) or 0)
        if slug and lgbm > 0:
            sig_by_slug[slug] = lgbm

    print(f"Signals with lgbm_prob: {len(sig_by_slug)}")

    # Strategies
    strategies = {
        "A) Current (ask-flat)": [],
        "B) Kelly (lgbm)": [],
        "C) LightGBM-gated ($10)": [],
        "D) Combined (lgbm+ask)": [],
    }

    for t in resolved:
        fill = float(t.get("fill_price", t.get("price", 0)))
        side = t.get("side", "")
        winner = t.get("polymarket_winner", "")
        won = (side == winner)
        slug = t.get("window_slug", "")
        lgbm = sig_by_slug.get(slug, 0.0)

        if fill <= 0 or fill >= 1:
            continue

        # A) Current: ask-based flat
        if fill >= 0.75:
            size_a = 10.00
        elif fill >= 0.65:
            size_a = 7.50
        else:
            size_a = 5.00
        shares_a = round(size_a / fill, 0)
        pnl_a = round(shares_a * (1 - fill), 2) if won else -round(shares_a * fill, 2)
        strategies["A) Current (ask-flat)"].append({"pnl": pnl_a, "won": won, "size": round(shares_a * fill, 2)})

        # B) Kelly: f = (p*b - q) / b where b = (1-fill)/fill, p = lgbm, q = 1-lgbm
        if lgbm > 0:
            b = (1 - fill) / fill
            q = 1 - lgbm
            kelly_f = (lgbm * b - q) / b if b > 0 else 0
            kelly_f = max(0, min(kelly_f, 0.25))  # quarter Kelly, cap at 25%
            bankroll = 500  # reference bankroll
            size_b = round(bankroll * kelly_f, 2)
            size_b = max(min(size_b, 10.00), 0)  # cap $10, floor $0
            if size_b >= 1.50:  # minimum bet
                shares_b = round(size_b / fill, 0)
                pnl_b = round(shares_b * (1 - fill), 2) if won else -round(shares_b * fill, 2)
                strategies["B) Kelly (lgbm)"].append({"pnl": pnl_b, "won": won, "size": round(shares_b * fill, 2)})

        # C) LightGBM-gated: only trade when lgbm > 0.60, flat $10
        if lgbm > 0.60:
            shares_c = round(10.00 / fill, 0)
            pnl_c = round(shares_c * (1 - fill), 2) if won else -round(shares_c * fill, 2)
            strategies["C) LightGBM-gated ($10)"].append({"pnl": pnl_c, "won": won, "size": round(shares_c * fill, 2)})

        # D) Combined: lgbm > 0.60 AND ask-based sizing
        if lgbm > 0.60:
            shares_d = round(size_a / fill, 0)
            pnl_d = round(shares_d * (1 - fill), 2) if won else -round(shares_d * fill, 2)
            strategies["D) Combined (lgbm+ask)"].append({"pnl": pnl_d, "won": won, "size": round(shares_d * fill, 2)})

    # Report
    print(f"\n{'=' * 80}")
    print(f"  SIZING STRATEGY COMPARISON — {len(resolved)} resolved trades")
    print(f"{'=' * 80}")
    print(f"  {'Strategy':<28} {'Trades':>7} {'WR':>7} {'P&L':>10} {'Avg':>8} {'Wagered':>10} {'Sharpe':>7}")
    print(f"  {'─' * 75}")

    for name, trades in strategies.items():
        if not trades:
            print(f"  {name:<28} {'0':>7} {'—':>7} {'—':>10} {'—':>8} {'—':>10} {'—':>7}")
            continue
        wins = len([t for t in trades if t["won"]])
        total_pnl = sum(t["pnl"] for t in trades)
        wr = wins / len(trades) * 100
        avg = total_pnl / len(trades)
        wagered = sum(t["size"] for t in trades)

        # Sharpe: mean(pnl) / std(pnl) * sqrt(trades_per_day)
        pnls = [t["pnl"] for t in trades]
        mean_pnl = sum(pnls) / len(pnls)
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / len(pnls) if len(pnls) > 1 else 1
        std_pnl = math.sqrt(variance)
        sharpe = (mean_pnl / std_pnl * math.sqrt(288)) if std_pnl > 0 else 0  # 288 = 5min windows per day

        print(f"  {name:<28} {len(trades):>7} {wr:>6.1f}% ${total_pnl:>+8.2f} ${avg:>+6.2f} ${wagered:>9.2f} {sharpe:>+6.2f}")

    # Detail: by ask bucket for current strategy
    print(f"\n  CURRENT STRATEGY BY ASK BUCKET:")
    print(f"  {'Bucket':<15} {'Trades':>7} {'WR':>7} {'P&L':>10} {'Avg Size':>9}")
    print(f"  {'─' * 50}")
    buckets = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0, "size": 0})
    for t in strategies["A) Current (ask-flat)"]:
        sz = t["size"]
        if sz >= 9:
            b = "$10 (high)"
        elif sz >= 6:
            b = "$7.50 (mid)"
        else:
            b = "$5 (low)"
        buckets[b]["pnl"] += t["pnl"]
        buckets[b]["size"] += t["size"]
        if t["won"]:
            buckets[b]["w"] += 1
        else:
            buckets[b]["l"] += 1

    for b in ["$5 (low)", "$7.50 (mid)", "$10 (high)"]:
        d = buckets.get(b, {"w": 0, "l": 0, "pnl": 0, "size": 0})
        n = d["w"] + d["l"]
        if n == 0:
            continue
        wr = d["w"] / n * 100
        avg_sz = d["size"] / n
        print(f"  {b:<15} {n:>7} {wr:>6.1f}% ${d['pnl']:>+8.2f} ${avg_sz:>7.2f}")

    # LightGBM coverage
    with_lgbm = sum(1 for t in resolved if sig_by_slug.get(t.get("window_slug", ""), 0) > 0)
    print(f"\n  LightGBM coverage: {with_lgbm}/{len(resolved)} trades have lgbm_prob data")
    if with_lgbm < len(resolved):
        print(f"  ⚠ {len(resolved) - with_lgbm} trades missing lgbm_prob — new logging will capture this")


if __name__ == "__main__":
    main()
