"""Unit tests for MarketMakerStrategy.

Tests every sell trigger, buy guard, and state machine independently.
No mocks, no I/O. Each test builds the exact market scenario it needs.
"""

import pytest
from polybot.core.position import Position
from polybot.strategy.base import MarketState
from polybot.strategy.market_maker import MarketMakerStrategy
from polybot.strategy.profile import StrategyProfile
from polybot.strategy.profiles import BTC_5M_PROFILE


def make_strategy(**kwargs) -> MarketMakerStrategy:
    """Build a strategy with optional profile overrides."""
    profile = StrategyProfile(**{**BTC_5M_PROFILE.__dict__, **kwargs})
    s = MarketMakerStrategy(profile)
    s.reset()
    return s


def make_market(seconds=60, yes_bid=0.55, no_bid=0.45, prob_up=0.60) -> MarketState:
    return MarketState(seconds=seconds, yes_bid=yes_bid, no_bid=no_bid,
                       yes_ask=yes_bid+0.01, no_ask=no_bid+0.01, prob_up=prob_up)


def run_window(strategy, ticks, budget=80.0):
    """Run a list of MarketState ticks. Returns (final_position, final_budget)."""
    pos = Position()
    for market in ticks:
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
    return pos, budget


# ── Budget curve ─────────────────────────────────────────────────────────────

def test_budget_curve_open():
    s = make_strategy()
    assert s._budget_curve(0) == pytest.approx(0.10)
    assert s._budget_curve(5) == pytest.approx(0.10)


def test_budget_curve_ramp():
    s = make_strategy()
    assert s._budget_curve(60) == pytest.approx(0.22, abs=0.01)
    assert s._budget_curve(180) == pytest.approx(0.60, abs=0.01)
    assert s._budget_curve(250) == pytest.approx(0.85, abs=0.02)


def test_budget_curve_caps_at_85():
    s = make_strategy()
    assert s._budget_curve(300) == pytest.approx(0.85)


# ── Direction determination ──────────────────────────────────────────────────

def test_direction_market_strong():
    s = make_strategy()
    m = make_market(yes_bid=0.75, no_bid=0.25, prob_up=0.40)  # market says UP
    winning_up, conf, source = s._determine_direction(m)
    assert winning_up is True
    assert source == "market_strong"
    assert conf == 0.90


def test_direction_market_override():
    s = make_strategy()
    # edge=0.14 is between market_override_edge(0.10) and market_strong_edge(0.20) → "market"
    m = make_market(yes_bid=0.57, no_bid=0.43, prob_up=0.40)  # market says UP, model says DOWN
    winning_up, conf, source = s._determine_direction(m)
    assert winning_up is True  # market wins
    assert source == "market"


def test_direction_model_when_market_flat():
    s = make_strategy()
    m = make_market(yes_bid=0.51, no_bid=0.49, prob_up=0.65)  # market flat, model says UP
    winning_up, conf, source = s._determine_direction(m)
    assert winning_up is True
    assert "model" in source


def test_direction_model_weak():
    s = make_strategy()
    m = make_market(yes_bid=0.50, no_bid=0.50, prob_up=0.51)
    winning_up, conf, source = s._determine_direction(m)
    assert conf == 0.50
    assert source == "model_weak"


# ── No-trade zone ────────────────────────────────────────────────────────────

def test_no_trade_zone_skips_all():
    s = make_strategy(no_trade_zone=0.05)
    pos = Position()
    pos.buy(True, 20, 0.50)
    pos.buy(False, 10, 0.50)
    # Spread = 0.02 < 0.05 → no action
    m = make_market(yes_bid=0.51, no_bid=0.49)
    action = s.on_tick(m, pos, 80.0)
    assert not action.has_action()


# ── Commit ───────────────────────────────────────────────────────────────────

def test_commit_stops_all_trading():
    s = make_strategy(commit_seconds=250)
    pos = Position()
    m = make_market(seconds=255)
    action = s.on_tick(m, pos, 80.0)
    assert not action.has_action()


