# Scaleflow Polymarket Trading Bot — Claude Code Context

> Read this file fully at the start of every session. This is the single source of truth.

---

## What this project is

An algorithmic trading bot for Polymarket crypto binary prediction markets.
We trade BTC/ETH/SOL × 5min/15min Up/Down markets (6 pairs).
Binary bets: does price close higher or lower than the open at window start?

Owner: Wouter (Scaleflow)
Stack: Python, AWS (us-east-1), Polygon blockchain
Live wallet: $32, $1 flat bets until 400+ trades with confirmed statistical edge

---

## How it trades (core loop)

1. Coinbase WebSocket feeds live prices every 250ms
2. Window opens → bot tracks price move from open
3. Entry logic fires based on tier (see Entry Tiers below)
4. Probability computed from blended model (see Probability Model below)
5. Signal fires when: edge > threshold AND ask < $0.90 AND conformal gate passes
6. Sizing = flat $1 per trade (Kelly computed but floored at $1 until 400+ trades)
7. Execution = FOK limit order on Polymarket CLOB (Tier A/C), GTD limit (Tier B)
8. After close → verify outcome via Polymarket Gamma API (Chainlink oracle)
9. After resolution → update River online model, run SPRT update, trigger meta-learning if 10-trade batch complete

---

## Current architecture (POST-MIGRATION: us-east-1)

### AWS Infrastructure
- Bot: ECS Fargate, us-east-1 — always-on, 250ms tick loop
- Dashboard: EC2 at http://54.155.183.45:8888/ — 4 pages (Overview, Trade Log, Analytics, Edge Analytics)
- Storage: DynamoDB us-east-1 (trades, windows, training_data, edge_metrics, meta_insights, bandit_state tables)
- SQLite: local backup inside container
- AI: AWS Bedrock Claude Sonnet 4.6 (anthropic.claude-sonnet-4-6 — native us-east-1, no eu. prefix)
- Model artifacts: S3 bucket, paths tracked in SSM Parameter Store
- Auto-claim: every 10 min, redeems winning positions

### Why us-east-1
- Coinbase exchange hosted in AWS us-east-1 → saves 67–85ms on price data vs eu-west-1
- Bedrock Claude native (no cross-region routing)
- Latency budget target: Coinbase tick → Polymarket order < 30ms total

---

## Data feeds (three WebSocket connections, always open)

### Feed 1: Coinbase WebSocket (price + order book)
- URL: wss://ws-direct.exchange.coinbase.com (authenticated direct feed, us-east-1 proximity)
- Channels: ticker (250ms price), level2 (order book for OFI/depth features)
- Assets: BTC-USD, ETH-USD, SOL-USD
- Used for: price data, microstructure features, realized volatility

### Feed 2: Polymarket RTDS (oracle lag signal)
- URL: wss://ws-live-data.polymarket.com
- Streams: binance_price AND chainlink_price per asset simultaneously
- Used for: oracle_lag_ms, oracle_lag_pct, oracle_dislocation signal
- Critical: Chainlink updates every ~10-30s or 0.5% deviation — Coinbase leads by 15-45s
- This lag is our primary edge

### Feed 3: Polymarket CLOB WebSocket
- Market channel: wss://ws-subscriptions-clob.polymarket.com/ws/market
- User channel: wss://ws-subscriptions-clob.polymarket.com/ws/user
- Used for: live order book, yes_ask/yes_bid, tick_size_change events, fill confirmations

---

## Entry tiers (replaces old T-60s to T-15s logic)

### STRATEGY PIVOT (2026-03-18 backtest finding)
Direction prediction is SOLVED: 97%+ win rate when move >0.08% by T-60s.
The ONLY problem is entry price. By T-60s the ask is $0.95 (EV: +$0.02).
We must enter EARLIER at $0.55-0.65 before market makers price it in.

