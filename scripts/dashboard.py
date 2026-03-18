"""Live dashboard — strategy analysis desk view."""

import sys
sys.path.insert(0, "src")

import json
import time
from collections import defaultdict
from datetime import datetime, timezone

import os as _os
import secrets
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

app = FastAPI()
_security = HTTPBasic()

# Dashboard password — set DASHBOARD_PASSWORD env var, default "polybot2026"
_DASHBOARD_USER = _os.getenv("DASHBOARD_USER", "admin")
_DASHBOARD_PASS = _os.getenv("DASHBOARD_PASSWORD", "polybot2026")

# ── Storage: SQLite locally, DynamoDB when on AWS (no local DB) ──────────────

_DB_PATH_CANDIDATES = [
    "polybot.db",
    _os.path.join(_os.path.dirname(__file__), "..", "polybot.db"),
]
_DB_PATH = next((p for p in _DB_PATH_CANDIDATES if _os.path.exists(p)), "polybot.db")
_USE_DYNAMO = not _os.path.exists(_DB_PATH)

if _USE_DYNAMO:
    try:
        import boto3 as _boto3
        # Try playground profile (local dev), fall back to instance/task role (AWS)
        try:
            _session = _boto3.Session(profile_name="playground", region_name="eu-west-1")
            _session.client("sts").get_caller_identity()  # validate credentials
        except Exception:
            _session = _boto3.Session(region_name="eu-west-1")
        _ddb = _session.resource("dynamodb")
        _logs_client = _session.client("logs")
        _trades_table  = _ddb.Table("polymarket-bot-trades")
        _windows_table = _ddb.Table("polymarket-bot-windows")
    except Exception:
        _USE_DYNAMO = False

_LOCAL_LOG = "/tmp/polybot_paper.log"

# Trading mode + bankroll from env (matches bot settings)
_TRADE_MODE = _os.getenv("MODE", "paper").lower()
_BANKROLL = float(_os.getenv("BANKROLL", "1000.0"))


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


def get_trades(limit=100, asset=None, tf=None):
    if _USE_DYNAMO:
        resp  = _trades_table.scan(Limit=max(limit, 500))
        items = resp.get("Items", [])
        items.sort(key=lambda x: float(x.get("timestamp", 0)), reverse=True)
        if asset:
            items = [t for t in items if _extract_field(t, "asset", "").upper() == asset.upper()]
        if tf:
            items = [t for t in items if (tf == "15m") == ("15m" in _extract_field(t, "window_slug", ""))]
        return items[:limit]
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


def get_windows(limit=30, asset=None):
    if _USE_DYNAMO:
        resp  = _windows_table.scan(Limit=max(limit, 200))
        items = resp.get("Items", [])
        items.sort(key=lambda x: int(x.get("open_ts", 0)), reverse=True)
        if asset:
            items = [w for w in items if _extract_field(w, "asset", "").upper() == asset.upper()]
        return items[:limit]
    sql = "SELECT * FROM windows WHERE 1=1"
    params = []
    if asset:
        sql += " AND UPPER(asset) = ?"
        params.append(asset.upper())
    sql += " ORDER BY open_ts DESC LIMIT ?"
    params.append(limit)
    return _sqlite_query(sql, tuple(params))


def get_logs(lines=100):
    if _USE_DYNAMO:
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
        return [l.rstrip() for l in all_lines[-lines:]]
    except Exception:
        return []


def _require_auth(creds: HTTPBasicCredentials = Depends(_security)):
    ok_user = secrets.compare_digest(creds.username.encode(), _DASHBOARD_USER.encode())
    ok_pass = secrets.compare_digest(creds.password.encode(), _DASHBOARD_PASS.encode())
    if not (ok_user and ok_pass):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Unauthorized", headers={"WWW-Authenticate": "Basic"})
    return creds.username


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


# ── API endpoints ─────────────────────────────────────────────────────────────