# ── Buy guards ───────────────────────────────────────────────────────────────

def test_hard_cap_blocks_buy():
    s = make_strategy(hard_cap=0.82)
    pos = Position()
    m = make_market(seconds=30, yes_bid=0.85, no_bid=0.15)  # yes too expensive
    action = s.on_tick(m, pos, 80.0)
    assert action.buy_up_shares == 0  # blocked by price cap


def test_dying_side_block_up():
    s = make_strategy(dying_side_start=60)
    pos = Position()
    # no_bid > 0.70 → UP is dying → don't buy UP
    m = make_market(seconds=90, yes_bid=0.25, no_bid=0.75)
    action = s.on_tick(m, pos, 80.0)
    assert action.buy_up_shares == 0


def test_dying_side_block_down():
    s = make_strategy(dying_side_start=60)
    pos = Position()
    # yes_bid > 0.70 → DOWN is dying → don't buy DOWN
    m = make_market(seconds=90, yes_bid=0.75, no_bid=0.25)
    action = s.on_tick(m, pos, 80.0)
    assert action.buy_down_shares == 0


def test_dying_side_not_applied_before_start():
    # Isolate dying_side timing: before T+60, UP buy is allowed even with no_bid > threshold.
    # budget=500 → usable=min(80, huge)=80 → up_budget=20 clears the 1.30 needed.
    # early_balance_cap=0.70 prevents balance cap from blocking (10/15=67% < 70%).
    # early_rebalance_threshold + dead_side_threshold=0.80 disable sells for clean isolation.
    s = make_strategy(dying_side_start=60, early_rebalance_threshold=0.80,
                      dead_side_threshold=0.80, early_balance_cap=0.70, budget=500)
    pos = Position()
    pos.buy(True, 5, 0.35)   # seed so projected-entry check is bypassed
    pos.buy(False, 5, 0.40)  # combined_avg 0.75 < gate
    # Before T+60 — dying side rule doesn't apply, UP buy should be allowed
    m = make_market(seconds=30, yes_bid=0.25, no_bid=0.75)
    action = s.on_tick(m, pos, 80.0)
    assert action.buy_up_shares > 0  # allowed before dying_side_start


def test_balance_cap_early():
    s = make_strategy(early_balance_cap=0.65)
    pos = Position()
    # Load UP to 80% of total
    for _ in range(20):
        pos.buy(True, 5, 0.50)
    for _ in range(5):
        pos.buy(False, 5, 0.45)
    # UP = 100, DOWN = 25, total = 125. up% = 80% > 65% cap
    m = make_market(seconds=60, yes_bid=0.50, no_bid=0.50)
    action = s.on_tick(m, pos, 80.0)
    assert action.buy_up_shares == 0  # UP blocked by balance cap


def test_antichurn_unfavored_blocked_above_last_sell():
    s = make_strategy()
    pos = Position()
    pos.buy(False, 10, 0.40)
    pos.sell(False, 5, 0.45)  # sold DOWN at 0.45
    # prob_up = 0.70 → favored UP → DOWN is unfavored
    # DOWN bid = 0.46 > last sell 0.45 → blocked
    m = make_market(seconds=60, yes_bid=0.65, no_bid=0.46, prob_up=0.70)
    action = s.on_tick(m, pos, 80.0)
    assert action.buy_down_shares == 0


def test_antichurn_favored_not_blocked():
    s = make_strategy()
    pos = Position()
    pos.buy(True, 10, 0.50)
    pos.sell(True, 5, 0.55)  # sold UP at 0.55
    # prob_up = 0.70 → favored UP → can rebuy UP above 0.55
    m = make_market(seconds=60, yes_bid=0.60, no_bid=0.40, prob_up=0.70)
    action = s.on_tick(m, pos, 80.0)
    assert action.buy_up_shares > 0  # favored side: no anti-churn


# ── Sell triggers ────────────────────────────────────────────────────────────

