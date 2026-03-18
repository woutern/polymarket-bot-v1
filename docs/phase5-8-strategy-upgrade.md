# Phase 5–8: Strategy Upgrade — AI, Signals, and Edge Measurement

The us-east-1 migration is complete. Now we upgrade the trading strategy.
Read CLAUDE.md again before starting. Execute phases in order. Each phase ends with a test run (193 tests must stay green) and a checkpoint where you report what was done before proceeding.

---

## PHASE 5: Add the RTDS feed (oracle lag signal)

This is the highest-priority signal change. Polymarket publishes both Binance and Chainlink prices on a single WebSocket. Comparing them reveals the exact lag between real price and oracle price — that gap is our primary edge.

**What to build:**
1. Add a second persistent WebSocket connection to: `wss://ws-live-data.polymarket.com`
2. Parse both the `binance_price` and `chainlink_price` fields per asset (BTC, ETH, SOL)
3. Compute `oracle_lag_ms` = timestamp difference between the two feeds per tick
4. Compute `oracle_lag_pct` = (binance_price - chainlink_price) / chainlink_price
5. Store rolling oracle lag stats (mean, p50, p95 over last 60 ticks) in memory per asset
6. Add a new signal: `oracle_dislocation` = True when abs(oracle_lag_pct) > 0.003 (0.3%)

**New entry logic using oracle dislocation:**
- If `oracle_dislocation` is True AND direction is confirmed by Binance price move:
  - Compute our own binary probability using Black-Scholes:
    - `d2 = ln(binance_price / strike) / (realized_vol * sqrt(time_to_expiry))`
    - `our_probability = N(d2)` for YES (use scipy.stats.norm.cdf)
  - Calculate `edge = our_probability - polymarket_yes_ask`
  - If `edge > 0.03`: fire trade signal immediately (do not wait for T-60s)
- This replaces the current "wait for T-60s to T-15s entry zone" logic
- Keep the old entry logic as a fallback when oracle_dislocation is False

**Realized volatility calculation:**
- Use the last 100 Coinbase price ticks (250ms each = ~25 seconds of data)
- `realized_vol_per_tick = std(log_returns) * sqrt(ticks_per_year)`
- `ticks_per_year = (365 * 24 * 3600 * 4)` for 250ms ticks
- Store rolling realized vol per asset in memory

**What to log per trade (add to DynamoDB trade record):**
- `oracle_lag_ms` at time of entry
- `oracle_lag_pct` at time of entry
- `our_probability` (Black-Scholes estimate)
- `market_price` (yes_ask at entry)
- `edge_at_entry` (our_probability - market_price)
- `entry_timing` (seconds before window close)

**Tests to add:**
- Test that RTDS WebSocket connects and parses both price fields
- Test Black-Scholes probability calculation with known inputs
- Test oracle_dislocation detection triggers correctly
- Test that edge < 0.03 suppresses trade signal

Checkpoint: show me the RTDS feed parsing output on BTC for 60 seconds in paper mode before proceeding.

---

## PHASE 6: Build the LightGBM signal model

Replace the 70% Bayesian model component. Keep Claude (Bedrock) as a 20% component temporarily during transition. Target: LightGBM 60% + Bedrock 20% + oracle signal 20%.

**Step 1: Feature engineering pipeline**

Build a `FeatureEngine` class that computes the following on every Coinbase tick, per asset:

Tier 1 — Microstructure (compute from Coinbase order book feed):
- `ofi_30s`: Order Flow Imbalance over last 30s = (buy_volume - sell_volume) / total_volume
- `ofi_1m`: same over 1 minute
- `ofi_5m`: same over 5 minutes
- `bid_ask_spread`: (best_ask - best_bid) / mid_price
- `depth_imbalance`: (bid_depth_top5 - ask_depth_top5) / (bid_depth_top5 + ask_depth_top5)
- `trade_arrival_rate`: trades per second over last 30s
- Note: subscribe to Coinbase full order book channel (not just ticker) for these

