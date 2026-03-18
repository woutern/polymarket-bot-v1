# Polymarket Trading Bot

Algorithmic trading bot for Polymarket crypto binary prediction markets.

**Status:** LIVE — $260 portfolio, 1% per trade, all 6 pairs
**Dashboard:** http://54.155.183.45:8888/
**Tests:** 295 passing

## What it does

Trades BTC/ETH/SOL × 5m/15m Up/Down prediction markets on Polymarket.
Enters T+2s-T+15s after window open when price moves >0.02%.
LightGBM models (6 per pair) filter low-confidence signals.
Resolves trades via Polymarket Gamma API (never Coinbase inference).

## Architecture

- **Bot:** ECS Fargate eu-west-1 (250ms tick loop, 24/7)
- **Dashboard:** EC2 us-east-1 (5 pages, CET timestamps)
- **Storage:** DynamoDB us-east-1 (trades, windows, signals, training_data, kpi_snapshots)
- **Models:** S3 us-east-1 (6 LightGBM .pkl files), SSM for paths
- **Retrain:** EventBridge every 4h → ECS RunTask
- **Watchdog:** Docker HEALTHCHECK restarts container if frozen >5min
- **Nothing runs on laptop**

## Safety

- Circuit breakers: 3 streak → 15min pause, 5/20 losses → $1 flat, 10% daily → stop
- Dedup: memory + DynamoDB (one trade per window)
- Resolution: Gamma API only (90s + 3 retries)
- Smoke test: 9 checks on startup
- LightGBM blocks trades when prob < 0.55
- Heartbeat every 60s → watchdog restarts on freeze

## Commands

```bash
./scripts/switch.sh live|paper          # switch mode
uv run python scripts/force_trade.py    # manual trade
uv run python scripts/redeem.py         # redeem via Safe
uv run pytest tests/ -q                 # 295 tests
```
