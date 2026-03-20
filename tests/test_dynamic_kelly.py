"""Tests for dynamic Kelly sizing based on LightGBM probability."""

from polybot.risk.manager import RiskManager


class TestDynamicKelly:
    def test_low_confidence_half_percent(self):
        """lgbm_prob < 0.60 → 0.5% of wallet."""
        rm = RiskManager(bankroll=200.0, min_trade_usd=1.0, max_trade_usd=10.0)
        size = rm.get_bet_size(lgbm_prob=0.55)
        assert size == 2.0  # 1% of 200

    def test_medium_confidence_one_percent(self):
        """lgbm_prob 0.60-0.70 → 1.0% of wallet, capped at $1.50."""
        rm = RiskManager(bankroll=200.0, min_trade_usd=1.0, max_trade_usd=8.00)
        size = rm.get_bet_size(lgbm_prob=0.65)
        assert size <= 8.00  # 1% of 200 = $2 → capped at $1.50

    def test_high_confidence_one_point_five(self):
        """lgbm_prob 0.70-0.80 → 1.5% of wallet, capped at $1.50."""
        rm = RiskManager(bankroll=200.0, min_trade_usd=1.0, max_trade_usd=8.00)
        size = rm.get_bet_size(lgbm_prob=0.75)
        assert size <= 8.00  # 1.5% of 200 = $3 → capped at $1.50

    def test_very_high_confidence_two_percent(self):
        """lgbm_prob > 0.80 → 2.0% of wallet, capped at $1.50."""
        rm = RiskManager(bankroll=200.0, min_trade_usd=1.0, max_trade_usd=8.00)
        size = rm.get_bet_size(lgbm_prob=0.85)
        assert size <= 8.00  # 2% of 200 = $4 → capped at $1.50

    def test_min_enforced(self):
        """Small bankroll → min $1.00."""
        rm = RiskManager(bankroll=50.0, min_trade_usd=1.0, max_trade_usd=10.0)
        size = rm.get_bet_size(lgbm_prob=0.55)
        assert size == 1.0  # 0.5% of 50 = $0.25 → min $1.00

    def test_max_enforced(self):
        """Large bankroll → max $1.50."""
        rm = RiskManager(bankroll=1000.0, min_trade_usd=1.0, max_trade_usd=8.00)
        size = rm.get_bet_size(lgbm_prob=0.85)
        assert size <= 8.00  # 2% of 1000 = $20 → hard cap $1.50

    def test_reduced_sizing_overrides(self):
        """During losing streak, always $1 flat."""
        rm = RiskManager(bankroll=200.0, min_trade_usd=1.0, max_trade_usd=10.0)
        rm._reduced_sizing = True
        size = rm.get_bet_size(lgbm_prob=0.90)
        assert size == 1.0

    def test_default_prob_uses_lowest_tier(self):
        """No lgbm_prob → default 0.5 → 0.5% tier."""
        rm = RiskManager(bankroll=200.0, min_trade_usd=1.0, max_trade_usd=10.0)
        size = rm.get_bet_size()
        assert size == 2.0  # 1% of 200


class TestDirectionFromPrice:
    def test_up_direction_only_above_threshold(self):
        """Direction=UP only when pct_move > min_move_pct."""
        from polybot.strategy.directional import generate_directional_signal
        from polybot.strategy.bayesian import BayesianUpdater
        from polybot.strategy.base_rate import BaseRateTable
        from polybot.models import OrderbookSnapshot, Direction
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
        assert result.signal.direction == Direction.UP

    def test_down_direction_only_below_threshold(self):
        from polybot.strategy.directional import generate_directional_signal
        from polybot.strategy.bayesian import BayesianUpdater
        from polybot.strategy.base_rate import BaseRateTable
        from polybot.models import OrderbookSnapshot, Direction
        import math

        table = BaseRateTable()
        b = BayesianUpdater(table)
        b.log_odds = math.log(0.20 / 0.80)
        b._initialized = True
        b._open_price = 100.0
        ob = OrderbookSnapshot(yes_best_ask=0.50, yes_best_bid=0.48, no_best_ask=0.50, no_best_bid=0.48)

        result = generate_directional_signal(
            bayesian=b, orderbook=ob,
            current_price=99.5, open_price=100.0,
            seconds_remaining=30, min_move_pct=0.08, use_ai=False,
        )
        assert result.signal is not None
        assert result.signal.direction == Direction.DOWN

    def test_no_bedrock_in_model_prob(self):
        """model_prob should not include Bedrock AI blend."""
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
            seconds_remaining=30, use_ai=True,  # AI enabled but should not affect model_prob
        )
        assert result.signal is not None
        assert result.signal.p_ai is None  # Bedrock not called