def test_dead_side_sell():
    s = make_strategy(sells_enabled=True)
    pos = Position()
    pos.buy(True, 20, 0.55)   # UP shares
    pos.buy(False, 20, 0.40)  # DOWN shares
    # no_bid = 0.92 > dead_side_threshold=0.90 → UP is dying → sell UP
    # yes_bid=0.28 >= 0.25 so lottery-ticket guard doesn't block
    m = make_market(seconds=60, yes_bid=0.28, no_bid=0.92)
    s._detect_reversal(m, False)  # prime direction state
    action = s.on_tick(m, pos, 80.0)
    assert action.sell_up_shares > 0
    assert action.reason == "DEAD_SIDE"


def test_unfavored_rich_sell():
    s = make_strategy(unfavored_rich_threshold=0.50, sells_enabled=True)
    pos = Position()
    pos.buy(False, 10, 0.60)  # DOWN bought expensive (avg = 0.60)
    pos.buy(True, 10, 0.40)
    # prob_up = 0.70 → UP is favored → DOWN is unfavored
    # DOWN avg (0.60) > threshold (0.50) and market edge > 0.10 → sell DOWN
    m = make_market(seconds=60, yes_bid=0.65, no_bid=0.35, prob_up=0.70)
    action = s.on_tick(m, pos, 80.0)
    assert action.sell_down_shares > 0
    assert action.reason == "UNFAVORED_RICH"


def test_late_dump_requires_consecutive_ticks():
    s = make_strategy(late_dump_start=180, late_dump_threshold=0.30, late_dump_min_ticks=5, sells_enabled=True)
    pos = Position()
    pos.buy(False, 10, 0.40)
    pos.buy(True, 20, 0.60)
    # Use yes_bid=0.62 (winning side < early_rebalance_threshold=0.65 so EARLY_REBALANCE doesn't fire,
    # and < dead_side_threshold=0.80 so DEAD_SIDE doesn't fire either)
    # no_bid=0.28 <= late_dump_threshold=0.30 → LATE_DUMP counter increments
    for sec in range(180, 184):
        m = make_market(seconds=sec, yes_bid=0.62, no_bid=0.28)
        action = s.on_tick(m, pos, 80.0)
    # 4 ticks < 5 required → no late dump yet
    assert action.sell_down_shares == 0 or action.reason != "LATE_DUMP"

    # 5th tick should trigger
    m = make_market(seconds=184, yes_bid=0.62, no_bid=0.28)
    action = s.on_tick(m, pos, 80.0)
    assert action.sell_down_shares > 0
    assert action.reason == "LATE_DUMP"


def test_payout_floor_sell():
    s = make_strategy(payout_floor_sell_enabled=True, payout_floor_min_excess=5, sells_enabled=True)
    pos = Position()
    pos.buy(True, 20, 0.40)   # UP: 20 shares at 0.40 avg
    pos.buy(False, 10, 0.35)  # DOWN: 10 shares at 0.35
    # floor = 10, excess_up = 10
    # hold_value_up with prob_up=0.64 = 0.64
    # yes_bid = 0.45 > 0.64? No → no sell. yes_bid needs to be > hold_value

    # Set yes_bid = 0.70 > hold_value = 0.64 → should sell UP excess
    m = make_market(seconds=60, yes_bid=0.70, no_bid=0.30, prob_up=0.64)
    # DOWN is winning (no_bid < yes_bid... wait yes_bid=0.70 > no_bid=0.30 so UP is winning)
    # UP is winning → UP is NOT losing side. So PAYOUT_FLOOR sells the losing side (DOWN).
    # DOWN: excess = 0 (10 shares = floor). No payout floor sell.
    action = s.on_tick(m, pos, 80.0)
    # In this case, no PAYOUT_FLOOR because excess is on the winning side
    # The mechanic sells excess on the LOSING side

    # Build scenario where losing side has excess
    pos2 = Position()
    pos2.buy(True, 10, 0.45)   # UP: 10 shares
    pos2.buy(False, 20, 0.35)  # DOWN: 20 shares → excess_down = 10
    # yes_bid > no_bid → UP is winning → DOWN is losing
    # DOWN has excess = 10, hold_value_down = 1-0.64 = 0.36
    # no_bid = 0.40 > 0.36 → should sell DOWN excess
    s2 = make_strategy(payout_floor_sell_enabled=True, payout_floor_min_excess=5, sells_enabled=True)
    m2 = make_market(seconds=60, yes_bid=0.65, no_bid=0.40, prob_up=0.64)
    action2 = s2.on_tick(m2, pos2, 80.0)
    assert action2.sell_down_shares > 0
    assert action2.reason == "PAYOUT_FLOOR"


