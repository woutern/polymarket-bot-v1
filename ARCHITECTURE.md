# Polymarket Trading System — Architecture

## Overview

3 processes in 1 ECS Fargate container (eu-west-1):
1. **5-Minute Crypto Bot** — BTC/SOL directional trading on 5min windows
2. **Opportunity Bot** — 7 parallel workers scan all Polymarket markets
3. **Dashboard** — FastAPI + Lambda behind CloudFront

---

## Process 1: 5-Minute Crypto Bot (scripts/run.py)

**Strategy:** Late-entry scan window on BTC/SOL 5-minute Up/Down markets

### Data Feeds (always-on WebSocket)
- **Coinbase WS** — ticker + L2 orderbook for BTC-USD, SOL-USD (250ms ticks)
- **Polymarket CLOB WS** — live orderbook (yes_ask, no_ask)
- **Polymarket RTDS WS** — Chainlink oracle lag signal

### 250ms Tick Loop
```
T+0s      Window opens, track price from Coinbase
T+210s    SCAN STARTS — refresh orderbook, record direction + ask
T+210-240s  SCAN PHASE (every 3s):
            - Refresh orderbook
            - If direction flips → SKIP "direction_unstable"
            - Track best (lowest) ask
            - If ask ≤ $0.58 → ENTER EARLY
T+240s    EXECUTE at best ask found during scan
```

### Guards
- ask < $0.55 → skip "no_conviction"
- ask > $0.82 (SOL) or $0.78 (BTC) → skip "fully_priced"
- Circuit breaker (3 consecutive losses → 15min pause)
- PAIRS must be set in live mode (smoke test)

### Sizing (flat, by ask price)
- ask $0.55–$0.65 → $5.00
- ask $0.65–$0.75 → $7.50
- ask $0.75–$0.82 → $10.00
- No balance scaling
- Hard cap: $10 (HARDCODED_MAX_BET)

### Execution
- FOK taker order on Polymarket CLOB
- py-clob-client SDK, signature_type=2 (Gnosis Safe)
- 3-layer dedup: memory set + DynamoDB query + atomic conditional put

### Resolution
1. Coinbase price → provisional (blue "WIN?" / "LOSS?" in dashboard)
2. 90s later → Polymarket Gamma API verification (Chainlink oracle)
3. 5 retries at 60s intervals → final (green WIN / red LOSS)
4. Detects manual sells via Polymarket activity API
5. Orphan resolver on startup catches missed windows

### Startup Smoke Tests (halt if critical failure)
- CLOB connectivity (not geoblocked)
- Polymarket credentials valid
- max_market_price ≤ 0.90
- max_trade_usd ≤ $10
- PAIRS explicitly set in live mode
- No rogue ECS tasks on different task-defs
- Mode is "paper" or "live"

---

## Process 2: Opportunity Bot (scripts/opportunity_bot.py)

**Strategy:** 7 parallel workers scan all Polymarket, tiered trading with AI assessment. Runs every 30 minutes.

### Step 0: Startup
- Fetch tag IDs from GET /tags (5,577 tags → map to workers)
- Load existing condition_ids from DynamoDB (dedup set)
- Calculate total deployed from unresolved trades
- Resolve any pending trades past their end_date

### Step 1: Parallel Fetch (asyncio.gather)

7 workers fetch simultaneously:

| Worker | Tags | Live Context |
|--------|------|-------------|
| crypto | crypto, bitcoin, ethereum, solana, xrp, crypto-prices | Coinbase REST prices |
| finance | finance, economics, stocks, earnings, fed, fed-rates | Yahoo Finance prices |
| politics | politics, us-politics, trump, elections, world-elections | Model knowledge |
| geopolitics | geopolitics, iran, world, middle-east, war, ukraine, russia | Model knowledge |
| tech | tech, ai, technology, openai, big-tech | Model knowledge |
| basketball | nba, ncaa, march-madness, college-basketball | ESPN live scores (IN-PROGRESS only) |
| news | world, temperature, weather, daily, pop-culture, tweets-markets | Model knowledge |

Each worker: `GET /events?active=true&tag_id={id}&end_date_max=+48h&end_date_min=+30min&order=volume&limit=50`

### Step 2: Combine + Dedup + Sort
- Merge all markets from all 7 workers
- Deduplicate by condition_id
- Remove already-traded condition_ids
- Sort by end_date ascending (soonest resolving first)
- Skip: slug contains 5m/15m/updown, vol < $1K, ask < $0.65