Tier 2 — Technical (compute from price history):
- `rsi_3`, `rsi_7`, `rsi_14`: RSI at 3, 7, 14 periods (using 250ms ticks aggregated to 1s)
- `macd_signal`: MACD(12,26,9) signal line
- `bb_position`: (price - bb_lower) / (bb_upper - bb_lower), Bollinger Bands 20-period
- `momentum_5m`: price return over last 5 minutes
- `momentum_15m`: price return over last 15 minutes
- `volume_momentum`: (volume_last_1m - volume_ma_5m) / volume_ma_5m

Tier 3 — Volatility:
- `realized_vol_5m`: realized vol over 5 minutes (from tick returns)
- `realized_vol_15m`: realized vol over 15 minutes
- `parkinson_vol`: (1/(4*ln(2))) * (ln(high/low))^2, rolling 20-period
- `vol_ratio`: realized_vol_5m / realized_vol_15m (short/long ratio)

Tier 4 — Cross-asset (compute cross-asset features, BTC as base):
- `btc_eth_corr`: exponentially weighted correlation, λ=0.94 (~15s half-life)
- `btc_sol_corr`: same
- `btc_rel_strength`: (btc_return_5m - asset_return_5m)
- `cross_asset_signal`: BTC momentum direction (1 = up, -1 = down, 0 = flat, threshold 0.05%)

Tier 5 — Regime/time:
- `hour_sin`, `hour_cos`: sin(2π * hour/24), cos(2π * hour/24)
- `dow_sin`, `dow_cos`: sin(2π * day_of_week/7), cos(2π * day_of_week/7)
- `vol_regime`: 1 if realized_vol_5m > vol_ma_1h else 0
- `adx_14`: Average Directional Index, 14-period

All features must be z-score normalized:
- `feature_normalized = (feature - rolling_mean_500) / rolling_std_500`
- Use a deque of size 500 per feature for rolling stats

**Step 2: Training data collection**

Before we can train, we need data. Add a `DataCollector` mode:
- When `DATA_COLLECTION_MODE=true` env var is set, bot runs normally but logs ALL features + outcome to DynamoDB `training_data` table
- Schema: `{window_id, asset, timeframe, features: {all 40+ features as map}, outcome: 1/0, market_price_at_entry, timestamp}`
- Run in data collection mode for minimum 500 windows per asset before training
- Add a dashboard page showing: data collection progress per asset/timeframe, feature distributions

**Step 3: Model training**

Build a `ModelTrainer` that runs as a scheduled ECS task (every 4 hours):
```
Training pipeline:
1. Load last 5,000 windows from DynamoDB training_data table (per asset+timeframe)
2. Split: 80% train, 20% validation — NO random split, use time-ordered split
3. Apply 5-minute embargo between train and validation sets
4. Train LightGBM binary classifier:
   - objective: binary
   - metric: binary_logloss + auc
   - num_leaves: 31
   - learning_rate: 0.05
   - feature_fraction: 0.8
   - bagging_fraction: 0.8
   - bagging_freq: 5
   - min_child_samples: 20
   - n_estimators: 500 with early stopping (patience=50)
5. Calibrate: fit Platt scaling (LogisticRegression on val probabilities)
   then fit Isotonic Regression on Platt-scaled val probabilities
6. Evaluate on validation: Brier score, AUC, calibration curve
7. Only deploy if new model Brier score < current model Brier score
8. Save model artifact to S3: s3://[bucket]/models/{asset}_{timeframe}_{timestamp}.pkl
9. Update SSM Parameter Store with path to latest model per asset+timeframe
```

Train a separate model per asset+timeframe combination (6 models total: BTC_5m, BTC_15m, ETH_5m, ETH_15m, SOL_5m, SOL_15m).

**Step 4: Model serving**

