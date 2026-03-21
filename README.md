# Polymarket Trading Bot

Algorithmic trading system for Polymarket prediction markets.

**Status:** LIVE on AWS ECS (eu-west-1)
**Dashboard:** https://d2rj5lnnfnptd.cloudfront.net/
**Tests:** 511 passing

## Two Trading Strategies

### 1. 5-Minute Crypto Bot
- Trades BTC/SOL 5-minute Up/Down windows
- Scan window T+210s–T+240s: finds best entry price
- LightGBM entry filter: lgbm_prob >= 0.62 required (trained on 22K Jon-Becker windows)
- Flat sizing: $5 default, $10 at ask >= $0.75 during peak hours only
- Resolution via Polymarket Chainlink oracle (not Coinbase)

### 2. Opportunity Bot
- 9 parallel workers scan all Polymarket markets every 30 min
- Categories: crypto, finance, fed, politics, geopolitics, elections, tech, weather, culture
- Tier 0 ($0.93+, ≤6h, vol≥$5K): $10 FOK — dual AI: Haiku sanity + Sonnet devil's advocate
- Tier 1 ($0.85–$0.95, ≤24h): $5 FOK — Haiku sanity check
- Tier 2 ($0.65–$0.85, ≤24h): $2.50 FOK — full Haiku AI assessment
- $1,250 max total deployed, FOK taker orders only, sorted by resolve time

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the complete system diagram.

| Component | Location |
|-----------|----------|
| Bot | ECS Fargate, eu-west-1 |
| Dashboard | Lambda + CloudFront, eu-west-1 |
| Storage | DynamoDB, eu-west-1 |
| AI | Bedrock (Haiku + Sonnet 4), eu-west-1 |
| Secrets | AWS Secrets Manager, eu-west-1 |

## Safety Guards

| Guard | Value |
|-------|-------|
| Max bet (5min bot) | $10 peak / $5 weak+weekend |
| LightGBM gate (5min) | lgbm_prob >= 0.62 |
| Max ask (5min bot) | $0.82 SOL / $0.78 BTC |
| Max deployed (opp bot) | $1,250 |
| Max ask (opp bot) | $0.95 |
| Dedup | 3-layer (memory + DynamoDB + atomic claim) |
| Rogue task detection | Smoke test on startup |
| Resolution | Polymarket Chainlink only (no Coinbase) |

## Running

```bash
# Local development
uv run pytest tests/          # 511 tests
uv run python scripts/run.py  # Start 5min bot

# Deploy to AWS
bash scripts/deploy_aws.sh              # Bot (ECS)
bash scripts/deploy_dashboard_lambda.sh  # Dashboard (Lambda)

# Opportunity scanner
PYTHONPATH=src uv run python scripts/opportunity_bot.py
PYTHONPATH=src uv run python scripts/opportunity_scanner.py
```

## Tech Stack

- Python 3.12, asyncio, uv
- py-clob-client (Polymarket CLOB SDK)
- Coinbase WebSocket (250ms price ticks)
- LightGBM (per-pair classifiers, trained on 22K Jon-Becker windows)
- AWS: ECS, DynamoDB, Bedrock (Haiku + Sonnet 4), Lambda, CloudFront, Secrets Manager
- structlog (JSON logging → CloudWatch)
