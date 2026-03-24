"""Strategy module: pluggable trading strategies for the replay simulator and live bot.

Architecture:
    TradingEngine (shared) calls Strategy.on_tick() every second.
    Strategy returns StrategyAction (what to buy/sell).
    Each pair/timeframe gets its own Strategy instance with different parameters.

Strategies:
    K9v2Strategy — market-driven, handles reversals, no direction lock
    AccumulateOnlyStrategy — for SOL/XRP/hourly: just buy both sides, no sells

All strategies follow the K9 Ruleset (see K9_RULESET.md).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum

# ---------------------------------------------------------------------------
# Actions + Position (shared types)
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
    reason: str = ""


@dataclass
class MarketState:
    """Current market state as seen by the strategy."""

    seconds: int = 0
    yes_bid: float = 0.50
    no_bid: float = 0.50
    yes_ask: float = 0.51
    no_ask: float = 0.51
    prob_up: float = 0.50  # model prediction


@dataclass
class Position:
    """Current position held by the bot."""

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
        if side_up:
            shares = min(shares, self.up_shares)
            avg = self.up_avg
            self.up_shares -= shares
            self.up_cost = max(round(self.up_cost - shares * avg, 2), 0.0)
        else:
            shares = min(shares, self.down_shares)
            avg = self.down_avg
            self.down_shares -= shares
            self.down_cost = max(round(self.down_cost - shares * avg, 2), 0.0)
        proceeds = round(shares * price, 2)
        self.sells_count += 1
        self.total_sold_proceeds += proceeds
        return proceeds


# ---------------------------------------------------------------------------
# Strategy profile (per-pair configuration)
# ---------------------------------------------------------------------------


@dataclass
class StrategyProfile:
    """Configuration that differs between pairs/timeframes.

    BTC_5m, SOL_5m, BTC_1h, etc. each get their own profile.
    The strategy logic is shared; only these parameters change.
    """

    name: str = "btc_5m"

    # Budget
    budget: float = 150.0
    open_budget_pct: float = 0.10  # % of budget at open (T+5-15)

    # Price caps
    hard_cap: float = 0.82  # never buy above this

    # Balance
    early_balance_cap: float = 0.75  # max % one side before T+120
    late_balance_cap: float = 0.90  # max % one side after T+120

    # Selling
    sells_enabled: bool = True  # False for SOL/XRP/hourly
    sell_cooldown: int = 10  # seconds between sells
    sell_start: int = 20  # don't sell before this
    sell_end: int = 240  # don't sell after this
    dead_side_threshold: float = 0.80  # sell ALL if other side bid > this
    unfavored_rich_threshold: float = 0.50  # sell unfavored if avg > this
    late_dump_start: int = 180  # start selling near-worthless
    late_dump_threshold: float = 0.25  # bid below this = near-worthless

    # Dying side
    dying_side_threshold: float = 0.70  # don't buy if other bid > this
    dying_side_start: int = 60  # only after this many seconds

    # Timing
    commit_seconds: int = 250  # stop all trading

    # Ladder
    shares_per_order: int = 5
    offsets_main: tuple = (0.00, 0.01, 0.02, 0.04, 0.06, 0.08)
    offsets_late: tuple = (0.01, 0.03, 0.05)
    levels_strong: int = 6  # levels when allocation >= 60%
    levels_medium: int = 4  # levels when allocation >= 40%
    levels_weak: int = 2  # levels below 40%

    # Market vs model
    market_override_edge: float = 0.10  # market overrides model when edge > this
    market_strong_edge: float = 0.20  # very clear market direction
    model_only_edge: float = 0.05  # market unclear, trust model only


# ---------------------------------------------------------------------------
# Pre-built profiles
# ---------------------------------------------------------------------------


BTC_5M_PROFILE = StrategyProfile(
    name="btc_5m",
    budget=150.0,
    sells_enabled=True,
    sell_cooldown=10,
    hard_cap=0.82,
    dying_side_threshold=0.70,
    dead_side_threshold=0.80,
)

SOL_5M_PROFILE = StrategyProfile(
    name="sol_5m",
    budget=50.0,
    sells_enabled=False,  # K9 data: zero sells on SOL 5m
    hard_cap=0.82,
    dying_side_threshold=0.70,
    open_budget_pct=0.08,  # smaller open — SOL moves faster
)

XRP_5M_PROFILE = StrategyProfile(
    name="xrp_5m",
    budget=50.0,
    sells_enabled=False,  # K9 data: zero sells on XRP 5m
    hard_cap=0.82,
    dying_side_threshold=0.70,
)

ETH_5M_PROFILE = StrategyProfile(
    name="eth_5m",
    budget=50.0,
    sells_enabled=True,  # no K9 ETH 5m data, assume BTC-style
    sell_cooldown=10,
    hard_cap=0.82,
    dying_side_threshold=0.70,
)

BTC_1H_PROFILE = StrategyProfile(
    name="btc_1h",
    budget=50.0,
    sells_enabled=False,  # K9 data: zero sells on hourly
    hard_cap=0.82,
    open_budget_pct=0.05,  # very small open — lots of time
    commit_seconds=3540,  # 59 minutes
    dying_side_threshold=0.70,
    dying_side_start=300,  # 5 minutes before checking dying side
)


# ---------------------------------------------------------------------------
# K9v2 Strategy — handles reversals
# ---------------------------------------------------------------------------


class K9v2Strategy:
    """K9-style strategy v2: market-driven, handles reversals.

    Core principles (from K9_RULESET.md):
    1. Market price is truth. yes_bid vs no_bid determines winner. Every tick.
    2. No direction lock. Adapt continuously.
    3. Sell the LOSING side (market determines loser, not model).
    4. Buy both sides, weighted by market + model combined signal.
    5. Deploy 80%+ of budget over the window.
    6. Don't buy dying shares (other side bid > 70c).
    7. Handle reversals: when market flips, flip with it.

    Reversal handling (our #1 weakness — 0% win rate on reversals):
    - Track the market direction from the PREVIOUS tick
    - When direction flips (yes_bid was > no_bid, now it's <), that's a reversal
    - On reversal: aggressively sell the now-losing side
    - On reversal: start buying the now-winning side
    - Don't wait — K9 rebuys within 2 seconds of selling
    """

    def __init__(self, profile: StrategyProfile | None = None):
        self.profile = profile or BTC_5M_PROFILE
        self.name = f"k9v2_{self.profile.name}"

        # State tracked across ticks within one window
        self.last_sell_seconds: int = -999
        self.prev_winning_up: bool | None = None
        self.reversal_detected: bool = False
        self.reversal_count: int = 0
        self.reversal_selling: bool = False  # in active reversal sell mode
        self.reversal_start_seconds: int = 0
        self.prev_yes_bid: float = 0.50
        self.prev_no_bid: float = 0.50
        self.peak_yes_bid: float = 0.50  # track peak to detect reversals early
        self.peak_no_bid: float = 0.50

    def reset(self):
        """Call at start of each new window."""
        self.last_sell_seconds = -999
        self.prev_winning_up = None
        self.reversal_detected = False
        self.reversal_count = 0
        self.reversal_selling = False
        self.reversal_start_seconds = 0
        self.prev_yes_bid = 0.50
        self.prev_no_bid = 0.50
        self.peak_yes_bid = 0.50
        self.peak_no_bid = 0.50

    def _determine_direction(self, market: MarketState) -> tuple[bool, float, str]:
        """Determine who is winning: market-first, model-second.

        Returns: (winning_up, confidence, source)
        - winning_up: True if UP is winning
        - confidence: how sure we are (0.0 to 1.0)
        - source: "market", "model", or "combined"
        """
        p = self.profile
        yes_bid = market.yes_bid
        no_bid = market.no_bid
        prob_up = market.prob_up

        market_edge = abs(yes_bid - no_bid)
        market_up = yes_bid > no_bid
        model_up = prob_up >= 0.50
        model_edge = abs(prob_up - 0.50)

        if market_edge > p.market_strong_edge:
            # Very clear market opinion — trust it completely
            return market_up, 0.90, "market_strong"
        elif market_edge > p.market_override_edge:
            # Clear market opinion — trust it over model
            return market_up, 0.75, "market"
        elif market_edge > p.model_only_edge:
            # Mild market opinion — combine with model
            if market_up == model_up:
                # Agreement — high confidence
                return market_up, 0.70, "combined_agree"
            else:
                # Disagreement — go with market but low confidence
                return market_up, 0.55, "combined_disagree"
        else:
            # Market is 50/50 — use model
            if model_edge > 0.10:
                return model_up, 0.60, "model_confident"
            else:
                return model_up, 0.50, "model_weak"

    def _allocation_split(
        self, winning_up: bool, confidence: float
    ) -> tuple[float, float]:
        """Determine budget allocation between UP and DOWN.

        Returns (up_pct, down_pct) where up_pct + down_pct = 1.0
        """
        if confidence >= 0.85:
            win_pct = 0.80
        elif confidence >= 0.70:
            win_pct = 0.70
        elif confidence >= 0.55:
            win_pct = 0.60
        else:
            win_pct = 0.50  # uncertain — equal split

        if winning_up:
            return win_pct, round(1.0 - win_pct, 2)
        else:
            return round(1.0 - win_pct, 2), win_pct

    def _budget_curve(self, seconds: int) -> float:
        """How much of the budget can be deployed by this point in time."""
        p = self.profile
        open_pct = p.open_budget_pct

        if seconds <= 5:
            return open_pct
        elif seconds <= 60:
            progress = (seconds - 5) / 55.0
            return open_pct + 0.12 * progress  # → 22%
        elif seconds <= 180:
            progress = (seconds - 60) / 120.0
            return 0.22 + 0.60 * progress  # → 82%
        elif seconds <= p.commit_seconds:
            progress = (seconds - 180) / max(p.commit_seconds - 180, 1)
            return 0.82 + 0.10 * progress  # → 92%
        return 0.92

    def _detect_reversal(self, market: MarketState, winning_up: bool) -> bool:
        """Detect reversals early by tracking peak prices.

        A reversal is detected when:
        1. Direction flips (yes_bid was > no_bid, now it's not), OR
        2. The winning side's bid drops > 8c from its peak (momentum shift)

        Early detection is critical — our #1 weakness was detecting
        reversals too late (T+197 when we needed T+120).
        """
        yes_bid = market.yes_bid
        no_bid = market.no_bid

        # Track peaks
        self.peak_yes_bid = max(self.peak_yes_bid, yes_bid)
        self.peak_no_bid = max(self.peak_no_bid, no_bid)

        if self.prev_winning_up is None:
            self.prev_winning_up = winning_up
            self.prev_yes_bid = yes_bid
            self.prev_no_bid = no_bid
            return False

        # Method 1: direction flip
        direction_flipped = winning_up != self.prev_winning_up

        # Method 2: momentum shift — winning side dropped > 8c from peak
        momentum_shift = False
        if self.prev_winning_up and (self.peak_yes_bid - yes_bid) > 0.08:
            momentum_shift = True
        elif not self.prev_winning_up and (self.peak_no_bid - no_bid) > 0.08:
            momentum_shift = True

        self.prev_winning_up = winning_up
        self.prev_yes_bid = yes_bid
        self.prev_no_bid = no_bid

        reversed = direction_flipped or momentum_shift
        if reversed:
            self.reversal_count += 1
            self.reversal_selling = True
            self.reversal_start_seconds = market.seconds
            # Reset peaks for the new direction
            self.peak_yes_bid = yes_bid
            self.peak_no_bid = no_bid

        # Stay in reversal selling mode for 30 seconds after detection
        if self.reversal_selling:
            if (market.seconds - self.reversal_start_seconds) > 30:
                self.reversal_selling = False

        return reversed

    def on_tick(
        self,
        market: MarketState,
        position: Position,
        budget_remaining: float,
    ) -> StrategyAction:
        """Main decision function. Called every tick (1 second).

        Returns what to buy and sell on this tick.
        """
        p = self.profile
        action = StrategyAction()
        seconds = market.seconds
        yes_bid = market.yes_bid
        no_bid = market.no_bid

        # ── COMMIT: no trading after commit time ──
        if seconds >= p.commit_seconds:
            return action

        # ── DIRECTION: market-first, model-second ──
        winning_up, confidence, direction_source = self._determine_direction(market)
        losing_up = not winning_up

        # ── REVERSAL DETECTION ──
        self.reversal_detected = self._detect_reversal(market, winning_up)

        # ── SELL LOGIC ──
        # Sell the LOSING side. Never sell the winning side.
        # Market determines who is losing, not the model.
        if p.sells_enabled and seconds >= p.sell_start and seconds <= p.sell_end:
            sell_cooldown_ok = (seconds - self.last_sell_seconds) >= p.sell_cooldown

            # On reversal or during reversal selling mode: skip cooldown
            if (self.reversal_detected or self.reversal_selling) and seconds >= 30:
                sell_cooldown_ok = True

            if sell_cooldown_ok:
                action = self._decide_sell(
                    market, position, winning_up, confidence, action
                )

        # ── BUY LOGIC ──
        # Buy both sides, weighted by direction.
        # Don't buy dying shares.
        max_deploy = p.budget * self._budget_curve(seconds)
        currently_deployed = position.net_cost
        curve_remaining = max(max_deploy - currently_deployed, 0)
        usable = min(budget_remaining, curve_remaining)

        if usable >= 0.50 and seconds < p.commit_seconds:
            action = self._decide_buy(
                market, position, winning_up, confidence, usable, action
            )

        return action

    def _decide_sell(
        self,
        market: MarketState,
        position: Position,
        winning_up: bool,
        confidence: float,
        action: StrategyAction,
    ) -> StrategyAction:
        """Decide what to sell on this tick.

        Rules:
        1. Only sell the LOSING side (determined by market)
        2. DEAD_SIDE: if other bid > 80c, sell everything on losing side
        3. UNFAVORED_RICH: if losing side avg > 50c and market edge > 10c
        4. LATE_DUMP: after T+180, sell anything with bid < 25c
        5. REVERSAL: on direction flip, sell the now-losing side immediately
        6. Never sell the winning side
        """
        p = self.profile
        seconds = market.seconds
        yes_bid = market.yes_bid
        no_bid = market.no_bid
        market_edge = abs(yes_bid - no_bid)

        losing_up = not winning_up

        # Which side are we considering selling?
        if losing_up:
            losing_shares = position.up_shares
            losing_bid = yes_bid
            losing_avg = position.up_avg
            other_bid = no_bid
        else:
            losing_shares = position.down_shares
            losing_bid = no_bid
            losing_avg = position.down_avg
            other_bid = yes_bid

        shares_to_sell = 0
        sell_price = losing_bid
        reason = ""

        # DEAD_SIDE: other bid > 80c — this side is almost certainly going to $0
        if other_bid > p.dead_side_threshold and losing_shares >= p.shares_per_order:
            # Sell up to 15 shares at once — it's going to zero anyway
            shares_to_sell = min(losing_shares, p.shares_per_order * 3)
            reason = "DEAD_SIDE"

        # REVERSAL / REVERSAL_SELLING: direction flipped or momentum shifted
        # Sell AGGRESSIVELY — up to 15 shares per tick during reversal mode
        elif (
            (self.reversal_detected or self.reversal_selling)
            and losing_shares >= p.shares_per_order
            and seconds >= 20
        ):
            # Sell more on first detection, then steady 10/tick during reversal mode
            if self.reversal_detected:
                shares_to_sell = min(losing_shares, p.shares_per_order * 3)
            else:
                shares_to_sell = min(losing_shares, p.shares_per_order * 2)
            reason = "REVERSAL"

        # UNFAVORED_RICH: losing side has expensive avg and we're clearly losing
        elif (
            losing_avg > p.unfavored_rich_threshold
            and market_edge > 0.10
            and losing_shares >= p.shares_per_order
        ):
            shares_to_sell = min(losing_shares, p.shares_per_order * 2)
            reason = "UNFAVORED_RICH"

        # LATE_DUMP: near end of window, dump worthless shares
        elif (
            seconds >= p.late_dump_start
            and losing_bid < p.late_dump_threshold
            and losing_bid > 0
            and losing_shares >= p.shares_per_order
        ):
            shares_to_sell = min(losing_shares, p.shares_per_order)
            reason = "LATE_DUMP"

        if shares_to_sell > 0 and sell_price > 0:
            if losing_up:
                action.sell_up_shares = shares_to_sell
                action.sell_up_price = sell_price
            else:
                action.sell_down_shares = shares_to_sell
                action.sell_down_price = sell_price
            action.reason = reason
            self.last_sell_seconds = seconds

        return action

    def _decide_buy(
        self,
        market: MarketState,
        position: Position,
        winning_up: bool,
        confidence: float,
        usable: float,
        action: StrategyAction,
    ) -> StrategyAction:
        """Decide what to buy on this tick.

        Rules:
        1. Both sides get orders, weighted by direction
        2. Hard cap: never buy above 82c
        3. Dying side: don't buy if other bid > 70c
        4. Balance cap: 75% before T+120, 90% after
        5. Sell-and-rebuy: if we just sold, immediately buy the winning side
        """
        p = self.profile
        seconds = market.seconds
        yes_bid = market.yes_bid
        no_bid = market.no_bid

        up_pct, down_pct = self._allocation_split(winning_up, confidence)
        up_budget = usable * up_pct
        down_budget = usable * down_pct

        # Dynamic balance cap
        balance_cap = p.late_balance_cap if seconds >= 120 else p.early_balance_cap
        total = position.total_shares

        # --- BUY UP ---
        can_buy_up = True

        # Hard cap
        if yes_bid <= 0 or yes_bid > p.hard_cap:
            can_buy_up = False

        # Dying side: don't buy UP if DOWN bid > threshold (UP is dying)
        if seconds >= p.dying_side_start and no_bid > p.dying_side_threshold:
            can_buy_up = False

        # Balance cap
        if total >= 10:
            up_after = position.up_shares + p.shares_per_order
            if up_after / (total + p.shares_per_order) > balance_cap:
                can_buy_up = False

        # Budget check
        up_cost = p.shares_per_order * yes_bid
        if up_cost > up_budget:
            can_buy_up = False

        if can_buy_up:
            action.buy_up_shares = p.shares_per_order
            action.buy_up_price = yes_bid

        # --- BUY DOWN ---
        can_buy_down = True

        if no_bid <= 0 or no_bid > p.hard_cap:
            can_buy_down = False

        if seconds >= p.dying_side_start and yes_bid > p.dying_side_threshold:
            can_buy_down = False

        if total >= 10:
            dn_after = position.down_shares + p.shares_per_order
            if dn_after / (total + p.shares_per_order) > balance_cap:
                can_buy_down = False

        dn_cost = p.shares_per_order * no_bid
        if dn_cost > down_budget:
            can_buy_down = False

        if can_buy_down:
            action.buy_down_shares = p.shares_per_order
            action.buy_down_price = no_bid

        # --- SELL-AND-REBUY ---
        # If we sold on this tick, also buy the winning side
        if action.sell_up_shares > 0 and action.buy_down_shares == 0:
            # Sold UP (losing), try to buy DOWN (winning)
            if (
                no_bid > 0
                and no_bid <= p.hard_cap
                and yes_bid <= p.dying_side_threshold
            ):
                action.buy_down_shares = p.shares_per_order
                action.buy_down_price = no_bid
        elif action.sell_down_shares > 0 and action.buy_up_shares == 0:
            # Sold DOWN (losing), try to buy UP (winning)
            if (
                yes_bid > 0
                and yes_bid <= p.hard_cap
                and no_bid <= p.dying_side_threshold
            ):
                action.buy_up_shares = p.shares_per_order
                action.buy_up_price = yes_bid

        return action


# ---------------------------------------------------------------------------
# Accumulate-only strategy (SOL, XRP, hourly)
# ---------------------------------------------------------------------------


class AccumulateOnlyStrategy:
    """Simple strategy: buy both sides continuously, never sell.

    Used for:
    - SOL_5m (K9 data: zero sells across 8 windows)
    - XRP_5m (K9 data: zero sells across 8 windows)
    - All hourly markets (K9 data: zero sells)

    This is simpler than K9v2 because it doesn't need sell logic,
    reversal detection, or market-vs-model arbitration.
    """

    def __init__(self, profile: StrategyProfile | None = None):
        self.profile = profile or SOL_5M_PROFILE
        self.name = f"accumulate_{self.profile.name}"

    def reset(self):
        pass

    def _budget_curve(self, seconds: int) -> float:
        p = self.profile
        open_pct = p.open_budget_pct

        if seconds <= 5:
            return open_pct
        elif seconds <= 60:
            progress = (seconds - 5) / 55.0
            return open_pct + 0.12 * progress
        elif seconds <= 180:
            progress = (seconds - 60) / 120.0
            return 0.22 + 0.60 * progress
        elif seconds <= p.commit_seconds:
            progress = (seconds - 180) / max(p.commit_seconds - 180, 1)
            return 0.82 + 0.10 * progress
        return 0.92

    def on_tick(
        self,
        market: MarketState,
        position: Position,
        budget_remaining: float,
    ) -> StrategyAction:
        p = self.profile
        action = StrategyAction()
        seconds = market.seconds
        yes_bid = market.yes_bid
        no_bid = market.no_bid
        prob_up = market.prob_up

        if seconds >= p.commit_seconds:
            return action

        # Simple model-driven split (no market override for accumulate-only)
        model_up = prob_up >= 0.50
        model_edge = abs(prob_up - 0.50)

        if model_edge > 0.15:
            win_pct = 0.70
        elif model_edge > 0.05:
            win_pct = 0.60
        else:
            win_pct = 0.50

        up_pct = win_pct if model_up else (1.0 - win_pct)
        down_pct = 1.0 - up_pct

        max_deploy = p.budget * self._budget_curve(seconds)
        currently_deployed = position.net_cost
        curve_remaining = max(max_deploy - currently_deployed, 0)
        usable = min(budget_remaining, curve_remaining)

        if usable < 0.50:
            return action

        up_budget = usable * up_pct
        down_budget = usable * down_pct

        # Balance cap
        balance_cap = p.late_balance_cap if seconds >= 120 else p.early_balance_cap
        total = position.total_shares

        # Buy UP
        if yes_bid > 0 and yes_bid <= p.hard_cap:
            up_cost = p.shares_per_order * yes_bid
            if up_cost <= up_budget:
                blocked = False
                if total >= 10:
                    up_after = position.up_shares + p.shares_per_order
                    if up_after / (total + p.shares_per_order) > balance_cap:
                        blocked = True
                # Dying side check
                if seconds >= p.dying_side_start and no_bid > p.dying_side_threshold:
                    blocked = True
                if not blocked:
                    action.buy_up_shares = p.shares_per_order
                    action.buy_up_price = yes_bid

        # Buy DOWN
        if no_bid > 0 and no_bid <= p.hard_cap:
            dn_cost = p.shares_per_order * no_bid
            if dn_cost <= down_budget:
                blocked = False
                if total >= 10:
                    dn_after = position.down_shares + p.shares_per_order
                    if dn_after / (total + p.shares_per_order) > balance_cap:
                        blocked = True
                if seconds >= p.dying_side_start and yes_bid > p.dying_side_threshold:
                    blocked = True
                if not blocked:
                    action.buy_down_shares = p.shares_per_order
                    action.buy_down_price = no_bid

        return action


# ---------------------------------------------------------------------------
# Strategy factory
# ---------------------------------------------------------------------------


def get_strategy(pair: str) -> K9v2Strategy | AccumulateOnlyStrategy:
    """Get the right strategy for a given pair.

    Uses K9 data to determine:
    - BTC_5m: K9v2 with sells (K9 sells actively on BTC)
    - SOL_5m: AccumulateOnly (K9 has zero sells on SOL)
    - XRP_5m: AccumulateOnly (K9 has zero sells on XRP)
    - ETH_5m: K9v2 with sells (no K9 data, assume BTC-style)
    - *_1h: AccumulateOnly (K9 has zero sells on hourly)
    """
    pair = pair.upper()

    profiles = {
        "BTC_5M": (K9v2Strategy, BTC_5M_PROFILE),
        "ETH_5M": (K9v2Strategy, ETH_5M_PROFILE),
        "SOL_5M": (AccumulateOnlyStrategy, SOL_5M_PROFILE),
        "XRP_5M": (AccumulateOnlyStrategy, XRP_5M_PROFILE),
        "BTC_1H": (AccumulateOnlyStrategy, BTC_1H_PROFILE),
        "ETH_1H": (
            AccumulateOnlyStrategy,
            StrategyProfile(
                name="eth_1h", budget=50.0, sells_enabled=False, commit_seconds=3540
            ),
        ),
        "SOL_1H": (
            AccumulateOnlyStrategy,
            StrategyProfile(
                name="sol_1h", budget=50.0, sells_enabled=False, commit_seconds=3540
            ),
        ),
        "XRP_1H": (
            AccumulateOnlyStrategy,
            StrategyProfile(
                name="xrp_1h", budget=50.0, sells_enabled=False, commit_seconds=3540
            ),
        ),
    }

    strategy_cls, profile = profiles.get(pair, (K9v2Strategy, BTC_5M_PROFILE))
    return strategy_cls(profile=profile)


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Quick test: run K9v2 against a simple scenario
    print("Testing K9v2Strategy...")

    strategy = K9v2Strategy(profile=BTC_5M_PROFILE)
    strategy.reset()
    pos = Position()
    budget = 150.0

    # Simulate: market goes UP
    for sec in range(5, 255):
        t = sec / 300.0
        yes_bid = 0.50 + 0.30 * t  # UP from 50c to 80c
        no_bid = 1.0 - yes_bid

        market = MarketState(
            seconds=sec,
            yes_bid=round(yes_bid, 3),
            no_bid=round(no_bid, 3),
            prob_up=0.55 if sec < 60 else 0.65,
        )

        action = strategy.on_tick(market, pos, budget)

        # Execute
        if action.sell_up_shares > 0:
            proceeds = pos.sell(True, action.sell_up_shares, action.sell_up_price)
            budget += proceeds
        if action.sell_down_shares > 0:
            proceeds = pos.sell(False, action.sell_down_shares, action.sell_down_price)
            budget += proceeds
        if action.buy_up_shares > 0:
            cost = action.buy_up_shares * action.buy_up_price
            if cost <= budget:
                pos.buy(True, action.buy_up_shares, action.buy_up_price)
                budget -= cost
        if action.buy_down_shares > 0:
            cost = action.buy_down_shares * action.buy_down_price
            if cost <= budget:
                pos.buy(False, action.buy_down_shares, action.buy_down_price)
                budget -= cost

        if sec % 30 == 0:
            print(
                f"  T+{sec:3d}s yes={market.yes_bid:.2f} no={market.no_bid:.2f} "
                f"UP:{pos.up_shares:3d}@{pos.up_avg:.2f} DN:{pos.down_shares:3d}@{pos.down_avg:.2f} "
                f"net=${pos.net_cost:.1f} buys={pos.buys_count} sells={pos.sells_count} "
                f"rem=${budget:.0f}"
                f"{' SELL:' + action.reason if action.reason else ''}"
            )

    print(
        f"\nFinal: UP:{pos.up_shares}@{pos.up_avg:.2f} DN:{pos.down_shares}@{pos.down_avg:.2f}"
    )
    print(f"Net cost: ${pos.net_cost:.2f}, Budget remaining: ${budget:.2f}")
    print(f"PnL if UP wins: ${pos.pnl_if_up():.2f}")
    print(f"PnL if DOWN wins: ${pos.pnl_if_down():.2f}")
    print(f"GP: {pos.is_gp()}")
    print(f"Buys: {pos.buys_count}, Sells: {pos.sells_count}")
    print(f"Reversals detected: {strategy.reversal_count}")

    # Test reversal scenario
    print("\n\nTesting reversal scenario...")
    strategy = K9v2Strategy(profile=BTC_5M_PROFILE)
    strategy.reset()
    pos = Position()
    budget = 150.0

    for sec in range(5, 255):
        t = sec / 300.0
        if sec < 120:
            # First half: UP
            yes_bid = 0.50 + 0.25 * (sec / 120.0)
        else:
            # Second half: reversal DOWN
            yes_bid = 0.75 - 0.40 * ((sec - 120) / 135.0)

        yes_bid = max(0.02, min(0.98, yes_bid))
        no_bid = max(0.02, round(1.0 - yes_bid, 3))

        market = MarketState(
            seconds=sec,
            yes_bid=round(yes_bid, 3),
            no_bid=no_bid,
            prob_up=0.60 if sec < 100 else 0.40,
        )

        action = strategy.on_tick(market, pos, budget)

        if action.sell_up_shares > 0:
            budget += pos.sell(True, action.sell_up_shares, action.sell_up_price)
        if action.sell_down_shares > 0:
            budget += pos.sell(False, action.sell_down_shares, action.sell_down_price)
        if action.buy_up_shares > 0:
            cost = action.buy_up_shares * action.buy_up_price
            if cost <= budget:
                pos.buy(True, action.buy_up_shares, action.buy_up_price)
                budget -= cost
        if action.buy_down_shares > 0:
            cost = action.buy_down_shares * action.buy_down_price
            if cost <= budget:
                pos.buy(False, action.buy_down_shares, action.buy_down_price)
                budget -= cost

        if sec % 30 == 0 or (action.reason and "REVERSAL" in action.reason):
            print(
                f"  T+{sec:3d}s yes={market.yes_bid:.2f} no={market.no_bid:.2f} "
                f"UP:{pos.up_shares:3d}@{pos.up_avg:.2f} DN:{pos.down_shares:3d}@{pos.down_avg:.2f} "
                f"net=${pos.net_cost:.1f} buys={pos.buys_count} sells={pos.sells_count} "
                f"rem=${budget:.0f}"
                f"{' ← ' + action.reason if action.reason else ''}"
            )

    # DOWN wins in reversal scenario
    print(
        f"\nFinal: UP:{pos.up_shares}@{pos.up_avg:.2f} DN:{pos.down_shares}@{pos.down_avg:.2f}"
    )
    print(f"Net cost: ${pos.net_cost:.2f}")
    print(f"PnL if UP wins: ${pos.pnl_if_up():.2f}")
    print(f"PnL if DOWN wins: ${pos.pnl_if_down():.2f}")
    print(f"GP: {pos.is_gp()}")
    print(f"Reversals detected: {strategy.reversal_count}")
    print(f"Expected outcome: DOWN wins → PnL = ${pos.pnl_if_down():.2f}")
