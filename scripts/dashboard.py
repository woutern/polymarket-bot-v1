"""Live dashboard — strategy analysis desk view."""

import sys
sys.path.insert(0, "src")

import json
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal

import os as _os
import secrets
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

app = FastAPI()
_security = HTTPBasic()

# Dashboard password — set DASHBOARD_PASSWORD env var, default "polybot2026"
_DASHBOARD_USER = _os.getenv("DASHBOARD_USER", "admin")
_DASHBOARD_PASS = _os.getenv("DASHBOARD_PASSWORD", "polybot2026")

# ── Storage: DynamoDB first, fall back to SQLite ──────────────────────────────

_DB_PATH_CANDIDATES = [
    "polybot.db",
    _os.path.join(_os.path.dirname(__file__), "..", "polybot.db"),
]
_DB_PATH = next((p for p in _DB_PATH_CANDIDATES if _os.path.exists(p)), "polybot.db")

_USE_DYNAMO = False
_trades_table = None
_windows_table = None
_signals_table = None
_logs_client = None

try:
    import boto3 as _boto3
    # Try playground profile (local dev), fall back to instance/task role (AWS)
    try:
        _session = _boto3.Session(profile_name="playground", region_name="eu-west-1")
        _session.client("sts").get_caller_identity()  # validate credentials
    except Exception:
        _session = _boto3.Session(region_name="eu-west-1")
    _ddb = _session.resource("dynamodb", region_name="eu-west-1")
    _logs_client = _session.client("logs", region_name="eu-west-1")  # Bot runs in eu-west-1
    _trades_table = _ddb.Table("polymarket-bot-trades")
    _windows_table = _ddb.Table("polymarket-bot-windows")
    _signals_table = _ddb.Table("polymarket-bot-signals")
    _USE_DYNAMO = True
except Exception:
    pass

_USE_SQLITE = _os.path.exists(_DB_PATH)
_LOCAL_LOG = "/tmp/polybot_paper.log"

# Trading mode + bankroll from env (matches bot settings)
_TRADE_MODE = _os.getenv("MODE", "paper").lower()
_BANKROLL = float(_os.getenv("BANKROLL", "1000.0"))

_WALLET_ADDRESS = _os.getenv(
    "POLYMARKET_FUNDER", "0x5ca439d661c9b44337E91fC681ec4b006C473610"
)
_TOTAL_DEPOSITED = float(_os.getenv("TOTAL_DEPOSITED", "1850.00"))  # Updated 2026-03-21


# ── Helper: DynamoDB Decimal → float ──────────────────────────────────────────

def _decimal_to_float(obj):
    """Recursively convert Decimal to float for JSON serialization."""
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: _decimal_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decimal_to_float(i) for i in obj]
    return obj


def _sqlite_query(sql: str, params=()):
    import sqlite3
    try:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def _extract_field(t, key: str, default="") -> str:
    """Extract a field that may be a plain value or a DynamoDB {'S': value} dict."""
    v = t.get(key, default)
    if isinstance(v, dict):
        return v.get("S", v.get("N", default))
    return str(v) if v is not None else default


def _float_field(t, key: str, default: float = 0.0) -> float:
    v = t.get(key, default)
    if isinstance(v, dict):
        v = v.get("N", default)
    try:
        return float(v) if v is not None else default
    except Exception:
        return default


def _bool_field(t, key: str, default=False) -> bool:
    v = t.get(key, default)
    if isinstance(v, dict):
        v = v.get("BOOL", v.get("N", default))
    if isinstance(v, (int, float, Decimal)):
        return bool(int(v))
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.lower() in ("true", "1", "yes")
    return default


# ── Data access ───────────────────────────────────────────────────────────────

def _dynamo_scan_all(table, limit=500):
    """Scan a DynamoDB table, paginating if needed up to limit."""
    items = []
    kwargs = {"Limit": min(limit, 500)}
    while True:
        resp = table.scan(**kwargs)
        items.extend(resp.get("Items", []))
        if len(items) >= limit:
            break
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        kwargs["ExclusiveStartKey"] = last_key
    return _decimal_to_float(items[:limit])


def get_trades(limit=100, asset=None, tf=None):
    if _USE_DYNAMO:
        items = _dynamo_scan_all(_trades_table, limit=max(limit, 500))
        # Filter out dedup claim records (id starts with "claim_")
        items = [t for t in items if not str(t.get("id", "")).startswith("claim_")]
        items.sort(key=lambda x: float(x.get("timestamp", 0)), reverse=True)
        if asset:
            items = [t for t in items if _extract_field(t, "asset", "").upper() == asset.upper()]
        if tf:
            items = [t for t in items if (tf == "5m") == ("5m" in _extract_field(t, "window_slug", ""))]
        return items[:limit]
    if _USE_SQLITE:
        sql = "SELECT * FROM trades WHERE 1=1"
        params = []
        if asset:
            sql += " AND UPPER(asset) = ?"
            params.append(asset.upper())
        if tf:
            sql += " AND window_slug LIKE '%5m%'"
        sql += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        return _sqlite_query(sql, tuple(params))
    return []


def get_windows(limit=30, asset=None):
    if _USE_DYNAMO:
        items = _dynamo_scan_all(_windows_table, limit=max(limit, 200))
        items.sort(key=lambda x: int(x.get("open_ts", 0)), reverse=True)
        if asset:
            items = [w for w in items if _extract_field(w, "asset", "").upper() == asset.upper()]
        return items[:limit]
    if _USE_SQLITE:
        sql = "SELECT * FROM windows WHERE 1=1"
        params = []
        if asset:
            sql += " AND UPPER(asset) = ?"
            params.append(asset.upper())
        sql += " ORDER BY open_ts DESC LIMIT ?"
        params.append(limit)
        return _sqlite_query(sql, tuple(params))
    return []


def get_signals(limit=200, asset=None, outcome=None):
    if _USE_DYNAMO and _signals_table:
        items = _dynamo_scan_all(_signals_table, limit=max(limit, 500))
        items.sort(key=lambda x: float(x.get("timestamp", 0)), reverse=True)
        if asset:
            items = [s for s in items if _extract_field(s, "asset", "").upper() == asset.upper()]
        if outcome:
            items = [s for s in items if _extract_field(s, "outcome", "") == outcome]
        return items[:limit]
    # No SQLite signals table — return empty
    return []


def get_logs(lines=100):
    if _USE_DYNAMO and _logs_client:
        try:
            streams = _logs_client.describe_log_streams(
                logGroupName="/polymarket-bot",
                orderBy="LastEventTime",
                descending=True,
                limit=1,
            )
            if not streams["logStreams"]:
                return []
            stream = streams["logStreams"][0]["logStreamName"]
            resp = _logs_client.get_log_events(
                logGroupName="/polymarket-bot",
                logStreamName=stream,
                limit=lines,
                startFromHead=False,
            )
            return [e["message"] for e in resp["events"]]
        except Exception as e:
            return [f"Error: {e}"]
    # Local: tail the log file
    try:
        with open(_LOCAL_LOG) as f:
            all_lines = f.readlines()
        return [line.rstrip() for line in all_lines[-lines:]]
    except Exception:
        return []


# ── Auth ──────────────────────────────────────────────────────────────────────

def _require_auth(creds: HTTPBasicCredentials = Depends(_security)):
    ok_user = secrets.compare_digest(creds.username.encode(), _DASHBOARD_USER.encode())
    ok_pass = secrets.compare_digest(creds.password.encode(), _DASHBOARD_PASS.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )
    return creds.username


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/health")
def api_health():
    """No auth — health check."""
    return {
        "status": "ok",
        "mode": _TRADE_MODE,
        "storage": "dynamo" if _USE_DYNAMO else ("sqlite" if _USE_SQLITE else "none"),
        "ts": time.time(),
    }


