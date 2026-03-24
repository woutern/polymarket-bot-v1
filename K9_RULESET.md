# K9 Ruleset — Definitive Trading Rules from All Learnings

This file is the SINGLE SOURCE OF TRUTH for how the bot should behave.
Every rule here comes from either K9 verified data or a real live failure we experienced.
Before making ANY code change, check it against this ruleset.

---

## K9 Verified Facts (from k9_analysis.json — 40 windows, 3,417 trades)

### 5m Windows Only (24 windows observed)
- GP rate: 67% (16/24 windows had combined < 1.00)
- GP combined avg: 0.857
- Non-GP combined avg: 0.988
- Both sides bought: 98% of windows
- Average budget deployed: $705 per window
- Average trades per window: 85.4
- Average heavy side: 70% (range: 50%–100%)
- Zero-sell windows: 65% (16/24)
- Selling windows: 35% (8/24) — ALL on BTC, zero sells on SOL/XRP
- Average sells per selling window: 35.5
- Max sells in one window: 52
- Sells at loss: 61%
- Rebuy after sell: median 2 seconds, 37% same second, 66% within 5 seconds
- First trade offset: avg 27 seconds (not at T+5)
- Trade span: avg 209 seconds (T+27 to T+236)

### K9 Buy Price Distribution
| Range | % of buys | Avg timing | Interpretation |
|-------|-----------|-----------|----------------|
| 1–9c | 16% | T+172 | Lottery on dying side, very late |
| 10–19c | 16% | T+149 | Cheap accumulation, mid-late |
| 20–29c | 10% | T+96 | Mid accumulation |
| 30–39c | 11% | T+50 | Mid price, early-mid |
| 40–49c | 18% | T+25 | Open baseline |
| 50–59c | 10% | T+32 | Open baseline |
| 60–79c | 13% | T+90 | Winning side buys |
| 80–99c | 6% | late | Near-guaranteed return |

### K9 Per-Market (5m only)
| Market | GP rate | Avg combined | Avg sells/window | Avg deployed |
|--------|---------|-------------|-----------------|-------------|
| BTC 5m | 62% (5/8) | 0.876 | 35.5 | $1,101 |
| SOL 5m | 62% (5/8) | 0.930 | 0.0 | $265 |
| XRP 5m | 75% (6/8) | 0.928 | 0.0 | $195 |

**Critical: K9 ONLY sells actively on BTC 5m. SOL and XRP are pure accumulate + hold.**

---

## Our Failures (March 24, 2026 — 12 deploys, estimated -$80 to -$120 lost)

### Failure 1: Direction lock prevented adapting to reversals
- **What happened:** Bot locked model prediction at T+60. Market reversed. Bot held 165 DOWN losing $60 while UP was winning.
- **Root cause:** `locked_prob_up` froze the allocation and the "never sell favored side" rule used the LOCKED model direction.
- **Rule:** NEVER lock direction. Adapt every tick. Use MARKET price as truth.

### Failure 2: Sold the winning side
- **What happened:** BAD_PAIR sell picked the side with highest avg (= winning side, because winning side is expensive). Sold UP at 55c that was going to pay $1.
- **Root cause:** Sell sort key used `side_avg` descending — higher avg = sold first. But higher avg side is often the WINNER.
- **Rule:** NEVER sell the side the MARKET says is winning (higher bid). Only sell the losing side.

### Failure 3: Churn loop on winning side
- **What happened:** Bought UP at 69c, sold at 66c, bought at 65c, sold at 62c. Lost 3-5c per round trip.
- **Root cause:** Buy logic posted at bid, sell trigger saw it as "expensive" and sold it, then buy posted again.
- **Rule:** Don't sell shares at a loss on the winning side. If you buy at 69c and bid drops to 66c, HOLD it — it pays $1 if it wins.

### Failure 4: Buying dying shares
- **What happened:** Bought UP at 4c, 3c, 2c when BTC had dropped $551. Bought DOWN at 22c when BTC was up $192. All went to zero.
- **Root cause:** The bot treated both sides equally for buying. No check on whether the side was clearly dying.
- **Rule:** Don't buy a side when the OTHER side's bid is > 70c. That side is dying.

### Failure 5: Pair guard blocked 90% of budget
- **What happened:** Deployed $7-12 of $100 budget. Guards checked projected combined_avg, cost_above_floor, position_ev before every buy. Almost everything was blocked.
- **Root cause:** Pair guard was designed for "guaranteed profit" strategy but prevented the bot from deploying capital.
- **Rule:** Only guard is: hard cap (82c) + balance cap (90%) + dying side block. No projected metrics.