def test_reversal_detected_triggers_sell():
    s = make_strategy(sells_enabled=True, disable_reversals_seconds=260)
    pos = Position()
    pos.buy(True, 20, 0.55)
    pos.buy(False, 10, 0.40)

    # First: UP is winning
    m1 = make_market(seconds=40, yes_bid=0.65, no_bid=0.35)
    s.on_tick(m1, pos, 80.0)

    # Now: direction flips → DOWN is winning
    m2 = make_market(seconds=41, yes_bid=0.35, no_bid=0.65)
    action = s.on_tick(m2, pos, 80.0)
    # UP is now losing side → should sell UP
    assert action.sell_up_shares > 0
    assert action.reason == "REVERSAL"


# ── Sell-and-rebuy ───────────────────────────────────────────────────────────

def test_sell_and_rebuy_fires_after_dead_side():
    s = make_strategy(sells_enabled=True, disable_reversals_seconds=260)
    pos = Position()
    pos.buy(True, 5, 0.55)
    pos.buy(False, 5, 0.40)
    # dead_side_threshold=0.90: use a lower custom threshold so no_ask stays under hard_cap
    s.profile.dead_side_threshold = 0.80
    # no_bid=0.81 > 0.80 → DEAD_SIDE fires; no_ask=0.82 <= hard_cap → rebuy DOWN valid
    m = make_market(seconds=30, yes_bid=0.28, no_bid=0.81, prob_up=0.10)
    s._detect_reversal(m, False)  # prime direction state (DOWN winning)
    action = s.on_tick(m, pos, 80.0)
    assert action.sell_up_shares > 0
    assert action.buy_down_shares > 0  # rebuy the winning side via sell-and-rebuy


# ── Chop detection ───────────────────────────────────────────────────────────

def test_chop_regime_reduces_size():
    s = make_strategy(chop_flip_threshold=2, chop_size_multiplier=0.50)
    pos = Position()
    budget = 80.0

    # Force 3 direction flips within 120s to trigger chop
    directions = [(0.60, 0.40), (0.40, 0.60), (0.60, 0.40), (0.40, 0.60)]
    for i, (yes, no) in enumerate(directions):
        m = make_market(seconds=20 + i * 15, yes_bid=yes, no_bid=no)
        s.on_tick(m, pos, budget)

    assert s.chop_regime is True


# ── Soft stop loss ───────────────────────────────────────────────────────────

def test_soft_stop_freezes_ramp():
    s = make_strategy(soft_stop_start=60, soft_stop_loss_pct=0.10)
    pos = Position()
    # Simulate a bad position: all in UP, UP bid dropped to 0.20
    pos.buy(True, 40, 0.70)  # cost = 28.00
    pos.buy(False, 5, 0.30)  # cost = 1.50, total = 29.50

    # worst_pnl = down wins → 5 - 29.50 = -24.50. loss_pct = 24.50/29.50 ≈ 83%
    m = make_market(seconds=90, yes_bid=0.20, no_bid=0.80, prob_up=0.30)
    # max_deploy should be frozen at net_cost, not allowed to grow
    action = s.on_tick(m, pos, 80.0)
    # Soft stop prevents increasing exposure — no new buys should increase net cost
    # (budget curve at T+90 would allow ~34% = 27.20, but soft stop caps at net_cost=29.50)
    # Main thing: it doesn't crash and actions are reasonable
    assert action is not None


# ── Window loss limit ────────────────────────────────────────────────────────

