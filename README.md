# Polymarket Trading Bot

Algorithmic trading system for Polymarket prediction markets.

**Status:** Deployed on AWS ECS (eu-west-1)
**Dashboard:** https://d2rj5lnnfnptd.cloudfront.net/
**Tests:** 888 passing
**V2 both-sides:** gated by `EARLY_ENTRY_ENABLED=true` and pair-level `PAIRS`

## Three Trading Strategies

### 1. V2 Both-Sides (profile-driven 5m engine)
Current live focus is `BTC_5m`, with other pairs enabled only after pair-specific tuning.
`WATCH_PAIRS` can track non-trading 5m markets for data collection.

- **Scope:** 5-minute windows only for the current V2 rollout, enabled per pair via `PAIRS`
- **Watch-only collection:** additional 5m pairs can be tracked via `WATCH_PAIRS` without becoming tradable
- **Cadence:** evaluate every 1s, but only trade when budget, pair-quality, and stale-order rules allow
- **Phase 1 — Open (T+5s to T+15s):** small two-sided open
  - uses 10% of per-window budget
  - model drives split (`80/20` to `50/50`)
  - executable size is based on whole shares with a 5-share minimum
- **Phase 2 — Main deploy (T+15s to T+180s):** accumulate both sides, recycle selectively
  - smooth budget curve ramps from 10% to 82% of budget by `T+180`
  - confidence scaling reduces spend in weak windows and allows fuller deployment in stronger ones
  - very strong windows (`abs(prob_up - 0.50) >= 0.20`) can use a small global budget bump
  - strong windows can top up the favored side more aggressively, but only if the projected position still has non-negative model-weighted EV
  - favored-side budget can borrow from the unfavored side when the signal is strong and the favored side is not already clearly ahead in shares
  - limited sell-and-recycle can start at `T+45` when inventory above the payout floor can be sold at a favorable bid
- **Phase 3 — Buy-only (T+180s to T+250s):**
  - frozen allocation split
  - no more sells
  - only passive adds if they pass pair-quality guards
- **Phase 4 — Commit/Hold (T+250s to resolution):**
  - cancel unfilled GTC orders
  - hold remaining inventory to market resolution
- **Active order recycling / repricing:**
  - open orders are inspected every tick
  - stale orders are cancelled after `early_entry_reprice_stale_after_seconds` (default `6s`) if they are no longer within `early_entry_reprice_price_tolerance` (default `1c`) of desired prices
  - cancelled orders release reserved budget immediately
- **Pair-quality guards:**
  - rich-side buys are capped later in the window
  - incomplete pairs do not keep averaging the filled side while the missing side drifts expensive
  - new adds are blocked when projected `combined_avg` / payout-floor pressure would worsen a bad state
  - projected pair states are checked against model-weighted expected value, not just share balance
  - reserved open orders count toward pair risk, so one resting rich-side order cannot "hide" risk from the next one
- **Budget / accounting:** all risk is based on executable USD notional, never on target size alone
  - `actual_notional_usd = actual_shares * actual_price`
  - `reserved_open_order_usd + filled_position_cost_usd + new_actual_notional_usd <= $50` per asset per window
  - reserve is released on fill, cancel, reject, timeout, and commit
- **Paper verification path:** paper mode exposes an in-memory order client so V2 can exercise post/fill/cancel/release accounting without real-money execution

### 2. 5-Minute Crypto Bot (Scenario C, paused)
- Trades BTC/SOL 5-minute Up/Down windows
- Scan window T+210s–T+240s: finds best entry price
- LightGBM entry filter: lgbm_prob >= 0.62 required (trained on 22K Jon-Becker windows)
- Scenario C: lgbm gates first, ask ceiling relaxed for high conviction
- Sizing: $5 default, $10 peak at ask >= $0.75, $5 at $0.82-$0.95 with lgbm >= 0.70/0.80
- Resolution via Polymarket Chainlink oracle (not Coinbase)

