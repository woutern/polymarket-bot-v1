"""Tests for the risk manager."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest

from polybot.risk.manager import RiskManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _today() -> str:
    return date.today().isoformat()


def _make_manager(**kwargs) -> RiskManager:
    rm = RiskManager(**kwargs)
    # Force date initialisation so _check_new_day is a no-op in tests
    rm._current_date = _today()
    return rm


# ---------------------------------------------------------------------------
# can_trade
# ---------------------------------------------------------------------------

class TestCanTrade:
    def test_can_trade_initially(self):
        rm = _make_manager()
        assert rm.can_trade() is True

    def test_cannot_trade_when_circuit_breaker_active(self):
        rm = _make_manager()
        rm._circuit_breaker_active = True
        assert rm.can_trade() is False

    def test_can_trade_after_circuit_breaker_resets_on_new_day(self):
        """Circuit breaker clears when a new calendar day begins."""
        rm = _make_manager()
        rm._circuit_breaker_active = True
        rm._current_date = "1970-01-01"  # stale date triggers _check_new_day
        assert rm.can_trade() is True
        assert rm._circuit_breaker_active is False


# ---------------------------------------------------------------------------
# record_trade and daily loss cap
# ---------------------------------------------------------------------------

class TestRecordTrade:
    def test_record_profitable_trade(self):
        rm = _make_manager(bankroll=1000.0)
        rm.record_trade(pnl=10.0)
        assert rm.daily_pnl == pytest.approx(10.0)
        assert rm.daily_trades == 1
        assert rm.bankroll == pytest.approx(1010.0)

    def test_record_losing_trade(self):
        rm = _make_manager(bankroll=1000.0)
        rm.record_trade(pnl=-5.0)
        assert rm.daily_pnl == pytest.approx(-5.0)
        assert rm.bankroll == pytest.approx(995.0)

    def test_multiple_trades_accumulate(self):
        rm = _make_manager(bankroll=1000.0)
        rm.record_trade(pnl=10.0)
        rm.record_trade(pnl=-3.0)
        assert rm.daily_pnl == pytest.approx(7.0)
        assert rm.daily_trades == 2
        assert rm.bankroll == pytest.approx(1007.0)

    def test_circuit_breaker_triggers_on_daily_loss_cap(self):
        """Loss > daily_loss_cap_pct triggers circuit breaker."""
        rm = _make_manager(bankroll=1000.0, daily_loss_cap_pct=0.05)
        # 5% of 1000 = $50 limit. A $60 loss should trigger.
        rm.record_trade(pnl=-60.0)
        assert rm._circuit_breaker_active is True
        assert rm.can_trade() is False

    def test_circuit_breaker_not_triggered_below_cap(self):
        """Loss below cap does not trigger circuit breaker."""
        rm = _make_manager(bankroll=1000.0, daily_loss_cap_pct=0.05)
        rm.record_trade(pnl=-40.0)  # 4% loss, below 5% cap
        assert rm._circuit_breaker_active is False

    def test_circuit_breaker_uses_updated_bankroll(self):
        """Loss limit is recalculated against the updated bankroll after the trade."""
        rm = _make_manager(bankroll=1000.0, daily_loss_cap_pct=0.05)
        # After losing $60 bankroll = $940, loss_limit = 940 * 0.05 = $47
        # daily_pnl = -60 which is < -47 → circuit breaker triggers
        rm.record_trade(pnl=-60.0)
        assert rm._circuit_breaker_active is True

    def test_zero_pnl_trade(self):
        """Recording a zero PnL trade still counts as a trade."""
        rm = _make_manager(bankroll=1000.0)
        rm.record_trade(pnl=0.0)
        assert rm.daily_trades == 1
        assert rm.daily_pnl == 0.0
        assert rm._circuit_breaker_active is False


# ---------------------------------------------------------------------------
# Slippage tracking
# ---------------------------------------------------------------------------

class TestSlippageTracking:
    def test_avg_slippage_no_trades(self):
        rm = _make_manager()
        assert rm.avg_slippage == 0.0

    def test_avg_slippage_single_trade(self):
        rm = _make_manager()
        rm.record_trade(pnl=0.0, slippage=0.01)
        assert rm.avg_slippage == pytest.approx(0.01)

    def test_avg_slippage_multiple_trades(self):
        rm = _make_manager()
        rm.record_trade(pnl=0.0, slippage=0.01)
        rm.record_trade(pnl=0.0, slippage=0.03)
        assert rm.avg_slippage == pytest.approx(0.02)

    def test_slippage_uses_absolute_value(self):
        """Negative slippage is stored as absolute value."""
        rm = _make_manager()
        rm.record_trade(pnl=0.0, slippage=-0.02)
        assert rm.avg_slippage == pytest.approx(0.02)

    def test_zero_slippage_not_counted(self):
        """slippage=0 does not increment _slippage_count."""
        rm = _make_manager()
        rm.record_trade(pnl=5.0, slippage=0.0)
        assert rm._slippage_count == 0
        assert rm.avg_slippage == 0.0

    def test_mixed_slippage_trades(self):
        rm = _make_manager()
        rm.record_trade(pnl=5.0, slippage=0.02)
        rm.record_trade(pnl=0.0, slippage=0.0)   # not counted
        rm.record_trade(pnl=-3.0, slippage=0.04)
        assert rm._slippage_count == 2
        assert rm.avg_slippage == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# max_position_size
# ---------------------------------------------------------------------------

class TestMaxPositionSize:
    def test_max_position_size(self):
        rm = _make_manager(bankroll=1000.0, max_position_pct=0.01)
        assert rm.max_position_size() == pytest.approx(10.0)

    def test_max_position_size_updates_with_bankroll(self):
        rm = _make_manager(bankroll=1000.0, max_position_pct=0.01)
        rm.bankroll = 2000.0
        assert rm.max_position_size() == pytest.approx(20.0)

    def test_max_position_size_zero_bankroll(self):
        rm = _make_manager(bankroll=0.0, max_position_pct=0.01)
        assert rm.max_position_size() == 0.0


# ---------------------------------------------------------------------------
# Daily reset (_check_new_day)
# ---------------------------------------------------------------------------

class TestDailyReset:
    def test_new_day_resets_daily_pnl(self):
        rm = _make_manager(bankroll=1000.0)
        rm.daily_pnl = -50.0
        rm._current_date = "1970-01-01"  # stale
        rm._check_new_day()
        assert rm.daily_pnl == 0.0

    def test_new_day_resets_daily_trades(self):
        rm = _make_manager()
        rm.daily_trades = 10
        rm._current_date = "1970-01-01"
        rm._check_new_day()
        assert rm.daily_trades == 0

    def test_new_day_clears_circuit_breaker(self):
        rm = _make_manager()
        rm._circuit_breaker_active = True
        rm._current_date = "1970-01-01"
        rm._check_new_day()
        assert rm._circuit_breaker_active is False

    def test_new_day_resets_slippage(self):
        rm = _make_manager()
        rm._slippage_total = 0.5
        rm._slippage_count = 5
        rm._current_date = "1970-01-01"
        rm._check_new_day()
        assert rm._slippage_total == 0.0
        assert rm._slippage_count == 0

    def test_same_day_does_not_reset(self):
        rm = _make_manager()
        rm.daily_pnl = -20.0
        rm.daily_trades = 3
        # _current_date already set to today in _make_manager
        rm._check_new_day()  # should be a no-op
        assert rm.daily_pnl == -20.0
        assert rm.daily_trades == 3

    def test_circuit_breaker_property_triggers_check(self):
        """Accessing circuit_breaker_active also calls _check_new_day."""
        rm = _make_manager()
        rm._circuit_breaker_active = True
        rm._current_date = "1970-01-01"
        # Accessing the property should reset on new day
        assert rm.circuit_breaker_active is False