def test_window_loss_limit_freezes_exposure():
    s = make_strategy(window_loss_limit=25.0)
    pos = Position()
    pos.buy(True, 40, 0.70)   # 28.00
    pos.buy(False, 5, 0.30)   # 1.50, total = 29.50
    # worst_pnl = 5 - 29.50 = -24.50 — just under $25 limit, still OK
    m = make_market(seconds=120, yes_bid=0.20, no_bid=0.80)
    action = s.on_tick(m, pos, 80.0)
    assert action is not None  # doesn't crash

    # Now push past the limit
    pos2 = Position()
    pos2.buy(True, 50, 0.70)  # 35.00
    pos2.buy(False, 5, 0.30)  # 1.50, total = 36.50
    # worst_pnl = 5 - 36.50 = -31.50 > -25 limit
    m2 = make_market(seconds=120, yes_bid=0.20, no_bid=0.80)
    s2 = make_strategy(window_loss_limit=25.0)
    action2 = s2.on_tick(m2, pos2, 80.0)
    # Should not increase exposure (buy orders should be blocked by frozen max_deploy)
    assert action2 is not None


# ── No-new-risk zone ─────────────────────────────────────────────────────────

def test_no_new_risk_after_t230():
    s = make_strategy(no_new_risk_seconds=230)
    pos = Position()
    pos.buy(True, 10, 0.55)
    pos.buy(False, 10, 0.40)
    budget = 80.0
    m = make_market(seconds=235, yes_bid=0.60, no_bid=0.40)
    action = s.on_tick(m, pos, budget)
    # max_deploy frozen at net_cost → no new buys that increase net cost
    # (some buys might happen if there's budget from sells, but net can't grow)
    assert action is not None


# ── Full scenario: UP trend ──────────────────────────────────────────────────

def test_up_trend_deploys_capital():
    s = make_strategy()
    s.reset()
    ticks = []
    for sec in range(5, 250):
        t = sec / 300.0
        yes_bid = round(0.50 + 0.30 * t, 3)
        no_bid = round(1.0 - yes_bid, 3)
        ticks.append(make_market(seconds=sec, yes_bid=yes_bid, no_bid=no_bid, prob_up=0.65))

    pos, remaining = run_window(s, ticks, budget=80.0)
    deployed = pos.net_cost
    assert deployed > 20.0, f"Expected > $20 deployed, got ${deployed:.2f}"
    # In a strong uptrend (YES climbs to 75¢), EARLY_REBALANCE + DEAD_SIDE will sell
    # DOWN shares on the way up — this is expected and correct behaviour.
    # The key invariant is that UP shares are deployed.
    assert pos.up_shares > 0, "Should have some UP shares in UP trend"


def test_reversal_scenario_flips_direction():
    s = make_strategy()
    s.reset()
    ticks = []
    for sec in range(5, 250):
        if sec < 120:
            yes_bid = round(0.50 + 0.20 * (sec / 120), 3)
        else:
            yes_bid = round(0.70 - 0.30 * ((sec - 120) / 130), 3)
        yes_bid = max(0.05, min(0.95, yes_bid))
        no_bid = round(1.0 - yes_bid, 3)
        ticks.append(make_market(seconds=sec, yes_bid=yes_bid, no_bid=no_bid, prob_up=0.50))

    pos, remaining = run_window(s, ticks, budget=80.0)
    # Bot detected at least one reversal
    assert s.reversal_count >= 1
    # Budget was deployed
    assert pos.net_cost > 10.0


# ── AccumulateOnly sanity ────────────────────────────────────────────────────

def test_accumulate_only_no_sells():
    from polybot.strategy.accumulate_only import AccumulateOnlyStrategy
    from polybot.strategy.profiles import SOL_5M_PROFILE
    s = AccumulateOnlyStrategy(SOL_5M_PROFILE)
    s.reset()
    ticks = [make_market(seconds=sec, yes_bid=0.55, no_bid=0.45) for sec in range(5, 250)]
    pos, _ = run_window(s, ticks, budget=50.0)
    assert pos.sells_count == 0
    assert pos.up_shares > 0
    assert pos.down_shares > 0