### Failure 6: Model is only 64% accurate
- **What happened:** Model said DOWN with prob=0.266, market went UP. Bot was stuck on wrong side.
- **Root cause:** Model accuracy is 64% — it's wrong 36% of the time. Strategy must survive being wrong.
- **Rule:** The model is a HINT. Market price is TRUTH. When they disagree and market has > 10c edge, trust the market.

### Failure 7: Too few sells
- **What happened:** 0-1 sells per window while K9 does 26-52 sells when selling.
- **Root cause:** Sell triggers were too strict: needed excess shares > 5, shares-5 > 10, edge_over_hold > 0.005, etc.
- **Rule:** Sell the losing side when its bid drops below its avg entry AND the other side is winning (bid > 60c). Simple.

### Failure 8: Phantom inventory wipe
- **What happened:** UP shares went from 5 to 0 without any sell order. _v2_apply_sell_fill was called without a real CLOB sell.
- **Root cause:** Unknown caller invoking _v2_apply_sell_fill. Guard added (confirmed_sell flag) but root cause not fully traced.
- **Rule:** _v2_apply_sell_fill MUST require confirmed_sell=True. Never decrement inventory without a real fill.

### Failure 9: Open too early, too expensive
- **What happened:** Open at T+5 with both sides near 50c. Combined = 1.00 from the start.
- **Root cause:** K9 opens at T+27 avg. We rush at T+5 when both sides are still 50/50 = max cost.
- **Rule:** Small open at T+5 (10% budget). Main deployment T+60-180. K9's cheap fills are mid-to-late window.

### Failure 10: Stopped buying too early
- **What happened:** Bot committed at T+250, was idle from T+180. K9's 32% cheapest fills happen after T+149.
- **Root cause:** "buy-only mode" froze allocation, "commit" killed all activity too early.
- **Rule:** Keep buying AND selling until T+240. Only commit at T+250.

---

## The Definitive Ruleset

### MARKET DIRECTION (every tick)
| Condition | Who is winning | Action |
|-----------|---------------|--------|
| YES bid > NO bid + 10c | UP is winning | Weight buys toward UP, sell DOWN |
| NO bid > YES bid + 10c | DOWN is winning | Weight buys toward DOWN, sell UP |
| Bids within 10c | Unclear | Use model split, no sells |
| YES bid > 70c | UP almost certain | STOP buying DOWN entirely |
| NO bid > 70c | DOWN almost certain | STOP buying UP entirely |
| YES bid > 80c | UP won | Sell ALL DOWN shares |
| NO bid > 80c | DOWN won | Sell ALL UP shares |

### BUYING RULES
| Rule | Value | Source |
|------|-------|--------|
| Hard price cap | 82c per share | K9 buys 6% above 80c |
| Balance cap (T+0 to T+120) | 75% max on any side | K9 avg 70% heavy side |
| Balance cap (T+120+) | 90% max on any side | K9 goes up to 87.6% |
| Dying side block | Don't buy if other bid > 70c | Market says this side lost |
| Minimum order | 5 shares | Polymarket minimum |
| Ladder levels | 6 (offsets: 0, 1c, 2c, 4c, 6c, 8c) | More resting = more fills |
| Late ladder | 3 (offsets: 1c, 3c, 5c) | Still active late window |
| Open size | 10% of budget | Small open, deploy later |
| Budget curve | 10% → 22% → 82% → 92% | Ramp deployment over window |
| Budget scale (weak signal) | 0.35x | Don't bet big on 52% confidence |
| Budget scale (strong signal) | 1.0-1.1x | Full deployment on 65%+ |

### SELLING RULES
| Rule | Value | Source |
|------|-------|--------|
| WHO to sell | The LOSING side (determined by MARKET, not model) | K9 sells dropping side; our failure #1, #2, #3 |
| WHEN to sell | T+20 to T+240 | Extended from T+45-T+180 |
| Sell cooldown | 10 seconds | K9 rebuys in 2s, we need some buffer |
| DEAD_SIDE sell | When other bid > 80c, sell ALL of this side | Market says it's over |
| UNFAVORED_RICH sell | When losing side avg > 55c and losing by > 10c in bid | Recover capital from expensive losing position |
| Late dump | T+180-T+250, sell any side with bid < 25c | Recover scraps before commit |
| Never sell | The side the MARKET says is winning (higher bid) | Failure #2, #3 |
| Sell-and-rebuy | After every sell, immediately buy on the cheap winning side | K9 rebuys within 2s (66% < 5s) |
| Don't sell at loss on winning side | If bid dropped below entry but this side is winning, HOLD | Failure #3: churn on winning side |

### WHAT "WINNING" MEANS (critical — this caused our biggest losses)
```
The MARKET determines who is winning, NOT the model.

winning_up = yes_bid > no_bid

This is recalculated EVERY TICK. No locking. No freezing.
The model (prob_up) is used for ALLOCATION SPLIT when the market
is unclear (bids within 5c of each other). When the market has
a clear opinion (> 10c edge), the market overrides the model.
```

