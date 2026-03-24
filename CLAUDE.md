# Scaleflow Polymarket Trading Bot — Claude Code Context

> Read this file fully at the start of every session. This is the single source of truth.

---

## What this project is

An algorithmic trading bot for Polymarket crypto binary prediction markets.
Three strategies: V2 both-sides (live), 5-minute Scenario C (paused), opportunity bot (paused).
Binary bets: does price close higher or lower than the open at window start?

Owner: Wouter (Scaleflow)
Stack: Python 3.12, asyncio, uv, AWS (eu-west-1), Polygon blockchain
Tests: 888 passing

---

## Strategy 1: V2 Both-Sides (live, profile-driven 5m engine)

### Overview
K9-style market-making strategy. Buys BOTH sides of each 5-minute window, accumulates throughout,
holds to resolution. If combined average cost < $1.00, the position is guaranteed profitable.

Current live focus: `BTC_5m`. Other pairs enabled only after pair-specific tuning.
Gated by `EARLY_ENTRY_ENABLED=true` and pair-level `PAIRS`.
Budget: **$50 per asset per window** (executable USD notional).

### How it trades — 4 phases

**Phase 1 — Open (T+5s to T+15s):**
- Small two-sided open using 10% of per-window budget
- LightGBM model drives split (80/20 to 50/50 based on confidence)
- Executable size based on whole shares with 5-share minimum
- GTC limit orders (NOT FOK)

**Phase 2 — Main deploy (T+15s to T+180s):**
- Accumulate both sides, recycle selectively
- Smooth budget curve ramps from 10% to 82% of budget by T+180
- Confidence scaling: less spend in weak windows, fuller deploy in strong ones
- Limited sell-and-recycle from T+45 when inventory above payout floor can be sold at favorable bid

**Phase 3 — Buy-only (T+180s to T+250s):**
- Frozen allocation split
- No more sells
- Only passive adds if they pass pair-quality guards

**Phase 4 — Commit/Hold (T+250s to resolution):**
- Cancel unfilled GTC orders
- Hold remaining inventory to market resolution
- One side pays $1/share, other pays $0
- If combined avg < $1 → profit

### Active order recycling / repricing
- Open orders inspected every tick (1s)
- Stale orders cancelled after 6s if no longer within 1c of desired prices
- Cancelled orders release reserved budget immediately

### Pair-quality guards
- Rich-side buys capped later in the window
- Incomplete pairs don't keep averaging the filled side while missing side drifts expensive
- New adds blocked when projected combined_avg / payout-floor pressure would worsen state

### Budget / accounting
- All risk based on executable USD notional, never target size alone
- `actual_notional_usd = actual_shares * actual_price`
- `reserved_open_order_usd + filled_position_cost_usd + new_actual_notional_usd <= $50` per asset per window
- Reserve released on fill, cancel, reject, timeout, and commit

### Paper mode
- Paper mode exposes an in-memory order client for V2 to exercise post/fill/cancel/release accounting without real-money execution

### Sell mechanics (from K9 analysis)
- Only sell the side that is LOSING value (dropping in price)
- Entry typically 30-50c, now dropped to 15-25c
- Sells to FREE CAPITAL, not to lock profit
- Rebuy within seconds: same side cheaper, or opposite side
- Never sell shares bought under 15c (lottery tickets)
- Never sell after T+240s
- Never sell the winning side (unless swapping direction)
- Stop-loss: only entries > 40c down > 25%, T+30-240s only

---

## Strategy 2: 5-Minute Crypto Bot — Scenario C (paused)

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
| 5 | Volatility filter | vol > 2x rolling avg |
| 6 | Time-of-day min ask | ask < $0.65 weekday / $0.70 weekend |
| 7 | Per-asset max ask | ask > $0.78 BTC / $0.82 SOL (unless high lgbm) |

### Sizing

| Ask Range | Condition | Size |
|-----------|-----------|------|
| $0.60-$0.75 | default | $5 |
| $0.75-$0.82 | peak hours only | $10 |
| $0.75-$0.82 | weak hours / weekend | $5 |
| $0.82-$0.88 | lgbm >= 0.70 | $5 |
| $0.88-$0.95 | lgbm >= 0.80 | $5 |

Peak hours: 09:00-21:00 UTC weekdays, excluding 12:00-13:00.
Hard cap: $10 (HARDCODED_MAX_BET in config.py).

---

## Strategy 3: Opportunity Bot (paused)

