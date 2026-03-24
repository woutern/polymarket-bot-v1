#!/usr/bin/env python3
"""Coinbase-driven scenario simulator for MarketMakerStrategy.

Fetches real BTC 1-minute candles from Coinbase, builds 5-minute windows,
maps price movement to bid probabilities, and runs MarketMakerStrategy
tick-by-tick. Reports what the bot does in each scenario.

Scenarios tested:
  1. Slow grind up
  2. Slow grind down
  3. Sideways / chop
  4. Sudden spike mid-window then reversal
  5. Dead side (one bid rises above 80c)
  6. Real BTC windows from Coinbase (most recent N windows)

Usage:
  uv run python scripts/coinbase_scenario_sim.py
  uv run python scripts/coinbase_scenario_sim.py --pair ETH-USD --windows 10
  uv run python scripts/coinbase_scenario_sim.py --synthetic-only
  uv run python scripts/coinbase_scenario_sim.py --real-only --windows 5
  uv run python scripts/coinbase_scenario_sim.py --verbose   (trace every action)
"""

from __future__ import annotations

import argparse
import math
import random
import sys
import time
from dataclasses import dataclass, field

import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent / "src"))

from polybot.core.position import Position
from polybot.strategy.base import MarketState
from polybot.strategy.market_maker import MarketMakerStrategy
from polybot.strategy.profiles import BTC_5M_PROFILE

# ── Coinbase API ──────────────────────────────────────────────────────────────

COINBASE_CANDLES_URL = "https://api.exchange.coinbase.com/products/{product}/candles"


def fetch_coinbase_candles(product: str = "BTC-USD", limit: int = 400) -> list[dict]:
    """Fetch recent 1-minute candles from Coinbase public API."""
    try:
        import httpx
    except ImportError:
        print("  [!] httpx not installed — skipping real-data windows")
        return []

    url = COINBASE_CANDLES_URL.format(product=product)
    try:
        resp = httpx.get(url, params={"granularity": 60}, timeout=10.0)
        resp.raise_for_status()
        raw = resp.json()
        # [[time, low, high, open, close, volume], ...] — newest first
        candles = [
            {"time": r[0], "low": r[1], "high": r[2], "open": r[3], "close": r[4]}
            for r in reversed(raw)
        ]
        return candles
    except Exception as e:
        print(f"  [!] Coinbase fetch failed: {e}")
        return []


# ── Price → bid mapping ──────────────────────────────────────────────────────


def price_to_bids(
    open_price: float, current_price: float, k: float = 50.0
) -> tuple[float, float]:
    """Map price move relative to window open → (yes_bid, no_bid).

    Linear model: yes_bid = 0.50 + move_pct * k, no_bid = 0.96 - yes_bid
    At 0% move: yes_bid = no_bid = 0.48 (symmetric market)
    At +0.4% move: yes_bid ≈ 0.70, no_bid ≈ 0.26 → clear UP signal
    At +0.8% move: yes_bid ≈ 0.90 (clamped), no_bid ≈ 0.06
    """
    move_pct = (current_price - open_price) / open_price
    yes_bid = 0.50 + move_pct * k
    yes_bid = max(0.04, min(0.93, yes_bid))
    no_bid = 0.96 - yes_bid
    no_bid = max(0.03, min(0.93, no_bid))
    return round(yes_bid, 3), round(no_bid, 3)


def bids_to_market(
    yes_bid: float, no_bid: float, seconds: int, prob_up: float
) -> MarketState:
    return MarketState(
        seconds=seconds,
        yes_bid=yes_bid,
        no_bid=no_bid,
        yes_ask=round(yes_bid + 0.01, 3),
        no_ask=round(no_bid + 0.01, 3),
        prob_up=prob_up,
    )


# ── Synthetic tick generators ─────────────────────────────────────────────────


def ticks_grind(
    direction: float, final_move_pct: float, prob_up: float, n: int = 245
) -> list[MarketState]:
    """Smooth monotonic grind. Direction: +1 = UP, -1 = DOWN."""
    open_price = 50000.0
    ticks = []
    for i in range(n):
        t = i / max(n - 1, 1)
        price = open_price * (1 + direction * final_move_pct * t)
        yes_bid, no_bid = price_to_bids(open_price, price)
        ticks.append(bids_to_market(yes_bid, no_bid, i + 5, prob_up))
    return ticks