### Step 3: Tier Classification

**Tier 1 — AUTO TRADE (no AI):**
- Ask $0.85–$0.95, resolves within 24h, volume > $1K
- Size: $5.00 FOK at best ask

**Tier 2 — AI CHECK (Bedrock Haiku):**
- Ask $0.65–$0.85 AND resolves within 24h
- OR ask $0.85–$0.95 AND resolves 24-48h
- AI returns: true_probability, edge, confidence, trade flag
- Trade if: confidence ≥ 0.80 AND edge ≥ 0.15
- Size: $2.50 FOK at best ask

### Step 4: Execute (sorted list, soonest first)
- Check $1,000 budget cap before each trade
- Dedup: atomic conditional put on condition_id
- Fetch CLOB orderbook → actual best ask (reject if > $0.95)
- FOK taker order (NEVER limit/GTC)
- Schedule resolution at end_date + 90s (5 retries at 60s)

---

## Process 3: Dashboard (scripts/dashboard.py)

**URL:** https://d2rj5lnnfnptd.cloudfront.net/
**Stack:** FastAPI + Lambda + API Gateway + CloudFront

### 4 Tabs
- **Overview** — Portfolio, P&L, win rate (BTC/SOL), recent trades, equity curve
- **Trades** — Full trade log with Polymarket links, sortable
- **Analytics** — P&L by asset, by ask bucket, by hour
- **Opportunities** — KPIs, active trades (sortable, "To Win" column), resolved trades

### Resolution Display
- Blue "WIN?" / "LOSS?" — Coinbase provisional (waiting for confirmation)
- Green "WIN" / Red "LOSS" — Polymarket Chainlink verified (final)

---

## Infrastructure

### AWS (split regions)

**eu-west-1 (Bot)** — Polymarket CLOB geoblocks us-east-1 AWS IPs
- ECS Fargate (task def rev 16, single task)
- Secrets Manager: `polymarket-bot-env`
- CloudWatch Logs: `/polymarket-bot`

**us-east-1 (Data + AI)**
- DynamoDB: trades, windows, signals, training-data, kpi-snapshots, opportunity-trades
- Bedrock: Claude Haiku (AI assessment)
- S3: LightGBM model artifacts
- SSM Parameter Store: model metadata
- Lambda + API Gateway + CloudFront: Dashboard

### External APIs (no auth needed)

| Service | Endpoint | Used For |
|---------|----------|----------|
| Polymarket CLOB REST | clob.polymarket.com | Orders, orderbook, balance |
| Polymarket CLOB WS | ws-subscriptions-clob.polymarket.com | Live orderbook |
| Polymarket Gamma | gamma-api.polymarket.com | Markets, resolution |
| Polymarket Data | data-api.polymarket.com | Portfolio, activity |
| Polymarket RTDS | ws-live-data.polymarket.com | Oracle prices |
| Coinbase WS | advanced-trade-ws.coinbase.com | 250ms price ticks |
| Coinbase REST | api.coinbase.com | Spot prices for opp bot |
| Yahoo Finance | query1.finance.yahoo.com | Stock prices |
| Binance | fapi.binance.com | Long/short ratio, funding |
| ESPN | site.api.espn.com | Live basketball scores |
| Polygon RPC | rpc-mainnet.matic.quiknode.pro | On-chain USDC balance |

---

## Safety Guards

### 5-Minute Bot
- HARDCODED_MAX_BET = $10 (config.py constant)
- Hard cap in live_trader.py (if size > max → cap)
- 3-layer dedup (memory + DynamoDB + atomic claim)
- Smoke test: rogue task detection (different task-defs)
- Smoke test: PAIRS must be set in live mode
- Circuit breaker: 3 consecutive losses → 15min pause
- Balance-proportional sizing (scale down if losing)
- Scan window: direction flip → abort

### Opportunity Bot
- $1,000 max total deployed (checked before every trade)
- $0.95 max ask price on orderbook
- Atomic conditional put dedup (condition_id)
- FOK only (never limit/GTC)
- Tier 2: AI must return conf ≥ 0.80 AND edge ≥ 0.15
- Basketball: only trade in-progress games (ESPN check)
- No other sports (no soccer/NFL/NHL/golf/esports)

### Tests
- 479 total (regressions, smoke, unit, integration)
