"""Tests for SQLite storage: trades, windows, outcome updates."""

from __future__ import annotations

import pytest

from polybot.storage.db import Database


@pytest.fixture
async def db(tmp_path):
    """Create a temporary in-memory-like DB for testing."""
    db_path = str(tmp_path / "test.db")
    d = Database(path=db_path)
    await d.connect()
    yield d
    await d.close()


# ---------------------------------------------------------------------------
# insert_trade / get_trades
# ---------------------------------------------------------------------------


class TestTradeStorage:
    async def test_insert_and_retrieve_trade(self, db):
        trade = {
            "id": "t1",
            "timestamp": 1000.0,
            "window_slug": "btc-updown-5m-100",
            "source": "directional",
            "direction": "up",
            "side": "YES",
            "price": 0.55,
            "size_usd": 1.0,
            "fill_price": 0.55,
            "pnl": None,
            "resolved": 0,
            "mode": "live",
            "asset": "BTC",
            "p_bayesian": 0.78,
            "p_ai": 0.82,
            "p_final": 0.79,
            "pct_move": 0.12,
            "seconds_remaining": 25.0,
            "ev": 0.33,
            "outcome_source": "coinbase_inferred",
            "polymarket_winner": None,
            "correct_prediction": None,
        }
        await db.insert_trade(trade)
        rows = await db.get_trades(window_slug="btc-updown-5m-100")
        assert len(rows) == 1
        assert rows[0]["id"] == "t1"
        assert rows[0]["p_bayesian"] == 0.78
        assert rows[0]["p_ai"] == 0.82
        assert rows[0]["p_final"] == 0.79
        assert rows[0]["pct_move"] == 0.12
        assert rows[0]["ev"] == 0.33
        assert rows[0]["outcome_source"] == "coinbase_inferred"

    async def test_new_fields_default_when_missing(self, db):
        """Older-format trade dicts (without new fields) still insert with defaults."""
        trade = {
            "id": "t2",
            "timestamp": 2000.0,
            "window_slug": "eth-updown-5m-200",
            "source": "directional",
            "direction": "down",
            "side": "NO",
            "price": 0.45,
            "size_usd": 1.0,
            "fill_price": 0.45,
            "pnl": 0.55,
            "resolved": 1,
            "mode": "paper",
            "asset": "ETH",
        }
        await db.insert_trade(trade)
        rows = await db.get_trades(window_slug="eth-updown-5m-200")
        assert len(rows) == 1
        assert rows[0]["p_bayesian"] == 0.0  # default
        assert rows[0]["p_ai"] is None  # default
        assert rows[0]["outcome_source"] == "coinbase_inferred"  # default

    async def test_upsert_trade_on_resolve(self, db):
        """INSERT OR REPLACE updates the trade when resolved."""
        trade = {
            "id": "t3",
            "timestamp": 3000.0,
            "window_slug": "sol-updown-5m-300",
            "source": "directional",
            "direction": "up",
            "side": "YES",
            "price": 0.60,
            "size_usd": 1.0,
            "fill_price": 0.60,
            "pnl": None,
            "resolved": 0,
            "mode": "live",
            "asset": "SOL",
        }
        await db.insert_trade(trade)

        # Resolve the trade
        trade["pnl"] = 0.67
        trade["resolved"] = 1
        await db.insert_trade(trade)

        rows = await db.get_trades(window_slug="sol-updown-5m-300")
        assert len(rows) == 1
        assert rows[0]["pnl"] == 0.67
        assert rows[0]["resolved"] == 1

    async def test_get_trades_limit(self, db):
        for i in range(5):
            await db.insert_trade({
                "id": f"lim{i}",
                "timestamp": float(i),
                "window_slug": f"btc-5m-{i}",
                "source": "directional",
                "direction": "up",
                "side": "YES",
                "price": 0.50,
                "size_usd": 1.0,
                "fill_price": 0.50,
                "pnl": None,
                "resolved": 0,
                "mode": "paper",
                "asset": "BTC",
            })
        rows = await db.get_trades(limit=3)
        assert len(rows) == 3