30-day BTC 5m backtest (4,065 signals):
- T-120s entry (0.03% threshold): 96.2% WR, ask ~$0.55-0.65, EV ~+$0.60/dollar
- T-180s entry (0.03% threshold): 92.3% WR, ask ~$0.50-0.60, EV ~+$0.55/dollar
- T-240s entry (0.03% threshold): 86.3% WR, ask cheapest but lower WR
- T-60s entry (current, 0.08%):   97.3% WR, ask $0.95+, EV ~+$0.02/dollar

### Tier A — Oracle dislocation (fires at any time in window)
Condition: oracle_dislocation = True AND edge > 0.05
- oracle_dislocation = abs(oracle_lag_pct) > 0.003
- Enter immediately, do not wait
- Order type: FOK (aggressive, take the ask)
- Rare event — bonus edge, not primary

### Tier B — Early directional (T-120s to T-90s) *** PRIMARY STRATEGY ***
Condition: move >0.03% from open AND ask < 0.70 AND ev > 0.10
- THIS IS THE MAIN EDGE — enter before market prices it in
- At $0.60 entry with 96% WR: EV = (0.96-0.60)*(1/0.60) = +$0.60 per dollar
- Order type: GTD limit at ask, auto-cancel at T-30s
- LightGBM goal: predict at T-120s whether move holds to close
- Only fires if Tier A did not already fire this window

### Tier C — Late confirmation (T-60s to T-30s) — FALLBACK ONLY
Condition: edge > 0.10 AND ask < 0.75
- Only fires if Tiers A and B did not fire this window
- Low EV at T-60s asks ($0.90+) — only worth it on outlier pricing
- Order type: FOK

---

## Probability model (blended ensemble)

### Components and weights
1. LightGBM (60% base) — batch model, retrained every 4h
2. River online model (10-30% adaptive) — updates on every resolved trade
3. Claude regime-adjusted probability (20% base) — from 5-min regime call
4. Oracle Black-Scholes probability (20% base) — real-time from RTDS feed

### Blending formula
```python
final_prob = (
    lgbm_weight * lgbm_prob +
    river_weight * river_prob +
    bedrock_weight * bedrock_regime_prob +
    oracle_weight * oracle_bs_prob
)
edge = final_prob - yes_ask
```

### Weight adjustment by regime
```
Regime A (TRENDING):         oracle +0.15, threshold -0.01, Tier B enabled
Regime B (CHOPPY):           oracle -0.10, threshold +0.02, Tier B disabled
Regime C (POST-MOVE):        oracle  0.00, threshold +0.03, Tier B disabled
Regime D (PRE-CATALYST):     oracle +0.05, threshold -0.005, Tier B enabled
Regime E (HIGH-UNCERTAINTY): oracle -0.20, threshold +0.05, Tier B disabled
Regime F (OVERREACTION): move >0.3% in first 120s AND yes_ask >0.75. Bias: FADE (Tier B fires in OPPOSITE direction to the move)
```

### Thompson Sampling bandit (dynamic weight optimizer)
- 5 weight configurations as arms (from oracle-heavy to lgbm-heavy)
- 30 contexts: asset × timeframe × regime
- State persisted in DynamoDB bandit_state table
- Updates after every resolved trade
- Meaningful signal after ~2 weeks of data

---

## AI components (detail)

### Claude Sonnet 4.6 — regime classifier ONLY (not per-trade)
- Called every 5 minutes via Bedrock (anthropic.claude-sonnet-4-6)
- Returns JSON: regime A-E, confidence 0-1, directional_bias UP/DOWN/NEUTRAL, reasoning
- Prompt includes: price, returns, vol, correlation, OFI, oracle lag, large trades, hour UTC
- Regime output fed as features into LightGBM (regime_encoded, regime_confidence, directional_bias_encoded)
- Regime also adjusts signal weights and entry thresholds (see above)
- Cost: ~$0.15/day at ~1,500 calls/day
- DO NOT call per-trade — too slow, wrong job for an LLM

