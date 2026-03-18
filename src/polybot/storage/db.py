"""SQLite storage for trades and windows.

Also mirrors writes to DynamoDB for dashboard access.
"""

from __future__ import annotations

import aiosqlite

DB_PATH = "polybot.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id TEXT PRIMARY KEY,
    timestamp REAL,
    window_slug TEXT,
    source TEXT,
    direction TEXT,
    side TEXT,
    price REAL,
    size_usd REAL,
    fill_price REAL,
    pnl REAL,
    resolved INTEGER DEFAULT 0,
    mode TEXT DEFAULT 'paper',
    asset TEXT,
    p_bayesian REAL DEFAULT 0,
    p_ai REAL,
    p_final REAL DEFAULT 0,
    pct_move REAL DEFAULT 0,
    seconds_remaining REAL DEFAULT 0,
    ev REAL DEFAULT 0,
    outcome_source TEXT DEFAULT 'coinbase_inferred',
    polymarket_winner TEXT,
    correct_prediction INTEGER
);

CREATE TABLE IF NOT EXISTS windows (
    slug TEXT PRIMARY KEY,
    open_ts INTEGER,
    close_ts INTEGER,
    open_price REAL,
    close_price REAL,
    direction TEXT,
    condition_id TEXT,
    asset TEXT,
    signals_fired INTEGER DEFAULT 0,
    trades_executed INTEGER DEFAULT 0,
    rejection_reason TEXT DEFAULT '',
    polymarket_winner TEXT
);
"""

# Migration: add new columns to existing tables if they don't exist
_MIGRATIONS = [
    "ALTER TABLE trades ADD COLUMN p_bayesian REAL DEFAULT 0",
    "ALTER TABLE trades ADD COLUMN p_ai REAL",
    "ALTER TABLE trades ADD COLUMN p_final REAL DEFAULT 0",
    "ALTER TABLE trades ADD COLUMN pct_move REAL DEFAULT 0",
    "ALTER TABLE trades ADD COLUMN seconds_remaining REAL DEFAULT 0",
    "ALTER TABLE trades ADD COLUMN ev REAL DEFAULT 0",
    "ALTER TABLE trades ADD COLUMN outcome_source TEXT DEFAULT 'coinbase_inferred'",
    "ALTER TABLE trades ADD COLUMN polymarket_winner TEXT",
    "ALTER TABLE trades ADD COLUMN correct_prediction INTEGER",
    "ALTER TABLE windows ADD COLUMN asset TEXT",
    "ALTER TABLE windows ADD COLUMN signals_fired INTEGER DEFAULT 0",
    "ALTER TABLE windows ADD COLUMN trades_executed INTEGER DEFAULT 0",
    "ALTER TABLE windows ADD COLUMN rejection_reason TEXT DEFAULT ''",
    "ALTER TABLE windows ADD COLUMN polymarket_winner TEXT",
]


class Database:
    def __init__(self, path: str = DB_PATH):
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._dynamo = None  # injected after construction if needed

    def attach_dynamo(self, dynamo):
        self._dynamo = dynamo

    async def connect(self):
        self._db = await aiosqlite.connect(self.path)
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        # Run migrations (ignore errors for columns that already exist)
        for sql in _MIGRATIONS:
            try:
                await self._db.execute(sql)
                await self._db.commit()
            except Exception:
                pass  # Column already exists

    async def close(self):
        if self._db:
            await self._db.close()

    async def insert_trade(self, trade: dict):
        await self._db.execute(
            """INSERT OR REPLACE INTO trades
               (id, timestamp, window_slug, source, direction, side, price, size_usd, fill_price,
                pnl, resolved, mode, asset, p_bayesian, p_ai, p_final, pct_move, seconds_remaining,
                ev, outcome_source, polymarket_winner, correct_prediction)
               VALUES (:id, :timestamp, :window_slug, :source, :direction, :side, :price, :size_usd,
                       :fill_price, :pnl, :resolved, :mode, :asset, :p_bayesian, :p_ai, :p_final,
                       :pct_move, :seconds_remaining, :ev, :outcome_source, :polymarket_winner,
                       :correct_prediction)""",
            {
                "mode": "paper",
                "asset": "",
                "p_bayesian": 0.0,
                "p_ai": None,
                "p_final": 0.0,
                "pct_move": 0.0,
                "seconds_remaining": 0.0,
                "ev": 0.0,
                "outcome_source": "coinbase_inferred",
                "polymarket_winner": None,
                "correct_prediction": None,
                **trade,
            },
        )
        await self._db.commit()
        if self._dynamo:
            try:
                self._dynamo.put_trade(trade)
            except Exception:
                pass  # DynamoDB write failure is non-fatal

    async def insert_window(self, window: dict):
        await self._db.execute(
            """INSERT OR REPLACE INTO windows
               (slug, open_ts, close_ts, open_price, close_price, direction, condition_id,
                asset, signals_fired, trades_executed, rejection_reason, polymarket_winner)
               VALUES (:slug, :open_ts, :close_ts, :open_price, :close_price, :direction,
                       :condition_id, :asset, :signals_fired, :trades_executed,
                       :rejection_reason, :polymarket_winner)""",
            {
                "condition_id": "",
                "asset": "",
                "signals_fired": 0,
                "trades_executed": 0,
                "rejection_reason": "",
                "polymarket_winner": None,
                **window,
            },
        )
        await self._db.commit()

    async def get_trades(self, window_slug: str | None = None, limit: int = 100) -> list[dict]:
        if window_slug:
            cursor = await self._db.execute(
                "SELECT * FROM trades WHERE window_slug = ? ORDER BY timestamp DESC LIMIT ?",
                (window_slug, limit),
            )
        else:
            cursor = await self._db.execute(
                "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
            )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    async def update_trade_outcome(
        self,
        trade_id: str,
        polymarket_winner: str,
        correct_prediction: bool,
        outcome_source: str = "polymarket_verified",
    ):
        """Update a trade record with verified Polymarket outcome."""
        await self._db.execute(
            """UPDATE trades
               SET polymarket_winner = ?, correct_prediction = ?, outcome_source = ?
               WHERE id = ?""",
            (polymarket_winner, int(correct_prediction), outcome_source, trade_id),
        )
        await self._db.commit()

    async def get_daily_stats(self, days: int = 30) -> list[dict]:
        # daily_stats table removed; return empty for backwards compat
        return []
