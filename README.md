# Polymarket Bot

Systematic directional trading bot for Polymarket BTC/ETH/SOL 5-minute and 15-minute Up/Down prediction markets.

**Status:** PAPER TRADING — $1000 virtual bankroll
**Live dashboard:** http://63.33.49.226:8888/ (login: `admin` / `polybot2026`, IP changes on redeploy)
**AWS region:** eu-west-1 (Ireland) on ECS Fargate
**Last updated:** 2026-03-18

---

## Strategy

**Core edge:** At T-60s before a 5m/15m window closes, if price has moved >X% from the window open, the probability of reversal in the remaining 60 seconds is very low. Historical base rate: 96.4% win rate at 0.08% threshold (25,900 BTC windows, 90 days backtested).

### Entry Conditions (all must be true)
1. Price moved > threshold from window open (per-asset calibrated)
2. Market ask < 0.75 (market hasn't priced in the move)
3. EV > 6% = `(model_prob - ask) / ask`
4. T-minus 15s–60s
5. Orderbook fetched within last 30s (stale data guard)

### Per-Asset Move Thresholds (research-calibrated)

| Asset | 5-min | 15-min | Rationale |
|-------|-------|--------|-----------|
| BTC | 0.08% | 0.12% | Base case |
| ETH | 0.10% | 0.14% | ~1.3x BTC volatility |
| SOL | 0.14% | 0.18% | ~1.8x BTC volatility |

15-min stricter: market makers have more time to price in momentum.

### Signal Pipeline
1. **Coinbase WS** → real-time BTC/ETH/SOL price
2. **Bayesian updater** → P(UP) via EMA of price ticks + base rate prior
3. **Bedrock Claude Sonnet 4.6** → optional 30% AI weight blend
4. **EV filter** → only trade if edge > 6% over market price
5. **Quarter-Kelly sizing** → max 1% bankroll, $10 hard cap

---

## Backtest (90-day BTC, Dec 2025–Mar 2026, 25,900 windows)

| Threshold | Win Rate | Signals/day | EV at ask=0.65 |
|-----------|----------|-------------|----------------|
| 0.05% | 93.9% | 159 | +86% |
| **0.08%** | **96.4%** | **113** | **+43%** |
| 0.10% | 97.3% | 91 | +43% |
| 0.15% | 98.4% | 54 | +43% |

EV stays ~40-50% for asks 0.65-0.70 across all thresholds. `max_market_price=0.75` is the binding frequency constraint. Strategy is profitable up to a 43% fee rate.

---

## Architecture

```
AWS eu-west-1 ECS Fargate (single container)
├── Bot (scripts/run.py)
│   ├── CoinbaseWS       — real-time BTC/ETH/SOL price feed
│   ├── WindowTracker    — 5m/15m window state machine
│   ├── BayesianUpdater  — P(UP) with base rate prior
│   ├── Bedrock AI       — Claude Sonnet 4.6 probability blend
│   ├── DirectionalSignal — entry decision
│   ├── QuarterKelly     — position sizing
│   ├── PaperTrader      — simulated fills
│   └── DynamoDB         — trade/window storage
└── Dashboard (scripts/dashboard.py :8888)
    ├── FastAPI + HTTP Basic Auth
    ├── DynamoDB reads (paper trades only)
    └── Bedrock hourly strategy review
```

---

## Test Coverage — 138/138 passing

```
tests/test_sizing.py          23 tests  Kelly formula, edge cases
tests/test_bayesian.py        19 tests  probability updates, bounds
tests/test_window_tracker.py  13 tests  state machine transitions
tests/test_directional.py     20 tests  signal conditions
tests/test_risk_manager.py    24 tests  circuit breaker, daily P&L
tests/test_latency_monitor.py 21 tests  p50/p95 latency stats
tests/test_base_rate.py        8 tests  base rate table
```

---

## Deploy

```bash
docker build --platform linux/amd64 -t polymarket-bot:latest .
aws --profile playground ecr get-login-password --region eu-west-1 | \
  docker login --username AWS --password-stdin 688567279867.dkr.ecr.eu-west-1.amazonaws.com
docker tag polymarket-bot:latest 688567279867.dkr.ecr.eu-west-1.amazonaws.com/polymarket-bot:latest
docker push 688567279867.dkr.ecr.eu-west-1.amazonaws.com/polymarket-bot:latest

aws --profile playground ecs run-task \
  --cluster polymarket-bot --task-definition polymarket-bot:9 \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-09d92195326f57aaa],securityGroups=[sg-02d37542b9d600034],assignPublicIp=ENABLED}" \
  --region eu-west-1
```

---

## Research Roadmap (priority order)

- [ ] Order book imbalance veto (OBI < -0.30 → skip, per Cont et al. 2014)
- [ ] ETH/SOL-specific base rate tables (currently uses BTC base rates)
- [ ] Chainlink price feed for signals (recovers 3.5% WR from oracle divergence)
- [ ] A/B test Bedrock blend vs pure Bayesian
- [ ] Raise Kelly to 0.33 after 200+ trades confirmed ≥ 90% WR

## Current Session P&L

Paper mode: -$30.00 (3 bad trades at price=0.001 from uninitialized orderbook — fixed).
Stale orderbook guard + min_price=0.05 prevents recurrence.
