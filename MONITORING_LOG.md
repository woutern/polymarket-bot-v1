# Live Monitoring Log — March 24, 2026

## Session Info
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

(Windows will be logged below as they complete)

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

**Root cause:** UNKNOWN — needs investigation. UP shares vanishing without a sell
is either an inventory tracking bug or the position got wiped by some other code path.
This is a critical bug to fix.

---

## Priority Improvements for Next Session

### Immediate (do first when back)
1. **Enable ETH_5m** — best calibrated model, same strategy, $50 budget
2. **Fix signal features** — stop writing 0s in training data
3. **Temperature scaling** — fix BTC/SOL calibration with 1-parameter method

### Short-term (this week)
4. **Add multi-timeframe features** — hourly trend as input to 5m model
5. **Add orderbook features** — depth imbalance, spread to model
6. **Wider ladder** — 5-6 price levels instead of 1-3
7. **Add XGBoost + CatBoost ensemble** — diversity improves accuracy

### Medium-term
8. **Build 1h training pipeline** — convert S3 candle data to labeled windows
9. **Train BTC_1h model** — K9's hourly strategy is simpler (no sells, just accumulate)
10. **Fix deploy speed** — slim Docker image, cache layers properly

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
