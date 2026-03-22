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
    _live_table = _ddb.Table("polymarket-bot-live-state")
    _USE_DYNAMO = True
except Exception:
    _live_table = None
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

    # Filter trades to current mode only, exclude early entry (shown separately)
    mode_trades = [t for t in trades
                   if _extract_field(t, "mode", "live") == _TRADE_MODE
                   and _extract_field(t, "source") not in ("early_entry", "early_exit", "early_hedge_exit")
                   and not _extract_field(t, "window_slug", "").startswith("early_")]

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

    # Early entry stats (source="early_entry" in trades table)
    early_trades = [t for t in trades if _extract_field(t, "source") in ("early_entry", "early_exit", "early_hedge_exit")]
    early_resolved = [t for t in early_trades if t.get("resolved") or _bool_field(t, "resolved")]
    early_wins = sum(1 for t in early_resolved if _float_field(t, "pnl") > 0)
    early_losses = sum(1 for t in early_resolved if _float_field(t, "pnl") <= 0)
    early_pnl = sum(_float_field(t, "pnl") for t in early_resolved)
    early_pending = len(early_trades) - len(early_resolved)
    early_deployed = sum(_float_field(t, "size_usd") for t in early_trades)

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
            "early_entry": {
                "trades": len(early_trades),
                "wins": early_wins,
                "losses": early_losses,
                "pending": early_pending,
                "pnl": early_pnl,
                "deployed": early_deployed,
                "wr": round(early_wins / (early_wins + early_losses) * 100) if (early_wins + early_losses) > 0 else 0,
            },
        },
        "early_trades": [{
            "slug": _extract_field(t, "window_slug"),
            "asset": _extract_field(t, "asset"),
            "side": _extract_field(t, "side"),
            "fill_price": _float_field(t, "fill_price"),
            "size_usd": _float_field(t, "size_usd"),
            "entry_type": _extract_field(t, "entry_type"),
            "model_prob": _float_field(t, "model_prob"),
            "ev": _float_field(t, "ev"),
            "pnl": _float_field(t, "pnl"),
            "resolved": bool(t.get("resolved") or _bool_field(t, "resolved")),
            "timestamp": _float_field(t, "timestamp"),
        } for t in early_trades[:20]],
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


