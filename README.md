# Polymarket Trading Bot

Algorithmic trading system for Polymarket prediction markets.

**Status:** LIVE on AWS ECS (eu-west-1)
**Dashboard:** https://d2rj5lnnfnptd.cloudfront.net/
**Tests:** 479 passing

## Two Trading Strategies

### 1. 5-Minute Crypto Bot
- Trades BTC/SOL 5-minute Up/Down windows
- Scan window T+210s–T+240s: finds best entry price
- Trailing-the-leader sizing (leader $10, tied $5, follower $2.50)
- Resolution via Polymarket Chainlink oracle (not Coinbase)

### 2. Opportunity Bot
- 7 parallel workers scan all Polymarket markets every 30 min
- Categories: crypto, finance, politics, geopolitics, tech, basketball, news
- Tier 1 ($0.85–$0.95): auto-trade $5 FOK, no AI
- Tier 2 ($0.65–$0.85): Claude Haiku AI assessment via Bedrock
- $1,000 max total deployed, FOK taker orders only

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the complete system diagram.

| Component | Location |
|-----------|----------|
| Bot | ECS Fargate, eu-west-1 |
| Dashboard | Lambda + CloudFront, us-east-1 |
| Storage | DynamoDB, us-east-1 |
| AI | Bedrock (Claude Haiku), us-east-1 |
| Secrets | AWS Secrets Manager, eu-west-1 |

## Safety Guards

| Guard | Value |
|-------|-------|
| Max bet (5min bot) | $10 hard cap |
| Max ask (5min bot) | $0.82 SOL / $0.78 BTC |
| Max deployed (opp bot) | $1,000 |
| Max ask (opp bot) | $0.95 |
| Dedup | 3-layer (memory + DynamoDB + atomic claim) |
| Rogue task detection | Smoke test on startup |
| Resolution | Polymarket Chainlink only (no Coinbase) |

## Running

```bash
# Local development
uv run pytest tests/          # 479 tests
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
- ESPN API (live basketball scores)
- AWS: ECS, DynamoDB, Bedrock, Lambda, CloudFront, Secrets Manager
- structlog (JSON logging → CloudWatch)