### 3. Opportunity Bot (paused)
- 13 parallel workers scan all Polymarket markets every 30 min
- Categories: crypto, finance, fed, geopolitics, elections, tech, weather, culture, economics, companies, health, iran, whitehouse
- Tier 0 ($0.93+, ≤6h, vol≥$5K): $10 FOK — dual AI: Haiku sanity + Sonnet devil's advocate
- Tier 1 ($0.85–$0.94, ≤24h): $5 FOK — Haiku sanity check (conf >= 0.80)
- Tier 2 ($0.85–$0.94, 24-48h): $2.50 FOK — full Haiku AI assessment (conf >= 0.85)
- Data-driven filters: skip morning 06-12 UTC, skip 6-12h resolve window
- Pause flag: `OPPORTUNITY_BOT_PAUSED=true` skips order execution, keeps scanning
- $1,250 max total deployed, FOK taker orders only, sorted by resolve time

## Architecture

See [ARCHITECTURE.md](ARCHITECTURE.md) for the complete system diagram.

| Component | Location |
|-----------|----------|
| Bot | ECS Fargate, eu-west-1 |
| Dashboard | Lambda + CloudFront, eu-west-1 |
| Storage | DynamoDB, eu-west-1 |
| Models | S3 eu-west-1 (LightGBM BTC/ETH/SOL live-loaded for 5m; retrain pipeline supports XRP_5m when enough data exists) |
| AI | Bedrock (Haiku + Sonnet 4), eu-west-1 |
| Secrets | AWS Secrets Manager, eu-west-1 |

Deployments use `scripts/deploy_aws.sh`, which now registers a fresh ECS task definition revision before forcing the service deployment.
The ECS task definition now maps both `WATCH_PAIRS` and `DATA_COLLECTION_MODE` from Secrets Manager.

## Safety Guards

