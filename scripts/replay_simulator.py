"""Replay simulator: test trading strategies against real AND synthetic market data.

Two modes:
1. REPLAY: Uses 49 real BTC 5m windows from data/replay_dataset.json
2. SYNTHETIC: Generates random market scenarios with configurable dynamics

Usage:
    # Replay real data
    python scripts/replay_simulator.py
    python scripts/replay_simulator.py --strategy k9 --budget 150

    # Synthetic scenarios
    python scripts/replay_simulator.py --synthetic --count 200
    python scripts/replay_simulator.py --synthetic --count 500 --seed 42

    # Specific real window
    python scripts/replay_simulator.py --window 5 --verbose

    # Compare strategies
    python scripts/replay_simulator.py --compare
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------------------
# Position / order tracking
# ---------------------------------------------------------------------------


@dataclass
class Position:
    up_shares: int = 0
    up_cost: float = 0.0
    down_shares: int = 0
    down_cost: float = 0.0
    sells_count: int = 0
    buys_count: int = 0
    total_sold_proceeds: float = 0.0
    total_bought_cost: float = 0.0

    @property
    def up_avg(self) -> float:
        return round(self.up_cost / self.up_shares, 4) if self.up_shares > 0 else 0.0

    @property
    def down_avg(self) -> float:
        return (
            round(self.down_cost / self.down_shares, 4) if self.down_shares > 0 else 0.0
        )

    @property
    def combined_avg(self) -> float:
        if self.up_shares > 0 and self.down_shares > 0:
            return round(self.up_avg + self.down_avg, 4)
        return 0.0

    @property
    def net_cost(self) -> float:
        return round(self.up_cost + self.down_cost, 2)

    @property
    def payout_floor(self) -> int:
        return min(self.up_shares, self.down_shares)

    @property
    def total_shares(self) -> int:
        return self.up_shares + self.down_shares

    def pnl_if_up(self) -> float:
        return round(self.up_shares - self.net_cost, 2)

    def pnl_if_down(self) -> float:
        return round(self.down_shares - self.net_cost, 2)

    def is_gp(self) -> bool:
        return self.pnl_if_up() > 0 and self.pnl_if_down() > 0

    def buy(self, side_up: bool, shares: int, price: float) -> float:
        cost = round(shares * price, 2)
        if side_up:
            self.up_shares += shares
            self.up_cost += cost
        else:
            self.down_shares += shares
            self.down_cost += cost
        self.buys_count += 1
        self.total_bought_cost += cost
        return cost

    def sell(self, side_up: bool, shares: int, price: float) -> float:
        proceeds = round(shares * price, 2)
        if side_up:
            if self.up_shares < shares:
                shares = self.up_shares
                proceeds = round(shares * price, 2)
            avg = self.up_avg
            self.up_shares -= shares
            self.up_cost = max(round(self.up_cost - shares * avg, 2), 0.0)
        else:
            if self.down_shares < shares:
                shares = self.down_shares
                proceeds = round(shares * price, 2)
            avg = self.down_avg
            self.down_shares -= shares
            self.down_cost = max(round(self.down_cost - shares * avg, 2), 0.0)
        self.sells_count += 1
        self.total_sold_proceeds += proceeds
        return proceeds


# ---------------------------------------------------------------------------
# Market state reconstruction
# ---------------------------------------------------------------------------


@dataclass
class MarketTick:
    seconds: int
    prob_up: float
    yes_bid: float  # inferred from fill prices / avg changes
    no_bid: float
    up_shares_actual: int  # what the bot actually held
    down_shares_actual: int
    up_avg_actual: float
    down_avg_actual: float


def reconstruct_market(window: dict, fills_by_ts: dict) -> list[MarketTick]:
    """Reconstruct yes_bid / no_bid from tick data + fill prices.

    For real data: infers bids from fill prices and avg changes.
    For synthetic data: uses the _yes_bid / _no_bid fields directly.
    """
    states = window.get("market_states", [])
    if not states:
        return []

    ticks: list[MarketTick] = []
    last_yes_bid = 0.50
    last_no_bid = 0.50
    prev_up_shares = 0
    prev_down_shares = 0
    prev_up_cost = 0.0
    prev_down_cost = 0.0

    # Check if this is synthetic data (has _yes_bid fields)
    is_synthetic = len(states) > 0 and "_yes_bid" in states[0]

    for s in states:
        seconds = s.get("seconds", 0)
        up_shares = s.get("up_shares", 0)
        down_shares = s.get("down_shares", 0)
        up_avg = s.get("up_avg", 0)
        down_avg = s.get("down_avg", 0)
        prob_up = s.get("prob_up", 0.5)

        up_cost = round(up_shares * up_avg, 2) if up_shares > 0 else 0.0
        down_cost = round(down_shares * down_avg, 2) if down_shares > 0 else 0.0

        # Synthetic data has exact prices
        if is_synthetic:
            last_yes_bid = s.get("_yes_bid", 0.50)
            last_no_bid = s.get("_no_bid", 0.50)
            ticks.append(
                MarketTick(
                    seconds=seconds,
                    prob_up=prob_up,
                    yes_bid=last_yes_bid,
                    no_bid=last_no_bid,
                    up_shares_actual=up_shares,
                    down_shares_actual=down_shares,
                    up_avg_actual=up_avg,
                    down_avg_actual=down_avg,
                )
            )
            continue

        # Real data: detect new fills by share count changes
        up_delta = up_shares - prev_up_shares
        down_delta = down_shares - prev_down_shares

        if up_delta > 0 and up_cost > prev_up_cost:
            # New UP fill — infer price from cost change
            fill_cost = up_cost - prev_up_cost
            last_yes_bid = (
                round(fill_cost / up_delta, 2) if up_delta > 0 else last_yes_bid
            )
        elif up_avg > 0:
            last_yes_bid = up_avg  # approximate

        if down_delta > 0 and down_cost > prev_down_cost:
            fill_cost = down_cost - prev_down_cost
            last_no_bid = (
                round(fill_cost / down_delta, 2) if down_delta > 0 else last_no_bid
            )
        elif down_avg > 0:
            last_no_bid = down_avg

        # Cross-check: YES + NO should be near 1.00
        # If we only have one side, infer the other
        if up_avg > 0 and down_avg == 0:
            last_no_bid = max(round(1.0 - up_avg, 2), 0.01)
        elif down_avg > 0 and up_avg == 0:
            last_yes_bid = max(round(1.0 - down_avg, 2), 0.01)

        # Clamp to reasonable range
        last_yes_bid = max(min(last_yes_bid, 0.99), 0.01)
        last_no_bid = max(min(last_no_bid, 0.99), 0.01)

        ticks.append(
            MarketTick(
                seconds=seconds,
                prob_up=prob_up,
                yes_bid=last_yes_bid,
                no_bid=last_no_bid,
                up_shares_actual=up_shares,
                down_shares_actual=down_shares,
                up_avg_actual=up_avg,
                down_avg_actual=down_avg,
            )
        )

        prev_up_shares = up_shares
        prev_down_shares = down_shares
        prev_up_cost = up_cost
        prev_down_cost = down_cost

    return ticks


# ---------------------------------------------------------------------------
# Synthetic market generator
# ---------------------------------------------------------------------------


def generate_synthetic_window(
    scenario: str = "random",
    seed: int | None = None,
    duration: int = 300,
) -> dict:
    """Generate a synthetic 5m market window with realistic price dynamics.

    Scenarios:
    - "random": random walk with drift, can go either way
    - "up": BTC trends up, YES gets expensive, NO gets cheap
    - "down": BTC trends down, NO gets expensive, YES gets cheap
    - "reversal_up_down": starts UP then reverses to DOWN mid-window
    - "reversal_down_up": starts DOWN then reverses to UP mid-window
    - "whipsaw": oscillates up and down rapidly
    - "strong_trend": very strong directional move
    - "flat": barely moves, stays near 50/50
    """
    rng = random.Random(seed)

    # Starting state: both sides near 50c
    yes_price = 0.50
    no_price = 0.50

    # Model accuracy: 64% chance model agrees with eventual direction
    # We pick the "true" outcome first, then set model accordingly
    if scenario == "random":
        true_up = rng.random() < 0.50
    elif scenario in ("up", "reversal_up_down", "strong_trend_up"):
        true_up = True
    elif scenario in ("down", "reversal_down_up", "strong_trend_down"):
        true_up = False
    elif scenario == "whipsaw":
        true_up = rng.random() < 0.50
    elif scenario == "flat":
        true_up = rng.random() < 0.50
    else:
        true_up = rng.random() < 0.50

    # Model prediction (64% accurate)
    if rng.random() < 0.64:
        model_up = true_up
    else:
        model_up = not true_up

    prob_up_base = (
        0.60 + rng.uniform(0, 0.15) if model_up else 0.25 + rng.uniform(0, 0.15)
    )

    # Generate price path
    ticks = []
    volatility = rng.uniform(0.002, 0.008)  # per-tick volatility

    for sec in range(5, 255):
        t = sec / 300.0  # normalized time 0-1

        # Drift based on scenario
        if scenario == "up":
            drift = 0.003 * (1 + t)
        elif scenario == "down":
            drift = -0.003 * (1 + t)
        elif scenario == "reversal_up_down":
            if t < 0.4:
                drift = 0.005
            else:
                drift = -0.008
        elif scenario == "reversal_down_up":
            if t < 0.4:
                drift = -0.005
            else:
                drift = 0.008
        elif scenario == "whipsaw":
            drift = 0.005 * math.sin(t * 12)
        elif scenario in ("strong_trend_up", "strong_trend"):
            drift = 0.008 * (1 + 2 * t)
        elif scenario == "strong_trend_down":
            drift = -0.008 * (1 + 2 * t)
        elif scenario == "flat":
            drift = 0.0
        else:  # random
            drift = rng.gauss(0, 0.001)

        # Random walk with drift
        shock = rng.gauss(drift, volatility)
        yes_price = max(0.01, min(0.99, yes_price + shock))
        no_price = max(0.01, min(0.99, 1.0 - yes_price + rng.gauss(0, 0.005)))

        # Ensure yes + no stays near 1.00 (binary market constraint)
        total = yes_price + no_price
        if total > 0:
            yes_price = max(0.01, min(0.99, yes_price / total))
            no_price = max(0.01, min(0.99, 1.0 - yes_price))

        # Model prob evolves slowly, with some noise
        prob_noise = rng.gauss(0, 0.02)
        # Model gradually aligns with price direction
        price_signal = 0.5 + (yes_price - 0.5) * 0.3
        prob_up = 0.7 * prob_up_base + 0.2 * price_signal + 0.1 * prob_noise
        prob_up = max(0.10, min(0.90, prob_up))

        ticks.append(
            {
                "seconds": sec,
                "prob_up": round(prob_up, 3),
                "up_pct": round(prob_up, 2),
                "down_pct": round(1.0 - prob_up, 2),
                "combined_avg": 0,
                "up_avg": 0,
                "down_avg": 0,
                "up_shares": 0,
                "down_shares": 0,
                "net_cost": 0,
                "remaining_budget": 0,
                "sell_fired": False,
                "sell_reason": "",
                "pair_guard_skipped": 0,
                "budget_scale": 1.0,
                "budget_curve_pct": 0,
                "posted_up": 0,
                "posted_down": 0,
                "hard_cap_skipped": 0,
                "stale_orders_cancelled": 0,
                "payout_floor": 0,
                "cost_above_floor": 0,
                # Synthetic-only fields for market state
                "_yes_bid": round(yes_price, 3),
                "_no_bid": round(no_price, 3),
                "_true_up": true_up,
                "_scenario": scenario,
            }
        )

    return {
        "timestamp": f"synthetic_{scenario}_{seed or rng.randint(0, 99999)}",
        "ticks": len(ticks),
        "market_states": ticks,
        "actual_final": {
            "up_shares": 0,
            "down_shares": 0,
            "up_avg": 0,
            "down_avg": 0,
            "combined_avg": 0,
            "net_cost": 0,
        },
        "_true_up": true_up,
        "_scenario": scenario,
    }


def generate_synthetic_batch(
    count: int = 200,
    seed: int | None = None,
) -> list[dict]:
    """Generate a batch of synthetic windows with mixed scenarios."""
    rng = random.Random(seed)
    windows = []

    # Distribution of scenarios (weighted toward realistic mix)
    scenario_weights = [
        ("random", 40),  # 40% pure random
        ("up", 10),
        ("down", 10),
        ("reversal_up_down", 10),
        ("reversal_down_up", 10),
        ("whipsaw", 5),
        ("strong_trend_up", 5),
        ("strong_trend_down", 5),
        ("flat", 5),
    ]

    total_weight = sum(w for _, w in scenario_weights)
    scenarios = []
    for scenario, weight in scenario_weights:
        n = max(1, round(count * weight / total_weight))
        scenarios.extend([scenario] * n)

    # Trim or pad to exact count
    while len(scenarios) < count:
        scenarios.append("random")
    scenarios = scenarios[:count]
    rng.shuffle(scenarios)

    for i, scenario in enumerate(scenarios):
        window_seed = rng.randint(0, 999999)
        windows.append(generate_synthetic_window(scenario=scenario, seed=window_seed))

    return windows


# ---------------------------------------------------------------------------
# Strategy interface
# ---------------------------------------------------------------------------


@dataclass
class StrategyAction:
    """What the strategy wants to do on this tick."""

    buy_up_shares: int = 0
    buy_up_price: float = 0.0
    buy_down_shares: int = 0
    buy_down_price: float = 0.0
    sell_up_shares: int = 0
    sell_up_price: float = 0.0
    sell_down_shares: int = 0
    sell_down_price: float = 0.0


class Strategy:
    """Base strategy interface."""

    def __init__(self, budget: float = 100.0):
        self.budget = budget
        self.name = "base"

    def on_tick(
        self,
        tick: MarketTick,
        position: Position,
        budget_remaining: float,
        seconds: int,
    ) -> StrategyAction:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Strategy: "what we actually did" (replay from data)
# ---------------------------------------------------------------------------


class ActualStrategy(Strategy):
    """Replays what the bot actually did — for comparison baseline."""

    def __init__(self, budget: float = 100.0):
        super().__init__(budget)
        self.name = "actual"

    def on_tick(self, tick, position, budget_remaining, seconds):
        # The actual strategy is already captured in the tick data
        # We just need to match the share changes
        action = StrategyAction()

        up_delta = tick.up_shares_actual - position.up_shares
        down_delta = tick.down_shares_actual - position.down_shares

        if up_delta > 0:
            action.buy_up_shares = up_delta
            action.buy_up_price = tick.yes_bid
        elif up_delta < 0:
            action.sell_up_shares = abs(up_delta)
            action.sell_up_price = tick.yes_bid

        if down_delta > 0:
            action.buy_down_shares = down_delta
            action.buy_down_price = tick.no_bid
        elif down_delta < 0:
            action.sell_down_shares = abs(down_delta)
            action.sell_down_price = tick.no_bid

        return action


# ---------------------------------------------------------------------------
# Strategy: K9-style (our ruleset)
# ---------------------------------------------------------------------------


class K9Strategy(Strategy):
    """K9-style strategy based on our definitive ruleset.

    Core principles:
    1. Market price is truth (yes_bid vs no_bid determines winner)
    2. No direction lock — adapt every tick
    3. Sell the LOSING side, never sell the winning side
    4. Buy both sides, weighted by market direction
    5. Deploy 80%+ of budget
    6. Don't buy dying shares (other bid > 70c)
    """

    def __init__(self, budget: float = 150.0):
        super().__init__(budget)
        self.name = "k9"
        self.last_sell_seconds = -999
        self.sell_cooldown = 10  # seconds between sells

    def on_tick(
        self,
        tick: MarketTick,
        position: Position,
        budget_remaining: float,
        seconds: int,
    ) -> StrategyAction:
        action = StrategyAction()

        yes_bid = tick.yes_bid
        no_bid = tick.no_bid
        prob_up = tick.prob_up

        HARD_CAP = 0.82
        SHARES_PER_ORDER = 5

        # ── COMMIT: no trading after T+250 ──
        if seconds >= 250:
            return action

        # ── DETERMINE MARKET DIRECTION ──
        market_edge = abs(yes_bid - no_bid)
        if market_edge > 0.10:
            winning_up = yes_bid > no_bid
        elif market_edge > 0.05:
            winning_up = yes_bid > no_bid
        else:
            winning_up = prob_up >= 0.50

        # ── SELL LOGIC: sell the losing side ──
        # Only sell after T+20, with cooldown
        if seconds >= 20 and (seconds - self.last_sell_seconds) >= self.sell_cooldown:
            losing_up = not winning_up

            # DEAD_SIDE: other bid > 80c — dump everything on losing side
            if losing_up and no_bid > 0.80 and position.up_shares >= 5:
                action.sell_up_shares = min(position.up_shares, SHARES_PER_ORDER)
                action.sell_up_price = yes_bid
                self.last_sell_seconds = seconds
            elif not losing_up and yes_bid > 0.80 and position.down_shares >= 5:
                action.sell_down_shares = min(position.down_shares, SHARES_PER_ORDER)
                action.sell_down_price = no_bid
                self.last_sell_seconds = seconds

            # UNFAVORED_RICH: losing side avg > 50c and losing by > 10c
            elif (
                losing_up
                and position.up_avg > 0.50
                and market_edge > 0.10
                and position.up_shares >= 5
            ):
                action.sell_up_shares = min(position.up_shares, SHARES_PER_ORDER)
                action.sell_up_price = yes_bid
                self.last_sell_seconds = seconds
            elif (
                not losing_up
                and position.down_avg > 0.50
                and market_edge > 0.10
                and position.down_shares >= 5
            ):
                action.sell_down_shares = min(position.down_shares, SHARES_PER_ORDER)
                action.sell_down_price = no_bid
                self.last_sell_seconds = seconds

            # LATE DUMP: after T+180, sell any side with bid < 25c
            elif seconds >= 180:
                if position.up_shares >= 5 and yes_bid < 0.25 and yes_bid > 0:
                    action.sell_up_shares = min(position.up_shares, SHARES_PER_ORDER)
                    action.sell_up_price = yes_bid
                    self.last_sell_seconds = seconds
                elif position.down_shares >= 5 and no_bid < 0.25 and no_bid > 0:
                    action.sell_down_shares = min(
                        position.down_shares, SHARES_PER_ORDER
                    )
                    action.sell_down_price = no_bid
                    self.last_sell_seconds = seconds

        # ── BUY LOGIC ──
        # Budget curve: deploy gradually
        if seconds <= 5:
            max_deploy_pct = 0.10
        elif seconds <= 60:
            max_deploy_pct = 0.10 + 0.12 * ((seconds - 5) / 55.0)
        elif seconds <= 180:
            max_deploy_pct = 0.22 + 0.60 * ((seconds - 60) / 120.0)
        elif seconds <= 250:
            max_deploy_pct = 0.82 + 0.10 * ((seconds - 180) / 70.0)
        else:
            max_deploy_pct = 0.92

        max_deploy = self.budget * max_deploy_pct
        currently_deployed = position.net_cost
        curve_remaining = max(max_deploy - currently_deployed, 0)
        usable = min(budget_remaining, curve_remaining)

        if usable < 0.50:
            return action

        # Allocation split based on market direction
        if market_edge > 0.20:
            win_pct = 0.80
        elif market_edge > 0.10:
            win_pct = 0.70
        else:
            win_pct = 0.60 if winning_up == (prob_up >= 0.50) else 0.50

        up_budget = usable * (win_pct if winning_up else (1.0 - win_pct))
        down_budget = usable * ((1.0 - win_pct) if winning_up else win_pct)

        # Dynamic balance cap: 75% before T+120, 90% after
        balance_cap = 0.90 if seconds >= 120 else 0.75
        total = position.total_shares
        if total >= 10:
            up_pct_now = position.up_shares / total if total > 0 else 0.5
            dn_pct_now = 1.0 - up_pct_now
            if up_pct_now > balance_cap:
                up_budget = 0
            if dn_pct_now > balance_cap:
                down_budget = 0

        # Buy UP
        if (
            yes_bid > 0
            and yes_bid <= HARD_CAP
            and up_budget >= SHARES_PER_ORDER * yes_bid
        ):
            # Dying side block: don't buy if other side bid > 70c
            if no_bid <= 0.70:
                action.buy_up_shares = SHARES_PER_ORDER
                action.buy_up_price = yes_bid

        # Buy DOWN
        if (
            no_bid > 0
            and no_bid <= HARD_CAP
            and down_budget >= SHARES_PER_ORDER * no_bid
        ):
            if yes_bid <= 0.70:
                action.buy_down_shares = SHARES_PER_ORDER
                action.buy_down_price = no_bid

        # SELL-AND-REBUY: if we sold, also buy on the winning side
        if (
            action.sell_up_shares > 0
            and action.buy_down_shares == 0
            and no_bid <= HARD_CAP
            and no_bid > 0
        ):
            if yes_bid <= 0.70:  # down side not dying
                action.buy_down_shares = SHARES_PER_ORDER
                action.buy_down_price = no_bid
        elif (
            action.sell_down_shares > 0
            and action.buy_up_shares == 0
            and yes_bid <= HARD_CAP
            and yes_bid > 0
        ):
            if no_bid <= 0.70:
                action.buy_up_shares = SHARES_PER_ORDER
                action.buy_up_price = yes_bid

        return action


# ---------------------------------------------------------------------------
# Simulation engine
# ---------------------------------------------------------------------------


@dataclass
class WindowResult:
    window_idx: int
    timestamp: str
    strategy: str
    ticks: int
    final_up: int
    final_down: int
    up_avg: float
    down_avg: float
    combined_avg: float
    net_cost: float
    pnl_up: float
    pnl_down: float
    gp: bool
    buys: int
    sells: int
    budget_deployed_pct: float


def simulate_window(
    window: dict,
    strategy: Strategy,
    fills_by_ts: dict | None = None,
    verbose: bool = False,
) -> WindowResult:
    """Simulate a strategy against one real window."""

    market_ticks = reconstruct_market(window, fills_by_ts or {})
    if not market_ticks:
        return WindowResult(
            window_idx=0,
            timestamp="",
            strategy=strategy.name,
            ticks=0,
            final_up=0,
            final_down=0,
            up_avg=0,
            down_avg=0,
            combined_avg=0,
            net_cost=0,
            pnl_up=0,
            pnl_down=0,
            gp=False,
            buys=0,
            sells=0,
            budget_deployed_pct=0,
        )

    position = Position()
    budget_remaining = strategy.budget

    # Reset strategy state
    if hasattr(strategy, "last_sell_seconds"):
        strategy.last_sell_seconds = -999

    for tick in market_ticks:
        seconds = tick.seconds
        action = strategy.on_tick(tick, position, budget_remaining, seconds)

        # Execute sells first (frees capital)
        if action.sell_up_shares > 0 and action.sell_up_price > 0:
            proceeds = position.sell(True, action.sell_up_shares, action.sell_up_price)
            budget_remaining += proceeds
        if action.sell_down_shares > 0 and action.sell_down_price > 0:
            proceeds = position.sell(
                False, action.sell_down_shares, action.sell_down_price
            )
            budget_remaining += proceeds

        # Execute buys
        if action.buy_up_shares > 0 and action.buy_up_price > 0:
            cost = action.buy_up_shares * action.buy_up_price
            if cost <= budget_remaining:
                position.buy(True, action.buy_up_shares, action.buy_up_price)
                budget_remaining -= cost
        if action.buy_down_shares > 0 and action.buy_down_price > 0:
            cost = action.buy_down_shares * action.buy_down_price
            if cost <= budget_remaining:
                position.buy(False, action.buy_down_shares, action.buy_down_price)
                budget_remaining -= cost

        if verbose and (seconds % 30 == 0 or seconds <= 10 or seconds >= 240):
            sell_info = ""
            if action.sell_up_shares > 0:
                sell_info = (
                    f" SELL UP {action.sell_up_shares}@{action.sell_up_price:.2f}"
                )
            if action.sell_down_shares > 0:
                sell_info = (
                    f" SELL DN {action.sell_down_shares}@{action.sell_down_price:.2f}"
                )
            buy_info = ""
            if action.buy_up_shares > 0:
                buy_info += f" BUY UP {action.buy_up_shares}@{action.buy_up_price:.2f}"
            if action.buy_down_shares > 0:
                buy_info += (
                    f" BUY DN {action.buy_down_shares}@{action.buy_down_price:.2f}"
                )
            mkt = "UP" if tick.yes_bid > tick.no_bid else "DN"
            print(
                f"  T+{seconds:3d}s yes={tick.yes_bid:.2f} no={tick.no_bid:.2f} "
                f"mkt={mkt} prob={tick.prob_up:.2f} "
                f"UP:{position.up_shares:3d}@{position.up_avg:.2f} "
                f"DN:{position.down_shares:3d}@{position.down_avg:.2f} "
                f"net=${position.net_cost:.1f} "
                f"rem=${budget_remaining:.0f}"
                f"{sell_info}{buy_info}"
            )

    deployed_pct = round(
        (strategy.budget - budget_remaining) / strategy.budget * 100, 1
    )

    return WindowResult(
        window_idx=0,
        timestamp=window.get("timestamp", ""),
        strategy=strategy.name,
        ticks=len(market_ticks),
        final_up=position.up_shares,
        final_down=position.down_shares,
        up_avg=position.up_avg,
        down_avg=position.down_avg,
        combined_avg=position.combined_avg,
        net_cost=position.net_cost,
        pnl_up=position.pnl_if_up(),
        pnl_down=position.pnl_if_down(),
        gp=position.is_gp(),
        buys=position.buys_count,
        sells=position.sells_count,
        budget_deployed_pct=deployed_pct,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Replay simulator for BTC 5m windows")
    parser.add_argument(
        "--strategy",
        choices=["actual", "k9"],
        default="k9",
        help="Strategy to simulate (default: k9)",
    )
    parser.add_argument(
        "--budget", type=float, default=150.0, help="Budget per window (default: 150)"
    )
    parser.add_argument(
        "--window", type=int, default=None, help="Run only this window index"
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Show tick-by-tick output"
    )
    parser.add_argument(
        "--compare", action="store_true", help="Run both actual and k9 side by side"
    )
    parser.add_argument(
        "--synthetic",
        action="store_true",
        help="Use synthetic generated markets instead of real data",
    )
    parser.add_argument(
        "--count",
        type=int,
        default=200,
        help="Number of synthetic windows to generate (default: 200)",
    )
    parser.add_argument(
        "--seed", type=int, default=None, help="Random seed for synthetic generation"
    )
    args = parser.parse_args()

    # Load data
    data_dir = Path(__file__).parent.parent / "data"
    dataset_path = data_dir / "replay_dataset.json"
    fills_path = data_dir / "replay_fills.json"

    if args.synthetic:
        print(
            f"Generating {args.count} synthetic windows (seed={args.seed or 'random'})..."
        )
        windows = generate_synthetic_batch(count=args.count, seed=args.seed)
        fills_by_ts = {}

        # Count scenario distribution
        scenario_counts: dict[str, int] = {}
        for w in windows:
            sc = w.get("_scenario", "unknown")
            scenario_counts[sc] = scenario_counts.get(sc, 0) + 1
        true_up_count = sum(1 for w in windows if w.get("_true_up", False))
        print(f"  Scenarios: {scenario_counts}")
        print(
            f"  True outcomes: {true_up_count} UP / {len(windows) - true_up_count} DOWN"
        )
        print()
    else:
        if not dataset_path.exists():
            print(f"ERROR: {dataset_path} not found. Run dump_replay_data.py first.")
            sys.exit(1)

        with open(dataset_path) as f:
            windows = json.load(f)

        fills_by_ts = {}
        if fills_path.exists():
            with open(fills_path) as f:
                fills = json.load(f)
            for fill in fills:
                ts = fill.get("timestamp", "")[:16]
                fills_by_ts.setdefault(ts, []).append(fill)

        print(f"Loaded {len(windows)} windows from {dataset_path.name}")
        print()

    if args.compare:
        strategies = [
            ActualStrategy(budget=args.budget),
            K9Strategy(budget=args.budget),
        ]
    elif args.strategy == "actual":
        strategies = [ActualStrategy(budget=args.budget)]
    else:
        strategies = [K9Strategy(budget=args.budget)]

    for strategy in strategies:
        print(f"{'=' * 80}")
        print(f"  Strategy: {strategy.name.upper()} (budget=${strategy.budget})")
        print(f"{'=' * 80}")
        print()

        results: list[WindowResult] = []
        window_indices = (
            [args.window] if args.window is not None else range(len(windows))
        )

        for i in window_indices:
            if i >= len(windows):
                print(f"Window {i} not found (only {len(windows)} windows)")
                continue

            window = windows[i]

            if args.verbose:
                print(f"--- Window {i}: {window.get('timestamp', '')[:19]} ---")

            result = simulate_window(
                window, strategy, fills_by_ts, verbose=args.verbose
            )
            result.window_idx = i
            results.append(result)

            gp_label = "GP " if result.gp else "DIR"
            print(
                f"  #{i:2d} {result.timestamp[:19]} "
                f"UP:{result.final_up:3d}@{result.up_avg:.2f} "
                f"DN:{result.final_down:3d}@{result.down_avg:.2f} "
                f"comb={result.combined_avg:.3f} "
                f"net=${result.net_cost:6.1f} "
                f"{gp_label} "
                f"buys={result.buys:3d} sells={result.sells:2d} "
                f"deployed={result.budget_deployed_pct:4.0f}% "
                f"if_UP=${result.pnl_up:+7.1f} "
                f"if_DN=${result.pnl_down:+7.1f}"
            )

            if args.verbose:
                print()

        # Summary
        if results:
            print()
            print(f"  {'─' * 76}")
            gp_count = sum(1 for r in results if r.gp)
            total = len(results)
            avg_deployed = sum(r.net_cost for r in results) / total
            avg_deployed_pct = sum(r.budget_deployed_pct for r in results) / total
            total_buys = sum(r.buys for r in results)
            total_sells = sum(r.sells for r in results)

            gp_profit = sum(min(r.pnl_up, r.pnl_down) for r in results if r.gp)
            dir_results = [r for r in results if not r.gp]
            dir_profit_if_right = sum(max(r.pnl_up, r.pnl_down) for r in dir_results)
            dir_loss_if_wrong = sum(min(r.pnl_up, r.pnl_down) for r in dir_results)

            # For synthetic data, we know the true outcome
            if args.synthetic:
                actual_pnl = 0.0
                wins = 0
                losses = 0
                for i_r, r in enumerate(results):
                    w_idx = (
                        r.window_idx
                        if r.window_idx < len(windows)
                        else i_r % len(windows)
                    )
                    true_up = windows[w_idx].get("_true_up", True)
                    pnl = r.pnl_up if true_up else r.pnl_down
                    actual_pnl += pnl
                    if pnl > 0:
                        wins += 1
                    elif pnl < 0:
                        losses += 1
                est_total = actual_pnl
                est_dir_pnl = actual_pnl - gp_profit
            else:
                # At 64% model accuracy
                est_dir_pnl = 0.64 * dir_profit_if_right + 0.36 * dir_loss_if_wrong
                est_total = gp_profit + est_dir_pnl

            print(
                f"  SUMMARY ({strategy.name.upper()}, {total} windows, ${strategy.budget}/window)"
            )
            print(
                f"  GP rate:           {gp_count}/{total} ({gp_count / total * 100:.0f}%) — K9 target: 67%"
            )
            print(f"  GP min profit:     ${gp_profit:.1f}")
            print(
                f"  Avg deployed:      ${avg_deployed:.1f} ({avg_deployed_pct:.0f}%) — K9 target: 80%+"
            )
            print(
                f"  Total buys:        {total_buys} ({total_buys / total:.1f}/window)"
            )
            print(
                f"  Total sells:       {total_sells} ({total_sells / total:.1f}/window)"
            )
            print()
            if args.synthetic:
                print(f"  ACTUAL P&L (synthetic — true outcomes known):")
                print(f"    GP profit (guaranteed):    ${gp_profit:+.1f}")
                print(f"    Directional P&L:           ${est_dir_pnl:+.1f}")
                print(f"    ─────────────────────────────────")
                print(f"    TOTAL ACTUAL P&L:          ${est_total:+.1f}")
                print(f"    Per window:                ${est_total / total:+.2f}")
                print(
                    f"    Win/Loss:                  {wins}W / {losses}L / {total - wins - losses}BE"
                )
                print(f"    Win rate:                  {wins / total * 100:.0f}%")
                print()

                # Breakdown by scenario
                print(f"  BY SCENARIO:")
                scenario_pnls: dict[str, list[float]] = {}
                for i_r, r in enumerate(results):
                    w_idx = (
                        r.window_idx
                        if r.window_idx < len(windows)
                        else i_r % len(windows)
                    )
                    sc = windows[w_idx].get("_scenario", "unknown")
                    true_up = windows[w_idx].get("_true_up", True)
                    pnl = r.pnl_up if true_up else r.pnl_down
                    scenario_pnls.setdefault(sc, []).append(pnl)
                for sc, pnls in sorted(scenario_pnls.items()):
                    avg_pnl = sum(pnls) / len(pnls)
                    win_r = sum(1 for p in pnls if p > 0) / len(pnls) * 100
                    print(
                        f"    {sc:25s} n={len(pnls):3d}  avg=${avg_pnl:+6.1f}  win={win_r:.0f}%  total=${sum(pnls):+8.1f}"
                    )
                print()
            else:
                print(f"  Estimated P&L (at 64% model accuracy):")
                print(f"    GP profit (guaranteed):    ${gp_profit:+.1f}")
                print(
                    f"    DIR profit (if right 64%): ${0.64 * dir_profit_if_right:+.1f}"
                )
                print(
                    f"    DIR loss (if wrong 36%):   ${0.36 * dir_loss_if_wrong:+.1f}"
                )
                print(f"    ─────────────────────────────────")
                print(f"    TOTAL ESTIMATED P&L:       ${est_total:+.1f}")
                print(f"    Per window:                ${est_total / total:+.2f}")
                print()

            # Compare to actual (from window summaries)
            summaries_path = data_dir / "replay_window_summaries.json"
            if summaries_path.exists() and strategy.name != "actual":
                with open(summaries_path) as f:
                    summaries = json.load(f)
                actual_gp = sum(1 for s in summaries if s.get("guaranteed_profit"))
                actual_deployed = sum(s.get("net_cost", 0) for s in summaries) / max(
                    len(summaries), 1
                )
                actual_gp_profit = sum(
                    min(s.get("pnl_if_up", 0), s.get("pnl_if_dn", 0))
                    for s in summaries
                    if s.get("guaranteed_profit")
                )
                print(f"  COMPARISON vs ACTUAL:")
                print(
                    f"    GP rate:    {gp_count}/{total} ({gp_count / total * 100:.0f}%) vs {actual_gp}/{len(summaries)} ({actual_gp / max(len(summaries), 1) * 100:.0f}%)"
                )
                print(f"    Avg deploy: ${avg_deployed:.1f} vs ${actual_deployed:.1f}")
                print(f"    GP profit:  ${gp_profit:.1f} vs ${actual_gp_profit:.1f}")
                print(
                    f"    Est total:  ${est_total:+.1f} vs actual unknown (need resolution data)"
                )
                print()


if __name__ == "__main__":
    main()
