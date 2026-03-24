# Polymarket Bot — Plan & Context
> Read this file at the start of every session. Single source of truth for what we're building and where we are.

---

## What This Is

An algorithmic trading bot for Polymarket BTC 5-minute binary prediction markets.
Strategy: buy both sides (YES + NO), recycle capital from losing side to winning side, hold to resolution.
If combined avg cost < $1.00, guaranteed profit regardless of outcome.

Owner: Wouter (Scaleflow)
Stack: Python 3.12, asyncio, AWS ECS (eu-west-1), Polygon blockchain
Bot: STOPPED as of end of March 24, 2026 session

---

## Current State

### Bot is STOPPED
- ECS desired-count = 0
- Last deployed: task def :62 (has bugs — do NOT restart without reading this file)
- Lost ~$100 in one day from 12 rapid deploys with untested strategy changes

### What's deployed vs what's in code
- Live code: `:62` (buggy — direction lock, wrong-side sells, budget frozen)
- New strategy: `scripts/strategies.py` (K9v2Strategy — NOT yet in live bot)
- New simulator: `scripts/replay_simulator.py` (for testing before deploy)
- **The live bot code in `src/polybot/core/loop.py` has NOT been updated yet**

---

## Architecture

### Single Bot, Multiple Strategy Profiles

```
TradingEngine (src/polybot/core/loop.py — shared, same for all pairs)
    │
    └── calls Strategy.on_tick() every second
            │
            ├── K9v2Strategy       — BTC_5m, ETH_5m (sells + rebalancing)
            └── AccumulateOnly     — SOL_5m, XRP_5m, all 1h (no sells, just buy both sides)
```

### Per-Pair Profiles (from K9 data)

| Pair | Strategy | Budget | Sells? | Source |
|------|----------|--------|--------|--------|
| BTC_5m | K9v2Strategy | $150 | YES — avg 35.5/window | K9 verified |
| ETH_5m | K9v2Strategy | $50 | YES — assume BTC-style | No K9 ETH data |
| SOL_5m | AccumulateOnly | $50 | NO — zero sells | K9 verified |
| XRP_5m | AccumulateOnly | $50 | NO — zero sells | K9 verified |
| BTC_1h | AccumulateOnly | $50 | NO — zero sells | K9 verified |
| ETH_1h, SOL_1h, XRP_1h | AccumulateOnly | $50 | NO | K9 verified |

---

## Key Files

| File | Purpose |
|------|---------|
| `K9_RULESET.md` | **READ FIRST** — definitive trading rules from K9 data + every failure |
| `PLAN.md` | This file |
| `MONITORING_LOG.md` | Post-mortem of today's losses, what went wrong |
| `STRATEGY_VS_K9.md` | Full comparison table, per-pair profiles |
| `MODEL_IMPROVEMENT_PLAN.md` | How to improve the LightGBM model |
| `scripts/strategies.py` | **NEW** — clean K9v2Strategy + AccumulateOnly + StrategyProfile |
| `scripts/replay_simulator.py` | **NEW** — test strategies against real + synthetic data |
| `scripts/dump_replay_data.py` | Dumps real CloudWatch tick data to `data/` |
| `data/replay_dataset.json` | 49 real BTC 5m windows with tick-by-tick data |
| `data/replay_window_summaries.json` | Per-window analysis of what actually happened |
| `src/polybot/core/loop.py` | Live bot execution engine (NOT yet updated with K9v2) |

---

## The K9 Ruleset (Summary — Read K9_RULESET.md for full detail)

### Market Direction (every tick, no locking)
```
winning_up = yes_bid > no_bid   # recalculated every tick, never frozen
```
- Market edge > 10c → trust market over model
- Market edge > 20c → very clear, lean 80/20
- Other side bid > 70c → don't buy this side (dying)
- Other side bid > 80c → sell ALL of this side (dead)

### Buying Rules
- Hard cap: 82c (K9 buys 6% above 80c on winning side)
- Balance cap: 75% before T+120, 90% after
- Don't buy dying side (other bid > 70c after T+60)
- Both sides get orders — weighted by direction
- Deploy 80%+ of budget

### Selling Rules (BTC only — SOL/XRP/1h never sell)
- Sell the LOSING side (determined by market, not model)
- DEAD_SIDE: other bid > 80c → sell 15 shares/tick
- REVERSAL: direction flipped → sell 15 shares on first tick, 10/tick for 30s
- UNFAVORED_RICH: losing side avg > 50c and market edge > 10c → sell 10 shares
- LATE_DUMP: T+180-250, sell anything with bid < 25c
- Sell-and-rebuy: after every sell, immediately buy winning side
- Cooldown: 10 seconds between sells (unless reversal)

### What Killed Us Today (do NOT repeat)
1. Direction lock at T+60 — froze model, market reversed, lost $60+
2. Sold winning side — BAD_PAIR picked highest avg (= winning) to sell
3. Pair guard blocked 90% of buys — $7-12 deployed out of $100
4. Bought dying shares — UP at 9c when BTC dropped $551
5. No reversal handling — 0% win rate on reversals

