"""Tests for storage/mm_store.py — InMemoryMMStore (no AWS needed)."""

from __future__ import annotations

import pytest

from polybot.core.engine import Engine, WindowResult
from polybot.core.engine import TickRecord
from polybot.storage.mm_store import InMemoryMMStore
from polybot.strategy.base import MarketState, StrategyAction


# ─── Helpers ────────────────────────────────────────────────────────────────

def make_tick_record(seconds: int = 10) -> TickRecord:
    return TickRecord(
        seconds=seconds,
        phase="ACCUMULATE",
        action=StrategyAction(buy_up_shares=5, buy_up_price=0.55, reason="OPEN"),
        position_snapshot={"up_shares": 5, "down_shares": 0, "net_cost": 2.75},
        fills=[],
    )


def make_window_result() -> tuple[str, WindowResult]:
    engine = Engine(pair="BTC_5M", mode="paper")
    engine.position.buy(True, 10, 0.50)
    engine.position.buy(False, 10, 0.45)
    engine.commit()
    return "w_001", engine.window_result()


# ─── Tick log ────────────────────────────────────────────────────────────────

class TestTickLog:
    def test_put_and_get_tick(self):
        store = InMemoryMMStore()
        store.put_tick("w_001", make_tick_record(10))
        ticks = store.get_ticks("w_001")
        assert len(ticks) == 1
        assert ticks[0]["seconds"] == 10
        assert ticks[0]["phase"] == "ACCUMULATE"

    def test_multiple_ticks_ordered(self):
        store = InMemoryMMStore()
        for s in [10, 20, 30]:
            store.put_tick("w_001", make_tick_record(s))
        ticks = store.get_ticks("w_001")
        assert len(ticks) == 3
        assert [t["seconds"] for t in ticks] == [10, 20, 30]

    def test_ticks_isolated_by_window(self):
        store = InMemoryMMStore()
        store.put_tick("w_001", make_tick_record(10))
        store.put_tick("w_002", make_tick_record(20))
        assert store.tick_count("w_001") == 1
        assert store.tick_count("w_002") == 1

    def test_get_ticks_empty_window(self):
        store = InMemoryMMStore()
        assert store.get_ticks("nonexistent") == []

    def test_tick_count(self):
        store = InMemoryMMStore()
        for s in range(0, 50, 5):
            store.put_tick("w_001", make_tick_record(s))
        assert store.tick_count("w_001") == 10

    def test_action_summary_stored(self):
        store = InMemoryMMStore()
        store.put_tick("w_001", make_tick_record(10))
        tick = store.get_ticks("w_001")[0]
        assert tick["action"]["buy_up"] == 5
        assert tick["action"]["buy_up_price"] == 0.55
        assert tick["action"]["reason"] == "OPEN"

    def test_position_snapshot_stored(self):
        store = InMemoryMMStore()
        store.put_tick("w_001", make_tick_record(10))
        tick = store.get_ticks("w_001")[0]
        assert tick["position"]["up_shares"] == 5
        assert tick["position"]["net_cost"] == 2.75


# ─── Window log ──────────────────────────────────────────────────────────────

