"""MarketMakerStrategy — dual-side market-making with direction bias.

Core goal: combined_avg < $1.00 → guaranteed profit at resolution.

How it works:
1. Buy BOTH sides every tick, weighted by direction.
2. Track payout_floor = min(up_shares, down_shares).
   Each floor pair pays $1.00 at resolution.
   If we bought at combined_avg < $1.00, every floor pair is profitable.
3. Sell excess shares on the LOSING side when selling beats holding.
   Proceeds fund more buys on the WINNING side.
4. Direction from market (yes_bid vs no_bid), not the model.
   Model used for allocation split only.
5. Handle reversals: when market flips, sell losing side fast, buy winning side.

Key mechanics (in order of importance):
A. PAYOUT_FLOOR sell — sells excess shares above min(up,down) when bid > hold_value
B. DEAD_SIDE sell — other bid > 80c → dump losing side
C. REVERSAL sell — direction flip or peak drop > 8c → aggressive exit from losing side
D. UNFAVORED_RICH sell — losing side avg > 50c and market edge > 10c
E. LATE_DUMP sell — T+180+, bid < 25c for 5 consecutive ticks
F. Payout floor mechanic — the primary driver of combined_avg < $1.00

Anti-patterns this strategy avoids:
- Direction lock (old bug that cost us $60 in one window)
- Selling the winning side (churn loop)
- Buying dying shares (other side > 70c)
- Over-committing early (slow budget curve)
"""

from __future__ import annotations

from polybot.core.position import Position
from polybot.strategy.base import MarketState, StrategyAction
from polybot.strategy.profile import StrategyProfile
from polybot.strategy.profiles import BTC_5M_PROFILE