---

## Simulator — How It Works

### Run Against Real Data (49 windows from today)
```bash
python3 scripts/replay_simulator.py --strategy k9 --budget 150
python3 scripts/replay_simulator.py --compare  # actual vs k9 side by side
python3 scripts/replay_simulator.py --window 5 --verbose  # single window detail
```

### Run Against Synthetic Data
```bash
python3 scripts/replay_simulator.py --synthetic --count 200 --seed 42 --budget 150
python3 scripts/replay_simulator.py --synthetic --count 500  # more windows
```

### Synthetic Scenario Types
| Scenario | % of batch | What happens |
|----------|-----------|-------------|
| random | 40% | Random walk, can go either way |
| up | 10% | BTC trends up steadily |
| down | 10% | BTC trends down steadily |
| reversal_up_down | 10% | UP first half, DOWN second half |
| reversal_down_up | 10% | DOWN first half, UP second half |
| whipsaw | 5% | Oscillates up/down rapidly |
| strong_trend_up | 5% | Very strong UP move |
| strong_trend_down | 5% | Very strong DOWN move |
| flat | 5% | Barely moves |

### Current Simulator Results (K9v2Strategy, 200 synthetic windows)

| Scenario | Win rate | Avg PnL | Status |
|----------|---------|---------|--------|
| up | 100% | +$23 | ✅ |
| down | 100% | +$23 | ✅ |
| strong_trend | 100% | +$13 | ✅ |
| random | 60% | +$12.50 | ✅ |
| reversal | 0% (v1) → testing | -$15 | 🔄 being fixed |
| whipsaw | 40% | -$25 | ❌ needs work |
| flat | 40% | -$7.60 | ❌ needs work |

**Overall: +$1,234 total across 200 windows (+$6.17/window)**
**Reversals fixed in K9v2: -$75 → +$65 (not yet validated at scale)**

---

## Model Accuracy

| Pair | Direction accuracy | AUC | Calibration | Status |
|------|-------------------|-----|-------------|--------|
| BTC_5m | 63.7% | 0.689 | BAD at extremes | Live, trading |
| ETH_5m | 63.9% | 0.700 | GOOD everywhere | Ready to enable |
| SOL_5m | 64.1% | 0.697 | BAD at extremes | Collecting data |
| XRP_5m | — | — | — | ~10 rows, not ready |

Run model accuracy check:
```bash
PYTHONPATH=src uv run python3 scripts/measure_model_accuracy.py
```

---

## Data Collection Status

| Pair | 5m rows | 1h rows | Model age |
|------|---------|---------|-----------|
| BTC | 17,504+ | collecting | ~11h |
| ETH | 17,804+ | collecting | ~2h |
| SOL | 9,289+ | collecting | ~11h |
| XRP | ~10 | collecting | no model |

Bot collects data via:
- `PAIRS=BTC_5m` — live trading + collection
- `WATCH_PAIRS=ETH_5m,SOL_5m,XRP_5m` — collection only, no trading
- All 4 hourly states tracked for 1h data
- Retrain: EventBridge every 4h → `scripts/retrain_entrypoint.py`

---

## What Needs to Happen (Strict Order)

### Phase 1: Validate Strategy in Simulation (DO BEFORE ANY DEPLOY)

**Step 1: Integrate K9v2Strategy into replay_simulator.py**
- Currently `replay_simulator.py` has its own inline `K9Strategy` class
- Replace it with `from strategies import K9v2Strategy, BTC_5M_PROFILE`
- The `on_tick()` interface is slightly different — needs adapter

**Step 2: Run 500 synthetic windows with K9v2**
```bash
python3 scripts/replay_simulator.py --synthetic --count 500 --seed 42 --budget 150
```
- Target: reversal win rate > 50%
- Target: whipsaw win rate > 50%
- Target: overall win rate > 60%
- Target: avg deployed > $100 of $150

**Step 3: Run against 49 real windows**
```bash
python3 scripts/replay_simulator.py --budget 150
```
- Check GP rate vs actual (we got 33%, K9 gets 67%)
- Check avg deployed vs actual ($25.6 actual, target > $100)

**Step 4: Fix whipsaw (40% win rate is bad)**
- Problem: bot deploys heavily in both directions, ends up with expensive both sides
- Fix: in whipsaw, reduce position size when combined > 0.97
- Or: whipsaw detection (direction changing every 20-30s) → stay tiny

**Step 5: All 8 scenarios must show positive EV before deploy**

### Phase 2: Port K9v2 to Live Bot

**Step 6: Add yes_bid/no_bid to v2_execution_tick log**
In `src/polybot/core/loop.py`, at the end of `_v2_execution_tick`, add:
```python
logger.info("v2_execution_tick", ..., yes_bid=round(yes_bid, 3), no_bid=round(no_bid, 3), ...)
```
This gives us real orderbook data in future replay datasets.

