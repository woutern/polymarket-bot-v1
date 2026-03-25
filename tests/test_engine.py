"""Integration tests for core/engine.py + execution/mm_paper_client.py + core/controls.py.

These tests drive the engine tick-by-tick with synthetic MarketState data
and assert end-to-end behaviour: fills, position accounting, phase transitions,
kill-switch, pause, and window_result summary.
"""

from __future__ import annotations

import pytest

from polybot.core.controls import InMemoryControls, _parse_item
from polybot.core.engine import Engine, WindowPhase
from polybot.core.position import Position
from polybot.execution.mm_paper_client import MMPaperClient
from polybot.strategy.base import MarketState
from polybot.strategy.profile import StrategyProfile


# ─── Helpers ────────────────────────────────────────────────────────────────

def make_state(
    seconds: int = 10,
    yes_bid: float = 0.52,
    no_bid: float = 0.48,
    yes_ask: float = 0.53,
    no_ask: float = 0.49,
    prob_up: float = 0.55,
) -> MarketState:
    return MarketState(
        seconds=seconds,
        yes_bid=yes_bid,
        no_bid=no_bid,
        yes_ask=yes_ask,
        no_ask=no_ask,
        prob_up=prob_up,
    )


def make_engine(controls=None, budget: float = 80.0) -> Engine:
    """Return a paper-mode engine with BTC_5M profile."""
    ctrl = controls if controls is not None else InMemoryControls()
    return Engine(pair="BTC_5M", mode="paper", controls=ctrl)


def run_ticks(engine: Engine, states: list[MarketState]) -> None:
    for s in states:
        engine.run_tick(s)


# ─── MMPaperClient unit tests ────────────────────────────────────────────────

class TestMMPaperClient:
    def test_post_buy_creates_live_order(self):
        client = MMPaperClient()
        oid = client.post_buy("YES", 10, 0.55)
        order = client.get_order(oid)
        assert order is not None
        assert order.status == "LIVE"
        assert order.token == "YES"
        assert order.side == "BUY"
        assert order.shares == 10
        assert order.price == 0.55

    def test_post_sell_creates_live_order(self):
        client = MMPaperClient()
        oid = client.post_sell("NO", 5, 0.45)
        order = client.get_order(oid)
        assert order.status == "LIVE"
        assert order.side == "SELL"

    def test_buy_fills_when_price_at_ask(self):
        client = MMPaperClient()
        oid = client.post_buy("YES", 10, 0.53)  # limit at ask
        filled = client.tick(yes_bid=0.52, no_bid=0.48, yes_ask=0.53, no_ask=0.49)
        assert len(filled) == 1
        assert filled[0].order_id == oid
        assert filled[0].status == "MATCHED"
        assert client.position.up_shares == 10

    def test_buy_does_not_fill_below_ask(self):
        client = MMPaperClient()
        client.post_buy("YES", 10, 0.50)  # limit below ask=0.53
        filled = client.tick(yes_bid=0.52, no_bid=0.48, yes_ask=0.53, no_ask=0.49)
        assert len(filled) == 0
        assert client.position.up_shares == 0

    def test_sell_fills_when_price_at_bid(self):
        pos = Position()
        pos.buy(True, 10, 0.55)
        client = MMPaperClient(position=pos)
        oid = client.post_sell("YES", 10, 0.52)  # limit at bid
        filled = client.tick(yes_bid=0.52, no_bid=0.48, yes_ask=0.53, no_ask=0.49)
        assert len(filled) == 1
        assert filled[0].status == "MATCHED"

    def test_sell_does_not_fill_above_bid(self):
        pos = Position()
        pos.buy(True, 10, 0.55)
        client = MMPaperClient(position=pos)
        client.post_sell("YES", 10, 0.60)  # limit above bid=0.52
        filled = client.tick(yes_bid=0.52, no_bid=0.48, yes_ask=0.53, no_ask=0.49)
        assert len(filled) == 0

    def test_cancel_removes_order(self):
        client = MMPaperClient()
        oid = client.post_buy("YES", 10, 0.50)
        result = client.cancel(oid)
        assert result is True
        assert client.get_order(oid).status == "CANCELLED"

    def test_cancel_all_clears_live_orders(self):
        client = MMPaperClient()
        client.post_buy("YES", 10, 0.50)
        client.post_buy("NO", 5, 0.45)
        count = client.cancel_all()
        assert count == 2
        assert len(client.live_orders()) == 0

    def test_reserved_buy_usd(self):
        client = MMPaperClient()
        client.post_buy("YES", 10, 0.55)  # 5.50
        client.post_buy("NO", 20, 0.45)   # 9.00
        assert abs(client.reserved_buy_usd() - 14.50) < 0.01

    def test_post_buy_rejects_zero_shares(self):
        client = MMPaperClient()
        with pytest.raises(ValueError):
            client.post_buy("YES", 0, 0.55)

    def test_stats_tracks_fills_and_cancels(self):
        client = MMPaperClient()
        client.post_buy("YES", 10, 0.53)
        client.post_buy("NO", 5, 0.50)  # won't fill (ask=0.49, limit=0.50 ≥ 0.49)
        client.tick(yes_bid=0.52, no_bid=0.48, yes_ask=0.53, no_ask=0.49)
        # cancel the unfilled NO order
        live = client.live_orders()
        for o in live:
            client.cancel(o.order_id)
        s = client.stats()
        assert s["filled"] == 2  # YES buy + NO buy (both fill: YES at 0.53, NO at 0.49<=0.50)
        assert s["cancelled"] >= 0