### PHASES (simplified)
| Phase | Time | Buy | Sell | Notes |
|-------|------|-----|------|-------|
| Open | T+5–T+15 | Both sides, 10% budget | No | Small initial position |
| Accumulate | T+15–T+240 | Both sides, budget curve | Yes (losing side) | Main deployment. Sell losers, buy winners. |
| Commit | T+250+ | No | No | Cancel unfilled, hold to resolution |

**No "buy-only" phase. No "direction lock" phase. Continuous buying AND selling until T+240.**

### INTERACTION RULES (preventing today's failures)
| Problem | Old behavior | New rule |
|---------|-------------|----------|
| Direction lock | Froze at T+60 | NO LOCK. Market direction recalculated every tick. |
| Sell favored side | Used model-locked direction | Use MARKET direction (yes_bid vs no_bid) |
| Pair guard blocks buys | Checked projected combined/EV | REMOVED. Only hard cap + balance cap + dying side. |
| Anti-churn | Blocked rebuy above last sell price | Only block on LOSING side. Winning side can rebuy at any price. |
| Phantom inventory | _v2_apply_sell_fill called without real sell | confirmed_sell=True required |
| Budget frozen | Guards blocked 90% of orders | Guards stripped to minimum |

### PER-PAIR DIFFERENCES (from K9 data)
| Pair | Sells? | Strategy | Budget |
|------|--------|----------|--------|
| BTC 5m | YES — avg 35.5 per selling window | Full sell-and-rebuy cycle | $150 |
| SOL 5m | NO — zero sells observed | Pure accumulate + hold | $50 (when enabled) |
| XRP 5m | NO — zero sells observed | Pure accumulate + hold | $50 (when enabled) |
| ETH 5m | Unknown (no ETH 5m in K9 data) | Start with BTC-style | $50 (when enabled) |
| All 1h | NO — zero sells | Pure accumulate + hold | $50 (when enabled) |

### MODEL USAGE
| Purpose | Use model? | Use market? |
|---------|-----------|-------------|
| Allocation split (bids within 5c) | YES | No |
| Allocation split (market edge > 10c) | No | YES |
| Which side to sell | No | YES (sell the lower-bid side) |
| Which side to buy more of | Combined | YES (market has priority) |
| Budget scale | YES (confidence → deploy more/less) | No |
| Direction lock | **NEVER** | Always recalculate from market |

### AUTOMATIC SAFETY
| Safety | Value | Reason |
|--------|-------|--------|
| Max loss per window | $25 (stop trading if net_cost - max(up_shares, down_shares) > $25) | Prevent catastrophic single-window loss |
| Max deploys per day | 1 code deploy, max 2 if critical fix | Today we did 12 — never again |
| Simulation before deploy | 50+ windows including REVERSAL scenarios | Fixed UP/DOWN/RANGE doesn't test reversals |
| ECS overlap check | Kill old task before new one trades | Two tasks = double risk |

---

## Simulation Scenarios Required Before Deploy

1. **UP** — BTC goes up steadily. Bot should end mostly UP shares.
2. **DOWN** — BTC goes down steadily. Bot should end mostly DOWN shares.
3. **RANGE** — BTC stays flat. Bot should stay tiny.
4. **UP THEN REVERSAL** — BTC goes up T+0-120, then drops T+120-300. Bot should ADAPT and sell UP, buy DOWN.
5. **DOWN THEN REVERSAL** — BTC goes down T+0-120, then rises T+120-300. Bot should ADAPT and sell DOWN, buy UP.
6. **WHIPSAW** — BTC goes up, down, up, down. Bot should stay small and balanced.
7. **STRONG TREND** — BTC drops $500 in 2 minutes. Bot should quickly dump UP and load DOWN.
8. **LATE REVERSAL** — BTC flat until T+200, then moves hard. Bot should still be able to act.

ALL 8 scenarios must show positive expected value before going live.

---

## Checklist Before Going Live

- [ ] All 8 simulation scenarios show positive EV
- [ ] No direction lock in code
- [ ] Sell decision uses market (yes_bid vs no_bid), not model
- [ ] Pair guard removed (only hard cap + balance cap + dying side)
- [ ] Budget deploys 60%+ in UP and DOWN sims
- [ ] Sell-and-rebuy fires at least 3x in trending sims
- [ ] Dying side block prevents buying when other bid > 70c
- [ ] DEAD_SIDE sell fires when other bid > 80c
- [ ] Late dump works T+180-250
- [ ] confirmed_sell guard on _v2_apply_sell_fill
- [ ] Single ECS task verified before first trade
- [ ] EARLY_ENTRY_MAX_BET = 150
- [ ] PAIRS = BTC_5m
- [ ] Automatic loss limit implemented