**Step 7: Replace v2_execution_tick with K9v2Strategy**
- The clean strategy logic is in `scripts/strategies.py`
- The live bot is in `src/polybot/core/loop.py`
- Key: remove direction lock, use `yes_bid > no_bid` as direction, sell losing side

**Step 8: Update secrets**
```
EARLY_ENTRY_MAX_BET = 150
PAIRS = BTC_5m
WATCH_PAIRS = ETH_5m,SOL_5m,XRP_5m
DATA_COLLECTION_MODE = true
```

**Step 9: Deploy with new deploy script**
```bash
bash scripts/deploy_aws.sh  # now waits for rollout, verifies single task
```

**Step 10: Monitor first 3 windows before leaving unattended**
- Check: yes_bid/no_bid in logs (new field)
- Check: sell_fired=True appears when losing side > 70c
- Check: budget_deployed > $80 of $150
- Check: no direction lock events in logs

### Phase 3: Expand to More Pairs

Only after BTC_5m is profitable for 50+ windows:

1. **ETH_5m** — best calibrated model, K9v2Strategy, $50 budget
2. **SOL_5m** — AccumulateOnly, $50 budget
3. **XRP_5m** — AccumulateOnly, wait for >500 rows (~2 days)
4. **BTC_1h** — AccumulateOnly, build 1h training pipeline from S3 candle data

### Phase 4: Model Improvements

From `MODEL_IMPROVEMENT_PLAN.md`:

1. **Fix broken features** — signal_move_pct and signal_ev are stuck at 0 in training data
2. **Temperature scaling** — fix BTC/SOL calibration (predicts 91%, actual 71%)
3. **Add hourly context features** — hourly trend as input to 5m model
4. **XGBoost + CatBoost ensemble** — diversity improves accuracy
5. **Add orderbook features** — bid imbalance, spread (need to collect first)

---

## Checklist Before Going Live

- [ ] K9v2Strategy integrated into replay_simulator
- [ ] 500 synthetic windows: reversal win > 50%, whipsaw win > 50%, overall > 60%
- [ ] 49 real windows: GP rate improving vs actual baseline
- [ ] No direction lock in code
- [ ] Sell decision uses market (yes_bid vs no_bid), not model
- [ ] Budget deploys > 80% in UP/DOWN sims
- [ ] Reversal sell fires at T+120-150 (not T+197)
- [ ] yes_bid/no_bid added to v2_execution_tick log
- [ ] Single ECS task verified before first trade
- [ ] EARLY_ENTRY_MAX_BET = 150
- [ ] Automatic stop if window loss > $30

---

## Quick Commands

```bash
# Run tests
PYTHONPATH=src uv run python3 -m pytest tests/test_v2_strategy.py -q

# Full test suite  
PYTHONPATH=src uv run python3 -m pytest -q

# Simulation (synthetic)
python3 scripts/replay_simulator.py --synthetic --count 200 --budget 150

# Simulation (real data)
python3 scripts/replay_simulator.py --budget 150

# Strategy self-test
python3 scripts/strategies.py

# Model accuracy
PYTHONPATH=src uv run python3 scripts/measure_model_accuracy.py

# Dump new real data
PYTHONPATH=src uv run python3 scripts/dump_replay_data.py

# Deploy (only after sim passes)
bash scripts/deploy_aws.sh

# Check bot status
aws ecs describe-services --cluster polymarket-bot --services polymarket-bot-service \
  --profile playground --region eu-west-1 \
  --query 'services[0].{desired:desiredCount,running:runningCount,taskDef:taskDefinition}' \
  --output json

# Start bot
aws ecs update-service --cluster polymarket-bot --service polymarket-bot-service \
  --desired-count 1 --region eu-west-1 --profile playground

# Stop bot
aws ecs update-service --cluster polymarket-bot --service polymarket-bot-service \
  --desired-count 0 --region eu-west-1 --profile playground

# Login AWS if expired
aws sso login --profile playground
```

---

## Do NOT Do

- Deploy 12 times in one day
- Add "safety" guards that interact in unexpected ways
- Lock direction based on a 64% accurate model
- Go live without passing reversal + whipsaw simulation tests
- Deploy without checking `git log` and `git diff` first
- Run two ECS tasks simultaneously (kill old before new trades)

---

## Git Status (end of March 24, 2026)

```
master @ 03313a3
Key recent commits:
  03313a3 K9v2 strategy + synthetic market simulator
  92dbe5c Add synthetic market generator to replay simulator
  b67919a Add replay dataset (49 real BTC windows) + K9 ruleset
  5411c5d Definitive K9 ruleset
  4efee46 Post-mortem: document all losses
  fce73d5 Widen ladder from 3 to 6 levels
  38d6a5c K9-style overhaul: strip pair guard (buggy — caused losses)
```

**The live bot code (`:62`) has bugs. Do NOT restart it.**
**Use the simulator to validate K9v2Strategy first.**