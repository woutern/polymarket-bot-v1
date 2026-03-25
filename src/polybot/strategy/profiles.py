"""Pre-built strategy profiles for each trading pair.

Each profile is a StrategyProfile instance with pair-specific tuning.
The strategy logic (MarketMakerStrategy, AccumulateOnlyStrategy) is shared.

Sell philosophy (all 5m pairs):
- Only sell to REDEPLOY capital when direction is near-certain (dead side ≥ 88c)
- Reversals disabled — too noisy in 5m windows, creates churn losses
- Unfavored-rich disabled — 55/45 splits are not clear enough to justify friction
- Late dump only if bid collapses to ≤ 10c very late in window
"""

from __future__ import annotations

from polybot.strategy.profile import StrategyProfile

# ── 5-Minute Profiles ───────────────────────────────────────────────────────

BTC_5M_PROFILE = StrategyProfile(
    name="btc_5m",
    budget=50.0,
    sells_enabled=True,
    dead_side_threshold=0.75,         # dump losing side when winning side > 75c
    disable_reversals_seconds=0,      # reversals disabled — too noisy in 5m
    unfavored_rich_threshold=0.99,    # effectively disabled
    late_dump_threshold=0.10,         # only late-dump if bid collapses to ≤ 10c
    hard_cap=0.82,
    dying_side_threshold=0.70,
    payout_floor_sell_enabled=False,
    min_hedge_shares=0,
)

ETH_5M_PROFILE = StrategyProfile(
    name="eth_5m",
    budget=50.0,
    sells_enabled=True,
    dead_side_threshold=0.75,
    disable_reversals_seconds=0,
    unfavored_rich_threshold=0.99,
    late_dump_threshold=0.10,
    hard_cap=0.82,
    dying_side_threshold=0.70,
    payout_floor_sell_enabled=False,
    min_hedge_shares=0,
)

SOL_5M_PROFILE = StrategyProfile(
    name="sol_5m",
    budget=50.0,
    sells_enabled=True,
    open_budget_pct=0.08,             # SOL moves faster — smaller open
    dead_side_threshold=0.75,
    disable_reversals_seconds=0,
    unfavored_rich_threshold=0.99,
    late_dump_threshold=0.10,
    hard_cap=0.82,
    dying_side_threshold=0.70,
    payout_floor_sell_enabled=False,
    min_hedge_shares=0,
)

XRP_5M_PROFILE = StrategyProfile(
    name="xrp_5m",
    budget=50.0,
    sells_enabled=True,
    dead_side_threshold=0.75,
    disable_reversals_seconds=0,
    unfavored_rich_threshold=0.99,
    late_dump_threshold=0.10,
    hard_cap=0.82,
    dying_side_threshold=0.70,
    payout_floor_sell_enabled=False,
    min_hedge_shares=0,
)

# ── 15-Minute Profiles ──────────────────────────────────────────────────────

BTC_15M_PROFILE = StrategyProfile(
    name="btc_15m",
    budget=150.0,
    open_budget_pct=0.08,         # smaller probe — wait for direction
    budget_curve_mid1=180,        # T+180 = first ramp milestone (3x of 5m's T+60)
    # Price caps at T+180, T+360, T+540 (scaled from 5m's T+60, T+120, T+180)
    cap_t60=0.78,
    cap_t120=0.73,
    cap_t180=0.68,
    hard_cap=0.82,
    # Sells — active throughout, K9-style recycling
    sells_enabled=True,
    sell_cooldown=10,             # K9 rebuys within 10s
    sell_start=60,                # give the open 1 min before selling
    sell_end=720,
    dead_side_threshold=0.78,
    early_rebalance_threshold=0.65,
    early_rebalance_min_bid=0.20,
    conviction_dump_start=780,    # last 2 min
    conviction_dump_threshold=0.72,
    # Dying side: wait 3 min before applying
    dying_side_threshold=0.72,
    dying_side_start=180,
    # Payout floor recycling — key for cheap late-window fills (K9 does 49% cheap)
    payout_floor_sell_enabled=True,
    payout_floor_min_excess=5,
    min_hedge_shares=3,
    # Budget / risk limits scaled for $150
    window_loss_limit=40.0,
    max_intrawindow_sell_loss=-8.0,
    no_new_risk_seconds=690,
    disable_reversals_seconds=750,
    soft_stop_start=180,
    # Late-window: don't dump cheap shares K9 would be buying
    late_dump_start=540,
    late_dump_threshold=0.08,
    late_dump_min_ticks=8,
    # Chop: more flips tolerated in longer window
    chop_flip_threshold=6,
    # Timing
    commit_seconds=780,
)

# ── 1-Hour Profiles ─────────────────────────────────────────────────────────

BTC_1H_PROFILE = StrategyProfile(
    name="btc_1h",
    budget=50.0,
    sells_enabled=False,      # K9: zero sells on hourly
    open_budget_pct=0.05,     # very small open — lots of time
    hard_cap=0.82,
    dying_side_threshold=0.70,
    dying_side_start=300,     # 5 minutes before applying dying side block
    commit_seconds=3540,      # 59 minutes
    payout_floor_sell_enabled=False,
    min_hedge_shares=0,
)

ETH_1H_PROFILE = StrategyProfile(
    name="eth_1h",
    budget=50.0,
    sells_enabled=False,
    open_budget_pct=0.05,
    hard_cap=0.82,
    dying_side_threshold=0.70,
    dying_side_start=300,
    commit_seconds=3540,
    payout_floor_sell_enabled=False,
    min_hedge_shares=0,
)

SOL_1H_PROFILE = StrategyProfile(
    name="sol_1h",
    budget=50.0,
    sells_enabled=False,
    open_budget_pct=0.05,
    hard_cap=0.82,
    dying_side_threshold=0.70,
    dying_side_start=300,
    commit_seconds=3540,
    payout_floor_sell_enabled=False,
    min_hedge_shares=0,
)

XRP_1H_PROFILE = StrategyProfile(
    name="xrp_1h",
    budget=50.0,
    sells_enabled=False,
    open_budget_pct=0.05,
    hard_cap=0.82,
    dying_side_threshold=0.70,
    dying_side_start=300,
    commit_seconds=3540,
    payout_floor_sell_enabled=False,
    min_hedge_shares=0,
)

# ── Registry ────────────────────────────────────────────────────────────────

ALL_PROFILES: dict[str, StrategyProfile] = {
    "BTC_5M": BTC_5M_PROFILE,
    "ETH_5M": ETH_5M_PROFILE,
    "SOL_5M": SOL_5M_PROFILE,
    "XRP_5M": XRP_5M_PROFILE,
    "BTC_15M": BTC_15M_PROFILE,
    "BTC_1H": BTC_1H_PROFILE,
    "ETH_1H": ETH_1H_PROFILE,
    "SOL_1H": SOL_1H_PROFILE,
    "XRP_1H": XRP_1H_PROFILE,
}


def get_profile(pair: str) -> StrategyProfile:
    """Return the profile for a given pair string (e.g. 'BTC_5m' or 'BTC_5M')."""
    key = pair.upper().replace("-", "_")
    if key not in ALL_PROFILES:
        raise ValueError(f"Unknown pair: {pair!r}. Known: {list(ALL_PROFILES)}")
    return ALL_PROFILES[key]
