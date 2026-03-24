"""Unit tests for Position.

Pure data class — no mocks, no I/O, just logic.
"""

import pytest
from polybot.core.position import Position


# ── Basics ──────────────────────────────────────────────────────────────────

def test_initial_state():
    p = Position()
    assert p.up_shares == 0
    assert p.down_shares == 0
    assert p.net_cost == 0.0
    assert p.combined_avg == 0.0
    assert p.payout_floor == 0
    assert p.total_shares == 0
    assert not p.is_gp()


def test_buy_up():
    p = Position()
    cost = p.buy(True, 10, 0.50)
    assert cost == 5.0
    assert p.up_shares == 10
    assert p.up_cost == 5.0
    assert p.up_avg == 0.50
    assert p.buys_count == 1
    assert p.total_bought_cost == 5.0


def test_buy_down():
    p = Position()
    cost = p.buy(False, 10, 0.45)
    assert cost == 4.50
    assert p.down_shares == 10
    assert p.down_avg == 0.45


def test_sell_up():
    p = Position()
    p.buy(True, 10, 0.60)
    proceeds = p.sell(True, 5, 0.65)
    assert proceeds == 3.25
    assert p.up_shares == 5
    assert p.sells_count == 1
    assert p.total_sold_proceeds == 3.25


def test_sell_clamps_to_available():
    p = Position()
    p.buy(True, 5, 0.50)
    proceeds = p.sell(True, 100, 0.50)  # only 5 available
    assert p.up_shares == 0
    assert proceeds == 2.50


def test_sell_records_last_price():
    p = Position()
    p.buy(True, 10, 0.50)
    p.sell(True, 5, 0.62)
    assert p.last_sell_price(True) == 0.62
    assert p.last_sell_price(False) == 0.0  # down side untouched


# ── Averages and combined ────────────────────────────────────────────────────

def test_combined_avg_both_sides():
    p = Position()
    p.buy(True, 10, 0.55)
    p.buy(False, 10, 0.40)
    assert p.combined_avg == pytest.approx(0.55 + 0.40, abs=0.001)


def test_combined_avg_one_side_only():
    p = Position()
    p.buy(True, 10, 0.55)
    assert p.combined_avg == 0.0  # needs both sides


# ── Payout floor ─────────────────────────────────────────────────────────────

def test_payout_floor_equal_sides():
    p = Position()
    p.buy(True, 10, 0.50)
    p.buy(False, 10, 0.45)
    assert p.payout_floor == 10


def test_payout_floor_unequal():
    p = Position()
    p.buy(True, 20, 0.50)
    p.buy(False, 10, 0.45)
    assert p.payout_floor == 10


def test_payout_floor_zero_one_side():
    p = Position()
    p.buy(True, 10, 0.50)
    assert p.payout_floor == 0


def test_excess_shares_up():
    p = Position()
    p.buy(True, 20, 0.50)
    p.buy(False, 10, 0.45)
    # floor = 10, up has 20 → excess = 10
    assert p.excess_shares(True) == 10
    assert p.excess_shares(False) == 0


# ── Hold value ───────────────────────────────────────────────────────────────

def test_hold_value_up():
    p = Position()
    assert p.hold_value(0.64, True) == pytest.approx(0.64)


def test_hold_value_down():
    p = Position()
    assert p.hold_value(0.64, False) == pytest.approx(0.36)


def test_hold_value_equal():
    p = Position()
    assert p.hold_value(0.50, True) == pytest.approx(0.50)
    assert p.hold_value(0.50, False) == pytest.approx(0.50)


# ── PnL ──────────────────────────────────────────────────────────────────────

def test_pnl_if_up_profitable():
    p = Position()
    p.buy(True, 10, 0.55)  # cost = 5.50
    p.buy(False, 10, 0.40)  # cost = 4.00, total = 9.50
    # if UP wins: 10 × $1 - 9.50 = +$0.50
    assert p.pnl_if_up() == pytest.approx(0.50, abs=0.01)


def test_pnl_if_down_profitable():
    p = Position()
    p.buy(True, 10, 0.55)
    p.buy(False, 10, 0.40)
    # if DOWN wins: 10 × $1 - 9.50 = +$0.50
    assert p.pnl_if_down() == pytest.approx(0.50, abs=0.01)


def test_is_gp_true():
    p = Position()
    p.buy(True, 10, 0.45)
    p.buy(False, 10, 0.40)
    assert p.is_gp()


def test_is_gp_false():
    p = Position()
    p.buy(True, 10, 0.65)
    p.buy(False, 10, 0.40)
    # combined = 1.05 → not GP
    assert not p.is_gp()


def test_best_and_worst_pnl():
    p = Position()
    p.buy(True, 20, 0.55)  # 11.00
    p.buy(False, 10, 0.40)  # 4.00, total 15.00
    assert p.best_pnl() == max(p.pnl_if_up(), p.pnl_if_down())
    assert p.worst_pnl() == min(p.pnl_if_up(), p.pnl_if_down())