@app.get("/api/early-entry")
def api_early_entry():
    """Early entry trades and stats."""
    import time as _t
    from decimal import Decimal
    trades = get_trades(limit=500)
    early = [t for t in trades if _extract_field(t, "source") in ("early_entry", "early_exit", "early_hedge_exit")]

    # Stats — net P&L per window (buys + sells for same slug)
    from collections import defaultdict
    windows = defaultdict(lambda: {"pnl": 0, "cost": 0, "resolved": False, "ts": 0, "buys": [], "sells": []})
    for t in early:
        slug = _extract_field(t, "window_slug")
        src = _extract_field(t, "source")
        pnl = _float_field(t, "pnl")
        ts = _float_field(t, "timestamp")
        if src == "early_entry":
            windows[slug]["buys"].append(t)
            windows[slug]["cost"] += _float_field(t, "size_usd")
            windows[slug]["ts"] = max(windows[slug]["ts"], ts)
            if t.get("resolved") or _bool_field(t, "resolved"):
                windows[slug]["resolved"] = True
                windows[slug]["pnl"] += pnl
        else:  # early_exit, early_hedge_exit
            windows[slug]["sells"].append(t)
            windows[slug]["pnl"] += pnl  # sells have negative pnl (cost - proceeds)
            windows[slug]["ts"] = max(windows[slug]["ts"], ts)

    resolved_windows = {s: w for s, w in windows.items() if w["resolved"]}
    unresolved_windows = {s: w for s, w in windows.items() if not w["resolved"]}
    total_pnl = sum(w["pnl"] for w in resolved_windows.values())
    win_windows = [w for w in resolved_windows.values() if w["pnl"] > 0]
    loss_windows = [w for w in resolved_windows.values() if w["pnl"] <= 0]

    buys_only = [t for t in early if _extract_field(t, "source") == "early_entry"]
    avg_entry = sum(_float_field(t, "fill_price") for t in buys_only) / len(buys_only) if buys_only else 0
    avg_prob = sum(_float_field(t, "model_prob") for t in buys_only) / len(buys_only) if buys_only else 0
    avg_ev = sum(_float_field(t, "ev") for t in buys_only) / len(buys_only) if buys_only else 0

    # Limit fill rate
    makers = sum(1 for t in buys_only if _extract_field(t, "entry_type") == "early_maker")
    takers = sum(1 for t in buys_only if _extract_field(t, "entry_type") == "early_taker")
    fallbacks = sum(1 for t in buys_only if _extract_field(t, "entry_type") == "early_taker_fallback")

    # Trades per hour (last 4h)
    now = _t.time()
    last_4h = [t for t in buys_only if _float_field(t, "timestamp") > now - 14400]
    tph = len(last_4h) / 4 if last_4h else 0

    # Streak (per window, not per trade)
    streak = 0
    streak_type = ""
    for w in sorted(resolved_windows.values(), key=lambda x: x["ts"], reverse=True):
        won = w["pnl"] > 0
        if not streak_type:
            streak_type = "W" if won else "L"
        if (streak_type == "W") == won:
            streak += 1
        else:
            break

    # Equity curve (per window)
    curve = []
    cum = 0
    for w in sorted(resolved_windows.values(), key=lambda x: x["ts"]):
        cum += w["pnl"]
        curve.append({"ts": w["ts"], "pnl": round(cum, 2)})

    # Trade list
    trade_list = []
    for t in sorted(early, key=lambda x: _float_field(x, "timestamp"), reverse=True):
        is_resolved = bool(t.get("resolved") or _bool_field(t, "resolved"))
        pnl = _float_field(t, "pnl")
        trade_list.append({
            "slug": _extract_field(t, "window_slug"),
            "asset": _extract_field(t, "asset"),
            "side": _extract_field(t, "side"),
            "fill_price": _float_field(t, "fill_price"),
            "size_usd": _float_field(t, "size_usd"),
            "entry_type": _extract_field(t, "entry_type"),
            "model_prob": _float_field(t, "model_prob"),
            "ev": _float_field(t, "ev"),
            "pnl": pnl,
            "resolved": is_resolved,
            "won": pnl > 0 if is_resolved else None,
            "timestamp": _float_field(t, "timestamp"),
        })

    return {
        "stats": {
            "trades": len(windows),
            "wins": len(win_windows),
            "losses": len(loss_windows),
            "open": len(unresolved_windows),
            "wr": round(len(win_windows) / len(resolved_windows) * 100) if resolved_windows else 0,
            "pnl": round(total_pnl, 2),
            "avg_entry": round(avg_entry, 3),
            "avg_prob": round(avg_prob, 3),
            "avg_ev": round(avg_ev, 3),
            "makers": makers,
            "takers": takers,
            "fallbacks": fallbacks,
            "limit_fill_rate": round(makers / len(early) * 100) if early else 0,
            "tph": round(tph, 1),
            "streak": f"{streak_type}{streak}" if streak else "—",
        },
        "curve": curve,
        "trades": trade_list,
    }


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


# ── NEW V2 API ENDPOINTS ─────────────────────────────────────────────────────

@app.get("/api/live-state")
def api_live_state():
    """Fast endpoint: current window state for all 3 assets from DynamoDB."""
    if not _live_table:
        return {"assets": {}}
    try:
        result = {}
        for asset in ["BTC", "ETH", "SOL"]:
            resp = _live_table.get_item(Key={"asset": asset})
            item = resp.get("Item")
            if item:
                result[asset] = {k: float(v) if isinstance(v, Decimal) else v for k, v in item.items()}
        return {"assets": result, "ts": time.time()}
    except Exception as e:
        return {"assets": {}, "error": str(e)[:60]}


