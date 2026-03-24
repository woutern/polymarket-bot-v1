# Strategy Comparison: Our Bot vs K9

## Quick Reference — Read This First

Our bot trades BTC 5-minute binary options on Polymarket. K9 is the most profitable known competitor on these same markets. This document tracks exactly what K9 does vs what we do, where we match, and where we're losing money.

---

## K9 Facts (from 25-minute data sample, ~4,900 trades)

| Metric | K9 Value |
|--------|----------|
| Capital per 5m window | $1,300–$3,500 |
| Markets traded simultaneously | ~23 (BTC/ETH/SOL/XRP × 5m/15m/1h) |
| Average buy price | $0.43 |
| Average sell price | $0.29 |
| Fills per 5m window | 40–200 |
| Peak buying zone | T+60 to T+120 (52% of volume) |
| Open-phase spend | 12% of budget (small) |
| Windows with zero sells | 56% |
| Windows with sells | 44% |
| Sell volume vs buy volume | 10% (sells are small/sparse) |
| Sell direction | Whichever side is dropping in price |
| Final position balance | 45–55% per side (roughly balanced) |
| Hourly strategy | Zero sells, pure buy-and-hold both sides |
| Order sizes | $0.50–$5 (micro-orders) |
| Trades per minute | ~688 |

## K9's Core Logic (Inferred)

1. **Open small on both sides** (12% of budget)
2. **Let market pick a direction for 60 seconds**
3. **One side gets cheap (30c), other gets expensive (70c)**
4. **Load heavily on the cheap side** — 3-4x more shares per dollar
5. **Occasionally sell the expensive/dropping side** — small batches, recover capital
6. **Keep buying both sides throughout** — never stop mid-window
7. **End roughly balanced** — asymmetric share count but balanced by value
8. **Hold to resolution** — one side pays $1.00 per share

K9's guaranteed-profit mechanic (47% of 5m windows):
- Buy $1,313 total (both sides)
- Sell $1,264 of cheap side back (at a small per-share loss)
- Net cost collapses to ~$50
- Hold 978 UP + 966 DOWN = either side wins ~$920

---

## Our Bot — Current State (Task Def :56, March 24 2026)

| Metric | Our Value | K9 Value | Gap |
|--------|-----------|----------|-----|
| Capital per 5m window | $100 | $1,300–$3,500 | 13-35x less |
| Markets traded | BTC_5m only | ~23 | We're single-market |
| Average fills per window | 5–15 | 40–200 | 3-40x fewer |
| Peak buying zone | T+5 (front-loaded) | T+60–120 | We buy too early |
| Open-phase spend | 10% of budget | 12% | Similar |
| Windows with sells | ~10% | 44% | We sell too rarely |
| Sell direction | Fixed in :55 — now sells expensive side | Drops price side | ✅ Fixed |
| Budget actually deployed | $5–20 of $100 (guard blocks rest) | 80%+ | We freeze our budget |
| Direction commitment | Lock at T+60 | Unclear, likely organic | New in :56 |
| Pair guard | Very strict — blocks most buys | None observed | **#1 problem** |
| Order repricing | Stale-only recycling | Aggressive cancel/rebuild | We're too passive |
| Ladder width | 1-3 price levels | 5-8+ levels | Too narrow |

---

## What's Working ✅

| Feature | Status | Since |
|---------|--------|-------|
| Both sides posted at open | ✅ Working | :38 |
| Correct sell token (sells expensive side, not winning side) | ✅ Fixed | :55 |
| BAD_PAIR detection | ✅ Working | :49 |
| Orphan rescue (try to complete missing side) | ✅ Working | :48 |
| Orphan salvage (sell orphan if rescue is bad) | ✅ Working | :50 |
| Direction lock at T+60 (stop chasing model flips) | ✅ New | :56 |
| UNFAVORED_RICH sell trigger | ✅ New | :56 |
| Shutdown cancels GTC orders | ✅ Working | :38 |
| 8-stream data collection (4×5m + 4×1h) | ✅ Working | :52 |
| Model retraining every 4h | ✅ Working | :47 (Claude fix) |
| Budget scaled to $100 | ✅ Set | :56 |

## What's Broken / Missing ❌

| Problem | Impact | Root Cause |
|---------|--------|------------|
| **Pair guard blocks 90%+ of buys** | Bot sits on $93 unused budget | Guard too strict for any buy that worsens combined_avg |
| **Not enough sells** | Capital stuck in losing side | Sell triggers too conservative |
| **Front-loaded buying** | Pays market price at open instead of waiting for cheap fills | Budget curve ramps too early |
| **Narrow ladder** | Only 1-3 price levels, few resting orders | Should be 5-8 levels per side |
| **No continuous order management** | Stale orders sit far from touch | Should cancel/rebuild aggressively |
| **Model flips cause whipsaw** | Bot buys both sides chasing each flip | Direction lock at T+60 is new, needs validation |

