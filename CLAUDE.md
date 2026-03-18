# Scaleflow Polymarket Trading Bot — Claude Code Context

## What this project is
An algorithmic trading bot for Polymarket crypto binary prediction markets.
We trade BTC/ETH/SOL × 5min/15min Up/Down markets (6 pairs).
Binary bets: does price close higher or lower than the open?

Owner: Wouter (Scaleflow)
Stack: Python, AWS, Polygon blockchain

---

## Current architecture (eu-west-1 — TO BE MIGRATED)

### How it trades
1. Coinbase WebSocket feeds live prices every 250ms
2. Window opens → bot tracks price move from open
3. Entry zone T-60s to T-15s → if price moved enough, evaluate trade
4. Signal fires when: move > threshold AND ask < $0.75 AND expected value > 6%
5. Probability = 70% Bayesian model + 30% Claude AI (AWS Bedrock)
6. Sizing = flat $1 per trade (Quarter-Kelly, floored at $1)
7. Execution = FOK limit order on Polymarket CLOB
8. After close → verify outcome via Polymarket Gamma API (Chainlink oracle)

### Current thresholds (to be replaced by ML model)
- BTC: 0.08% move
- ETH: 0.10% move
- SOL: 0.14% move

### Infrastructure (current)
- Bot: ECS Fargate, eu-west-1 (Ireland)
- Dashboard: EC2 at http://54.155.183.45:8888/ (3 pages: Overview, Trade Log, Analytics)
- Storage: DynamoDB (trades + windows tables), SQLite (local backup)
- AI: AWS Bedrock Claude Sonnet 4.6 (cross-region inference via eu.anthropic prefix)
- Auto-claim: runs every 10 min, redeems winning positions
- 193 tests passing
- Live mode, $32 wallet, $1 flat trades

---

## Migration target: us-east-1 (N. Virginia)

### Why we're moving
- Coinbase exchange is hosted in AWS us-east-1 → -67-85ms on price data
- Bedrock Claude is native in us-east-1 (no cross-region prefix needed)
- Polymarket CLOB API calls may also be faster

### What's already available in us-east-1
- ECS Fargate: empty, ready
- DynamoDB: empty, ready (need to recreate tables with same schema)
- ECR: empty, ready (need to push images)
- Bedrock Claude: native (use anthropic.claude-sonnet-4-6, no eu. prefix)
- Secrets Manager: needs secrets recreated from eu-west-1

### Migration steps needed
1. Recreate DynamoDB tables (trades, windows) with same schema as eu-west-1
2. Push ECR images to us-east-1 repos
3. Recreate Secrets Manager secrets
4. Update ECS task definitions to point to us-east-1 resources
5. Update Bedrock model ID (remove eu. prefix)
6. Deploy ECS service in us-east-1
7. Verify dashboard EC2 instance connectivity
8. Run smoke tests
9. Decommission eu-west-1 resources

---

## Strategy improvements to implement (from research)

### Problem: T-60s entry is too late (mathematical certainty has arrived)
Binary option pricing: at 60s to expiry with BTC vol ~50%, a 0.1% move from strike = 92%+ probability.
The market correctly prices YES at 0.99. We're trying to buy at the wrong time.

### Fix 1: Earlier entry window (T-240s to T-120s)
- Enter when market is at 0.55–0.70, not 0.95+
- At $0.60 entry, need only 60% win rate to profit
- Current T-60s entry needs ~99% accuracy

### Fix 2: Oracle latency exploitation
- Polymarket uses Chainlink Data Streams (updates every ~10-30s or 0.5% deviation)
- Our Coinbase WebSocket leads Chainlink by 15-45 seconds
- Strategy: compute our own binary probability in real-time
- Trade when our probability > Polymarket price by >3%

### Fix 3: Replace Bayesian model with LightGBM
Research shows gradient boosting >> LSTM/deep learning for this use case.
Feature tiers (in priority order):

**Tier 1 - Microstructure (most important)**
- Order Flow Imbalance (OFI) at 30s/1m/5m windows
- VPIN (Volume-synchronized Probability of Informed Trading)
- Bid-ask spread
- Depth imbalance
- Trade arrival rate
- Effective spread

**Tier 2 - Technical**
- RSI at 3/7/14 periods
- MACD signal
- Bollinger Band position
- 5m/15m momentum
- Volume momentum