@app.get("/api/data")
def api_data(_: str = Depends(_require_auth)):
    trades = get_trades(limit=100)
    windows = get_windows(limit=30)
    log_lines = get_logs()

    # Filter trades to current mode only
    mode_trades = [t for t in trades if _extract_field(t, "mode", "live") == _TRADE_MODE]

    total_pnl = sum(_float_field(t, "pnl") for t in mode_trades if t.get("resolved"))
    wins = sum(1 for t in mode_trades if t.get("resolved") and _float_field(t, "pnl") > 0)
    losses = sum(1 for t in mode_trades if t.get("resolved") and _float_field(t, "pnl") <= 0)
    open_trades = sum(1 for t in mode_trades if not t.get("resolved"))

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

    # Per-asset × timeframe breakdown
    strategy_pnl = {}
    for t in mode_trades:
        if not t.get("resolved"):
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

    current_bankroll = _BANKROLL + total_pnl

    return {
        "trades": trades,
        "windows": windows,
        "logs": log_lines,
        "stats": {
            "total_pnl": total_pnl,
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


@app.get("/api/windows")
def api_windows(
    asset: str = None,
    limit: int = 30,
    _: str = Depends(_require_auth),
):
    """Recent windows with signal/trade counts."""
    windows = get_windows(limit=limit, asset=asset)
    return {"windows": windows, "count": len(windows)}


@app.get("/api/strategy-stats")
def api_strategy_stats(_: str = Depends(_require_auth)):
    """Win rates by asset × timeframe × hour-of-day segment."""
    trades = get_trades(limit=500)
    resolved = [t for t in trades if t.get("resolved")]

    # By asset × tf
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
        # Fallback: show all 6 combos with default thresholds
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
    resolved = [t for t in trades if t.get("resolved")]
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


@app.get("/api/calibration")
def api_calibration(_: str = Depends(_require_auth)):
    """p_final buckets vs actual win rate — model calibration curve."""
    trades = get_trades(limit=500)
    resolved = [t for t in trades if t.get("resolved")]

    buckets: dict[str, dict] = {}
    bucket_edges = [0.5, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 1.0]

    for t in resolved:
        p = _float_field(t, "p_final")
        if p == 0.0:
            p = _float_field(t, "p_bayesian")  # fallback for older records
        if p == 0.0:
            continue
        pnl = _float_field(t, "pnl")
        # Find bucket
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


@app.get("/api/signal-feed")
def api_signal_feed(limit: int = 20, _: str = Depends(_require_auth)):
    """Last N trades (signals that were executed) with full metadata."""
    trades = get_trades(limit=limit)
    enriched = []
    for t in trades:
        enriched.append({
            "id": _extract_field(t, "id"),
            "timestamp": _float_field(t, "timestamp"),
            "asset": _extract_field(t, "asset", "BTC"),
            "window_slug": _extract_field(t, "window_slug"),
            "direction": _extract_field(t, "direction"),
            "side": _extract_field(t, "side"),
            "fill_price": _float_field(t, "fill_price"),
            "size_usd": _float_field(t, "size_usd"),
            "pnl": _float_field(t, "pnl") if t.get("resolved") else None,
            "resolved": bool(t.get("resolved")),
            "p_bayesian": _float_field(t, "p_bayesian"),
            "p_ai": _float_field(t, "p_ai") if t.get("p_ai") is not None else None,
            "p_final": _float_field(t, "p_final"),
            "pct_move": _float_field(t, "pct_move"),
            "seconds_remaining": _float_field(t, "seconds_remaining"),
            "ev": _float_field(t, "ev"),
            "outcome_source": _extract_field(t, "outcome_source", "coinbase_inferred"),
            "polymarket_winner": _extract_field(t, "polymarket_winner", ""),
            "correct_prediction": t.get("correct_prediction"),
            "mode": _extract_field(t, "mode", "paper"),
        })
    return {"signals": enriched, "count": len(enriched)}


@app.get("/api/pnl-history")
def api_pnl_history(_: str = Depends(_require_auth)):
    """Return hourly P&L buckets from resolved trades."""
    trades = get_trades(limit=500)
    buckets: dict[str, float] = defaultdict(float)
    for t in trades:
        if not t.get("resolved"):
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


_WALLET_ADDRESS = _os.getenv(
    "POLYMARKET_FUNDER", "0x5ca439d661c9b44337E91fC681ec4b006C473610"
)


@app.get("/api/balance")
async def api_balance(_: str = Depends(_require_auth)):
    """Return wallet USDC balances."""
    try:
        from polybot.market.balance_checker import BalanceChecker

        if not _WALLET_ADDRESS:
            return {"polymarket_value": 0.0, "polygon_usdc": 0.0, "error": "no_address"}

        checker = BalanceChecker()
        return await checker.check(_WALLET_ADDRESS)
    except Exception as e:
        return {"polymarket_value": 0.0, "polygon_usdc": 0.0, "error": str(e)}


# ── HTML dashboard ─────────────────────────────────────────────────────────────

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
    --btc:        #f7931a;
    --btc-bg:     #fff4e6;
    --eth:        #627eea;
    --eth-bg:     #edf2ff;
    --sol:        #9945ff;
    --sol-bg:     #f3f0ff;
    --orange:     #d9480f;
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
    background: var(--border);
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

  /* ── Navbar ── */
  nav {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 0 24px;
    height: 56px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    box-shadow: var(--shadow-sm);
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
    font-size: 16px; font-weight: 700; color: var(--text); letter-spacing: -0.3px;
  }
  .nav-title span { color: var(--blue); }
  .nav-tabs {
    display: flex;
    gap: 2px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 3px;
  }
  .nav-tab {
    padding: 5px 14px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 600;
    color: var(--text-3);
    cursor: pointer;
    transition: all .15s;
    border: none;
    background: none;
  }
  .nav-tab:hover { color: var(--text-2); background: var(--border); }
  .nav-tab.active {
    color: var(--text);
    background: var(--surface);
    box-shadow: var(--shadow-sm);
  }
  .nav-right {
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .nav-meta {
    font-size: 12px; color: var(--text-3);
    display: flex; align-items: center; gap: 10px;
  }
  .nav-meta .sep { color: var(--border-2); }
  .status-dot {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: 12px; font-weight: 600; color: var(--green);
    background: var(--green-bg); border: 1px solid var(--green-bd);
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
    grid-template-columns: repeat(7, 1fr);
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
  .outcome-verified   { color: var(--green); font-weight: 700; }
  .outcome-coinbase   { color: var(--gold);  font-weight: 700; }
  .outcome-pending    { color: var(--text-3); }

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

  /* ── Analytics tables ── */
  .analytics-grid {
    display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 20px;
  }
  .analytics-grid.wide { grid-template-columns: 1fr; }
  .calibration-bar-wrap { width: 60px; display: inline-block; vertical-align: middle; }
  .calibration-bar { height: 8px; border-radius: 4px; background: var(--green); display: inline-block; }

  /* ── Responsive ── */
  @media (max-width: 1200px) {
    .stats-grid { grid-template-columns: repeat(4, 1fr); }
    .strategy-grid { grid-template-columns: repeat(3, 1fr); }
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
    <button class="nav-tab active" onclick="showPage('overview')">Overview</button>
    <button class="nav-tab" onclick="showPage('tradelog')">Trade Log</button>
    <button class="nav-tab" onclick="showPage('analytics')">Analytics</button>
  </div>
  <div class="nav-right">
    <div class="nav-meta">
      <span>eu-west-1</span>
      <span class="sep">|</span>
      <span>BTC &middot; ETH &middot; SOL</span>
      <span class="sep">|</span>
      <span>Updated: <strong id="last-update">—</strong></span>
    </div>
    <div class="status-dot" id="mode-badge">PAPER</div>
  </div>
</nav>

<!-- ══════════════════════════ PAGE 1: OVERVIEW ══════════════════════════ -->
<div id="page-overview" class="page-content active">
<div class="page">

  <!-- Stats row -->
  <div class="stats-grid">
    <div class="stat-card">
      <div class="stat-label" id="bankroll-label">Virtual Bankroll</div>
      <div class="stat-value gold" id="s-bankroll">—</div>
      <div class="stat-sub" id="s-bankroll-sub">Paper trading</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Total P&amp;L</div>
      <div class="stat-value" id="s-pnl">—</div>
      <div class="stat-sub" id="s-pnl-sub">vs $<span id="s-starting">—</span> start</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Live Wallet</div>
      <div class="stat-value" id="s-balance">—</div>
      <div class="stat-sub" id="s-balance-sub">USDC on-chain</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Win / Loss</div>
      <div class="stat-value" id="s-wl">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Open Positions</div>
      <div class="stat-value blue" id="s-open">—</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">BTC Windows</div>
      <div class="stat-value" id="s-btc">—</div>
      <div class="stat-sub" id="s-btc-sub">5m + 15m</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">ETH Windows</div>
      <div class="stat-value" id="s-eth">—</div>
      <div class="stat-sub" id="s-eth-sub">5m + 15m</div>
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
      <span class="section-title">Cumulative P&amp;L — Equity Curve</span>
      <span class="section-badge" id="chart-badge">Loading...</span>
    </div>
    <div id="pnl-chart-wrap"><canvas id="pnl-chart"></canvas></div>
  </div>

  <!-- Trades + Windows tables -->
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
        <span class="section-title">Recent Windows</span>
        <span class="section-badge" id="window-count"></span>
      </div>
      <div class="scroll-wrap">
        <table>
          <thead><tr>
            <th>Time</th><th>Asset</th><th>Open</th><th>Close</th><th>Move</th><th>Result</th>
          </tr></thead>
          <tbody id="windows-body"></tbody>
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

<!-- ══════════════════════════ PAGE 2: TRADE LOG ══════════════════════════ -->
<div id="page-tradelog" class="page-content">
<div class="page">

  <div class="section-header">
    <span class="section-title">Trade Log</span>
    <div style="display:flex;gap:8px">
      <select id="tl-asset" onchange="loadTradeLog()" style="font-size:12px;padding:4px 8px;border:1px solid var(--border);border-radius:6px;background:var(--surface)">
        <option value="">All assets</option>
        <option value="BTC">BTC</option>
        <option value="ETH">ETH</option>
        <option value="SOL">SOL</option>
      </select>
      <select id="tl-tf" onchange="loadTradeLog()" style="font-size:12px;padding:4px 8px;border:1px solid var(--border);border-radius:6px;background:var(--surface)">
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
        </tr></thead>
        <tbody id="tl-body">
          <tr class="empty-row"><td colspan="15">Loading...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

</div>
</div>

<!-- ══════════════════════════ PAGE 3: ANALYTICS ══════════════════════════ -->
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
          <th>Pair</th><th>Status</th><th>Min Move</th><th>Min EV</th><th>Max Price</th>
          <th>Entry</th><th>Kelly</th><th>Trade Size</th>
          <th>Trades</th><th>Win Rate</th><th>P&amp;L</th>
        </tr></thead>
        <tbody id="pairs-body"><tr class="empty-row"><td colspan="11">Loading...</td></tr></tbody>
      </table>
    </div>
  </div>

  <div class="analytics-grid">
    <!-- By segment -->
    <div class="panel-card">
      <div class="panel-head"><span class="section-title">Win Rate by Segment</span></div>
      <div style="overflow-y:auto;max-height:280px">
        <table>
          <thead><tr><th>Segment</th><th>Trades</th><th>Win Rate</th><th>P&amp;L</th></tr></thead>
          <tbody id="seg-body"><tr class="empty-row"><td colspan="4">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>

    <!-- By pct_move bucket -->
    <div class="panel-card">
      <div class="panel-head"><span class="section-title">Win Rate by Move %</span></div>
      <div style="overflow-y:auto;max-height:280px">
        <table>
          <thead><tr><th>Move Bucket</th><th>Trades</th><th>Win Rate</th></tr></thead>
          <tbody id="move-body"><tr class="empty-row"><td colspan="3">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="analytics-grid">
    <!-- By hour of day -->
    <div class="panel-card">
      <div class="panel-head"><span class="section-title">Win Rate by Hour (UTC)</span></div>
      <div style="padding:16px 16px 8px">
        <canvas id="hour-chart" height="180"></canvas>
      </div>
    </div>

    <!-- Model calibration -->
    <div class="panel-card">
      <div class="panel-head"><span class="section-title">Model Calibration (p_final vs Actual WR)</span></div>
      <div style="overflow-y:auto;max-height:280px">
        <table>
          <thead><tr><th>p_final bucket</th><th>N</th><th>Model P</th><th>Actual WR</th><th>Calibration</th></tr></thead>
          <tbody id="cal-body"><tr class="empty-row"><td colspan="5">Loading...</td></tr></tbody>
        </table>
      </div>
    </div>
  </div>

</div>
</div>

<script>
const REFRESH_MS = 4000;

// ── Page navigation ───────────────────────────────────────────────────────────
function showPage(name) {
  document.querySelectorAll('.page-content').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  event.target.classList.add('active');
  if (name === 'analytics') loadAnalytics();
  if (name === 'tradelog') loadTradeLog();
}

// ── Chart.js cumulative P&L ───────────────────────────────────────────────────
let pnlChart = null;
let hourChart = null;

function initChart() {
  const ctx = document.getElementById('pnl-chart').getContext('2d');
  const gradient = ctx.createLinearGradient(0, 0, 0, 180);
  gradient.addColorStop(0,   'rgba(47,158,68,.26)');
  gradient.addColorStop(0.6, 'rgba(47,158,68,.04)');
  gradient.addColorStop(1,   'rgba(47,158,68,0)');

  pnlChart = new Chart(ctx, {
    type: 'line',
    data: { labels: [], datasets: [{ label: 'Cumulative P&L ($)', data: [], borderColor: '#2f9e44', borderWidth: 2, backgroundColor: gradient, pointRadius: 3, tension: 0.4, fill: true }] },
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

async function refreshChart() {
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
  } catch(e) {}
}

// ── Balance ───────────────────────────────────────────────────────────────────
let lastBalanceFetch = 0;
async function refreshBalance() {
  if (Date.now() - lastBalanceFetch < 30000) return;
  lastBalanceFetch = Date.now();
  try {
    const resp = await fetch('/api/balance');
    const d = await resp.json();
    const total = (d.polygon_usdc || 0) + (d.polymarket_value || 0);
    document.getElementById('s-balance').textContent = '$' + total.toFixed(2);
    document.getElementById('s-balance-sub').textContent =
      'USDC $' + (d.polygon_usdc||0).toFixed(2) + ' + pos $' + (d.polymarket_value||0).toFixed(2);
  } catch(e) {}
}

// ── Formatters ────────────────────────────────────────────────────────────────
function assetTag(a) {
  const m = { BTC: 'tag-btc', ETH: 'tag-eth', SOL: 'tag-sol' };
  return `<span class="tag ${m[a]||''}">${a||'?'}</span>`;
}
function dirTag(d) {
  if (!d) return '—';
  const up = d === 'YES' || d === 'up' || d === 'UP';
  return `<span class="tag ${up ? 'tag-up' : 'tag-down'}">${d}</span>`;
}
function outcomeTag(t) {
  const src = dval(t, 'outcome_source') || 'coinbase_inferred';
  const winner = dval(t, 'polymarket_winner') || '';
  const correct = t.correct_prediction;
  const resolved = t.resolved || dval(t, 'resolved');
  const pnl = parseFloat(dval(t, 'pnl') || 0);

  if (!resolved) return '<span class="tag tag-open">OPEN</span>';
  if (src === 'polymarket_verified') {
    const won = correct == 1 || correct === true;
    return `<span class="outcome-verified">${won ? '✓ WIN' : '✗ LOSS'} <span style="font-size:10px;color:var(--text-3)">(PM)</span></span>`;
  }
  // Coinbase inferred
  const won = pnl > 0;
  return `<span class="outcome-coinbase">${won ? '✓ WIN' : '✗ LOSS'} <span style="font-size:10px">⚠</span></span>`;
}
function fmtTs(ts)  { return ts ? new Date(parseFloat(ts)*1000).toLocaleTimeString() : '—'; }
function fmtTs2(ts) { return ts ? new Date(parseInt(ts)*1000).toLocaleTimeString() : '—'; }
function fmtPnl(p) {
  if (p == null || p === '') return '—';
  const v = parseFloat(p);
  const c = v >= 0 ? '#2f9e44' : '#c92a2a';
  return `<span style="color:${c};font-weight:600">${v>=0?'+':''}$${v.toFixed(4)}</span>`;
}
function fmtProb(p) {
  if (p == null || p === 0) return '—';
  return (parseFloat(p)*100).toFixed(1)+'%';
}
function fmtPct(p) {
  if (p == null || p === 0) return '—';
  const v = parseFloat(p);
  return (v >= 0 ? '+' : '') + v.toFixed(3) + '%';
}
function fmtPrice(p, asset) {
  if (!p) return '—';
  const v = parseFloat(p);
  if (asset === 'BTC') return '$'+v.toLocaleString(undefined,{maximumFractionDigits:0});
  if (asset === 'ETH') return '$'+v.toLocaleString(undefined,{maximumFractionDigits:1});
  return '$'+v.toLocaleString(undefined,{maximumFractionDigits:2});
}
function dval(item, key) {
  const v = item[key];
  if (v == null) return null;
  if (typeof v === 'object') return v.S || v.N || v.BOOL || null;
  return v;
}

// ── Main refresh (Overview) ───────────────────────────────────────────────────
async function refresh() {
  try {
    const resp = await fetch('/api/data');
    const data = await resp.json();
    const s = data.stats;

    const mode = (s.mode || 'paper').toUpperCase();
    const modeBadge = document.getElementById('mode-badge');
    modeBadge.textContent = mode;
    modeBadge.style.background = mode === 'LIVE' ? '#c92a2a22' : '#1971c222';
    modeBadge.style.color = mode === 'LIVE' ? '#c92a2a' : '#1971c2';
    modeBadge.style.borderColor = mode === 'LIVE' ? '#ffc9c9' : '#a5d8ff';

    document.getElementById('s-bankroll').textContent = '$' + (s.current_bankroll || s.starting_bankroll || 0).toFixed(2);
    document.getElementById('bankroll-label').textContent = mode === 'LIVE' ? 'Bankroll' : 'Virtual Bankroll';
    document.getElementById('s-bankroll-sub').textContent = mode === 'LIVE' ? 'Live trading' : 'Paper trading';
    document.getElementById('s-starting').textContent = (s.starting_bankroll || 1000).toFixed(0);

    const pnl = s.total_pnl;
    const pnlEl = document.getElementById('s-pnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(4);
    pnlEl.className = 'stat-value ' + (pnl >= 0 ? 'green' : 'red');

    document.getElementById('s-wl').textContent   = s.wins + ' / ' + s.losses;
    document.getElementById('s-open').textContent = s.open_trades;
    const w15 = s.asset_windows_15m || {};
    document.getElementById('s-btc').textContent = (s.asset_windows.BTC || 0);
    document.getElementById('s-eth').textContent = (s.asset_windows.ETH || 0);
    document.getElementById('s-btc-sub').textContent = `5m: ${s.asset_windows.BTC||0}  15m: ${w15.BTC||0}`;
    document.getElementById('s-eth-sub').textContent = `5m: ${s.asset_windows.ETH||0}  15m: ${w15.ETH||0}`;

    // Per-asset × timeframe performance cards
    const strats = s.strategy_pnl || {};
    const all_strats = ['BTC 5m', 'ETH 5m', 'SOL 5m', 'BTC 15m', 'ETH 15m', 'SOL 15m'];
    let scHtml = '';
    for (const st of all_strats) {
      const d = strats[st] || { pnl: 0, count: 0, wins: 0 };
      const wr = d.count > 0 ? Math.round(d.wins / d.count * 100) : 0;
      const pnlColor = d.pnl >= 0 ? '#2f9e44' : '#c92a2a';
      scHtml += `
        <div class="strat-card">
          <div class="strat-name">${st}</div>
          <div class="strat-pnl" style="color:${pnlColor}">${d.pnl>=0?'+':''}$${d.pnl.toFixed(2)}</div>
          <div class="strat-meta">${d.count} trades &middot; ${wr}% WR</div>
          <div class="win-bar-wrap">
            <div class="win-bar-fill" style="width:${wr}%;${d.pnl<0?'background:linear-gradient(90deg,#c92a2a,#fa5252)':''}"></div>
          </div>
        </div>`;
    }
    document.getElementById('strategy-section').innerHTML = scHtml;

    document.getElementById('trade-count').textContent  = data.trades.length + ' trades';
    document.getElementById('window-count').textContent = data.windows.length + ' windows';

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
        tbody.innerHTML += `<tr>
          <td>${fmtTs(dval(t,'timestamp'))}</td>
          <td>${assetTag(asset)}</td>
          <td>${dirTag(side)}</td>
          <td>$${parseFloat(dval(t,'price')||0).toFixed(3)}</td>
          <td>$${parseFloat(dval(t,'size_usd')||0).toFixed(2)}</td>
          <td>${fmtPnl(pnlv)}</td>
          <td>${outcomeTag(t)}</td>
        </tr>`;
      }
    }

    // Windows table
    const wbody = document.getElementById('windows-body');
    wbody.innerHTML = '';
    if (data.windows.length === 0) {
      wbody.innerHTML = '<tr class="empty-row"><td colspan="6">Accumulating window data...</td></tr>';
    } else {
      for (const w of data.windows.slice(0, 20)) {
        const asset = dval(w,'asset') || 'BTC';
        const op    = parseFloat(dval(w,'open_price')  || 0);
        const cp    = parseFloat(dval(w,'close_price') || 0);
        const dir   = dval(w,'direction');
        const move  = op > 0 ? ((cp - op) / op * 100).toFixed(3) + '%' : '—';
        const moveColor = cp >= op ? '#2f9e44' : '#c92a2a';
        const dirBadge  = dir
          ? (dir === 'up' ? '<span class="tag tag-up">UP</span>' : '<span class="tag tag-down">DOWN</span>')
          : '—';
        wbody.innerHTML += `<tr>
          <td>${fmtTs2(dval(w,'open_ts'))}</td>
          <td>${assetTag(asset)}</td>
          <td>${fmtPrice(op, asset)}</td>
          <td>${fmtPrice(cp, asset)}</td>
          <td style="color:${moveColor};font-weight:600">${move}</td>
          <td>${dirBadge}</td>
        </tr>`;
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
        const ts    = obj.timestamp ? obj.timestamp.substring(11,19) : '';
        const asset = obj.asset ? `[${obj.asset}]` : '';
        const rest  = Object.entries(obj)
          .filter(([k]) => !['event','level','timestamp','asset'].includes(k))
          .map(([k,v]) => `${k}=${typeof v==='number' ? (v.toFixed ? v.toFixed(4) : v) : JSON.stringify(v)}`)
          .join(' ');
        formatted = `${ts} ${asset} <b>${ev}</b>  ${rest}`;
      } catch {}
      logsEl.innerHTML += `<div class="${cls}">${formatted}</div>`;
    }
    logsEl.scrollTop = logsEl.scrollHeight;

    document.getElementById('last-update').textContent = new Date().toLocaleTimeString();
  } catch(e) { console.error(e); }
}

function _float_or_null(v) {
  if (v == null) return null;
  const f = parseFloat(v);
  return isNaN(f) ? null : f;
}

// ── Trade Log page ────────────────────────────────────────────────────────────
async function loadTradeLog() {
  const asset = document.getElementById('tl-asset').value;
  const tf    = document.getElementById('tl-tf').value;
  let url = `/api/trades?limit=200`;
  if (asset) url += `&asset=${asset}`;
  if (tf)    url += `&tf=${tf}`;
  try {
    const resp = await fetch(url);
    const data = await resp.json();
    const tbody = document.getElementById('tl-body');
    tbody.innerHTML = '';
    if (!data.trades.length) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="15">No trades found</td></tr>';
      return;
    }
    for (const t of data.trades) {
      const asset_v = dval(t, 'asset') || 'BTC';
      const slug    = dval(t, 'window_slug') || '';
      const tf_v    = slug.includes('15m') ? '15m' : '5m';
      const resolved = t.resolved || dval(t, 'resolved');
      const pnlv = resolved ? _float_or_null(dval(t,'pnl')) : null;
      const pAI = _float_or_null(dval(t, 'p_ai'));
      tbody.innerHTML += `<tr>
        <td style="white-space:nowrap">${fmtTs(dval(t,'timestamp'))}</td>
        <td>${assetTag(asset_v)}</td>
        <td><span style="font-size:10px;color:var(--text-3)">${tf_v}</span></td>
        <td>${dirTag(dval(t,'direction'))}</td>
        <td>${dirTag(dval(t,'side'))}</td>
        <td>$${parseFloat(dval(t,'fill_price')||dval(t,'price')||0).toFixed(3)}</td>
        <td>$${parseFloat(dval(t,'size_usd')||0).toFixed(2)}</td>
        <td>${fmtProb(dval(t,'p_bayesian'))}</td>
        <td>${pAI != null ? fmtProb(pAI) : '<span style="color:var(--text-3)">—</span>'}</td>
        <td>${fmtProb(dval(t,'p_final') || dval(t,'p_bayesian'))}</td>
        <td>${fmtProb(dval(t,'ev'))}</td>
        <td style="${parseFloat(dval(t,'pct_move')||0)>=0?'color:#2f9e44':'color:#c92a2a'}">${fmtPct(dval(t,'pct_move'))}</td>
        <td>${dval(t,'seconds_remaining') ? parseFloat(dval(t,'seconds_remaining')).toFixed(0)+'s' : '—'}</td>
        <td>${fmtPnl(pnlv)}</td>
        <td>${outcomeTag(t)}</td>
      </tr>`;
    }
  } catch(e) { console.error(e); }
}

// ── Analytics page ────────────────────────────────────────────────────────────
async function loadAnalytics() {
  try {
    const [statsResp, calResp, pairsResp] = await Promise.all([
      fetch('/api/strategy-stats'),
      fetch('/api/calibration'),
      fetch('/api/pairs'),
    ]);
    const stats = await statsResp.json();
    const cal   = await calResp.json();
    const pairsData = await pairsResp.json();

    // Pairs config table
    const pairsBody = document.getElementById('pairs-body');
    pairsBody.innerHTML = '';
    document.getElementById('pairs-badge').textContent = pairsData.total_enabled + ' pairs enabled';
    for (const p of pairsData.pairs) {
      const perf = p.perf || {};
      const wr = perf.trades > 0 ? Math.round(perf.wr * 100) : 0;
      const pnlColor = (perf.pnl || 0) >= 0 ? '#2f9e44' : '#c92a2a';
      const assetCls = { BTC: 'tag-btc', ETH: 'tag-eth', SOL: 'tag-sol' }[p.asset] || '';
      pairsBody.innerHTML += `<tr>
        <td><span class="tag ${assetCls}" style="font-size:11px">${p.pair}</span></td>
        <td><span class="tag tag-up" style="font-size:10px">ACTIVE</span></td>
        <td><code>${p.min_move_pct}%</code></td>
        <td><code>${(p.min_ev_threshold * 100).toFixed(0)}%</code></td>
        <td><code>${p.max_market_price}</code></td>
        <td><code>T-${p.entry_seconds}s</code></td>
        <td><code>${p.kelly_fraction}x</code></td>
        <td><code>$${p.min_trade_usd}-$${p.max_trade_usd}</code></td>
        <td>${perf.trades || 0}</td>
        <td><span style="font-weight:700;color:${wr>60?'#2f9e44':wr<40?'#c92a2a':'#e67700'}">${perf.trades ? wr+'%' : '—'}</span></td>
        <td style="color:${pnlColor};font-weight:600">${perf.trades ? (perf.pnl>=0?'+':'')+'$'+perf.pnl.toFixed(2) : '—'}</td>
      </tr>`;
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
        segBody.innerHTML += `<tr>
          <td><strong>${k}</strong></td>
          <td>${v.total}</td>
          <td>
            <span style="font-weight:700;color:${wr>60?'#2f9e44':wr<40?'#c92a2a':'#e67700'}">${wr.toFixed(0)}%</span>
            <div style="width:60px;height:4px;background:var(--surface-2);border-radius:2px;display:inline-block;vertical-align:middle;margin-left:8px">
              <div style="width:${wr}%;height:100%;background:${wr>60?'#2f9e44':wr<40?'#c92a2a':'#e67700'};border-radius:2px"></div>
            </div>
          </td>
          <td style="color:${pnlColor};font-weight:600">${v.pnl>=0?'+':''}$${v.pnl.toFixed(2)}</td>
        </tr>`;
      }
    }

    // By pct_move bucket
    const moveBody = document.getElementById('move-body');
    moveBody.innerHTML = '';
    const moves = Object.entries(stats.by_pct_move || {});
    if (moves.length === 0) {
      moveBody.innerHTML = '<tr class="empty-row"><td colspan="3">No data yet</td></tr>';
    } else {
      for (const [bucket, v] of moves) {
        const wr = v.wr * 100;
        moveBody.innerHTML += `<tr>
          <td><code style="font-size:11px">${bucket}</code></td>
          <td>${v.total}</td>
          <td><span style="font-weight:700;color:${wr>60?'#2f9e44':wr<40?'#c92a2a':'#e67700'}">${wr.toFixed(0)}%</span></td>
        </tr>`;
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

    // Calibration table
    const calBody = document.getElementById('cal-body');
    calBody.innerHTML = '';
    const calEntries = Object.entries(cal);
    if (calEntries.length === 0) {
      calBody.innerHTML = '<tr class="empty-row"><td colspan="5">Need more trades to calibrate</td></tr>';
    } else {
      for (const [bucket, v] of calEntries) {
        const diff = v.actual_wr - v.model_p_avg;
        const diffColor = Math.abs(diff) < 0.05 ? '#2f9e44' : Math.abs(diff) < 0.1 ? '#e67700' : '#c92a2a';
        const barW = Math.round(v.actual_wr * 100);
        calBody.innerHTML += `<tr>
          <td><code style="font-size:11px">${bucket}</code></td>
          <td>${v.total}</td>
          <td>${(v.model_p_avg*100).toFixed(1)}%</td>
          <td><strong>${(v.actual_wr*100).toFixed(1)}%</strong></td>
          <td>
            <div style="width:80px;height:6px;background:var(--surface-2);border-radius:3px;display:inline-block;vertical-align:middle">
              <div style="width:${barW}%;height:100%;background:${diffColor};border-radius:3px"></div>
            </div>
            <span style="font-size:10px;color:${diffColor};margin-left:6px">${diff>=0?'+':''}${(diff*100).toFixed(1)}%</span>
          </td>
        </tr>`;
      }
    }
  } catch(e) { console.error(e); }
}

function animateBar() {
  const bar = document.getElementById('refresh-progress');
  bar.style.transition = 'none';
  bar.style.width = '0%';
  setTimeout(() => {
    bar.style.transition = `width ${REFRESH_MS}ms linear`;
    bar.style.width = '100%';
  }, 50);
}

// Boot
initChart();
refresh(); refreshChart(); refreshBalance(); animateBar();
setInterval(() => { refresh(); animateBar(); }, REFRESH_MS);
setInterval(refreshChart, 60000);
setInterval(refreshBalance, 30000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTML


if __name__ == "__main__":
    print("\n  Dashboard: http://localhost:8888\n")
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="warning")