---

## Execution Phases — Current vs Target

### Phase 1: Open (T+5 to T+15)

| | Current | Target (K9-like) |
|---|---------|-------------------|
| Budget | 10% of cap | 10% — similar |
| Posting | bid+1c on both sides | Same |
| Model split | 80/20 to 50/50 based on confidence | Same |
| Issue | None — this works | - |

### Phase 2: Main Deploy (T+15 to T+180)

| | Current | Target (K9-like) |
|---|---------|-------------------|
| Budget curve | Smooth ramp to 82% by T+180 | Same curve, but actually USE it |
| Pair guard | Blocks almost all buys after open | **Remove for favored side** |
| Sell trigger | BAD_PAIR + UNFAVORED_RICH (new) | Sell any unfavored side with avg > 0.55 when model edge > 0.08 |
| Sell frequency | ~0-1 per window | 2-4 per window |
| Ladder | 1-3 levels | 5-8 levels per side |
| Repricing | Stale-order recycling | Cancel any order >2c from touch |
| Direction | Model-driven, locks at T+60 | Same |
| Issue | **Budget frozen by guards** | Should deploy $40-60 by T+180 |

### Phase 3: Buy-Only (T+180 to T+250)

| | Current | Target (K9-like) |
|---|---------|-------------------|
| Sells | None | None — correct |
| Buys | Frozen allocation, passive | Keep buying cheap side |
| Issue | Often idle because budget already blocked | Should still actively buy cheap fills |

### Phase 4: Commit/Hold (T+250 to T+300)

| | Current | Target (K9-like) |
|---|---------|-------------------|
| Cancel unfilled | ✅ Yes | Same |
| Hold to resolution | ✅ Yes | Same |
| Issue | None — this works | - |

---

## Sell Triggers — Current Implementation

| Trigger | Condition | When | Status |
|---------|-----------|------|--------|
| PAYOUT_FLOOR | Excess shares above payout floor + bid > hold value | T+45 to T+180 | ✅ Works but rarely fires (positions too small) |
| BAD_PAIR | Combined > max+0.02, cost_above_floor > max+0.15, or EV < -0.05 | T+20 to T+180 | ✅ Fixed in :55 (correct side now) |
| UNFAVORED_RICH | Model edge >= 0.10, unfavored side avg > 0.55, bid > 0.10 | T+45 to T+180 | ✅ New in :56 |
| ORPHAN_SALVAGE | Orphan leg + rescue would make bad pair + bid >= 0.30 | T+20 to T+180 | ✅ Works |

### Missing sell triggers we need:
- **REBALANCE_CONTINUOUS**: if unfavored side has >20% more shares than favored, trim 5 shares every 30s
- **EXPENSIVE_TRIM**: if any side's avg > 0.65 and total_shares > 20, sell 5 shares to lower avg

---

## Model Accuracy (Measured March 24, 2026)

**NOTE: This is measured on training data, not out-of-sample. Real live accuracy may be lower.**

### BTC_5m (8,467 valid rows, AUC 0.689)

| Confidence Bucket | n | Accuracy | Actual UP% | Calibration |
|-------------------|------|----------|------------|-------------|
| Very Strong DOWN (<0.30) | 3,580 | 65.8% | 34.2% | BAD — predicts 16%, actual 34% |
| Strong DOWN (0.30-0.40) | 368 | 53.0% | 47.0% | BAD — predicts 34%, actual 47% |
| Weak DOWN (0.45-0.50) | 293 | 48.1% | 51.9% | OK |
| Weak UP (0.50-0.55) | 1,109 | 55.6% | 55.6% | GOOD |
| Moderate UP (0.55-0.60) | 583 | 55.7% | 55.7% | GOOD |
| Strong UP (0.60-0.70) | 75 | 60.0% | 60.0% | OK |
| Very Strong UP (>0.70) | 2,395 | 70.6% | 70.6% | BAD — predicts 91%, actual 71% |

**Overall direction accuracy: 63.7%** (vs 50.4% base rate)
**Raw price-move baseline: 63.9%** (model barely beats raw price move)

Key findings:
- Model is **overconfident** at extremes: predicts 91% when actual is 71%, predicts 16% when actual is 34%
- The 0.50-0.60 range is **well calibrated** — predictions match reality
- Strong signals (>0.70 or <0.30) are directionally correct but probabilities are wrong
- **For our strategy this means**: trust the direction, don't trust the magnitude

### ETH_5m (8,644 valid rows, AUC 0.700)

