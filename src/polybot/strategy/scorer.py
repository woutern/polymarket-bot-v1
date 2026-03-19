"""Confidence score engine — evaluates 5 signals per window.

Each signal contributes +1 to the score (0-5 range).
Score 4-5 → taker FOK, Score 2-3 → maker GTC at $0.48, Score 0-1 → skip.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScoreResult:
    """Result of scoring a single window evaluation."""

    total: int  # 0-5
    ofi: bool  # OFI positive and increasing T+2s → T+8s
    no_reversal: bool  # price still same direction at T+8s as T+2s
    cross_asset: bool  # BTC same direction (ETH/SOL only)
    pm_pressure: bool  # Polymarket ask stable or improving
    volume: bool  # volume > 1.5x avg of prior 5 windows
    details: dict = field(default_factory=dict)


def compute_score(
    ofi_at_2s: float,
    ofi_at_8s: float,
    price_at_2s: float,
    price_at_8s: float,
    open_price: float,
    btc_move_pct: float,
    asset: str,
    ask_at_open: float,
    ask_now: float,
    window_volume: float,
    avg_prior_volume: float,
) -> ScoreResult:
    """Compute confidence score 0-5 for a window.

    Args:
        ofi_at_2s: Order flow imbalance at T+2s
        ofi_at_8s: Order flow imbalance at T+8s
        price_at_2s: Asset price at T+2s
        price_at_8s: Asset price at T+8s
        open_price: Window open price
        btc_move_pct: BTC % move this window (0 for BTC itself)
        asset: "BTC", "ETH", or "SOL"
        ask_at_open: YES/NO ask price at window open
        ask_now: Current ask price
        window_volume: Tick count in current window
        avg_prior_volume: Average tick count of prior 5 windows
    """
    # 1. OFI: positive and increasing from T+2s to T+8s
    move_dir = 1 if price_at_8s >= open_price else -1
    # For DOWN direction, negative OFI (sell pressure) is what we want
    if move_dir > 0:
        s_ofi = ofi_at_8s > 0 and ofi_at_8s > ofi_at_2s
    else:
        s_ofi = ofi_at_8s < 0 and ofi_at_8s < ofi_at_2s

    # 2. No reversal: price at T+8s still moving same direction as T+2s
    move_2s = price_at_2s - open_price
    move_8s = price_at_8s - open_price
    s_no_reversal = (move_2s > 0 and move_8s > 0) or (move_2s < 0 and move_8s < 0)

    # 3. Cross-asset: BTC confirms direction (ETH/SOL only)
    if asset == "BTC":
        s_cross = False
    else:
        s_cross = abs(btc_move_pct) > 0.02 and (
            (btc_move_pct > 0 and move_8s > 0) or (btc_move_pct < 0 and move_8s < 0)
        )

    # 4. PM pressure: ask stable or improving (2c tolerance)
    s_pm = ask_now <= ask_at_open + 0.02

    # 5. Volume: > 1.5x average of prior 5 windows
    s_volume = window_volume > 1.5 * avg_prior_volume if avg_prior_volume > 0 else False

    total = sum([s_ofi, s_no_reversal, s_cross, s_pm, s_volume])

    return ScoreResult(
        total=total,
        ofi=s_ofi,
        no_reversal=s_no_reversal,
        cross_asset=s_cross,
        pm_pressure=s_pm,
        volume=s_volume,
        details={
            "ofi_2s": round(ofi_at_2s, 4),
            "ofi_8s": round(ofi_at_8s, 4),
            "price_2s": round(price_at_2s, 2),
            "price_8s": round(price_at_8s, 2),
            "btc_move": round(btc_move_pct, 4),
            "ask_open": round(ask_at_open, 3),
            "ask_now": round(ask_now, 3),
            "vol_ratio": round(window_volume / avg_prior_volume, 2) if avg_prior_volume > 0 else 0,
        },
    )
