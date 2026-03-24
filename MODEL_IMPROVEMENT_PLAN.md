# Model Improvement Plan — Polymarket Trading Bot

## Current State (March 24, 2026)

### What we have

| Pair | Training rows | AUC | Direction accuracy | Calibration |
|------|--------------|-----|-------------------|-------------|
| BTC_5m | 17,504 | 0.737 | 63.7% | BAD at extremes |
| ETH_5m | 17,804 | 0.706 | 63.9% | GOOD everywhere |
| SOL_5m | 9,289 | 0.771 | 64.1% | BAD at extremes |
| XRP_5m | ~10 | — | — | Collecting |
| All 1h | 0 | — | — | Collecting |

### Current features (14)

1. `move_pct_15s` — price change in first 15 seconds
2. `realized_vol_5m` — realized volatility over the window
3. `vol_ratio` — current vol / rolling average vol
4. `body_ratio` — candle body vs range
5. `prev_window_direction` — did previous window go up or down
6. `prev_window_move_pct` — how much did previous window move
7. `hour_sin` / `hour_cos` — time of day (cyclical)
8. `dow_sin` / `dow_cos` — day of week (cyclical)
9. `signal_move_pct` — absolute move at entry
10. `signal_ask_price` — yes ask at window open
11. `signal_seconds` — seconds since open at first significant move
12. `signal_ev` — estimated EV at entry time

### Key finding: model barely beats raw price move

- Raw `move_pct_15s` direction accuracy: **63.9%**
- Full 14-feature LightGBM accuracy: **63.7%**
- The model adds almost nothing over just looking at the 15-second price move
- This means: either the other 13 features are noise, or they're not computed well

### Calibration problem

BTC and SOL models are badly calibrated at extremes:
- Model outputs prob=0.91 when actual rate is 0.71 (overconfident by 20pp)
- Model outputs prob=0.16 when actual rate is 0.34 (overconfident by 18pp)
- ETH model is well calibrated across all buckets

**Why this happens:**
- Platt scaling + Isotonic regression are fitted on the same 20% validation split
- With only ~1,700 validation rows, the isotonic regression memorizes the calibration curve
- The isotonic step function overfits — it maps narrow raw-probability bands to extreme outputs
- More data (50K+ rows) would smooth this naturally, but we can also fix the method

**What we need:**
- 50K+ training rows per pair (currently 8-17K valid)
- Simpler calibration that doesn't overfit (temperature scaling = 1 parameter vs isotonic = N parameters)
- Or: calibrate on a separate holdout set, not the validation set used for AUC measurement

This matters because position sizing depends on model probabilities.

---

## Research Areas (Priority Order)

### 1. Multi-timeframe features (HIGH IMPACT, LOW EFFORT)

**Idea:** Use hourly trend as context for 5m predictions. Use daily trend as context for 1h predictions.

**Why this should help:**
- A 5m window during a strong hourly uptrend behaves differently than during a downtrend
- K9 likely has this context — they trade across all timeframes simultaneously
- The current model has zero awareness of the larger trend

**Features to add for 5m model:**
- `hourly_direction` — is the current 1h window trending UP or DOWN?
- `hourly_move_pct` — how much has the current hourly candle moved?
- `hourly_position_in_range` — where is current price relative to hourly high/low? (0.0 = at low, 1.0 = at high)
- `multi_window_momentum_3` — direction consistency of last 3 five-minute windows (all same = strong, mixed = choppy)
- `multi_window_momentum_6` — same but last 6 windows (30 minutes)

**Features to add for 1h model:**
- `daily_direction` — is the 24h trend up or down?
- `daily_move_pct` — 24h price change
- `hourly_momentum_3` — last 3 hours same direction?
- `hourly_momentum_6` — last 6 hours

**Data source:** We already track hourly states in the bot loop. Just need to pass them as features.

**Effort:** Small code change in feature builder + retrain

### 2. Orderbook features (HIGH IMPACT, MEDIUM EFFORT)

**Idea:** Use Polymarket orderbook state to predict direction and improve execution.

**Why this should help:**
- Orderbook imbalance (more bids than asks, or vice versa) is a known predictor of short-term direction
- The market knows things our model doesn't — orderbook reflects that information
- We already fetch the orderbook every tick but only use best bid/ask