| Confidence Bucket | n | Accuracy | Actual UP% | Calibration |
|-------------------|------|----------|------------|-------------|
| Very Strong DOWN (<0.30) | 901 | 80.4% | 19.6% | GOOD |
| Strong DOWN (0.30-0.40) | 1,083 | 69.1% | 30.9% | GOOD |
| Weak UP (0.50-0.55) | 2,495 | 52.5% | 52.5% | GOOD |
| Very Strong UP (>0.70) | 1,694 | 76.9% | 76.9% | GOOD |

**Overall direction accuracy: 63.9%** — **Best calibrated model of all three**

### SOL_5m (8,649 valid rows, AUC 0.697)

| Confidence Bucket | n | Accuracy | Actual UP% | Calibration |
|-------------------|------|----------|------------|-------------|
| Very Strong DOWN (<0.30) | 2,454 | 69.1% | 30.9% | BAD — predicts 16%, actual 31% |
| Very Strong UP (>0.70) | 3,324 | 69.9% | 69.9% | BAD — predicts 91%, actual 70% |

**Overall direction accuracy: 64.1%** — Similar accuracy but badly calibrated at extremes

### What This Means For Trading

1. **Direction is useful** — all three models beat coin-flip by ~14 percentage points
2. **Probability magnitudes are NOT trustworthy** — especially BTC and SOL at extremes
3. **ETH model is best calibrated** — predictions actually match outcomes
4. **For bet sizing**: use direction (UP vs DOWN) but don't scale position size linearly with prob_up
5. **The 0.50-0.55 zone is real edge** — both BTC and ETH show 55-56% actual UP rate here
6. **Model barely beats raw price move** — 63.7% vs 63.9% for BTC. The model's value is mainly in combining multiple features, not a huge alpha over simple momentum

### Prediction Distribution (where does the model spend its time?)

| Range | BTC | ETH | SOL |
|-------|-----|-----|-----|
| <0.30 (strong DOWN) | 42.3% | 10.4% | 28.4% |
| 0.30-0.50 (mild DOWN) | 8.6% | 34.1% | 22.0% |
| 0.50-0.60 (mild UP) | 20.0% | 34.5% | 5.8% |
| >0.60 (strong UP) | 29.2% | 21.0% | 43.9% |

BTC and SOL models are bimodal — they strongly predict UP or strongly predict DOWN. ETH is more evenly distributed. This means BTC/SOL will take larger directional bets more often.

---

## P&L Summary (March 24, 2026)

Approximate losses from today's live windows:
- :54 wrong-side sell (UP sold instead of DOWN): ~$2.50 loss
- :55 model flips / too much DOWN bought: ~$11 loss  
- Multiple small bad-pair windows at 5/5 combined > 1.00: ~$3-5 loss
- Overnight (70 windows, mostly 5/5 tiny): ~$0-5 net (mixed)

**Estimated total today: -$15 to -$20**

Root causes of all losses:
1. Selling the wrong side (fixed in :55)
2. Model chasing / no direction commitment (fixed in :56)
3. Pair guard blocking capital deployment (being fixed now)
4. Not enough sells to recover capital from losing side

---

## Per-Pair Strategy Profiles

Each pair gets its own profile based on model quality. The execution engine is shared,
but thresholds, sizing, and sell behavior differ per pair.

### Profile: BTC_5m — "Directional with hedge"

| Parameter | Value | Reason |
|-----------|-------|--------|
| Model accuracy | 63.7% overall, 65.8% on strong DOWN, 70.6% on strong UP | Good directional signal |
| Calibration | BAD at extremes (predicts 91%, actual 71%) | Trust direction, not magnitude |
| Budget | $100 per window | Primary pair |
| Open size | 10% of budget | Small open, deploy mid-window |
| Direction lock | T+60 | Stop chasing model flips |
| Favored-side guard | NONE when edge >= 0.08 | Let the bot deploy capital |
| Unfavored-side guard | Loose (combined+0.02, cost+0.50) | Don't block hedge entirely |
| Sell triggers | PAYOUT_FLOOR, BAD_PAIR, UNFAVORED_RICH | All three active |
| Sell cooldown | 30s | Sparse but real |
| Confidence scaling | Clamp: treat >0.70 as 0.70, treat <0.30 as 0.30 | Don't overtrust extreme probs |
| Weak signal behavior | Stay tiny ($5-10) when prob 0.45-0.55 | Model has no real edge here |
| Strong signal behavior | Deploy $50-80, lean 70/30 favored | Direction is right ~66-71% |

### Profile: ETH_5m — "Best model, balanced approach"

