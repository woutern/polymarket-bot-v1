"""V3SimpleStrategy — signal-driven dual-side strategy with fixed rebalance moments.

Philosophy:
    1. Open both sides equally at T+5s (guaranteed-profit setup if combined_avg < $1)
    2. At 3 fixed rebalance moments, check LightGBM signal:
       - Strong signal → dump the losing side, concentrate on winner
       - Neutral       → buy the cheaper side to lower combined_avg
    3. Commit and hold at T+250s. No selling outside rebalance moments.

This replaces the complex per-tick MarketMakerStrategy with just 4 decision points
per window, eliminating the churn that caused $1,782 in losses on March 22-24.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from polybot.core.position import Position
from polybot.strategy.base import MarketState, StrategyAction

logger = logging.getLogger(__name__)


@dataclass
class V3Params:
    """Tunable parameters for V3SimpleStrategy.

    These are adjusted by V3Learner after each batch of windows.
    All fields also satisfy the minimal StrategyProfile interface used by Engine.
    """

    name: str = "btc_v3"

    # ── Engine-required interface (same names as StrategyProfile) ────────────
    budget: float = 80.0
    commit_seconds: int = 750               # 15m window: commit at T+750 (83% of 900s)
    open_budget_pct: float = 0.25   # used by Engine._compute_phase only

    # ── Open phase ──────────────────────────────────────────────────────────
    open_shares_per_side: int = 5           # small placeholder open; main deployment via rebalances
    open_window_seconds: int = 600          # allow open up to T+600s; late-join bots skip past rebalances
    entry_gate: float = 1.010               # enter when combined ≤ this (15m markets open ~1.01)
    entry_max_imbalance: float = 0.30       # skip open when |yes_ask - no_ask| > this (directional markets won't tighten, rebalances won't fire)
    rebalance_gate: float = 0.999           # neutral rebalances only in GP territory (combined < $1.00); entry_gate allows the open at 1.010 but we don't pile on unless it's profitable

    # ── Rebalance moments ────────────────────────────────────────────────────
    rebalance_times: tuple = (270, 450, 630)  # T+4.5m, T+7.5m, T+10.5m into 15m window
    rebalance_usd: float = 25.0             # USD to add each neutral rebalance (primary deployment; 3×$25 = $75 on good windows)

    # ── Signal thresholds for dump decision ──────────────────────────────────
    # BTC_15m model output range observed: 0.452–0.596. Thresholds must be within
    # this range or dumps never fire. Set just inside the extremes.
    dump_threshold_up: float = 0.58    # prob_up > this → dump NO, go YES
    dump_threshold_down: float = 0.45  # prob_up < this → dump YES, go NO

    # ── Order constraints ────────────────────────────────────────────────────
    min_shares: int = 5                # Polymarket minimum order size


class V3SimpleStrategy:
    """Market-making strategy with 4 fixed decision points per window.

    Replaces the 300-tick-per-window MarketMakerStrategy.
    Call reset() at the start of each window.
    """

    def __init__(self, params: V3Params | None = None):
        self.params = params or V3Params()
        self.name = f"v3_{self.params.name}"

        # Per-window state
        self._opened: bool = False
        self._rebalanced: set[int] = set()
        self._rebalance_log: list[dict] = []   # for V3Learner

    def reset(self) -> None:
        """Call at the start of each new window."""
        self._opened = False
        self._rebalanced = set()
        self._rebalance_log = []

    def rebalance_log(self) -> list[dict]:
        """Return log of rebalance decisions this window (for learner)."""
        return list(self._rebalance_log)

    # ── Main tick ─────────────────────────────────────────────────────────────

    def on_tick(
        self,
        market: MarketState,
        position: Position,
        budget_remaining: float,
    ) -> StrategyAction:
        """Called every second. Only acts at 4 fixed moments."""
        p = self.params
        seconds = market.seconds

        # Committed: no trading
        if seconds >= p.commit_seconds:
            return StrategyAction()

        # Phase 1: Open — at T+5s normally, or immediately if we join mid-window.
        # Allow up to open_window_seconds so a late-joining runner can still open.
        if not self._opened and seconds < p.open_window_seconds:
            action = self._open(market, budget_remaining)
            if self._opened:
                # Skip rebalance moments already past so we don't backfill them
                for rt in p.rebalance_times:
                    if seconds >= rt:
                        self._rebalanced.add(rt)
            return action

        # Phase 2: Rebalance at fixed moments (once each)
        # Skip all rebalances if we never opened — no one-sided builds without a base position
        if not self._opened:
            return StrategyAction()

        for rt in p.rebalance_times:
            if seconds >= rt and rt not in self._rebalanced:
                self._rebalanced.add(rt)
                return self._rebalance(market, position, budget_remaining, rt)

        return StrategyAction()

    # ── Open ─────────────────────────────────────────────────────────────────

    def _open(self, market: MarketState, budget_remaining: float) -> StrategyAction:
        p = self.params
        action = StrategyAction()

        # Entry gate: retry each tick until spread is cheap enough or open window closes
        if market.yes_ask > 0 and market.no_ask > 0:
            combined_entry = market.yes_ask + market.no_ask
            if combined_entry > p.entry_gate:
                logger.debug(
                    "v3_open_waiting combined_entry=%.3f gate=%.3f",
                    combined_entry, p.entry_gate,
                )
                return action  # keep trying next tick
            # Directional-market filter: if prices are far apart, rebalances won't fire
            if abs(market.yes_ask - market.no_ask) > p.entry_max_imbalance:
                logger.debug(
                    "v3_open_skipping_directional yes=%.3f no=%.3f imbalance=%.3f",
                    market.yes_ask, market.no_ask,
                    abs(market.yes_ask - market.no_ask),
                )
                return action  # keep trying next tick

            # Signal gate: at combined ≥ 1.005 buying pairs guarantees a loss unless
            # the dump signal fires. Only open if we're already near GP OR have a real signal.
            near_gp = combined_entry < 1.005
            has_signal = (market.prob_up > p.dump_threshold_up or
                          market.prob_up < p.dump_threshold_down)
            if not near_gp and not has_signal:
                logger.debug(
                    "v3_open_skipping_no_signal combined=%.3f prob=%.3f thresholds=%.2f/%.2f",
                    combined_entry, market.prob_up, p.dump_threshold_up, p.dump_threshold_down,
                )
                return action  # keep trying — wait for signal or tighter spread

        # Cost check: make sure we have budget
        cost_per_side = p.open_shares_per_side * max(market.yes_ask, market.no_ask)
        if cost_per_side * 2 > budget_remaining:
            shares = max(p.min_shares, int(budget_remaining * 0.4 / max(market.yes_ask, 0.01)))
            shares = (shares // p.min_shares) * p.min_shares
        else:
            shares = p.open_shares_per_side

        if shares < p.min_shares or market.yes_ask <= 0 or market.no_ask <= 0:
            return action

        yes_ask_levels = getattr(market, "yes_ask_levels", [])
        no_ask_levels = getattr(market, "no_ask_levels", [])

        # Signal-weighted open: lean toward model's favoured side (max 70/30 split).
        # At prob=0.50 → equal shares. At prob≥0.65 → 70% main / 30% hedge.
        # Keeps both sides open for GP structure while reducing avg cost on winning side.
        prob = market.prob_up
        lean = min(0.70, max(0.30, 0.50 + (prob - 0.50) * 2.0))  # 0.50 ± scaled
        yes_shares = max(p.min_shares, int(shares * lean * 2 / (lean + (1 - lean) + 1e-9)))
        no_shares = max(p.min_shares, int(shares * (1 - lean) * 2 / (lean + (1 - lean) + 1e-9)))
        # Simplify: just use lean directly on base shares
        yes_shares = max(p.min_shares, round(shares * lean))
        no_shares = max(p.min_shares, round(shares * (1 - lean)))

        yes_limit = sweep_limit_price(yes_ask_levels, yes_shares, market.yes_ask)
        no_limit = sweep_limit_price(no_ask_levels, no_shares, market.no_ask)

        action.buy_up_shares = yes_shares
        action.buy_up_price = yes_limit
        action.buy_down_shares = no_shares
        action.buy_down_price = no_limit
        action.reason = "V3_OPEN"
        self._opened = True

        logger.info(
            "v3_open prob=%.3f lean=%.2f yes=%d@%.3f no=%d@%.3f combined_entry=%.3f",
            prob, lean, yes_shares, yes_limit, no_shares, no_limit,
            market.yes_ask + market.no_ask,
        )
        return action

    # ── Rebalance ────────────────────────────────────────────────────────────

    def _rebalance(
        self,
        market: MarketState,
        position: Position,
        budget_remaining: float,
        moment: int,
    ) -> StrategyAction:
        p = self.params
        action = StrategyAction()
        prob = market.prob_up

        decision = "none"

        # Depth at this moment (shares available at best price; 0 = unknown)
        yes_ask_size = getattr(market, "yes_ask_size", 0.0)
        no_ask_size = getattr(market, "no_ask_size", 0.0)
        yes_ask_levels = getattr(market, "yes_ask_levels", [])
        no_ask_levels = getattr(market, "no_ask_levels", [])

        # Strong UP signal: dump NO, go heavier on YES
        if prob > p.dump_threshold_up and position.down_shares >= p.min_shares:
            action.sell_down_shares = position.down_shares
            action.sell_down_price = market.no_bid
            proceeds_est = position.down_shares * market.no_bid
            total_available = proceeds_est + budget_remaining
            if market.yes_ask > 0:
                # Size conservatively: use the HIGHER of live ask vs depth level.
                # ask_levels may be fresher (from snapshot) if live ask drifted low due
                # to a stale/corrupted incremental update. Using max() prevents computing
                # thousands of shares when best_ask is temporarily near-zero.
                depth_ask = yes_ask_levels[0][0] if yes_ask_levels else market.yes_ask
                sizing_ask = max(market.yes_ask, depth_ask)
                yes_shares = max(p.min_shares, int(total_available / max(sizing_ask, 0.01)))
                # Re-cap using actual sweep limit price (reflects real available liquidity).
                # Always take max with live ask so the order crosses the market and fills
                # immediately even when depth levels are stale from the initial WS snapshot.
                yes_limit = max(sweep_limit_price(yes_ask_levels, yes_shares, market.yes_ask),
                                market.yes_ask)
                yes_shares = max(p.min_shares, int(total_available / max(yes_limit, 0.01)))
                # Hard budget cap: YES buy cost = up_cost after NO sell zeroes down_cost.
                # Prevents corrupt/stale WS no_bid values from inflating proceeds_est and
                # causing a 2× budget overrun (e.g. no_bid=0.98 when actual is 0.48).
                yes_budget = max(0.0, p.budget - position.up_cost)
                yes_shares = min(yes_shares, max(p.min_shares, int(yes_budget / max(yes_limit, 0.01))))
                if yes_shares >= p.min_shares:
                    action.buy_up_shares = yes_shares
                    action.buy_up_price = yes_limit
            action.reason = f"V3_DUMP_NO_T{moment}"
            decision = "dump_no"

        # Strong DOWN signal: dump YES, go heavier on NO
        elif prob < p.dump_threshold_down and position.up_shares >= p.min_shares:
            action.sell_up_shares = position.up_shares
            action.sell_up_price = market.yes_bid
            proceeds_est = position.up_shares * market.yes_bid
            total_available = proceeds_est + budget_remaining
            if market.no_ask > 0:
                depth_ask = no_ask_levels[0][0] if no_ask_levels else market.no_ask
                sizing_ask = max(market.no_ask, depth_ask)
                no_shares = max(p.min_shares, int(total_available / max(sizing_ask, 0.01)))
                # Re-cap using actual sweep limit price; always ≥ live ask so order fills.
                no_limit = max(sweep_limit_price(no_ask_levels, no_shares, market.no_ask),
                               market.no_ask)
                no_shares = max(p.min_shares, int(total_available / max(no_limit, 0.01)))
                # Hard budget cap: NO buy cost = down_cost after YES sell zeroes up_cost.
                # Prevents corrupt/stale WS yes_bid values from inflating proceeds_est.
                no_budget = max(0.0, p.budget - position.down_cost)
                no_shares = min(no_shares, max(p.min_shares, int(no_budget / max(no_limit, 0.01))))
                if no_shares >= p.min_shares:
                    action.buy_down_shares = no_shares
                    action.buy_down_price = no_limit
            action.reason = f"V3_DUMP_YES_T{moment}"
            decision = "dump_yes"

        # Neutral: buy equal SHARES of both sides — but only when combined <= rebalance_gate.
        # If combined > rebalance_gate at this moment, adding shares doesn't improve GP structure
        # (we'd be buying pairs at > $1.00, which are break-even or losing). Skip instead.
        elif (budget_remaining >= p.rebalance_usd * 0.5 and market.yes_ask > 0 and market.no_ask > 0
              and market.yes_ask + market.no_ask <= p.rebalance_gate):
            combined = market.yes_ask + market.no_ask
            usd = min(p.rebalance_usd, budget_remaining)
            shares = max(p.min_shares, int(usd / combined))
            if shares >= p.min_shares:
                action.buy_up_shares = shares
                action.buy_up_price = sweep_limit_price(yes_ask_levels, shares, market.yes_ask)
                action.buy_down_shares = shares
                action.buy_down_price = sweep_limit_price(no_ask_levels, shares, market.no_ask)
                action.reason = f"V3_NEUTRAL_BOTH_T{moment}"
                decision = "neutral_both"

        self._rebalance_log.append({
            "moment": moment,
            "prob_up": round(prob, 4),
            "yes_bid": round(market.yes_bid, 4),
            "no_bid": round(market.no_bid, 4),
            "decision": decision,
            "up_shares_before": position.up_shares,
            "down_shares_before": position.down_shares,
            "yes_ask_size": round(yes_ask_size, 1),
            "no_ask_size": round(no_ask_size, 1),
        })

        logger.info(
            "v3_rebalance T+%ds prob=%.3f decision=%s budget_remaining=%.2f "
            "yes_ask_size=%.1f no_ask_size=%.1f depth=%s/%s",
            moment, prob, decision, budget_remaining,
            yes_ask_size, no_ask_size,
            yes_ask_levels[:2], no_ask_levels[:2],
        )
        return action


# ── Helpers ──────────────────────────────────────────────────────────────────

def sweep_limit_price(levels: list, want_shares: int, fallback_price: float) -> float:
    """Return the limit price needed to fill `want_shares` by sweeping ask levels.

    Iterates through ask levels (sorted ascending by price) and returns the price
    of the deepest level needed to accumulate enough shares.  If depth is unknown
    or insufficient, returns `fallback_price` (the current best ask).

    Example: levels=[(0.51, 5), (0.52, 20)], want=18 → returns 0.52
             levels=[(0.51, 5), (0.52, 20)], want=4  → returns 0.51
             levels=[], want=18                        → returns fallback_price
    """
    if not levels:
        return fallback_price

    accumulated = 0
    limit = fallback_price
    for price, size in levels:
        limit = price
        accumulated += int(size)
        if accumulated >= want_shares:
            break
    return round(limit, 4)