def ticks_sideways(prob_up: float, n: int = 245, seed: int = 42) -> list[MarketState]:
    """Choppy sideways: direction flips every ~30 ticks."""
    rng = random.Random(seed)
    # Pre-generate clean flip pattern (no noise)
    ticks = []
    yes_bid = 0.50
    phase_len = 35
    for i in range(n):
        # Square-wave oscillation ±0.12
        phase = (i // phase_len) % 2
        target = 0.62 if phase == 0 else 0.38
        # Smooth approach to target
        yes_bid = yes_bid + (target - yes_bid) * 0.10
        yes_bid = round(max(0.10, min(0.90, yes_bid)), 3)
        no_bid = round(0.96 - yes_bid, 3)
        ticks.append(bids_to_market(yes_bid, no_bid, i + 5, prob_up))
    return ticks


def ticks_spike_reversal(
    direction: float, spike_at: int = 120, prob_up: float = 0.50, n: int = 245
) -> list[MarketState]:
    """Gradual drift then sudden spike, then reversal."""
    ticks = []
    yes_bid = 0.50
    for i in range(n):
        if i < spike_at:
            # Slow drift toward direction
            target = 0.58 if direction > 0 else 0.42
        elif i < spike_at + 30:
            # Fast spike in direction
            t = (i - spike_at) / 30
            extreme = 0.85 if direction > 0 else 0.15
            target = 0.58 + t * (extreme - 0.58) if direction > 0 else 0.42 - t * (0.42 - extreme)
        else:
            # Full reversal
            t = (i - spike_at - 30) / max(n - spike_at - 30, 1)
            spike_peak = 0.85 if direction > 0 else 0.15
            reversal = 0.25 if direction > 0 else 0.75
            target = spike_peak + t * (reversal - spike_peak)
        yes_bid = yes_bid + (target - yes_bid) * 0.15
        yes_bid = round(max(0.04, min(0.93, yes_bid)), 3)
        no_bid = round(max(0.03, 0.96 - yes_bid), 3)
        ticks.append(bids_to_market(yes_bid, no_bid, i + 5, prob_up))
    return ticks


def ticks_dead_side(direction: float, prob_up: float = 0.55, n: int = 245) -> list[MarketState]:
    """Strong trend that sends the losing side below 10c by T+150."""
    ticks = []
    yes_bid = 0.50
    for i in range(n):
        t = i / max(n - 1, 1)
        # Accelerating move
        target = (0.50 + direction * 0.43 * t**1.3)
        yes_bid = yes_bid + (target - yes_bid) * 0.08
        yes_bid = round(max(0.04, min(0.93, yes_bid)), 3)
        no_bid = round(max(0.03, 0.96 - yes_bid), 3)
        ticks.append(bids_to_market(yes_bid, no_bid, i + 5, prob_up))
    return ticks


def ticks_from_prices(
    prices: list[float], open_price: float, prob_up: float
) -> list[MarketState]:
    """Build tick stream from real price series."""
    ticks = []
    n = min(len(prices), 245)
    for i in range(n):
        yes_bid, no_bid = price_to_bids(open_price, prices[i])
        ticks.append(bids_to_market(yes_bid, no_bid, i + 5, prob_up))
    return ticks


# ── Simulation runner ─────────────────────────────────────────────────────────


@dataclass
class SimResult:
    name: str
    deployed: float
    up_shares: int
    down_shares: int
    up_avg: float
    down_avg: float
    combined_avg: float
    pnl_if_up: float
    pnl_if_down: float
    is_gp: bool
    sells_count: int
    buys_count: int
    sell_reasons: dict = field(default_factory=dict)
    reversal_count: int = 0
    chop_regime: bool = False


def run_sim(
    name: str,
    ticks: list[MarketState],
    budget: float = 80.0,
    verbose: bool = False,
) -> SimResult:
    strategy = MarketMakerStrategy(BTC_5M_PROFILE)
    strategy.reset()
    pos = Position()
    remaining = budget
    sell_reasons: dict[str, int] = {}

    for market in ticks:
        action = strategy.on_tick(market, pos, remaining)

        if action.sell_up_shares > 0:
            proceeds = pos.sell(True, action.sell_up_shares, action.sell_up_price)
            remaining += proceeds
            sell_reasons[action.reason] = sell_reasons.get(action.reason, 0) + 1
            if verbose:
                print(f"  T+{market.seconds:3d} SELL_UP  {action.sell_up_shares}sh "
                      f"@ {action.sell_up_price:.2f}  [{action.reason}]  "
                      f"yes={market.yes_bid:.2f} no={market.no_bid:.2f}")
        if action.sell_down_shares > 0:
            proceeds = pos.sell(False, action.sell_down_shares, action.sell_down_price)
            remaining += proceeds
            sell_reasons[action.reason] = sell_reasons.get(action.reason, 0) + 1
            if verbose:
                print(f"  T+{market.seconds:3d} SELL_DN  {action.sell_down_shares}sh "
                      f"@ {action.sell_down_price:.2f}  [{action.reason}]  "
                      f"yes={market.yes_bid:.2f} no={market.no_bid:.2f}")

        if action.buy_up_shares > 0:
            cost = action.buy_up_shares * action.buy_up_price
            if cost <= remaining:
                pos.buy(True, action.buy_up_shares, action.buy_up_price)
                remaining -= cost
                if verbose:
                    print(f"  T+{market.seconds:3d} BUY_UP   {action.buy_up_shares}sh "
                          f"@ {action.buy_up_price:.2f}  "
                          f"yes={market.yes_bid:.2f} no={market.no_bid:.2f}")
        if action.buy_down_shares > 0:
            cost = action.buy_down_shares * action.buy_down_price
            if cost <= remaining:
                pos.buy(False, action.buy_down_shares, action.buy_down_price)
                remaining -= cost
                if verbose:
                    print(f"  T+{market.seconds:3d} BUY_DN   {action.buy_down_shares}sh "
                          f"@ {action.buy_down_price:.2f}  "
                          f"yes={market.yes_bid:.2f} no={market.no_bid:.2f}")

    return SimResult(
        name=name,
        deployed=pos.net_cost,
        up_shares=pos.up_shares,
        down_shares=pos.down_shares,
        up_avg=pos.up_avg,
        down_avg=pos.down_avg,
        combined_avg=pos.combined_avg,
        pnl_if_up=pos.pnl_if_up(),
        pnl_if_down=pos.pnl_if_down(),
        is_gp=pos.is_gp(),
        sells_count=pos.sells_count,
        buys_count=pos.buys_count,
        sell_reasons=sell_reasons,
        reversal_count=strategy.reversal_count,
        chop_regime=strategy.chop_regime,
    )


# ── Reporting ─────────────────────────────────────────────────────────────────


def print_result(r: SimResult) -> None:
    tags = []
    if r.is_gp:
        tags.append("*** GUARANTEED PROFIT ***")
    if r.chop_regime:
        tags.append("[CHOP]")
    if r.reversal_count > 0:
        tags.append(f"[{r.reversal_count} reversals]")
    tag_str = "  " + " ".join(tags) if tags else ""

    print(f"\n{'─'*62}")
    print(f"  {r.name}")
    if tag_str:
        print(tag_str)
    print(f"{'─'*62}")
    print(f"  Deployed:   ${r.deployed:6.2f}  ({r.buys_count} buys, {r.sells_count} sells)")
    print(f"  UP:         {r.up_shares:4d} shares @ {r.up_avg:.3f} avg")
    print(f"  DOWN:       {r.down_shares:4d} shares @ {r.down_avg:.3f} avg")
    if r.combined_avg > 0:
        gp_arrow = " ← GP!" if r.combined_avg < 1.00 else " ← not GP"
        print(f"  Combined:   {r.combined_avg:.3f}{gp_arrow}")
    else:
        print(f"  Combined:   —  (one-sided position)")
    print(f"  P&L if UP:  ${r.pnl_if_up:+6.2f}   P&L if DOWN: ${r.pnl_if_down:+6.2f}")
    if r.sell_reasons:
        reasons = ", ".join(f"{k}:{v}" for k, v in sorted(r.sell_reasons.items()))
        print(f"  Sell reasons: {reasons}")


def print_summary(results: list[SimResult]) -> None:
    print(f"\n{'='*62}")
    print("  SUMMARY")
    print(f"{'='*62}")
    header = f"  {'Scenario':<36} {'Deploy':>7} {'GP?':>4} {'Comb':>6} {'Worst PnL':>10}"
    print(header)
    print(f"  {'-'*36} {'-'*7} {'-'*4} {'-'*6} {'-'*10}")
    for r in results:
        gp = "YES" if r.is_gp else "no"
        comb = f"{r.combined_avg:.3f}" if r.combined_avg > 0 else "—"
        worst = min(r.pnl_if_up, r.pnl_if_down)
        print(f"  {r.name:<36} ${r.deployed:>6.2f} {gp:>4}  {comb:>6} {worst:>+9.2f}")
    print()
    gp_count = sum(1 for r in results if r.is_gp)
    avg_deployed = sum(r.deployed for r in results) / len(results) if results else 0
    avg_worst = sum(min(r.pnl_if_up, r.pnl_if_down) for r in results) / len(results) if results else 0
    print(f"  GP windows:      {gp_count}/{len(results)}")
    print(f"  Avg deployed:    ${avg_deployed:.2f}")
    print(f"  Avg worst P&L:   ${avg_worst:+.2f}")


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pair", default="BTC-USD")
    parser.add_argument("--windows", type=int, default=8, help="Real Coinbase windows to test")
    parser.add_argument("--budget", type=float, default=80.0)
    parser.add_argument("--prob-up", type=float, default=0.55)
    parser.add_argument("--synthetic-only", action="store_true")
    parser.add_argument("--real-only", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    results: list[SimResult] = []
    prob_up = args.prob_up

    # ── Synthetic scenarios ───────────────────────────────────────────────────
    if not args.real_only:
        print("\n=== SYNTHETIC SCENARIOS ===")

        scenarios = [
            ("Slow grind UP   (+0.4%)",
             ticks_grind(+1, 0.004, prob_up)),
            ("Slow grind DOWN (-0.4%)",
             ticks_grind(-1, 0.004, prob_up)),
            ("Strong UP       (+0.8%)",
             ticks_grind(+1, 0.008, prob_up)),
            ("Strong DOWN     (-0.8%)",
             ticks_grind(-1, 0.008, prob_up)),
            ("Sideways / chop (oscillating ±12c)",
             ticks_sideways(prob_up)),
            ("Spike UP then reversal  (T+120)",
             ticks_spike_reversal(+1, 120, prob_up)),
            ("Spike DOWN then reversal (T+120)",
             ticks_spike_reversal(-1, 120, prob_up)),
            ("Dead side — UP crushes DOWN",
             ticks_dead_side(+1, prob_up)),
            ("Dead side — DOWN crushes UP",
             ticks_dead_side(-1, 1.0 - prob_up)),
        ]

        for name, ticks in scenarios:
            if args.verbose:
                print(f"\n--- {name} ---")
            r = run_sim(name, ticks, args.budget, args.verbose)
            print_result(r)
            results.append(r)

    # ── Real Coinbase data ────────────────────────────────────────────────────
    if not args.synthetic_only:
        print(f"\n=== REAL {args.pair} WINDOWS (last {args.windows} × 5min) ===")
        print("  Fetching from Coinbase...")

        candles = fetch_coinbase_candles(args.pair, limit=400)
        if not candles:
            print("  No data. Skipping.")
        else:
            print(f"  Got {len(candles)} 1-min candles")

            # Estimate volatility for bid scaling
            closes = [c["close"] for c in candles]
            returns = [(closes[i] - closes[i-1]) / closes[i-1]
                       for i in range(1, len(closes)) if closes[i-1] > 0]
            # k calibration: 0.5% move → yes_bid ≈ 0.70 → k = 0.20/0.005 = 40
            # Use actual volatility to calibrate
            vol_1m = (sum(r**2 for r in returns) / len(returns))**0.5 if returns else 0.001
            k = min(80.0, max(20.0, 0.20 / (vol_1m * 5)))  # scaled to 5-min window
            print(f"  1-min vol: {vol_1m*100:.3f}%  → bid-scale k={k:.1f}\n")

            n_windows = min(args.windows, len(candles) // 5)
            for w in range(n_windows):
                start_idx = len(candles) - (w + 1) * 5
                if start_idx < 0:
                    break
                wc = candles[start_idx:start_idx + 5]
                open_price = wc[0]["open"]
                close_price = wc[-1]["close"]
                move_pct = (close_price - open_price) / open_price * 100
                direction = "UP" if move_pct > 0 else "DN"

                # Expand 5 candles into ~50 price points (10 per minute)
                prices = []
                for candle in wc:
                    o, h, l, c = candle["open"], candle["high"], candle["low"], candle["close"]
                    for tick in range(10):
                        t = tick / 9
                        # Simple OHLC path: open→high→low→close
                        if t < 0.33:
                            p = o + (h - o) * (t / 0.33)
                        elif t < 0.67:
                            p = h + (l - h) * ((t - 0.33) / 0.34)
                        else:
                            p = l + (c - l) * ((t - 0.67) / 0.33)
                        prices.append(p)

                import datetime
                dt = datetime.datetime.utcfromtimestamp(wc[0]["time"]).strftime("%H:%M")
                name = f"{args.pair} {dt}  {direction} {abs(move_pct):.3f}%"

                ticks = ticks_from_prices(prices, open_price, prob_up)
                if args.verbose:
                    print(f"\n--- {name} ---")
                r = run_sim(name, ticks, args.budget, args.verbose)
                print_result(r)
                results.append(r)

    if results:
        print_summary(results)


if __name__ == "__main__":
    main()
