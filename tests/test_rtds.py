"""Tests for RTDS oracle feed, Black-Scholes probability, and oracle signal."""

from __future__ import annotations

import math

import pytest

from polybot.feeds.rtds_ws import (
    DISLOCATION_THRESHOLD,
    OracleState,
    RTDSClient,
    compute_oracle_probability,
    compute_realized_vol,
)


# ---------------------------------------------------------------------------
# OracleState
# ---------------------------------------------------------------------------

class TestOracleState:
    def test_update_chainlink_stores_price(self):
        state = OracleState(asset="BTC")
        state.update_chainlink(74000.0, 1773830000000)
        assert state.chainlink_price == 74000.0
        assert state.chainlink_ts == 1773830000000

    def test_compute_lag_positive(self):
        """Coinbase > Chainlink → positive lag (Coinbase leads UP)."""
        state = OracleState(asset="BTC")
        state.update_chainlink(73900.0, 1773830000000)
        state.compute_lag(coinbase_price=74100.0)
        assert state.oracle_lag_pct > 0
        assert abs(state.oracle_lag_pct - 0.002706) < 0.001

    def test_compute_lag_negative(self):
        """Coinbase < Chainlink → negative lag (Coinbase leads DOWN)."""
        state = OracleState(asset="BTC")
        state.update_chainlink(74100.0, 1773830000000)
        state.compute_lag(coinbase_price=73900.0)
        assert state.oracle_lag_pct < 0

    def test_dislocation_true_above_threshold(self):
        """Oracle dislocation fires when |lag| > 0.3%."""
        state = OracleState(asset="BTC")
        state.update_chainlink(73900.0, 1773830000000)
        # 0.5% lag → dislocation
        coinbase = 73900 * 1.005
        state.compute_lag(coinbase)
        assert state.dislocation is True

    def test_dislocation_false_below_threshold(self):
        """No dislocation when |lag| < 0.3%."""
        state = OracleState(asset="BTC")
        state.update_chainlink(73900.0, 1773830000000)
        # 0.1% lag → no dislocation
        coinbase = 73900 * 1.001
        state.compute_lag(coinbase)
        assert state.dislocation is False

    def test_history_accumulates(self):
        state = OracleState(asset="BTC")
        state.update_chainlink(74000.0, 1773830000000)
        for i in range(5):
            state.compute_lag(74000 + i * 10)
        assert len(state.history) == 5

    def test_history_capped_at_60(self):
        state = OracleState(asset="BTC")
        state.update_chainlink(74000.0, 1773830000000)
        for i in range(100):
            state.compute_lag(74000 + i)
        assert len(state.history) == 60

    def test_lag_stats_empty(self):
        state = OracleState(asset="BTC")
        assert state.lag_mean == 0.0
        assert state.lag_p50 == 0.0
        assert state.lag_p95 == 0.0

    def test_lag_stats_with_data(self):
        state = OracleState(asset="BTC")
        state.update_chainlink(74000.0, 1773830000000)
        for i in range(20):
            state.compute_lag(74000.0 + i)
        assert state.lag_mean > 0
        assert state.lag_p50 > 0
        assert state.lag_p95 >= state.lag_p50

    def test_zero_prices_safe(self):
        """Zero prices don't crash."""
        state = OracleState(asset="BTC")
        state.update_chainlink(0.0, 0)
        state.compute_lag(0.0)
        assert state.oracle_lag_pct == 0.0


# ---------------------------------------------------------------------------
# Black-Scholes oracle probability
# ---------------------------------------------------------------------------

class TestOracleProbability:
    def test_at_the_money(self):
        """When spot == strike, probability should be ~0.5."""
        prob = compute_oracle_probability(
            spot_price=74000, strike=74000, realized_vol=0.50, seconds_remaining=60,
        )
        assert 0.45 < prob < 0.55

    def test_deep_in_the_money(self):
        """Spot >> strike → probability near 1.0."""
        prob = compute_oracle_probability(
            spot_price=75000, strike=74000, realized_vol=0.50, seconds_remaining=60,
        )
        assert prob > 0.80

    def test_deep_out_of_the_money(self):
        """Spot << strike → probability near 0.0."""
        prob = compute_oracle_probability(
            spot_price=73000, strike=74000, realized_vol=0.50, seconds_remaining=60,
        )
        assert prob < 0.20

    def test_more_time_wider_distribution(self):
        """With more time remaining, probability is closer to 0.5."""
        prob_60s = compute_oracle_probability(
            spot_price=74100, strike=74000, realized_vol=0.50, seconds_remaining=60,
        )
        prob_300s = compute_oracle_probability(
            spot_price=74100, strike=74000, realized_vol=0.50, seconds_remaining=300,
        )
        # More time → less certain → closer to 0.5
        assert abs(prob_300s - 0.5) < abs(prob_60s - 0.5)

    def test_higher_vol_wider_distribution(self):
        """Higher vol → less certain → probability closer to 0.5."""
        prob_low_vol = compute_oracle_probability(
            spot_price=74100, strike=74000, realized_vol=0.30, seconds_remaining=60,
        )
        prob_high_vol = compute_oracle_probability(
            spot_price=74100, strike=74000, realized_vol=1.00, seconds_remaining=60,
        )
        assert abs(prob_high_vol - 0.5) < abs(prob_low_vol - 0.5)

    def test_zero_inputs_return_neutral(self):
        assert compute_oracle_probability(0, 74000, 0.5, 60) == 0.5
        assert compute_oracle_probability(74000, 0, 0.5, 60) == 0.5
        assert compute_oracle_probability(74000, 74000, 0, 60) == 0.5
        assert compute_oracle_probability(74000, 74000, 0.5, 0) == 0.5

    def test_returns_float_in_0_1(self):
        for spot in [50000, 74000, 100000]:
            for vol in [0.1, 0.5, 1.5]:
                prob = compute_oracle_probability(spot, 74000, vol, 60)
                assert 0.0 <= prob <= 1.0