@app.get("/api/data")
def api_data():
    trades = get_trades(limit=200)
    windows = get_windows(limit=50)
    log_lines = get_logs()
    signals = get_signals(limit=20)

    # Filter trades to current mode only
    mode_trades = [t for t in trades if _extract_field(t, "mode", "live") == _TRADE_MODE]

    resolved_trades = [t for t in mode_trades if t.get("resolved") or _bool_field(t, "resolved")]
    open_trade_list = [t for t in mode_trades if not (t.get("resolved") or _bool_field(t, "resolved"))]
    # Only count verified resolutions for realized P&L
    verified = [t for t in resolved_trades if _extract_field(t, "outcome_source") == "polymarket_verified"]
    total_pnl = sum(_float_field(t, "pnl") for t in verified)
    # Include coinbase_inferred as preliminary (show separately)
    preliminary_pnl = sum(_float_field(t, "pnl") for t in resolved_trades if _extract_field(t, "outcome_source") != "polymarket_verified")
    wins = sum(1 for t in verified if _float_field(t, "pnl") > 0)
    losses = sum(1 for t in verified if _float_field(t, "pnl") <= 0)
    open_trades = len(open_trade_list)
    # Unrealized: open positions with preliminary Coinbase P&L
    unrealized_pnl = sum(_float_field(t, "pnl") for t in open_trade_list if _float_field(t, "pnl") != 0)

    # Per-asset window counts (5m only)
    asset_windows = {}
    for w in windows:
        slug = w.get("slug", "") or ""
        slug_upper = slug.upper()
        if slug_upper.startswith("ETH"):
            asset = "ETH"
        elif slug_upper.startswith("SOL"):
            asset = "SOL"
        elif slug_upper.startswith("BTC"):
            asset = "BTC"
        else:
            a = w.get("asset", {})
            asset = a.get("S", "BTC") if isinstance(a, dict) else str(a) if a else "BTC"
        asset_windows[asset] = asset_windows.get(asset, 0) + 1

    # Per-asset x timeframe breakdown
    strategy_pnl = {}
    for t in mode_trades:
        if not (t.get("resolved") or _bool_field(t, "resolved")):
            continue
        asset = _extract_field(t, "asset", "BTC").upper() or "BTC"
        if asset == "ETH":
            continue  # ETH disabled — exclude from stats
        slug = _extract_field(t, "window_slug", "")
        key = f"{asset} 5m"
        p = _float_field(t, "pnl")
        if key not in strategy_pnl:
            strategy_pnl[key] = {"pnl": 0, "count": 0, "wins": 0}
        strategy_pnl[key]["pnl"] += p
        strategy_pnl[key]["count"] += 1
        if p > 0:
            strategy_pnl[key]["wins"] += 1

    # Recent signals for overview panel
    recent_signals = []
    for s in signals[:10]:
        recent_signals.append({
            "timestamp": _float_field(s, "timestamp"),
            "asset": _extract_field(s, "asset", ""),
            "timeframe": _extract_field(s, "timeframe", "5m"),
            "outcome": _extract_field(s, "outcome", ""),
            "rejection_reason": _extract_field(s, "rejection_reason", ""),
            "direction": _extract_field(s, "direction", ""),
            "pct_move": _float_field(s, "pct_move"),
            "ev": _float_field(s, "ev"),
            "model_prob": _float_field(s, "model_prob"),
            "market_price": _float_field(s, "market_price"),
        })

    current_bankroll = _BANKROLL + total_pnl

    return {
        "trades": trades[:50],
        "windows": windows,
        "logs": log_lines,
        "recent_signals": recent_signals,
        "stats": {
            "total_pnl": total_pnl,
            "unrealized_pnl": unrealized_pnl,
            "preliminary_pnl": preliminary_pnl,
            "wins": wins,
            "losses": losses,
            "open_trades": open_trades,
            "total_resolved": wins + losses,
            "asset_windows": asset_windows,
            "strategy_pnl": strategy_pnl,
            "mode": _TRADE_MODE,
            "starting_bankroll": _BANKROLL,
            "current_bankroll": current_bankroll,
        },
    }


@app.get("/api/trades")
def api_trades(
    asset: str = None,
    tf: str = None,
    limit: int = 100,
    ):
    """Filtered, paginated trade list."""
    trades = get_trades(limit=limit, asset=asset, tf=tf)
    return {"trades": trades, "count": len(trades)}


@app.get("/api/signals")
def api_signals(
    asset: str = None,
    outcome: str = None,
    limit: int = 200,
    ):
    """Signal evaluations — both fired and rejected."""
    signals = get_signals(limit=limit, asset=asset, outcome=outcome)
    return {"signals": signals, "count": len(signals)}


@app.get("/api/signals/summary")
def api_signals_summary():
    """Rejection counts by reason + funnel data."""
    signals = get_signals(limit=500)

    total = len(signals)
    executed = 0
    rejected_by_reason = defaultdict(int)
    passed_min_move = 0
    passed_filters = 0

    for s in signals:
        outcome = _extract_field(s, "outcome", "")
        reason = _extract_field(s, "rejection_reason", "")
        if outcome == "executed":
            executed += 1
            passed_min_move += 1
            passed_filters += 1
        elif outcome == "rejected":
            rejected_by_reason[reason] += 1
            if reason != "min_move":
                passed_min_move += 1
                if reason in ("insufficient_ev",):
                    passed_filters += 1

    return {
        "total": total,
        "executed": executed,
        "passed_min_move": passed_min_move,
        "passed_filters": passed_filters,
        "by_reason": dict(rejected_by_reason),
    }


@app.get("/api/strategy-stats")
def api_strategy_stats():
    """Win rates by asset x timeframe x hour-of-day segment."""
    trades = get_trades(limit=500)
    resolved = [t for t in trades if t.get("resolved") or _bool_field(t, "resolved")]

    by_segment: dict[str, dict] = {}
    by_hour: dict[int, dict] = {}
    by_pct_move_bucket: dict[str, dict] = {}

    for t in resolved:
        asset = _extract_field(t, "asset", "BTC").upper() or "BTC"
        slug = _extract_field(t, "window_slug", "")
        seg_key = f"{asset} 5m"
        pnl = _float_field(t, "pnl")
        won = pnl > 0

        # Segment breakdown
        if seg_key not in by_segment:
            by_segment[seg_key] = {"wins": 0, "total": 0, "pnl": 0.0}
        by_segment[seg_key]["total"] += 1
        by_segment[seg_key]["pnl"] += pnl
        if won:
            by_segment[seg_key]["wins"] += 1

        # Hour of day
        ts = _float_field(t, "timestamp")
        if ts:
            hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
            if hour not in by_hour:
                by_hour[hour] = {"wins": 0, "total": 0}
            by_hour[hour]["total"] += 1
            if won:
                by_hour[hour]["wins"] += 1

        # pct_move bucket
        pct_move = abs(_float_field(t, "pct_move"))
        if pct_move < 0.1:
            bucket = "0.0-0.1%"
        elif pct_move < 0.2:
            bucket = "0.1-0.2%"
        elif pct_move < 0.4:
            bucket = "0.2-0.4%"
        else:
            bucket = "0.4%+"
        if bucket not in by_pct_move_bucket:
            by_pct_move_bucket[bucket] = {"wins": 0, "total": 0}
        by_pct_move_bucket[bucket]["total"] += 1
        if won:
            by_pct_move_bucket[bucket]["wins"] += 1

    def wr(d):
        return round(d["wins"] / d["total"], 3) if d["total"] else 0

    return {
        "by_segment": {k: {**v, "wr": wr(v)} for k, v in by_segment.items()},
        "by_hour": {str(h): {**v, "wr": wr(v)} for h, v in sorted(by_hour.items())},
        "by_pct_move": {k: {**v, "wr": wr(v)} for k, v in by_pct_move_bucket.items()},
        "total_resolved": len(resolved),
    }


@app.get("/api/calibration")
def api_calibration():
    """p_final buckets vs actual win rate — model calibration curve."""
    trades = get_trades(limit=500)
    resolved = [t for t in trades if t.get("resolved") or _bool_field(t, "resolved")]

    buckets: dict[str, dict] = {}
    bucket_edges = [0.5, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 1.0]

    for t in resolved:
        p = _float_field(t, "p_final")
        if p == 0.0:
            p = _float_field(t, "p_bayesian")  # fallback for older records
        if p == 0.0:
            continue
        pnl = _float_field(t, "pnl")
        label = None
        for i in range(len(bucket_edges) - 1):
            if bucket_edges[i] <= p < bucket_edges[i + 1]:
                label = f"{bucket_edges[i]:.2f}-{bucket_edges[i+1]:.2f}"
                break
        if label is None:
            continue
        if label not in buckets:
            buckets[label] = {"wins": 0, "total": 0, "p_avg": 0.0, "p_sum": 0.0}
        buckets[label]["total"] += 1
        buckets[label]["p_sum"] += p
        if pnl > 0:
            buckets[label]["wins"] += 1

    result = {}
    for label, d in sorted(buckets.items()):
        result[label] = {
            "wins": d["wins"],
            "total": d["total"],
            "actual_wr": round(d["wins"] / d["total"], 3) if d["total"] else 0,
            "model_p_avg": round(d["p_sum"] / d["total"], 3) if d["total"] else 0,
        }
    return result


@app.get("/api/pairs")
def api_pairs():
    """Per-pair strategy config and performance summary."""
    try:
        from polybot.config import Settings
        settings = Settings()
        enabled = settings.enabled_pairs
        pairs = []
        for asset, dur in enabled:
            cfg = settings.pair_config(asset, dur)
            pairs.append(cfg)
    except Exception:
        _defaults = {
            "BTC 5m": 0.08, "ETH 5m": 0.10, "SOL 5m": 0.14,
        }
        pairs = [
            {"pair": k, "asset": k.split()[0], "timeframe": k.split()[1],
             "min_move_pct": v, "min_ev_threshold": 0.06, "max_market_price": 0.75,
             "entry_seconds": 60, "kelly_fraction": 0.25,
             "min_trade_usd": 1.0, "max_trade_usd": 1.0}
            for k, v in _defaults.items()
        ]

    # Enrich with actual performance from trades
    trades = get_trades(limit=500)
    resolved = [t for t in trades if t.get("resolved") or _bool_field(t, "resolved")]
    perf: dict[str, dict] = {}
    for t in resolved:
        asset = _extract_field(t, "asset", "BTC").upper()
        slug = _extract_field(t, "window_slug", "")
        key = f"{asset} 5m"
        if key not in perf:
            perf[key] = {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0}
        perf[key]["trades"] += 1
        pnl = _float_field(t, "pnl")
        perf[key]["pnl"] += pnl
        if pnl > 0:
            perf[key]["wins"] += 1
        else:
            perf[key]["losses"] += 1

    for p in pairs:
        k = p["pair"]
        s = perf.get(k, {"wins": 0, "losses": 0, "pnl": 0.0, "trades": 0})
        p["perf"] = {
            **s,
            "wr": round(s["wins"] / s["trades"], 3) if s["trades"] else 0,
            "pnl": round(s["pnl"], 4),
        }

    return {"pairs": pairs, "total_enabled": len(pairs)}


