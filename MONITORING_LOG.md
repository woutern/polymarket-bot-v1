# Live Monitoring Log — March 24, 2026

## POST-MORTEM: What went wrong today

### Total estimated losses: -$80 to -$120

The bot went through 12+ code deploys (:48 through :62) in a single day.
Each deploy fixed one problem but introduced new ones. The result was a
series of different failure modes, each causing real money losses.

### Root causes (in order of impact)

**1. Direction lock killed us (-$60+ estimated)**
At T+60 the bot locked its model prediction and refused to change.
When the market reversed after T+60, the bot was stuck buying the
losing side. Example: model locked DOWN at T+60, BTC went UP after,
bot held 165 DOWN shares losing $31.90 while UP shares were worth $15.85.
The "never sell favored side" rule prevented recovering because the
locked model said DOWN was favored even though the market said UP.

**2. Buying dying shares (-$20+ estimated)**
The bot kept buying UP at 4-9c when BTC had already dropped $551.
Or buying DOWN at 22c when BTC was up $192. These are shares going
to zero at resolution. Capital wasted that should have gone to the
winning side.

**3. Churn loop on winning side (-$15+ estimated)**
Buying UP at 69c, selling at 66c, buying again at 65c. The sell
triggers and buy logic fought each other. Each round trip lost
3-5c per share in spread.

**4. Pair guard blocked 90% of budget (-$30+ opportunity cost)**
For most of the day, the bot deployed $7-12 of $100 budget. The
pair guard checked projected combined_avg, cost_above_floor, and
position_ev before every buy. Almost everything failed these checks.
K9 deploys 80%+ of budget.

**5. Phantom inventory bug**
Shares disappeared from inventory without a sell order. The
`_v2_apply_sell_fill` was being called from an unexpected code path.
Guard added (`confirmed_sell` flag) but root cause not fully traced.

### What each deploy changed and what went wrong

| Deploy | Change | Result |
|--------|--------|--------|
| :48-:51 | Orphan rescue, salvage, recycle | Recycle never fired (thresholds too strict) |
| :52 | 8-stream data collection | Working correctly |
| :53-:54 | Loosen BAD_PAIR thresholds | Sold the WRONG side (winning side) |
| :55 | Fix sell side (expensive-avg not high-bid) | Correct side, but still too few sells |
| :56 | UNFAVORED_RICH sell + direction lock at T+60 | Direction lock prevented adapting to reversals |
| :57 | Favored-side pair guard bypass | Bot went 96/4 one-sided (too aggressive) |
| :58 | 75% balance cap | Better balance but still low deployment |
| :59 | 55c hard cap + anti-churn | Budget frozen at $5-12 (cap too strict) |
| :60 | Phantom inventory guard | Fixed symptom not root cause |
| :61 | 82c hard cap + 10s cooldown | Churn loop: buy 69c sell 66c buy 65c |
| :62 | Strip pair guard, dying side block, sell-and-rebuy | Direction lock still kills us on reversals |

### The fundamental mistake

I kept making incremental patches and deploying them live immediately.
Each patch was tested against simulations but simulations don't capture:
- Market reversals mid-window
- The interaction between buy logic and sell logic
- Model being wrong 36% of the time
- The speed at which losing positions compound

### What needs to happen before going live again

1. **Remove direction lock entirely** — use market price, not locked model
2. **Use market bid as the "favored side" signal for sells** — if UP bid > 60c, UP is winning regardless of model
3. **Proper simulation with realistic market reversals** — not just UP/DOWN/RANGE
4. **Test for at least 50 simulated windows** before live deploy
5. **Single deploy, single test** — not 12 deploys in one day
6. **Set a loss limit** — stop the bot automatically if losses exceed $X per window

### Lessons learned

- More guards ≠ better. Each guard blocked something useful.
- The model is 64% accurate. 36% of the time it's wrong. The strategy MUST handle being wrong gracefully.
- K9 doesn't lock direction. K9 adapts to the market continuously.
- Simulations with fixed UP/DOWN scenarios don't test the most important case: the market reversing mid-window.
- Deploying 12 times in one day is reckless. Each deploy should be a deliberate, tested change.

---

## Session Info (final state)
- **Task definition:** :59
- **Budget:** $100 per window (EARLY_ENTRY_MAX_BET=100)
- **Pairs trading:** BTC_5m only
- **Pairs collecting:** ETH_5m, SOL_5m, XRP_5m + all 4 hourly
- **Code version:** 64a6141 (55c hard cap + anti-churn + 75% balance + UNFAVORED_RICH sells + late dump)

---

## What's Going Right ✅