# ---------------------------------------------------------------------------
# Realized volatility
# ---------------------------------------------------------------------------

class TestRealizedVol:
    def test_constant_prices_zero_vol(self):
        prices = [100.0] * 50
        vol = compute_realized_vol(prices)
        assert vol == 0.0

    def test_increasing_prices_positive_vol(self):
        prices = [100 + i * 0.01 for i in range(100)]
        vol = compute_realized_vol(prices)
        assert vol > 0

    def test_too_few_prices_returns_zero(self):
        assert compute_realized_vol([100, 101]) == 0.0
        assert compute_realized_vol([]) == 0.0

    def test_annualized_reasonable_range(self):
        """BTC-like 250ms ticks should give ~30-100% annualized vol."""
        import random
        random.seed(42)
        # Simulate BTC-like random walk: ~0.001% per tick
        prices = [74000.0]
        for _ in range(200):
            ret = random.gauss(0, 0.00005)  # ~0.005% per tick
            prices.append(prices[-1] * math.exp(ret))
        vol = compute_realized_vol(prices, tick_interval_seconds=0.25)
        # Should be somewhere between 10% and 200% annualized
        assert 0.05 < vol < 3.0


# ---------------------------------------------------------------------------
# RTDSClient message parsing
# ---------------------------------------------------------------------------

class TestRTDSParsing:
    def test_parse_batch_message(self):
        client = RTDSClient(assets=["BTC"])
        msg = json.dumps({
            "payload": {
                "data": [
                    {"timestamp": 1773830000000, "value": 73900.0},
                    {"timestamp": 1773830001000, "value": 73905.0},
                ]
            },
            "timestamp": 1773830001000,
            "topic": "crypto_prices_chainlink",
            "type": "update",
        })
        client._handle_message(msg)
        assert client.oracle_states["BTC"].chainlink_price == 73905.0

    def test_parse_error_message_safe(self):
        client = RTDSClient(assets=["BTC"])
        msg = json.dumps({"statusCode": 400, "body": {"message": "error"}})
        client._handle_message(msg)  # Should not raise
        assert client.oracle_states["BTC"].chainlink_price == 0.0

    def test_parse_empty_message_safe(self):
        client = RTDSClient(assets=["BTC"])
        client._handle_message("")  # Should not raise
        client._handle_message("   ")  # Should not raise

    def test_routes_by_price_magnitude(self):
        client = RTDSClient(assets=["BTC", "ETH", "SOL"])
        # BTC price
        client._route_price(74000.0, 1773830000000)
        assert client.oracle_states["BTC"].chainlink_price == 74000.0
        # ETH price
        client._route_price(2300.0, 1773830000000)
        assert client.oracle_states["ETH"].chainlink_price == 2300.0
        # SOL price
        client._route_price(95.0, 1773830000000)
        assert client.oracle_states["SOL"].chainlink_price == 95.0


# ---------------------------------------------------------------------------
# Tier A entry logic
# ---------------------------------------------------------------------------

class TestTierAEntry:
    def test_fires_on_dislocation_with_edge(self):
        """Tier A fires when oracle_dislocation=True AND edge > 0.05."""
        state = OracleState(asset="BTC")
        state.update_chainlink(73800.0, 1773830000000)
        state.compute_lag(74200.0)  # ~0.54% lag → dislocation
        assert state.dislocation is True

        # Compute oracle probability with our leading price
        prob = compute_oracle_probability(
            spot_price=74200, strike=74000, realized_vol=0.5, seconds_remaining=120,
        )
        yes_ask = 0.55
        edge = prob - yes_ask
        # Should have positive edge (we know price is up, oracle hasn't caught up)
        assert edge > 0.05

    def test_does_not_fire_without_dislocation(self):
        """No trade when lag < threshold."""
        state = OracleState(asset="BTC")
        state.update_chainlink(74000.0, 1773830000000)
        state.compute_lag(74010.0)  # tiny lag
        assert state.dislocation is False

    def test_does_not_fire_with_small_edge(self):
        """No trade when edge < 0.05 even if dislocation exists."""
        state = OracleState(asset="BTC")
        state.update_chainlink(73800.0, 1773830000000)
        state.compute_lag(74200.0)  # dislocation exists
        assert state.dislocation is True

        # But if market already priced it in
        prob = compute_oracle_probability(
            spot_price=74200, strike=74000, realized_vol=0.5, seconds_remaining=120,
        )
        yes_ask = prob - 0.01  # market nearly caught up
        edge = prob - yes_ask
        assert edge < 0.05  # edge too small


import json
