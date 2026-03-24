# Strategy Comparison: Our Bot vs K9

## Quick Reference — Read This First

Our bot trades BTC 5-minute binary options on Polymarket. K9 is the most profitable known competitor on these same markets. This document tracks exactly what K9 does vs what we do, where we match, and where we're losing money.

---

## K9 VERIFIED Facts (from k9_analysis.json — 40 windows, 3,417 trades)

| Metric | K9 Value (verified) | Source |
|--------|---------------------|--------|
| ROI | 13.4% | k9_analysis.json |
| Total windows observed | 40 | 40 windows across BTC/ETH/SOL/XRP × 5m/15m/1h |
| Total trades | 3,417 (3,053 buys + 364 sells) | summary |
| Avg budget per window | $705 | aggregate |
| Avg trades per window | 85.4 | aggregate |
| Buys both sides | 98% of windows (39/40) | verified per_window |
| Guaranteed profit rate | 69% (27/40 windows) | verified per_window |
| GP windows avg combined | 0.857 | calculated |
| Non-GP windows avg combined | 0.988 | calculated |
| Windows with zero sells | 65% (26/40) | verified per_window |
| Windows with sells | 35% (14/40) | verified per_window |
| Sell ratio (sells/buys) | 11.9% (364/3,053) | summary |
| Sells at loss | 61% | sell_pattern |
| Rebuy after sell median | 2 seconds | sell_pattern |
| Rebuy same second | 37% | sell_pattern |
| Rebuy within 5 seconds | 66% | sell_pattern |
| Avg sells per selling-window | 26.0 | calculated |
| Max sells in one window | 52 | verified per_window |
| Avg heavier side % | 70% | aggregate |
| Windows ≤65% heavy side | 40% (16/40) — fairly balanced | verified |
| Windows >80% heavy side | 22% (9/40) — sometimes very one-sided | verified |
| Avg first trade offset | 27 seconds | aggregate |
| Avg trade span | 209 seconds | aggregate |
| Cheap fills (<25c) | 31% of all buys | aggregate |
| Order size median | $3.50 | order_sizes |
| Order size avg | $9.23 | order_sizes |
| Max single order | $129.98 | order_sizes |
| Max orders per second | 33 | clustering |
| Avg batch size | 4.1 orders | clustering |

### K9 Buy Price Distribution (verified)

| Price range | % of buys | Timing | Note |
|-------------|-----------|--------|------|
| 1–9c | 16% | avg T+172s | Lottery tickets, very late window |
| 10–19c | 16% | avg T+149s | Cheap accumulation, mid-late window |
| 20–29c | 10% | avg T+96s | Mid accumulation |
| 30–39c | 11% | avg T+50s | Mid price, early-mid window |
| 40–49c | 18% | avg T+25s | Open baseline |
| 50–59c | 10% | avg T+32s | Open baseline |
| 60–79c | 13% | avg T+90s | Winning side buys |
| 80–99c | 6% | late | Winning side guaranteed return |

**Key insight: 42% of K9's buys are under 20c.** These are the cheap fills that give 5-10x share count per dollar. K9 buys them mid-to-late window when the losing side has collapsed in price.

**K9 also buys ABOVE 60c (19% of buys).** This disproves the "never buy above 50c" rule. K9 buys the winning side at 60-99c for guaranteed returns.

### K9 Per-Market Performance (verified)

| Market | Windows | GP rate | Avg combined | Avg buy $ | Avg sells/window |
|--------|---------|---------|--------------|-----------|-----------------|
| BTC 5m | 8 | 62% | 0.876 | $1,101 | 35.5 |
| SOL 5m | 8 | 62% | 0.930 | $265 | 0.0 |
| XRP 5m | 8 | 75% | 0.928 | $195 | 0.0 |
| BTC 15m | 3 | 67% | 0.897 | $1,487 | 18.3 |
| BTC 1h | 2 | 100% | 0.971 | $2,614 | 0.0 |

**Critical finding: K9 only sells actively on BTC.** SOL, XRP, and hourly windows have zero sells. BTC 5m averages 35.5 sells per window when selling. This means BTC is where the sell-and-recover mechanic matters most.

### K9 Sell Behavior (verified deep dive)

Top 5 highest-sell windows:
| Window | Sells | Trades | Combined | GP? | Buy $ | Heavy side |
|--------|-------|--------|----------|-----|-------|-----------|
| btc-5m-1774205400 | 52 | 226 | 1.029 | No | $1,673 | 61% |
| btc-5m-1774204500 | 51 | 280 | 0.800 | Yes | $1,700 | 60% |
| btc-5m-1774204200 | 50 | 136 | 0.578 | Yes | $647 | 54% |
| btc-5m-1774205700 | 35 | 151 | 1.016 | No | $1,201 | 60% |
| btc-5m-1774205100 | 32 | 165 | 0.809 | Yes | $849 | 55% |

**Pattern: K9 sells most when combined is near 1.00 or above.** The 52-sell window had combined 1.029 (bad). The 50-sell window ended at 0.578 (excellent) — K9 sold aggressively and brought the combined way down.

**K9's sell-and-rebuy cycle is FAST:** 66% of rebuys happen within 5 seconds of the sell. 37% happen in the SAME SECOND. K9 sells at the current bid, then immediately rebuys cheaper. This is continuous capital recycling, not occasional trimming.

## K9's Core Logic (Verified from data)

