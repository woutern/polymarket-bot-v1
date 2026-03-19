# Polymarket Trading Bot

Algorithmic trading bot for Polymarket crypto binary prediction markets.

**Status:** LIVE — BTC/ETH/SOL 5m windows only
**Dashboard:** https://d2rj5lnnfnptd.cloudfront.net/
**Tests:** 384 passing

## Strategy

Trades BTC/ETH/SOL × 5m Up/Down windows on Polymarket.
Every window is scored 0-5 using a confidence scoring engine at T+12s.
LightGBM models + hard filter override for high-conviction entries.

### Scored Entry System

Every 5-minute window is evaluated with 5 confirmation signals:

| Signal | What it checks |
|--------|---------------|
| **OFI** | Order flow imbalance positive and increasing T+2s → T+8s |
| **No Reversal** | Price still moving same direction at T+8s as T+2s |
| **Cross-Asset** | BTC confirms direction (ETH/SOL only) |
| **PM Pressure** | Polymarket ask stable or improving since open |
| **Volume** | Window volume > 1.5x average of prior 5 windows |

### Entry Rules

| Condition | Action |
|-----------|--------|
| **Hard filter override** (lgbm ≥ 0.65, ask ≤ $0.55, ev ≥ 0.10) | Taker FOK — enters regardless of score |
| Score 4-5 + lgbm ≥ 0.60 + ask ≤ $0.55 + ev ≥ 0.08 | Taker FOK |
| Score 2-3 + lgbm ≥ 0.55 | Maker GTC at $0.48 (cancel after 8s) |
| Score 0-1, no override | Skip |

### Hard Limits (enforced by smoke test on every startup)

| Parameter | Value | Enforced by |
|-----------|-------|-------------|
| Max ask price | $0.55/share | Code + Secrets Manager |
| Max bet size | $1.50/trade | `HARDCODED_MAX_BET` constant |
| Min EV | 8% | Code + Secrets Manager |
| Min LightGBM prob | 0.60 | Code (adaptive threshold) |

### Pairs (5m only — 15m disabled after negative SPRT)

| Pair | Live WR | SPRT | Status |
|------|---------|------|--------|
| BTC 5m | 67% | +0.17 | Accumulating |
| ETH 5m | 62% | +0.16 | Accumulating |
| SOL 5m | 60% | +0.08 | Accumulating |

$1.00 flat bets until SPRT confirms edge at boundary 2.77.

## Architecture

```
eu-west-1 (Trading)
├── Bot — ECS Fargate (250ms tick loop, 24/7)
│   ├── CoinbaseWS — ticker + level2 orderbook (OFI, spread, depth)
│   ├── RTDS — Chainlink oracle prices
│   ├── Scored Entry Engine (5 signals at T+12s)
│   ├── LightGBM (3 models, signal-weighted, 14 features)
│   ├── Hard Filter Override (lgbm≥0.65 + ask≤$0.55 + ev≥0.10)
│   ├── LiveTrader — FOK taker + GTC maker on CLOB
│   ├── 3-layer dedup (memory + DynamoDB query + atomic claim)
│   ├── Gamma API resolution (90s + 6 retries)
│   ├── Binance long/short ratio (liq_cluster_bias)
│   ├── Heartbeat + Docker HEALTHCHECK watchdog
│   └── Smoke test (12 checks on startup, halts on threshold violation)

us-east-1 (Data + Models)
├── DynamoDB — trades, windows, signals, training_data, kpi_snapshots
├── S3 — 3 LightGBM model artifacts
├── SSM — model paths + metrics
├── Dashboard — Lambda + API Gateway + CloudFront (HTTPS)
│   ├── 5 pages: Overview, Trade Log, Window Scores, Analytics, KPIs
│   ├── Score breakdown per window (OFI/NoRev/Cross/PM/Vol)
│   ├── P&L from Polymarket activity API
│   └── Mobile hamburger menu
└── EventBridge — retrain every 4h
```

## Safety

- **Smoke test**: halts bot if max_bet > $1.50, max_ask > $0.55, min_ev < 0.08, or min_lgbm < 0.60
- **3-layer dedup**: memory set + DynamoDB query + atomic conditional put (cross-container safe)
- **Circuit breakers**: 3 consecutive losses → 15min pause, 5/20 losses → $1 flat, 10% daily → stop
- **Resolution**: Gamma API verified (Polymarket Chainlink oracle, not Coinbase)
- **Watchdog**: heartbeat every 60s, Docker HEALTHCHECK restarts if frozen >5min

## Commands

```bash
./scripts/switch.sh live|paper
uv run python scripts/force_trade.py --asset BTC
uv run python scripts/redeem.py
uv run pytest tests/ -q                            # 384 tests
bash scripts/deploy_dashboard_lambda.sh             # deploy dashboard to Lambda
PYTHONPATH=src uv run python -c \
  "from polybot.ml.trainer import train_all; train_all()"
```
