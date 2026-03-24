"""Analyze wasted capital from holding expensive losing shares to resolution.

Usage:
    PYTHONPATH=src uv run python3 scripts/analyze_wasted_holds.py
"""

import json
import subprocess
import sys


def get_committed_windows():
    """Pull all v2_committed events from the current task's logs."""
    # Find current task
    result = subprocess.run(
        [
            "aws",
            "ecs",
            "list-tasks",
            "--cluster",
            "polymarket-bot",
            "--service-name",
            "polymarket-bot-service",
            "--desired-status",
            "RUNNING",
            "--profile",
            "playground",
            "--region",
            "eu-west-1",
            "--query",
            "taskArns[0]",
            "--output",
            "text",
        ],
        capture_output=True,
        text=True,
    )
    task_arn = result.stdout.strip()
    if not task_arn or task_arn == "None":
        print("No running task found")
        sys.exit(1)

    task_id = task_arn.split("/")[-1]
    print(f"Task: {task_id}")

    # Pull committed events
    result = subprocess.run(
        [
            "aws",
            "logs",
            "filter-log-events",
            "--log-group-name",
            "/polymarket-bot",
            "--log-stream-names",
            f"polybot/polymarket-bot/{task_id}",
            "--filter-pattern",
            "v2_committed",
            "--profile",
            "playground",
            "--region",
            "eu-west-1",
            "--query",
            "events[*].message",
            "--output",
            "text",
        ],
        capture_output=True,
        text=True,
    )

    windows = []
    for line in result.stdout.split("\t"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            if d.get("asset") != "BTC":
                continue
            if d.get("seconds", 0) < 250:
                continue
            windows.append(d)
        except (json.JSONDecodeError, KeyError):
            continue

    return windows


def analyze(windows):
    print(f"\nTotal committed BTC 5m windows: {len(windows)}\n")

    if not windows:
        print("No windows to analyze.")
        return

    # === Overall P&L summary ===
    print("=" * 80)
    print("  OVERALL POSITION SUMMARY")
    print("=" * 80)

    total_cost = 0
    guaranteed_profit = 0
    one_sided = 0
    tiny = 0

    for w in windows:
        up = w.get("up_shares", 0)
        dn = w.get("down_shares", 0)
        up_avg = w.get("up_avg", 0)
        dn_avg = w.get("down_avg", 0)
        net = round(up * up_avg + dn * dn_avg, 2)
        pnl_up = round(up - net, 2)
        pnl_dn = round(dn - net, 2)
        total_cost += net

        if pnl_up > 0 and pnl_dn > 0:
            guaranteed_profit += 1
        if up == 0 or dn == 0:
            one_sided += 1
        if net < 10:
            tiny += 1

    print(f"  Windows:              {len(windows)}")
    print(f"  Total cost deployed:  ${total_cost:.1f}")
    print(f"  Avg cost per window:  ${total_cost / len(windows):.1f}")
    print(
        f"  Guaranteed profit:    {guaranteed_profit}/{len(windows)} ({guaranteed_profit / len(windows) * 100:.1f}%)"
    )
    print(
        f"  One-sided (0 on one): {one_sided}/{len(windows)} ({one_sided / len(windows) * 100:.1f}%)"
    )
    print(
        f"  Tiny (<$10 deployed): {tiny}/{len(windows)} ({tiny / len(windows) * 100:.1f}%)"
    )

    # === Wasted expensive holds ===
    print()
    print("=" * 80)
    print("  WASTED CAPITAL: EXPENSIVE SHARES HELD TO RESOLUTION")
    print("=" * 80)
    print()
    print("These are shares bought above 50c on the minority side.")
    print("If the majority side wins, these go to $0.")
    print("If we had sold them mid-window at ~35c, we'd recover capital.")
    print()

    total_wasted_cost = 0
    total_could_recover = 0
    cases = 0

    for w in windows:
        up = w.get("up_shares", 0)
        dn = w.get("down_shares", 0)
        up_avg = w.get("up_avg", 0)
        dn_avg = w.get("down_avg", 0)
        ts = w.get("timestamp", "")[:19]

        if up == 0 or dn == 0:
            continue

        # Find the expensive side (higher avg = likely overpaid)
        if up_avg > dn_avg:
            exp_side = "UP"
            exp_shares = up
            exp_avg = up_avg
            cheap_side = "DOWN"
            cheap_shares = dn
            cheap_avg = dn_avg
        else:
            exp_side = "DOWN"
            exp_shares = dn
            exp_avg = dn_avg
            cheap_side = "UP"
            cheap_shares = up
            cheap_avg = up_avg

        if exp_avg <= 0.50:
            continue

        exp_cost = round(exp_shares * exp_avg, 2)
        could_recover = round(exp_shares * 0.35, 2)
        wasted = round(exp_cost - could_recover, 2)
        total_wasted_cost += exp_cost
        total_could_recover += could_recover
        cases += 1

        print(
            f"  {ts}  Held {exp_shares:3d} {exp_side:4s} @ {exp_avg:.2f}c "
            f"cost=${exp_cost:6.1f}  |  if sold at 35c: recover ${could_recover:5.1f}  "
            f"save ${wasted:5.1f}"
        )

    print()
    print(f"  Cases where we held expensive shares: {cases}")
    print(f"  Total cost of expensive holds:        ${total_wasted_cost:.1f}")
    print(f"  Could have recovered (sell at 35c):   ${total_could_recover:.1f}")
    print(
        f"  Total potential savings:               ${total_wasted_cost - total_could_recover:.1f}"
    )
    print(
        f"  At 36% model-wrong rate, actual waste: ${(total_wasted_cost - total_could_recover) * 0.36:.1f}"
    )

    # === Windows where we should have sold earlier ===
    print()
    print("=" * 80)
    print("  SPECIFIC IMPROVEMENTS")
    print("=" * 80)
    print()

    # Count windows where expensive side had > 10 shares at commit
    big_expensive = 0
    big_expensive_cost = 0
    for w in windows:
        up = w.get("up_shares", 0)
        dn = w.get("down_shares", 0)
        up_avg = w.get("up_avg", 0)
        dn_avg = w.get("down_avg", 0)

        if up == 0 or dn == 0:
            continue

        if up_avg > dn_avg and up_avg > 0.50 and up > 10:
            big_expensive += 1
            big_expensive_cost += round(up * up_avg, 2)
        elif dn_avg > up_avg and dn_avg > 0.50 and dn > 10:
            big_expensive += 1
            big_expensive_cost += round(dn * dn_avg, 2)

    print(f"  Windows with >10 expensive shares held: {big_expensive}")
    print(f"  Total cost in those holds: ${big_expensive_cost:.1f}")
    print()
    print("  Recommendations:")
    print("  1. Sell expensive unfavored side earlier (T+60-120) when avg > 50c")
    print("  2. The UNFAVORED_RICH trigger (avg > 55c) should be lowered to 50c")
    print("  3. Late dump threshold (bid < 10c) should be raised to bid < 25c")
    print("  4. Consider selling ALL unfavored shares above 45c when model edge > 0.15")

    # === Combined avg analysis ===
    print()
    print("=" * 80)
    print("  COMBINED AVG DISTRIBUTION")
    print("=" * 80)
    print()

    buckets = {
        "<0.80": 0,
        "0.80-0.90": 0,
        "0.90-0.95": 0,
        "0.95-1.00": 0,
        "1.00-1.05": 0,
        ">1.05": 0,
    }
    for w in windows:
        combined = w.get("combined_avg", 0)
        if combined == 0:
            continue
        if combined < 0.80:
            buckets["<0.80"] += 1
        elif combined < 0.90:
            buckets["0.80-0.90"] += 1
        elif combined < 0.95:
            buckets["0.90-0.95"] += 1
        elif combined < 1.00:
            buckets["0.95-1.00"] += 1
        elif combined < 1.05:
            buckets["1.00-1.05"] += 1
        else:
            buckets[">1.05"] += 1

    for bucket, count in buckets.items():
        bar = "#" * count
        label = (
            "PROFIT"
            if bucket in ("<0.80", "0.80-0.90", "0.90-0.95", "0.95-1.00")
            else "LOSS"
        )
        print(f"  {bucket:12s} {count:3d} {bar}  [{label}]")


if __name__ == "__main__":
    windows = get_committed_windows()
    analyze(windows)
