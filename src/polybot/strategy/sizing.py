"""Position sizing: Quarter-Kelly criterion with caps."""

from __future__ import annotations


def kelly_fraction(p: float, b: float, kelly_mult: float = 0.25) -> float:
    """Compute Kelly fraction for a binary bet.

    Args:
        p: Probability of winning.
        b: Net odds (payout / stake). For a market price of `ask`,
           b = (1 - ask) / ask  (you pay `ask`, win `1 - ask` profit).
        kelly_mult: Fraction of full Kelly to use (default quarter-Kelly).

    Returns:
        Optimal fraction of bankroll to bet (0 if negative edge).
    """
    if b <= 0 or p <= 0 or p >= 1:
        return 0.0
    q = 1 - p
    f = (p * b - q) / b
    return max(f * kelly_mult, 0.0)


def compute_size(
    model_prob: float,
    market_price: float,
    bankroll: float,
    kelly_mult: float = 0.25,
    max_position_pct: float = 0.01,
    min_trade_usd: float = 1.0,
    max_trade_usd: float = 10.0,
) -> float:
    """Compute position size in USD.

    Args:
        model_prob: Our estimated probability of winning.
        market_price: Current ask price (what we pay per share).
        bankroll: Current bankroll in USD.
        kelly_mult: Kelly multiplier (0.25 = quarter Kelly).
        max_position_pct: Maximum fraction of bankroll per trade.
        min_trade_usd: Minimum trade size in USD (Polymarket minimum is $1).
        max_trade_usd: Hard cap per trade in USD.

    Returns:
        Position size in USD (0 if no bet).
    """
    if market_price <= 0 or market_price >= 1:
        return 0.0
    if bankroll <= 0:
        return 0.0

    # Net odds: if we pay 0.60, we win 0.40 profit → b = 0.40/0.60
    b = (1 - market_price) / market_price
    f = kelly_fraction(model_prob, b, kelly_mult)

    if f <= 0:
        return 0.0

    # Apply bankroll % cap
    f = min(f, max_position_pct)
    size = round(f * bankroll, 2)

    # Apply hard caps — min_trade_usd acts as floor (always bet at least this
    # much when there's positive edge), max_trade_usd prevents outsized bets.
    size = max(size, min_trade_usd)
    size = min(size, max_trade_usd)
    return size
