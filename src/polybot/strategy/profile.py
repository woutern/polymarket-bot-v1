"""StrategyProfile — per-pair configuration.

Each pair (BTC_5m, SOL_1h, etc.) gets its own profile.
The strategy logic is shared; only these parameters change.

All fields have sensible defaults for a 5-minute BTC window.
Override only what differs per pair.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StrategyProfile:
    """Configuration that differs between pairs and timeframes.

    Sections:
    - Identity
    - Budget
    - Price caps (time-varying)
    - Balance caps
    - Selling rules
    - Reversal controls
    - Dying side detection
    - No-trade zone
    - Direction confidence
    - Dynamic flip threshold
    - Chop detection
    - Soft stop loss
    - Window loss limit
    - Ladder configuration
    - Market vs model arbitration
    - Timing
    """

    # ── Identity ────────────────────────────────────────────────────────────
    name: str = "btc_5m"

    # ── Budget ──────────────────────────────────────────────────────────────
    budget: float = 80.0           # max USD to deploy per window
    open_budget_pct: float = 0.10  # % of budget at open (T+5–15)

    # ── Price caps (time-varying via _expensive_side_cap()) ─────────────────
    hard_cap: float = 0.82         # never buy above this, any time
    # Caps tighten as the window progresses:
    # T+0:   hard_cap (0.82)
    # T+60:  cap_t60  (0.75)
    # T+120: cap_t120 (0.70)
    # T+180: cap_t180 (0.65)
    cap_t60: float = 0.75
    cap_t120: float = 0.70
    cap_t180: float = 0.65

    # ── Balance caps ────────────────────────────────────────────────────────
    early_balance_cap: float = 0.65   # max % one side before T+120
    late_balance_cap: float = 0.70    # max % one side after T+120

    # ── Selling ─────────────────────────────────────────────────────────────
    sells_enabled: bool = True        # False for SOL/XRP/hourly
    sell_cooldown: int = 10           # seconds between normal sells
    sell_start: int = 20              # no sells before this second
    sell_end: int = 240               # no sells after this second

    # Dead side: sell ALL if other side bid is above this
    dead_side_threshold: float = 0.80

    # Unfavored rich: sell if losing side avg > this AND market edge > 10c
    unfavored_rich_threshold: float = 0.50

    # Late dump: sell losing side shares with bid below this
    late_dump_start: int = 180
    late_dump_threshold: float = 0.25
    late_dump_min_ticks: int = 5      # require N consecutive ticks below threshold

    # Payout floor excess sell: sell shares above min(up,down) when bid > hold_value
    payout_floor_sell_enabled: bool = True
    payout_floor_min_excess: int = 5  # minimum excess shares to trigger

    # Min hedge: always keep at least this many shares on unfavored side
    min_hedge_shares: int = 5

    # ── Reversal controls ───────────────────────────────────────────────────
    max_reversal_sell_pct: float = 0.25   # max % of position sold per tick on reversal
    reversal_sell_window: int = 30        # seconds to stay in reversal sell mode
    no_new_risk_seconds: int = 230        # freeze net exposure after this
    disable_reversals_seconds: int = 260  # no reversals after this

    # ── Dying side detection ─────────────────────────────────────────────────
    dying_side_threshold: float = 0.70    # don't buy if other side bid > this
    dying_side_start: int = 60            # only apply after this second

    # ── No-trade zone ───────────────────────────────────────────────────────
    no_trade_zone: float = 0.02           # skip all buys if spread < this

    # ── Direction confidence filter ─────────────────────────────────────────
    min_spread_early: float = 0.04        # minimum spread to act, early window
    min_spread_late: float = 0.07         # minimum spread to act, late window
    spread_threshold_late_start: int = 120

    # ── Dynamic flip threshold ──────────────────────────────────────────────
    flip_threshold_early: float = 0.10    # edge needed to flip direction, early
    flip_threshold_late: float = 0.06     # edge needed to flip direction, late
    flip_threshold_late_start: int = 200

    # ── Chop detection ──────────────────────────────────────────────────────
    chop_flip_threshold: int = 4          # >N flips in first 120s = chop regime
    chop_size_multiplier: float = 0.60    # scale buys to 60% in chop regime

    # ── Soft stop loss ──────────────────────────────────────────────────────
    soft_stop_loss_pct: float = 0.15      # freeze ramp if unrealized loss > 15%
    soft_stop_start: int = 60

    # ── Window loss limit ───────────────────────────────────────────────────
    window_loss_limit: float = 25.0       # stop increasing exposure if loss > $25

    # ── Intra-window sell loss cap ───────────────────────────────────────────
    max_intrawindow_sell_loss: float = -3.0  # stop selling if realized losses exceed $3

    # ── Combined avg gate ────────────────────────────────────────────────────
    # Freeze buys on the expensive side when combined_avg >= this threshold.
    # Prevents over-deploying into a losing combined position (>$1 = guaranteed loss).
    combined_avg_buy_gate: float = 0.97     # stop adding to expensive side above this

    # ── Ladder configuration ─────────────────────────────────────────────────
    shares_per_order: int = 5

    # Bid-dependent offsets (chosen by _ladder_for_bid):
    #   lottery  (bid <= 0.15): 9 levels, large spread
    #   cheap    (bid <= 0.35): 7 levels
    #   mid      (bid <= 0.60): 5 levels
    #   winning  (bid >  0.60): 3 levels
    offsets_lottery: tuple = (0.00, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08)
    offsets_cheap: tuple = (0.00, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06)
    offsets_mid: tuple = (0.00, 0.01, 0.02, 0.03, 0.05)
    offsets_winning: tuple = (0.00, 0.01, 0.03)

    # ── Market vs model arbitration ─────────────────────────────────────────
    market_override_edge: float = 0.10    # market overrides model when edge > this
    market_strong_edge: float = 0.20      # very clear market direction
    model_only_edge: float = 0.05         # market unclear, trust model only

    # ── Timing ─────────────────────────────────────────────────────────────
    commit_seconds: int = 250             # stop all trading at this second
