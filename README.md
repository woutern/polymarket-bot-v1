# Polymarket Trading Bot

Algorithmic trading bot for Polymarket crypto binary prediction markets. Trades BTC/ETH/SOL on 5-minute and 15-minute Up/Down windows.

**Status:** LIVE TRADING — $260 portfolio, 1% per trade
**Dashboard:** http://54.155.183.45:8888/
**Region:** Bot eu-west-1 (trading), DynamoDB/Models us-east-1
**Tests:** 281 passing

---

## Strategy

**Entry:** T+2s to T+15s after window open (first-mover advantage)
**Signal:** Price move >0.05% + 5 quality filters + LightGBM confidence >0.55
**Target ask:** $0.45-$0.60 (cheap, before market prices in)
**Resolution:** Gamma API verification (90s wait + 3 retries, never Coinbase-inferred)

### Entry Filters (all must pass)
1. Momentum: |move| > 0.05% in first 15 seconds
2. Vol ratio: 0.5 < vol/vol_ma < 3.0 (not too quiet, not too wild)
3. Body ratio: > 0.4 (decisive candle, not indecisive)
4. Previous window: same direction (momentum continuation)
5. Spread: bid-ask < $0.10 (liquid market)
6. Max ask: < $0.60 (cheap entry only)
7. LightGBM: prob > 0.55 (model confident)

### P&L Tracking
- P&L = Portfolio value - Total deposited (matches Polymarket UI)
- All resolved trades verified via Polymarket Gamma API
- Never resolved from Coinbase price inference

---

## Architecture

```
eu-west-1 (Ireland) — Trading
├── Bot — ECS Fargate (250ms tick loop)
│   ├── CoinbaseWS — real-time BTC/ETH/SOL price feed
│   ├── RTDS — Chainlink oracle prices (oracle lag detection)
│   ├── 5 Entry Filters + LightGBM → Signal
│   ├── LiveTrader — FOK orders on Polymarket CLOB
│   ├── KPI Tracker — BSS, SPRT, win rates after every trade
│   └── Smoke test on startup (9 checks)
│
us-east-1 (Virginia) — Data + Models
├── DynamoDB — trades, windows, signals, training_data, kpi_snapshots
├── S3 — 6 LightGBM model artifacts
├── SSM — model paths + metrics
├── Dashboard EC2 (54.155.183.45:8888)
│   ├── 5 pages: Overview, Trade Log, Signals, Analytics, KPIs
│   ├── P&L from Polymarket data-api (source of truth)
│   └── CET timestamps
│
└── Models (6 LightGBM classifiers)
    ├── BTC_5m  — Brier 0.222, AUC 0.676
    ├── BTC_15m — Brier 0.232, AUC 0.641
    ├── ETH_5m  — Brier 0.221, AUC 0.680
    ├── ETH_15m — Brier 0.233, AUC 0.629
    ├── SOL_5m  — Brier 0.218, AUC 0.688
    └── SOL_15m — Brier 0.226, AUC 0.658
```

---

## Quick Commands

```bash
# Switch mode
./scripts/switch.sh live
./scripts/switch.sh paper

# Force test trade
uv run python scripts/force_trade.py --asset BTC --amount 2.50

# Run tests
uv run pytest tests/ -q

# Retrain models
PYTHONPATH=src uv run python -c "from polybot.ml.trainer import train_all; train_all()"

# Deploy bot
docker build --platform linux/amd64 -t polymarket-bot:latest .
docker tag polymarket-bot:latest 688567279867.dkr.ecr.eu-west-1.amazonaws.com/polymarket-bot:latest
docker push 688567279867.dkr.ecr.eu-west-1.amazonaws.com/polymarket-bot:latest

# Deploy dashboard
docker build --platform linux/amd64 -f Dockerfile.dashboard -t polymarket-dashboard:latest .
docker tag polymarket-dashboard:latest 688567279867.dkr.ecr.us-east-1.amazonaws.com/polymarket-dashboard:latest
docker push 688567279867.dkr.ecr.us-east-1.amazonaws.com/polymarket-dashboard:latest
```

---

## Test Coverage — 281 tests

| File | Tests | What |
|------|-------|------|
| test_sizing.py | 25 | Kelly, min/max trade, bankroll edge cases |
| test_bayesian.py | 19 | Probability updates, EMA blending |
| test_window_tracker.py | 13 | State machine transitions |
| test_directional.py | 26 | Signal guards, SignalEvaluation rejections |
| test_risk_manager.py | 24 | Circuit breakers, streak detection |
| test_latency_monitor.py | 21 | p50/p95 latency stats |
| test_base_rate.py | 8 | Base rate table |
| test_paper_trader.py | 5 | Dedup, price floor |
| test_outcome_verification.py | 17 | Gamma API, resolution logic |
| test_live_trader.py | 9 | create_order, tick_size, metadata |
| test_storage.py | 10 | SQLite CRUD, migrations |
| test_dashboard_api.py | 14 | API endpoints, auth |
| test_region_config.py | 6 | No eu-west-1 in code |
| test_latency_fields.py | 4 | Timing fields |
| test_force_trade.py | 9 | Dynamic sizing, signal evaluation |
| test_rtds.py | 28 | Oracle lag, Black-Scholes, RTDS parsing |
| test_vol_filters.py | 9 | vol_ratio, body_ratio |
| test_ml.py | 16 | LightGBM trainer, model server |
| test_kpi.py | 18 | Brier score, BSS, SPRT, KPI snapshots |

---

## KPI Dashboard (Page 5)

- **Brier Skill Score**: Are we better than the market?
- **SPRT Edge Detection**: Statistical proof of edge (accumulating → confirmed → reassess)
- **Win Rate**: Last 20 / Last 50 / All time
- **Model Separation**: Does LightGBM predict wins vs losses differently?
- **Risk**: Sharpe ratio, max drawdown, daily P&L
- **Per Pair**: Breakdown by BTC/ETH/SOL × 5m/15m

---

## Safety Features

- Circuit breakers: 3 streak → 15min pause, 5/20 losses → $1 flat, 10% daily → stop
- Dedup: memory + DynamoDB check (one trade per window)
- Resolution: Gamma API only (90s + 3 retries), never Coinbase inference
- Smoke test: 9 checks on startup (CLOB, Coinbase, Gamma, DynamoDB, S3, creds)
- Auto-claim: disabled (Gnosis Safe proxy — claim manually in UI)
- LightGBM: blocks trades when prob < 0.55