**Features to add:**
- `yes_bid_depth` — total size at top 3 YES bid levels
- `no_bid_depth` — total size at top 3 NO bid levels
- `bid_imbalance` — (yes_bid_depth - no_bid_depth) / (yes_bid_depth + no_bid_depth)
- `yes_spread` — yes_ask - yes_bid (tighter = more liquid = stronger conviction)
- `no_spread` — no_ask - no_bid
- `mid_price_vs_model` — (yes_bid + yes_ask) / 2 vs model's prob_up (market disagrees with model?)

**For execution (not model, but timing):**
- Don't post orders when spread is very wide (low liquidity)
- Post more aggressively when our side has thin asks (easy to fill)
- Cancel orders faster when opposing depth builds up

**Data source:** Already collected in `_refresh_orderbook()`. Some depth features already exist in the OrderbookSnapshot but aren't fed to the model.

**Effort:** Medium — need to add features to training data collection, retrain, and wire into the model.

### 3. Coinbase L2 / trade flow features (HIGH IMPACT, MEDIUM EFFORT)

**Idea:** Use real-time Coinbase order flow to predict the 5-minute direction.

**Why this should help:**
- Coinbase price drives Polymarket prices (the oracle is Coinbase-based)
- Large buys/sells on Coinbase appear 200-800ms before Polymarket reprices
- This is the "latency arbitrage" edge from the original strategy thesis
- OFI (Order Flow Imbalance) at T+2s and T+8s was already designed as a feature but shows as 0 in training data

**Features to add:**
- `ofi_2s` — order flow imbalance in first 2 seconds (net buy vs sell pressure)
- `ofi_8s` — same at 8 seconds
- `trade_arrival_rate` — trades per second on Coinbase (high = volatile event)
- `large_trade_direction` — did a large trade (>$50K) just hit? Which direction?
- `coinbase_spread` — current bid-ask spread on Coinbase (tight = calm, wide = volatile)
- `coinbase_mid_change_1s` — mid price change in last 1 second (momentum)

**Data source:** CoinbaseWS already connected. Need to compute and store these per tick.

**Effort:** Medium — WebSocket already connected, need aggregation logic + training pipeline.

### 4. Cross-asset features (MEDIUM IMPACT, LOW EFFORT)

**Idea:** Use BTC movement to predict ETH/SOL, and vice versa.

**Why this should help:**
- Crypto assets are highly correlated
- BTC often leads ETH/SOL by seconds to minutes
- If BTC dumps 0.5% in 15 seconds, SOL is likely to follow

**Features to add:**
- `btc_move_pct_15s` — BTC price move (for ETH/SOL models)
- `btc_move_pct_60s` — BTC 1-minute move
- `btc_confirms_direction` — does BTC agree with the 5m asset's direction?
- `eth_move_pct_15s` — ETH move (for BTC/SOL models)
- `correlation_30m` — rolling 30-minute correlation between BTC and this asset

**Data source:** Already have Coinbase prices for all assets. Just need to cross-reference.

**Effort:** Low — prices already available, just compute and add to features.

### 5. Recalibration (MEDIUM IMPACT, LOW EFFORT)

**Idea:** Fix the overconfident model outputs at extremes.

**Why this should help:**
- BTC model says 91% when actual is 71% — position sizing is wrong
- If we clamp or recalibrate, the allocation split becomes more accurate
- ETH model is already well calibrated, so this mainly helps BTC and SOL

**Root cause:** Isotonic regression overfits calibration on small validation sets.
With 1,700 validation rows the isotonic step function creates extreme mappings
that don't generalize. This is a known problem in ML calibration literature.

**Approaches (from simplest to best):**
1. **Simple clamp:** Cap model output between 0.25 and 0.75 (5 min, hacky but works)
2. **Temperature scaling:** Single parameter T that softens: `p_calibrated = sigmoid(logit(p_raw) / T)`. Only 1 parameter so it can't overfit. Standard in deep learning.
3. **Platt scaling on separate holdout:** Split data 60/20/20 instead of 80/20. Train on 60%, validate on 20%, calibrate on the other 20%.
4. **Venn-Abers calibration:** Distribution-free method that gives calibrated probability intervals. More robust than isotonic.
5. **More data:** At 50K+ rows per pair, even isotonic works well. Fastest path is to collect more windows + potentially backfill from historical Polymarket data.