### LightGBM — batch model (6 models, one per asset+timeframe)
- Models: BTC_5m, BTC_15m, ETH_5m, ETH_15m, SOL_5m, SOL_15m
- Training: rolling 5,000 windows, time-ordered 80/20 split, 5-min embargo
- Calibration: Platt scaling → Isotonic regression (double calibration)
- Artifact storage: S3, latest path in SSM Parameter Store
- Retrain: every 4h or 100 predictions (whichever first), scheduled ECS task
- Deploy gate: only if new Brier score < current Brier score
- Fallback: Bayesian model if no artifact available

### River — online learning (inside main ECS container)
- Model: HoeffdingAdaptiveTreeClassifier (drift-aware, grace_period=50)
- Updates: after every resolved trade, <1ms
- Weight in blend: 0.10 (at 50% accuracy) → 0.30 (at 60%+ accuracy), linear interpolation
- Purpose: catches intraday session effects between 4h retrains
- State: in-memory, checkpoint to DynamoDB every 100 updates

### MAPIE — conformal prediction (trade gate)
- Wraps calibrated LightGBM, method="score", cv="prefit"
- Calibration set: last 500 resolved trades
- Coverage: 90% (alpha=0.10)
- Trade gate: SKIP if interval_width > 0.15
- Effect: filters ~30-50% of uncertain trades

### Meta-learning loop — async, not in hot path
- Trigger: every 10 resolved trades
- Sends batch to Claude: features, outcomes, regime, entry tier, top 5 features per trade
- Claude returns: loss_patterns, win_patterns, regime_observation, feature_flag,
  recommended_threshold_adjustment, skip_condition
- Stored in meta_insights DynamoDB table
- Loss patterns fed as context into next regime classification prompts
- Threshold nudges: max ±20% of current value, require 3 consecutive same suggestion to apply
- Cost: ~$0.05/day

---

## Feature engineering (40+ features per tick)

All features z-score normalized: (feature - rolling_mean_500) / rolling_std_500
Use deque(maxlen=500) per feature for rolling stats.

### Tier 1 — Microstructure (Coinbase level2 order book)
- ofi_30s, ofi_1m, ofi_5m: (buy_volume - sell_volume) / total_volume
- vpin: Volume-synchronized Probability of Informed Trading
- bid_ask_spread: (best_ask - best_bid) / mid_price
- depth_imbalance: (bid_depth_top5 - ask_depth_top5) / (bid + ask depth top5)
- trade_arrival_rate: trades per second, last 30s
- effective_spread: 2 × abs(trade_price - mid_price) / mid_price

### Tier 2 — Technical
- rsi_3, rsi_7, rsi_14
- macd_signal: MACD(12,26,9) signal line
- bb_position: (price - lower) / (upper - lower), 20-period Bollinger Bands
- momentum_5m, momentum_15m: log price returns
- volume_momentum: (vol_1m - vol_ma_5m) / vol_ma_5m

### Tier 3 — Volatility
- realized_vol_5m, realized_vol_15m: from tick log returns
- parkinson_vol: (1/(4×ln2)) × (ln(high/low))², 20-period rolling
- garman_klass_vol: OHLC estimator
- vol_ratio: realized_vol_5m / realized_vol_15m

### Tier 4 — Cross-asset (BTC as base)
- btc_eth_corr: EW correlation λ=0.94 (~15s half-life)
- btc_sol_corr: same
- btc_rel_strength: btc_return_5m - asset_return_5m
- cross_asset_signal: BTC momentum direction (1/-1/0, threshold 0.05%)

### Tier 5 — Regime/time
- hour_sin, hour_cos: sin/cos(2π × hour/24)
- dow_sin, dow_cos: sin/cos(2π × day/7)
- vol_regime: 1 if realized_vol_5m > vol_ma_1h else 0
- adx_14: Average Directional Index, 14-period

### Tier 6 — AI-derived (from 5-min Claude regime call)
- regime_encoded: A=0, B=1, C=2, D=3, E=4
- regime_confidence: 0.0-1.0 from Claude output
- directional_bias_encoded: UP=1, DOWN=-1, NEUTRAL=0

---

## Oracle signal computation