| Parameter | Value | Reason |
|-----------|-------|--------|
| Model accuracy | 63.9% overall, 80.4% on strong DOWN, 76.9% on strong UP | Best of all three |
| Calibration | GOOD across all buckets | Can trust both direction AND magnitude |
| Budget | $50 per window (start conservative) | Second pair, prove first |
| Open size | 10% of budget | Same as BTC |
| Direction lock | T+60 | Same |
| Favored-side guard | NONE when edge >= 0.08 | Same |
| Sell triggers | All three | Same |
| Confidence scaling | Use raw model output (calibration is good) | ETH model is trustworthy |
| Weak signal behavior | Stay tiny | Same |
| Strong signal behavior | Can lean harder (80/20) because calibration supports it | Model says 77% UP → actual is 77% |

### Profile: SOL_5m — "Volatile, needs caution"

| Parameter | Value | Reason |
|-----------|-------|--------|
| Model accuracy | 64.1% overall, 69.1% on strong DOWN, 69.9% on strong UP | Similar to BTC |
| Calibration | BAD at extremes (predicts 91%, actual 70%) | Same issue as BTC |
| Budget | $50 per window (start conservative) | SOL is more volatile |
| Open size | 8% of budget | Smaller open — SOL moves fast |
| Direction lock | T+45 | Lock earlier — SOL trends faster |
| Favored-side guard | NONE when edge >= 0.10 | Stricter — SOL punishes mistakes harder |
| Unfavored-side guard | Tight (combined+0.01, cost+0.25) | More conservative |
| Sell triggers | All three, but UNFAVORED_RICH at avg > 0.50 (not 0.55) | Sell earlier |
| Sell cooldown | 20s | More frequent sells — SOL needs faster recycling |
| Confidence scaling | Clamp: treat >0.70 as 0.65 | Even less trust in extreme probs |
| Rich-side cap | 0.70 (not 0.82) | Stricter — don't pay up on SOL |

### Profile: XRP_5m — "Not ready"

| Parameter | Value | Reason |
|-----------|-------|--------|
| Model | None — zero training rows, collecting now | Need ~500 rows minimum |
| Status | Watch-only data collection | Don't trade until model exists |
| Estimated ready | ~48h from now (12 windows/hour × 48h = 576 rows) | Then train + validate |

### Profile: BTC_1h — "To be built"

| Parameter | Value | Reason |
|-----------|-------|--------|
| Model | None — zero 1h training rows in DynamoDB | Collection just started |
| Historical data | 129,566 1-min candles (89 days) → ~2,159 potential 1h windows | Can build from candle data |
| K9 behavior | Zero sells, pure buy-and-hold both sides | Much simpler than 5m |
| Strategy | No selling, continuous two-sided accumulation, hold to resolution | Prices bounce more in 1h |
| Budget curve | Very gradual — deploy over 55 minutes | No rush |
| Estimated ready | Need to build training pipeline from candle data first | 1-2 days of work |

### Profile: ETH_1h / SOL_1h / XRP_1h — "To be built"

| Parameter | Value | Reason |
|-----------|-------|--------|
| Historical data | ETH/SOL: ~719 potential 1h windows, XRP: none | Less data than BTC |
| Strategy | Same as BTC_1h — no selling, accumulate, hold | K9 pattern |
| Priority | After BTC_1h is validated | Don't spread too thin |

---

## Rollout Order

1. ✅ BTC_5m — live now, optimizing
2. **ETH_5m** — best model calibration, add next with $50 budget
3. **SOL_5m** — similar accuracy but needs stricter profile, add after ETH
4. **BTC_1h** — build training pipeline from candle data, then validate
5. **XRP_5m** — wait for data accumulation (~48h), then train model
6. **ETH_1h / SOL_1h** — after BTC_1h is validated
7. **XRP_1h** — last, needs both XRP data and 1h pipeline

---

## Next Steps — Priority Order

1. **Monitor BTC_5m :57** — verify pair guard fix works, sells fire correctly
2. **Build per-pair profile config** — so each pair uses its own thresholds
3. **Enable ETH_5m** — with conservative $50 budget, good calibration
4. **Build 1h training pipeline** — convert candle parquets to labeled windows
5. **Train BTC_1h model** — validate accuracy before trading
6. **Add continuous rebalance sell** — trim unfavored side every 30s when imbalanced
7. **Widen ladder** — 5-6 price levels per side instead of 1-3
8. **Aggressive order repricing** — cancel any order >2c from current bid

---

## Key Metrics to Watch Per Window

| Metric | Bad | OK | Good | K9-like |
|--------|-----|----|----- |---------|
| Budget deployed | <$10 | $10-30 | $30-60 | $60-80 |
| Combined avg | >1.05 | 1.00-1.05 | 0.95-1.00 | <0.95 |
| Sell count | 0 | 1 | 2-4 | 2-10 |
| Final balance (UP/DOWN ratio) | >3:1 | 2:1 | 1.5:1 | ~1:1 |
| pair_guard_skipped | >50% of ticks | 20-50% | <20% | ~0% |
| Payout floor vs net cost | floor < cost | floor ≈ cost | floor > cost | floor >> cost |