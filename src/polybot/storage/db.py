"""SQLite storage for trades, windows, and daily stats.

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
    resolved INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS windows (
    slug TEXT PRIMARY KEY,
    open_ts INTEGER,
    close_ts INTEGER,
    open_price REAL,
    close_price REAL,
    direction TEXT,
    condition_id TEXT
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    trades INTEGER,
    wins INTEGER,
    losses INTEGER,
    gross_pnl REAL,
    net_pnl REAL,
    max_drawdown REAL,
    bankroll_start REAL,
    bankroll_end REAL
);
"""


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

    async def close(self):
        if self._db:
            await self._db.close()

    async def insert_trade(self, trade: dict):
        await self._db.execute(
            """INSERT OR REPLACE INTO trades
               (id, timestamp, window_slug, source, direction, side, price, size_usd, fill_price, pnl, resolved)
               VALUES (:id, :timestamp, :window_slug, :source, :direction, :side, :price, :size_usd, :fill_price, :pnl, :resolved)""",
            trade,
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
               (slug, open_ts, close_ts, open_price, close_price, direction, condition_id)
               VALUES (:slug, :open_ts, :close_ts, :open_price, :close_price, :direction, :condition_id)""",
            window,
        )
        await self._db.commit()

    async def insert_daily_stats(self, stats: dict):
        await self._db.execute(
            """INSERT OR REPLACE INTO daily_stats
               (date, trades, wins, losses, gross_pnl, net_pnl, max_drawdown, bankroll_start, bankroll_end)
               VALUES (:date, :trades, :wins, :losses, :gross_pnl, :net_pnl, :max_drawdown, :bankroll_start, :bankroll_end)""",
            stats,
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

    async def get_daily_stats(self, days: int = 30) -> list[dict]:
        cursor = await self._db.execute(
            "SELECT * FROM daily_stats ORDER BY date DESC LIMIT ?", (days,)
        )
        rows = await cursor.fetchall()
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, row)) for row in rows]
