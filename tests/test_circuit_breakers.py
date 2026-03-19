"""Tests for circuit breaker logic in RiskManager."""

import time
from unittest.mock import patch

import pytest

from polybot.risk.manager import RiskManager


class TestConsecutiveLosses:
    def test_three_losses_triggers_pause(self):
        rm = RiskManager(bankroll=100.0)
        rm.record_trade(-1.0)
        rm.record_trade(-1.0)
        rm.record_trade(-1.0)
        assert rm.can_trade() is False  # paused for 15 min

    def test_win_resets_streak(self):
        rm = RiskManager(bankroll=100.0)
        rm.record_trade(-1.0)
        rm.record_trade(-1.0)
        rm.record_trade(2.0)  # win resets
        rm.record_trade(-1.0)
        assert rm.can_trade() is True  # only 1 loss since reset

    def test_pause_lifts_after_15_min(self):
        rm = RiskManager(bankroll=100.0)
        rm.record_trade(-1.0)
        rm.record_trade(-1.0)
        rm.record_trade(-1.0)
        assert rm.can_trade() is False
        # Simulate 16 minutes passing
        rm._streak_pause_until = time.time() - 1
        assert rm.can_trade() is True


class TestReducedSizing:
    def test_five_losses_in_twenty_reduces_size(self):
        rm = RiskManager(bankroll=100.0)
        # 5 losses, 5 wins (enough for 10 recent)
        for _ in range(5):
            rm.record_trade(-1.0)
            rm._streak_pause_until = 0  # clear pause
        for _ in range(5):
            rm.record_trade(1.0)
        assert rm._reduced_sizing is True
        assert rm.get_bet_size() == rm.min_trade_usd  # $1 flat

    def test_recovery_lifts_reduced_sizing(self):
        rm = RiskManager(bankroll=100.0)
        # Trigger reduced sizing: 5 losses in 10
        for _ in range(5):
            rm.record_trade(-1.0)
            rm._streak_pause_until = 0
        for _ in range(5):
            rm.record_trade(1.0)
        assert rm._reduced_sizing is True
        # Add many more wins to push losses out of the 20-window
        for _ in range(15):
            rm.record_trade(1.0)
            rm._streak_pause_until = 0
        # Now recent 20 has mostly wins → should lift
        assert rm._reduced_sizing is False


class TestDailyLossCap:
    def test_ten_percent_daily_loss_stops_trading(self):
        rm = RiskManager(bankroll=100.0, daily_loss_cap_pct=0.10)
        rm.record_trade(-11.0)  # 11% loss
        assert rm._circuit_breaker_active is True
        assert rm.can_trade() is False


class TestBetSizing:
    def test_one_percent_of_bankroll(self):
        rm = RiskManager(bankroll=250.0, max_position_pct=0.01, min_trade_usd=1.0, max_trade_usd=10.0)
        size = rm.get_bet_size(lgbm_prob=0.65)  # 0.60-0.70 tier = 1%
        assert size == 2.50  # 1% of 250

    def test_min_enforced(self):
        rm = RiskManager(bankroll=50.0, max_position_pct=0.01, min_trade_usd=1.0, max_trade_usd=10.0)
        assert rm.get_bet_size() == 1.0  # 0.50 < min

    def test_max_enforced(self):
        rm = RiskManager(bankroll=2000.0, max_position_pct=0.01, min_trade_usd=1.0, max_trade_usd=10.0)
        assert rm.get_bet_size() == 5.0  # hard cap $5.00
