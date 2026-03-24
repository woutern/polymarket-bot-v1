"""AccumulateOnlyStrategy — buy both sides, never sell.

Used for SOL_5m, XRP_5m, and all 1-hour markets.
K9 data shows zero sells on these pairs — pure accumulation + hold to resolution.

Simpler than MarketMakerStrategy: no sells, no reversal detection, no payout floor.
Model drives allocation split. Budget curve controls deployment pace.
"""

from __future__ import annotations

from polybot.core.position import Position
from polybot.strategy.base import MarketState, StrategyAction
from polybot.strategy.profile import StrategyProfile
from polybot.strategy.profiles import SOL_5M_PROFILE


class AccumulateOnlyStrategy:
    """Accumulate both sides continuously. Hold to resolution. No sells.

    Call reset() at the start of each window.
    Call on_tick() every second.
    """

    def __init__(self, profile: StrategyProfile | None = None):
        self.profile = profile or SOL_5M_PROFILE
        self.name = f"accumulate_{self.profile.name}"

    def reset(self) -> None:
        """Call at the start of each new window. No internal state to reset."""
        pass

    def _budget_curve(self, seconds: int) -> float:
        """Same ramp as MarketMakerStrategy — slow deployment."""
        p = self.profile
        open_pct = p.open_budget_pct

        if seconds <= 5:
            return open_pct
        elif seconds <= 60:
            progress = (seconds - 5) / 55.0
            return open_pct + 0.12 * progress
        elif seconds <= 180:
            progress = (seconds - 60) / 120.0
            return 0.22 + 0.38 * progress
        elif seconds <= p.commit_seconds:
            progress = (seconds - 180) / max(p.commit_seconds - 180, 1)
            return 0.60 + 0.25 * progress
        return 0.85

    def on_tick(
        self,
        market: MarketState,
        position: Position,
        budget_remaining: float,
    ) -> StrategyAction:
        """Buy both sides. Stop at commit. No sells."""
        p = self.profile
        action = StrategyAction()
        seconds = market.seconds
        yes_bid = market.yes_bid
        no_bid = market.no_bid
        prob_up = market.prob_up

        if seconds >= p.commit_seconds:
            return action

        # Model-driven allocation split (no market override — accumulate-only)
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
        curve_remaining = max(max_deploy - position.net_cost, 0)
        usable = min(budget_remaining, curve_remaining)

        if usable < 0.50:
            return action

        up_budget = usable * up_pct
        down_budget = usable * down_pct
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
