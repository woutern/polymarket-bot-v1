"""Tests for dashboard API data correctness."""

from __future__ import annotations

import base64
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path):
    """Create a test client with a temp SQLite DB."""
    import sqlite3

    db_path = str(tmp_path / "test.db")

    # Create schema
    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id TEXT PRIMARY KEY, timestamp REAL, window_slug TEXT, source TEXT,
            direction TEXT, side TEXT, price REAL, size_usd REAL, fill_price REAL,
            pnl REAL, resolved INTEGER DEFAULT 0, mode TEXT DEFAULT 'live', asset TEXT,
            p_bayesian REAL DEFAULT 0, p_ai REAL, p_final REAL DEFAULT 0,
            pct_move REAL DEFAULT 0, seconds_remaining REAL DEFAULT 0, ev REAL DEFAULT 0,
            outcome_source TEXT DEFAULT 'coinbase_inferred', polymarket_winner TEXT,
            correct_prediction INTEGER
        );
        CREATE TABLE IF NOT EXISTS windows (
            slug TEXT PRIMARY KEY, open_ts INTEGER, close_ts INTEGER,
            open_price REAL, close_price REAL, direction TEXT, condition_id TEXT,
            asset TEXT, signals_fired INTEGER DEFAULT 0, trades_executed INTEGER DEFAULT 0,
            rejection_reason TEXT DEFAULT '', polymarket_winner TEXT
        );
    """)

    # Insert sample data
    conn.execute("""
        INSERT INTO trades VALUES ('t1', 1000.0, 'btc-updown-5m-100', 'directional',
        'up', 'YES', 0.55, 1.0, 0.55, 0.82, 1, 'live', 'BTC',
        0.78, 0.82, 0.79, 0.12, 25.0, 0.33, 'polymarket_verified', 'YES', 1)
    """)
    conn.execute("""
        INSERT INTO trades VALUES ('t2', 2000.0, 'eth-updown-5m-200', 'directional',
        'down', 'NO', 0.45, 1.0, 0.45, -1.0, 1, 'live', 'ETH',
        0.72, NULL, 0.72, 0.15, 30.0, 0.20, 'coinbase_inferred', NULL, NULL)
    """)
    conn.execute("""
        INSERT INTO trades VALUES ('t3', 3000.0, 'sol-updown-5m-300', 'directional',
        'up', 'YES', 0.60, 1.0, 0.60, NULL, 0, 'live', 'SOL',
        0.80, 0.85, 0.82, 0.20, 40.0, 0.37, 'coinbase_inferred', NULL, NULL)
    """)
    conn.execute("""
        INSERT INTO windows VALUES ('btc-updown-5m-100', 100, 400, 50000.0, 50050.0,
        'up', 'cid1', 'BTC', 1, 1, '', 'YES')
    """)
    conn.execute("""
        INSERT INTO windows VALUES ('eth-updown-5m-200', 200, 500, 2000.0, 1995.0,
        'down', 'cid2', 'ETH', 2, 1, '', NULL)
    """)
    conn.commit()
    conn.close()

    # Patch the dashboard to use our test DB
    import scripts.dashboard as dash
    orig_db_path = dash._DB_PATH
    orig_use_dynamo = dash._USE_DYNAMO
    orig_use_sqlite = dash._USE_SQLITE
    orig_mode = dash._TRADE_MODE
    orig_bankroll = dash._BANKROLL

    dash._DB_PATH = db_path
    dash._USE_DYNAMO = False
    dash._USE_SQLITE = True
    dash._TRADE_MODE = "live"
    dash._BANKROLL = 43.0

    with TestClient(dash.app) as c:
        # Basic auth header
        creds = base64.b64encode(b"admin:polybot2026").decode()
        c.headers["Authorization"] = f"Basic {creds}"
        yield c

    dash._DB_PATH = orig_db_path
    dash._USE_DYNAMO = orig_use_dynamo
    dash._USE_SQLITE = orig_use_sqlite
    dash._TRADE_MODE = orig_mode
    dash._BANKROLL = orig_bankroll


class TestApiData:
    def test_returns_stats(self, client):
        resp = client.get("/api/data")
        assert resp.status_code == 200
        data = resp.json()
        s = data["stats"]
        assert s["mode"] == "live"
        assert s["starting_bankroll"] == 43.0
        # Only polymarket_verified trades count as realized:
        # t1 (verified, won $0.82), t2 (coinbase_inferred, not counted)
        assert abs(s["total_pnl"] - 0.82) < 0.01
        assert s["wins"] == 1
        assert s["losses"] == 0  # t2 is coinbase_inferred, not in verified count
        assert s["open_trades"] == 1  # t3 is unresolved

    def test_strategy_pnl_breakdown(self, client):
        resp = client.get("/api/data")
        strats = resp.json()["stats"]["strategy_pnl"]
        assert "BTC 5m" in strats
        assert strats["BTC 5m"]["count"] == 1
        assert strats["BTC 5m"]["wins"] == 1
        assert "ETH 5m" in strats
        assert strats["ETH 5m"]["count"] == 1
        assert strats["ETH 5m"]["wins"] == 0


class TestApiTrades:
    def test_filtered_by_asset(self, client):
        resp = client.get("/api/trades?asset=BTC")
        trades = resp.json()["trades"]
        assert all(t["asset"] == "BTC" for t in trades)
        assert len(trades) == 1

    def test_filtered_by_timeframe(self, client):
        resp = client.get("/api/trades?tf=5m")
        trades = resp.json()["trades"]
        assert len(trades) == 3
        assert all("5m" in t["window_slug"] for t in trades)


class TestApiStrategyStats:
    def test_segment_breakdown(self, client):
        resp = client.get("/api/strategy-stats")
        data = resp.json()
        assert data["total_resolved"] == 2
        segs = data["by_segment"]
        assert "BTC 5m" in segs
        assert segs["BTC 5m"]["wr"] == 1.0  # 1 win out of 1


class TestApiCalibration:
    def test_calibration_buckets(self, client):
        resp = client.get("/api/calibration")
        data = resp.json()
        # t1 has p_final=0.79 → bucket 0.75-0.80, t2 has p_final=0.72 → bucket 0.70-0.75
        assert len(data) >= 1  # at least one bucket with data


class TestApiTrades2:
    def test_trades_endpoint_returns_all(self, client):
        resp = client.get("/api/trades?limit=10")
        data = resp.json()
        assert "trades" in data
        assert len(data["trades"]) == 3

    def test_trades_filter_by_asset(self, client):
        resp = client.get("/api/trades?asset=BTC&limit=10")
        trades = resp.json()["trades"]
        assert all(t.get("asset") == "BTC" for t in trades)

    def test_trades_has_metadata_fields(self, client):
        resp = client.get("/api/trades?limit=10")
        trades = resp.json()["trades"]
        t1 = next(t for t in trades if t.get("id") == "t1")
        assert t1["p_bayesian"] == 0.78
        assert t1["outcome_source"] == "polymarket_verified"


class TestApiPnlHistory:
    def test_hourly_buckets(self, client):
        resp = client.get("/api/pnl-history")
        data = resp.json()
        assert "labels" in data
        assert "values" in data
        # We have 2 resolved trades → at least 1 hourly bucket
        assert len(data["values"]) >= 1


class TestAuth:
    def test_html_page_requires_auth(self):
        """HTML page requires Basic Auth, API endpoints do not."""
        import scripts.dashboard as dash
        with TestClient(dash.app) as c:
            # HTML page requires auth
            resp = c.get("/")
            assert resp.status_code == 401
            # API endpoints are open (auth only on HTML gate)
            resp = c.get("/api/health")
            assert resp.status_code == 200

    def test_html_wrong_password_rejected(self):
        import scripts.dashboard as dash
        with TestClient(dash.app) as c:
            creds = base64.b64encode(b"admin:wrong").decode()
            c.headers["Authorization"] = f"Basic {creds}"
            resp = c.get("/")
            assert resp.status_code == 401