class TestWindowLog:
    def test_put_and_get_window(self):
        store = InMemoryMMStore()
        wid, result = make_window_result()
        store.put_window(wid, result)
        item = store.get_window(wid)
        assert item is not None
        assert item["pair"] == "BTC_5M"
        assert item["up_shares"] == 10
        assert item["down_shares"] == 10

    def test_combined_avg_stored(self):
        store = InMemoryMMStore()
        wid, result = make_window_result()
        store.put_window(wid, result)
        item = store.get_window(wid)
        assert abs(item["combined_avg"] - result.combined_avg) < 0.0001

    def test_is_gp_flag_stored(self):
        store = InMemoryMMStore()
        wid, result = make_window_result()
        store.put_window(wid, result)
        item = store.get_window(wid)
        assert "is_gp" in item
        # combined_avg = (0.50 + 0.45) = 0.475 < 1.00 → GP
        assert item["is_gp"] is True

    def test_pnl_fields_stored(self):
        store = InMemoryMMStore()
        wid, result = make_window_result()
        store.put_window(wid, result)
        item = store.get_window(wid)
        assert "pnl_if_up" in item
        assert "pnl_if_down" in item

    def test_sell_reasons_stored(self):
        store = InMemoryMMStore()
        wid, result = make_window_result()
        store.put_window(wid, result)
        item = store.get_window(wid)
        assert isinstance(item["sell_reasons"], dict)

    def test_fill_stats_stored(self):
        store = InMemoryMMStore()
        wid, result = make_window_result()
        store.put_window(wid, result)
        item = store.get_window(wid)
        assert isinstance(item["fill_stats"], dict)

    def test_get_window_missing_returns_none(self):
        store = InMemoryMMStore()
        assert store.get_window("missing") is None

    def test_get_recent_windows(self):
        store = InMemoryMMStore()
        for i in range(5):
            wid, result = make_window_result()
            store.put_window(f"w_{i:03d}", result)
        windows = store.get_recent_windows(limit=3)
        assert len(windows) == 3

    def test_all_window_ids(self):
        store = InMemoryMMStore()
        for i in range(4):
            wid, result = make_window_result()
            store.put_window(f"w_{i:03d}", result)
        ids = store.all_window_ids()
        assert len(ids) == 4
        assert "w_000" in ids


# ─── Position store ──────────────────────────────────────────────────────────

class TestPositionStore:
    def test_put_and_get_position(self):
        store = InMemoryMMStore()
        snap = {"up_shares": 10, "down_shares": 8, "net_cost": 8.20}
        store.put_position("w_001", snap)
        result = store.get_position("w_001")
        assert result is not None
        assert result["up_shares"] == 10
        assert result["net_cost"] == 8.20

    def test_position_overwritten_on_update(self):
        store = InMemoryMMStore()
        store.put_position("w_001", {"up_shares": 5, "net_cost": 2.75})
        store.put_position("w_001", {"up_shares": 15, "net_cost": 8.00})
        result = store.get_position("w_001")
        assert result["up_shares"] == 15
        assert result["net_cost"] == 8.00

    def test_position_includes_window_id(self):
        store = InMemoryMMStore()
        store.put_position("w_001", {"up_shares": 5})
        result = store.get_position("w_001")
        assert result["window_id"] == "w_001"

    def test_get_position_missing_returns_none(self):
        store = InMemoryMMStore()
        assert store.get_position("nonexistent") is None

    def test_positions_isolated_by_window(self):
        store = InMemoryMMStore()
        store.put_position("w_001", {"up_shares": 10})
        store.put_position("w_002", {"up_shares": 20})
        assert store.get_position("w_001")["up_shares"] == 10
        assert store.get_position("w_002")["up_shares"] == 20


# ─── End-to-end: engine → store ──────────────────────────────────────────────

class TestEngineToStore:
    def test_engine_window_result_to_store(self):
        """Full flow: run engine ticks, store result, retrieve and verify."""
        store = InMemoryMMStore()
        engine = Engine(pair="BTC_5M", mode="paper")
        window_id = "e2e_001"

        for s in range(10, 60, 10):
            state = MarketState(
                seconds=s, yes_bid=0.55, no_bid=0.45,
                yes_ask=0.55, no_ask=0.45, prob_up=0.60,
            )
            action = engine.run_tick(state)
            tick = engine.window_result().tick_log[-1]
            store.put_tick(window_id, tick)
            pos_snap = engine._position_snapshot()
            store.put_position(window_id, pos_snap)

        engine.commit()
        result = engine.window_result()
        store.put_window(window_id, result)

        # Verify window stored correctly
        item = store.get_window(window_id)
        assert item["pair"] == "BTC_5M"
        assert item["total_ticks"] == 5

        # Verify ticks stored
        assert store.tick_count(window_id) == 5

        # Verify position stored
        pos = store.get_position(window_id)
        assert pos is not None
        assert "up_shares" in pos