| Guard | Value |
|-------|-------|
| V2 per-asset notional cap | $50/window based on executable USD notional (`reserved_open_order_usd + filled_position_cost_usd`) |
| V2 open position | 70/30 to 50/50 main/hedge split (LGBM-driven) |
| V2 accounting basis | `actual_notional_usd = actual_shares * actual_price`, never raw target USD or share count |
| V2 stale repricing | every 1s tick, stale after 6s by default, 1c price tolerance |
| V2 recycle start | T+45s, payout-floor driven |
| V2 buy-only phase | T+180s–T+250s |
| V2 commit | T+250s |
| V2 stop-loss | Only entries > 40¢ down > 25%, T+30–240s only |
| V2 winning side gate | bid > 0.60 accumulation blocked before T+60s |
| Max bet (5min bot) | $10 peak / $5 weak+weekend |
| LightGBM gate (5min) | lgbm_prob >= 0.62 (Scenario C) |
| Max ask (5min bot) | $0.95 ceiling, $0.78/$0.82 default, relaxed with high lgbm |
| Model smoke test | Bot **halts** if models can't load or predict non-0.5 |
| Max ask (opp bot) | $0.94 (data-driven: above loses money) |
| Min ask (opp bot) | $0.85 (data-driven: below 71% WR, -$3.42) |
| Haiku gate (opp Tier 1) | confidence >= 0.80 |
| Haiku gate (opp Tier 2) | confidence >= 0.85 + edge >= 0.15 |
| Sonnet gate (opp Tier 0) | confidence >= 0.85 (devil's advocate) |
| Morning block (opp) | 06-12 UTC skipped (76% WR, -$12.12) |
| Resolve window block | 6-12h resolution skipped (80% WR, -$8.90) |
| Max deployed (opp bot) | $1,250 |
| Dedup | 3-layer (memory + DynamoDB + atomic claim) |
| Rogue task detection | Smoke test on startup |
| Resolution | Polymarket Chainlink only (no Coinbase) |
| Auto-retrain quality gate | New AUC must be >= current AUC - 0.02 |
| ECS deploy safety | register new task definition revision before `update-service` |

## Running

```bash
# Local development
uv run pytest tests/          # 888 tests
uv run python scripts/run.py  # Start 5min bot

# Deploy to AWS
bash scripts/deploy_aws.sh              # Bot (ECS)
bash scripts/deploy_dashboard_lambda.sh  # Dashboard (Lambda)

# Opportunity scanner
PYTHONPATH=src uv run python scripts/opportunity_bot.py
```

## Roadmap / TODO

### Immediate
- Keep refining `BTC_5m` as the control profile until live windows are consistently sane
- Tighten bad-pair and payout-floor recycle rules without reintroducing churn
- Keep weak/sideways windows small, deploy harder only in strong mid-window setups, and spend extra only on the favored side
- Fix runtime Secrets Manager refresh permissions so `EARLY_ENTRY_ENABLED=false` can finish the current window and skip the next one reliably
- Keep retraining healthy across all 8 collection streams; 5m is immediately trainable, 1h will begin as live-only models once enough rows accumulate

### Collection / retrain
- Live trading remains `BTC_5m` only via `PAIRS=BTC_5m`
- Use `WATCH_PAIRS=ETH_5m,SOL_5m,XRP_5m` to collect 5m rows without trading them
- Hourly states (`BTC_1h`, `ETH_1h`, `SOL_1h`, `XRP_1h`) are always tracked for resolution/training-data collection
- Training-data rows now use the actual timeframe (`5m`, `15m`, `1h`) instead of hardcoding `5m`
- Auto-retrain now attempts all 8 streams every 4h:
  - `BTC_5m`, `ETH_5m`, `SOL_5m`, `XRP_5m`
  - `BTC_1h`, `ETH_1h`, `SOL_1h`, `XRP_1h`
- Jon-Becker base data is only used for `5m`; hourly models start as live-data-only until enough rows accumulate

### Overnight / Tomorrow
- Let `BTC_5m` run by itself overnight; do not add `ETH_5m`, `SOL_5m`, or `XRP_5m` yet
- Review the live task definition first thing in the morning to confirm which BTC `5m` tuning actually ran overnight
- Check whether weak / sideways windows stayed tiny and whether strong windows deployed more on the favored side
- Check for any rich-side chasing, one-sided runaway accumulation, or recycle churn
- If BTC looks clean across a solid batch of windows, move next to `ETH_5m`; keep `SOL_5m` as a separate higher-volatility tuning pass
- Wire `put_v2_window` / `put_v2_fill` into the live loop after the overnight review so post-trade analysis stops depending mainly on CloudWatch parsing

### Next 5m rollout
- Add per-pair strategy profiles instead of assuming one `5m` parameter set works everywhere
- Enable `ETH_5m` after `BTC_5m` is stable
- Tune `SOL_5m` separately with a smaller open and stricter rich-side caps
- Wire live `XRP_5m` model loading so `XRP_5m` does not fall back to neutral predictions

### Later timeframes
- Add a separate `1h` profile: small open, gradual two-sided accumulation, no selling, late commit
- Add a separate `15m` profile after `5m` and `1h` are stable
- Keep the execution engine shared, but tune budget curve, rich-side caps, recycle rules, and timing per pair/timeframe

### Infra / data
- Reduce Docker image size and speed up ECS rollout times
- Prevent overlapping service deployments/tasks during live rollouts
- Expand V2 structured logging so post-trade analysis can compare fill quality and payout-floor pressure by pair/profile

## Tech Stack

- Python 3.12, asyncio, uv
- py-clob-client (Polymarket CLOB SDK)
- Coinbase WebSocket (250ms price ticks)
- LightGBM (per-pair classifiers, trained on 22K+ Jon-Becker/live windows)
- AWS: ECS, DynamoDB, Bedrock (Haiku + Sonnet 4), Lambda, CloudFront, Secrets Manager
- structlog (JSON logging → CloudWatch)
- Auto-retrain every 4h (Jon-Becker base + live windows, AUC quality gate)