```python
# Black-Scholes probability from RTDS feed
d2 = ln(binance_price / strike) / (realized_vol_per_second * sqrt(time_to_expiry_seconds))
oracle_probability = scipy.stats.norm.cdf(d2)

# Oracle dislocation detection
oracle_lag_pct = (binance_price - chainlink_price) / chainlink_price
oracle_dislocation = abs(oracle_lag_pct) > 0.003

# Realized vol: last 100 Coinbase ticks (250ms = ~25s of data)
# strike = window open price
# time_to_expiry_seconds = seconds remaining in window
```

---

## DynamoDB tables (all us-east-1)

### trades (primary key: trade_id)
Fields: asset, timeframe, entry_price, our_probability, outcome,
        oracle_lag_ms, oracle_lag_pct, regime, regime_confidence,
        directional_bias, entry_tier, lgbm_prob, river_prob, oracle_prob,
        bedrock_prob, final_prob, edge_at_entry, interval_width,
        confidence_gate_passed, kelly_suggested_size, actual_bet_size,
        arm_index, context, timestamp, window_id,
        coinbase_to_order_ms, fill_latency_ms, bedrock_latency_ms, feature_compute_ms

### windows (primary key: window_id)
Fields: asset, timeframe, open_price, close_price, outcome, start_ts, end_ts

### training_data (primary key: window_id, sort key: asset_timeframe)
Fields: all 40+ features as map, outcome, market_price_at_entry, timestamp

### edge_metrics (primary key: trade_id)
Fields: sprt_log_likelihood, brier_contribution, oracle_lag_at_entry,
        entry_tier, interval_width, regime, timestamp

### meta_insights (primary key: batch_id)
Fields: loss_patterns, win_patterns, regime_observation, feature_flag,
        recommended_thresholds, skip_condition, timestamp, trades_analyzed

### bandit_state (primary key: context e.g. "BTC_5m_A")
Fields: arm_alphas (list of 5), arm_betas (list of 5), total_pulls, last_updated

---

## Polymarket API reference

### Endpoints
- CLOB REST: https://clob.polymarket.com
- CLOB WS market: wss://ws-subscriptions-clob.polymarket.com/ws/market
- CLOB WS user: wss://ws-subscriptions-clob.polymarket.com/ws/user
- RTDS: wss://ws-live-data.polymarket.com
- Gamma API: https://gamma-api.polymarket.com (resolution / Chainlink oracle)

### Order types
- FOK: aggressive take — Tier A and C entries
- GTD: passive limit with expiry — Tier B entries, cancel at T-30s
- FAK: partial fill OK — fallback if FOK fill rate drops below 70%

### Key constraints
- Minimum order: 5 shares
- Batch endpoint: up to 15 orders per request
- Maker rebate: order must rest ≥3.5s
- Rate limits: 350/s burst, 60/s sustained
- FOK BUY amount = dollar amount (not share count)
- Tick size refines to 0.001 when price >0.96 or <0.04 (watch tick_size_change events)
- Signing: EIP-712 in Python (~1s) — future Rust migration (rs-clob-client) deferred

---

## Position sizing

```python
def compute_position_size(edge, market_price, bankroll, model_age_hours):
    kelly = edge / (1 - market_price)
    uncertainty_discount = max(0.1, 1.0 - (model_age_hours / 8.0))
    fraction = kelly * 0.25 * uncertainty_discount
    raw = max(1.0, min(bankroll * 0.05, bankroll * fraction))
    return round(raw, 2)
```

HARD RULE: actual_bet_size = $1.00 until SPRT confirms edge AND trade count ≥ 400.
Always log both kelly_suggested_size and actual_bet_size.

---

## Hard rules (from 112K wallet analysis + Wouter directives)

1. **MIN_EV = 0.15** — only trade when EV per dollar > 15%:
   `ev = (model_prob - market_price) * (1 / market_price)`
   NOT the old `(model_prob - market_price) / market_price`

2. **Early exit**: if position value increases >30% before settlement,
   SELL immediately and recycle capital. Don't always hold to resolution.

3. **Regime F (OVERREACTION)** validated by top 1% wallets — they fade
   extreme prices. Confirmed in Phase 6 plan.