**Tier 3 - Volatility**
- Realized volatility 5m/15m
- Parkinson estimator
- Garman-Klass OHLC estimator
- Short/long vol ratio

**Tier 4 - Cross-asset**
- BTC-ETH/SOL rolling correlation (λ=0.94 for 15s half-life)
- Relative strength
- Cross-asset VPIN

**Tier 5 - Regime/time**
- Hour-of-day (sin/cos encoded)
- Day-of-week
- Volatility regime
- ADX trend strength

Model config:
- LightGBM binary classification
- Rolling 5,000 candle training window
- Retrain every 4h or 100 predictions
- Double calibration: Platt scaling → Isotonic regression
- CPCV validation with 5-min embargo
- Minimum edge threshold: 3% over market price

### Fix 4: Rust order signing (future, not now)
Python EIP-712 signing takes ~1s. Rust client does it in <2ms.
Polymarket official Rust client: https://github.com/Polymarket/rs-clob-client
Defer this — do us-east-1 move and ML model first.

### Fix 5: Subscribe to Polymarket RTDS feed
URL: wss://ws-live-data.polymarket.com
Streams both Binance AND Chainlink prices simultaneously.
Comparing these directly reveals the oracle lag in real-time.
This is the core signal for oracle latency exploitation.

---

## Polymarket API reference

### CLOB endpoints
- Base: https://clob.polymarket.com
- WS market channel: wss://ws-subscriptions-clob.polymarket.com/ws/market
- WS user channel: wss://ws-subscriptions-clob.polymarket.com/ws/user
- RTDS: wss://ws-live-data.polymarket.com

### Order types supported
- GTC (Good Till Cancelled)
- GTD (Good Till Date) — use this for passive entries with auto-cancel
- FOK (Fill or Kill) — current, for aggressive market taking
- FAK (Fill and Kill) — IOC equivalent, partial fills OK

### Key constraints
- Minimum order: 5 shares
- Batch endpoint: up to 15 orders per request
- Maker orders must rest ≥3.5s for liquidity rebates
- Rate limits: 350 orders/s burst, 60/s sustained
- FOK BUY specifies dollar amount, not share count

### Tick sizes
- Default: 0.01
- When price >0.96 or <0.04: automatically 0.001 or 0.0001
- Watch for tick_size_change WebSocket events

---

## Edge measurement framework

### Metrics to track
- Brier score: mean((entry_price - outcome)²) — target < 0.08
- Brier Skill Score: 1 - (our_BS / market_BS) — positive = edge exists
- Win rate vs market-implied probability
- ROI per market, per asset, per timeframe
- Oracle lag captured (ms between Coinbase tick and Polymarket price update)

### SPRT for live monitoring
After each trade, update log-likelihood ratio:
log(Λₙ) = Σ [xᵢ × log(p₁/p₀) + (1-xᵢ) × log((1-p₁)/(1-p₀))]
Cross boundary A = (1-β)/α → edge confirmed
Cross boundary B = β/(1-α) → no edge, reassess

### Minimum trades for significance
- 5% edge: ~620 trades needed
- 10% edge: ~150 trades needed
- 20% edge: ~40 trades needed
Current status: 1 trade. No statistical claims yet.

---

## Key decisions and constraints

- Keep $1 flat bet sizing until we hit 400+ trades with confirmed edge
- Do NOT increase position size without statistical validation
- Keep 193 passing tests green throughout migration
- Dashboard must stay accessible throughout migration (EC2 stays up)
- Keep eu-west-1 running in parallel until us-east-1 is fully validated
- Use the same DynamoDB table schema — dashboard reads from it

---

## What NOT to change (yet)
- Order execution logic (FOK for now)
- Blockchain/Polygon interaction (auto-claim logic)
- Dashboard frontend (EC2, port 8888)
- SQLite local backup structure
- Test suite structure (193 tests must stay green)

---

## How to work with this codebase
- Always run tests before and after changes: check that 193 pass
- Use AWS CLI with --region us-east-1 for all new resources
- Secrets are in AWS Secrets Manager (recreate in us-east-1 with same key names)
- DynamoDB table names must match exactly what the code expects
- ECS task definitions reference ECR image URIs — update these for us-east-1 ECR
