"""Live dashboard — multi-asset view with strategy tracking."""

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


def get_trades(limit=50):
    if _USE_DYNAMO:
        resp  = _trades_table.scan(Limit=limit)
        items = resp.get("Items", [])
        items.sort(key=lambda x: float(x.get("timestamp", 0)), reverse=True)
        return items[:limit]
    rows = _sqlite_query(
        "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?", (limit,)
    )
    return rows


def get_windows(limit=30):
    if _USE_DYNAMO:
        resp  = _windows_table.scan(Limit=limit)
        items = resp.get("Items", [])
        items.sort(key=lambda x: int(x.get("open_ts", 0)), reverse=True)
        return items[:limit]
    rows = _sqlite_query(
        "SELECT * FROM windows ORDER BY open_ts DESC LIMIT ?", (limit,)
    )
    return rows


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


@app.get("/api/data")
def api_data(_: str = Depends(_require_auth)):
    trades = get_trades()
    windows = get_windows()
    log_lines = get_logs()

    # Filter trades to current mode only — keeps paper and live P&L completely separate
    mode_trades = [t for t in trades if _extract_field(t, "mode", "live") == _TRADE_MODE]

    total_pnl = sum(float(t.get("pnl", 0) or 0) for t in mode_trades if t.get("resolved"))
    wins = sum(1 for t in mode_trades if t.get("resolved") and float(t.get("pnl", 0) or 0) > 0)
    losses = sum(1 for t in mode_trades if t.get("resolved") and float(t.get("pnl", 0) or 0) <= 0)
    open_trades = sum(1 for t in mode_trades if not t.get("resolved"))

    # Per-asset window counts
    asset_windows = {}
    asset_windows_15m = {}
    for w in windows:
        slug = w.get("slug", "") or ""
        # Extract asset from slug (e.g. "eth-updown-5m-..." → "ETH")
        # Fall back to stored asset field if present
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

    # Per-asset × timeframe breakdown (e.g. "BTC 5m", "ETH 15m")
    strategy_pnl = {}
    for t in mode_trades:
        if not t.get("resolved"):
            continue
        asset = _extract_field(t, "asset", "BTC").upper() or "BTC"
        slug = _extract_field(t, "window_slug", "")
        tf = "15m" if "15m" in slug else "5m"
        key = f"{asset} {tf}"
        p = float(t.get("pnl", 0) or 0)
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
        pnl = float(t.get("pnl", 0) or 0)
        try:
            dt = datetime.fromtimestamp(float(ts), tz=timezone.utc)
            # Round down to the hour
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
    import os
    try:
        from polybot.config import Settings
        from polybot.market.balance_checker import BalanceChecker

        settings = Settings()
        # Use the funder/proxy wallet address (shown in Polymarket UI) if set,
        # otherwise fall back to deriving EOA from private key.
        address = settings.polymarket_funder or None
        if not address and settings.polymarket_private_key:
            from eth_account import Account
            address = Account.from_key(settings.polymarket_private_key).address

        if not address:
            return {"polymarket_value": 0.0, "polygon_usdc": 0.0, "error": "no_address"}

        checker = BalanceChecker()
        return await checker.check(address)
    except Exception as e:
        return {"polymarket_value": 0.0, "polygon_usdc": 0.0, "error": str(e)}


HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket Bot — Live Dashboard</title>
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
    z-index: 100;
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
    text-decoration: none;
  }
  .nav-logo {
    width: 30px; height: 30px;
    background: linear-gradient(135deg, #1971c2, #339af0);
    border-radius: 8px;
    display: flex; align-items: center; justify-content: center;
    font-size: 15px; color: #fff; font-weight: 800;
  }
  .nav-title {
    font-size: 16px;
    font-weight: 700;
    color: var(--text);
    letter-spacing: -0.3px;
  }
  .nav-title span { color: var(--blue); }
  .nav-right {
    display: flex;
    align-items: center;
    gap: 16px;
  }
  .nav-meta {
    font-size: 12px;
    color: var(--text-3);
    display: flex;
    align-items: center;
    gap: 12px;
  }
  .nav-meta .sep { color: var(--border-2); }
  .status-dot {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-size: 12px;
    font-weight: 600;
    color: var(--green);
    background: var(--green-bg);
    border: 1px solid var(--green-bd);
    padding: 3px 10px;
    border-radius: 20px;
  }
  .status-dot::before {
    content: '';
    width: 7px; height: 7px;
    background: var(--green);
    border-radius: 50%;
    animation: pulse 2s infinite;
  }
  @keyframes pulse {
    0%, 100% { opacity: 1; transform: scale(1); }
    50% { opacity: 0.6; transform: scale(0.85); }
  }

  /* ── Layout ── */
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
  .stat-card:hover {
    box-shadow: var(--shadow-md);
    transform: translateY(-1px);
  }
  .stat-label {
    font-size: 11px;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.8px;
    color: var(--text-3);
    margin-bottom: 8px;
  }
  .stat-value {
    font-size: 26px;
    font-weight: 800;
    letter-spacing: -0.5px;
    color: var(--text);
    line-height: 1;
  }
  .stat-value.green { color: var(--green); }
  .stat-value.red   { color: var(--red); }
  .stat-value.blue  { color: var(--blue); }
  .stat-value.gold  { color: var(--gold); }
  .stat-sub {
    font-size: 11px;
    color: var(--text-3);
    margin-top: 5px;
    font-weight: 500;
  }

  /* ── Section headers ── */
  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
  }
  .section-title {
    font-size: 13px;
    font-weight: 700;
    color: var(--text-2);
    text-transform: uppercase;
    letter-spacing: 0.6px;
  }
  .section-badge {
    font-size: 11px;
    font-weight: 600;
    color: var(--text-3);
    background: var(--surface-2);
    border: 1px solid var(--border);
    padding: 2px 8px;
    border-radius: 20px;
  }

  /* ── Strategy cards ── */
  .strategy-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
    margin-bottom: 20px;
  }
  #strategy-section {
    display: contents;
  }
  .strat-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 16px 18px;
    box-shadow: var(--shadow-sm);
  }
  .strat-card-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 10px;
  }
  .strat-name {
    font-size: 12px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.6px;
    color: var(--text-2);
  }
  .strat-pnl {
    font-size: 20px;
    font-weight: 800;
    letter-spacing: -0.4px;
    margin-bottom: 8px;
  }
  .strat-meta {
    font-size: 11px;
    color: var(--text-3);
    margin-bottom: 8px;
    font-weight: 500;
  }
  .win-bar-wrap {
    height: 5px;
    background: var(--surface-2);
    border-radius: 3px;
    overflow: hidden;
  }
  .win-bar-fill {
    height: 100%;
    border-radius: 3px;
    background: linear-gradient(90deg, #2f9e44, #51cf66);
    transition: width .6s cubic-bezier(.4,0,.2,1);
  }

  /* ── Chart card ── */
  .chart-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px 22px;
    box-shadow: var(--shadow-sm);
    margin-bottom: 20px;
  }
  .chart-card .section-header { margin-bottom: 16px; }
  #pnl-chart-wrap { height: 200px; }

  /* ── Two-column panels ── */
  .panels-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    margin-bottom: 20px;
  }
  .panel-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow-sm);
    overflow: hidden;
  }
  .panel-head {
    padding: 12px 16px;
    border-bottom: 1px solid var(--border);
    background: var(--surface);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  /* ── Tables ── */
  .scroll-wrap { max-height: 340px; overflow-y: auto; }
  table { width: 100%; border-collapse: collapse; }
  thead { position: sticky; top: 0; z-index: 1; }
  th {
    text-align: left;
    font-size: 11px;
    font-weight: 600;
    color: var(--text-3);
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 9px 14px;
    background: var(--surface-2);
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }
  td {
    padding: 9px 14px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    color: var(--text-2);
    vertical-align: middle;
  }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover td { background: var(--surface-2); }
  .empty-row td {
    color: var(--text-3);
    text-align: center;
    padding: 32px 16px;
    font-style: italic;
    font-size: 13px;
  }

  /* ── Tags / badges ── */
  .tag {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.3px;
    white-space: nowrap;
  }
  .tag-up    { background: var(--green-bg); color: var(--green); border: 1px solid var(--green-bd); }
  .tag-down  { background: var(--red-bg);   color: var(--red);   border: 1px solid var(--red-bd); }
  .tag-open  { background: var(--blue-bg);  color: var(--blue);  border: 1px solid var(--blue-bd); }
  .tag-btc   { background: var(--btc-bg);   color: var(--btc);   border: 1px solid #ffd8a8; }
  .tag-eth   { background: var(--eth-bg);   color: var(--eth);   border: 1px solid #bac8ff; }
  .tag-sol   { background: var(--sol-bg);   color: var(--sol);   border: 1px solid #d0bfff; }
  .tag-dir   { background: var(--blue-bg);  color: var(--blue);  border: 1px solid var(--blue-bd); }
  .tag-arb   { background: var(--green-bg); color: var(--green); border: 1px solid var(--green-bd); }
  .tag-copy  { background: var(--btc-bg);   color: var(--btc);   border: 1px solid #ffd8a8; }
  .tag-news  { background: var(--sol-bg);   color: var(--sol);   border: 1px solid #d0bfff; }

  /* ── Logs ── */
  .logs-card {
    background: #1a1b26;
    border: 1px solid #2a2b3d;
    border-radius: var(--radius);
    overflow: hidden;
    box-shadow: var(--shadow-sm);
  }
  .logs-head {
    padding: 12px 16px;
    border-bottom: 1px solid #2a2b3d;
    background: #16172a;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .logs-head .section-title { color: #a9b1d6; }
  .logs-head .section-badge {
    background: #2a2b3d;
    border-color: #3a3b4d;
    color: #565f89;
  }
  #logs {
    background: #1a1b26;
    padding: 12px 16px;
    max-height: 240px;
    overflow-y: auto;
    font-family: 'JetBrains Mono', 'Fira Code', 'SF Mono', ui-monospace, monospace;
    font-size: 12px;
    line-height: 1.9;
  }
  #logs::-webkit-scrollbar { width: 6px; }
  #logs::-webkit-scrollbar-track { background: #1a1b26; }
  #logs::-webkit-scrollbar-thumb { background: #2a2b3d; border-radius: 3px; }
  .log-line { white-space: pre-wrap; word-break: break-all; padding-left: 10px; border-left: 2px solid transparent; }
  .log-line.error  { color: #f7768e; border-left-color: #f7768e; }
  .log-line.warn   { color: #e0af68; border-left-color: #e0af68; }
  .log-line.trade  { color: #9ece6a; border-left-color: #9ece6a; background: rgba(158,206,106,.04); }
  .log-line.signal { color: #bb9af7; border-left-color: #bb9af7; }
  .log-line.entry  { color: #7aa2f7; border-left-color: #3d59a1; }
  .log-line.window { color: #565f89; }
  .log-line.info   { color: #3b4261; }

  /* ── Responsive ── */
  @media (max-width: 1100px) {
    .stats-grid { grid-template-columns: repeat(4, 1fr); }
    .strategy-grid { grid-template-columns: repeat(2, 1fr); }
  }
  @media (max-width: 768px) {
    .page { padding: 12px 16px 32px; }
    nav { padding: 0 16px; }
    .stats-grid { grid-template-columns: repeat(2, 1fr); }
    .strategy-grid { grid-template-columns: 1fr 1fr; }
    .panels-grid { grid-template-columns: 1fr; }
    .nav-meta { display: none; }
    .stat-value { font-size: 22px; }
  }
  @media (max-width: 480px) {
    .stats-grid { grid-template-columns: repeat(2, 1fr); }
    .strategy-grid { grid-template-columns: 1fr; }
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
    <div class="stat-card">
      <div class="stat-label">SOL Windows</div>
      <div class="stat-value" id="s-sol">—</div>
      <div class="stat-sub" id="s-sol-sub">5m + 15m</div>
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
            <th>Time</th><th>Asset</th><th>Side</th><th>Strategy</th>
            <th>Price</th><th>Size</th><th>P&amp;L</th><th>Status</th><th>Market</th>
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

</div><!-- /page -->

<script>
const REFRESH_MS = 4000;

// ── Chart.js cumulative P&L area chart ──────────────────────────────────────
let pnlChart = null;

function initChart() {
  const ctx = document.getElementById('pnl-chart').getContext('2d');

  // Gradient fill
  const gradient = ctx.createLinearGradient(0, 0, 0, 200);
  gradient.addColorStop(0,   'rgba(47,158,68,.28)');
  gradient.addColorStop(0.6, 'rgba(47,158,68,.06)');
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
        pointBackgroundColor: '#2f9e44',
        pointBorderColor: '#fff',
        pointBorderWidth: 1.5,
        pointRadius: 3,
        pointHoverRadius: 5,
        tension: 0.4,
        fill: true,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: 'index', intersect: false },
      plugins: {
        legend: { display: false },
        tooltip: {
          backgroundColor: '#fff',
          borderColor: '#dee2e6',
          borderWidth: 1,
          titleColor: '#868e96',
          bodyColor: '#212529',
          padding: 10,
          callbacks: {
            label: ctx => {
              const v = ctx.raw;
              return (v >= 0 ? '+' : '') + '$' + v.toFixed(4);
            },
          },
        },
      },
      scales: {
        x: {
          ticks: {
            color: '#868e96',
            font: { size: 11, family: 'Inter' },
            maxTicksLimit: 10,
          },
          grid: { color: '#f1f3f5' },
          border: { color: '#dee2e6' },
        },
        y: {
          ticks: {
            color: '#868e96',
            font: { size: 11, family: 'Inter' },
            callback: v => (v >= 0 ? '+' : '') + '$' + v.toFixed(2),
          },
          grid: { color: '#f1f3f5' },
          border: { color: '#dee2e6' },
        },
      },
    },
  });
}

async function refreshChart() {
  try {
    const resp = await fetch('/api/pnl-history');
    const data = await resp.json();
    if (!pnlChart) return;

    // Compute cumulative values from hourly buckets
    const cumulative = [];
    let running = 0;
    for (const v of data.values) {
      running += v;
      cumulative.push(parseFloat(running.toFixed(4)));
    }

    pnlChart.data.labels = data.labels.map(l => l.substring(5).replace('T', ' ')); // MM-DD HH:00
    pnlChart.data.datasets[0].data = cumulative;

    // Dynamic color: green if final equity positive, red if negative
    const finalVal = cumulative.length ? cumulative[cumulative.length - 1] : 0;
    const posColor = finalVal >= 0 ? '#2f9e44' : '#c92a2a';
    const ctx = document.getElementById('pnl-chart').getContext('2d');
    const gradient = ctx.createLinearGradient(0, 0, 0, 200);
    if (finalVal >= 0) {
      gradient.addColorStop(0,   'rgba(47,158,68,.28)');
      gradient.addColorStop(0.6, 'rgba(47,158,68,.06)');
      gradient.addColorStop(1,   'rgba(47,158,68,0)');
    } else {
      gradient.addColorStop(0,   'rgba(201,42,42,.22)');
      gradient.addColorStop(0.6, 'rgba(201,42,42,.04)');
      gradient.addColorStop(1,   'rgba(201,42,42,0)');
    }
    pnlChart.data.datasets[0].borderColor = posColor;
    pnlChart.data.datasets[0].pointBackgroundColor = posColor;
    pnlChart.data.datasets[0].backgroundColor = gradient;
    pnlChart.update('none');

    // Update badge
    const badge = document.getElementById('chart-badge');
    badge.textContent = data.labels.length + ' hourly buckets';
  } catch(e) { /* non-fatal */ }
}

// ── Balance (every 30s) ──────────────────────────────────────────────────────
let lastBalanceFetch = 0;

async function refreshBalance() {
  if (Date.now() - lastBalanceFetch < 30000) return;
  lastBalanceFetch = Date.now();
  try {
    const resp = await fetch('/api/balance');
    const d = await resp.json();
    const polygon = d.polygon_usdc || 0;
    const pmval = d.polymarket_value || 0;
    const total = polygon + pmval;
    document.getElementById('s-balance').textContent = '$' + total.toFixed(2);
    document.getElementById('s-balance-sub').textContent =
      'USDC $' + polygon.toFixed(2) + ' + positions $' + pmval.toFixed(2);
  } catch(e) { /* non-fatal */ }
}

// ── Formatters ───────────────────────────────────────────────────────────────
function assetTag(a) {
  const m = { BTC: 'tag-btc', ETH: 'tag-eth', SOL: 'tag-sol' };
  return `<span class="tag ${m[a]||''}">${a||'BTC'}</span>`;
}
function stratTag(s) {
  const m = { directional:'tag-dir', arbitrage:'tag-arb', copy:'tag-copy', news:'tag-news' };
  return `<span class="tag ${m[s]||''}">${s||'—'}</span>`;
}
function dirTag(d) {
  if (!d) return '—';
  const up = d === 'YES' || d === 'up';
  return `<span class="tag ${up ? 'tag-up' : 'tag-down'}">${d}</span>`;
}
function fmtTs(ts)  { return ts ? new Date(parseFloat(ts)*1000).toLocaleTimeString() : '—'; }
function fmtTs2(ts) { return ts ? new Date(parseInt(ts)*1000).toLocaleTimeString() : '—'; }
function fmtPnl(p) {
  if (p == null) return '—';
  const v = parseFloat(p);
  const c = v >= 0 ? '#2f9e44' : '#c92a2a';
  return `<span style="color:${c};font-weight:600">${v>=0?'+':''}$${v.toFixed(4)}</span>`;
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

// ── Main refresh ─────────────────────────────────────────────────────────────
async function refresh() {
  try {
    const resp = await fetch('/api/data');
    const data = await resp.json();
    const s = data.stats;

    // Mode badge + bankroll
    const mode = (s.mode || 'paper').toUpperCase();
    const modeBadge = document.getElementById('mode-badge');
    modeBadge.textContent = mode;
    modeBadge.style.background = mode === 'LIVE' ? '#c92a2a' : '#1971c2';

    const bankrollEl = document.getElementById('s-bankroll');
    bankrollEl.textContent = '$' + (s.current_bankroll || s.starting_bankroll || 0).toFixed(2);
    document.getElementById('bankroll-label').textContent = mode === 'LIVE' ? 'Bankroll' : 'Virtual Bankroll';
    document.getElementById('s-bankroll-sub').textContent = mode === 'LIVE' ? 'Live trading' : 'Paper trading';
    document.getElementById('s-starting').textContent = (s.starting_bankroll || 1000).toFixed(0);

    // Stats
    const pnl = s.total_pnl;
    const pnlEl = document.getElementById('s-pnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + '$' + pnl.toFixed(4);
    pnlEl.className = 'stat-value ' + (pnl >= 0 ? 'green' : 'red');

    document.getElementById('s-wl').textContent   = s.wins + ' / ' + s.losses;
    document.getElementById('s-open').textContent = s.open_trades;
    const w15 = s.asset_windows_15m || {};
    document.getElementById('s-btc').textContent = (s.asset_windows.BTC || 0);
    document.getElementById('s-eth').textContent = (s.asset_windows.ETH || 0);
    document.getElementById('s-sol').textContent = (s.asset_windows.SOL || 0);
    document.getElementById('s-btc-sub').textContent = `5m: ${s.asset_windows.BTC||0}  15m: ${w15.BTC||0}`;
    document.getElementById('s-eth-sub').textContent = `5m: ${s.asset_windows.ETH||0}  15m: ${w15.ETH||0}`;
    document.getElementById('s-sol-sub').textContent = `5m: ${s.asset_windows.SOL||0}  15m: ${w15.SOL||0}`;

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
          <div class="strat-card-header">
            <span class="strat-name">${st}</span>
          </div>
          <div class="strat-pnl" style="color:${pnlColor}">${d.pnl>=0?'+':''}$${d.pnl.toFixed(2)}</div>
          <div class="strat-meta">${d.count} trades &nbsp;&middot;&nbsp; ${wr}% win rate</div>
          <div class="win-bar-wrap">
            <div class="win-bar-fill" style="width:${wr}%;${d.pnl<0?'background:linear-gradient(90deg,#c92a2a,#fa5252)':''}"></div>
          </div>
        </div>`;
    }
    document.getElementById('strategy-section').innerHTML = scHtml;

    document.getElementById('trade-count').textContent  = data.trades.length + ' trades';
    document.getElementById('window-count').textContent = data.windows.length + ' windows';

    // Trades table
    const tbody = document.getElementById('trades-body');
    tbody.innerHTML = '';
    if (data.trades.length === 0) {
      tbody.innerHTML = '<tr class="empty-row"><td colspan="8">Waiting for first trade signal...</td></tr>';
    } else {
      for (const t of data.trades.slice(0, 20)) {
        const asset    = dval(t,'asset')  || 'BTC';
        const side     = dval(t,'side')   || '';
        const source   = dval(t,'source') || '';
        const resolved = t.resolved || dval(t,'resolved');
        const pnlv     = dval(t,'pnl');
        const status   = resolved
          ? (parseFloat(pnlv||0) >= 0
              ? '<span class="tag tag-up">WIN</span>'
              : '<span class="tag tag-down">LOSS</span>')
          : '<span class="tag tag-open">OPEN</span>';
        const slug = dval(t,'window_slug') || '';
        const pmUrl = slug ? `https://polymarket.com/event/${slug}` : null;
        const slugCell = pmUrl
          ? `<a href="${pmUrl}" target="_blank" style="color:var(--blue);font-size:11px;text-decoration:none;font-family:monospace" title="${slug}">${slug.substring(0,22)}…</a>`
          : '—';
        tbody.innerHTML += `<tr>
          <td>${fmtTs(dval(t,'timestamp'))}</td>
          <td>${assetTag(asset)}</td>
          <td>${dirTag(side)}</td>
          <td>${stratTag(source)}</td>
          <td>$${parseFloat(dval(t,'price')||0).toFixed(3)}</td>
          <td>$${parseFloat(dval(t,'size_usd')||0).toFixed(2)}</td>
          <td>${fmtPnl(pnlv)}</td>
          <td>${status}</td>
          <td>${slugCell}</td>
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
          ? (dir === 'up'
              ? '<span class="tag tag-up">UP</span>'
              : '<span class="tag tag-down">DOWN</span>')
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
        if (obj.level === 'error')                               cls = 'log-line error';
        else if (obj.level === 'warning')                        cls = 'log-line warn';
        else if (ev.includes('signal') || ev.includes('arb'))   cls = 'log-line signal';
        else if (ev.includes('order') || ev.includes('trade'))  cls = 'log-line trade';
        else if (ev.includes('entry_zone'))                      cls = 'log-line entry';
        else if (ev.includes('window_'))                         cls = 'log-line window';
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
setInterval(refreshChart, 60000);   // P&L chart: every 60s
setInterval(refreshBalance, 30000); // Balance: every 30s
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTML


if __name__ == "__main__":
    print("\n  Dashboard: http://localhost:8888\n")
    uvicorn.run(app, host="0.0.0.0", port=8888, log_level="warning")