@app.get("/api/kpi")
def api_kpi():
    """Latest KPI snapshot from DynamoDB."""
    if _USE_DYNAMO:
        try:
            kpi_table = _ddb.Table("polymarket-bot-kpi-snapshots")
            resp = kpi_table.scan(Limit=5)
            items = resp.get("Items", [])
            if items:
                # Get most recent
                items.sort(key=lambda x: float(x.get("timestamp", 0)), reverse=True)
                latest = items[0]
                # Convert Decimals to floats
                def to_float(obj):
                    if hasattr(obj, '__float__'):
                        return float(obj)
                    if isinstance(obj, dict):
                        return {k: to_float(v) for k, v in obj.items()}
                    if isinstance(obj, list):
                        return [to_float(i) for i in obj]
                    return obj
                return to_float(latest)
        except Exception as e:
            return {"error": str(e), "status": "no_data"}
    return {"status": "no_data"}


@app.get("/api/pnl-history")
def api_pnl_history():
    """Return hourly P&L buckets from resolved trades."""
    trades = get_trades(limit=500)
    buckets: dict[str, float] = defaultdict(float)
    for t in trades:
        if not (t.get("resolved") or _bool_field(t, "resolved")):
            continue
        ts = t.get("timestamp")
        if not ts:
            continue
        pnl = _float_field(t, "pnl")
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            hour_key = dt.strftime("%Y-%m-%dT%H:00")
            buckets[hour_key] += pnl
        except Exception:
            continue
    sorted_buckets = sorted(buckets.items())
    return {
        "labels": [b[0] for b in sorted_buckets],
        "values": [round(b[1], 4) for b in sorted_buckets],
    }


@app.get("/api/balance")
async def api_balance():
    """Return wallet balance from on-chain USDC + Polymarket data-api.

    Cash: on-chain Polygon USDC balance (bridged USDC, 6 decimals).
    This is the actual liquid USDC — no auth needed, no calculation drift.
    Positions: from data-api /value endpoint.
    P&L: from activity history (spend vs redeems).
    """
    import httpx

    # Polygon bridged USDC contract + balanceOf selector
    USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
    BALANCE_OF = "0x70a08231"
    POLYGON_RPCS = [
        "https://rpc-mainnet.matic.quiknode.pro",
        "https://polygon.llamarpc.com",
        "https://rpc.ankr.com/polygon",
    ]

    result = {"cash": 0.0, "positions": 0.0, "portfolio": 0.0, "total_pnl": 0.0}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            # 1. On-chain USDC balance (liquid cash)
            addr_padded = _WALLET_ADDRESS.lower().replace("0x", "").zfill(64)
            call_data = BALANCE_OF + addr_padded
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method": "eth_call",
                "params": [{"to": USDC_CONTRACT, "data": call_data}, "latest"],
            }
            cash = 0.0
            for rpc in POLYGON_RPCS:
                try:
                    resp = await client.post(rpc, json=payload, timeout=5.0)
                    if resp.status_code == 200:
                        hex_val = resp.json().get("result", "0x0") or "0x0"
                        cash = int(hex_val, 16) / 1_000_000
                        break
                except Exception:
                    continue

            # 2. Open position value
            positions = 0.0
            try:
                resp = await client.get(
                    "https://data-api.polymarket.com/value",
                    params={"user": _WALLET_ADDRESS},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and data:
                        positions = float(data[0].get("value", 0) or 0)
            except Exception:
                pass

            # 3. P&L from activity history (excluding opportunity bot trades)
            # Load opportunity condition_ids to exclude from crypto bot P&L
            opp_condition_ids = set()
            try:
                _opp_tbl = _ddb.Table("polymarket-bot-opportunity-trades")
                _opp_resp = _opp_tbl.scan(ProjectionExpression="condition_id")
                for _oi in _opp_resp.get("Items", []):
                    cid = _oi.get("condition_id", "")
                    if cid:
                        opp_condition_ids.add(cid)
            except Exception:
                pass  # table may not exist yet

            total_spent = 0.0
            total_redeemed = 0.0
            opp_spent = 0.0
            opp_redeemed = 0.0
            offset = 0
            while offset < 2000:
                resp = await client.get(
                    "https://data-api.polymarket.com/activity",
                    params={"user": _WALLET_ADDRESS, "limit": 500, "offset": offset},
                )
                if resp.status_code != 200:
                    break
                batch = resp.json()
                if not batch:
                    break
                for a in batch:
                    usdc = float(a.get("usdcSize", 0) or 0)
                    cid = a.get("conditionId", "")
                    is_opp = cid in opp_condition_ids
                    if a.get("type") == "TRADE":
                        total_spent += usdc
                        if is_opp:
                            opp_spent += usdc
                    elif a.get("type") == "REDEEM":
                        total_redeemed += usdc
                        if is_opp:
                            opp_redeemed += usdc
                if len(batch) < 500:
                    break
                offset += 500

            portfolio = round(cash + positions, 2)
            # P&L = portfolio value - total deposited (simplest, most accurate)
            pnl = round(portfolio - _TOTAL_DEPOSITED, 2)

            result = {
                "cash": round(cash, 2),
                "positions": round(positions, 2),
                "portfolio": portfolio,
                "total_pnl": pnl,
                "total_deposited": _TOTAL_DEPOSITED,
                "total_spent": round(total_spent, 2),
                "total_redeemed": round(total_redeemed, 2),
            }
    except Exception as e:
        result["error"] = str(e)[:100]

    return result


@app.get("/api/opportunities")
async def api_opportunities():
    """Return opportunity trades and stats from DynamoDB."""
    result = {"active": [], "resolved": [], "deployed_today": 0, "win_rate": 0, "total_pnl": 0, "trades_today": 0}
    try:
        import time as _t
        from decimal import Decimal
        _opp_table = _ddb.Table("polymarket-bot-opportunity-trades")
        resp = _opp_table.scan()
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = _opp_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))

        today_start = _t.time() - 86400
        active = []
        resolved = []
        today_pnl = 0.0
        today_wins = 0
        today_resolved_count = 0
        today_trades_count = 0
        today_deployed = 0.0

        for i in items:
            i = {k: float(v) if isinstance(v, Decimal) else v for k, v in i.items()}
            ts = float(i.get("timestamp", 0))
            is_today = ts >= today_start

            if int(i.get("resolved", 0)):
                resolved.append(i)
                if is_today:
                    pnl = float(i.get("pnl", 0))
                    today_pnl += pnl
                    today_resolved_count += 1
                    today_trades_count += 1
                    if pnl > 0:
                        today_wins += 1
                    today_deployed += float(i.get("size_usd", 0))
            else:
                active.append(i)
                if is_today:
                    today_deployed += float(i.get("size_usd", 0))
                    today_trades_count += 1

        resolved.sort(key=lambda x: float(x.get("timestamp", 0)), reverse=True)
        active.sort(key=lambda x: float(x.get("timestamp", 0)), reverse=True)

        # P&L by category (all time)
        by_cat = {}
        for i in resolved + active:
            cat = str(i.get("category", "") or i.get("worker", "") or "other")
            if cat not in by_cat:
                by_cat[cat] = {"trades": 0, "wins": 0, "pnl": 0.0, "deployed": 0.0}
            by_cat[cat]["trades"] += 1
            by_cat[cat]["deployed"] += float(i.get("size_usd", 0))
            if int(i.get("resolved", 0)):
                p = float(i.get("pnl", 0))
                by_cat[cat]["pnl"] += p
                if p > 0:
                    by_cat[cat]["wins"] += 1

        result = {
            "active": active,
            "resolved": resolved[:50],
            "deployed_today": round(today_deployed, 2),
            "win_rate": round(today_wins / today_resolved_count * 100, 1) if today_resolved_count else 0,
            "total_pnl": round(today_pnl, 2),
            "trades_today": today_trades_count,
        }
    except Exception as e:
        result["error"] = str(e)[:100]
    return result


@app.post("/api/opportunities/resolve")
async def api_opportunities_resolve():
    """Force-resolve any opportunity trades past their end_date."""
    import httpx as _hx
    import json as _j
    from decimal import Decimal

    resolved_count = 0
    try:
        _opp_table = _ddb.Table("polymarket-bot-opportunity-trades")
        resp = _opp_table.scan(
            FilterExpression="resolved = :r",
            ExpressionAttributeValues={":r": 0},
        )
        items = resp.get("Items", [])
        now_iso = datetime.now(timezone.utc).isoformat()

        async with _hx.AsyncClient(timeout=10) as client:
            for item in items:
                end_str = item.get("end_date", "")
                if not end_str or end_str > now_iso:
                    continue  # Not past end_date yet

                slug = item.get("slug", "")
                if not slug:
                    continue

                try:
                    r = await client.get(
                        "https://gamma-api.polymarket.com/markets",
                        params={"slug": slug},
                    )
                    if r.status_code != 200:
                        continue
                    markets = r.json()
                    if not markets or not markets[0].get("closed"):
                        continue

                    prices = markets[0].get("outcomePrices", [])
                    if isinstance(prices, str):
                        prices = _j.loads(prices)
                    if len(prices) < 2:
                        continue

                    yf = float(prices[0])
                    if yf >= 0.99:
                        winner = "YES"
                    elif yf <= 0.01:
                        winner = "NO"
                    else:
                        continue

                    side = str(item.get("side", ""))
                    won = side == winner
                    ask = float(item.get("ask_price", 0))
                    size = float(item.get("size_usd", 0))
                    shares = float(item.get("shares", 0))
                    pnl = round(shares * (1.0 - ask), 2) if won else -size

                    _opp_table.update_item(
                        Key={"slug": slug},
                        UpdateExpression="SET resolved=:r, outcome=:o, pnl=:p",
                        ExpressionAttributeValues={
                            ":r": 1,
                            ":o": "won" if won else "lost",
                            ":p": Decimal(str(round(pnl, 4))),
                        },
                    )
                    resolved_count += 1
                except Exception:
                    pass

    except Exception as e:
        return {"resolved": resolved_count, "error": str(e)[:100]}

    return {"resolved": resolved_count}