**Real fix = more data + simpler calibration method.** Temperature scaling with 50K rows would solve this completely.

**Effort:** Temperature scaling = 1 hour. More data = ongoing collection.

### 6. Training data quality (MEDIUM IMPACT, MEDIUM EFFORT)

**Idea:** Improve what goes into the training pipeline.

**Current issues:**
- ~50% of training rows have `signal_move_pct=0` and `signal_ev=0` (these are placeholder values from the data collection code, not real measurements)
- Jon-Becker base data (22K rows) may be from a different market regime
- No time weighting — a row from 3 months ago counts the same as yesterday

**Improvements:**
1. **Fix signal features:** Actually compute signal_move_pct and signal_ev during live collection instead of defaulting to 0
2. **Time weighting:** Give 2x weight to rows from the last 7 days, 1x to 7-30 days, 0.5x to older
3. **Regime filtering:** Drop training rows from very different market conditions (e.g., major crash days)
4. **Purge look-ahead leakage:** Verify no features use information from after the 15-second mark
5. **More granular outcomes:** Instead of binary UP/DOWN, also track magnitude — a 0.01% move is different from a 0.5% move

### 7. Alternative model architectures (MEDIUM-HIGH PRIORITY)

The current LightGBM is a good start but there are many proven alternatives for this type of problem:

**Tree-based (most proven for tabular financial data):**
- **XGBoost** — often slightly better than LightGBM, slower but more accurate. Drop-in replacement.
- **CatBoost** — handles categorical features natively, often better calibrated out of the box. Good for time-of-day/day-of-week features.
- **Random Forest** — more stable/less overfit than boosting. Good for ensemble diversity.