@app.get("/api/overview")
def api_overview():
    """Aggregated stats + last 100 windows for overview tab."""
    import httpx as _hx
    from collections import defaultdict

    # Get on-chain activity for real P&L
    try:
        r = _hx.get("https://data-api.polymarket.com/activity", params={
            "user": _WALLET_ADDRESS, "limit": 500,
        }, timeout=15)
        activity = r.json() if r.status_code == 200 else []
    except Exception:
        activity = []

    fivemin = [a for a in activity if "Up or Down" in a.get("title", "")]

    # Group by market
    windows = defaultdict(lambda: {"buys": 0, "sells": 0, "redeems": 0, "buy_count": 0})
    for a in fivemin:
        title = a.get("title", "")
        typ = a.get("type", "")
        usd = float(a.get("usdcSize", 0) or 0)
        if typ == "TRADE":
            if a.get("side") == "BUY":
                windows[title]["buys"] += usd
                windows[title]["buy_count"] += 1
            elif a.get("side") == "SELL":
                windows[title]["sells"] += usd
        elif typ == "REDEEM":
            windows[title]["redeems"] += usd

    # Compute stats
    window_list = []
    total_pnl = 0
    wins = 0
    losses = 0
    combined_avgs = []

    for title in sorted(windows.keys(), reverse=True)[:100]:
        d = windows[title]
        if d["buys"] == 0:
            continue
        net = d["redeems"] + d["sells"] - d["buys"]
        total_pnl += net
        is_win = net > 0.5
        if is_win:
            wins += 1
        elif net < -0.5:
            losses += 1

        # Determine asset
        asset = "BTC" if "Bitcoin" in title else "ETH" if "Ethereum" in title else "SOL" if "Solana" in title else "?"

        window_list.append({
            "title": title[:55],
            "asset": asset,
            "buys": round(d["buys"], 2),
            "sells": round(d["sells"], 2),
            "redeems": round(d["redeems"], 2),
            "net": round(net, 2),
            "win": is_win,
            "buy_count": d["buy_count"],
        })

    resolved = wins + losses
    wr = round(wins / resolved * 100) if resolved > 0 else 0

    # Asset breakdown
    asset_stats = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0})
    for w in window_list:
        a = w["asset"]
        asset_stats[a]["pnl"] += w["net"]
        if w["net"] > 0.5:
            asset_stats[a]["wins"] += 1
        elif w["net"] < -0.5:
            asset_stats[a]["losses"] += 1

    # Equity curve
    cum = 0
    curve = []
    for w in reversed(window_list):
        cum += w["net"]
        curve.append(round(cum, 2))
    curve.reverse()

    # Portfolio value
    portfolio = 0
    try:
        r2 = _hx.get("https://data-api.polymarket.com/value", params={"user": _WALLET_ADDRESS}, timeout=10)
        if r2.status_code == 200 and r2.json():
            portfolio = float(r2.json()[0].get("value", 0))
    except Exception:
        pass

    return {
        "stats": {
            "total_pnl": round(total_pnl, 2),
            "wins": wins,
            "losses": losses,
            "wr": wr,
            "windows": len(window_list),
            "portfolio": round(portfolio, 2),
        },
        "assets": {k: {"wins": v["wins"], "losses": v["losses"], "pnl": round(v["pnl"], 2)}
                   for k, v in asset_stats.items()},
        "windows": window_list[:50],
        "curve": curve[:50],
    }