@app.get("/api/opportunities/skipped")
async def api_opportunities_skipped():
    """Return skipped opportunities from the last scan for manual review."""
    result = {"skipped": []}
    try:
        from decimal import Decimal
        _skip_tbl = _ddb.Table("polymarket-bot-opportunity-skipped")
        resp = _skip_tbl.scan()
        items = resp.get("Items", [])
        # Convert Decimals and sort by volume desc
        converted = []
        for i in items:
            converted.append({k: float(v) if isinstance(v, Decimal) else v for k, v in i.items()})
        converted.sort(key=lambda x: float(x.get("volume", 0)), reverse=True)
        result["skipped"] = converted[:30]
    except Exception as e:
        result["error"] = str(e)[:100]
    return result


@app.post("/api/opportunities/trade")
async def api_opportunities_trade(slug: str, side: str, price: float):
    """Place a manual $2 FOK trade on a skipped opportunity.

    Called from the dashboard Trade button. Uses same wallet as main bot.
    Moves the market from skipped to trades table.
    """
    import httpx as _hx

    result = {"success": False, "error": ""}
    try:
        # 1. Get market data from skipped table
        from decimal import Decimal
        _skip_tbl = _ddb.Table("polymarket-bot-opportunity-skipped")
        resp = _skip_tbl.get_item(Key={"slug": slug})
        item = resp.get("Item")
        if not item:
            return {"success": False, "error": "Market not found in skipped list"}

        # 2. Get fresh market data from Gamma
        async with _hx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"https://gamma-api.polymarket.com/markets", params={"slug": slug})
            if resp.status_code != 200:
                return {"success": False, "error": f"Gamma API error: {resp.status_code}"}
            markets = resp.json()
            if not markets:
                return {"success": False, "error": "Market not found on Gamma"}
            market = markets[0]

        import json as _j
        tokens = market.get("clobTokenIds", [])
        if isinstance(tokens, str):
            tokens = _j.loads(tokens)
        prices = market.get("outcomePrices", [])
        if isinstance(prices, str):
            prices = _j.loads(prices)

        token_id = tokens[0] if side == "YES" else (tokens[1] if len(tokens) > 1 else "")
        current_price = float(prices[0]) if side == "YES" else (float(prices[1]) if len(prices) > 1 else 0)
        neg_risk = market.get("negRisk", False)
        condition_id = market.get("conditionId", "")

        if not token_id or current_price <= 0 or current_price >= 1:
            return {"success": False, "error": f"Bad token/price: {current_price}"}

        # 3. Place FOK order
        from polybot.config import Settings as _S
        from py_clob_client.client import ClobClient as _CC
        from py_clob_client.clob_types import ApiCreds as _AC, CreateOrderOptions as _CO, OrderArgs as _OA, OrderType as _OT

        _s = _S()
        _creds = _AC(api_key=_s.polymarket_api_key, api_secret=_s.polymarket_api_secret, api_passphrase=_s.polymarket_api_passphrase)
        _funder = _s.polymarket_funder or None
        _sig = 2 if _funder else 0
        _client = _CC(host="https://clob.polymarket.com", chain_id=_s.polymarket_chain_id, key=_s.polymarket_private_key, creds=_creds, signature_type=_sig, funder=_funder)

        _price = round(current_price, 2)
        _shares = max(round(2.00 / _price, 0), 5)
        _cost = round(_shares * _price, 2)

        _signed = _client.create_order(_OA(token_id=token_id, price=_price, size=_shares, side="BUY"), _CO(tick_size="0.01", neg_risk=neg_risk))
        _resp = _client.post_order(_signed, _OT.FOK)
        _success = _resp.get("success", False) if _resp else False
        _order_id = _resp.get("orderID", "") if _resp else ""
        _error = _resp.get("errorMsg", "") if _resp else ""

        if not _success:
            return {"success": False, "error": _error[:80]}

        # 4. Move from skipped → trades table
        import time as _t
        _trades_tbl = _ddb.Table("polymarket-bot-opportunity-trades")
        _trades_tbl.put_item(Item={
            "slug": slug,
            "timestamp": Decimal(str(_t.time())),
            "question": str(item.get("question", "?"))[:200],
            "condition_id": condition_id,
            "side": side,
            "ask_price": Decimal(str(_price)),
            "size_usd": Decimal(str(_cost)),
            "shares": Decimal(str(_shares)),
            "order_id": _order_id,
            "ai_verdict": str(item.get("ai_verdict", "manual")),
            "ai_confidence": item.get("ai_confidence", Decimal("0")),
            "ai_reasoning": "Manual trade from dashboard",
            "category": str(item.get("category", "")),
            "hours_left": item.get("hours_left", Decimal("0")),
            "volume": item.get("volume", Decimal("0")),
            "neg_risk": neg_risk,
            "resolved": 0,
            "outcome": "pending",
            "pnl": Decimal("0"),
        })

        # Remove from skipped
        _skip_tbl.delete_item(Key={"slug": slug})

        result = {"success": True, "order_id": _order_id, "price": _price, "shares": _shares, "cost": _cost}
    except Exception as e:
        result = {"success": False, "error": str(e)[:100]}

    return result