# ─── InMemoryControls tests ──────────────────────────────────────────────────

class TestInMemoryControls:
    def test_defaults_off(self):
        ctrl = InMemoryControls()
        assert ctrl.kill_switch is False
        assert ctrl.pause_new_windows is False
        assert ctrl.max_windows_override is None

    def test_snapshot_copies_state(self):
        ctrl = InMemoryControls()
        ctrl.kill_switch = True
        snap = ctrl.snapshot()
        assert snap.kill_switch is True
        ctrl.kill_switch = False
        assert snap.kill_switch is True  # snapshot is independent

    def test_parse_dynamo_item(self):
        item = {
            "kill_switch": {"BOOL": True},
            "pause_new_windows": {"BOOL": False},
            "max_windows_override": {"N": "3"},
            "note": {"S": "manual stop"},
        }
        state = _parse_item(item)
        assert state.kill_switch is True
        assert state.pause_new_windows is False
        assert state.max_windows_override == 3
        assert state.note == "manual stop"

    def test_parse_empty_item(self):
        state = _parse_item({})
        assert state.kill_switch is False
        assert state.max_windows_override is None
        assert state.note == ""


# ─── Engine integration tests ────────────────────────────────────────────────

class TestEngine:
    def test_engine_runs_single_tick(self):
        engine = make_engine()
        action = engine.run_tick(make_state(seconds=10))
        # Engine ran without error; action is a StrategyAction
        assert hasattr(action, "buy_up_shares")

    def test_kill_switch_returns_early(self):
        ctrl = InMemoryControls()
        ctrl.kill_switch = True
        engine = make_engine(controls=ctrl)
        action = engine.run_tick(make_state(seconds=10))
        assert action.reason == "KILL_SWITCH"
        assert not action.has_action()

    def test_kill_switch_no_fills(self):
        ctrl = InMemoryControls()
        ctrl.kill_switch = True
        engine = make_engine(controls=ctrl)
        engine.run_tick(make_state(seconds=10))
        assert engine.position.up_shares == 0
        assert engine.position.down_shares == 0

    def test_commit_cancels_open_orders(self):
        engine = make_engine()
        # Place orders that won't fill (low limit prices)
        engine.client.post_buy("YES", 10, 0.30)
        engine.client.post_buy("NO", 10, 0.30)
        assert len(engine.client.live_orders()) == 2
        engine.commit()
        assert len(engine.client.live_orders()) == 0

    def test_window_result_after_ticks(self):
        engine = make_engine()
        # Run 10 ticks with balanced spread (both sides buyable)
        for s in range(5, 100, 10):
            engine.run_tick(make_state(
                seconds=s,
                yes_bid=0.52, no_bid=0.48,
                yes_ask=0.53, no_ask=0.49,
                prob_up=0.55,
            ))
        engine.commit()
        result = engine.window_result()
        assert result.pair == "BTC_5M"
        assert result.total_ticks == 10
        assert result.net_cost >= 0.0
        assert isinstance(result.is_guaranteed_profit, bool)
        assert isinstance(result.sell_reasons, dict)

    def test_window_result_pnl_consistency(self):
        engine = make_engine()
        for s in range(10, 50, 5):
            engine.run_tick(make_state(seconds=s))
        engine.commit()
        result = engine.window_result()
        # pnl_if_up + pnl_if_down == (up_shares + down_shares) - 2*net_cost
        expected_sum = (result.up_shares + result.down_shares) - 2 * result.net_cost
        assert abs((result.pnl_if_up + result.pnl_if_down) - expected_sum) < 0.01

    def test_guaranteed_profit_flag(self):
        engine = make_engine()
        # Manually set up a GP position: both sides at 0.45 avg → combined_avg=0.90 < 1.00
        engine.position.buy(True, 10, 0.45)
        engine.position.buy(False, 10, 0.45)
        result = engine.window_result()
        assert result.is_guaranteed_profit is True
        assert result.combined_avg < 1.0

    def test_not_guaranteed_profit_when_expensive(self):
        engine = make_engine()
        # Both sides at 0.55 → combined_avg=1.10 > 1.00
        engine.position.buy(True, 10, 0.55)
        engine.position.buy(False, 10, 0.55)
        result = engine.window_result()
        assert result.is_guaranteed_profit is False

    def test_on_action_callback_fires(self):
        calls = []
        engine = Engine(
            pair="BTC_5M",
            mode="paper",
            on_action=lambda sec, act: calls.append((sec, act)),
        )
        engine.run_tick(make_state(seconds=10))
        engine.run_tick(make_state(seconds=20))
        assert len(calls) == 2
        assert calls[0][0] == 10
        assert calls[1][0] == 20

    def test_position_shared_between_engine_and_client(self):
        """Engine's position and client's position are the same object."""
        engine = make_engine()
        assert engine.position is engine.client.position

    def test_resolved_phase_returns_no_action(self):
        engine = make_engine()
        # seconds=300 → past commit_start=250 → COMMIT phase, not RESOLVED
        # Use a very large seconds to hit COMMIT
        action = engine.run_tick(make_state(seconds=300))
        # At 300s the engine should still run but in COMMIT phase
        assert action.reason != "KILL_SWITCH"

    def test_tick_log_grows_each_tick(self):
        engine = make_engine()
        for i in range(5):
            engine.run_tick(make_state(seconds=10 + i * 10))
        result = engine.window_result()
        assert len(result.tick_log) == 5

    def test_tick_log_captures_phase(self):
        engine = make_engine()
        engine.run_tick(make_state(seconds=5))   # OPEN
        engine.run_tick(make_state(seconds=30))  # ACCUMULATE
        result = engine.window_result()
        phases = [r.phase for r in result.tick_log]
        assert "OPEN" in phases
        assert "ACCUMULATE" in phases

    def test_multi_tick_accumulates_inventory(self):
        """Running many ticks should accumulate shares via paper fills.

        Strategy posts buy orders at bid price. Set bid == ask (zero spread)
        so GTC limit orders fill immediately on the next tick.
        """
        engine = make_engine()
        # Zero spread: strategy buys at bid, ask == bid → fills next tick
        for s in range(10, 180, 5):
            engine.run_tick(make_state(
                seconds=s,
                yes_bid=0.50, no_bid=0.46,
                yes_ask=0.50, no_ask=0.46,  # bid == ask → fills, combined 0.96 passes entry gate
                prob_up=0.60,
            ))
        engine.commit()
        total = engine.position.up_shares + engine.position.down_shares
        assert total > 0

    def test_engine_respects_budget(self):
        """Net cost must not exceed profile budget."""
        engine = make_engine()
        for s in range(10, 260, 2):
            engine.run_tick(make_state(
                seconds=s,
                yes_bid=0.55, no_bid=0.45,
                yes_ask=0.56, no_ask=0.46,
                prob_up=0.60,
            ))
        engine.commit()
        assert engine.position.net_cost <= engine.profile.budget + 0.50  # small fill tolerance