# ── HTML dashboard ────────────────────────────────────────────────────────────


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>PolyBot V2</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,system-ui,sans-serif;background:#f8f9fb;color:#1a1d2e;min-height:100vh}
.nav{background:#fff;border-bottom:1px solid #e2e6ef;padding:0 20px;display:flex;align-items:center;height:48px;gap:16px;box-shadow:0 1px 3px rgba(0,0,0,0.06)}
.nav-logo{font-weight:700;font-size:16px;color:#059669}
.nav-tab{background:none;border:none;color:#64748b;font-size:13px;cursor:pointer;padding:8px 12px;border-radius:6px}
.nav-tab:hover{color:#1a1d2e;background:#f1f5f9}
.nav-tab.active{color:#059669;background:#ecfdf5;font-weight:600}
.nav-right{margin-left:auto;font-size:11px;color:#94a3b8}
.page{display:none;padding:16px}
.page.active{display:block}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
@media(max-width:900px){.grid3{grid-template-columns:1fr}}
.card{background:#fff;border:1px solid #e2e6ef;border-radius:10px;padding:14px;box-shadow:0 1px 4px rgba(0,0,0,0.05)}
.card-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;padding-bottom:8px;border-bottom:1px solid #f1f5f9}
.card-title{font-weight:700;font-size:15px}
.card-badge{font-size:10px;padding:2px 8px;border-radius:10px;font-weight:600}
.badge-pre{background:#dbeafe;color:#2563eb}
.badge-open{background:#fef3c7;color:#d97706}
.badge-acc{background:#d1fae5;color:#059669}
.badge-hold{background:#ede9fe;color:#7c3aed}
.stat-row{display:flex;justify-content:space-between;padding:4px 0;font-size:12px;border-bottom:1px solid #f8f9fb}
.stat-label{color:#94a3b8}
.stat-value{font-weight:600;color:#1a1d2e}
.green{color:#059669}.red{color:#dc2626}.blue{color:#2563eb}.yellow{color:#d97706}.gray{color:#94a3b8}
.log{max-height:180px;overflow-y:auto;font-size:11px;font-family:monospace;padding:4px;background:#f8f9fb;border-radius:6px;margin-top:6px}
.log-entry{padding:2px 4px;border-bottom:1px solid #f1f5f9}
.kpi-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;margin-bottom:12px}
.kpi{background:#fff;border:1px solid #e2e6ef;border-radius:10px;padding:12px;text-align:center;box-shadow:0 1px 4px rgba(0,0,0,0.04)}
.kpi-label{font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:0.5px}
.kpi-value{font-size:22px;font-weight:700;margin-top:2px}
.tbl{width:100%;border-collapse:collapse;font-size:12px}
.tbl th{text-align:left;padding:6px 8px;color:#94a3b8;font-size:10px;text-transform:uppercase;border-bottom:1px solid #e2e6ef}
.tbl td{padding:6px 8px;border-bottom:1px solid #f1f5f9;color:#1a1d2e}
.tbl tr:hover{background:#f8f9fb}
.win-row{background:#f0fdf4}.loss-row{background:#fef2f2}
.chart-wrap{height:200px;position:relative}
.section{margin-top:12px}
.section-title{font-size:13px;font-weight:600;color:#64748b;margin-bottom:6px}
</style>
</head>
<body>
<div class="nav">
  <span class="nav-logo">PolyBot V2</span>
  <button class="nav-tab active" onclick="showTab('live',this)">Live</button>
  <button class="nav-tab" onclick="showTab('overview',this)">Overview</button>
  <span class="nav-right" id="nav-ts">—</span>
</div>

<!-- LIVE TAB -->
<div id="page-live" class="page active">
  <div class="grid3" id="live-grid"></div>
</div>

<!-- OVERVIEW TAB -->
<div id="page-overview" class="page">
  <div class="kpi-row" id="ov-kpis"></div>
  <div class="section">
    <div class="section-title">Equity Curve</div>
    <div class="card"><div class="chart-wrap"><canvas id="eq-chart"></canvas></div></div>
  </div>
  <div class="section" style="margin-top:12px">
    <div class="section-title">Asset Breakdown</div>
    <div id="ov-assets" style="display:flex;gap:8px"></div>
  </div>
  <div class="section" style="margin-top:12px">
    <div class="section-title">Recent Windows</div>
    <div class="card" style="max-height:400px;overflow-y:auto">
      <table class="tbl"><thead><tr>
        <th>Market</th><th>Asset</th><th>Buys</th><th>Sells</th><th>Redeem</th><th>Net P&L</th><th>Result</th>
      </tr></thead><tbody id="ov-windows"></tbody></table>
    </div>
  </div>
</div>

<script>
let eqChart = null;
let liveInterval = null;

function showTab(name, btn) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  if (btn) btn.classList.add('active');
  if (name === 'live') startLive();
  else if (name === 'overview') loadOverview();
}

function phaseClass(p) {
  if (p === 'PRE-POSITION') return 'badge-pre';
  if (p === 'CONFIRM' || p === 'ACCUMULATE') return 'badge-acc';
  if (p === 'HOLD') return 'badge-hold';
  return 'badge-open';
}

function renderAssetCard(asset, d) {
  const secs = d.seconds || 0;
  const remaining = Math.max(300 - secs, 0);
  const mm = Math.floor(remaining / 60);
  const ss = Math.floor(remaining % 60);
  const comb = d.combined_avg || 0;
  const combClass = comb > 0 && comb < 1 ? 'green' : comb >= 1 ? 'red' : 'gray';
  const margin = d.margin || 0;
  const dir = d.direction || '—';
  const dirColor = dir === 'UP' ? 'green' : dir === 'DOWN' ? 'red' : 'gray';

  return `<div class="card">
    <div class="card-header">
      <span class="card-title">${asset}</span>
      <span>
        <span class="card-badge ${phaseClass(d.phase)}">${d.phase || 'WAITING'}</span>
        <span style="margin-left:6px;font-size:12px;color:#64748b">${mm}:${ss.toString().padStart(2,'0')}</span>
      </span>
    </div>
    <div class="stat-row"><span class="stat-label">Direction</span><span class="stat-value ${dirColor}">${dir}</span></div>
    <div class="stat-row"><span class="stat-label">UP</span><span class="stat-value">${d.up_shares||0} sh @ $${(d.up_avg||0).toFixed(2)} = $${(d.up_cost||0).toFixed(2)}</span></div>
    <div class="stat-row"><span class="stat-label">DOWN</span><span class="stat-value">${d.down_shares||0} sh @ $${(d.down_avg||0).toFixed(2)} = $${(d.down_cost||0).toFixed(2)}</span></div>
    <div class="stat-row"><span class="stat-label">Combined Avg</span><span class="stat-value ${combClass}">$${comb.toFixed(3)}</span></div>
    <div class="stat-row"><span class="stat-label">Margin</span><span class="stat-value ${margin>0?'green':'red'}">${margin.toFixed(1)}%</span></div>
    <div class="stat-row"><span class="stat-label">Orders</span><span class="stat-value">${d.open_orders||0} open / ${d.filled_orders||0} filled</span></div>
    <div class="stat-row"><span class="stat-label">Spent</span><span class="stat-value">$${((d.main_filled||0)+(d.hedge_filled||0)+(d.cheap_filled||0)).toFixed(2)}</span></div>
    <div class="stat-row"><span class="stat-label">Orderbook</span><span class="stat-value gray" style="font-size:10px">YES ${(d.yes_bid||0).toFixed(2)}/${(d.yes_ask||0).toFixed(2)} | NO ${(d.no_bid||0).toFixed(2)}/${(d.no_ask||0).toFixed(2)}</span></div>
    <div class="log">${(d.activity||[]).slice().reverse().map(a => {
      let cls = 'gray';
      if (a.includes('BUY') || a.includes('FILL')) cls = 'blue';
      else if (a.includes('SELL')) cls = 'red';
      else if (a.includes('PRE-POS')) cls = 'yellow';
      else if (a.includes('CHECK')) cls = 'gray';
      return '<div class="log-entry ' + cls + '">' + a + '</div>';
    }).join('') || '<div class="log-entry gray">No activity yet</div>'}</div>
  </div>`;
}

async function refreshLive() {
  try {
    const r = await fetch('/api/live-state');
    const data = await r.json();
    const grid = document.getElementById('live-grid');
    let html = '';
    for (const asset of ['BTC', 'ETH', 'SOL']) {
      const d = (data.assets || {})[asset] || {};
      html += renderAssetCard(asset, d);
    }
    grid.innerHTML = html;
    document.getElementById('nav-ts').textContent = new Date().toLocaleTimeString('en-GB', {timeZone:'Europe/Amsterdam'});
  } catch(e) { console.error('live error', e); }
}

function startLive() {
  if (liveInterval) clearInterval(liveInterval);
  refreshLive();
  liveInterval = setInterval(refreshLive, 1000);
}

async function loadOverview() {
  if (liveInterval) { clearInterval(liveInterval); liveInterval = null; }
  try {
    const r = await fetch('/api/overview');
    const data = await r.json();
    const s = data.stats || {};

    // KPIs
    const pnlClass = s.total_pnl >= 0 ? 'green' : 'red';
    document.getElementById('ov-kpis').innerHTML = `
      <div class="kpi"><div class="kpi-label">P&L</div><div class="kpi-value ${pnlClass}">${s.total_pnl>=0?'+':''}$${Math.abs(s.total_pnl||0).toFixed(2)}</div></div>
      <div class="kpi"><div class="kpi-label">Win Rate</div><div class="kpi-value">${s.wr||0}%</div></div>
      <div class="kpi"><div class="kpi-label">Windows</div><div class="kpi-value">${s.windows||0}</div></div>
      <div class="kpi"><div class="kpi-label">W/L</div><div class="kpi-value">${s.wins||0}/${s.losses||0}</div></div>
      <div class="kpi"><div class="kpi-label">Portfolio</div><div class="kpi-value">$${(s.portfolio||0).toFixed(0)}</div></div>
    `;

    // Equity curve
    const curve = data.curve || [];
    if (curve.length > 0) {
      const labels = curve.map((_,i) => i+1);
      const values = curve;
      const ctx = document.getElementById('eq-chart').getContext('2d');
      if (eqChart) eqChart.destroy();
      eqChart = new Chart(ctx, {
        type: 'line',
        data: {labels, datasets: [{data: values, borderColor: values[0]>=0?'#10b981':'#ef4444', backgroundColor:'transparent', borderWidth:2, pointRadius:0, tension:0.1}]},
        options: {responsive:true, maintainAspectRatio:false, animation:false, plugins:{legend:{display:false}}, scales:{y:{ticks:{callback:v=>'$'+v,color:'#64748b',font:{size:10}},grid:{color:'#1e2030'}},x:{ticks:{color:'#64748b',font:{size:9},maxTicksLimit:10},grid:{color:'#1e2030'}}}}
      });
    }

    // Asset breakdown
    const assets = data.assets || {};
    let ah = '';
    for (const a of ['BTC','ETH','SOL']) {
      const ad = assets[a] || {wins:0,losses:0,pnl:0};
      const total = ad.wins + ad.losses;
      const wr = total > 0 ? Math.round(ad.wins/total*100) : 0;
      const pc = ad.pnl >= 0 ? 'green' : 'red';
      ah += `<div class="kpi" style="flex:1"><div class="kpi-label">${a}</div><div class="kpi-value ${pc}">${ad.pnl>=0?'+':''}$${Math.abs(ad.pnl).toFixed(2)}</div><div style="font-size:10px;color:#64748b">${wr}% WR (${total})</div></div>`;
    }
    document.getElementById('ov-assets').innerHTML = ah;

    // Windows table
    let wh = '';
    for (const w of (data.windows || [])) {
      const rc = w.net > 0.5 ? 'win-row' : w.net < -0.5 ? 'loss-row' : '';
      const badge = w.net > 0.5 ? '<span class="green">WIN</span>' : w.net < -0.5 ? '<span class="red">LOSS</span>' : '<span class="gray">—</span>';
      wh += `<tr class="${rc}"><td>${w.title}</td><td>${w.asset}</td><td>$${w.buys.toFixed(2)}</td><td>$${w.sells.toFixed(2)}</td><td>$${w.redeems.toFixed(2)}</td><td class="${w.net>=0?'green':'red'}" style="font-weight:700">${w.net>=0?'+':''}$${Math.abs(w.net).toFixed(2)}</td><td>${badge}</td></tr>`;
    }
    document.getElementById('ov-windows').innerHTML = wh || '<tr><td colspan="7" style="text-align:center;color:#64748b">No data yet</td></tr>';
  } catch(e) { console.error('overview error', e); }
}

// Start
startLive();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard(_: str = Depends(_require_auth)):
    return HTML


if __name__ == "__main__":
    print("\n  Dashboard: http://localhost:8888\n")
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="warning")
