# Scaleflow Polymarket Trading Bot — Claude Code Context

> Read this file fully at the start of every session. This is the single source of truth.

---

## What this project is

An algorithmic trading bot for Polymarket crypto binary prediction markets.
We trade BTC/ETH/SOL × 5min Up/Down markets (3 pairs: BTC_5m, ETH_5m, SOL_5m).
15m pairs disabled — SPRT negative, sub-40% WR on all three.
Binary bets: does price close higher or lower than the open at window start?

Owner: Wouter (Scaleflow)
Stack: Python, AWS (eu-west-1 bot, us-east-1 data), Polygon blockchain
Live wallet: ~$240 on Polymarket, $1 flat bets until SPRT confirms edge

---

## How it trades (scored confirmation system)

1. Coinbase WebSocket feeds live prices every 250ms (ticker + level2 orderbook)
2. Window opens → bot tracks price, OFI, volume from open
3. At T+2s: capture price and OFI baseline
4. At T+8s: capture price and OFI again
5. At T+12s: compute confidence score 0-5 from 5 signals
6. Decision: override / taker / maker / skip (see Entry Rules below)
7. Execution: FOK or GTC limit order on Polymarket CLOB
8. After close → verify outcome via Polymarket Gamma API (Chainlink oracle)
9. After resolution → update SPRT, KPI tracker

---

## Current architecture

### AWS Infrastructure
- Bot: ECS Fargate, eu-west-1 — always-on, 250ms tick loop
- Dashboard: Lambda + API Gateway + CloudFront (HTTPS) — https://d2rj5lnnfnptd.cloudfront.net/
- Storage: DynamoDB us-east-1 (trades, windows, signals, training_data, kpi_snapshots)
- Models: S3 us-east-1 — 3 LightGBM artifacts, paths in SSM Parameter Store
- Secrets: AWS Secrets Manager eu-west-1 (`polymarket-bot-env`)
- Auto-retrain: EventBridge every 4h

### Why split regions
- Bot in eu-west-1: Polymarket CLOB geoblocks us-east-1 AWS IPs
- Data/models in us-east-1: Coinbase proximity, Bedrock native

---

## Data feeds (three WebSocket connections)

### Feed 1: Coinbase WebSocket (price + order book)
- URL: wss://advanced-trade-ws.coinbase.com
- Channels: ticker (250ms price), l2_data (level2 orderbook for OFI/depth)
- Assets: BTC-USD, ETH-USD, SOL-USD
- Computed features: ofi_30s, bid_ask_spread, depth_imbalance, trade_arrival_rate

### Feed 2: Polymarket RTDS (oracle lag signal)
- URL: wss://ws-live-data.polymarket.com
- Used for: oracle_lag_ms, oracle_dislocation signal (Tier A entries)

### Feed 3: Polymarket CLOB WebSocket
- Market channel: wss://ws-subscriptions-clob.polymarket.com/ws/market
- Used for: live order book (yes_ask/yes_bid)

---

## Scored entry system (replaces old tier-based entry)

### 5 Confirmation Signals (computed at T+12s)

| Signal | Condition | +1 if true |
|--------|-----------|-----------|
| OFI | Order flow imbalance increasing T+2s → T+8s in trade direction | Yes |
| No Reversal | Price at T+8s still same direction as T+2s | Yes |
| Cross-Asset | BTC moved same direction (ETH/SOL only, N/A for BTC) | Yes |
| PM Pressure | Polymarket ask stable or improving (2c tolerance) | Yes |
| Volume | Window tick count > 1.5x average of prior 5 windows | Yes |

### Entry Rules

| Condition | Action | Order type |
|-----------|--------|-----------|
| **Hard filter override**: lgbm ≥ 0.65 AND ask ≤ $0.55 AND ev ≥ 0.10 | ENTER regardless of score | Taker FOK |
| Score 4-5 + lgbm ≥ 0.60 + ask ≤ $0.55 + ev ≥ 0.08 | ENTER | Taker FOK |
| Score 2-3 + lgbm ≥ 0.55 | ENTER | Maker GTC at $0.48, cancel after 8s |
| Score 0-1, no override | SKIP | — |

### Hard Limits (enforced by smoke test — bot halts if violated)

| Parameter | Value | Cannot be overridden by |
|-----------|-------|------------------------|
| MAX_MARKET_PRICE | $0.55 | Env var or Secrets Manager |
| MAX_BET_SIZE | $1.50 | `HARDCODED_MAX_BET` constant in config.py |
| MIN_EV_THRESHOLD | 0.08 | — |
| MIN_LGBM_PROB | 0.60 | `_DEFAULT_GATE` in server.py |

---

## LightGBM models

### 3 models (one per pair): BTC_5m, ETH_5m, SOL_5m
- Training: rolling 5,000 windows, time-ordered 80/20 split, 5-min embargo
- Signal-weighted: 3x weight on windows where |move_pct_15s| > 0.02%
- `is_unbalance=True` for class imbalance handling
- Calibration: Platt scaling → Isotonic regression
- Deploy gate: only if brier_score < baseline (0.25)
- Retrain: every 4h via EventBridge