class MarketMakerStrategy:
    """Market-driven dual-side strategy for BTC_5m and ETH_5m.

    Call reset() at the start of each window.
    Call on_tick() every second during the window.
    """

    def __init__(self, profile: StrategyProfile | None = None):
        self.profile = profile or BTC_5M_PROFILE
        self.name = f"mm_{self.profile.name}"

        # Tick state — reset each window
        self.last_sell_seconds: int = -999
        self.prev_winning_up: bool | None = None
        self.reversal_count: int = 0
        self.reversal_selling: bool = False
        self.reversal_start_seconds: int = 0
        self.prev_yes_bid: float = 0.50
        self.prev_no_bid: float = 0.50
        self.peak_yes_bid: float = 0.50
        self.peak_no_bid: float = 0.50
        self.chop_flip_count: int = 0
        self.chop_regime: bool = False
        self.late_dump_ticks_up: int = 0
        self.late_dump_ticks_down: int = 0

    def reset(self) -> None:
        """Call at the start of each new window."""
        self.last_sell_seconds = -999
        self.prev_winning_up = None
        self.reversal_count = 0
        self.reversal_selling = False
        self.reversal_start_seconds = 0
        self.prev_yes_bid = 0.50
        self.prev_no_bid = 0.50
        self.peak_yes_bid = 0.50
        self.peak_no_bid = 0.50
        self.chop_flip_count = 0
        self.chop_regime = False
        self.late_dump_ticks_up = 0
        self.late_dump_ticks_down = 0

    # ── Direction ────────────────────────────────────────────────────────────

    def _determine_direction(
        self, market: MarketState
    ) -> tuple[bool, float, str]:
        """Determine winning direction: market-first, model-second.

        Returns (winning_up, confidence, source).
        - winning_up: True if UP side is winning
        - confidence: 0.0–1.0 how sure we are
        - source: which signal drove the decision (for logging)
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
            return market_up, 0.90, "market_strong"
        elif market_edge > p.market_override_edge:
            return market_up, 0.75, "market"
        elif market_edge > p.model_only_edge:
            if market_up == model_up:
                return market_up, 0.70, "combined_agree"
            else:
                return market_up, 0.55, "combined_disagree"
        else:
            if model_edge > 0.10:
                return model_up, 0.60, "model_confident"
            return model_up, 0.50, "model_weak"

    def _allocation_split(
        self, winning_up: bool, confidence: float
    ) -> tuple[float, float]:
        """Budget allocation between UP and DOWN.

        Returns (up_pct, down_pct). More confident = more to the winning side.
        """
        if confidence >= 0.85:
            win_pct = 0.80
        elif confidence >= 0.70:
            win_pct = 0.70
        elif confidence >= 0.55:
            win_pct = 0.60
        else:
            win_pct = 0.50

        if winning_up:
            return win_pct, round(1.0 - win_pct, 2)
        return round(1.0 - win_pct, 2), win_pct

    # ── Budget curve ─────────────────────────────────────────────────────────

    def _budget_curve(self, seconds: int) -> float:
        """How much of the budget can be deployed at this point in time.

        Slow early to avoid over-committing before direction is clear.
        10% → 22% by T+60 → 60% by T+180 → 85% by T+240.
        """
        p = self.profile
        open_pct = p.open_budget_pct

        if seconds <= 5:
            return open_pct
        elif seconds <= 60:
            progress = (seconds - 5) / 55.0
            return open_pct + 0.12 * progress       # → 22%
        elif seconds <= 180:
            progress = (seconds - 60) / 120.0
            return 0.22 + 0.38 * progress           # → 60%
        elif seconds <= p.commit_seconds:
            progress = (seconds - 180) / max(p.commit_seconds - 180, 1)
            return 0.60 + 0.25 * progress           # → 85%
        return 0.85

    def _expensive_side_cap(self, seconds: int) -> float:
        """Maximum price we'll pay on either side, tightening over time.

        Early: 82c (both sides near 50c, hard cap is enough).
        Later: tighten to 65c (if you're buying at 65c+, the window is nearly over).
        """
        p = self.profile
        if seconds >= 180:
            return p.cap_t180
        elif seconds >= 120:
            return p.cap_t120
        elif seconds >= 60:
            return p.cap_t60
        return p.hard_cap

    def _confidence_budget_scale(self, prob_up: float) -> float:
        """Scale total budget by model confidence. Weak signals = smaller position.

        For a both-sides MM strategy, a neutral/no-model 0.50 signal means
        "go symmetric" — not "reduce position". Minimum scale is 0.80 so the
        budget curve isn't choked even when no model is loaded.
        """
        edge = abs(prob_up - 0.50)
        if edge < 0.03:
            return 0.80  # neutral/no-model: deploy at 80%, go symmetric
        if edge < 0.06:
            return 0.85
        if edge < 0.10:
            return 0.95
        if edge >= 0.20:
            return 1.10
        return 1.00

    # ── Reversal detection ───────────────────────────────────────────────────

    def _detect_reversal(
        self, market: MarketState, winning_up: bool
    ) -> bool:
        """Detect reversals early via direction flip or momentum shift.

        Returns True on the tick a reversal is first detected.
        Sets self.reversal_selling = True for the next 30 seconds.
        Also tracks chop regime (>4 flips in first 120s).
        """
        yes_bid = market.yes_bid
        no_bid = market.no_bid

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

        reversed_ = direction_flipped or momentum_shift
        if reversed_:
            self.reversal_count += 1
            self.reversal_selling = True
            self.reversal_start_seconds = market.seconds
            self.peak_yes_bid = yes_bid
            self.peak_no_bid = no_bid
            if market.seconds <= 120:
                self.chop_flip_count += 1
                if self.chop_flip_count > self.profile.chop_flip_threshold:
                    self.chop_regime = True

        if self.reversal_selling:
            elapsed = market.seconds - self.reversal_start_seconds
            if elapsed > self.profile.reversal_sell_window:
                self.reversal_selling = False

        return reversed_

    # ── Main tick ────────────────────────────────────────────────────────────

    def on_tick(
        self,
        market: MarketState,
        position: Position,
        budget_remaining: float,
    ) -> StrategyAction:
        """Main decision function. Called every second.

        Returns what to buy and sell this tick.
        Engine executes the action and updates position + budget.
        """
        p = self.profile
        action = StrategyAction()
        seconds = market.seconds
        yes_bid = market.yes_bid
        no_bid = market.no_bid

        # ── Commit: no trading ───────────────────────────────────────────────
        if seconds >= p.commit_seconds:
            return action

        # ── Direction ────────────────────────────────────────────────────────
        winning_up, confidence, direction_source = self._determine_direction(market)

        # ── Reversal detection ───────────────────────────────────────────────
        reversal_detected = self._detect_reversal(market, winning_up)

        # ── No-trade zone: bids nearly equal → skip to avoid noise churn ────
        if market.spread < p.no_trade_zone:
            return action

        # ── Update late dump tick counters ───────────────────────────────────
        if seconds >= p.late_dump_start:
            self.late_dump_ticks_up = (
                self.late_dump_ticks_up + 1
                if yes_bid < p.late_dump_threshold and yes_bid > 0
                else 0
            )
            self.late_dump_ticks_down = (
                self.late_dump_ticks_down + 1
                if no_bid < p.late_dump_threshold and no_bid > 0
                else 0
            )

        # ── Sells ────────────────────────────────────────────────────────────
        in_reversal_mode = reversal_detected or self.reversal_selling
        if in_reversal_mode and seconds >= p.disable_reversals_seconds:
            in_reversal_mode = False

        if p.sells_enabled and p.sell_start <= seconds <= p.sell_end:
            sell_cooldown_ok = (seconds - self.last_sell_seconds) >= p.sell_cooldown
            if in_reversal_mode and seconds >= 30:
                sell_cooldown_ok = True  # reversal bypasses cooldown

            if sell_cooldown_ok:
                action = self._decide_sell(
                    market, position, winning_up, confidence,
                    action, in_reversal_mode, reversal_detected,
                )

        # ── Budget calculation ────────────────────────────────────────────────
        max_deploy = p.budget * self._budget_curve(seconds)
        max_deploy *= self._confidence_budget_scale(market.prob_up)

        # Soft stop loss: freeze ramp if unrealized loss > 15% after T+60
        if seconds >= p.soft_stop_start and position.net_cost > 0:
            best_pnl = position.best_pnl()
            if best_pnl < 0:
                loss_pct = abs(best_pnl) / position.net_cost
                if loss_pct > p.soft_stop_loss_pct:
                    max_deploy = min(max_deploy, position.net_cost)

        # Window loss limit: stop increasing exposure if down > $25
        if position.best_pnl() < -p.window_loss_limit:
            max_deploy = min(max_deploy, position.net_cost)

        # No-new-risk zone: freeze net exposure after T+230
        if seconds >= p.no_new_risk_seconds:
            max_deploy = min(max_deploy, position.net_cost)

        curve_remaining = max(max_deploy - position.net_cost, 0)
        usable = min(budget_remaining, curve_remaining)

        # Chop regime: scale down buy size
        if self.chop_regime:
            usable *= p.chop_size_multiplier

        # ── Buys ─────────────────────────────────────────────────────────────
        if usable >= 0.50:
            action = self._decide_buy(
                market, position, winning_up, confidence, usable, action
            )

        return action

    # ── Sell decisions ───────────────────────────────────────────────────────

    def _decide_sell(
        self,
        market: MarketState,
        position: Position,
        winning_up: bool,
        confidence: float,
        action: StrategyAction,
        in_reversal_mode: bool,
        reversal_detected: bool,
    ) -> StrategyAction:
        """Decide what to sell this tick.

        Priority order:
        1. DEAD_SIDE — other bid > 80c, this side is going to zero
        2. REVERSAL — direction flipped, exit losing side fast (capped at 25%)
        3. PAYOUT_FLOOR — excess shares above floor where bid > hold_value
        4. UNFAVORED_RICH — losing side avg > 50c and market edge > 10c
        5. LATE_DUMP — T+180+, bid < 25c for 5 consecutive ticks

        NEVER sell the winning side.
        """
        p = self.profile
        seconds = market.seconds
        yes_bid = market.yes_bid
        no_bid = market.no_bid
        market_edge = abs(yes_bid - no_bid)
        prob_up = market.prob_up

        losing_up = not winning_up

        # Which side is losing?
        if losing_up:
            losing_shares = position.up_shares
            losing_bid = yes_bid
            losing_avg = position.up_avg
            other_bid = no_bid
            late_dump_ticks = self.late_dump_ticks_up
        else:
            losing_shares = position.down_shares
            losing_bid = no_bid
            losing_avg = position.down_avg
            other_bid = yes_bid
            late_dump_ticks = self.late_dump_ticks_down

        shares_to_sell = 0
        sell_price = losing_bid
        reason = ""

        # 1. DEAD_SIDE: other bid > 80c → dump the losing side
        if other_bid > p.dead_side_threshold and losing_shares >= p.shares_per_order:
            shares_to_sell = min(losing_shares, p.shares_per_order * 3)
            reason = "DEAD_SIDE"

        # 2. REVERSAL: flip detected → exit losing side fast
        elif in_reversal_mode and losing_shares >= p.shares_per_order and seconds >= 20:
            max_shares = max(
                int(losing_shares * p.max_reversal_sell_pct),
                p.shares_per_order,
            )
            shares_to_sell = min(
                losing_shares,
                max_shares if reversal_detected else p.shares_per_order * 2,
            )
            reason = "REVERSAL"

        # 3. PAYOUT_FLOOR: sell excess shares when bid beats hold value
        elif (
            p.payout_floor_sell_enabled
            and position.excess_shares(losing_up) >= p.payout_floor_min_excess
            and losing_bid > position.hold_value(prob_up, losing_up)
            and losing_shares - p.shares_per_order >= p.min_hedge_shares
        ):
            excess = position.excess_shares(losing_up)
            shares_to_sell = min(excess, p.shares_per_order * 2)
            reason = "PAYOUT_FLOOR"

        # 4. UNFAVORED_RICH: expensive losing side and market is clear
        elif (
            losing_avg > p.unfavored_rich_threshold
            and market_edge > 0.10
            and losing_shares - p.shares_per_order >= p.min_hedge_shares
        ):
            shares_to_sell = min(losing_shares, p.shares_per_order * 2)
            reason = "UNFAVORED_RICH"

        # 5. LATE_DUMP: near-worthless shares held long enough
        elif (
            seconds >= p.late_dump_start
            and late_dump_ticks >= p.late_dump_min_ticks
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

    # ── Buy decisions ────────────────────────────────────────────────────────

    def _decide_buy(
        self,
        market: MarketState,
        position: Position,
        winning_up: bool,
        confidence: float,
        usable: float,
        action: StrategyAction,
    ) -> StrategyAction:
        """Decide what to buy this tick.

        Both sides get orders, weighted by direction confidence.
        Checks (in order):
        - Time-varying price cap
        - Dying side block (other bid > 70c after T+60)
        - Balance cap (65% early, 70% late)
        - Budget check
        - Anti-churn: unfavored side can't rebuy above last sell price
        - Min hedge: always keep >= 5 shares on unfavored side

        After selling, also try to rebuy the winning side (sell-and-rebuy).
        """
        p = self.profile
        seconds = market.seconds
        yes_bid = market.yes_bid
        no_bid = market.no_bid
        yes_ask = market.yes_ask
        no_ask = market.no_ask
        price_cap = self._expensive_side_cap(seconds)
        prob_up = market.prob_up

        up_pct, down_pct = self._allocation_split(winning_up, confidence)

        # Budget boost toward favored side (if model is confident and favored isn't ahead)
        up_pct, down_pct = self._boost_favored_side(
            prob_up, usable, up_pct, down_pct,
            yes_bid, no_bid,
            position.up_shares, position.down_shares,
        )

        up_budget = usable * up_pct
        down_budget = usable * down_pct

        # Rebalance override: if one side is completely empty and the other has shares,
        # redirect enough budget to buy at least one order on the empty side.
        # Sets budget directly (not via percentage) to avoid floating-point rounding.
        # Dying-side gate still applies — this only fixes the budget starvation.
        if position.up_shares == 0 and position.down_shares > 0 and yes_ask > 0:
            needed = p.shares_per_order * yes_ask
            if needed <= usable and needed > up_budget:
                up_budget = needed
                down_budget = max(0.0, usable - up_budget)
        elif position.down_shares == 0 and position.up_shares > 0 and no_ask > 0:
            needed = p.shares_per_order * no_ask
            if needed <= usable and needed > down_budget:
                down_budget = needed
                up_budget = max(0.0, usable - down_budget)

        balance_cap = p.late_balance_cap if seconds >= 120 else p.early_balance_cap
        total = position.total_shares

        # ── BUY UP ──────────────────────────────────────────────────────────
        can_buy_up = True
        favored_up = prob_up >= 0.50

        # Use ask for price cap + budget — that's what we actually pay
        if yes_ask <= 0 or round(yes_ask, 4) > price_cap:
            can_buy_up = False
        # Dying side: UP dying if DOWN bid > threshold
        if seconds >= p.dying_side_start and no_bid > p.dying_side_threshold:
            can_buy_up = False
        # Balance cap
        if total >= 10:
            up_after = position.up_shares + p.shares_per_order
            if up_after / (total + p.shares_per_order) > balance_cap:
                can_buy_up = False
        # Budget check (at ask — the price we pay)
        if p.shares_per_order * yes_ask > up_budget:
            can_buy_up = False
        # Anti-churn: unfavored side can't rebuy above last sell price
        if not favored_up:
            last_sell = position.last_sell_price(True)
            if last_sell > 0 and yes_ask >= last_sell:
                can_buy_up = False

        if can_buy_up:
            action.buy_up_shares = p.shares_per_order
            action.buy_up_price = yes_ask  # post at ask → immediate taker fill

        # ── BUY DOWN ────────────────────────────────────────────────────────
        can_buy_down = True

        if no_ask <= 0 or round(no_ask, 4) > price_cap:
            can_buy_down = False
        # Dying side: DOWN dying if UP bid > threshold
        if seconds >= p.dying_side_start and yes_bid > p.dying_side_threshold:
            can_buy_down = False
        # Balance cap
        if total >= 10:
            dn_after = position.down_shares + p.shares_per_order
            if dn_after / (total + p.shares_per_order) > balance_cap:
                can_buy_down = False
        # Budget check (at ask — the price we pay)
        if p.shares_per_order * no_ask > down_budget:
            can_buy_down = False
        # Anti-churn: unfavored side can't rebuy above last sell price
        if favored_up:
            last_sell = position.last_sell_price(False)
            if last_sell > 0 and no_ask >= last_sell:
                can_buy_down = False

        if can_buy_down:
            action.buy_down_shares = p.shares_per_order
            action.buy_down_price = no_ask  # post at ask → immediate taker fill

        # ── SELL-AND-REBUY ───────────────────────────────────────────────────
        # After selling the losing side, immediately try to buy the winning side.
        if action.sell_up_shares > 0 and action.buy_down_shares == 0:
            # Sold UP (losing), try to buy DOWN (winning)
            if (
                no_ask > 0
                and round(no_ask, 4) <= price_cap
                and yes_bid <= p.dying_side_threshold
            ):
                action.buy_down_shares = p.shares_per_order
                action.buy_down_price = no_ask
        elif action.sell_down_shares > 0 and action.buy_up_shares == 0:
            # Sold DOWN (losing), try to buy UP (winning)
            if (
                yes_ask > 0
                and round(yes_ask, 4) <= price_cap
                and no_bid <= p.dying_side_threshold
            ):
                action.buy_up_shares = p.shares_per_order
                action.buy_up_price = yes_ask

        return action

    def _boost_favored_side(
        self,
        prob_up: float,
        usable: float,
        up_pct: float,
        down_pct: float,
        up_bid: float,
        down_bid: float,
        up_shares: int,
        down_shares: int,
    ) -> tuple[float, float]:
        """Transfer budget toward favored side when model is confident.

        Only acts when:
        - Model edge > 10% (at least 60/40 confidence)
        - Favored side isn't already clearly ahead in shares
        This prevents over-concentrating when favored side is already large.
        """
        p = self.profile
        edge = abs(prob_up - 0.50)
        if edge < 0.10 or usable <= 0:
            return up_pct, down_pct

        favored_up = prob_up >= 0.50
        favored_shares = up_shares if favored_up else down_shares
        unfavored_shares = down_shares if favored_up else up_shares

        # Don't boost if favored already clearly ahead
        if favored_shares > unfavored_shares + 5:
            return up_pct, down_pct

        favored_budget = up_pct * usable if favored_up else down_pct * usable
        favored_bid = up_bid if favored_up else down_bid
        if favored_bid <= 0:
            return up_pct, down_pct

        min_orders = 2 if edge >= 0.20 else 1
        target = min(p.shares_per_order * favored_bid * min_orders, usable)

        if favored_budget >= target - 1e-9:
            return up_pct, down_pct

        transfer_usd = min(
            (1.0 - (up_pct if favored_up else down_pct)) * usable,
            target - favored_budget,
        )
        if transfer_usd <= 0 or usable <= 0:
            return up_pct, down_pct

        transfer_pct = transfer_usd / usable
        if favored_up:
            return min(up_pct + transfer_pct, 0.90), max(down_pct - transfer_pct, 0.10)
        return max(up_pct - transfer_pct, 0.10), min(down_pct + transfer_pct, 0.90)

    def _ladder_for_bid(self, bid: float) -> tuple:
        """Return ladder offsets based on current bid price.

        Lower price = more levels (more resting orders = more fills on cheap side).
        Higher price = fewer levels (winning side moves fast, don't over-ladder).
        """
        p = self.profile
        if bid <= 0.15:
            return p.offsets_lottery
        elif bid <= 0.35:
            return p.offsets_cheap
        elif bid <= 0.60:
            return p.offsets_mid
        return p.offsets_winning
