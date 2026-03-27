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
    open_shares_per_side: int = 25          # shares to buy each side at open
    open_window_seconds: int = 180          # allow open up to T+180s (20% of 15m window)
    entry_gate: float = 1.005               # skip window if yes_ask + no_ask > this (15m markets open at ~1.01, tighten quickly)

    # ── Rebalance moments ────────────────────────────────────────────────────
    rebalance_times: tuple = (270, 450, 630)  # T+4.5m, T+7.5m, T+10.5m into 15m window
    rebalance_usd: float = 12.0             # USD to add each neutral rebalance

    # ── Signal thresholds for dump decision ──────────────────────────────────
    dump_threshold_up: float = 0.68    # prob_up > this → dump NO, go YES
    dump_threshold_down: float = 0.32  # prob_up < this → dump YES, go NO

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
        # Allow up to open_window_seconds so a late-joining runner can still open before first rebalance.
        if not self._opened and seconds < p.open_window_seconds:
            return self._open(market, budget_remaining)

        # Phase 2: Rebalance at fixed moments (once each)
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

        # Cost check: make sure we have budget
        cost_per_side = p.open_shares_per_side * max(market.yes_ask, market.no_ask)
        if cost_per_side * 2 > budget_remaining:
            shares = max(p.min_shares, int(budget_remaining * 0.4 / max(market.yes_ask, 0.01)))
            shares = (shares // p.min_shares) * p.min_shares
        else:
            shares = p.open_shares_per_side

        if shares < p.min_shares or market.yes_ask <= 0 or market.no_ask <= 0:
            return action

        action.buy_up_shares = shares
        action.buy_up_price = market.yes_ask
        action.buy_down_shares = shares
        action.buy_down_price = market.no_ask
        action.reason = "V3_OPEN"
        self._opened = True

        logger.info(
            "v3_open shares=%d yes_ask=%.3f no_ask=%.3f combined_entry=%.3f",
            shares, market.yes_ask, market.no_ask, market.yes_ask + market.no_ask,
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

        # Strong UP signal: dump NO, go heavier on YES
        if prob > p.dump_threshold_up and position.down_shares >= p.min_shares:
            action.sell_down_shares = position.down_shares
            action.sell_down_price = market.no_bid
            # Buy YES with proceeds estimate + rebalance budget
            proceeds_est = position.down_shares * market.no_bid
            extra_usd = min(p.rebalance_usd, budget_remaining)
            yes_shares = max(p.min_shares, int((proceeds_est + extra_usd) / max(market.yes_ask, 0.01)))
            yes_shares = (yes_shares // p.min_shares) * p.min_shares
            if yes_shares >= p.min_shares and market.yes_ask > 0:
                action.buy_up_shares = yes_shares
                action.buy_up_price = market.yes_ask
            action.reason = f"V3_DUMP_NO_T{moment}"
            decision = "dump_no"

        # Strong DOWN signal: dump YES, go heavier on NO
        elif prob < p.dump_threshold_down and position.up_shares >= p.min_shares:
            action.sell_up_shares = position.up_shares
            action.sell_up_price = market.yes_bid
            proceeds_est = position.up_shares * market.yes_bid
            extra_usd = min(p.rebalance_usd, budget_remaining)
            no_shares = max(p.min_shares, int((proceeds_est + extra_usd) / max(market.no_ask, 0.01)))
            no_shares = (no_shares // p.min_shares) * p.min_shares
            if no_shares >= p.min_shares and market.no_ask > 0:
                action.buy_down_shares = no_shares
                action.buy_down_price = market.no_ask
            action.reason = f"V3_DUMP_YES_T{moment}"
            decision = "dump_yes"

        # Neutral: buy cheaper side to lower combined_avg
        elif budget_remaining >= p.rebalance_usd * 0.5:
            usd = min(p.rebalance_usd, budget_remaining)
            if market.yes_ask <= market.no_ask and market.yes_ask > 0:
                shares = max(p.min_shares, int(usd / market.yes_ask))
                shares = (shares // p.min_shares) * p.min_shares
                if shares >= p.min_shares:
                    action.buy_up_shares = shares
                    action.buy_up_price = market.yes_ask
                    action.reason = f"V3_NEUTRAL_YES_T{moment}"
                    decision = "neutral_yes"
            elif market.no_ask > 0:
                shares = max(p.min_shares, int(usd / market.no_ask))
                shares = (shares // p.min_shares) * p.min_shares
                if shares >= p.min_shares:
                    action.buy_down_shares = shares
                    action.buy_down_price = market.no_ask
                    action.reason = f"V3_NEUTRAL_NO_T{moment}"
                    decision = "neutral_no"

        self._rebalance_log.append({
            "moment": moment,
            "prob_up": round(prob, 4),
            "yes_bid": round(market.yes_bid, 4),
            "no_bid": round(market.no_bid, 4),
            "decision": decision,
            "up_shares_before": position.up_shares,
            "down_shares_before": position.down_shares,
        })

        logger.info(
            "v3_rebalance T+%ds prob=%.3f decision=%s budget_remaining=%.2f",
            moment, prob, decision, budget_remaining,
        )
        return action