`ModelServer` class loaded at ECS startup:
- Load latest model artifacts from S3 on startup (check SSM for paths)
- Refresh models every 4 hours (after ModelTrainer runs)
- `predict(asset, timeframe, features) → probability, confidence_interval`
- If model unavailable for an asset: fall back to Bayesian model
- Log prediction + actual outcome to DynamoDB for continuous calibration tracking

**Step 5: New probability blending**

Replace the current 70/30 Bayesian/Bedrock blend:
```python
def compute_final_probability(asset, timeframe, features, market_price, oracle_signal):
    lgbm_prob = model_server.predict(asset, timeframe, features)
    bedrock_prob = bedrock_client.get_probability(asset, timeframe)  # keep for now
    oracle_prob = oracle_signal.black_scholes_probability  # from Phase 5
    
    # Weights: LightGBM 60%, Bedrock 20%, Oracle 20%
    # If oracle_dislocation is active, increase oracle weight
    if oracle_signal.is_dislocated:
        weights = (0.40, 0.10, 0.50)
    else:
        weights = (0.60, 0.20, 0.20)
    
    final_prob = (weights[0] * lgbm_prob + 
                  weights[1] * bedrock_prob + 
                  weights[2] * oracle_prob)
    
    edge = final_prob - market_price
    return final_prob, edge
```

**Tests to add:**
- Test FeatureEngine computes all 40+ features without NaN on first 500 ticks
- Test ModelTrainer loads data, trains, calibrates, and saves artifact
- Test ModelServer loads artifact and returns probability in expected range
- Test blending logic with mocked component probabilities
- Test data collection writes correct schema to DynamoDB

Checkpoint: run in DATA_COLLECTION_MODE for 24 hours. Show me feature distributions and confirm no NaN/inf values before training.

---

## PHASE 7: Entry timing and position sizing upgrade

**Entry timing overhaul:**

Replace the single entry zone (T-60s to T-15s) with a three-tier system:

```
Tier A — Oracle dislocation entry (T-any, fires immediately):
  Condition: oracle_dislocation = True AND edge > 0.05
  Enter immediately regardless of time in window
  This is the primary edge — don't wait

Tier B — Early directional entry (T-240s to T-120s):
  Condition: edge > 0.04 AND lgbm_prob confidence high (model not uncertain)
  Enter when market is at 0.50–0.70, while genuine uncertainty exists
  Use GTD order with expiry at T-30s as safety cancel

Tier C — Late confirmation entry (T-90s to T-30s):
  Condition: edge > 0.06 AND oracle_lag confirms direction
  Replaces current T-60s to T-15s logic
  Only fires if Tiers A and B did not already fire this window
```

For each tier, log `entry_tier` (A/B/C) to DynamoDB so we can analyze which tier generates edge.

**Entry order type by tier:**
- Tier A: FOK (aggressive, take the ask immediately)
- Tier B: GTD limit at mid or 1 tick above bid (passive, auto-cancel at T-30s)
- Tier C: FOK (same as current)

**Position sizing upgrade:**

Keep $1 flat bet for now, but add the infrastructure for dynamic sizing:
```python
def compute_position_size(edge, our_probability, market_price, bankroll):
    # Kelly fraction = edge / (1 - market_price)
    kelly_fraction = edge / (1 - market_price)
    
    # Quarter Kelly with additional uncertainty discount
    # Discount further based on model age (hours since last retrain)
    model_age_hours = get_model_age()
    uncertainty_discount = max(0.1, 1.0 - (model_age_hours / 8.0))
    
    position_fraction = (kelly_fraction * 0.25) * uncertainty_discount
    
    # Floor at $1, cap at 5% of bankroll
    min_bet = 1.0
    max_bet = bankroll * 0.05
    position_usd = max(min_bet, min(max_bet, bankroll * position_fraction))
    
    return round(position_usd, 2)
```

DO NOT increase above $1 bet until we have 400+ resolved trades with positive SPRT.
The compute function should be live but the actual bet remains floored at $1 for now.
Log `kelly_suggested_size` alongside `actual_bet_size` so we can track divergence.

