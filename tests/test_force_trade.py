"""Tests for force_trade.py and dynamic sizing (0.5-1% of bankroll)."""

from __future__ import annotations

import pytest

from polybot.strategy.sizing import compute_size


class TestDynamicSizing:
    """Position sizing at 0.5-1% of bankroll."""

    def test_half_percent_min_on_253_bankroll(self):
        """$253 bankroll → min $1.25 (0.5%)."""
        size = compute_size(
            model_prob=0.70, market_price=0.55, bankroll=253.0,
            kelly_mult=0.25, max_position_pct=0.01,
            min_trade_usd=1.25, max_trade_usd=8.00,
        )
        assert size >= 1.25
        assert size <= 8.00

    def test_one_percent_max_on_253_bankroll(self):
        """Even with huge edge, cap at $2.50 (1%)."""
        size = compute_size(
            model_prob=0.99, market_price=0.10, bankroll=253.0,
            kelly_mult=0.25, max_position_pct=0.10,
            min_trade_usd=1.25, max_trade_usd=8.00,
        )
        assert size <= 8.00

    def test_no_edge_returns_zero(self):
        """No positive edge → no trade."""
        size = compute_size(
            model_prob=0.50, market_price=0.55, bankroll=253.0,
            min_trade_usd=1.25, max_trade_usd=8.00,
        )
        assert size == 0.0

    def test_sizing_scales_with_bankroll(self):
        """Larger bankroll → larger min/max."""
        # $500 bankroll, 0.5% = $2.50, 1% = $5.00
        size = compute_size(
            model_prob=0.80, market_price=0.50, bankroll=500.0,
            kelly_mult=0.25, max_position_pct=0.01,
            min_trade_usd=1.50, max_trade_usd=5.00,
        )
        assert 1.00 <= size <= 8.00

    def test_polymarket_minimum_is_one_dollar(self):
        """Even with tiny bankroll, Polymarket requires $1 minimum."""
        size = compute_size(
            model_prob=0.80, market_price=0.50, bankroll=50.0,
            min_trade_usd=1.00, max_trade_usd=1.00,
        )
        assert size == 1.00


class TestSignalEvaluation:
    """Test that SignalEvaluation captures rejection reasons correctly."""

    def test_min_move_rejection(self):
        from polybot.strategy.directional import generate_directional_signal
        from polybot.strategy.bayesian import BayesianUpdater
        from polybot.strategy.base_rate import BaseRateTable
        from polybot.models import OrderbookSnapshot
        import math

        table = BaseRateTable()
        b = BayesianUpdater(table)
        b.log_odds = math.log(0.75 / 0.25)
        b._initialized = True
        b._open_price = 100.0
        ob = OrderbookSnapshot(yes_best_ask=0.50, yes_best_bid=0.48, no_best_ask=0.50, no_best_bid=0.48)

        result = generate_directional_signal(
            bayesian=b, orderbook=ob,
            current_price=100.01, open_price=100.0,  # tiny move
            seconds_remaining=30, min_move_pct=0.08,
        )
        assert result.signal is None
        assert result.rejection_reason == "min_move"
        assert result.outcome == "rejected"

    def test_market_efficient_rejection(self):
        from polybot.strategy.directional import generate_directional_signal
        from polybot.strategy.bayesian import BayesianUpdater
        from polybot.strategy.base_rate import BaseRateTable
        from polybot.models import OrderbookSnapshot
        import math

        table = BaseRateTable()
        b = BayesianUpdater(table)
        b.log_odds = math.log(0.90 / 0.10)
        b._initialized = True
        b._open_price = 100.0
        ob = OrderbookSnapshot(yes_best_ask=0.95, yes_best_bid=0.93, no_best_ask=0.06, no_best_bid=0.04)

        result = generate_directional_signal(
            bayesian=b, orderbook=ob,
            current_price=100.5, open_price=100.0,  # 0.5% move
            seconds_remaining=30, max_market_price=0.75,
            use_ai=False,
        )
        assert result.signal is None
        assert result.rejection_reason == "market_efficient"

    def test_executed_signal_has_no_rejection(self):
        from polybot.strategy.directional import generate_directional_signal
        from polybot.strategy.bayesian import BayesianUpdater
        from polybot.strategy.base_rate import BaseRateTable
        from polybot.models import OrderbookSnapshot
        import math

        table = BaseRateTable()
        b = BayesianUpdater(table)
        b.log_odds = math.log(0.80 / 0.20)
        b._initialized = True
        b._open_price = 100.0
        ob = OrderbookSnapshot(yes_best_ask=0.50, yes_best_bid=0.48, no_best_ask=0.50, no_best_bid=0.48)

        result = generate_directional_signal(
            bayesian=b, orderbook=ob,
            current_price=100.5, open_price=100.0,
            seconds_remaining=30, min_move_pct=0.08, use_ai=False,
        )
        assert result.signal is not None
        assert result.rejection_reason is None
        assert result.outcome == "executed"
        assert result.pct_move > 0

    def test_obi_veto_rejection(self):
        from polybot.strategy.directional import generate_directional_signal
        from polybot.strategy.bayesian import BayesianUpdater
        from polybot.strategy.base_rate import BaseRateTable
        from polybot.models import OrderbookSnapshot
        import math

        table = BaseRateTable()
        b = BayesianUpdater(table)
        b.log_odds = math.log(0.80 / 0.20)
        b._initialized = True
        b._open_price = 100.0
        # Wide spread on YES side
        ob = OrderbookSnapshot(yes_best_ask=0.50, yes_best_bid=0.30, no_best_ask=0.50, no_best_bid=0.48)

        result = generate_directional_signal(
            bayesian=b, orderbook=ob,
            current_price=100.5, open_price=100.0,
            seconds_remaining=30, use_ai=False,
        )
        assert result.signal is None
        assert result.rejection_reason == "obi_veto"