# ---------------------------------------------------------------------------
# update_trade_outcome
# ---------------------------------------------------------------------------


class TestUpdateTradeOutcome:
    async def test_outcome_update_sets_fields(self, db):
        trade = {
            "id": "out1",
            "timestamp": 5000.0,
            "window_slug": "btc-5m-500",
            "source": "directional",
            "direction": "up",
            "side": "YES",
            "price": 0.55,
            "size_usd": 1.0,
            "fill_price": 0.55,
            "pnl": 0.82,
            "resolved": 1,
            "mode": "live",
            "asset": "BTC",
            "outcome_source": "coinbase_inferred",
        }
        await db.insert_trade(trade)

        await db.update_trade_outcome(
            trade_id="out1",
            polymarket_winner="YES",
            correct_prediction=True,
            outcome_source="polymarket_verified",
        )

        rows = await db.get_trades(window_slug="btc-5m-500")
        assert rows[0]["outcome_source"] == "polymarket_verified"
        assert rows[0]["polymarket_winner"] == "YES"
        assert rows[0]["correct_prediction"] == 1  # SQLite stores bool as int

    async def test_outcome_update_wrong_prediction(self, db):
        trade = {
            "id": "out2",
            "timestamp": 6000.0,
            "window_slug": "btc-5m-600",
            "source": "directional",
            "direction": "up",
            "side": "YES",
            "price": 0.55,
            "size_usd": 1.0,
            "fill_price": 0.55,
            "pnl": -1.0,
            "resolved": 1,
            "mode": "live",
            "asset": "BTC",
        }
        await db.insert_trade(trade)

        await db.update_trade_outcome(
            trade_id="out2",
            polymarket_winner="NO",
            correct_prediction=False,
        )

        rows = await db.get_trades(window_slug="btc-5m-600")
        assert rows[0]["polymarket_winner"] == "NO"
        assert rows[0]["correct_prediction"] == 0


# ---------------------------------------------------------------------------
# insert_window
# ---------------------------------------------------------------------------


class TestWindowStorage:
    async def test_insert_and_default_fields(self, db):
        window = {
            "slug": "btc-updown-5m-1000",
            "open_ts": 1000,
            "close_ts": 1300,
            "open_price": 50000.0,
            "close_price": 50050.0,
            "direction": "up",
            "condition_id": "abc",
            "asset": "BTC",
        }
        await db.insert_window(window)

        # Read back (use raw SQL since no get_windows method)
        import aiosqlite
        async with aiosqlite.connect(db.path) as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT * FROM windows WHERE slug = ?", ("btc-updown-5m-1000",)
            )
            row = dict(await cursor.fetchone())

        assert row["slug"] == "btc-updown-5m-1000"
        assert row["asset"] == "BTC"
        assert row["signals_fired"] == 0  # default
        assert row["trades_executed"] == 0  # default
        assert row["rejection_reason"] == ""  # default
        assert row["polymarket_winner"] is None  # default


# ---------------------------------------------------------------------------
# Schema migration (existing DB gets new columns)
# ---------------------------------------------------------------------------


class TestMigrations:
    async def test_connect_twice_is_idempotent(self, db):
        """Calling connect() again (re-running migrations) should not error."""
        await db.close()
        db2 = Database(path=db.path)
        await db2.connect()
        # Insert should work on the migrated DB
        await db2.insert_trade({
            "id": "mig1",
            "timestamp": 9000.0,
            "window_slug": "btc-5m-900",
            "source": "directional",
            "direction": "up",
            "side": "YES",
            "price": 0.55,
            "size_usd": 1.0,
            "fill_price": 0.55,
            "pnl": None,
            "resolved": 0,
            "mode": "paper",
            "asset": "BTC",
        })
        rows = await db2.get_trades()
        assert any(r["id"] == "mig1" for r in rows)
        await db2.close()