---

## PHASE 8: Edge measurement dashboard

Add a fourth dashboard page: **Edge Analytics**. Update the existing EC2 dashboard.

**Page 4: Edge Analytics — sections:**

Section A — Live SPRT Monitor:
- Chart: log-likelihood ratio over time (one line per asset+timeframe combination)
- Current Λₙ value, upper boundary A, lower boundary B
- Status per pair: "Accumulating data" / "Edge confirmed" / "No edge — reassess"
- Trades remaining to significance (estimated)

Section B — Brier Score Tracker:
- Brier score per model (LightGBM, Bedrock, Oracle, blended)
- Brier Skill Score vs market baseline
- Rolling 50-trade Brier score (trend line)
- Calibration curve: predicted probability vs actual win rate (decile buckets)

Section C — Oracle Lag Monitor (live):
- Current oracle_lag_ms per asset (BTC/ETH/SOL)
- Rolling 5-min mean lag
- Oracle dislocation events last 24h (count, avg edge at entry)
- P&L attributable to oracle entries vs other entries

Section D — Model Performance:
- Feature importance chart (top 15 features from latest LightGBM model)
- Model age (hours since last retrain)
- Validation Brier score at last training
- Data collection progress (windows collected per asset+timeframe)

Section E — Entry Tier Analysis:
- Trades by entry tier (A/B/C) with win rate per tier
- Average edge at entry per tier
- Average entry timing (seconds before close) per tier
- P&L breakdown by tier

Section F — Kelly Sizing Tracker:
- Chart: kelly_suggested_size vs actual_bet_size over time
- When actual bet will unlock from $1 floor (trade count to 400)
- Running bankroll with upper/lower confidence bands

**Implementation:**
- Add DynamoDB `edge_metrics` table: tracks per-trade SPRT log-likelihood, Brier inputs, oracle lag, entry tier
- Dashboard reads from this table
- Update every 30 seconds (same polling interval as other pages)
- Add a "Download CSV" button for the trade log (for offline analysis)

---

## PHASE 9: Smoke test and validation

After all phases complete:

1. Run full test suite — must be ≥193 tests passing (add new tests from phases 5-8)
2. Paper trade for 48 hours with all new components active
3. Verify in dashboard:
   - RTDS feed showing oracle lag values (not null)
   - Features computing without NaN
   - LightGBM model loaded (or "collecting data" if <500 windows)
   - SPRT monitor showing Λₙ updating after each trade
   - Edge Analytics page loading all 6 sections
4. Confirm DynamoDB writes to us-east-1 (not eu-west-1)
5. Confirm Bedrock calls going to us-east-1 (check CloudWatch latency — should be <200ms vs previous >350ms)
6. Then and only then: switch from paper to live mode

Report final latency measurements:
- Coinbase tick → order submission (ms), p50 and p95
- Order submission → fill confirmation (ms), p50 and p95
- Bedrock inference (ms), p50 and p95
- Feature computation time (ms), p50 and p95

---

## Summary of what changes vs current system

| Component | Before | After |
|-----------|--------|-------|
| Entry timing | T-60s to T-15s | Tier A (any time), B (T-240s), C (T-90s) |
| Probability model | 70% Bayesian + 30% Bedrock | 60% LightGBM + 20% Bedrock + 20% Oracle |
| Price feed | Coinbase WebSocket only | Coinbase + Polymarket RTDS (Binance+Chainlink) |
| Edge signal | Price move threshold | Oracle dislocation + calibrated probability |
| Sizing | $1 flat | $1 flat (Kelly computed but not yet applied) |
| AWS region | eu-west-1 | us-east-1 |
| Order signing | Python EIP-712 (~1s) | Python EIP-712 (unchanged for now) |
| Dashboard | 3 pages | 4 pages (+ Edge Analytics) |
| Metrics | P&L only | Brier score, SPRT, oracle lag, entry tier |
