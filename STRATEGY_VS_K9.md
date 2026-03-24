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

## Next Steps — Priority Order

1. **Remove pair guard for favored-side buys** — in progress
2. **Validate with full simulation suite** — UP, DOWN, RANGE, model-flip
3. **Deploy once, monitor 3 windows carefully**
4. **Add continuous rebalance sell** — trim unfavored side every 30s when imbalanced
5. **Widen ladder** — 5-6 price levels per side instead of 1-3
6. **Aggressive order repricing** — cancel any order >2c from current bid
7. **Add SOL_5m** — same framework, different profile
8. **Add ETH_5m, XRP_5m** — as models become ready

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