# ── HTML dashboard ────────────────────────────────────────────────────────────


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Bot</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
  :root{
    --bg:#f4f5f7;--card:#fff;--border:#e5e7eb;--text:#111827;--text2:#6b7280;--text3:#9ca3af;
    --green:#10b981;--red:#ef4444;--purple:#8b5cf6;--blue:#3b82f6;
    --radius:12px;
  }
  body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}

  /* Nav */
  nav{background:#111;padding:0 24px;height:48px;display:flex;align-items:center;justify-content:space-between;position:sticky;top:0;z-index:50}
  .nav-logo{font-size:15px;font-weight:700;color:#fff;letter-spacing:-0.3px}
  .nav-logo span{color:var(--purple);font-weight:800}
  .nav-tabs{display:flex;gap:2px}
  .nav-tab{padding:6px 14px;border-radius:6px;font-size:12px;font-weight:600;color:#9ca3af;cursor:pointer;border:none;background:none;transition:.15s}
  .nav-tab:hover{color:#fff;background:rgba(255,255,255,.08)}
  .nav-tab.active{color:#fff;background:rgba(139,92,246,.25)}
  .nav-mode{font-size:11px;font-weight:700;padding:3px 10px;border-radius:20px;background:rgba(16,185,129,.15);color:var(--green);letter-spacing:.5px}

  /* Layout */
  .page{max-width:1200px;margin:0 auto;padding:24px 20px 60px}
  .page-content{display:none}.page-content.active{display:block}

  /* Cards */
  .grid{display:grid;gap:16px;margin-bottom:24px}
  .grid-4{grid-template-columns:repeat(4,1fr)}
  .grid-2{grid-template-columns:repeat(2,1fr)}
  .card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:20px;transition:box-shadow .2s}
  .card:hover{box-shadow:0 4px 12px rgba(0,0,0,.06)}
  .card-label{font-size:11px;font-weight:600;text-transform:uppercase;letter-spacing:.8px;color:var(--text3);margin-bottom:8px}
  .card-value{font-size:28px;font-weight:800;letter-spacing:-.5px;line-height:1.1}
  .card-sub{font-size:12px;color:var(--text2);margin-top:6px}
  .green{color:var(--green)}.red{color:var(--red)}.purple{color:var(--purple)}.blue{color:#2563eb}

  /* Tables */
  table{width:100%;border-collapse:collapse}
  th{text-align:left;font-size:11px;font-weight:600;color:var(--text3);text-transform:uppercase;letter-spacing:.5px;padding:10px 14px;border-bottom:2px solid var(--border);white-space:nowrap}
  td{padding:10px 14px;border-bottom:1px solid #f3f4f6;font-size:13px;color:var(--text2)}
  tbody tr:hover td{background:#fafafa}
  .tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
  .tag-btc{background:#fff7ed;color:#ea580c}
  .tag-sol{background:#f3f0ff;color:#7c3aed}
  .tag-up{background:#ecfdf5;color:#059669}
  .tag-down{background:#fef2f2;color:#dc2626}
  .tag-win{background:#ecfdf5;color:#059669}
  .tag-loss{background:#fef2f2;color:#dc2626}
  .tag-prov-win{background:#eff6ff;color:#2563eb}
  .tag-prov-loss{background:#eff6ff;color:#2563eb}
  .tag-open{background:#f0f9ff;color:#0284c7}
  .empty{text-align:center;padding:40px;color:var(--text3);font-size:13px}

  /* Section */
  .section{margin-bottom:24px}
  .section-title{font-size:14px;font-weight:700;color:var(--text);margin-bottom:12px}
  .panel{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);overflow:hidden}

  /* Chart */
  .chart-wrap{padding:16px;height:220px}

  /* WR bars */
  .wr-row{display:flex;align-items:center;gap:8px;margin:6px 0}
  .wr-label{width:80px;font-size:12px;color:var(--text2);flex-shrink:0}
  .wr-bar-bg{flex:1;height:24px;background:#f3f4f6;border-radius:4px;overflow:hidden;position:relative}
  .wr-bar{height:100%;border-radius:4px;display:flex;align-items:center;padding:0 8px;font-size:11px;font-weight:700;color:#fff;min-width:30px}
  .wr-val{width:50px;text-align:right;font-size:12px;font-weight:600;color:var(--text);flex-shrink:0}

  /* Hamburger + mobile menu */
  .hamburger{display:none;background:none;border:none;cursor:pointer;color:#9ca3af;padding:4px}
  .mobile-menu{display:none;position:fixed;top:48px;left:0;right:0;background:#111;padding:8px 16px 12px;z-index:49;flex-direction:column;gap:4px}
  .mobile-menu.open{display:flex}
  .mobile-menu button{width:100%;padding:10px;border-radius:8px;font-size:14px;font-weight:600;color:#9ca3af;border:none;background:none;text-align:left;cursor:pointer}
  .mobile-menu button:hover,.mobile-menu button.active{color:#fff;background:rgba(139,92,246,.2)}

  /* Responsive */
  @media(max-width:768px){
    .grid-4{grid-template-columns:repeat(2,1fr)}
    .page{padding:16px 12px 40px}
    nav{padding:0 12px}
    .nav-tabs{display:none}
    .hamburger{display:block}
  }
  .sortable th[data-sort]{cursor:pointer;user-select:none;position:relative;padding-right:18px}
  .sortable th[data-sort]:hover{background:#f1f5f9}
  .sortable th[data-sort]::after{content:'↕';position:absolute;right:4px;opacity:0.3;font-size:11px}
  .sortable th.sort-asc::after{content:'↑';opacity:0.8}
  .sortable th.sort-desc::after{content:'↓';opacity:0.8}
  .btn-trade{background:#3b82f6;color:#fff;border:none;padding:4px 10px;border-radius:6px;font-size:12px;font-weight:600;cursor:pointer;white-space:nowrap}
  .q-cell{cursor:help;position:relative}
  .q-cell:hover .q-tip{display:block}
  .q-tip{display:none;position:absolute;left:0;top:100%;z-index:100;background:#1e293b;color:#fff;padding:10px 14px;border-radius:8px;font-size:13px;line-height:1.4;max-width:400px;min-width:250px;white-space:normal;box-shadow:0 4px 12px rgba(0,0,0,.15)}
  .btn-trade:hover{background:#2563eb}
  .btn-trade:disabled{background:#94a3b8;cursor:not-allowed}
  .badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
  .badge-pending{background:#fef3c7;color:#92400e}
  .badge-verifying{background:#dbeafe;color:#1e40af}
  .badge-win{background:#d1fae5;color:#065f46}
  .badge-loss{background:#fee2e2;color:#991b1b}
</style>
</head>
<body>

<nav>
  <div class="nav-logo">Poly<span>Bot</span></div>
  <div class="nav-tabs">
    <button class="nav-tab active" onclick="showPage('overview',this)">Overview</button>
    <button class="nav-tab" onclick="showPage('trades',this)">Trades</button>
    <button class="nav-tab" onclick="showPage('analytics',this)">Analytics</button>
    <button class="nav-tab" onclick="showPage('opportunities',this)">Opportunities</button>
    <button class="nav-tab" onclick="showPage('rules',this)">Rules</button>
  </div>
  <div class="nav-mode" id="mode-badge">LIVE</div>
  <button class="hamburger" onclick="document.getElementById('mobile-menu').classList.toggle('open')" aria-label="Menu">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" width="22" height="22">
      <line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>
    </svg>
  </button>
</nav>
<div class="mobile-menu" id="mobile-menu">
  <button class="active" onclick="showPage('overview',this);document.getElementById('mobile-menu').classList.remove('open')">Overview</button>
  <button onclick="showPage('trades',this);document.getElementById('mobile-menu').classList.remove('open')">Trades</button>
  <button onclick="showPage('analytics',this);document.getElementById('mobile-menu').classList.remove('open')">Analytics</button>
  <button onclick="showPage('opportunities',this);document.getElementById('mobile-menu').classList.remove('open')">Opportunities</button>
  <button onclick="showPage('rules',this);document.getElementById('mobile-menu').classList.remove('open')">Rules</button>
</div>

<!-- ═══ OVERVIEW ═══ -->
<div id="page-overview" class="page-content active">
<div class="page">

  <div class="grid grid-4">
    <div class="card">
      <div class="card-label">Total P&L</div>
      <div class="card-value" id="s-pnl">—</div>
      <div class="card-sub" id="s-pnl-sub"></div>
    </div>
    <div class="card">
      <div class="card-label">Portfolio</div>
      <div class="card-value purple" id="s-portfolio">—</div>
      <div class="card-sub" id="s-portfolio-sub"></div>
    </div>
    <div class="card">
      <div class="card-label">Win Rate</div>
      <div class="card-value" id="s-wr">—</div>
      <div class="card-sub" id="s-wr-sub"></div>
    </div>
    <div class="card">
      <div class="card-label">Trades</div>
      <div class="card-value" id="s-count">—</div>
      <div class="card-sub" id="s-count-sub"></div>
    </div>
  </div>

  <!-- WR by asset -->
  <div class="grid grid-2">
    <div class="card">
      <div class="card-label">Win Rate by Asset</div>
      <div id="wr-asset"></div>
    </div>
    <div class="card">
      <div class="card-label">Win Rate by Ask Price</div>
      <div id="wr-price"></div>
    </div>
  </div>

  <!-- Equity curve -->
  <div class="section">
    <div class="panel">
      <div style="padding:16px 20px 0;font-weight:700;font-size:14px">Equity Curve</div>
      <div class="chart-wrap"><canvas id="pnl-chart"></canvas></div>
    </div>
  </div>

  <!-- Recent trades -->
  <div class="section">
    <div class="section-title">Recent Trades</div>
    <div class="panel">
      <div style="overflow-x:auto">
        <table>
          <thead><tr><th>Time</th><th>Asset</th><th>Dir</th><th>Ask</th><th>Size</th><th>P&L</th><th>Result</th></tr></thead>
          <tbody id="trades-body"><tr><td colspan="7" class="empty">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>

</div>
</div>

<!-- ═══ TRADES ═══ -->
<div id="page-trades" class="page-content">
<div class="page">
  <div class="section-title">Trade History</div>
  <div class="panel">
    <div style="overflow-x:auto">
      <table>
        <thead><tr><th>Time</th><th>Asset</th><th>Side</th><th>Dir</th><th>Price</th><th>Size</th><th>P&L</th><th>Result</th></tr></thead>
        <tbody id="tl-body"><tr><td colspan="8" class="empty">Loading...</td></tr></tbody>
      </table>
    </div>
  </div>
</div>
</div>

<!-- ═══ ANALYTICS ═══ -->
<div id="page-analytics" class="page-content">
<div class="page">
  <div class="grid grid-2">
    <div class="card">
      <div class="card-label">P&L by Asset</div>
      <div id="pnl-asset"></div>
    </div>
    <div class="card">
      <div class="card-label">P&L by Ask Bucket</div>
      <div id="pnl-bucket"></div>
    </div>
  </div>
  <div class="section">
    <div class="section-title">P&L by Hour (CET)</div>
    <div class="panel">
      <div class="chart-wrap"><canvas id="hour-chart"></canvas></div>
    </div>
  </div>
</div>
</div>

<!-- ═══ OPPORTUNITIES ═══ -->
<div id="page-opportunities" class="page-content">
<div class="page">
  <div class="grid grid-4">
    <div class="card"><div class="card-label">Deployed Today</div><div class="card-value" id="opp-deployed">—</div></div>
    <div class="card"><div class="card-label">Win Rate</div><div class="card-value" id="opp-wr">—</div></div>
    <div class="card"><div class="card-label">P&L</div><div class="card-value" id="opp-pnl">—</div></div>
    <div class="card"><div class="card-label">Active Trades (24h)</div><div class="card-value" id="opp-count">—</div></div>
  </div>
  <div class="section">
    <div class="section-title">Active Trades</div>
    <div class="panel"><table class="t sortable" id="tbl-opp-active"><thead><tr>
      <th data-sort="str">Market</th><th data-sort="str">Side</th><th data-sort="num">Entry</th><th data-sort="num">Invested</th><th data-sort="num">To Win</th><th data-sort="str">AI</th><th data-sort="num">Resolves</th><th data-sort="str">Status</th>
    </tr></thead><tbody id="opp-active"></tbody></table></div>
  </div>
  <div class="section">
    <div class="section-title">Resolved Trades (7 days)</div>
    <div class="panel"><table class="t sortable" id="tbl-opp-resolved"><thead><tr>
      <th data-sort="str">Market</th><th data-sort="str">Side</th><th data-sort="num">Entry</th><th data-sort="num">Invested</th><th data-sort="str">Result</th><th data-sort="num">P&L</th><th data-sort="str">Resolved</th>
    </tr></thead><tbody id="opp-resolved"></tbody></table></div>
  </div>
</div>
</div>

<!-- ═══ RULES ═══ -->
<div id="page-rules" class="page-content">
<div class="page">
  <div class="section">
    <div class="section-title">5-Minute Crypto Bot</div>
    <div class="panel">
      <table class="t">
        <thead><tr><th>Rule</th><th>Value</th></tr></thead>
        <tbody>
          <tr><td><b>Pairs</b></td><td>BTC_5m, SOL_5m (ETH disabled)</td></tr>
          <tr><td><b>Entry window</b></td><td>T+210s → T+240s scan (every 3s), hard deadline T+255s</td></tr>
          <tr><td><b>Direction</b></td><td>Follow higher ask side (YES if yes_ask ≥ no_ask, else NO)</td></tr>
          <tr><td colspan="2" style="padding-top:12px;font-weight:700;color:var(--text2)">PRICE GUARDS</td></tr>
          <tr><td>Min ask (weekday)</td><td>$0.65</td></tr>
          <tr><td>Min ask (weekend Sat/Sun)</td><td>$0.70</td></tr>
          <tr><td>Max ask BTC</td><td>$0.78</td></tr>
          <tr><td>Max ask SOL</td><td>$0.82</td></tr>
          <tr><td>Early entry (peak)</td><td>≤ $0.58 → enter immediately</td></tr>
          <tr><td>Early entry (weak hours)</td><td>≤ $0.68</td></tr>
          <tr><td>Early entry (weekend)</td><td>≤ $0.72</td></tr>
          <tr><td colspan="2" style="padding-top:12px;font-weight:700;color:var(--text2)">SIZING</td></tr>
          <tr><td>Ask $0.65–$0.75</td><td><b>$5.00</b></td></tr>
          <tr><td>Ask $0.75–$0.82</td><td><b>$10.00</b></td></tr>
          <tr><td>Hard cap</td><td>$10.00 (HARDCODED_MAX_BET)</td></tr>
          <tr><td colspan="2" style="padding-top:12px;font-weight:700;color:var(--text2)">TIME FILTERS</td></tr>
          <tr><td>Peak hours (CET)</td><td>10:00–13:00, 14:00–22:00 → min_ask $0.65</td></tr>
          <tr><td>Weak hours (CET)</td><td>22:00–10:00 + 13:00–14:00 → min_ask $0.65, early $0.68</td></tr>
          <tr><td>Weekend (Sat/Sun)</td><td>min_ask $0.70, early $0.72</td></tr>
          <tr><td colspan="2" style="padding-top:12px;font-weight:700;color:var(--text2)">OTHER GUARDS</td></tr>
          <tr><td>Volatility filter</td><td>Skip if realized_vol > 2× rolling avg ("choppy_market")</td></tr>
          <tr><td>Direction flip</td><td>Skip if direction reverses during scan</td></tr>
          <tr><td>Circuit breaker</td><td>3 consecutive losses → 15min pause</td></tr>
          <tr><td>Dedup</td><td>3-layer: memory + DynamoDB query + atomic claim</td></tr>
          <tr><td>Resolution</td><td>Polymarket Chainlink oracle only (verify sweep every 5min)</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Opportunity Bot</div>
    <div class="panel">
      <table class="t">
        <thead><tr><th>Rule</th><th>Value</th></tr></thead>
        <tbody>
          <tr><td><b>Scan interval</b></td><td>Every 30 minutes</td></tr>
          <tr><td><b>Workers</b></td><td>7 parallel: crypto, finance, politics, geopolitics, tech, basketball, news</td></tr>
          <tr><td><b>Scan window</b></td><td>Markets resolving in 30min – 48h</td></tr>
          <tr><td><b>Min volume</b></td><td>$1,000</td></tr>
          <tr><td><b>Skip</b></td><td>Slugs with 5m, 15m, updown (main bot handles)</td></tr>
          <tr><td colspan="2" style="padding-top:12px;font-weight:700;color:var(--text2)">TIER 1 — AUTO TRADE</td></tr>
          <tr><td>Ask range</td><td>$0.85–$0.95</td></tr>
          <tr><td>Resolves within</td><td>24 hours</td></tr>
          <tr><td>Size</td><td><b>$5.00</b> FOK at best ask</td></tr>
          <tr><td>AI check</td><td>Haiku sanity check (conf ≥ 0.75 to proceed)</td></tr>
          <tr><td colspan="2" style="padding-top:12px;font-weight:700;color:var(--text2)">TIER 2 — AI ASSESSED</td></tr>
          <tr><td>Ask range</td><td>$0.65–$0.85 (<24h) OR $0.85–$0.95 (24-48h)</td></tr>
          <tr><td>AI model</td><td>Claude Haiku via Bedrock</td></tr>
          <tr><td>Trade gate</td><td>confidence ≥ 0.80 AND edge ≥ 0.15</td></tr>
          <tr><td>Size</td><td><b>$2.50</b> FOK at best ask</td></tr>
          <tr><td colspan="2" style="padding-top:12px;font-weight:700;color:var(--text2)">LIMITS</td></tr>
          <tr><td>Max total deployed</td><td>$1,250</td></tr>
          <tr><td>Max ask on orderbook</td><td>$0.95 (reject if best ask > $0.95)</td></tr>
          <tr><td>Basketball</td><td>Only in-progress games (ESPN live check)</td></tr>
          <tr><td>Dedup</td><td>Atomic conditional put by condition_id</td></tr>
          <tr><td>Resolution</td><td>Gamma API, 5 retries at 60s, checked every scan + on dashboard load</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Auto-Claim</div>
    <div class="panel">
      <table class="t">
        <thead><tr><th>Rule</th><th>Value</th></tr></thead>
        <tbody>
          <tr><td><b>Interval</b></td><td>Every 30 minutes</td></tr>
          <tr><td><b>Method</b></td><td>Builder Relayer API (gasless, Polymarket pays gas)</td></tr>
          <tr><td><b>Contracts</b></td><td>CTF (regular) + NegRisk CTF Exchange (neg-risk markets)</td></tr>
          <tr><td><b>Auth</b></td><td>Builder API credentials via Gnosis Safe</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <div class="section">
    <div class="section-title">Infrastructure</div>
    <div class="panel">
      <table class="t">
        <thead><tr><th>Component</th><th>Detail</th></tr></thead>
        <tbody>
          <tr><td>Bot</td><td>ECS Fargate, eu-west-1 (single task, task-def rev 17)</td></tr>
          <tr><td>Dashboard</td><td>Lambda + API Gateway + CloudFront, eu-west-1</td></tr>
          <tr><td>Storage</td><td>DynamoDB, eu-west-1</td></tr>
          <tr><td>AI</td><td>Bedrock Claude Haiku, eu-west-1</td></tr>
          <tr><td>Processes</td><td>4: 5min bot, opportunity bot, dashboard, auto-claim</td></tr>
          <tr><td>Tests</td><td id="rules-test-count">502 passing</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</div>
</div>

<script>
let pnlChart = null, hourChart = null;

function showPage(name, btn) {
  document.querySelectorAll('.page-content').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  if (btn) btn.classList.add('active');
  if (name === 'overview') refresh();
  else if (name === 'trades') loadTrades();
  else if (name === 'analytics') loadAnalytics();
  else if (name === 'opportunities') loadOpportunities();
  // 'rules' page is static — no data to load
}

function fmtTs(ts) {
  if (!ts) return '—';
  return new Date(parseFloat(ts)*1000).toLocaleTimeString('en-GB',{timeZone:'Europe/Amsterdam',hour12:false});
}

function assetTag(a) {
  const cls = a === 'BTC' ? 'tag-btc' : a === 'SOL' ? 'tag-sol' : '';
  return '<span class="tag '+cls+'">'+a+'</span>';
}

function wrBar(label, wins, total, color) {
  const pct = total > 0 ? Math.round(wins/total*100) : 0;
  const w = Math.max(3, pct);
  return '<div class="wr-row"><span class="wr-label">'+label+'</span><div class="wr-bar-bg"><div class="wr-bar" style="width:'+w+'%;background:'+color+'">'+pct+'%</div></div><span class="wr-val">'+wins+'/'+total+'</span></div>';
}

async function refresh() {
  try {
    const [dataResp, balResp] = await Promise.all([fetch('/api/data'), fetch('/api/balance')]);
    const data = await dataResp.json();
    const bal = await balResp.json();
    const s = data.stats;
    const trades = (data.trades || []).filter(t => (t.asset||'') !== 'ETH');

    // Stats — Polymarket is source of truth for P&L and portfolio
    const pnl = bal.total_pnl || 0;
    document.getElementById('s-pnl').textContent = (pnl>=0?'+':'')+'\$'+Math.abs(pnl).toFixed(2);
    document.getElementById('s-pnl').className = 'card-value '+(pnl>=0?'green':'red');
    document.getElementById('s-pnl-sub').textContent = 'Deposited: \$'+(bal.total_deposited||0).toFixed(2);

    document.getElementById('s-portfolio').textContent = '\$'+(bal.portfolio||0).toFixed(2);
    document.getElementById('s-portfolio-sub').textContent = 'Cash: \$'+(bal.cash||0).toFixed(2)+(bal.positions > 0.01 ? ' + Positions: \$'+(bal.positions).toFixed(2) : '');

    const wr = s.total_resolved > 0 ? Math.round(s.wins/s.total_resolved*100) : 0;
    document.getElementById('s-wr').textContent = wr+'%';
    document.getElementById('s-wr').className = 'card-value '+(wr>=55?'green':wr<45?'red':'');
    document.getElementById('s-wr-sub').textContent = s.wins+'W / '+s.losses+'L';

    document.getElementById('s-count').textContent = s.total_resolved + s.open_trades;
    document.getElementById('s-count-sub').textContent = s.open_trades > 0 ? s.open_trades+' open' : 'all resolved';

    // WR by asset
    const sp = s.strategy_pnl || {};
    let assetHtml = '';
    for (const [pair, d] of Object.entries(sp)) {
      const a = pair.split(' ')[0];
      const c = d.wins/d.count >= 0.55 ? 'var(--green)' : d.wins/d.count < 0.45 ? 'var(--red)' : 'var(--blue)';
      assetHtml += wrBar(a, d.wins, d.count, c);
    }
    document.getElementById('wr-asset').innerHTML = assetHtml || '<div class="empty">No data yet</div>';

    // WR by ask price bucket
    const buckets = {'$0.50-0.60':{w:0,n:0},'$0.60-0.70':{w:0,n:0},'$0.70-0.78':{w:0,n:0}};
    for (const t of trades) {
      if (!t.resolved) continue;
      const p = parseFloat(t.fill_price||t.price||0);
      const won = parseFloat(t.pnl||0) > 0;
      let bk = p < 0.60 ? '$0.50-0.60' : p < 0.70 ? '$0.60-0.70' : '$0.70-0.78';
      if (buckets[bk]) { buckets[bk].n++; if (won) buckets[bk].w++; }
    }
    let bkHtml = '';
    for (const [lbl, d] of Object.entries(buckets)) {
      if (d.n === 0) continue;
      const c = d.w/d.n >= 0.55 ? 'var(--green)' : d.w/d.n < 0.45 ? 'var(--red)' : 'var(--blue)';
      bkHtml += wrBar(lbl, d.w, d.n, c);
    }
    document.getElementById('wr-price').innerHTML = bkHtml || '<div class="empty">No data yet</div>';

    // Recent trades
    const tbody = document.getElementById('trades-body');
    tbody.innerHTML = '';
    if (!trades.length) { tbody.innerHTML = '<tr><td colspan="7" class="empty">No trades yet</td></tr>'; }
    for (const t of trades.slice(0, 20)) {
      const pnl = parseFloat(t.pnl||0);
      const resolved = t.resolved && parseFloat(t.resolved) === 1;
      const won = pnl > 0;
      const verified = t.outcome_source === 'polymarket_verified' || t.outcome_source === 'manual_sell';
      const result = !resolved ? '<span class="tag tag-open">OPEN</span>'
        : verified ? (won ? '<span class="tag tag-win">WIN</span>' : '<span class="tag tag-loss">LOSS</span>')
        : (won ? '<span class="tag tag-prov-win">WIN?</span>' : '<span class="tag tag-prov-loss">LOSS?</span>');
      const dir = t.direction === 'up' || t.side === 'YES' ? '<span class="tag tag-up">UP</span>' : '<span class="tag tag-down">DOWN</span>';
      const pnlStr = resolved ? (pnl>=0?'+':'')+'\$'+Math.abs(pnl).toFixed(2) : '—';
      const pnlCls = resolved ? (verified ? (won?'green':'red') : 'blue') : '';
      const slug = t.window_slug || '';
      const link = slug ? ' <a href="https://polymarket.com/market/'+slug+'" target="_blank" style="opacity:.4;text-decoration:none;font-size:11px">&#x1F517;</a>' : '';
      tbody.innerHTML += '<tr><td style="white-space:nowrap">'+fmtTs(t.timestamp)+'</td><td>'+assetTag(t.asset||'')+'</td><td>'+dir+'</td><td>\$'+(parseFloat(t.fill_price||0)).toFixed(2)+'</td><td>\$'+(parseFloat(t.size_usd||0)).toFixed(2)+'</td><td class="'+pnlCls+'" style="font-weight:600">'+pnlStr+'</td><td>'+result+link+'</td></tr>';
    }

    // Equity curve from resolved trades
    try {
      const resolved = trades.filter(t => t.resolved && parseFloat(t.resolved) === 1).reverse();
      if (resolved.length > 1) {
        let cum = 0;
        const labels = resolved.map(t => fmtTs(t.timestamp));
        const points = resolved.map(t => { cum += parseFloat(t.pnl||0); return Math.round(cum*100)/100; });
        const ctx = document.getElementById('pnl-chart').getContext('2d');
        if (pnlChart) pnlChart.destroy();
        pnlChart = new Chart(ctx, {
          type: 'line',
          data: { labels, datasets: [{ data: points, borderColor: '#8b5cf6', backgroundColor: 'rgba(139,92,246,.08)', fill: true, tension: 0.3, pointRadius: 2, borderWidth: 2 }] },
          options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { display: false } }, scales: { x: { display: true, ticks: { maxTicksLimit: 8, font: { size: 10 } } }, y: { ticks: { callback: v => '\$'+v.toFixed(0), font: { size: 10 } } } } }
        });
      }
    } catch(e) {}

    document.getElementById('mode-badge').textContent = (s.mode||'live').toUpperCase();
  } catch(e) { console.error('refresh error', e); }
}

async function loadTrades() {
  try {
    const resp = await fetch('/api/trades?limit=100');
    const data = await resp.json();
    const tbody = document.getElementById('tl-body');
    tbody.innerHTML = '';
    for (const t of (data.trades||[]).filter(t => (t.asset||'') !== 'ETH')) {
      const pnl = parseFloat(t.pnl||0);
      const resolved = t.resolved && parseFloat(t.resolved) === 1;
      const won = pnl > 0;
      const verified2 = t.outcome_source === 'polymarket_verified' || t.outcome_source === 'manual_sell';
      const result = !resolved ? '<span class="tag tag-open">OPEN</span>'
        : verified2 ? (won ? '<span class="tag tag-win">WIN</span>' : '<span class="tag tag-loss">LOSS</span>')
        : (won ? '<span class="tag tag-prov-win">WIN?</span>' : '<span class="tag tag-prov-loss">LOSS?</span>');
      const dir = t.direction === 'up' || t.side === 'YES' ? 'UP' : 'DOWN';
      const slug2 = t.window_slug || '';
      const link2 = slug2 ? ' <a href="https://polymarket.com/market/'+slug2+'" target="_blank" style="opacity:.4;text-decoration:none;font-size:11px">&#x1F517;</a>' : '';
      const pnlCls2 = resolved ? (verified2 ? (won?'green':'red') : 'blue') : '';
      tbody.innerHTML += '<tr><td style="white-space:nowrap">'+fmtTs(t.timestamp)+'</td><td>'+assetTag(t.asset||'')+'</td><td>'+t.side+'</td><td>'+dir+'</td><td>\$'+(parseFloat(t.fill_price||0)).toFixed(2)+'</td><td>\$'+(parseFloat(t.size_usd||0)).toFixed(2)+'</td><td class="'+pnlCls2+'" style="font-weight:600">'+(resolved?(pnl>=0?'+':'')+'\$'+Math.abs(pnl).toFixed(2):'—')+'</td><td>'+result+link2+'</td></tr>';
    }
  } catch(e) {}
}

async function loadAnalytics() {
  try {
    const resp = await fetch('/api/data');
    const data = await resp.json();
    const trades = (data.trades || []).filter(t => (t.asset||'') !== 'ETH');
    const sp = data.stats.strategy_pnl || {};

    // PnL by asset
    let ah = '';
    for (const [pair, d] of Object.entries(sp)) {
      const pnl = d.pnl || 0;
      const wr = d.count > 0 ? Math.round(d.wins/d.count*100) : 0;
      ah += '<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f3f4f6"><span style="font-weight:600">'+pair+'</span><span><span class="'+(pnl>=0?'green':'red')+'" style="font-weight:700">'+(pnl>=0?'+':'')+'\$'+Math.abs(pnl).toFixed(2)+'</span> <span style="color:var(--text3);font-size:12px">'+wr+'% WR ('+d.count+')</span></span></div>';
    }
    document.getElementById('pnl-asset').innerHTML = ah || '<div class="empty">No data</div>';

    // PnL by bucket
    const bk = {'$0.50-0.60':{pnl:0,n:0},'$0.60-0.70':{pnl:0,n:0},'$0.70-0.78':{pnl:0,n:0}};
    for (const t of trades) {
      if (!t.resolved) continue;
      const p = parseFloat(t.fill_price||0);
      const tpnl = parseFloat(t.pnl||0);
      let b = p < 0.60 ? '$0.50-0.60' : p < 0.70 ? '$0.60-0.70' : '$0.70-0.78';
      if (bk[b]) { bk[b].pnl += tpnl; bk[b].n++; }
    }
    let bh = '';
    for (const [lbl, d] of Object.entries(bk)) {
      if (!d.n) continue;
      bh += '<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f3f4f6"><span>'+lbl+'</span><span class="'+(d.pnl>=0?'green':'red')+'" style="font-weight:700">'+(d.pnl>=0?'+':'')+'\$'+Math.abs(d.pnl).toFixed(2)+' ('+d.n+')</span></div>';
    }
    document.getElementById('pnl-bucket').innerHTML = bh || '<div class="empty">No data</div>';

    // Hourly P&L chart (Amsterdam/CET time)
    function getCETHour(ts) {
      return parseInt(new Date(parseFloat(ts)*1000).toLocaleString('en-GB',{timeZone:'Europe/Amsterdam',hour:'numeric',hour12:false}));
    }
    const hours = {};
    for (const t of trades) {
      if (!t.resolved) continue;
      const h = getCETHour(t.timestamp);
      if (!hours[h]) hours[h] = 0;
      hours[h] += parseFloat(t.pnl||0);
    }
    const hLabels = Array.from({length:24},(_,i)=>i+':00');
    const hData = hLabels.map((_,i)=>hours[i]||0);
    const hColors = hData.map(v=>v>=0?'rgba(16,185,129,.7)':'rgba(239,68,68,.7)');
    const ctx = document.getElementById('hour-chart').getContext('2d');
    if (hourChart) hourChart.destroy();
    hourChart = new Chart(ctx, {
      type: 'bar',
      data: { labels: hLabels, datasets: [{ data: hData, backgroundColor: hColors, borderRadius: 4 }] },
      options: { responsive: true, maintainAspectRatio: false, animation: false, plugins: { legend: { display: false } }, scales: { y: { ticks: { callback: v => '\$'+v.toFixed(0), font: { size: 10 } } }, x: { ticks: { font: { size: 9 } } } } }
    });
  } catch(e) { console.error('analytics error', e); }
}

async function loadOpportunities() {
  try {
    // Force-resolve any trades past end_date before loading
    try {
      const resolveResp = await fetch('/api/opportunities/resolve', {method:'POST'});
      const resolveData = await resolveResp.json().catch(() => ({}));
      if (resolveData.resolved > 0) console.log('Resolved '+resolveData.resolved+' trades');
    } catch(e) {
      // Try direct API Gateway if CloudFront blocks POST
      try {
        const r2 = await fetch('https://mwdbfw44q0.execute-api.eu-west-1.amazonaws.com/api/opportunities/resolve', {method:'POST'});
        const d2 = await r2.json().catch(() => ({}));
        if (d2.resolved > 0) console.log('Resolved '+d2.resolved+' trades (direct)');
      } catch(e2) {}
    }

    const resp = await fetch('/api/opportunities');
    const data = await resp.json();
    // KPIs
    document.getElementById('opp-deployed').textContent = '$'+(data.deployed_today||0).toFixed(2);
    document.getElementById('opp-wr').textContent = (data.win_rate||0).toFixed(0)+'%';
    document.getElementById('opp-pnl').textContent = (data.total_pnl>=0?'+':'')+'\$'+(data.total_pnl||0).toFixed(2);
    document.getElementById('opp-pnl').className = 'card-value '+(data.total_pnl>=0?'green':'red');
    document.getElementById('opp-count').textContent = data.trades_today||0;
    // Active trades — with 3 status states
    let ah = '';
    const now = Date.now();
    for (const t of (data.active||[])) {
      const aSlug = t.slug||'';
      const aLink = aSlug ? ' <a href="https://polymarket.com/market/'+aSlug+'" target="_blank" style="opacity:0.4">&#x1F517;</a>' : '';
      let aTimeStr = '—';
      let pastEnd = false;
      if (t.end_date) {
        const endMs = new Date(t.end_date).getTime();
        pastEnd = endMs < now;
        const aHrs = Math.max(0,(endMs-now)/3600000);
        const aH = Math.floor(aHrs); const aM = Math.round((aHrs-aH)*60);
        aTimeStr = pastEnd ? 'Ended' : aH > 0 ? aH+'h '+aM+'m' : aM+'m';
      }
      const invested = parseFloat(t.size_usd||0);
      const shares = parseFloat(t.shares||0);
      const toWin = shares > 0 ? (shares - invested).toFixed(2) : '—';
      const statusBadge = pastEnd
        ? '<span class="badge badge-verifying">Verifying</span>'
        : '<span class="badge badge-pending">Pending</span>';
      const reasoning = (t.ai_reasoning||'');
      const aiShort = reasoning.slice(0,30) + (reasoning.length > 30 ? '...' : '');
      const aiCell = reasoning ? '<span class="q-cell" style="font-size:12px;color:#64748b">' + aiShort + '<span class="q-tip">' + reasoning + '</span></span>' : '—';
      ah += '<tr><td>'+'<span class="q-cell">'+t.question.slice(0,45)+'<span class="q-tip">'+t.question+'</span></span>'+aLink+'</td><td>'+t.side+'</td><td>$'+parseFloat(t.ask_price).toFixed(2)+'</td><td>$'+invested.toFixed(2)+'</td><td class="green">$'+toWin+'</td><td>'+aiCell+'</td><td>'+aTimeStr+'</td><td>'+statusBadge+'</td></tr>';
    }
    document.getElementById('opp-active').innerHTML = ah || '<tr><td colspan="8" class="empty">No active trades</td></tr>';
    // Resolved trades
    let rh = '';
    for (const t of (data.resolved||[])) {
      const pnl = parseFloat(t.pnl||0);
      const won = pnl > 0;
      const rSlug = t.slug||'';
      const rLink = rSlug ? ' <a href="https://polymarket.com/market/'+rSlug+'" target="_blank" style="opacity:0.4">&#x1F517;</a>' : '';
      const invested = parseFloat(t.size_usd||0);
      const rTs = t.timestamp ? new Date(parseFloat(t.timestamp)*1000).toLocaleString('en-GB',{timeZone:'Europe/Amsterdam',day:'2-digit',month:'short',hour:'2-digit',minute:'2-digit',hour12:false}) : '—';
      rh += '<tr style="background:'+(won?'#f0fdf4':'#fef2f2')+'"><td>'+'<span class="q-cell">'+t.question.slice(0,45)+'<span class="q-tip">'+t.question+'</span></span>'+rLink+'</td><td>'+t.side+'</td><td>$'+parseFloat(t.ask_price).toFixed(2)+'</td><td>$'+invested.toFixed(2)+'</td><td><span class="badge badge-'+(won?'win':'loss')+'">'+(won?'WON':'LOST')+'</span></td><td class="'+(won?'green':'red')+'" style="font-weight:700">'+(pnl>=0?'+':'')+'\$'+Math.abs(pnl).toFixed(2)+'</td><td style="font-size:12px;color:#64748b">'+rTs+'</td></tr>';
    }
    document.getElementById('opp-resolved').innerHTML = rh || '<tr><td colspan="7" class="empty">No resolved trades yet</td></tr>';
  } catch(e) { console.error('opportunities error', e); }
}

async function manualTrade(slug, side, price) {
  if (!confirm('Place $2 FOK trade on '+slug+' ('+side+' at $'+price.toFixed(2)+')?')) return;
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = '...';
  try {
    let resp;
    try {
      resp = await fetch('/api/opportunities/trade?slug='+encodeURIComponent(slug)+'&side='+side+'&price='+price, {method:'POST'});
    } catch(fetchErr) {
      // CloudFront may block POST — try direct API Gateway
      resp = await fetch('https://mwdbfw44q0.execute-api.eu-west-1.amazonaws.com/api/opportunities/trade?slug='+encodeURIComponent(slug)+'&side='+side+'&price='+price, {method:'POST'});
    }
    const txt = await resp.text();
    let data;
    try { data = JSON.parse(txt); } catch(e) {
      // CloudFront returned HTML — retry via direct API Gateway
      const resp2 = await fetch('https://mwdbfw44q0.execute-api.eu-west-1.amazonaws.com/api/opportunities/trade?slug='+encodeURIComponent(slug)+'&side='+side+'&price='+price, {method:'POST'});
      data = await resp2.json();
    }
    if (data.success) {
      btn.textContent = 'Filled $'+data.cost.toFixed(2);
      btn.style.background = '#22c55e';
      setTimeout(() => loadOpportunities(), 2000);
    } else {
      btn.textContent = 'Failed';
      btn.style.background = '#ef4444';
      alert('Trade failed: '+(data.error||'unknown'));
      setTimeout(() => { btn.textContent = 'Trade $2'; btn.disabled = false; btn.style.background = ''; }, 3000);
    }
  } catch(e) {
    btn.textContent = 'Error';
    alert('Error: '+e.message);
    setTimeout(() => { btn.textContent = 'Trade $2'; btn.disabled = false; btn.style.background = ''; }, 3000);
  }
}

// Sortable tables
document.addEventListener('click', function(e) {
  const th = e.target.closest('th[data-sort]');
  if (!th) return;
  const table = th.closest('table');
  const tbody = table.querySelector('tbody');
  const idx = Array.from(th.parentNode.children).indexOf(th);
  const type = th.dataset.sort; // "str" or "num"
  const isAsc = th.classList.contains('sort-asc');
  // Reset all headers in this table
  th.parentNode.querySelectorAll('th').forEach(h => h.classList.remove('sort-asc','sort-desc'));
  th.classList.add(isAsc ? 'sort-desc' : 'sort-asc');
  const dir = isAsc ? -1 : 1;
  const rows = Array.from(tbody.querySelectorAll('tr'));
  rows.sort((a, b) => {
    let av = a.children[idx]?.textContent?.trim() || '';
    let bv = b.children[idx]?.textContent?.trim() || '';
    if (type === 'num') {
      av = parseFloat(av.replace(/[^0-9.\-]/g, '')) || 0;
      bv = parseFloat(bv.replace(/[^0-9.\-]/g, '')) || 0;
      return (av - bv) * dir;
    }
    return av.localeCompare(bv) * dir;
  });
  rows.forEach(r => tbody.appendChild(r));
});

// Auto-refresh overview every 30s
refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard(_: str = Depends(_require_auth)):
    return HTML


if __name__ == "__main__":
    print("\n  Dashboard: http://localhost:8888\n")
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="warning")
