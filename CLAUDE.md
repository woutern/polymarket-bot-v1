# Scaleflow Polymarket Trading Bot — Claude Code Context

> Read this file fully at the start of every session. This is the single source of truth.

---

## What this project is

An algorithmic trading bot for Polymarket crypto binary prediction markets.
Two strategies: 5-minute crypto bot (BTC/SOL) + opportunity bot (13 category workers).
Binary bets: does price close higher or lower than the open at window start?

Owner: Wouter (Scaleflow)
Stack: Python, AWS (eu-west-1), Polygon blockchain
Live wallet: ~$920 on Polymarket
Tests: 538 passing

---

## Strategy 1: 5-Minute Crypto Bot (Scenario C)

### How it trades
1. Coinbase WebSocket feeds live prices every 250ms
2. Window opens → bot tracks price from open
3. At T+210s: scan window starts, checks orderbook every 3s
4. LightGBM predicts probability (trained on 22K Jon-Becker windows)
5. At T+240s: execute if all gates pass (lgbm, ask, volatility)
6. Hard deadline: T+255s
7. FOK taker order on Polymarket CLOB
8. After close → verify outcome via Polymarket Gamma API (Chainlink oracle)

### Scenario C Entry Rules (lgbm gates first)

| Order | Check | Skip if |
|-------|-------|---------|
| 1 | LightGBM gate | lgbm_prob < 0.62 |
| 2 | Ask floor | ask < $0.60 |
| 3 | Ask ceiling | ask > $0.95 |
| 4 | Circuit breaker | 3 consecutive losses |
| 5 | Volatility filter | vol > 2× rolling avg |
| 6 | Time-of-day min ask | ask < $0.65 weekday / $0.70 weekend |
| 7 | Per-asset max ask | ask > $0.78 BTC / $0.82 SOL (unless high lgbm) |

### Sizing

| Ask Range | Condition | Size |
|-----------|-----------|------|
| $0.60–$0.75 | default | $5 |
| $0.75–$0.82 | peak hours only | $10 |
| $0.75–$0.82 | weak hours / weekend | $5 |
| $0.82–$0.88 | lgbm >= 0.70 | $5 |
| $0.88–$0.95 | lgbm >= 0.80 | $5 |

Peak hours: 09:00–21:00 UTC weekdays, excluding 12:00–13:00.
Hard cap: $10 (HARDCODED_MAX_BET in config.py).

### Early Entry
- Peak: ask <= $0.58 → enter immediately
- Weak hours: ask <= $0.68
- Weekend: ask <= $0.72

---

## Strategy 1b: Early Entry (T+14-18s, disabled by default)

Independent strategy that fires at T+14-18s if the LightGBM model is confident and the ask is cheap.
Enabled via `EARLY_ENTRY_ENABLED=true` in Secrets Manager. Default: disabled.

| Setting | Default | Description |
|---------|---------|-------------|
| early_entry_enabled | False | Master switch |
| early_entry_max_bet | $2.00 | Hard cap per trade |
| early_entry_lgbm_threshold | 0.62 | Min lgbm_prob |
| early_entry_max_ask | $0.55 | Max ask price |
| early_entry_min_ask | $0.40 | Min ask price |
| early_entry_use_limit | True | GTC limit with FOK fallback |
| early_entry_limit_offset | 0.02 | GTC price = best_bid + offset |
| early_entry_limit_wait_seconds | 8.0 | Cancel GTC after this |

- Own dedup: `early_{slug}` prefix (no collision with Scenario C)
- Own DynamoDB records: `source = "early_entry"`
- Both strategies can trade the same window

---

## Strategy 2: Opportunity Bot

### Overview
- 13 parallel workers scan all Polymarket markets every 30 min
- All workers fetch → combine → dedup by condition_id → sort by resolve time → trade top-to-bottom
- FOK taker orders only (never GTC)
- MAX_BUDGET: $1,250 total deployed

### 13 Workers
crypto, finance, fed, geopolitics, elections, tech, weather, culture, economics, companies, health, iran, whitehouse

Removed: politics (-$7.91 all-time), basketball, tweets, sports

### Tiers

| Tier | Ask | Hours | Volume | Size | AI Gate |
|------|-----|-------|--------|------|---------|
| 0 | >= $0.93 | <= 6h | >= $5K | $10 | Haiku (conf >= 0.90) + Sonnet devil's advocate (conf >= 0.85) |
| 1 | $0.85–$0.94 | <= 24h | any | $5 | Haiku sanity (conf >= 0.80) |
| 2 | $0.85–$0.94 | 24-48h | any | $2.50 | Full Haiku (conf >= 0.85, edge >= 0.15) |

### Data-Driven Filters (from opportunity_analysis.txt)
- Min ask: $0.85 (below loses money: 71% WR, -$3.42)
- Max ask: $0.94 (above $0.95 margin too thin: -$3.60)
- Morning 06-12 UTC: blocked (76% WR, -$12.12)
- 6-12h resolution window: blocked (80% WR, -$8.90)

### AI Models
- Haiku: `eu.anthropic.claude-haiku-4-5-20251001-v1:0`
- Sonnet: `eu.anthropic.claude-sonnet-4-20250514-v1:0`
- Both via AWS Bedrock eu-west-1

