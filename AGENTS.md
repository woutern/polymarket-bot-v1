# Agent Handoff — Polymarket Bot

**For Codex and Claude:** Read this file at the start of every session before doing anything else.
Update your section when you start, finish, block, or hand off. This is the single source of truth for what each agent is doing.

---

## Current focus
**BTC_5m optimization + data collection for all pairs.** Do not deploy 1h trading yet.

---

## Codex — current work

**Status:** LIVE on :57 + `btc_5m_arb_scanner_v3` deployed in ECR image, one-off ECS scanner run validated, interactive ECS Exec task running for manual login; now validating `btc_5m_arb_sniper_v2.py` guardrails/tuning from live logs
**Last commit:** 0109c17 (model accuracy analysis + per-pair profiles)

### What's deployed (task def :57, March 24 2026)

- Budget: $100 per window (EARLY_ENTRY_MAX_BET=100)
- PAIRS=BTC_5m (trading)
- WATCH_PAIRS=ETH_5m,SOL_5m,XRP_5m (data collection only)
- DATA_COLLECTION_MODE=true
- All 8 streams tracked: 4×5m + 4×1h

### Changes made today

1. **8-stream data collection** — ETH/SOL/XRP 5m + all 4 hourly streams now collecting training data
2. **BAD_PAIR recycle fix** — loosened thresholds so recycle actually fires on bad pairs
3. **Correct sell side** — BAD_PAIR now sells the expensive-avg side, not the high-bid side (was selling winners before)
4. **UNFAVORED_RICH sell trigger** — sells expensive unfavored side when model edge >= 0.10
5. **Direction lock at T+60** — stops chasing model flips mid-window
6. **Pair guard removed for favored-side buys** — when model edge >= 0.08, favored side buys bypass pair guard entirely
7. **Bug fixes** — undefined btc_confirms, unbound variable, unused imports

### Model accuracy (measured today on full training data)

| Pair | Accuracy | AUC | Calibration | Status |
|------|----------|-----|-------------|--------|
| BTC_5m | 63.7% | 0.689 | BAD at extremes | Live, trading |
| ETH_5m | 63.9% | 0.700 | GOOD everywhere | Ready to enable |
| SOL_5m | 64.1% | 0.697 | BAD at extremes | Needs stricter profile |
| XRP_5m | — | — | — | Zero data, collecting |

### Live observation (latest window)

First window on :57 showed correct behavior:
- Model said strong DOWN → bot deployed 100 DOWN / 5 UP
- Deployed $70 of $100 budget (not frozen!)
- If DOWN wins: +$29.59 profit

### Per-pair strategy profiles defined

See STRATEGY_VS_K9.md for full details. Key differences:
- BTC: clamp extreme model probs, $100 budget
- ETH: trust raw model output (good calibration), $50 budget, enable next
- SOL: stricter caps, faster sells, $50 budget
- XRP: not ready
- 1h: not ready (need training pipeline from candle data)

### Tests
- `PYTHONPATH=src uv run pytest -q` → 654 passed
- Simulations: UP +$31.32, DOWN +$27.67, RANGE $0.00

### Next steps (Codex)
1. User login via ECS Exec into task `6e1a4ebe5f2e427a9c123ba82847747a` and run `arbitrage/btc_5m_arb_scanner_v3.py`
2. Monitor BTC_5m :57 for 5-10 windows
3. Build per-pair profile config in code (not just docs)
4. Enable ETH_5m with conservative profile
5. Build 1h training pipeline from S3 candle data (don't deploy 1h trading)
6. Investigate wider ladder (5-6 price levels vs current 1-3)

---

## Claude — completed work

**Retrain pipeline audit and fix (2026-03-24)**

Found and fixed 3 root causes:

1. **IAM permission missing** — added S3 read on training bucket
2. **Trainer dedup bug** — dedup key now uses `slug or window_slug or window_id`
3. **EventBridge stale target** — updated to task def :47 with retrain command

Manual retrain results:
- ETH_5m: DEPLOYED (AUC 0.7060)
- BTC_5m: SKIPPED (quality gate — Jon-Becker model still better)
- SOL_5m: SKIPPED (quality gate)
- XRP_5m: SKIPPED (0 rows)

**DynamoDB tables created:**
- `polymarket-bot-v2-windows` (not yet wired in loop)
- `polymarket-bot-v2-fills` (not yet wired in loop)

Schedule working: EventBridge every 4h → task def :47 → retrain entrypoint

---

## Data status

### DynamoDB training rows (as of March 24 09:25 UTC)

| Pair | 5m rows | 1h rows | Model |
|------|---------|---------|-------|
| BTC | 17,504 | 0 (collecting) | ✅ AUC 0.737, 11h old |
| ETH | 17,804 | 0 (collecting) | ✅ AUC 0.706, 2h old |
| SOL | 9,289 | 0 (collecting) | ✅ AUC 0.771, 11h old |
| XRP | ~10 (just started) | 0 (collecting) | ❌ No model |

### S3 candle data (for building 1h training pipeline)

| Asset | 1-min candles | Date range | Potential 1h windows |
|-------|--------------|------------|---------------------|
| BTC | 129,566 | Dec 17 – Mar 17 | ~2,159 |
| ETH | 43,167 | Feb 16 – Mar 18 | ~719 |
| SOL | 43,167 | Feb 16 – Mar 18 | ~719 |

---

## Open items / blockers

- [ ] Wire `put_v2_window` / `put_v2_fill` into live loop (V2 analytics)
- [ ] Build per-pair profile config in code (currently only in docs)
- [ ] Build 1h training pipeline from S3 candle parquets
- [ ] Train and validate BTC_1h model before deploying 1h trading
- [ ] Fix runtime secrets refresh (AccessDeniedException on GetSecretValue)
- [ ] Reduce Docker image size for faster deploys
- [ ] Prevent ECS overlapping tasks during rollout

---

## What "clean BTC_5m window" means

- Budget actually deployed ($30+, not frozen at $5)
- Direction matches model signal
- Combined avg trending toward or below 1.00
- Sells fire when unfavored side is expensive
- No model-flip whipsaw after T+60
- Weak/sideways windows stay small ($5-10)
- Strong windows lean hard (70/30 or 80/20)

---

## Key files

| File | Purpose |
|------|---------|
| `src/polybot/core/loop.py` | Main strategy engine |
| `src/polybot/config.py` | Settings + pair config |
| `src/polybot/ml/trainer.py` | Model training (all 8 pairs) |
| `src/polybot/ml/server.py` | Model serving (all 8 pairs) |
| `aws/task-definition.json` | ECS task def with WATCH_PAIRS + DATA_COLLECTION_MODE |
| `tests/test_v2_strategy.py` | V2 strategy regression tests (113 tests) |
| `scripts/simulate_v2_window.py` | Local CLI simulation |
| `scripts/measure_model_accuracy.py` | Model accuracy by confidence bucket |
| `STRATEGY_VS_K9.md` | Strategy comparison + per-pair profiles |
| `AGENTS.md` | This file — agent coordination |