1. **Recycle is finally firing** — BAD_PAIR and PAYOUT_FLOOR sells now trigger correctly
2. **Correct sell side** — sells the expensive-avg side, not the winning side (fixed in :55)
3. **Direction lock at T+60** — stops chasing model flips mid-window
4. **55c hard cap** — prevents buying the expensive side at 58-68c
5. **75% balance cap** — prevents 96/4 one-sided positions
6. **Anti-churn** — won't rebuy above last sell price
7. **Late dump** — sells near-worthless shares (bid < 10c) before commit
8. **Data collection working** — all 4 × 5m and 4 × 1h streams writing to DynamoDB
9. **Model retraining** — EventBridge every 4h, BTC/ETH/SOL models fresh
10. **Sim results are positive** — UP: 31/42 +$9.02, DOWN: 36/32 +$3.47, RANGE: 5/5 $0

## What Needs Improvement ❌

1. **Deployed capital still sometimes low** — some windows only deploy $5-10 of $100 budget when signal is weak. This is "correct" behavior but means low absolute profit on correct predictions.
2. **Model accuracy is only 64%** — barely beats raw 15-second price move (63.9%). The 14-feature LightGBM adds almost nothing over simple momentum.
3. **Calibration bad at extremes (BTC/SOL)** — model says 91% when actual is 71%. Position sizing is wrong because of this.
4. **No multi-timeframe context** — 5m model doesn't know if the hourly trend is UP or DOWN. This is probably the biggest missing feature.
5. **Orderbook not used in model** — we fetch the orderbook every tick but only use best bid/ask for execution. Depth, imbalance, spread are not model features.
6. **Signal features broken** — signal_move_pct and signal_ev are stuck at 0 in training data. 2-4 features are pure noise.
7. **Only 1-3 ladder levels** — K9 posts 5-8+ price levels per side. We're missing cheap fills at wider offsets.
8. **Deploy speed is slow** — one 161MB app layer means 5-10 minute deploys.
9. **No automatic health alerting** — if the bot dies we don't know until we check manually.
10. **Graceful stop still broken in production** — AccessDeniedException on GetSecretValue means we can't disable new windows mid-flight.

## Observed Windows

### Template for each window:
```
Time: [UTC]
Model: prob_up=[X]
Open: UP [X] @ [Xc] / DOWN [X] @ [Xc]
T+60 (lock): UP [X] / DOWN [X] combined=[X]
T+120: UP [X] / DOWN [X] combined=[X] sells=[X]
Final: UP [X] / DOWN [X] net=$[X] combined=[X]
Result: [UP/DOWN won] PnL: $[X]
Notes: [anything unusual]
```

(Windows logged below as they complete)

### Window 0 — ~13:05 UTC (task :59)
```
13:05:06 FILL UP=5@0.51, DN=5@0.50
13:05:14 FILL DN=5@0.51, DN=5@0.50 (more DOWN)
13:05:35 CANCEL UP (stale)
13:05:36 v2_sell_inventory_updated ← BUG: wipes UP inventory
13:05:45 T+45 UP:0 DN:15 — UP vanished
13:06:54 FILL UP=5@0.45 (cheap fills come back)
13:07:00 T+120 UP:5 DN:10 combined=0.95
13:08:00 T+180 UP:20 DN:10 combined=0.935
13:08:30 v2_sell_inventory_updated ← late dump fires
13:09:15 COMMITTED UP:15 DN:10 combined=0.79
```
Result: UP:15 DN:10 combined=0.79 — decent pair but UP shares were wiped mid-window

### Window 1 — ~12:15 UTC (task :59)
```
T+  5s prob=0.333 UP:0  DN:5@0.47  comb=0     net=$2.4  sell=No  guard=3  bal=0%UP   rem=$95
T+ 30s prob=0.52  UP:5  DN:5@0.47  comb=1.000 net=$5.0  sell=No  guard=0  bal=50%UP  rem=$95
T+ 60s prob=0.52  UP:0  DN:20@0.48 comb=0     net=$9.7  sell=No  guard=0  bal=0%UP   rem=$90
T+120s prob=0.52  UP:0  DN:20@0.48 comb=0     net=$9.7  sell=No  guard=0  bal=0%UP   rem=$90
T+180s prob=0.52  UP:0  DN:20@0.48 comb=0     net=$9.7  sell=No  guard=2  bal=0%UP   rem=$90
T+240s prob=0.52  UP:0  DN:15@0.49 comb=0     net=$7.3  sell=No  guard=2  bal=0%UP   rem=$93
```
**Issues found:**
- UP shares went from 5 to 0 between T+30 and T+60 WITHOUT any sell firing
- This is a state/accounting bug — shares disappeared from inventory
- Bot stuck at 0 UP / 20 DOWN for the entire window
- $90 budget unused
- Model was weak/neutral (prob=0.52) the whole time
- No sells fired at all despite being one-sided

