# Polymarket Trading Bot

Algorithmic trading system for Polymarket prediction markets.

**Status:** LIVE on AWS ECS (eu-west-1)
**Dashboard:** https://d2rj5lnnfnptd.cloudfront.net/
**Tests:** 538 passing
**V2 both-sides:** paused (enable via `EARLY_ENTRY_ENABLED=true`)

## Three Trading Strategies

### 1. V2 Both-Sides (experimental, currently paused)
- Opens YES + NO on every 5-min BTC/ETH/SOL window at T+0
- $6 main + $3 hedge GTC at bid+1¢ (immediate fill)
- Accumulation: GTC limit ladder on BOTH sides every 3s for 270s
  - Cheap side (bid ≤ 0.35): 5 levels at bid, -3¢, -5¢, -8¢, -10¢
  - Expensive side (bid > 0.35): 3 levels at bid, -5¢, -10¢
  - Orders sit in book unfilled until market moves to them (free until filled)
- Fill budget: `EARLY_ENTRY_MAX_BET` ($20/window) caps actual fills, not orders posted
- Stop-loss: only on main entries > 40¢; hedge < 40¢ always held to resolution
- Enabled via `EARLY_ENTRY_ENABLED=true` in Secrets Manager

### 2. 5-Minute Crypto Bot (Scenario C, paused)
- Trades BTC/SOL 5-minute Up/Down windows
- Scan window T+210s–T+240s: finds best entry price
- LightGBM entry filter: lgbm_prob >= 0.62 required (trained on 22K Jon-Becker windows)
- Scenario C: lgbm gates first, ask ceiling relaxed for high conviction
- Sizing: $5 default, $10 peak at ask >= $0.75, $5 at $0.82-$0.95 with lgbm >= 0.70/0.80
- Resolution via Polymarket Chainlink oracle (not Coinbase)

### 3. Opportunity Bot (paused)
- 13 parallel workers scan all Polymarket markets every 30 min
- Categories: crypto, finance, fed, geopolitics, elections, tech, weather, culture, economics, companies, health, iran, whitehouse
- Tier 0 ($0.93+, ≤6h, vol≥$5K): $10 FOK — dual AI: Haiku sanity + Sonnet devil's advocate
- Tier 1 ($0.85–$0.94, ≤24h): $5 FOK — Haiku sanity check (conf >= 0.80)
- Tier 2 ($0.85–$0.94, 24-48h): $2.50 FOK — full Haiku AI assessment (conf >= 0.85)
- Data-driven filters: skip morning 06-12 UTC, skip 6-12h resolve window
- Pause flag: `OPPORTUNITY_BOT_PAUSED=true` skips order execution, keeps scanning
- $1,250 max total deployed, FOK taker orders only, sorted by resolve time

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the complete system diagram.

| Component | Location |
|-----------|----------|
| Bot | ECS Fargate, eu-west-1 |
| Dashboard | Lambda + CloudFront, eu-west-1 |
| Storage | DynamoDB, eu-west-1 |
| Models | S3 eu-west-1 (LightGBM BTC/SOL, trained on 22K windows) |
| AI | Bedrock (Haiku + Sonnet 4), eu-west-1 |
| Secrets | AWS Secrets Manager, eu-west-1 |

## Safety Guards

| Guard | Value |
|-------|-------|
| V2 fill budget | $20/window (`EARLY_ENTRY_MAX_BET`) |
| V2 open position | $6 main + $3 hedge at window open |
| V2 stop-loss | Only main entries > 40¢; cheap hedge always held |
| Max bet (5min bot) | $10 peak / $5 weak+weekend |
| LightGBM gate (5min) | lgbm_prob >= 0.62 (Scenario C) |
| Max ask (5min bot) | $0.95 ceiling, $0.78/$0.82 default, relaxed with high lgbm |
| Model smoke test | Bot **halts** if models can't load or predict non-0.5 |
| Max ask (opp bot) | $0.94 (data-driven: above loses money) |
| Min ask (opp bot) | $0.85 (data-driven: below 71% WR, -$3.42) |
| Haiku gate (opp Tier 1) | confidence >= 0.80 |
| Haiku gate (opp Tier 2) | confidence >= 0.85 + edge >= 0.15 |
| Sonnet gate (opp Tier 0) | confidence >= 0.85 (devil's advocate) |
| Morning block (opp) | 06-12 UTC skipped (76% WR, -$12.12) |
| Resolve window block | 6-12h resolution skipped (80% WR, -$8.90) |
| Max deployed (opp bot) | $1,250 |
| Dedup | 3-layer (memory + DynamoDB + atomic claim) |
| Rogue task detection | Smoke test on startup |
| Resolution | Polymarket Chainlink only (no Coinbase) |
| Auto-retrain quality gate | New AUC must be >= current AUC - 0.02 |

## Running

```bash
# Local development
uv run pytest tests/          # 538 tests
uv run python scripts/run.py  # Start 5min bot

# Deploy to AWS
bash scripts/deploy_aws.sh              # Bot (ECS)
bash scripts/deploy_dashboard_lambda.sh  # Dashboard (Lambda)

# Opportunity scanner
PYTHONPATH=src uv run python scripts/opportunity_bot.py
```

## Tech Stack

- Python 3.12, asyncio, uv
- py-clob-client (Polymarket CLOB SDK)
- Coinbase WebSocket (250ms price ticks)
- LightGBM (per-pair classifiers, trained on 22K Jon-Becker windows, AUC 0.73/0.77)
- AWS: ECS, DynamoDB, Bedrock (Haiku + Sonnet 4), Lambda, CloudFront, Secrets Manager
- structlog (JSON logging → CloudWatch)
- Auto-retrain every 4h (Jon-Becker base + live windows, AUC quality gate)
