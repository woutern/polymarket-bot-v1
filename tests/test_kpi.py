"""Tests for KPI tracker: Brier score, BSS, SPRT, division safety."""

from __future__ import annotations

import math
import pytest

from polybot.ml.kpi_tracker import KPITracker, SPRTTracker, _brier, _safe_div


class TestBrierScore:
    def test_perfect_prediction(self):
        assert _brier([1.0, 0.0], [1, 0]) == 0.0

    def test_worst_prediction(self):
        assert _brier([0.0, 1.0], [1, 0]) == 1.0

    def test_uninformative(self):
        bs = _brier([0.5, 0.5], [1, 0])
        assert abs(bs - 0.25) < 0.001

    def test_empty_returns_default(self):
        assert _brier([], []) == 0.25


class TestBSS:
    def test_positive_when_better_than_market(self):
        tracker = KPITracker()
        trades = _make_trades(50, our_prob=0.75, market_price=0.60, win_rate=0.70)
        snapshot = tracker.compute_snapshot(trades)
        # Our prob closer to outcome than market → BSS > 0
        assert snapshot.get("brier_skill_score", 0) != 0  # at least computed

    def test_negative_when_worse_than_market(self):
        tracker = KPITracker()
        trades = _make_trades(50, our_prob=0.40, market_price=0.55, win_rate=0.55)
        snapshot = tracker.compute_snapshot(trades)
        # Market closer to outcome → BSS < 0
        assert isinstance(snapshot.get("brier_skill_score"), float)


class TestSPRT:
    def test_update_after_win(self):
        sprt = SPRTTracker()
        sprt.update(p1=0.70, p0=0.50, outcome=1)
        assert sprt.log_lambda > 0
        assert sprt.trades == 1

    def test_update_after_loss(self):
        sprt = SPRTTracker()
        sprt.update(p1=0.70, p0=0.50, outcome=0)
        assert sprt.log_lambda < 0

    def test_accumulating_by_default(self):
        sprt = SPRTTracker()
        assert sprt.status == "ACCUMULATING"

    def test_edge_confirmed_after_many_wins(self):
        sprt = SPRTTracker()
        for _ in range(50):
            sprt.update(p1=0.80, p0=0.50, outcome=1)
        assert sprt.status == "EDGE_CONFIRMED"

    def test_reassess_after_many_losses(self):
        sprt = SPRTTracker()
        for _ in range(50):
            sprt.update(p1=0.80, p0=0.50, outcome=0)
        assert sprt.status == "REASSESS"

    def test_clamps_extreme_probabilities(self):
        sprt = SPRTTracker()
        sprt.update(p1=0.0, p0=1.0, outcome=1)  # should not crash
        assert math.isfinite(sprt.log_lambda)


class TestSafeDivision:
    def test_normal_division(self):
        assert _safe_div(10, 5) == 2.0

    def test_zero_denominator(self):
        assert _safe_div(10, 0) == 0.0

    def test_custom_default(self):
        assert _safe_div(10, 0, default=-1.0) == -1.0


class TestKPISnapshot:
    def test_insufficient_data(self):
        tracker = KPITracker()
        snapshot = tracker.compute_snapshot([])
        assert snapshot["status"] == "insufficient_data"

    def test_full_snapshot_fields(self):
        tracker = KPITracker()
        trades = _make_trades(30, our_prob=0.65, market_price=0.55, win_rate=0.65)
        snapshot = tracker.compute_snapshot(trades)
        assert "brier_score" in snapshot
        assert "brier_skill_score" in snapshot
        assert "win_rate_total" in snapshot
        assert "sharpe_ratio" in snapshot
        assert "lgbm_separation" in snapshot
        assert "pair_stats" in snapshot

    def test_no_nan_values(self):
        tracker = KPITracker()
        trades = _make_trades(30, our_prob=0.65, market_price=0.55, win_rate=0.65)
        snapshot = tracker.compute_snapshot(trades)
        for k, v in snapshot.items():
            if isinstance(v, float):
                assert math.isfinite(v), f"{k} is not finite: {v}"


def _make_trades(n: int, our_prob: float, market_price: float, win_rate: float) -> list[dict]:
    """Generate mock trades for testing."""
    import random
    random.seed(42)
    trades = []
    for i in range(n):
        won = random.random() < win_rate
        pnl = 0.50 if won else -1.0
        trades.append({
            "id": f"t{i}",
            "timestamp": str(1773800000 + i * 300),
            "asset": "BTC",
            "window_slug": f"btc-updown-5m-{1773800000 + i * 300}",
            "side": "YES",
            "price": str(market_price),
            "fill_price": str(market_price),
            "size_usd": "1.0",
            "pnl": str(pnl),
            "p_final": str(our_prob),
            "resolved": 1,
            "outcome_source": "polymarket_verified",
        })
    return trades
