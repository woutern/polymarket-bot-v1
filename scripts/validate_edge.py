"""Step 7 Checkpoint: Validate base rates have edge (>55%).

Loads historical candles, builds base rate table, and prints
P(up) for various (pct_move, seconds_remaining) combinations.
"""

from __future__ import annotations

import sys
sys.path.insert(0, "src")

from pathlib import Path
from polybot.strategy.base_rate import BaseRateTable


def main():
    parquet_path = Path("data/candles/btc_usd_1min.parquet")
    if not parquet_path.exists():
        print("No candle data found. Run backfill_coinbase.py first.")
        return

    print("Loading candle data...")
    table = BaseRateTable()
    table.load_from_parquet(str(parquet_path))

    print(f"\nTotal bins with >=20 samples: {len([b for b in table.bins.values() if b.total >= 20])}")
    print(f"Total bins: {len(table.bins)}")

    # Print full summary
    print("\n{'='*70}")
    print("BASE RATE TABLE: P(BTC finishes UP | move X% at T-N seconds)")
    print("="*70)
    print(f"{'Move Bin':>12} {'T-secs':>8} {'P(UP)':>8} {'Count':>8} {'Edge?':>8}")
    print("-"*50)

    has_edge = False
    edge_bins = []

    for entry in table.summary():
        p = entry["p_up"]
        edge = ""
        if p > 0.55:
            edge = "UP ✓"
            has_edge = True
            edge_bins.append(entry)
        elif p < 0.45:
            edge = "DOWN ✓"
            has_edge = True
            edge_bins.append(entry)

        print(f"{entry['pct_move_min']:>12.2f} {entry['seconds_remaining']:>8} {p:>8.4f} {entry['total']:>8} {edge:>8}")

    # Specific lookups for our trading parameters
    print("\n" + "="*70)
    print("KEY LOOKUPS (our trading conditions)")
    print("="*70)

    for move in [0.15, 0.20, 0.30, 0.50, -0.15, -0.20, -0.30, -0.50]:
        for secs in [10, 15, 20, 30]:
            p = table.lookup(move, secs)
            marker = " ← EDGE" if (move > 0 and p > 0.55) or (move < 0 and p < 0.45) else ""
            print(f"  Move={move:>+6.2f}%  T-{secs:>2}s  →  P(UP)={p:.4f}{marker}")

    # Verdict
    print("\n" + "="*70)
    if has_edge:
        print("CHECKPOINT PASSED: Base rates show exploitable edge")
        print(f"Found {len(edge_bins)} bins with >55% or <45% hit rate")
        print("→ Proceed with directional strategy")
    else:
        print("CHECKPOINT FAILED: No significant edge in base rates")
        print("→ Consider pivoting to arbitrage-only or wallet tracking")
    print("="*70)


if __name__ == "__main__":
    main()