**Root cause: FOUND — `v2_sell_inventory_updated` fires after stale order cancels**

Detailed trace from the 13:05 UTC window:
```
13:05:06 FILL UP shares=5 price=0.51    ← UP filled, inventory = 5 UP
13:05:14 FILL DOWN shares=5 price=0.51  ← DOWN filled
13:05:14 FILL DOWN shares=5 price=0.50  ← more DOWN
13:05:35 CANCEL side=UP                 ← stale UP order cancelled
13:05:36 v2_sell_inventory_updated      ← ⚠️ INVENTORY WIPED as if sold!
13:05:45 TICK T+45 UP:0 DN:15           ← UP vanished
```

The `v2_sell_inventory_updated` log event (which comes from `_v2_apply_sell_fill`)
fires RIGHT AFTER a stale order cancel. This means either:
1. The late-dump path (`LATE_DUMP_START=210`) is somehow triggering early, OR
2. A sell path is being called from the cancel/recycle flow incorrectly, OR  
3. The `_early_sell` function is being invoked from an unexpected code path

Additional evidence from the same session:
- `v2_sell_inventory_updated` fires 4 times across the window without any
  `sell_fired=True` in the execution tick logs
- Late fills at price=0.02 suggest the bot is posting lottery-price orders
  that fill, then the inventory gets wiped by the phantom sell path
- The late-dump path has `bid < 0.10` as the trigger — but the sells are
  happening when bids are 0.45+, so late-dump is NOT the cause

**This is a critical bug. Shares are being removed from inventory without
an actual sell order being placed on the CLOB. This means our position
tracking is wrong and P&L calculations are unreliable.**

**Fix needed:** Add a guard or trace logging in `_v2_apply_sell_fill` to
identify which caller is invoking it outside of a real sell execution.

---

## Priority Improvements for Next Session

### CRITICAL (do BEFORE any live trading)
0. **Remove direction lock** — use market price to determine winning side, not locked model
1. **Use market bid for sell decisions** — if UP bid > 60c, UP is winning. Sell DOWN.
2. **Build realistic reversal simulations** — test market flipping mid-window
3. **Test 50+ simulated windows** — not just 3 fixed scenarios
4. **Set automatic loss limit** — stop bot if window loss > $20

### After strategy is validated
5. **Fix vanishing shares bug** — trace the exact caller
6. **Enable ETH_5m** — best calibrated model, $50 budget
7. **Fix signal features** — stop writing 0s in training data
8. **Temperature scaling** — fix BTC/SOL calibration
9. **Build 1h training pipeline** — convert S3 candle data

### Do NOT do again
- Deploy 12 times in one day
- Add "safety" guards without testing interaction effects
- Lock direction based on a 64% accurate model
- Override market signals with model predictions
- Go live without reversal-scenario testing

---

## Quick Commands

```bash
# Check if bot is running
aws ecs describe-services --cluster polymarket-bot --services polymarket-bot-service --profile playground --region eu-west-1 --query 'services[0].{desired:desiredCount,running:runningCount,taskDef:taskDefinition}' --output json

# Start the bot
aws ecs update-service --cluster polymarket-bot --service polymarket-bot-service --desired-count 1 --region eu-west-1 --profile playground

# Stop the bot
aws ecs update-service --cluster polymarket-bot --service polymarket-bot-service --desired-count 0 --region eu-west-1 --profile playground

# Check latest BTC window
TASK_ARN=$(aws ecs list-tasks --cluster polymarket-bot --service-name polymarket-bot-service --desired-status RUNNING --profile playground --region eu-west-1 --query 'taskArns[0]' --output text)
TASK_ID=$(echo "$TASK_ARN" | awk -F/ '{print $NF}')
aws logs filter-log-events --log-group-name /polymarket-bot --log-stream-names "polybot/polymarket-bot/$TASK_ID" --filter-pattern "v2_execution_tick" --start-time $(python3 -c "import time; print(int((time.time()-30)*1000))") --limit 1 --profile playground --region eu-west-1 --query 'events[-1].message' --output text

# Deploy new code
bash scripts/deploy_aws.sh

# Measure model accuracy
PYTHONPATH=src uv run python3 scripts/measure_model_accuracy.py

# Run simulations
PYTHONPATH=src uv run python3 scripts/simulate_v2_window.py --scenario up --budget 100
PYTHONPATH=src uv run python3 scripts/simulate_v2_window.py --scenario down --budget 100
PYTHONPATH=src uv run python3 scripts/simulate_v2_window.py --scenario range --budget 100
```
