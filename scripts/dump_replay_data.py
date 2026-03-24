"""Dump execution tick data from CloudWatch for replay simulation.

Usage:
    PYTHONPATH=src uv run python3 scripts/dump_replay_data.py
"""

import json
import os
import subprocess
import sys
from collections import defaultdict


def get_task_log_streams(max_streams=10):
    """Get recent log stream names for the polymarket bot."""
    result = subprocess.run(
        [
            "aws",
            "logs",
            "describe-log-streams",
            "--log-group-name",
            "/polymarket-bot",
            "--order-by",
            "LastEventTime",
            "--descending",
            "--max-items",
            str(max_streams),
            "--profile",
            "playground",
            "--region",
            "eu-west-1",
            "--query",
            "logStreams[*].logStreamName",
            "--output",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        streams = json.loads(result.stdout)
        return [s for s in streams if "polymarket-bot" in s]
    except (json.JSONDecodeError, TypeError):
        return []


def fetch_ticks_from_stream(stream_name, event_filter="v2_execution_tick"):
    """Fetch all matching events from a CloudWatch log stream."""
    ticks = []
    result = subprocess.run(
        [
            "aws",
            "logs",
            "filter-log-events",
            "--log-group-name",
            "/polymarket-bot",
            "--log-stream-names",
            stream_name,
            "--filter-pattern",
            event_filter,
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
        timeout=120,
    )
    for line in result.stdout.split("\t"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            if d.get("event") == event_filter and d.get("asset") == "BTC":
                ticks.append(d)
        except (json.JSONDecodeError, KeyError):
            pass
    return ticks


def fetch_fills_from_stream(stream_name):
    """Fetch all fill events from a log stream."""
    fills = []
    result = subprocess.run(
        [
            "aws",
            "logs",
            "filter-log-events",
            "--log-group-name",
            "/polymarket-bot",
            "--log-stream-names",
            stream_name,
            "--filter-pattern",
            "v2_fill_detected",
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
        timeout=120,
    )
    for line in result.stdout.split("\t"):
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            if d.get("event") == "v2_fill_detected" and d.get("asset") == "BTC":
                fills.append(d)
        except (json.JSONDecodeError, KeyError):
            pass
    return fills


def fetch_sells_from_stream(stream_name):
    """Fetch all sell events from a log stream."""
    sells = []
    for pattern in [
        "early_sell_filled",
        "early_sell_gtc_filled",
        "v2_late_dump",
        "v2_sell_rebuy",
    ]:
        result = subprocess.run(
            [
                "aws",
                "logs",
                "filter-log-events",
                "--log-group-name",
                "/polymarket-bot",
                "--log-stream-names",
                stream_name,
                "--filter-pattern",
                pattern,
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
            timeout=60,
        )
        for line in result.stdout.split("\t"):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if d.get("asset") == "BTC":
                    sells.append(d)
            except (json.JSONDecodeError, KeyError):
                pass
    return sells


def group_into_windows(ticks):
    """Group execution ticks into 5-minute windows based on seconds resetting."""
    windows = []
    current_window = []
    prev_seconds = 999

    for tick in sorted(ticks, key=lambda t: t.get("timestamp", "")):
        seconds = tick.get("seconds", 0)
        # New window detected when seconds drops back to near zero
        if seconds < prev_seconds - 30 and current_window:
            windows.append(current_window)
            current_window = []
        current_window.append(tick)
        prev_seconds = seconds

    if current_window:
        windows.append(current_window)

    return windows


def analyze_window(ticks):
    """Analyze a single window's tick data and produce a summary."""
    if not ticks:
        return None

    first = ticks[0]
    last = ticks[-1]

    # Find the tick with highest seconds (closest to commit)
    final_tick = max(ticks, key=lambda t: t.get("seconds", 0))

    up_shares = final_tick.get("up_shares", 0)
    dn_shares = final_tick.get("down_shares", 0)
    up_avg = final_tick.get("up_avg", 0)
    dn_avg = final_tick.get("down_avg", 0)
    combined = final_tick.get("combined_avg", 0)
    net_cost = final_tick.get("net_cost", 0)
    remaining = final_tick.get("remaining_budget", 0)

    pnl_up = round(up_shares - net_cost, 2) if up_shares > 0 else round(-net_cost, 2)
    pnl_dn = round(dn_shares - net_cost, 2) if dn_shares > 0 else round(-net_cost, 2)
    gp = pnl_up > 0 and pnl_dn > 0

    # Track market direction over time (yes_bid trajectory)
    # We don't have yes_bid in execution_tick directly, but we can infer
    # from up_avg and down_avg trends

    # Count sells
    sells_fired = sum(1 for t in ticks if t.get("sell_fired"))
    sell_reasons = [t.get("sell_reason", "") for t in ticks if t.get("sell_fired")]

    # Count guard blocks
    total_guard = sum(t.get("pair_guard_skipped", 0) for t in ticks)

    # Budget deployment over time
    deployment_curve = []
    for t in ticks:
        sec = t.get("seconds", 0)
        if sec % 30 == 0 or sec <= 10 or sec >= 240:
            deployment_curve.append(
                {
                    "seconds": sec,
                    "net_cost": t.get("net_cost", 0),
                    "remaining": t.get("remaining_budget", 0),
                    "up_shares": t.get("up_shares", 0),
                    "down_shares": t.get("down_shares", 0),
                    "combined_avg": t.get("combined_avg", 0),
                    "prob_up": t.get("prob_up", 0),
                    "sell_fired": t.get("sell_fired", False),
                    "sell_reason": t.get("sell_reason", ""),
                    "pair_guard_skipped": t.get("pair_guard_skipped", 0),
                }
            )

    return {
        "timestamp": first.get("timestamp", "")[:19],
        "ticks": len(ticks),
        "final_seconds": final_tick.get("seconds", 0),
        "up_shares": up_shares,
        "up_avg": round(up_avg, 3),
        "down_shares": dn_shares,
        "down_avg": round(dn_avg, 3),
        "combined_avg": round(combined, 3),
        "net_cost": round(net_cost, 2),
        "remaining_budget": round(remaining, 2),
        "budget_deployed_pct": round(
            (100 - remaining) if remaining < 100 else net_cost, 1
        ),
        "pnl_if_up": pnl_up,
        "pnl_if_dn": pnl_dn,
        "guaranteed_profit": gp,
        "sells_fired": sells_fired,
        "sell_reasons": sell_reasons,
        "total_guard_blocks": total_guard,
        "deployment_curve": deployment_curve,
    }


def build_replay_dataset(ticks):
    """Build a replay dataset: for each window, the sequence of market states
    that the bot saw. This allows re-running different strategies against
    the same market data."""

    windows = group_into_windows(ticks)
    replay_windows = []

    for window_ticks in windows:
        if len(window_ticks) < 5:
            continue

        # Each tick represents one second of market state
        market_states = []
        for tick in window_ticks:
            market_states.append(
                {
                    "seconds": tick.get("seconds", 0),
                    "prob_up": tick.get("prob_up", 0.5),
                    "up_pct": tick.get("up_pct", 0.5),
                    "down_pct": tick.get("down_pct", 0.5),
                    "combined_avg": tick.get("combined_avg", 0),
                    "up_avg": tick.get("up_avg", 0),
                    "down_avg": tick.get("down_avg", 0),
                    "up_shares": tick.get("up_shares", 0),
                    "down_shares": tick.get("down_shares", 0),
                    "net_cost": tick.get("net_cost", 0),
                    "remaining_budget": tick.get("remaining_budget", 0),
                    "sell_fired": tick.get("sell_fired", False),
                    "sell_reason": tick.get("sell_reason", ""),
                    "pair_guard_skipped": tick.get("pair_guard_skipped", 0),
                    "budget_scale": tick.get("budget_scale", 1.0),
                    "budget_curve_pct": tick.get("budget_curve_pct", 0),
                    "posted_up": tick.get("posted_up", 0),
                    "posted_down": tick.get("posted_down", 0),
                    "hard_cap_skipped": tick.get("hard_cap_skipped", 0),
                    "stale_orders_cancelled": tick.get("stale_orders_cancelled", 0),
                    "payout_floor": tick.get("payout_floor", 0),
                    "cost_above_floor": tick.get("cost_above_floor", 0),
                }
            )

        first_tick = window_ticks[0]
        final_tick = max(window_ticks, key=lambda t: t.get("seconds", 0))

        # Infer yes_bid and no_bid from the data we have
        # up_avg increases when we buy UP, and we buy at bid
        # So the bid at any time is approximately the price of recent fills
        # We'll use a simple heuristic: if up_avg exists, bid was near there

        replay_windows.append(
            {
                "timestamp": first_tick.get("timestamp", "")[:19],
                "ticks": len(market_states),
                "market_states": market_states,
                "actual_final": {
                    "up_shares": final_tick.get("up_shares", 0),
                    "down_shares": final_tick.get("down_shares", 0),
                    "up_avg": final_tick.get("up_avg", 0),
                    "down_avg": final_tick.get("down_avg", 0),
                    "combined_avg": final_tick.get("combined_avg", 0),
                    "net_cost": final_tick.get("net_cost", 0),
                },
            }
        )

    return replay_windows


def main():
    os.makedirs("data", exist_ok=True)

    print("Fetching log streams...")
    streams = get_task_log_streams(max_streams=10)
    print(f"Found {len(streams)} streams")

    all_ticks = []
    all_fills = []
    all_sells = []

    for stream in streams:
        task_id = stream.split("/")[-1][:12]
        print(f"\nFetching from {task_id}...")

        ticks = fetch_ticks_from_stream(stream)
        print(f"  Execution ticks: {len(ticks)}")
        all_ticks.extend(ticks)

        fills = fetch_fills_from_stream(stream)
        print(f"  Fill events: {len(fills)}")
        all_fills.extend(fills)

        sells = fetch_sells_from_stream(stream)
        print(f"  Sell events: {len(sells)}")
        all_sells.extend(sells)

    print(f"\n{'=' * 60}")
    print(f"Total BTC execution ticks: {len(all_ticks)}")
    print(f"Total BTC fills: {len(all_fills)}")
    print(f"Total BTC sells: {len(all_sells)}")

    # Save raw data
    with open("data/replay_ticks.json", "w") as f:
        json.dump(all_ticks, f, indent=2)
    print(f"\nSaved raw ticks to data/replay_ticks.json")

    with open("data/replay_fills.json", "w") as f:
        json.dump(all_fills, f, indent=2)
    print(f"Saved fills to data/replay_fills.json")

    with open("data/replay_sells.json", "w") as f:
        json.dump(all_sells, f, indent=2)
    print(f"Saved sells to data/replay_sells.json")

    # Group into windows and analyze
    windows = group_into_windows(all_ticks)
    print(f"\nGrouped into {len(windows)} windows")

    # Analyze each window
    summaries = []
    for window_ticks in windows:
        summary = analyze_window(window_ticks)
        if summary:
            summaries.append(summary)

    with open("data/replay_window_summaries.json", "w") as f:
        json.dump(summaries, f, indent=2)
    print(
        f"Saved {len(summaries)} window summaries to data/replay_window_summaries.json"
    )

    # Build replay dataset
    replay_data = build_replay_dataset(all_ticks)
    with open("data/replay_dataset.json", "w") as f:
        json.dump(replay_data, f, indent=2)
    print(f"Saved {len(replay_data)} replay windows to data/replay_dataset.json")

    # Print summary table
    print(f"\n{'=' * 60}")
    print(f"  WINDOW SUMMARY")
    print(f"{'=' * 60}")

    total_pnl_gp = 0
    gp_count = 0
    dir_count = 0

    for s in summaries:
        gp = "GP" if s["guaranteed_profit"] else "DIR"
        if s["guaranteed_profit"]:
            gp_count += 1
            total_pnl_gp += min(s["pnl_if_up"], s["pnl_if_dn"])
        else:
            dir_count += 1

        print(
            f"  {s['timestamp']} "
            f"UP:{s['up_shares']:3d}@{s['up_avg']:.2f} "
            f"DN:{s['down_shares']:3d}@{s['down_avg']:.2f} "
            f"comb={s['combined_avg']:.3f} "
            f"net=${s['net_cost']:6.1f} "
            f"{gp:3s} "
            f"sells={s['sells_fired']} "
            f"guard={s['total_guard_blocks']:3d} "
            f"if_UP=${s['pnl_if_up']:+7.1f} "
            f"if_DN=${s['pnl_if_dn']:+7.1f}"
        )

    print(f"\n  Windows: {len(summaries)}")
    print(
        f"  Guaranteed profit: {gp_count}/{len(summaries)} ({gp_count / len(summaries) * 100:.0f}%)"
        if summaries
        else ""
    )
    print(f"  GP minimum profit: ${total_pnl_gp:.1f}")
    print(f"  Directional: {dir_count}/{len(summaries)}")

    if summaries:
        avg_deployed = sum(s["net_cost"] for s in summaries) / len(summaries)
        avg_guard = sum(s["total_guard_blocks"] for s in summaries) / len(summaries)
        total_sells = sum(s["sells_fired"] for s in summaries)
        print(f"  Avg deployed: ${avg_deployed:.1f}")
        print(f"  Avg guard blocks/window: {avg_guard:.0f}")
        print(f"  Total sells across all windows: {total_sells}")

    print(f"\nReplay dataset ready in data/replay_dataset.json")
    print(f"Use this with scripts/replay_simulator.py to test new strategies.")


if __name__ == "__main__":
    main()
