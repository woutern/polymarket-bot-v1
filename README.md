# Polymarket Trading Bot

Algorithmic trading bot for Polymarket crypto binary prediction markets.

**Status:** LIVE — $225 portfolio, all 6 pairs active
**Dashboard:** http://54.155.183.45:8888/
**Tests:** 336 passing

## Strategy

Trades BTC/ETH/SOL × 5m/15m Up/Down windows on Polymarket.
Enters T+2s-T+15s after window open when price moves above threshold.
LightGBM models filter low-confidence signals. Dynamic Kelly sizing.

### Thresholds (backtested 30 days, 50K+ windows)

| Pair | Threshold | WR | Signals/day |
|------|-----------|-----|-------------|
| BTC 5m | 0.02% | 68% | ~24 |
| ETH 5m | 0.02% | 67% | ~28 |
| SOL 5m | 0.02% | 64% | ~30 |
| BTC 15m | 0.15% | 73% | ~4 |
| ETH 15m | 0.15% | 73% | ~8 |
| SOL 15m | 0.15% | 73% | ~10 |

### Entry Filters (all must pass)
1. Price move > threshold in first 15 seconds
2. Vol ratio 0.5-3.0 (not too quiet, not too wild)
3. Body ratio > 0.4 (decisive candle)
4. Previous window same direction
5. Spread < $0.10
6. Ask < $0.60
7. EV > 0.05
8. LightGBM prob > adaptive threshold (0.52-0.60, adjusts to model confidence)

### Sizing (Dynamic Kelly)
- lgbm_prob < 0.60: 0.5% of wallet
- lgbm_prob 0.60-0.70: 1.0%
- lgbm_prob 0.70-0.80: 1.5%
- lgbm_prob > 0.80: 2.0%
- Min $1.00, max $5.00

## Architecture

```
eu-west-1 (Trading)
├── Bot — ECS Fargate (250ms tick loop, 24/7)
│   ├── CoinbaseWS — ticker + level2 orderbook (OFI, spread, depth)
│   ├── RTDS — Chainlink oracle prices
│   ├── 8 Entry Filters + LightGBM (signal-weighted, 14 features)
│   ├── LiveTrader — FOK orders on CLOB
│   ├── DynamoDB dedup (survives restarts)
│   ├── Gamma API resolution (90s + 5 retries)
│   ├── KPI Tracker (BSS, SPRT, per-pair)
│   ├── Heartbeat + Docker HEALTHCHECK watchdog
│   └── Smoke test (9 checks on startup)

us-east-1 (Data + Models)
├── DynamoDB — trades, windows, signals, training_data, kpi_snapshots
├── S3 — 6 LightGBM model artifacts
├── SSM — model paths + metrics
├── Dashboard EC2 (54.155.183.45:8888)
│   ├── 5 pages: Overview, Trade Log, Signals, Analytics, KPIs
│   └── P&L from Polymarket data-api
└── EventBridge — retrain every 4h
```

## Safety

- Circuit breakers: 3 streak → 15min pause, 5/20 losses → $1 flat, 10% daily → stop
- Dedup: DynamoDB + memory (one trade per window, survives restarts)
- Resolution: Gamma API verified (all 88 trades confirmed via Polymarket oracle)
- Watchdog: heartbeat every 60s, Docker HEALTHCHECK restarts if frozen >5min
- Orphan resolver: resolves stale trades on startup
- Backfill script: `scripts/backfill_verification.py` upgrades legacy coinbase_inferred → polymarket_verified

## Commands

```bash
./scripts/switch.sh live|paper
uv run python scripts/force_trade.py --asset BTC
uv run python scripts/redeem.py
uv run pytest tests/ -q                    # 336 tests
uv run python scripts/backfill_verification.py  # one-time: verify old trades
PYTHONPATH=src uv run python -c \
  "from polybot.ml.trainer import train_all; train_all()"
```
