"""Pre-built strategy profiles for each trading pair.

Each profile is a StrategyProfile instance with pair-specific tuning.
The strategy logic (MarketMakerStrategy, AccumulateOnlyStrategy) is shared.

Sources:
- BTC_5m: K9 data — sells actively, high budget
- SOL_5m: K9 data — zero sells, pure accumulation
- XRP_5m: K9 data — zero sells, pure accumulation
- ETH_5m: no K9 data — assume BTC-style
- *_1h:   K9 data — zero sells, very small open, long commit time
"""

from __future__ import annotations

from polybot.strategy.profile import StrategyProfile

# ── 5-Minute Profiles ───────────────────────────────────────────────────────

BTC_5M_PROFILE = StrategyProfile(
    name="btc_5m",
    budget=80.0,
    sells_enabled=True,
    sell_cooldown=10,
    hard_cap=0.82,
    dying_side_threshold=0.70,
    dead_side_threshold=0.80,
    payout_floor_sell_enabled=True,
    min_hedge_shares=5,
)

ETH_5M_PROFILE = StrategyProfile(
    name="eth_5m",
    budget=50.0,
    sells_enabled=True,       # no K9 ETH data — assume BTC-style
    sell_cooldown=10,
    hard_cap=0.82,
    dying_side_threshold=0.70,
    dead_side_threshold=0.80,
    payout_floor_sell_enabled=True,
    min_hedge_shares=5,
)

SOL_5M_PROFILE = StrategyProfile(
    name="sol_5m",
    budget=50.0,
    sells_enabled=False,      # K9: zero sells on SOL 5m
    open_budget_pct=0.08,     # SOL moves faster — smaller open
    hard_cap=0.82,
    dying_side_threshold=0.70,
    payout_floor_sell_enabled=False,
    min_hedge_shares=0,
)

XRP_5M_PROFILE = StrategyProfile(
    name="xrp_5m",
    budget=50.0,
    sells_enabled=False,      # K9: zero sells on XRP 5m
    hard_cap=0.82,
    dying_side_threshold=0.70,
    payout_floor_sell_enabled=False,
    min_hedge_shares=0,
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
