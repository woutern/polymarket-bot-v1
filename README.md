# Polymarket Trading Bot

Algorithmic trading bot for Polymarket crypto binary prediction markets. Trades BTC/ETH/SOL on 5-minute and 15-minute Up/Down windows.

**Status:** LIVE TRADING — $253 wallet, $1.25-$2.50 per trade (0.5-1% bankroll)
**Dashboard:** http://54.155.183.45:8888/
**Region:** us-east-1 (Virginia) — lower latency to Polymarket CLOB
**Tests:** 214 passing

---

## How It Works

1. **Coinbase WebSocket** feeds live BTC/ETH/SOL prices every 250ms
2. **Window tracker** monitors 5m/15m prediction windows
3. **Entry zone** (T-60s to T-15s) — evaluates potential trade
4. **Signal guards** — move threshold, EV > 6%, ask < $0.75, OBI spread < 0.15
5. **Probability** = 70% Bayesian model + 30% Claude AI (AWS Bedrock)
6. **Execution** = FOK limit order on Polymarket CLOB (`create_order` with `tick_size='0.01'`)
7. **Verification** = Polymarket Gamma API checks Chainlink oracle outcome 30s after close

### Per-Asset Move Thresholds

| Asset | 5-min | 15-min |
|-------|-------|--------|
| BTC   | 0.08% | 0.12%  |
| ETH   | 0.10% | 0.14%  |
| SOL   | 0.14% | 0.18%  |

---

## Architecture

```
us-east-1 (Virginia)
├── Bot — ECS Fargate (always-on, 250ms tick loop)
│   ├── CoinbaseWS         — real-time price feed
│   ├── WindowTracker      — 5m/15m state machine (6 pairs)
│   ├── BayesianUpdater    — P(UP) with base rate prior
│   ├── Bedrock Claude     — native us-east-1 (anthropic.claude-sonnet-4-6)
│   ├── SignalEvaluation   — logs every evaluation (fired + rejected)
│   ├── LiveTrader         — FOK orders via py-clob-client
│   └── DynamoDB           — trades, windows, signals tables
│
├── Dashboard — EC2 (54.155.183.45:8888)
│   ├── 4 pages: Overview, Trade Log, Signals, Analytics
│   ├── Signal funnel + rejection breakdown
│   └── Reads from us-east-1 DynamoDB
│
└── Storage
    ├── polymarket-bot-trades    — every executed trade with full metadata
    ├── polymarket-bot-windows   — window outcomes
    └── polymarket-bot-signals   — every signal evaluation (fired + rejected)
```

---

## Quick Commands

```bash
# Switch between paper/live mode
./scripts/switch.sh live
./scripts/switch.sh paper

# Force a test trade
uv run python scripts/force_trade.py --asset BTC --amount 1.50
uv run python scripts/force_trade.py --asset SOL --side NO --dry-run

# Run tests
uv run pytest tests/ -q

# Deploy bot
docker build --platform linux/amd64 -t polymarket-bot:latest .
aws --profile playground ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin 688567279867.dkr.ecr.us-east-1.amazonaws.com
docker tag polymarket-bot:latest 688567279867.dkr.ecr.us-east-1.amazonaws.com/polymarket-bot:latest
docker push 688567279867.dkr.ecr.us-east-1.amazonaws.com/polymarket-bot:latest

# Deploy dashboard
docker build --platform linux/amd64 -f Dockerfile.dashboard -t polymarket-dashboard:latest .
docker tag polymarket-dashboard:latest 688567279867.dkr.ecr.us-east-1.amazonaws.com/polymarket-dashboard:latest
docker push 688567279867.dkr.ecr.us-east-1.amazonaws.com/polymarket-dashboard:latest
```

---

## Test Coverage — 214 tests

| File | Tests | What |
|------|-------|------|
| test_sizing.py | 25 | Kelly formula, min/max trade USD, bankroll edge cases |
| test_bayesian.py | 19 | Probability updates, EMA blending, bounds |
| test_window_tracker.py | 13 | 5m/15m state machine transitions |
| test_directional.py | 26 | Signal guards, SignalEvaluation rejections, OBI veto |
| test_risk_manager.py | 24 | Circuit breaker, daily P&L cap |
| test_latency_monitor.py | 21 | p50/p95 latency stats |
| test_base_rate.py | 8 | Base rate table lookup |
| test_paper_trader.py | 5 | Dedup guard, price floor, circuit breaker |
| test_outcome_verification.py | 17 | Gamma API outcome, DB update, JSON parsing |
| test_live_trader.py | 9 | create_order (not create_market_order), tick_size, metadata |
| test_storage.py | 10 | SQLite CRUD, migrations, outcome update |
| test_dashboard_api.py | 14 | API endpoints, filtering, auth, stats correctness |
| test_region_config.py | 6 | No eu-west-1 in code, native Bedrock model ID |
| test_latency_fields.py | 4 | Timing fields stored on trades |
| test_force_trade.py | 9 | Dynamic sizing (0.5-1%), signal evaluation reasons |

---

## Roadmap

### Completed
- [x] us-east-1 migration (lower latency)
- [x] Live trading with $1.25-$2.50 per trade
- [x] Outcome verification via Polymarket Gamma API (Chainlink oracle)
- [x] Signal evaluation logging (fired + rejected, with reasons)
- [x] 4-page dashboard with signal funnel
- [x] Per-pair enable/disable control
- [x] Latency instrumentation (signal_ms, order_ms, bedrock_ms)

### Next (Phase 5-9)
- [ ] **RTDS oracle feed** — Binance vs Chainlink price lag = primary edge signal
- [ ] **Black-Scholes probability** — compute our own binary price, enter on oracle dislocation
- [ ] **Claude regime classifier** — 5-min regime calls (trending/choppy/post-move/pre-catalyst)
- [ ] **LightGBM model** — 40+ microstructure features, replaces Bayesian
- [ ] **River online learning** — adapts in real-time between LightGBM retrains
- [ ] **Conformal prediction gate** — MAPIE confidence intervals, skip uncertain trades
- [ ] **Thompson Sampling** — bandit optimization for model weight blending
- [ ] **SPRT edge measurement** — statistical proof of edge before scaling position size
- [ ] **Kelly sizing unlock** — dynamic sizing after 400+ trades with confirmed edge
- [ ] **Edge Analytics dashboard** — SPRT monitor, Brier scores, oracle lag, model performance