### Skip Filters
- SKIP_SLUGS: {5m, 15m, updown}
- SKIP_KEYWORDS: {esports, lol, league-of-legends, dota, cs2, valorant, gaming}

---

## AWS Infrastructure (all eu-west-1)

| Component | Service | Details |
|-----------|---------|---------|
| Bot | ECS Fargate | Single task, 4 processes (bot, opp bot, dashboard, auto-claim) |
| Dashboard | Lambda + CloudFront | https://d2rj5lnnfnptd.cloudfront.net/ |
| Storage | DynamoDB | trades, windows, signals, training_data, opportunity-trades |
| Models | S3 | `polymarket-bot-data-688567279867-euw1/models/` |
| Model paths | SSM Parameter Store | `/polymarket/models/{pair}/latest_path` |
| Secrets | Secrets Manager | `polymarket-bot-env` |
| AI | Bedrock | Haiku + Sonnet 4 |
| Auto-retrain | EventBridge | Every 4h |
| Auto-claim | Builder Relayer API | Every 30 min via Node.js script |

---

## LightGBM Models

### 2 active models: BTC_5m, SOL_5m (ETH disabled)
- Trained on 22,888 enriched windows from Jon-Becker dataset
- Jon-Becker base (S3: `polymarket-bot-training-data-688567279867`) + live DynamoDB windows
- BTC AUC: 0.7294, SOL AUC: 0.7660
- Time-ordered 80/20 split, 5-min embargo
- Signal-weighted: 3x on windows where |move_pct_15s| > 0.02%
- Calibration: Platt scaling → Isotonic regression (if available)
- Raw LightGBM output also supported (Jon-Becker models)

### 14 Features
move_pct_15s, realized_vol_5m, vol_ratio, body_ratio,
prev_window_direction, prev_window_move_pct,
hour_sin, hour_cos, dow_sin, dow_cos,
signal_move_pct, signal_ask_price, signal_seconds, signal_ev

### Auto-Retrain (every 4h)
- Loads Jon-Becker 22K base + live DynamoDB windows
- Deduplicates by slug, sorts by timestamp
- Quality gate: new AUC >= current AUC - 0.02 (prevents regression)
- Brier gate: must beat baseline (0.25)
- Calibration gate: mean_prob must be 0.25-0.75

---

## Smoke Test (bot halts on failure)

| Check | Type | What it verifies |
|-------|------|-----------------|
| clob_connectivity | Critical | Polymarket CLOB reachable (not geoblocked) |
| model_load_{pair} | Critical | Models load from S3 via SSM paths |
| model_predict_{pair} | Critical | Predictions are not 0.5 fallback |
| polymarket_creds | Critical | Private key + API key present |
| pairs_not_set | Critical | PAIRS explicitly set in live mode |
| max_trade_usd | Critical | Not above $10 hard cap |
| rogue_task_check | Critical | No duplicate ECS tasks running |

---

## Dashboard

URL: https://d2rj5lnnfnptd.cloudfront.net/
5 pages: Overview, Trades, Analytics, Opportunities, Rules

---

## Constraints — read before every change

1. **538 tests must pass** — run before and after every change
2. Never increase bet above $10 without Wouter's explicit instruction
3. Never deploy a model with higher Brier score than the current one
4. **All AWS resources in eu-west-1** (bot, data, models, dashboard)
5. DynamoDB table names must match exactly what code expects
6. Secrets Manager: `polymarket-bot-env` in eu-west-1 is the source for ECS env vars
7. Smoke test must pass on every startup — if model loading fails, bot halts
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
- FOK only (taker). No GTC/limit orders ever.

### Key constraints
- Minimum order: 5 shares
- Tick size: 0.01
- Signing: EIP-712, signature_type=2 (Gnosis Safe proxy wallet)

---

## Libraries

- lightgbm: per-pair classifiers (BTC/SOL)
- py_clob_client: Polymarket orders (FOK)
- boto3: AWS (DynamoDB, S3, SSM, Secrets Manager, Bedrock, CloudWatch)
- websockets: Coinbase + Polymarket feeds
- httpx: Gamma API, Coinbase REST, Polymarket data API
- structlog: JSON structured logging → CloudWatch
- fastapi + mangum: dashboard (Lambda)
- pandas / numpy / pyarrow: feature computation + training data

---

## Secrets (AWS Secrets Manager, eu-west-1)

Secret name: `polymarket-bot-env`
Keys: POLYMARKET_PRIVATE_KEY, POLYMARKET_API_KEY, POLYMARKET_API_SECRET,
POLYMARKET_API_PASSPHRASE, POLYMARKET_FUNDER, POLYMARKET_CHAIN_ID,
MODE, BANKROLL, MAX_POSITION_PCT, DAILY_LOSS_CAP_PCT, KELLY_FRACTION,
MIN_EV_THRESHOLD, MAX_MARKET_PRICE, MAX_TRADE_USD, MIN_TRADE_USD,
ASSETS, PAIRS, DATA_COLLECTION_MODE, WINDOW_DURATIONS, LOG_LEVEL
