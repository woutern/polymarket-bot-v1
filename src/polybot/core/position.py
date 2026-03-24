"""Position — tracks all shares and costs within a single window.

Pure data class. No I/O, no logging. Easy to test.

Key concepts:
- payout_floor: min(up_shares, down_shares) — the guaranteed-profit count.
  If combined_avg < 1.00 per share pair, every floor share pair is profitable.
- excess_shares: shares above the floor on one side — single-sided risk.
- hold_value: expected value of holding one share to resolution.
  UP share: prob_up × $1.00. DOWN share: (1 - prob_up) × $1.00.
  Used to decide: is selling now worth more than holding?
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Position:
    """Per-window position state for one pair."""

    up_shares: int = 0
    up_cost: float = 0.0
    down_shares: int = 0
    down_cost: float = 0.0

    # Counters for logging / model training
    sells_count: int = 0
    buys_count: int = 0
    total_sold_proceeds: float = 0.0
    total_bought_cost: float = 0.0

    # Track last sell price per side (used for anti-churn)
    last_sell_price_up: float = 0.0
    last_sell_price_down: float = 0.0

    # ── Derived properties ──────────────────────────────────────────────────

    @property
    def up_avg(self) -> float:
        """Average cost per UP share."""
        return round(self.up_cost / self.up_shares, 4) if self.up_shares > 0 else 0.0

    @property
    def down_avg(self) -> float:
        """Average cost per DOWN share."""
        return round(self.down_cost / self.down_shares, 4) if self.down_shares > 0 else 0.0

    @property
    def combined_avg(self) -> float:
        """Sum of both side averages. < 1.00 = guaranteed profitable."""
        if self.up_shares > 0 and self.down_shares > 0:
            return round(self.up_avg + self.down_avg, 4)
        return 0.0

    @property
    def net_cost(self) -> float:
        """Total USD spent on both sides (minus sells already booked)."""
        return round(self.up_cost + self.down_cost, 2)

    @property
    def payout_floor(self) -> int:
        """Number of share pairs that guarantee a payout regardless of outcome.

        Each floor unit pays $1.00 at resolution (either UP or DOWN wins).
        Cost of one floor unit = up_avg + down_avg = combined_avg.
        If combined_avg < 1.00 → each floor unit is profitable.
        """
        return min(self.up_shares, self.down_shares)

    @property
    def total_shares(self) -> int:
        return self.up_shares + self.down_shares

    # ── Methods ─────────────────────────────────────────────────────────────

    def excess_shares(self, side_up: bool) -> int:
        """Shares above the payout floor on the given side.

        These are single-sided risk: they only pay out if that side wins.
        Selling excess when bid > hold_value is the payout-floor mechanic.
        """
        shares = self.up_shares if side_up else self.down_shares
        return max(shares - self.payout_floor, 0)

    def hold_value(self, prob_up: float, side_up: bool) -> float:
        """Expected value of holding one share to resolution.

        UP share:   prob_up × $1.00
        DOWN share: (1 - prob_up) × $1.00

        If current bid > hold_value → selling now beats holding.
        """
        if side_up:
            return round(prob_up, 4)
        return round(1.0 - prob_up, 4)

    def last_sell_price(self, side_up: bool) -> float:
        """Last price we sold shares on this side (for anti-churn)."""
        return self.last_sell_price_up if side_up else self.last_sell_price_down

    def buy(self, side_up: bool, shares: int, price: float) -> float:
        """Record a buy. Returns USD spent."""
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
        """Record a sell. Returns USD received. Clamps to available shares."""
        if side_up:
            shares = min(shares, self.up_shares)
            avg = self.up_avg
            self.up_shares -= shares
            self.up_cost = max(round(self.up_cost - shares * avg, 2), 0.0)
            if shares > 0:
                self.last_sell_price_up = price
        else:
            shares = min(shares, self.down_shares)
            avg = self.down_avg
            self.down_shares -= shares
            self.down_cost = max(round(self.down_cost - shares * avg, 2), 0.0)
            if shares > 0:
                self.last_sell_price_down = price
        proceeds = round(shares * price, 2)
        self.sells_count += 1
        self.total_sold_proceeds += proceeds
        return proceeds

    def pnl_if_up(self) -> float:
        """PnL if UP side wins (pays $1/share)."""
        return round(self.up_shares - self.net_cost, 2)

    def pnl_if_down(self) -> float:
        """PnL if DOWN side wins (pays $1/share)."""
        return round(self.down_shares - self.net_cost, 2)

    def is_gp(self) -> bool:
        """True if profitable regardless of outcome (guaranteed profit)."""
        return self.pnl_if_up() > 0 and self.pnl_if_down() > 0

    def best_pnl(self) -> float:
        """Best possible PnL across both outcomes."""
        return max(self.pnl_if_up(), self.pnl_if_down())

    def worst_pnl(self) -> float:
        """Worst possible PnL across both outcomes."""
        return min(self.pnl_if_up(), self.pnl_if_down())