### 14 Features
move_pct_15s, realized_vol_5m, vol_ratio, body_ratio,
prev_window_direction, prev_window_move_pct,
hour_sin, hour_cos, dow_sin, dow_cos,
signal_move_pct, signal_ask_price, signal_seconds, signal_ev

### Orderbook features (collected for future retrains)
ofi_30s, bid_ask_spread, depth_imbalance, trade_arrival_rate,
liq_cluster_bias (Binance long/short ratio), btc_confirms_direction

---

## Position sizing

$1.00 flat bet until SPRT confirms edge. Max $1.50 hard cap.
EV formula: `ev = prob × (1 - price) - (1 - prob) × price`

---

## Hard rules

1. **MAX_ASK = $0.55** — never buy above $0.55 per share
2. **MAX_BET = $1.50** — never bet more than $1.50 total per trade
3. **$1 flat bet** until SPRT confirmed at boundary 2.77. No exceptions.
4. **Never expand beyond crypto price markets** until edge confirmed
5. **Only 5m windows** — 15m disabled (negative SPRT)
6. **3-layer dedup**: memory set + DynamoDB query + atomic conditional put
7. When in doubt: ask before executing

---

## Edge measurement

### SPRT — update after every trade
```
log_lambda += outcome * log(p1/p0) + (1-outcome) * log((1-p1)/(1-p0))
Boundary A = log((1-β)/α) = 2.7726 → edge confirmed
Boundary B = log(β/(1-α)) = -1.5581 → no edge
```
α=0.05, β=0.20. Current status: all 3 pairs accumulating (SPRT +0.08 to +0.17).

### Per-pair SPRT (clean data, ask ≤ $0.55 only)
| Pair | Trades | WR | SPRT | To confirm |
|------|--------|-----|------|-----------|
| BTC_5m | 6 | 67% | +0.17 | ~91 |
| ETH_5m | 8 | 62% | +0.16 | ~130 |
| SOL_5m | 5 | 60% | +0.08 | ~179 |

---

## Dashboard (Lambda + API Gateway + CloudFront)

URL: https://d2rj5lnnfnptd.cloudfront.net/
Direct: https://r1a61boamb.execute-api.us-east-1.amazonaws.com/
Login: admin / polybot2026 (Basic Auth on HTML page only, API endpoints open)

5 pages:
1. Overview: Portfolio, Cash, P&L, Win/Loss, Strategy cards, Equity curve, Recent Trades, Live Logs
2. Trade Log: Full trade history with P&L, outcome, Polymarket links
3. Window Scores: Score distribution, signal hit rates, per-window breakdown (OFI/NoRev/Cross/PM/Vol)
4. Analytics: Per-pair config, calibration, strategy stats
5. KPIs: SPRT, Brier score, win rates, model separation

---

## Constraints — read before every change

1. 384 tests must pass — run before and after every change
2. Never increase bet above $1.50 without Wouter's explicit instruction
3. Never deploy a model with higher Brier score than the current one
4. All new AWS resources follow existing region split (bot=eu-west-1, data=us-east-1)
5. DynamoDB table names must match exactly what code expects
6. Secrets Manager: `polymarket-bot-env` in eu-west-1 is the source for ECS env vars
7. Smoke test must pass on every startup — if thresholds violated, bot halts
8. When in doubt: ask before executing

---

## Polymarket API reference

### Endpoints
- CLOB REST: https://clob.polymarket.com
- CLOB WS market: wss://ws-subscriptions-clob.polymarket.com/ws/market
- RTDS: wss://ws-live-data.polymarket.com
- Gamma API: https://gamma-api.polymarket.com (resolution / Chainlink oracle)
- Data API: https://data-api.polymarket.com (balance, activity, positions)

### Order types
- FOK: aggressive take — taker entries (score 4-5 + override)
- GTC: passive limit — maker entries (score 2-3), cancel after 8s

### Key constraints
- Minimum order: 5 shares
- Tick size: 0.01 (hardcoded, short-lived markets return 404 from get_tick_size)
- Signing: EIP-712, signature_type=2 (Gnosis Safe proxy wallet)

---

## Libraries

- lightgbm: batch model (3 per-pair classifiers)
- py_clob_client: Polymarket orders (FOK + GTC)
- boto3: AWS (DynamoDB, S3, SSM, Secrets Manager, CloudWatch)
- websockets: Coinbase + Polymarket feeds
- httpx: Gamma API, Binance long/short ratio, Polymarket data API
- structlog: JSON structured logging → CloudWatch
- fastapi + mangum: dashboard (Lambda)
- pandas / numpy: feature computation

---

## Secrets (AWS Secrets Manager, eu-west-1)

Secret name: `polymarket-bot-env`
Keys: POLYMARKET_PRIVATE_KEY, POLYMARKET_API_KEY, POLYMARKET_API_SECRET,
POLYMARKET_API_PASSPHRASE, POLYMARKET_FUNDER, POLYMARKET_CHAIN_ID,
MODE, BANKROLL, MAX_POSITION_PCT, DAILY_LOSS_CAP_PCT, KELLY_FRACTION,
MIN_EV_THRESHOLD, MAX_MARKET_PRICE, MAX_TRADE_USD, MIN_TRADE_USD,
ASSETS, PAIRS, DATA_COLLECTION_MODE, WINDOW_DURATIONS, LOG_LEVEL