1. **Buy both sides in 98% of windows** — only 1 window was one-sided
2. **Start trading at T+27 on average** — not at T+5 like us
3. **Trade continuously for 209 seconds** — not just at open
4. **32% of buys are under 20c** — heavy cheap accumulation mid-late window
5. **19% of buys are above 60c** — also buys winning side
6. **65% of windows: zero sells** — most windows are pure accumulate + hold
7. **35% of windows: heavy selling (avg 26 sells)** — when K9 sells, it sells A LOT
8. **Sells at loss 61% of the time** — sells to recover capital, not to lock profit
9. **Rebuys within 2 seconds of selling** — sell-recycle-buy is one atomic operation
10. **Ends with 70% on heavy side on average** — NOT 50/50 balanced. Directional lean is normal.
11. **69% guaranteed profit rate** — combined < 1.00 in most windows
12. **GP combined avg: 0.857** — when it works, the margin is 14.3c per share pair

---

## Our Bot — Current State (Task Def :60, March 24 2026)

| Metric | Our Value | K9 Value (verified) | Gap |
|--------|-----------|---------------------|-----|
| Capital per 5m window | $100 | $705 avg ($265-$3,706) | 7x less |
| Markets traded | BTC_5m only | BTC/SOL/XRP/ETH × 5m/15m/1h | We're single-market |
| Average fills per window | 5–15 | 85.4 | 6-17x fewer fills |
| Trade span | T+5 to T+180 | T+27 to T+236 (209s span) | K9 starts later, trades longer |
| Windows with sells | ~10-20% | 35% (and 26 sells avg when selling) | We sell too rarely, too few per window |
| Sell-and-rebuy speed | 30s cooldown | 2s median (37% same second!) | We're 15x slower |
| Budget actually deployed | $5–70 of $100 | 80%+ of $705 | We often freeze budget |
| Guaranteed profit rate | 39% (11/28 recent) | 69% (27/40) | **30pp gap — biggest issue** |
| Avg combined (GP windows) | ~0.85 | 0.857 | Similar when GP works |
| Position balance | 75% cap | 70% avg heavy side | Similar |
| Cheap fills (<25c) | ~10% | 31% | We miss cheap late fills |
| Hard price cap | 55c | Buys up to 99c (6% above 80c) | **Our 55c cap is too strict** |
| Direction commitment | Lock at T+60 | No explicit lock observed | K9 adapts continuously |
| Pair guard | Favored bypass + 75% cap | None observed | Improving but still restrictive |
| Ladder width | 1-3 price levels | Unknown but 85 trades/window suggests wide | Too narrow |
| Order sizes | 5 shares fixed | $3.50 median, variable | We need variable sizing |

---

## What's Working ✅

| Feature | Status | Since |
|---------|--------|-------|
| Both sides posted at open | ✅ Working | :38 |
| Correct sell token (sells expensive side, not winning side) | ✅ Fixed | :55 |
| BAD_PAIR detection + trim | ✅ Working | :55 |
| UNFAVORED_RICH sell trigger | ✅ Working | :56 |
| Orphan rescue (try to complete missing side) | ✅ Working | :48 |
| Orphan salvage (sell orphan if rescue is bad) | ✅ Working | :50 |
| Direction lock at T+60 (stop chasing model flips) | ✅ New | :57 |
| 75% balance cap | ✅ Working | :58 |
| 55c hard buy cap | ✅ Working | :59 |
| Anti-churn (don't rebuy above last sell price) | ✅ Working | :59 |
| Late dump (sell near-worthless shares before commit) | ✅ Working | :59 |
| Phantom inventory wipe guard | ✅ Fixed | :60 |
| Shutdown cancels GTC orders | ✅ Working | :38 |
| 8-stream data collection (4×5m + 4×1h) | ✅ Working | :52 |
| Model retraining every 4h | ✅ Working | :47 (Claude fix) |
| Deploy script auto-starts + waits for healthy | ✅ Working | :60 |
| Budget scaled to $100 | ✅ Set | :56 |

## What's Broken / Missing ❌ (with K9 data evidence)

| Problem | Impact | K9 evidence | Root Cause |
|---------|--------|-------------|------------|
| **55c hard cap too strict** | Can't buy winning side at 60-80c | K9 buys 19% above 60c, 6% above 80c | Our cap blocks profitable winning-side buys |
| **Not enough sells / too slow** | Capital stuck in losing side | K9 averages 26 sells per selling-window, rebuys in 2s | Our 30s cooldown = 15x slower than K9 |
| **No sell-and-rebuy cycle** | Can't recycle capital fast enough | K9 sells and rebuys in same second (37%) or within 5s (66%) | We sell then wait 30s+ before buying again |
| **Missing cheap late fills** | Only 10% of fills under 25c | K9 gets 31% of fills under 25c, 16% under 10c | We commit at T+250, K9 trades until T+236 on average |
| **Narrow ladder** | Only 1-3 price levels | K9 does 85 trades/window across many levels | Should be 5-8+ levels per side |
| **Single market** | BTC_5m only | K9 trades BTC/SOL/XRP/ETH across 5m/15m/1h simultaneously | Need to add more pairs |
| **GP rate 30pp below K9** | 39% vs 69% guaranteed profit | K9's sell-and-recover collapses net cost | At $100 we can't do the same capital recycling as $705 |
| **Phantom inventory bug** | Shares vanish without sell | Guard added in :60 but root cause not fully identified | Need to monitor for v2_apply_sell_fill_BLOCKED warnings |
| **Direction lock may be wrong** | K9 doesn't lock — adapts continuously | K9 has no explicit direction lock in data | Our T+60 lock may prevent beneficial reallocation |
| **Fixed 5-share order size** | Can't do micro-orders | K9 median order $3.50, at 10c that's 35 shares | We always post 5 shares regardless of price |

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