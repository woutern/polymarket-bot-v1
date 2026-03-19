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
        _session = _boto3.Session(profile_name="playground", region_name="us-east-1")
        _session.client("sts").get_caller_identity()  # validate credentials
    except Exception:
        _session = _boto3.Session(region_name="us-east-1")
    _ddb = _session.resource("dynamodb", region_name="us-east-1")
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
_TOTAL_DEPOSITED = float(_os.getenv("TOTAL_DEPOSITED", "265.72"))  # Total USDC ever deposited


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
        items.sort(key=lambda x: float(x.get("timestamp", 0)), reverse=True)
        if asset:
            items = [t for t in items if _extract_field(t, "asset", "").upper() == asset.upper()]
        if tf:
            items = [t for t in items if (tf == "15m") == ("15m" in _extract_field(t, "window_slug", ""))]
        return items[:limit]
    if _USE_SQLITE:
        sql = "SELECT * FROM trades WHERE 1=1"
        params = []
        if asset:
            sql += " AND UPPER(asset) = ?"
            params.append(asset.upper())
        if tf:
            if tf == "15m":
                sql += " AND window_slug LIKE '%15m%'"
            else:
                sql += " AND window_slug NOT LIKE '%15m%'"
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
def api_data(_: str = Depends(_require_auth)):
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

    # Per-asset window counts
    asset_windows = {}
    asset_windows_15m = {}
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
        if "15m" in slug:
            asset_windows_15m[asset] = asset_windows_15m.get(asset, 0) + 1
        else:
            asset_windows[asset] = asset_windows.get(asset, 0) + 1

    # Per-asset x timeframe breakdown
    strategy_pnl = {}
    for t in mode_trades:
        if not (t.get("resolved") or _bool_field(t, "resolved")):
            continue
        asset = _extract_field(t, "asset", "BTC").upper() or "BTC"
        slug = _extract_field(t, "window_slug", "")
        tf = "15m" if "15m" in slug else "5m"
        key = f"{asset} {tf}"
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
            "asset_windows_15m": asset_windows_15m,
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
    _: str = Depends(_require_auth),
):
    """Filtered, paginated trade list."""
    trades = get_trades(limit=limit, asset=asset, tf=tf)
    return {"trades": trades, "count": len(trades)}


@app.get("/api/signals")
def api_signals(
    asset: str = None,
    outcome: str = None,
    limit: int = 200,
    _: str = Depends(_require_auth),
):
    """Signal evaluations — both fired and rejected."""
    signals = get_signals(limit=limit, asset=asset, outcome=outcome)
    return {"signals": signals, "count": len(signals)}


@app.get("/api/signals/summary")
def api_signals_summary(_: str = Depends(_require_auth)):
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
def api_strategy_stats(_: str = Depends(_require_auth)):
    """Win rates by asset x timeframe x hour-of-day segment."""
    trades = get_trades(limit=500)
    resolved = [t for t in trades if t.get("resolved") or _bool_field(t, "resolved")]

    by_segment: dict[str, dict] = {}
    by_hour: dict[int, dict] = {}
    by_pct_move_bucket: dict[str, dict] = {}

    for t in resolved:
        asset = _extract_field(t, "asset", "BTC").upper() or "BTC"
        slug = _extract_field(t, "window_slug", "")
        tf = "15m" if "15m" in slug else "5m"
        seg_key = f"{asset} {tf}"
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
def api_calibration(_: str = Depends(_require_auth)):
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
def api_pairs(_: str = Depends(_require_auth)):
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
            "BTC 15m": 0.12, "ETH 15m": 0.14, "SOL 15m": 0.18,
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
        tf = "15m" if "15m" in slug else "5m"
        key = f"{asset} {tf}"
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
def api_kpi(_: str = Depends(_require_auth)):
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
def api_pnl_history(_: str = Depends(_require_auth)):
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
async def api_balance(_: str = Depends(_require_auth)):
    """Return wallet USDC balances."""
    try:
        if not _WALLET_ADDRESS:
            return {"polymarket_value": 0.0, "polygon_usdc": 0.0, "error": "no_address"}

        import httpx

        result = {"polygon_usdc": 0.0, "polymarket_value": 0.0, "total_pnl": 0.0, "unclaimed_winnings": 0.0}

        async with httpx.AsyncClient(timeout=10) as client:
            # On-chain USDC balance (optional — needs polybot module)
            try:
                from polybot.market.balance_checker import BalanceChecker
                checker = BalanceChecker()
                bal = await checker.check(_WALLET_ADDRESS)
                result["polygon_usdc"] = bal.get("polygon_usdc", 0.0)
            except Exception:
                pass

            # Polymarket portfolio value + P&L (source of truth)
            try:
                resp = await client.get(
                    "https://data-api.polymarket.com/value",
                    params={"user": _WALLET_ADDRESS},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, list) and data:
                        result["polymarket_value"] = float(data[0].get("value", 0) or 0)
            except Exception:
                pass

            # P&L = portfolio - total deposited (matches Polymarket UI)
            portfolio = result["polygon_usdc"] + result["polymarket_value"]
            result["total_pnl"] = round(portfolio - _TOTAL_DEPOSITED, 2)
            result["portfolio"] = round(portfolio, 2)

            # Unclaimed winnings from positions
            try:
                resp = await client.get(
                    "https://data-api.polymarket.com/positions",
                    params={"user": _WALLET_ADDRESS, "sizeThreshold": "0.01"},
                )
                if resp.status_code == 200:
                    positions = resp.json()
                    result["unclaimed_winnings"] = round(sum(
                        p.get("currentValue", 0) for p in positions
                        if isinstance(p, dict) and p.get("currentValue", 0) > 0.5
                    ), 2)
            except Exception:
                pass

        return result
    except Exception as e:
        return {"polymarket_value": 0.0, "polygon_usdc": 0.0, "error": str(e)}