4. **Never expand beyond crypto price markets** until SPRT confirms edge
   at 400+ trades. No politics, no sports, no custom markets.

5. **Capital reserve**: never commit >70% of bankroll to open positions
   simultaneously. Check `sum(open_sizes) + new_size <= bankroll * 0.70`
   before every trade. Reject with reason="capital_reserve" if breached.

6. **$1 flat bet** until SPRT confirmed. No exceptions.

---

## Edge measurement

### SPRT — update after every trade
```
log_lambda += outcome * log(p1/p0) + (1-outcome) * log((1-p1)/(1-p0))
Boundary A = log((1-β)/α)  → edge confirmed, unlock dynamic sizing
Boundary B = log(β/(1-α))  → no edge, halt and reassess
```
Use α=0.05, β=0.20. Plot log_lambda on Edge Analytics dashboard.

### Brier score
Per trade: (entry_price - outcome)²
Rolling 50-trade average. Target < 0.08.
Brier Skill Score = 1 - (our_BS / market_BS). Positive = edge exists.

### Sample size requirements
5% edge: ~620 trades | 10% edge: ~150 trades | 20% edge: ~40 trades
Current: 1 trade. No statistical claims valid.

---

## Dashboard (4 pages, EC2 port 8888)

Page 1 — Overview: live P&L, positions, balance, win rate
Page 2 — Trade Log: all trades with edge, tier, regime, interval width
Page 3 — Analytics: P&L by asset/timeframe/hour, drawdown
Page 4 — Edge Analytics:
  - SPRT monitor (log-likelihood curve per pair, boundaries A and B)
  - Brier score + calibration curve (predicted vs actual, decile buckets)
  - Oracle lag monitor (live per asset, p50/p95, dislocation events last 24h)
  - LightGBM feature importance (top 15)
  - Entry tier analysis (win rate and P&L per tier A/B/C)
  - Kelly sizing tracker (suggested vs actual, trades to unlock dynamic sizing)
  - Meta-insights log (latest Claude pattern analysis output)
  - Data collection progress (windows collected per asset/timeframe toward 500)
  - Model age and last retrain Brier score

---

## Performance targets (log every trade)

- Coinbase tick → order submission: p50 < 15ms, p95 < 30ms
- Order submission → fill confirmation: p50 < 50ms
- Bedrock regime call: p50 < 400ms (async, non-blocking)
- Feature computation: p50 < 5ms
- River update: < 1ms
- LightGBM inference: < 10ms

---

## Constraints — read before every change

1. 193 tests must pass at all times — run before and after every change
2. EC2 dashboard stays up and accessible throughout all work
3. Keep eu-west-1 running until Wouter explicitly says to shut it down
4. Never increase bet above $1 without Wouter's explicit instruction
5. Never deploy a model with higher Brier score than the current one
6. Never call Bedrock per-trade — regime classifier only, every 5 minutes
7. Meta-learning threshold nudges: max ±20%, require 3 consecutive same suggestion
8. All new AWS resources in us-east-1
9. DynamoDB table names must match exactly what code expects
10. When in doubt: ask before executing

---

## What is NOT changing yet

- Blockchain/Polygon auto-claim logic
- Dashboard frontend framework (adding page 4 only)
- SQLite backup structure
- Test suite structure (add tests, never remove)
- EIP-712 Python signing (Rust migration is future work)

---

## Libraries

- lightgbm: batch model
- river: online learning
- mapie: conformal prediction
- scipy.stats: Black-Scholes N(d2)
- py_clob_client: Polymarket orders
- boto3: AWS (Bedrock, DynamoDB, S3, SSM, Secrets Manager)
- websockets: all three feeds
- pandas / numpy: feature computation

---

## Secrets (AWS Secrets Manager, us-east-1)

Same key names as eu-west-1:
POLYMARKET_PRIVATE_KEY, POLYMARKET_API_KEY, POLYMARKET_API_SECRET,
POLYMARKET_API_PASSPHRASE, COINBASE_API_KEY, COINBASE_API_SECRET