### Overview
- 13 parallel workers scan all Polymarket markets every 30 min
- All workers fetch → combine → dedup by condition_id → sort by resolve time → trade top-to-bottom
- FOK taker orders only (never GTC)
- MAX_BUDGET: $1,250 total deployed
- Pause flag: `OPPORTUNITY_BOT_PAUSED=true` skips order execution, keeps scanning

### 13 Workers
crypto, finance, fed, geopolitics, elections, tech, weather, culture, economics, companies, health, iran, whitehouse

### Tiers

| Tier | Ask | Hours | Volume | Size | AI Gate |
|------|-----|-------|--------|------|---------|
| 0 | >= $0.93 | <= 6h | >= $5K | $10 | Haiku (conf >= 0.90) + Sonnet devil's advocate (conf >= 0.85) |
| 1 | $0.85-$0.94 | <= 24h | any | $5 | Haiku sanity (conf >= 0.80) |
| 2 | $0.85-$0.94 | 24-48h | any | $2.50 | Full Haiku (conf >= 0.85, edge >= 0.15) |

### Data-Driven Filters
- Min ask: $0.85 (below loses money: 71% WR, -$3.42)
- Max ask: $0.94 (above $0.95 margin too thin: -$3.60)
- Morning 06-12 UTC: blocked (76% WR, -$12.12)
- 6-12h resolution window: blocked (80% WR, -$8.90)

### AI Models
- Haiku: `eu.anthropic.claude-haiku-4-5-20251001-v1:0`
- Sonnet: `eu.anthropic.claude-sonnet-4-20250514-v1:0`
- Both via AWS Bedrock eu-west-1

---

## AWS Infrastructure (all eu-west-1)

| Component | Service | Details |
|-----------|---------|---------|
| Bot | ECS Fargate | Single task, multiple processes |
| Dashboard | Lambda + CloudFront | https://d2rj5lnnfnptd.cloudfront.net/ |
| Storage | DynamoDB | trades, windows, signals, training_data, opportunity-trades, polymarket-bot-controls |
| Models | S3 | `polymarket-bot-data-688567279867-euw1/models/` |
| Model paths | SSM Parameter Store | `/polymarket/models/{pair}/latest_path` |
| Secrets | Secrets Manager | `polymarket-bot-env` |
| AI | Bedrock | Haiku + Sonnet 4 |
| Auto-retrain | EventBridge | Every 4h |
| Auto-claim | Builder Relayer API | Every 30 min via Node.js script |

Deployments use `scripts/deploy_aws.sh`, which registers a fresh ECS task definition revision before forcing the service deployment.

### Kill switch / pause (DynamoDB-backed, takes effect within 10s)

```bash
# Pause new windows (finish current window, then stop):
aws dynamodb put-item --table-name polymarket-bot-controls --region eu-west-1 \
  --item '{"bot":{"S":"bot"},"pause_new_windows":{"BOOL":true},"note":{"S":"manual pause"}}'

# Hard kill (stops immediately after current tick):
aws dynamodb put-item --table-name polymarket-bot-controls --region eu-west-1 \
  --item '{"bot":{"S":"bot"},"kill_switch":{"BOOL":true},"note":{"S":"emergency stop"}}'

# Resume (clear all flags):
aws dynamodb delete-item --table-name polymarket-bot-controls --region eu-west-1 \
  --key '{"bot":{"S":"bot"}}'

# Emergency: scale ECS to 0 (instant, no graceful cancel):
aws ecs update-service --cluster polymarket-bot --service polymarket-bot-service \
  --desired-count 0 --region eu-west-1
```

### Shell into running container (ECS Exec)

```bash
TASK=$(aws ecs list-tasks --cluster polymarket-bot --service-name polymarket-bot-service \
  --region eu-west-1 --query 'taskArns[0]' --output text)
aws ecs execute-command --cluster polymarket-bot --task $TASK \
  --container polymarket-bot --interactive --command /bin/sh --region eu-west-1
```

---

## LightGBM Models

### Active models: BTC_5m, SOL_5m (retrain pipeline also supports ETH_5m, XRP_5m)
- Trained on 22,888 enriched windows from Jon-Becker dataset
- Jon-Becker base (S3: `polymarket-bot-training-data-688567279867`) + live DynamoDB windows
- BTC AUC: 0.7294, SOL AUC: 0.7660
- Time-ordered 80/20 split, 5-min embargo
- Signal-weighted: 3x on windows where |move_pct_15s| > 0.02%
- Calibration: Platt scaling → Isotonic regression (if available)

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

## V2 Safety Guards