# ── HTML dashboard ────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Bot — Trading Desk</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  :root {
    --bg:         #f8f9fa;
    --surface:    #ffffff;
    --surface-2:  #f1f3f5;
    --border:     #e9ecef;
    --border-2:   #dee2e6;
    --text:       #212529;
    --text-2:     #495057;
    --text-3:     #868e96;
    --green:      #2f9e44;
    --green-bg:   #ebfbee;
    --green-bd:   #b2f2bb;
    --red:        #c92a2a;
    --red-bg:     #fff5f5;
    --red-bd:     #ffc9c9;
    --blue:       #1971c2;
    --blue-bg:    #e7f5ff;
    --blue-bd:    #a5d8ff;
    --gold:       #e67700;
    --gold-bg:    #fff9db;
    --gold-bd:    #ffec99;
    --amber:      #d9480f;
    --amber-bg:   #fff4e6;
    --amber-bd:   #ffd8a8;
    --btc:        #f7931a;
    --btc-bg:     #fff4e6;
    --eth:        #627eea;
    --eth-bg:     #edf2ff;
    --sol:        #9945ff;
    --sol-bg:     #f3f0ff;
    --nav-bg:     #1a1b2e;
    --nav-text:   #c8ccd4;
    --shadow-sm:  0 1px 3px rgba(0,0,0,.06), 0 1px 2px rgba(0,0,0,.04);
    --shadow-md:  0 4px 12px rgba(0,0,0,.08), 0 2px 4px rgba(0,0,0,.04);
    --radius:     10px;
    --radius-sm:  6px;
  }

  body {
    background: var(--bg);
    color: var(--text);
    font-family: 'Inter', system-ui, -apple-system, sans-serif;
    font-size: 14px;
    line-height: 1.5;
    min-height: 100vh;
  }

  /* ── Refresh bar ── */
  #refresh-bar {
    height: 3px;
    background: var(--nav-bg);
    position: fixed;
    top: 0; left: 0; right: 0;
    z-index: 200;
  }
  #refresh-progress {
    height: 100%;
    background: linear-gradient(90deg, #1971c2, #339af0);
    width: 0%;
    transition: width linear;
  }

  /* ── Navbar (dark) ── */
  nav {
    background: var(--nav-bg);
    padding: 0 24px;
    height: 56px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: sticky;
    top: 0;
    z-index: 50;
  }
  .nav-brand {
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .nav-logo {
    width: 30px; height: 30px;
    background: linear-gradient(135deg, #1971c2, #339af0);
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-size: 15px; color: #fff; font-weight: 800;
  }
  .nav-title {
    font-size: 16px; font-weight: 700; color: #e8eaed; letter-spacing: -0.3px;
  }
  .nav-title span { color: #339af0; }
  .nav-tabs {
    display: flex;
    gap: 2px;
    background: rgba(255,255,255,.06);
    border: 1px solid rgba(255,255,255,.08);
    border-radius: 8px;
    padding: 3px;
  }
  .nav-tab {
    padding: 5px 14px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 600;
    color: var(--nav-text);
    cursor: pointer;
    transition: all .15s;
    border: none;
    background: none;
  }
  .nav-tab:hover { color: #e8eaed; background: rgba(255,255,255,.08); }
  .nav-tab.active {
    color: #fff;
    background: rgba(255,255,255,.12);
    box-shadow: 0 1px 4px rgba(0,0,0,.2);
  }
  .nav-right {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .nav-meta {
    font-size: 12px; color: #6b7280;
    display: flex; align-items: center; gap: 10px;
  }
  .nav-meta .sep { color: #3b3d50; }
  .status-dot {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 12px; font-weight: 600; color: var(--green);
    background: rgba(47,158,68,.15); border: 1px solid rgba(47,158,68,.3);
    padding: 3px 10px; border-radius: 20px;
  }
  .status-dot::before {
    content: ''; width: 7px; height: 7px;
    background: var(--green); border-radius: 50%;
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.6; transform: scale(0.85); }
  }

  /* ── Pages ── */
  .page-content { display: none; }
  .page-content.active { display: block; }
  .page { max-width: 1440px; margin: 0 auto; padding: 20px 24px 40px; }

  /* ── Stats row ── */
  .stats-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
    margin-bottom: 20px;
  }
  .stat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px 18px;
    box-shadow: var(--shadow-sm);
    transition: box-shadow .15s, transform .15s;
  }
  .stat-card:hover { box-shadow: var(--shadow-md); transform: translateY(-1px); }
  .stat-card.primary { border-left: 4px solid var(--gold); }
  .stat-label {
    font-size: 11px; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.8px; color: var(--text-3); margin-bottom: 8px;
  }
  .stat-value {
    font-size: 26px; font-weight: 800; letter-spacing: -0.5px;
    color: var(--text); line-height: 1;
  }
  .stat-value.green { color: var(--green); }
  .stat-value.red   { color: var(--red); }
  .stat-value.blue  { color: var(--blue); }
  .stat-value.gold  { color: var(--gold); }
  .stat-sub { font-size: 11px; color: var(--text-3); margin-top: 5px; font-weight: 500; }

  /* ── Section headers ── */
  .section-header {
    display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px;
  }
  .section-title {
    font-size: 13px; font-weight: 700; color: var(--text-2);
    text-transform: uppercase; letter-spacing: 0.6px;
  }
  .section-badge {
    font-size: 11px; font-weight: 600; color: var(--text-3);
    background: var(--surface-2); border: 1px solid var(--border);
    padding: 2px 8px; border-radius: 20px;
  }

  /* ── Strategy cards ── */
  .strategy-grid {
    display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 20px;
  }
  #strategy-section { display: contents; }
  .strat-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 14px 16px; box-shadow: var(--shadow-sm);
  }
  .strat-name {
    font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: 0.6px; color: var(--text-2); margin-bottom: 6px;
  }
  .strat-pnl {
    font-size: 18px; font-weight: 800; letter-spacing: -0.4px; margin-bottom: 6px;
  }
  .strat-meta { font-size: 11px; color: var(--text-3); margin-bottom: 6px; }
  .win-bar-wrap { height: 4px; background: var(--surface-2); border-radius: 3px; overflow: hidden; }
  .win-bar-fill {
    height: 100%; border-radius: 3px;
    background: linear-gradient(90deg, #2f9e44, #51cf66);
    transition: width .6s cubic-bezier(.4,0,.2,1);
  }

  /* ── Chart card ── */
  .chart-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 20px 22px;
    box-shadow: var(--shadow-sm); margin-bottom: 20px;
  }
  .chart-card .section-header { margin-bottom: 16px; }
  #pnl-chart-wrap { height: 180px; }

  /* ── Two-column panels ── */
  .panels-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px;
  }
  .panel-card {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); box-shadow: var(--shadow-sm); overflow: hidden;
  }
  .panel-head {
    padding: 12px 16px; border-bottom: 1px solid var(--border);
    background: var(--surface);
    display: flex; align-items: center; justify-content: space-between;
  }

  /* ── Tables ── */
  .scroll-wrap { max-height: 320px; overflow-y: auto; }
  table { width: 100%; border-collapse: collapse; }
  thead { position: sticky; top: 0; z-index: 1; }
  th {
    text-align: left; font-size: 11px; font-weight: 600;
    color: var(--text-3); text-transform: uppercase; letter-spacing: 0.5px;
    padding: 9px 12px; background: var(--surface-2);
    border-bottom: 1px solid var(--border); white-space: nowrap;
  }
  td {
    padding: 8px 12px; border-bottom: 1px solid var(--border);
    font-size: 12px; color: var(--text-2); vertical-align: middle;
  }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover td { background: var(--surface-2); }
  .empty-row td {
    color: var(--text-3); text-align: center; padding: 28px 16px;
    font-style: italic; font-size: 12px;
  }
  /* Signal table row highlighting */
  tr.signal-executed td { background: rgba(47,158,68,.04); }
  tr.signal-rejected td { background: rgba(230,119,0,.04); }

  /* ── Tags ── */
  .tag {
    display: inline-block; padding: 2px 7px; border-radius: 4px;
    font-size: 10px; font-weight: 700; letter-spacing: 0.3px; white-space: nowrap;
  }
  .tag-up    { background: var(--green-bg); color: var(--green); border: 1px solid var(--green-bd); }
  .tag-down  { background: var(--red-bg);   color: var(--red);   border: 1px solid var(--red-bd); }
  .tag-open  { background: var(--blue-bg);  color: var(--blue);  border: 1px solid var(--blue-bd); }
  .tag-warn  { background: var(--gold-bg);  color: var(--gold);  border: 1px solid var(--gold-bd); }
  .tag-btc   { background: var(--btc-bg);   color: var(--btc);   border: 1px solid #ffd8a8; }
  .tag-eth   { background: var(--eth-bg);   color: var(--eth);   border: 1px solid #bac8ff; }
  .tag-sol   { background: var(--sol-bg);   color: var(--sol);   border: 1px solid #d0bfff; }

  /* ── Outcome badges ── */
  .outcome-win-pm     { color: var(--green); font-weight: 700; }
  .outcome-win-cb     { color: var(--gold);  font-weight: 700; }
  .outcome-loss-pm    { color: var(--red);   font-weight: 700; }
  .outcome-loss-cb    { color: var(--red);   font-weight: 700; }
  .outcome-open       { color: var(--blue);  font-weight: 600; }

  /* ── Rejection reason badges ── */
  .reason-badge {
    display: inline-block; padding: 2px 7px; border-radius: 4px;
    font-size: 10px; font-weight: 700; letter-spacing: 0.3px; white-space: nowrap;
  }
  .reason-min_move       { background: #f1f3f5; color: #868e96; border: 1px solid #dee2e6; }
  .reason-market_efficient { background: var(--gold-bg); color: var(--gold); border: 1px solid var(--gold-bd); }
  .reason-insufficient_ev { background: var(--amber-bg); color: var(--amber); border: 1px solid var(--amber-bd); }
  .reason-obi_veto       { background: var(--red-bg);  color: var(--red);  border: 1px solid var(--red-bd); }
  .reason-unrealistic_price { background: #343a40; color: #adb5bd; border: 1px solid #495057; }

  /* ── Logs ── */
  .logs-card { background: #1a1b26; border: 1px solid #2a2b3d; border-radius: var(--radius); overflow: hidden; }
  .logs-head {
    padding: 12px 16px; border-bottom: 1px solid #2a2b3d; background: #16172a;
    display: flex; align-items: center; justify-content: space-between;
  }
  .logs-head .section-title { color: #a9b1d6; }
  .logs-head .section-badge { background: #2a2b3d; border-color: #3a3b4d; color: #565f89; }
  #logs {
    background: #1a1b26; padding: 12px 16px; max-height: 220px; overflow-y: auto;
    font-family: 'JetBrains Mono', 'Fira Code', 'SF Mono', ui-monospace, monospace;
    font-size: 11.5px; line-height: 1.9;
  }
  .log-line { white-space: pre-wrap; word-break: break-all; padding-left: 10px; border-left: 2px solid transparent; }
  .log-line.error  { color: #f7768e; border-left-color: #f7768e; }
  .log-line.warn   { color: #e0af68; border-left-color: #e0af68; }
  .log-line.trade  { color: #9ece6a; border-left-color: #9ece6a; background: rgba(158,206,106,.04); }
  .log-line.signal { color: #bb9af7; border-left-color: #bb9af7; }
  .log-line.entry  { color: #7aa2f7; border-left-color: #3d59a1; }
  .log-line.window { color: #565f89; }
  .log-line.info   { color: #3b4261; }

  /* ── Filter bar ── */
  .filter-bar {
    display: flex; gap: 8px; align-items: center;
  }
  .filter-select {
    font-size: 12px; padding: 5px 10px;
    border: 1px solid var(--border); border-radius: 6px;
    background: var(--surface); color: var(--text-2);
    font-family: inherit; font-weight: 500;
  }
  .filter-select:focus { outline: none; border-color: var(--blue); }

  /* ── Signal funnel ── */
  .funnel-bar-wrap {
    display: flex; align-items: center; gap: 12px; margin-bottom: 8px;
  }
  .funnel-label {
    font-size: 12px; font-weight: 600; color: var(--text-2); min-width: 140px; text-align: right;
  }
  .funnel-bar {
    height: 24px; border-radius: 4px; min-width: 2px;
    display: flex; align-items: center; padding-left: 8px;
    font-size: 11px; font-weight: 700; color: #fff;
    transition: width .6s cubic-bezier(.4,0,.2,1);
  }
  .funnel-count {
    font-size: 12px; font-weight: 600; color: var(--text-3); margin-left: 8px;
  }

  /* ── Analytics ── */
  .analytics-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px;
  }
  .analytics-grid.wide { grid-template-columns: 1fr; }

  /* ── Responsive ── */
  @media (max-width: 1200px) {
    .stats-grid { grid-template-columns: repeat(2, 1fr); }
    .strategy-grid { grid-template-columns: repeat(3, 1fr); }
  }
  /* ── Hamburger menu (mobile) ── */
  .hamburger {
    display: none;
    background: none; border: none; cursor: pointer; padding: 6px;
    color: var(--nav-text);
  }
  .hamburger svg { width: 24px; height: 24px; }
  .mobile-menu {
    display: none;
    position: fixed;
    top: 56px; left: 0; right: 0;
    background: var(--nav-bg);
    border-bottom: 1px solid rgba(255,255,255,.1);
    padding: 8px 16px 12px;
    z-index: 49;
    flex-direction: column;
    gap: 4px;
  }
  .mobile-menu.open { display: flex; }
  .mobile-menu button {
    width: 100%;
    padding: 10px 14px;
    border-radius: 8px;
    font-size: 14px;
    font-weight: 600;
    color: var(--nav-text);
    cursor: pointer;
    border: none;
    background: none;
    text-align: left;
    transition: all .15s;
  }
  .mobile-menu button:hover { color: #e8eaed; background: rgba(255,255,255,.08); }
  .mobile-menu button.active {
    color: #fff;
    background: rgba(255,255,255,.12);
  }

  @media (max-width: 768px) {
    .page { padding: 12px 16px 32px; }
    nav { padding: 0 16px; }
    .stats-grid { grid-template-columns: repeat(2, 1fr); }
    .strategy-grid { grid-template-columns: repeat(2, 1fr); }
    .panels-grid, .analytics-grid { grid-template-columns: 1fr; }
    .nav-meta { display: none; }
    .stat-value { font-size: 22px; }
    .nav-tabs { display: none; }
    .hamburger { display: block; }
  }
</style>
</head>
<body>

<div id="refresh-bar"><div id="refresh-progress"></div></div>

<nav>
  <div class="nav-brand">
    <div class="nav-logo">P</div>
    <span class="nav-title">Polymarket <span>Bot</span></span>
  </div>
  <div class="nav-tabs">
    <button class="nav-tab active" data-page="overview" onclick="showPage('overview', this)">Overview</button>
    <button class="nav-tab" data-page="tradelog" onclick="showPage('tradelog', this)">Trade Log</button>
    <button class="nav-tab" data-page="signals" onclick="showPage('signals', this)">Signals</button>
    <button class="nav-tab" data-page="analytics" onclick="showPage('analytics', this)">Analytics</button>
    <button class="nav-tab" data-page="kpis" onclick="showPage('kpis', this)">KPIs</button>
  </div>
  <div class="nav-right">
    <div class="nav-meta">
      <span>us-east-1</span>
      <span class="sep">|</span>
      <span>BTC &middot; ETH &middot; SOL</span>
      <span class="sep">|</span>
      <span>Updated: <strong id="last-update">&mdash;</strong></span>
    </div>
    <div class="status-dot" id="mode-badge">PAPER</div>
    <button class="hamburger" onclick="toggleMobileMenu()" aria-label="Menu">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
        <line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/>
      </svg>
    </button>
  </div>
</nav>
<div class="mobile-menu" id="mobile-menu">
  <button class="active" onclick="showPageMobile('overview', this)">Overview</button>
  <button onclick="showPageMobile('tradelog', this)">Trade Log</button>
  <button onclick="showPageMobile('signals', this)">Signals</button>
  <button onclick="showPageMobile('analytics', this)">Analytics</button>
  <button onclick="showPageMobile('kpis', this)">KPIs</button>
</div>

<!-- ======================================================================= -->
<!-- PAGE 1: OVERVIEW                                                        -->
<!-- ======================================================================= -->
<div id="page-overview" class="page-content active">
<div class="page">

  <!-- Stats row -->
  <div class="stats-grid">
    <div class="stat-card primary">
      <div class="stat-label" id="s-balance-label">Wallet Balance</div>
      <div class="stat-value gold" id="s-balance">&mdash;</div>
      <div class="stat-sub" id="s-balance-sub">USDC on Polymarket</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Total P&amp;L</div>
      <div class="stat-value" id="s-pnl">&mdash;</div>
      <div class="stat-sub" id="s-pnl-sub">since reset</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Win / Loss</div>
      <div class="stat-value" id="s-wl">&mdash;</div>
      <div class="stat-sub" id="s-wl-sub">resolved trades</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Open Positions</div>
      <div class="stat-value blue" id="s-open">&mdash;</div>
      <div class="stat-sub" id="s-open-sub"></div>
    </div>
  </div>

  <!-- Strategy cards -->
  <div class="section-header">
    <span class="section-title">Strategy Performance</span>
  </div>
  <div class="strategy-grid">
    <div id="strategy-section"></div>
  </div>

  <!-- Cumulative P&L chart -->
  <div class="chart-card">
    <div class="section-header">
      <span class="section-title">Cumulative P&amp;L &mdash; Equity Curve</span>
      <span class="section-badge" id="chart-badge">Loading...</span>
    </div>
    <div id="pnl-chart-wrap"><canvas id="pnl-chart"></canvas></div>
  </div>

  <!-- Trades + Signals tables -->
  <div class="panels-grid">
    <div class="panel-card">
      <div class="panel-head">
        <span class="section-title">Recent Trades</span>
        <span class="section-badge" id="trade-count"></span>
      </div>
      <div class="scroll-wrap">
        <table>
          <thead><tr>
            <th>Time</th><th>Asset</th><th>Side</th>
            <th>Price</th><th>Size</th><th>P&amp;L</th><th>Outcome</th>
          </tr></thead>
          <tbody id="trades-body"></tbody>
        </table>
      </div>
    </div>
    <div class="panel-card">
      <div class="panel-head">
        <span class="section-title">Recent Signals</span>
        <span class="section-badge" id="signal-count-overview"></span>
      </div>
      <div class="scroll-wrap">
        <table>
          <thead><tr>
            <th>Time</th><th>Asset</th><th>Dir</th><th>Move%</th><th>EV</th><th>Status</th>
          </tr></thead>
          <tbody id="signals-overview-body"></tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Live logs -->
  <div class="logs-card">
    <div class="logs-head">
      <span class="section-title">Live Logs</span>
      <span class="section-badge" id="log-count"></span>
    </div>
    <div id="logs"></div>
  </div>

</div>
</div>

<!-- ======================================================================= -->
<!-- PAGE 2: TRADE LOG                                                       -->
<!-- ======================================================================= -->
<div id="page-tradelog" class="page-content">
<div class="page">

  <div class="section-header">
    <span class="section-title">Trade Log</span>
    <div class="filter-bar">
      <select id="tl-asset" onchange="loadTradeLog()" class="filter-select">
        <option value="">All assets</option>
        <option value="BTC">BTC</option>
        <option value="ETH">ETH</option>
        <option value="SOL">SOL</option>
      </select>
      <select id="tl-tf" onchange="loadTradeLog()" class="filter-select">
        <option value="">All timeframes</option>
        <option value="5m">5m</option>
        <option value="15m">15m</option>
      </select>
    </div>
  </div>

  <div class="panel-card">
    <div style="overflow-x:auto">
      <table>
        <thead><tr>
          <th>Time</th><th>Asset</th><th>TF</th><th>Dir</th><th>Side</th>
          <th>Fill</th><th>Size</th><th>P(bay)</th><th>P(AI)</th><th>P(final)</th>
          <th>EV</th><th>Move%</th><th>T-left</th><th>P&amp;L</th><th>Outcome</th>
          <th>Sig ms</th><th>Ord ms</th><th>BR ms</th>
        </tr></thead>
        <tbody id="tl-body">
          <tr class="empty-row"><td colspan="18">Loading...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

</div>
</div>

<!-- ======================================================================= -->
<!-- PAGE 3: SIGNALS                                                         -->
<!-- ======================================================================= -->
<div id="page-signals" class="page-content">
<div class="page">

  <!-- Signal funnel -->
  <div class="section-header">
    <span class="section-title">Signal Funnel</span>
    <span class="section-badge" id="funnel-badge">Loading...</span>
  </div>
  <div class="panel-card" style="padding:20px;margin-bottom:20px">
    <div id="funnel-bars"></div>
  </div>

  <!-- Rejection breakdown + near-miss -->
  <div class="analytics-grid" style="margin-bottom:20px">
    <div class="panel-card">
      <div class="panel-head">
        <span class="section-title">Rejection Breakdown</span>
      </div>
      <div style="padding:16px;height:250px">
        <canvas id="rejection-donut"></canvas>
      </div>
    </div>
    <div class="panel-card">
      <div class="panel-head">
        <span class="section-title">Near Misses (EV within 2% of threshold)</span>
      </div>
      <div class="scroll-wrap" style="max-height:250px">
        <table>
          <thead><tr>
            <th>Time</th><th>Asset</th><th>Dir</th><th>Move%</th><th>EV</th><th>P(model)</th>
          </tr></thead>
          <tbody id="near-miss-body">
            <tr class="empty-row"><td colspan="6">Loading...</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <!-- Signal table -->
  <div class="section-header">
    <span class="section-title">All Signal Evaluations</span>
    <div class="filter-bar">
      <select id="sig-asset" onchange="loadSignals()" class="filter-select">
        <option value="">All assets</option>
        <option value="BTC">BTC</option>
        <option value="ETH">ETH</option>
        <option value="SOL">SOL</option>
      </select>
      <select id="sig-outcome" onchange="loadSignals()" class="filter-select">
        <option value="">All outcomes</option>
        <option value="executed">Executed</option>
        <option value="rejected">Rejected</option>
      </select>
    </div>
  </div>
  <div class="panel-card">
    <div style="overflow-x:auto">
      <table>
        <thead><tr>
          <th>Time</th><th>Asset</th><th>TF</th><th>Dir</th>
          <th>Move%</th><th>P(model)</th><th>Mkt Price</th><th>EV</th>
          <th>p(bay)</th><th>Sec Left</th><th>YES Ask</th><th>NO Ask</th>
          <th>Status</th><th>Reason</th>
        </tr></thead>
        <tbody id="sig-body">
          <tr class="empty-row"><td colspan="14">Loading...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

</div>
</div>

<!-- ======================================================================= -->
<!-- PAGE 4: ANALYTICS                                                       -->
<!-- ======================================================================= -->
<div id="page-analytics" class="page-content">
<div class="page">

  <!-- Per-pair strategy config -->
  <div class="section-header" style="margin-bottom:12px">
    <span class="section-title">Pair Configuration &amp; Performance</span>
    <span class="section-badge" id="pairs-badge">Loading...</span>
  </div>
  <div class="panel-card" style="margin-bottom:20px">
    <div style="overflow-x:auto">
      <table>
        <thead><tr>
          <th>Pair</th><th>Min Move</th><th>Min EV</th><th>Max Price</th>
          <th>Entry</th><th>Trades</th><th>Win Rate</th><th>P&amp;L</th>
        </tr></thead>
        <tbody id="pairs-body"><tr class="empty-row"><td colspan="8">Loading...</td></tr></tbody>
      </table>
    </div>
  </div>

  <div class="analytics-grid">
    <!-- By hour of day chart -->
    <div class="panel-card">
      <div class="panel-head"><span class="section-title">Win Rate by Hour (UTC)</span></div>
      <div style="padding:16px 16px 8px">
        <canvas id="hour-chart" height="180"></canvas>
      </div>
    </div>

    <!-- Model calibration -->
    <div class="panel-card">
      <div class="panel-head"><span class="section-title">Model Calibration (p_final vs Actual WR)</span></div>
      <div style="padding:16px;height:260px">
        <canvas id="calibration-chart"></canvas>
      </div>
    </div>
  </div>

  <div class="analytics-grid">
    <!-- Win rate by segment -->
    <div class="panel-card">
      <div class="panel-head"><span class="section-title">Win Rate by Segment</span></div>
      <div style="overflow-y:auto;max-height:280px">
        <table>
          <thead><tr><th>Segment</th><th>Trades</th><th>Win Rate</th><th>P&amp;L</th></tr></thead>
          <tbody id="seg-body"><tr class="empty-row"><td colspan="4">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- Latency distribution -->
    <div class="panel-card">
      <div class="panel-head"><span class="section-title">Latency Distribution</span></div>
      <div style="padding:16px;height:260px">
        <canvas id="latency-chart"></canvas>
      </div>
    </div>
  </div>

</div>
</div>

<!-- ══════════════════════════ PAGE 5: KPIs ══════════════════════════ -->
<div id="page-kpis" class="page-content">
<div class="page">

  <!-- Edge Score -->
  <div class="stat-card" style="text-align:center;padding:24px;margin-bottom:20px">
    <div class="stat-label">THE EDGE SCORE (Brier Skill Score)</div>
    <div class="stat-value" id="kpi-bss" style="font-size:36px">&mdash;</div>
    <div class="stat-sub" id="kpi-bss-sub">Collecting data...</div>
  </div>

  <!-- SPRT + Win Rate -->
  <div class="panels-grid">
    <div class="panel-card">
      <div class="panel-head"><span class="section-title">SPRT Edge Detection</span></div>
      <div style="padding:16px">
        <div id="kpi-sprt-status" style="font-size:18px;font-weight:700;margin-bottom:8px">ACCUMULATING</div>
        <div id="kpi-sprt-lambda" style="font-size:13px;color:var(--text-3)">log(&Lambda;) = 0.000</div>
        <div id="kpi-sprt-trades" style="font-size:13px;color:var(--text-3)">Trades to significance: ~400</div>
      </div>
    </div>
    <div class="panel-card">
      <div class="panel-head"><span class="section-title">Win Rate</span></div>
      <div style="padding:16px;display:flex;gap:20px">
        <div style="text-align:center;flex:1">
          <div style="font-size:11px;color:var(--text-3)">Last 20</div>
          <div id="kpi-wr20" style="font-size:24px;font-weight:800">&mdash;</div>
        </div>
        <div style="text-align:center;flex:1">
          <div style="font-size:11px;color:var(--text-3)">Last 50</div>
          <div id="kpi-wr50" style="font-size:24px;font-weight:800">&mdash;</div>
        </div>
        <div style="text-align:center;flex:1">
          <div style="font-size:11px;color:var(--text-3)">All Time</div>
          <div id="kpi-wrall" style="font-size:24px;font-weight:800">&mdash;</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Model + Risk -->
  <div class="panels-grid">
    <div class="panel-card">
      <div class="panel-head"><span class="section-title">Model Intelligence</span></div>
      <div style="padding:16px">
        <div style="display:flex;gap:20px;margin-bottom:12px">
          <div style="flex:1;text-align:center">
            <div style="font-size:11px;color:var(--text-3)">Avg prob (wins)</div>
            <div id="kpi-prob-wins" style="font-size:20px;font-weight:700;color:var(--green)">&mdash;</div>
          </div>
          <div style="flex:1;text-align:center">
            <div style="font-size:11px;color:var(--text-3)">Avg prob (losses)</div>
            <div id="kpi-prob-losses" style="font-size:20px;font-weight:700;color:var(--red)">&mdash;</div>
          </div>
        </div>
        <div style="text-align:center">
          <div style="font-size:11px;color:var(--text-3)">Separation (target &gt;0.10)</div>
          <div id="kpi-separation" style="font-size:18px;font-weight:700">&mdash;</div>
        </div>
      </div>
    </div>
    <div class="panel-card">
      <div class="panel-head"><span class="section-title">Risk</span></div>
      <div style="padding:16px;display:flex;gap:20px">
        <div style="text-align:center;flex:1">
          <div style="font-size:11px;color:var(--text-3)">Sharpe</div>
          <div id="kpi-sharpe" style="font-size:20px;font-weight:700">&mdash;</div>
        </div>
        <div style="text-align:center;flex:1">
          <div style="font-size:11px;color:var(--text-3)">Max DD</div>
          <div id="kpi-dd" style="font-size:20px;font-weight:700">&mdash;</div>
        </div>
        <div style="text-align:center;flex:1">
          <div style="font-size:11px;color:var(--text-3)">Today P&amp;L</div>
          <div id="kpi-daily" style="font-size:20px;font-weight:700">&mdash;</div>
        </div>
      </div>
    </div>
  </div>

  <!-- Per Pair -->
  <div class="panel-card">
    <div class="panel-head"><span class="section-title">Per Pair Breakdown</span></div>
    <div style="overflow-x:auto">
      <table>
        <thead><tr><th>Pair</th><th>Trades</th><th>Win Rate</th><th>Avg Entry</th><th>Avg LightGBM</th><th>P&amp;L</th><th>SPRT</th></tr></thead>
        <tbody id="kpi-pairs-body"><tr class="empty-row"><td colspan="7">Collecting data...</td></tr></tbody>
      </table>
    </div>
  </div>

</div>
</div>

<script>
// ── State ─────────────────────────────────────────────────────────────────────
let currentPage = 'overview';
let pnlChart = null;
let hourChart = null;
let rejectionDonut = null;
let calibrationChart = null;
let latencyChart = null;
let lastBalanceFetch = 0;

const OVERVIEW_REFRESH_MS = 4000;
const OTHER_REFRESH_MS = 10000;
let refreshInterval = null;

// ── Page navigation ───────────────────────────────────────────────────────────
function toggleMobileMenu() {
  document.getElementById('mobile-menu').classList.toggle('open');
}
function showPageMobile(name, btn) {
  // Update mobile menu active state
  document.querySelectorAll('.mobile-menu button').forEach(b => b.classList.remove('active'));
  if (btn) btn.classList.add('active');
  // Close menu
  document.getElementById('mobile-menu').classList.remove('open');
  // Delegate to main showPage (also update desktop tabs)
  const desktopBtn = document.querySelector(`.nav-tab[data-page="${name}"]`);
  showPage(name, desktopBtn);
}
function showPage(name, btn) {
  currentPage = name;
  document.querySelectorAll('.page-content').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  if (btn) btn.classList.add('active');

  clearInterval(refreshInterval);
  if (name === 'overview') {
    refreshOverview();
    refreshInterval = setInterval(refreshOverview, OVERVIEW_REFRESH_MS);
    animateBar(OVERVIEW_REFRESH_MS);
  } else if (name === 'tradelog') {
    loadTradeLog();
    refreshInterval = setInterval(loadTradeLog, OTHER_REFRESH_MS);
    animateBar(OTHER_REFRESH_MS);
  } else if (name === 'signals') {
    loadSignalsPage();
    refreshInterval = setInterval(loadSignalsPage, OTHER_REFRESH_MS);
    animateBar(OTHER_REFRESH_MS);
  } else if (name === 'analytics') {
    loadAnalytics();
    refreshInterval = setInterval(loadAnalytics, OTHER_REFRESH_MS);
    animateBar(OTHER_REFRESH_MS);
  } else if (name === 'kpis') {
    loadKPIs();
    refreshInterval = setInterval(loadKPIs, 30000);
    animateBar(30000);
  }
}

async function loadKPIs() {
  try {
    const resp = await fetch('/api/kpi');
    const k = await resp.json();
    if (k.status === 'no_data') {
      document.getElementById('kpi-bss-sub').textContent = 'No KPI data yet — waiting for resolved trades';
      return;
    }

    // Edge score
    const bss = k.brier_skill_score || 0;
    const bssEl = document.getElementById('kpi-bss');
    bssEl.textContent = (bss >= 0 ? '+' : '') + (bss * 100).toFixed(1) + '%';
    bssEl.style.color = bss >= 0 ? '#2f9e44' : '#c92a2a';
    document.getElementById('kpi-bss-sub').textContent =
      bss >= 0 ? 'You are ' + (bss*100).toFixed(1) + '% better than the market' : 'Market is ' + (-bss*100).toFixed(1) + '% better than you';

    // SPRT
    const sprt = k.sprt_status || 'ACCUMULATING';
    const sprtEl = document.getElementById('kpi-sprt-status');
    sprtEl.textContent = sprt;
    sprtEl.style.color = sprt === 'EDGE_CONFIRMED' ? '#2f9e44' : sprt === 'REASSESS' ? '#c92a2a' : '#868e96';
    document.getElementById('kpi-sprt-lambda').textContent = 'log(\u039B) = ' + (k.sprt_log_lambda || 0).toFixed(4);
    document.getElementById('kpi-sprt-trades').textContent = 'Trades to significance: ~' + (k.trades_to_significance || '?');

    // Win rates
    const wrColor = (v) => v >= 0.70 ? '#2f9e44' : v >= 0.60 ? '#e67700' : '#c92a2a';
    const wr20 = k.win_rate_last_20 || 0;
    const wr50 = k.win_rate_last_50 || 0;
    const wrAll = k.win_rate_total || 0;
    document.getElementById('kpi-wr20').textContent = (wr20*100).toFixed(0) + '%';
    document.getElementById('kpi-wr20').style.color = wrColor(wr20);
    document.getElementById('kpi-wr50').textContent = (wr50*100).toFixed(0) + '%';
    document.getElementById('kpi-wr50').style.color = wrColor(wr50);
    document.getElementById('kpi-wrall').textContent = (wrAll*100).toFixed(0) + '%';
    document.getElementById('kpi-wrall').style.color = wrColor(wrAll);

    // Model
    document.getElementById('kpi-prob-wins').textContent = (k.lgbm_avg_prob_wins || 0).toFixed(3);
    document.getElementById('kpi-prob-losses').textContent = (k.lgbm_avg_prob_losses || 0).toFixed(3);
    const sep = k.lgbm_separation || 0;
    document.getElementById('kpi-separation').textContent = (sep >= 0 ? '+' : '') + sep.toFixed(4);
    document.getElementById('kpi-separation').style.color = sep >= 0.10 ? '#2f9e44' : sep >= 0.05 ? '#e67700' : '#c92a2a';

    // Risk
    document.getElementById('kpi-sharpe').textContent = (k.sharpe_ratio || 0).toFixed(2);
    document.getElementById('kpi-dd').textContent = '$' + (k.max_drawdown || 0).toFixed(2);
    const daily = k.daily_pnl_today || 0;
    document.getElementById('kpi-daily').textContent = (daily >= 0 ? '+' : '') + '$' + daily.toFixed(2);
    document.getElementById('kpi-daily').style.color = daily >= 0 ? '#2f9e44' : '#c92a2a';

    // Per pair
    const pairs = k.pair_stats || {};
    const tbody = document.getElementById('kpi-pairs-body');
    tbody.innerHTML = '';
    for (const [pair, ps] of Object.entries(pairs)) {
      const wr = ps.win_rate || 0;
      const pnlColor = ps.total_pnl >= 0 ? '#2f9e44' : '#c92a2a';
      const sprtBadge = ps.sprt_status === 'EDGE_CONFIRMED' ? '<span style=\"color:#2f9e44\">CONFIRMED</span>'
        : ps.sprt_status === 'REASSESS' ? '<span style=\"color:#c92a2a\">REASSESS</span>'
        : '<span style=\"color:#868e96\">accumulating</span>';
      tbody.innerHTML += '<tr>' +
        '<td><strong>' + pair + '</strong></td>' +
        '<td>' + (ps.trades || 0) + '</td>' +
        '<td style=\"color:' + wrColor(wr) + ';font-weight:700\">' + (wr*100).toFixed(0) + '%</td>' +
        '<td>$' + (ps.avg_entry || 0).toFixed(2) + '</td>' +
        '<td>' + (ps.avg_lgbm_prob || 0).toFixed(3) + '</td>' +
        '<td style=\"color:' + pnlColor + ';font-weight:600\">' + (ps.total_pnl >= 0 ? '+' : '') + '$' + (ps.total_pnl || 0).toFixed(2) + '</td>' +
        '<td>' + sprtBadge + '</td>' +
        '</tr>';
    }
    if (!Object.keys(pairs).length) {
      tbody.innerHTML = '<tr class=\"empty-row\"><td colspan=\"7\">No pair data yet</td></tr>';
    }
  } catch(e) { console.error('kpi error', e); }
}

// ── Refresh bar ───────────────────────────────────────────────────────────────
function animateBar(ms) {
  const bar = document.getElementById('refresh-progress');
  bar.style.transition = 'none';
  bar.style.width = '0%';
  setTimeout(() => {
    bar.style.transition = `width ${ms}ms linear`;
    bar.style.width = '100%';
  }, 50);
}

// ── Chart.js cumulative P&L ───────────────────────────────────────────────────
function initPnlChart() {
  const ctx = document.getElementById('pnl-chart').getContext('2d');
  const gradient = ctx.createLinearGradient(0, 0, 0, 180);
  gradient.addColorStop(0,   'rgba(47,158,68,.26)');
  gradient.addColorStop(0.6, 'rgba(47,158,68,.04)');
  gradient.addColorStop(1,   'rgba(47,158,68,0)');

  pnlChart = new Chart(ctx, {
    type: 'line',
    data: {
      labels: [],
      datasets: [{
        label: 'Cumulative P&L ($)',
        data: [],
        borderColor: '#2f9e44',
        borderWidth: 2,
        backgroundColor: gradient,
        pointRadius: 3,
        tension: 0.4,
        fill: true,
      }],
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#fff', borderColor: '#dee2e6', borderWidth: 1,
          titleColor: '#868e96', bodyColor: '#212529', padding: 10,
          callbacks: { label: ctx => (ctx.raw >= 0 ? '+' : '') + '$' + ctx.raw.toFixed(4) },
        },
      },
      scales: {
        x: { ticks: { color: '#868e96', font: { size: 10 }, maxTicksLimit: 8 }, grid: { color: '#f1f3f5' } },
        y: { ticks: { color: '#868e96', font: { size: 10 }, callback: v => (v>=0?'+':'')+'$'+v.toFixed(2) }, grid: { color: '#f1f3f5' } },
      },
    },
  });
}

async function refreshPnlChart() {
  try {
    const resp = await fetch('/api/pnl-history');
    const data = await resp.json();
    if (!pnlChart) return;
    const cumulative = [];
    let running = 0;
    for (const v of data.values) { running += v; cumulative.push(parseFloat(running.toFixed(4))); }
    pnlChart.data.labels = data.labels.map(l => l.substring(5).replace('T', ' '));
    pnlChart.data.datasets[0].data = cumulative;
    const finalVal = cumulative.length ? cumulative[cumulative.length - 1] : 0;
    const posColor = finalVal >= 0 ? '#2f9e44' : '#c92a2a';
    pnlChart.data.datasets[0].borderColor = posColor;
    pnlChart.data.datasets[0].pointBackgroundColor = posColor;
    pnlChart.update('none');
    document.getElementById('chart-badge').textContent = data.labels.length + ' hourly buckets';
  } catch(e) { console.error('pnl chart error', e); }
}

// ── Balance ───────────────────────────────────────────────────────────────────
async function refreshBalance() {
  if (Date.now() - lastBalanceFetch < 30000) return;
  lastBalanceFetch = Date.now();
  try {
    const mode = (window._tradeMode || 'paper').toUpperCase();
    if (mode === 'PAPER') {
      // Paper mode: show virtual bankroll
      document.getElementById('s-balance-label').textContent = 'Virtual Bankroll';
      document.getElementById('s-balance').textContent = '$' + (window._bankroll || 1000).toFixed(2);
      document.getElementById('s-balance-sub').textContent = 'Paper trading (not real money)';
      return;
    }
    const resp = await fetch('/api/balance');
    const d = await resp.json();
    const wallet = (d.polygon_usdc || 0) + (d.polymarket_value || 0);
    document.getElementById('s-balance-label').textContent = 'Wallet Balance';
    document.getElementById('s-balance').textContent = '$' + wallet.toFixed(2);
    const unclaimed = d.unclaimed_winnings || 0;
    let subText = 'Cash $' + (d.polygon_usdc||0).toFixed(2) + ' + positions $' + (d.polymarket_value||0).toFixed(2);
    if (unclaimed > 0.5) {
      subText += ' | $' + unclaimed.toFixed(2) + ' unclaimed';
    }
    document.getElementById('s-balance-sub').textContent = subText;
  } catch(e) {}
}

// ── Formatters ────────────────────────────────────────────────────────────────
function assetTag(a) {
  const m = { BTC: 'tag-btc', ETH: 'tag-eth', SOL: 'tag-sol' };
  return '<span class="tag ' + (m[a]||'') + '">' + (a||'?') + '</span>';
}
function dirTag(d) {
  if (!d) return '&mdash;';
  const up = d === 'YES' || d === 'up' || d === 'UP';
  return '<span class="tag ' + (up ? 'tag-up' : 'tag-down') + '">' + d + '</span>';
}
function outcomeTag(t) {
  const src = dval(t, 'outcome_source') || 'coinbase_inferred';
  const correct = t.correct_prediction;
  const resolved = t.resolved || dval(t, 'resolved');
  const pnl = parseFloat(dval(t, 'pnl') || 0);

  if (!resolved) return '<span class="tag tag-open">OPEN</span>';
  if (src === 'polymarket_verified') {
    const won = correct == 1 || correct === true;
    if (won) return '<span class="outcome-win-pm">&#10003; WIN <span style="font-size:10px;color:var(--text-3)">(PM)</span></span>';
    return '<span class="outcome-loss-pm">&#10007; LOSS <span style="font-size:10px;color:var(--text-3)">(PM)</span></span>';
  }
  const won = pnl > 0;
  if (won) return '<span class="outcome-win-cb">&#10003; WIN <span style="font-size:10px">&#9888;</span></span>';
  return '<span class="outcome-loss-cb">&#10007; LOSS <span style="font-size:10px">&#9888;</span></span>';
}
function reasonBadge(reason) {
  if (!reason) return '&mdash;';
  const cls = 'reason-' + reason;
  const labels = {
    'min_move': 'min_move',
    'market_efficient': 'market_efficient',
    'insufficient_ev': 'insufficient_ev',
    'obi_veto': 'obi_veto',
    'unrealistic_price': 'unrealistic_price',
  };
  return '<span class="reason-badge ' + cls + '">' + (labels[reason] || reason) + '</span>';
}
function fmtTs(ts)  { return ts ? new Date(parseFloat(ts)*1000).toLocaleTimeString('en-GB', {timeZone: 'Europe/Amsterdam', hour12: false}) : '&mdash;'; }
function fmtTs2(ts) { return ts ? new Date(parseInt(ts)*1000).toLocaleTimeString() : '&mdash;'; }
function fmtPnl(p) {
  if (p == null || p === '') return '&mdash;';
  const v = parseFloat(p);
  const c = v >= 0 ? '#2f9e44' : '#c92a2a';
  return '<span style="color:'+c+';font-weight:600">' + (v>=0?'+':'') + '$' + v.toFixed(2) + '</span>';
}
function fmtProb(p) {
  if (p == null || p === 0 || p === '0') return '&mdash;';
  return (parseFloat(p)*100).toFixed(1)+'%';
}
function fmtPct(p) {
  if (p == null || p === 0 || p === '0') return '&mdash;';
  const v = parseFloat(p);
  return (v >= 0 ? '+' : '') + v.toFixed(3) + '%';
}
function fmtMs(v) {
  if (!v || v === 0) return '&mdash;';
  return parseFloat(v).toFixed(0);
}
function dval(item, key) {
  const v = item[key];
  if (v == null) return null;
  if (typeof v === 'object') return v.S || v.N || v.BOOL || null;
  return v;
}
function _float_or_null(v) {
  if (v == null) return null;
  const f = parseFloat(v);
  return isNaN(f) ? null : f;
}

// ── Overview refresh ──────────────────────────────────────────────────────────
async function refreshOverview() {
  try {
    const resp = await fetch('/api/data');
    const data = await resp.json();
    const s = data.stats;

    const mode = (s.mode || 'paper').toUpperCase();
    window._tradeMode = mode.toLowerCase();
    window._bankroll = s.starting_bankroll || 1000;
    const modeBadge = document.getElementById('mode-badge');
    modeBadge.textContent = mode;
    if (mode === 'LIVE') {
      modeBadge.style.background = 'rgba(201,42,42,.15)';
      modeBadge.style.color = '#c92a2a';
      modeBadge.style.borderColor = 'rgba(201,42,42,.3)';
    } else {
      modeBadge.style.background = 'rgba(25,113,194,.15)';
      modeBadge.style.color = '#339af0';
      modeBadge.style.borderColor = 'rgba(25,113,194,.3)';
    }

    // P&L: always use DynamoDB bot-tracked trades (not Polymarket cumulative)
    const pnl = s.total_pnl;
    const pnlEl = document.getElementById('s-pnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(2);
    pnlEl.className = 'stat-value ' + (pnl >= 0 ? 'green' : 'red');
    document.getElementById('s-pnl-sub').textContent = 'Source: Bot trades (verified)';

    const wr = s.total_resolved > 0 ? Math.round(s.wins / s.total_resolved * 100) : 0;
    document.getElementById('s-wl').textContent = s.wins + ' / ' + s.losses;
    document.getElementById('s-wl-sub').textContent = s.total_resolved + ' resolved' + (wr ? ' (' + wr + '% WR)' : '');
    document.getElementById('s-open').textContent = s.open_trades;

    // Per-asset x timeframe performance cards
    const strats = s.strategy_pnl || {};
    const all_strats = ['BTC 5m', 'ETH 5m', 'SOL 5m', 'BTC 15m', 'ETH 15m', 'SOL 15m'];
    let scHtml = '';
    for (const st of all_strats) {
      const d = strats[st] || { pnl: 0, count: 0, wins: 0 };
      const wr = d.count > 0 ? Math.round(d.wins / d.count * 100) : 0;
      const pnlColor = d.pnl >= 0 ? '#2f9e44' : '#c92a2a';
      scHtml += '<div class="strat-card">' +
        '<div class="strat-name">' + st + '</div>' +
        '<div class="strat-pnl" style="color:' + pnlColor + '">' + (d.pnl>=0?'+':'') + '$' + d.pnl.toFixed(2) + '</div>' +
        '<div class="strat-meta">' + d.count + ' trades &middot; ' + wr + '% WR</div>' +
        '<div class="win-bar-wrap"><div class="win-bar-fill" style="width:' + wr + '%;' + (d.pnl<0?'background:linear-gradient(90deg,#c92a2a,#fa5252)':'') + '"></div></div>' +
        '</div>';
    }
    document.getElementById('strategy-section').innerHTML = scHtml;

    document.getElementById('trade-count').textContent = data.trades.length + ' trades';

    // Trades table (overview: compact)
    const tbody = document.getElementById('trades-body');
    tbody.innerHTML = '';
    if (data.trades.length === 0) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="7">Waiting for first trade signal...</td></tr>';
    } else {
      for (const t of data.trades.slice(0, 20)) {
        const asset = dval(t,'asset') || 'BTC';
        const side  = dval(t,'side') || '';
        const resolved = t.resolved || dval(t,'resolved');
        const pnlv = resolved ? _float_or_null(dval(t,'pnl')) : null;
        tbody.innerHTML += '<tr>' +
          '<td>' + fmtTs(dval(t,'timestamp')) + '</td>' +
          '<td>' + assetTag(asset) + '</td>' +
          '<td>' + dirTag(side) + '</td>' +
          '<td>$' + parseFloat(dval(t,'price')||0).toFixed(3) + '</td>' +
          '<td>$' + parseFloat(dval(t,'size_usd')||0).toFixed(2) + '</td>' +
          '<td>' + fmtPnl(pnlv) + '</td>' +
          '<td>' + outcomeTag(t) + '</td>' +
          '</tr>';
      }
    }

    // Recent signals panel
    const sigBody = document.getElementById('signals-overview-body');
    sigBody.innerHTML = '';
    const recentSignals = data.recent_signals || [];
    document.getElementById('signal-count-overview').textContent = recentSignals.length + ' signals';
    if (recentSignals.length === 0) {
      sigBody.innerHTML = '<tr class="empty-row"><td colspan="6">No signal evaluations yet...</td></tr>';
    } else {
      for (const s of recentSignals) {
        const outcome = s.outcome || '';
        const isExecuted = outcome === 'executed';
        const rowCls = isExecuted ? 'signal-executed' : 'signal-rejected';
        const statusHtml = isExecuted
          ? '<span class="tag tag-up">FIRED</span>'
          : '<span class="tag tag-warn">' + (s.rejection_reason || 'rejected') + '</span>';
        sigBody.innerHTML += '<tr class="' + rowCls + '">' +
          '<td>' + fmtTs(s.timestamp) + '</td>' +
          '<td>' + assetTag(s.asset) + '</td>' +
          '<td>' + dirTag(s.direction) + '</td>' +
          '<td>' + fmtPct(s.pct_move) + '</td>' +
          '<td>' + fmtProb(s.ev) + '</td>' +
          '<td>' + statusHtml + '</td>' +
          '</tr>';
      }
    }

    // Logs
    const logsEl = document.getElementById('logs');
    logsEl.innerHTML = '';
    const reversed = [...data.logs].reverse();
    document.getElementById('log-count').textContent = reversed.length + ' events';
    for (const line of reversed) {
      let cls = 'log-line info';
      let formatted = line;
      try {
        const obj = JSON.parse(line);
        const ev = obj.event || '';
        if (obj.level === 'error')                                cls = 'log-line error';
        else if (obj.level === 'warning')                         cls = 'log-line warn';
        else if (ev.includes('signal') || ev.includes('blend'))   cls = 'log-line signal';
        else if (ev.includes('order') || ev.includes('trade'))    cls = 'log-line trade';
        else if (ev.includes('entry_zone'))                       cls = 'log-line entry';
        else if (ev.includes('window_'))                          cls = 'log-line window';
        // Convert UTC to CET (UTC+1)
        let ts = '';
        if (obj.timestamp) {
          const utc = new Date(obj.timestamp);
          ts = utc.toLocaleTimeString('en-GB', {timeZone: 'Europe/Amsterdam', hour12: false});
        }
        const asset = obj.asset ? '<span style="color:#7aa2f7">['+obj.asset+']</span>' : '';
        // Format key fields nicely, skip noise
        const skip = ['event','level','timestamp','asset','logger'];
        const highlights = ['pnl','price','side','direction','slug','error','pct_move','ev','winner'];
        const rest = Object.entries(obj)
          .filter(([k]) => !skip.includes(k))
          .map(([k,v]) => {
            const val = typeof v==='number' ? (v.toFixed ? v.toFixed(4) : v) : (typeof v==='string' ? v : JSON.stringify(v));
            if (highlights.includes(k)) return '<span style="color:#e0af68">'+k+'</span>='+val;
            return '<span style="color:#565f89">'+k+'</span>='+val;
          })
          .join(' ');
        formatted = '<span style="color:#565f89">'+ts+'</span> ' + asset + ' <b>' + ev + '</b>  ' + rest;
      } catch(ex) {}
      logsEl.innerHTML += '<div class="' + cls + '">' + formatted + '</div>';
    }
    logsEl.scrollTop = logsEl.scrollHeight;

    document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
  } catch(e) { console.error('overview error', e); }

  refreshPnlChart();
  refreshBalance();
}

// ── Trade Log page ────────────────────────────────────────────────────────────
async function loadTradeLog() {
  const asset = document.getElementById('tl-asset').value;
  const tf    = document.getElementById('tl-tf').value;
  let url = '/api/trades?limit=200';
  if (asset) url += '&asset=' + asset;
  if (tf)    url += '&tf=' + tf;
  try {
    const resp = await fetch(url);
    const data = await resp.json();
    const tbody = document.getElementById('tl-body');
    tbody.innerHTML = '';
    if (!data.trades.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="18">No trades found</td></tr>';
      return;
    }
    for (const t of data.trades) {
      const asset_v = dval(t, 'asset') || 'BTC';
      const slug    = dval(t, 'window_slug') || '';
      const tf_v    = slug.includes('15m') ? '15m' : '5m';
      const resolved = t.resolved || dval(t, 'resolved');
      const pnlv = resolved ? _float_or_null(dval(t,'pnl')) : null;
      const pAI = _float_or_null(dval(t, 'p_ai'));
      tbody.innerHTML += '<tr>' +
        '<td style="white-space:nowrap">' + fmtTs(dval(t,'timestamp')) + '</td>' +
        '<td>' + assetTag(asset_v) + '</td>' +
        '<td><span style="font-size:10px;color:var(--text-3)">' + tf_v + '</span></td>' +
        '<td>' + dirTag(dval(t,'direction')) + '</td>' +
        '<td>' + dirTag(dval(t,'side')) + '</td>' +
        '<td>$' + parseFloat(dval(t,'fill_price')||dval(t,'price')||0).toFixed(3) + '</td>' +
        '<td>$' + parseFloat(dval(t,'size_usd')||0).toFixed(2) + '</td>' +
        '<td>' + fmtProb(dval(t,'p_bayesian')) + '</td>' +
        '<td>' + (pAI != null ? fmtProb(pAI) : '<span style="color:var(--text-3)">&mdash;</span>') + '</td>' +
        '<td>' + fmtProb(dval(t,'p_final') || dval(t,'p_bayesian')) + '</td>' +
        '<td>' + fmtProb(dval(t,'ev')) + '</td>' +
        '<td style="' + (parseFloat(dval(t,'pct_move')||0)>=0?'color:#2f9e44':'color:#c92a2a') + '">' + fmtPct(dval(t,'pct_move')) + '</td>' +
        '<td>' + (dval(t,'seconds_remaining') ? parseFloat(dval(t,'seconds_remaining')).toFixed(0)+'s' : '&mdash;') + '</td>' +
        '<td>' + fmtPnl(pnlv) + '</td>' +
        '<td>' + outcomeTag(t) + '</td>' +
        '<td style="font-size:11px;color:var(--text-3)">' + fmtMs(dval(t,'latency_signal_ms')) + '</td>' +
        '<td style="font-size:11px;color:var(--text-3)">' + fmtMs(dval(t,'latency_order_ms')) + '</td>' +
        '<td style="font-size:11px;color:var(--text-3)">' + fmtMs(dval(t,'latency_bedrock_ms')) + '</td>' +
        '</tr>';
    }
  } catch(e) { console.error('tradelog error', e); }
}

// ── Signals page ──────────────────────────────────────────────────────────────
async function loadSignalsPage() {
  await Promise.all([loadSignalFunnel(), loadSignals(), loadNearMisses()]);
}

async function loadSignalFunnel() {
  try {
    const resp = await fetch('/api/signals/summary');
    const data = await resp.json();

    document.getElementById('funnel-badge').textContent = data.total + ' total evaluations';

    const steps = [
      { label: 'Total Evaluations', count: data.total, color: '#339af0' },
      { label: 'Passed min_move', count: data.passed_min_move, color: '#1971c2' },
      { label: 'Passed Filters', count: data.passed_filters, color: '#e67700' },
      { label: 'Executed', count: data.executed, color: '#2f9e44' },
    ];
    const maxCount = Math.max(1, data.total);
    let html = '';
    for (const step of steps) {
      const pct = Math.max(2, Math.round(step.count / maxCount * 100));
      html += '<div class="funnel-bar-wrap">' +
        '<div class="funnel-label">' + step.label + '</div>' +
        '<div class="funnel-bar" style="width:' + pct + '%;background:' + step.color + '">' + step.count + '</div>' +
        '</div>';
    }
    document.getElementById('funnel-bars').innerHTML = html;

    // Rejection donut
    const reasons = data.by_reason || {};
    const reasonLabels = Object.keys(reasons);
    const reasonValues = Object.values(reasons);
    const reasonColors = reasonLabels.map(r => {
      const m = {
        'min_move': '#868e96',
        'market_efficient': '#e67700',
        'insufficient_ev': '#d9480f',
        'obi_veto': '#c92a2a',
        'unrealistic_price': '#495057',
      };
      return m[r] || '#adb5bd';
    });

    if (!rejectionDonut) {
      const ctx = document.getElementById('rejection-donut').getContext('2d');
      rejectionDonut = new Chart(ctx, {
        type: 'doughnut',
        data: {
          labels: reasonLabels,
          datasets: [{
            data: reasonValues,
            backgroundColor: reasonColors,
            borderWidth: 2,
            borderColor: '#fff',
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          cutout: '55%',
          plugins: {
            legend: {
              position: 'right',
              labels: { font: { size: 11, weight: '600' }, color: '#495057', padding: 12 },
            },
          },
        },
      });
    } else {
      rejectionDonut.data.labels = reasonLabels;
      rejectionDonut.data.datasets[0].data = reasonValues;
      rejectionDonut.data.datasets[0].backgroundColor = reasonColors;
      rejectionDonut.update('none');
    }
  } catch(e) { console.error('funnel error', e); }
}

async function loadSignals() {
  const asset = document.getElementById('sig-asset').value;
  const outcome = document.getElementById('sig-outcome').value;
  let url = '/api/signals?limit=200';
  if (asset)   url += '&asset=' + asset;
  if (outcome) url += '&outcome=' + outcome;
  try {
    const resp = await fetch(url);
    const data = await resp.json();
    const tbody = document.getElementById('sig-body');
    tbody.innerHTML = '';
    if (!data.signals.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="14">No signals found</td></tr>';
      return;
    }
    for (const s of data.signals) {
      const outc = dval(s, 'outcome') || '';
      const isExec = outc === 'executed';
      const rowCls = isExec ? 'signal-executed' : 'signal-rejected';
      const reason = dval(s, 'rejection_reason') || '';
      const statusHtml = isExec
        ? '<span class="tag tag-up">EXECUTED</span>'
        : '<span class="tag tag-warn">REJECTED</span>';
      tbody.innerHTML += '<tr class="' + rowCls + '">' +
        '<td style="white-space:nowrap">' + fmtTs(dval(s,'timestamp')) + '</td>' +
        '<td>' + assetTag(dval(s,'asset')||'') + '</td>' +
        '<td><span style="font-size:10px;color:var(--text-3)">' + (dval(s,'timeframe')||'5m') + '</span></td>' +
        '<td>' + dirTag(dval(s,'direction')) + '</td>' +
        '<td>' + fmtPct(dval(s,'pct_move')) + '</td>' +
        '<td>' + fmtProb(dval(s,'model_prob')) + '</td>' +
        '<td>' + (dval(s,'market_price') ? '$'+parseFloat(dval(s,'market_price')).toFixed(3) : '&mdash;') + '</td>' +
        '<td>' + fmtProb(dval(s,'ev')) + '</td>' +
        '<td>' + fmtProb(dval(s,'p_bayesian')) + '</td>' +
        '<td>' + (dval(s,'seconds_remaining') ? parseFloat(dval(s,'seconds_remaining')).toFixed(0)+'s' : '&mdash;') + '</td>' +
        '<td>' + (dval(s,'yes_ask') ? '$'+parseFloat(dval(s,'yes_ask')).toFixed(3) : '&mdash;') + '</td>' +
        '<td>' + (dval(s,'no_ask') ? '$'+parseFloat(dval(s,'no_ask')).toFixed(3) : '&mdash;') + '</td>' +
        '<td>' + statusHtml + '</td>' +
        '<td>' + (reason ? reasonBadge(reason) : '&mdash;') + '</td>' +
        '</tr>';
    }
  } catch(e) { console.error('signals error', e); }
}

async function loadNearMisses() {
  try {
    const resp = await fetch('/api/signals?outcome=rejected&limit=500');
    const data = await resp.json();
    const tbody = document.getElementById('near-miss-body');
    tbody.innerHTML = '';

    // Filter to insufficient_ev with EV within 2% of threshold
    const nearMisses = data.signals.filter(s => {
      const reason = dval(s, 'rejection_reason') || '';
      if (reason !== 'insufficient_ev') return false;
      const ev = parseFloat(dval(s, 'ev') || 0);
      return ev > 0 && ev >= 0.03;  // within ~2% of typical 5% threshold
    }).slice(0, 20);

    if (nearMisses.length === 0) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="6">No near-miss signals</td></tr>';
      return;
    }

    for (const s of nearMisses) {
      tbody.innerHTML += '<tr>' +
        '<td>' + fmtTs(dval(s,'timestamp')) + '</td>' +
        '<td>' + assetTag(dval(s,'asset')||'') + '</td>' +
        '<td>' + dirTag(dval(s,'direction')) + '</td>' +
        '<td>' + fmtPct(dval(s,'pct_move')) + '</td>' +
        '<td>' + fmtProb(dval(s,'ev')) + '</td>' +
        '<td>' + fmtProb(dval(s,'model_prob')) + '</td>' +
        '</tr>';
    }
  } catch(e) { console.error('near-miss error', e); }
}

// ── Analytics page ────────────────────────────────────────────────────────────
async function loadAnalytics() {
  try {
    const [statsResp, calResp, pairsResp, tradesResp] = await Promise.all([
      fetch('/api/strategy-stats'),
      fetch('/api/calibration'),
      fetch('/api/pairs'),
      fetch('/api/trades?limit=200'),
    ]);
    const stats = await statsResp.json();
    const cal = await calResp.json();
    const pairsData = await pairsResp.json();
    const tradesData = await tradesResp.json();

    // Pairs config table
    const pairsBody = document.getElementById('pairs-body');
    pairsBody.innerHTML = '';
    document.getElementById('pairs-badge').textContent = pairsData.total_enabled + ' pairs enabled';
    for (const p of pairsData.pairs) {
      const perf = p.perf || {};
      const wr = perf.trades > 0 ? Math.round(perf.wr * 100) : 0;
      const pnlColor = (perf.pnl || 0) >= 0 ? '#2f9e44' : '#c92a2a';
      const assetCls = { BTC: 'tag-btc', ETH: 'tag-eth', SOL: 'tag-sol' }[p.asset] || '';
      pairsBody.innerHTML += '<tr>' +
        '<td><span class="tag ' + assetCls + '" style="font-size:11px">' + p.pair + '</span></td>' +
        '<td><code>' + p.min_move_pct + '%</code></td>' +
        '<td><code>' + (p.min_ev_threshold * 100).toFixed(0) + '%</code></td>' +
        '<td><code>' + p.max_market_price + '</code></td>' +
        '<td><code>T-' + p.entry_seconds + 's</code></td>' +
        '<td>' + (perf.trades || 0) + '</td>' +
        '<td><span style="font-weight:700;color:' + (wr>60?'#2f9e44':wr<40?'#c92a2a':'#e67700') + '">' + (perf.trades ? wr+'%' : '&mdash;') + '</span></td>' +
        '<td style="color:' + pnlColor + ';font-weight:600">' + (perf.trades ? (perf.pnl>=0?'+':'')+'$'+perf.pnl.toFixed(2) : '&mdash;') + '</td>' +
        '</tr>';
    }

    // By segment
    const segBody = document.getElementById('seg-body');
    segBody.innerHTML = '';
    const segs = Object.entries(stats.by_segment || {}).sort((a,b) => b[1].total - a[1].total);
    if (segs.length === 0) {
      segBody.innerHTML = '<tr class="empty-row"><td colspan="4">No data yet</td></tr>';
    } else {
      for (const [k, v] of segs) {
        const wr = v.wr * 100;
        const pnlColor = v.pnl >= 0 ? '#2f9e44' : '#c92a2a';
        segBody.innerHTML += '<tr>' +
          '<td><strong>' + k + '</strong></td>' +
          '<td>' + v.total + '</td>' +
          '<td><span style="font-weight:700;color:' + (wr>60?'#2f9e44':wr<40?'#c92a2a':'#e67700') + '">' + wr.toFixed(0) + '%</span>' +
          ' <div style="width:60px;height:4px;background:var(--surface-2);border-radius:2px;display:inline-block;vertical-align:middle;margin-left:8px"><div style="width:' + wr + '%;height:100%;background:' + (wr>60?'#2f9e44':wr<40?'#c92a2a':'#e67700') + ';border-radius:2px"></div></div></td>' +
          '<td style="color:' + pnlColor + ';font-weight:600">' + (v.pnl>=0?'+':'') + '$' + v.pnl.toFixed(2) + '</td>' +
          '</tr>';
      }
    }

    // Hourly chart
    const hourData = stats.by_hour || {};
    const hours = Array.from({length:24}, (_,i) => i);
    const hourWRs = hours.map(h => {
      const d = hourData[String(h)];
      return d && d.total > 0 ? Math.round(d.wr * 100) : null;
    });
    if (!hourChart) {
      const ctx = document.getElementById('hour-chart').getContext('2d');
      hourChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: hours.map(h => h+'h'),
          datasets: [{
            label: 'Win Rate %',
            data: hourWRs,
            backgroundColor: hourWRs.map(v => v === null ? '#e9ecef' : v >= 60 ? 'rgba(47,158,68,.7)' : v < 40 ? 'rgba(201,42,42,.7)' : 'rgba(230,119,0,.7)'),
            borderRadius: 4,
          }],
        },
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: '#868e96', font: { size: 10 } }, grid: { display: false } },
            y: { min: 0, max: 100, ticks: { color: '#868e96', font: { size: 10 }, callback: v => v+'%' }, grid: { color: '#f1f3f5' } },
          },
        },
      });
    } else {
      hourChart.data.datasets[0].data = hourWRs;
      hourChart.data.datasets[0].backgroundColor = hourWRs.map(v => v === null ? '#e9ecef' : v >= 60 ? 'rgba(47,158,68,.7)' : v < 40 ? 'rgba(201,42,42,.7)' : 'rgba(230,119,0,.7)');
      hourChart.update('none');
    }

    // Calibration chart (scatter/line: model p vs actual WR)
    const calEntries = Object.entries(cal);
    const calLabels = calEntries.map(([k]) => k);
    const calModelP = calEntries.map(([, v]) => v.model_p_avg * 100);
    const calActualWR = calEntries.map(([, v]) => v.actual_wr * 100);
    const calCounts = calEntries.map(([, v]) => v.total);

    if (!calibrationChart) {
      const ctx = document.getElementById('calibration-chart').getContext('2d');
      calibrationChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: calLabels,
          datasets: [
            {
              label: 'Model P (avg)',
              data: calModelP,
              backgroundColor: 'rgba(25,113,194,.4)',
              borderColor: '#1971c2',
              borderWidth: 1,
              borderRadius: 3,
            },
            {
              label: 'Actual WR',
              data: calActualWR,
              backgroundColor: 'rgba(47,158,68,.4)',
              borderColor: '#2f9e44',
              borderWidth: 1,
              borderRadius: 3,
            },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          plugins: {
            legend: {
              labels: { font: { size: 11, weight: '600' }, color: '#495057', padding: 12 },
            },
            tooltip: {
              callbacks: {
                afterLabel: function(ctx) {
                  const idx = ctx.dataIndex;
                  return 'N = ' + (calCounts[idx] || 0);
                }
              }
            },
          },
          scales: {
            x: { ticks: { color: '#868e96', font: { size: 9 } }, grid: { display: false } },
            y: { min: 0, max: 100, ticks: { color: '#868e96', font: { size: 10 }, callback: v => v+'%' }, grid: { color: '#f1f3f5' } },
          },
        },
      });
    } else {
      calibrationChart.data.labels = calLabels;
      calibrationChart.data.datasets[0].data = calModelP;
      calibrationChart.data.datasets[1].data = calActualWR;
      calibrationChart.update('none');
    }

    // Latency distribution histogram
    const trades = tradesData.trades || [];
    const sigLatencies = [];
    const ordLatencies = [];
    const brLatencies = [];
    for (const t of trades) {
      const sl = parseFloat(dval(t, 'latency_signal_ms') || 0);
      const ol = parseFloat(dval(t, 'latency_order_ms') || 0);
      const bl = parseFloat(dval(t, 'latency_bedrock_ms') || 0);
      if (sl > 0) sigLatencies.push(sl);
      if (ol > 0) ordLatencies.push(ol);
      if (bl > 0) brLatencies.push(bl);
    }

    // Build histogram buckets
    function histBuckets(values, n) {
      if (!values.length) return { labels: [], counts: [] };
      const sorted = [...values].sort((a,b) => a-b);
      const minV = sorted[0];
      const maxV = sorted[sorted.length - 1];
      const step = Math.max(1, Math.ceil((maxV - minV) / n));
      const labels = [];
      const counts = [];
      for (let i = 0; i < n; i++) {
        const lo = minV + i * step;
        const hi = lo + step;
        labels.push(lo.toFixed(0));
        counts.push(values.filter(v => v >= lo && v < hi).length);
      }
      return { labels, counts };
    }

    const sigHist = histBuckets(sigLatencies, 10);
    const ordHist = histBuckets(ordLatencies, 10);
    const brHist = histBuckets(brLatencies, 10);

    // Use the longest set of labels
    const allHists = [sigHist, ordHist, brHist];
    const maxLen = Math.max(sigHist.labels.length, ordHist.labels.length, brHist.labels.length);
    const latLabels = (maxLen === sigHist.labels.length ? sigHist : maxLen === ordHist.labels.length ? ordHist : brHist).labels;

    // Pad shorter arrays
    function padArr(arr, len) { while (arr.length < len) arr.push(0); return arr; }

    if (!latencyChart) {
      const ctx = document.getElementById('latency-chart').getContext('2d');
      latencyChart = new Chart(ctx, {
        type: 'bar',
        data: {
          labels: latLabels.length ? latLabels : ['No data'],
          datasets: [
            {
              label: 'Signal (ms)',
              data: padArr(sigHist.counts, latLabels.length),
              backgroundColor: 'rgba(187,154,247,.5)',
              borderColor: '#bb9af7',
              borderWidth: 1,
              borderRadius: 2,
            },
            {
              label: 'Order (ms)',
              data: padArr(ordHist.counts, latLabels.length),
              backgroundColor: 'rgba(47,158,68,.4)',
              borderColor: '#2f9e44',
              borderWidth: 1,
              borderRadius: 2,
            },
            {
              label: 'Bedrock (ms)',
              data: padArr(brHist.counts, latLabels.length),
              backgroundColor: 'rgba(230,119,0,.4)',
              borderColor: '#e67700',
              borderWidth: 1,
              borderRadius: 2,
            },
          ],
        },
        options: {
          responsive: true, maintainAspectRatio: false, animation: false,
          plugins: {
            legend: {
              labels: { font: { size: 11, weight: '600' }, color: '#495057', padding: 8 },
            },
          },
          scales: {
            x: { title: { display: true, text: 'Latency (ms)', font: { size: 10 }, color: '#868e96' }, ticks: { color: '#868e96', font: { size: 9 } }, grid: { display: false } },
            y: { title: { display: true, text: 'Count', font: { size: 10 }, color: '#868e96' }, ticks: { color: '#868e96', font: { size: 10 } }, grid: { color: '#f1f3f5' } },
          },
        },
      });
    } else {
      latencyChart.data.labels = latLabels.length ? latLabels : ['No data'];
      latencyChart.data.datasets[0].data = padArr(sigHist.counts, latLabels.length);
      latencyChart.data.datasets[1].data = padArr(ordHist.counts, latLabels.length);
      latencyChart.data.datasets[2].data = padArr(brHist.counts, latLabels.length);
      latencyChart.update('none');
    }

  } catch(e) { console.error('analytics error', e); }
}

// ── Boot ──────────────────────────────────────────────────────────────────────
initPnlChart();
showPage('overview', document.querySelector('.nav-tab[data-page="overview"]'));
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard(_: str = Depends(_require_auth)):
    return HTML


if __name__ == "__main__":
    print("\n  Dashboard: http://localhost:8888\n")
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="warning")