**Neural networks:**
- **TabNet** — Google's attention-based model for tabular data. Can learn feature interactions LightGBM misses.
- **MLP (2-3 hidden layers)** — simple feedforward net. Works surprisingly well on small tabular datasets with proper regularization.
- **Temporal Fusion Transformer (TFT)** — designed for time series with known/unknown inputs. Overkill for now but state-of-the-art for multi-horizon forecasting.
- **1D-CNN on price sequence** — feed raw 15-second tick series instead of hand-crafted features. Learns its own features.
- **LSTM / GRU** — recurrent nets that can learn from sequences of windows (not just one window's features). "This window follows 3 UP windows" type patterns.

**Ensemble methods (highest expected improvement):**
- **Stacking:** Train LightGBM, XGBoost, CatBoost, MLP separately. Then train a meta-model on their combined predictions. Typically +2-5pp accuracy over best single model.
- **Simple average:** Average 3-4 model predictions. Reduces variance, improves calibration naturally.
- **Weighted average:** Weight models by their recent accuracy. Models that are hot get more weight.
- **Model + rule hybrid:** Average model prediction with simple momentum signal (if price moved UP in first 15s, bias toward UP). The momentum signal is uncorrelated with model errors.

**Online / adaptive learning:**
- **Online gradient boosting:** Update tree ensemble incrementally with each new window instead of batch retrain every 4h. Adapts to regime changes faster.
- **Bayesian online learning:** Maintain a probability distribution over model parameters. Update with each observation. Natural uncertainty estimation.
- **Regime detection + model switching:** Detect trending vs ranging markets (e.g., using realized vol or Hurst exponent). Use different models for each regime.

**Reinforcement learning (long-term):**
- **Contextual bandits:** Treat each window as a decision: how much to allocate UP vs DOWN? Learn the policy directly from outcomes instead of predicting direction first.
- **PPO/A2C on the full trading loop:** End-to-end RL that learns when to buy, sell, how much, at what price. Very hard to train but potentially optimal. Would need 100K+ simulated windows.

**Probabilistic / uncertainty-aware models:**
- **Bayesian Neural Network:** Outputs a distribution, not a point estimate. Naturally calibrated.
- **Gaussian Process:** Good uncertainty estimates on small datasets. Expensive for >10K rows.
- **Conformal prediction:** Wraps any model to produce calibrated prediction intervals with guaranteed coverage.
- **Quantile regression:** Instead of predicting mean direction, predict the 10th/50th/90th percentile of the price move. Better for sizing.

**Effort:** Each model type is 1-3 days to implement. Ensemble of 3-4 models is 1 week. RL is 2-4 weeks.

**Recommended next model steps:**
1. Add XGBoost + CatBoost alongside LightGBM (3 models, 2 days)
2. Simple average ensemble of all 3 (1 hour)
3. Measure: if ensemble beats best single model by 1%+ on validation, deploy it
4. Then add MLP as 4th ensemble member
5. Then explore regime detection + model switching

### 8. External data sources (MEDIUM PRIORITY)

**Free, no API key needed:**
- **Binance funding rate** — already partially wired (`liq_cluster_bias`). Positive = crowded longs = bearish squeeze risk. Very predictive for 5m crypto.
- **Binance open interest changes** — sudden OI spike = new positions opening = potential big move. Free via `fapi.binance.com/futures/data/openInterestHist`.
- **Binance long/short ratio** — already fetching this. When longs are crowded, shorts squeeze. Vice versa.
- **Binance liquidation data** — cascade of liquidations = forced selling = predictable price impact. Free via WebSocket.
- **CoinGecko / CoinMarketCap** — 24h volume, market cap changes. Free tier.
- **Google Trends** — search volume for "bitcoin" spikes before retail FOMO moves. Free but slow (daily).

**Paid / API key needed:**
- **Deribit options data** — BTC/ETH implied volatility, put/call ratio, max pain. Strong predictor of expected move size. API key free for basic.
- **Glassnode on-chain metrics** — exchange inflows/outflows (big deposit to exchange = about to sell). Paid.
- **Santiment social sentiment** — Twitter/Reddit/Telegram crypto sentiment aggregated. Paid.
- **Kaiko / CryptoCompare** — institutional-grade orderbook data across exchanges. Paid.
- **Alternative.me Fear & Greed Index** — free, daily update. Macro sentiment.

**Polymarket-specific data:**
- **Competitor activity tracking** — monitor K9 and other top traders' positions in real-time via Polymarket activity API. We partially have this (shadow tracker). Knowing what K9 is doing in the current window is extremely valuable.
- **Historical Polymarket orderbook snapshots** — if we can get historical orderbook data for past 5m windows, we can train on actual Polymarket microstructure, not just Coinbase prices.
- **Resolution patterns** — how often does the "obvious winner" at T+120 actually win? Are there common reversal patterns near window close?

**Effort:** Binance features = 1 day (already partially wired). Deribit = 2 days. Social sentiment = 3-5 days. On-chain = 1 week+.

---

## Implementation Plan

### Phase 1: Quick wins (1-2 days)

1. **Temperature scaling for BTC/SOL calibration**
   - File: `src/polybot/ml/server.py` (apply in predict())
   - Method: fit single parameter T on validation set, apply `sigmoid(logit(p)/T)`
   - Expected: calibration gap drops from 0.18 to <0.05

2. **Fix broken signal features** — stop writing 0s for signal_move_pct and signal_ev
   - File: `src/polybot/core/loop.py` (_on_window_close training data section)
   - Expected: 2-4 features become useful instead of noise

3. **Add cross-asset features** — BTC move as feature for ETH/SOL
   - File: `src/polybot/core/loop.py` (feature builder) + `src/polybot/ml/trainer.py` (FEATURE_COLUMNS)
   - Expected: 1-3pp accuracy boost for ETH/SOL models

4. **Add time weighting** to training — recent data weighted 2x
   - File: `src/polybot/ml/trainer.py`
   - Expected: model adapts faster to current market regime

### Phase 2: Multi-timeframe + ensemble (3-7 days)

5. **Add hourly context features** for 5m model
   - `hourly_direction`, `hourly_move_pct`, `multi_window_momentum_3`
   - File: `src/polybot/core/loop.py` + `src/polybot/ml/trainer.py`
   
6. **Build 1h training pipeline** from S3 candle data
   - Convert 129K BTC 1-min candles → ~2,159 labeled hourly windows
   - New script: `scripts/build_hourly_training.py`

7. **Add XGBoost + CatBoost** alongside LightGBM
   - File: `src/polybot/ml/trainer.py` (train 3 models per pair)
   - File: `src/polybot/ml/server.py` (serve ensemble prediction)

8. **Simple ensemble** — average of 3 model predictions
   - Expected: +2-3pp accuracy over best single model

### Phase 3: Orderbook + Coinbase flow (1-2 weeks)

9. **Add Polymarket orderbook features to model**
   - `bid_imbalance`, `yes_spread`, `no_spread`, `mid_price_vs_model`
   - Collect in training data, retrain after ~2000 rows accumulated

10. **Add Coinbase L2 features**
    - `ofi_2s`, `ofi_8s`, `trade_arrival_rate`, `coinbase_spread`
    - Fix existing OFI computation that's stuck at 0

11. **Add Binance features**
    - Funding rate changes, OI changes, liquidation events
    - Already partially wired — complete the pipeline

### Phase 4: Advanced models (2-4 weeks)

12. **1D-CNN on raw tick sequence** — learn features from raw data instead of hand-crafting
13. **LSTM/GRU on window sequences** — "3 UP windows in a row" patterns
14. **Regime detection** — trending vs ranging classifier, switch model/strategy per regime
15. **Stacking meta-learner** — train a model on top of the ensemble predictions

### Phase 5: Long-term (1-2 months)

16. **Contextual bandit** — learn allocation policy directly from outcomes
17. **Competitor shadow features** — what is K9 doing right now as a model input
18. **Historical Polymarket orderbook** — train on actual PM microstructure
19. **Deribit implied vol** — options market knows expected move size

---

## How to Measure Improvement

For each change, measure these on held-out validation data (last 20% by time):

| Metric | Current BTC | Target |
|--------|-------------|--------|
| Direction accuracy | 63.7% | 68%+ |
| AUC | 0.689 | 0.75+ |
| Brier score | 0.2502 | 0.220 |
| Calibration gap (extremes) | 0.18-0.21 | <0.05 |
| Strong signal accuracy (>0.65 or <0.35) | 66-71% | 73%+ |

**Rule: only deploy a model change if it improves AUC by >= 0.01 on validation data.**

---

## Data Assets Available

| Source | Location | Size | Useful for |
|--------|----------|------|-----------|
| Jon-Becker 5m base | S3 `polymarket-bot-training-data-688567279867/jon-becker/` | 22K windows BTC+SOL | Base training |
| Live DynamoDB rows | `polymarket-bot-training-data` table | 17.5K BTC, 17.8K ETH, 9.3K SOL | Recent data |
| Coinbase 1-min candles | S3 `polymarket-bot-data-688567279867-euw1/candles/` | 129K BTC (89 days), 43K ETH/SOL (29 days) | Build 1h training data |
| Coinbase WebSocket | Live connection in bot | Real-time | L2 features, OFI |
| Polymarket orderbook | Fetched every tick via REST | Real-time | Orderbook features |
| Binance futures | `fapi.binance.com` (free, no key) | Real-time | Funding rate, long/short ratio |

---

## Key Insight

The model's direction accuracy (64%) is already enough for the current strategy to be profitable
IF the execution is right (buy cheap, sell expensive, balance position). Improving the model
to 68-70% would roughly double the expected profit per window.

**Impact of accuracy on profit (at $100/window, $2 avg profit on correct direction):**

| Accuracy | Win rate | Expected profit per window | Daily (288 windows) |
|----------|----------|---------------------------|---------------------|
| 55% | Break-even | ~$0 | ~$0 |
| 60% | Slight edge | ~$0.80 | ~$230 |
| 64% (current) | Decent | ~$1.60 | ~$460 |
| 68% | Good | ~$2.80 | ~$810 |
| 72% | Strong | ~$4.20 | ~$1,210 |
| 75% | Excellent | ~$5.50 | ~$1,580 |

Every 1pp of accuracy improvement ≈ ~$115/day at current scale.

**The fastest path to better accuracy:**
1. Fix broken features (signal_move_pct stuck at 0) — removes noise
2. Temperature scaling calibration — fixes position sizing
3. Add multi-timeframe context (hourly trend for 5m) — new information
4. Add orderbook imbalance — market intelligence the model currently lacks
5. XGBoost + CatBoost ensemble — diversity reduces variance

These five together could plausibly push accuracy from 64% to 70-72%.

**The path to 75%+ accuracy (longer term):**
- Regime-aware models (trending vs ranging)
- Coinbase L2 order flow (200-800ms latency edge)
- Competitor activity as features (what is K9 doing?)
- More training data (50K+ rows per pair for stable calibration)