| Guard | Value |
|-------|-------|
| V2 per-asset notional cap | $50/window based on executable USD notional |
| V2 accounting basis | `actual_notional_usd = actual_shares * actual_price` |
| V2 open position | 70/30 to 50/50 main/hedge split (LGBM-driven) |
| V2 stale repricing | every 1s tick, stale after 6s, 1c price tolerance |
| V2 recycle start | T+45s, payout-floor driven |
| V2 buy-only phase | T+180s-T+250s |
| V2 commit | T+250s |
| V2 stop-loss | Only entries > 40c down > 25%, T+30-240s only |
| V2 winning side gate | bid > 0.60 accumulation blocked before T+60s |
| Max bet (Scenario C) | $10 peak / $5 weak+weekend |
| LightGBM gate (Scenario C) | lgbm_prob >= 0.62 |
| Max deployed (opp bot) | $1,250 |
| Model smoke test | Bot halts if models can't load or predict non-0.5 |
| Rogue task detection | Smoke test on startup |
| Resolution | Polymarket Chainlink only (not Coinbase) |
| Auto-retrain quality gate | New AUC must be >= current AUC - 0.02 |
| ECS deploy safety | Register new task def revision before update-service |
| Dedup | 3-layer (memory + DynamoDB + atomic claim) |

---

## Smoke Test (bot halts on failure)

| Check | Type | What it verifies |
|-------|------|-----------------|
| clob_connectivity | Critical | Polymarket CLOB reachable (not geoblocked) |
| model_load_{pair} | Critical | Models load from S3 via SSM paths |
| model_predict_{pair} | Critical | Predictions are not 0.5 fallback |
| polymarket_creds | Critical | Private key + API key present |
| pairs_not_set | Critical | PAIRS explicitly set in live mode |
| rogue_task_check | Critical | No duplicate ECS tasks running |

---

## Dashboard

URL: https://d2rj5lnnfnptd.cloudfront.net/
Pages: Overview, Trades, Analytics, Opportunities, Rules

---

## Roadmap

### Immediate
- Refine `BTC_5m` as the control profile until live windows are consistently sane
- Tighten payout-floor-based recycle rules without reintroducing churn
- Keep weak/sideways windows small, deploy harder only in strong mid-window setups
- Fix runtime Secrets Manager refresh permissions

### Next 5m rollout
- Per-pair strategy profiles (not one-size-fits-all)
- Enable `ETH_5m` after `BTC_5m` is stable
- Tune `SOL_5m` separately (smaller open, stricter rich-side caps)
- Wire live `XRP_5m` model loading (currently falls back to neutral)

### Later timeframes
- `1h` profile: small open, gradual two-sided accumulation, no selling, late commit
- `15m` profile after 5m and 1h are stable
- Shared execution engine, but tune budget curve / rich-side caps / recycle rules / timing per pair/timeframe

### Infra
- Reduce Docker image size and speed up ECS rollout
- Prevent overlapping service deployments during live rollouts
- Expand V2 structured logging for post-trade fill quality analysis

---

## Constraints — read before every change

1. **635 tests must pass** — run before and after every change
2. V2 per-asset budget is $50/window — do not exceed without Wouter's instruction
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
- V2: GTC limit orders with repricing (primary)
- Scenario C / Opportunity: FOK taker only

### Key constraints
- Minimum order: 5 shares
- Tick size: 0.01
- Signing: EIP-712, signature_type=2 (Gnosis Safe proxy wallet)

---

## Libraries

- lightgbm: per-pair classifiers (BTC/SOL/ETH/XRP)
- py_clob_client: Polymarket orders (GTC + FOK)
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
ASSETS, PAIRS, DATA_COLLECTION_MODE, WINDOW_DURATIONS, LOG_LEVEL,
EARLY_ENTRY_ENABLED, EARLY_ENTRY_MAX_BET, OPPORTUNITY_BOT_PAUSED

---

## Running

```bash
# Local development
uv run pytest tests/          # 888 tests
uv run python scripts/run.py  # Start old Scenario C bot

# V2 MM strategy (new architecture)
uv run python scripts/run_mm.py              # Paper mode (default, $50 budget)
uv run python scripts/run_mm.py --budget 80  # Paper mode, $80 budget
uv run python scripts/run_mm.py --live       # LIVE mode — real money, prompts for confirmation

# Deploy to AWS
bash scripts/deploy_aws.sh              # Bot (ECS)
bash scripts/deploy_dashboard_lambda.sh  # Dashboard (Lambda)

# Opportunity scanner
PYTHONPATH=src uv run python scripts/opportunity_bot.py
```
