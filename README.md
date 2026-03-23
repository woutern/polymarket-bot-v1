# Polymarket Trading Bot

Algorithmic trading system for Polymarket prediction markets.

**Status:** Deployed on AWS ECS (eu-west-1)
**Dashboard:** https://d2rj5lnnfnptd.cloudfront.net/
**Tests:** 620 passing
**V2 both-sides:** gated by `EARLY_ENTRY_ENABLED=true` and pair-level `PAIRS`

## Three Trading Strategies

### 1. V2 Both-Sides (K9-style, gated rollout)
Modelled on K9 trader: 13.4% ROI, 69% GP rate across 3,500 real trades.

- **Scope:** 5-minute windows only, enabled per pair via `PAIRS` and currently used for controlled early-entry rollout
- **Phase 1 — Open (T+5s):** post GTC main + hedge orders after the book forms
  - Executable size is based on whole shares with a 5-share minimum
  - Every post logs `actual_notional_usd` and `actual_shares`
- **Phase 2 — Confirm (T+15–20s):** rerun LightGBM
  - If direction flips, swap main/hedge labels and continue accumulating on the new side
  - If direction confirms, post one more main-side GTC sized at 20% of per-asset budget
- **Phase 3 — Accumulate (every 3s, T+5s to T+270s):** maintain GTC ladders on both sides
  - `bid <= 0.15`: 9 levels `[0,1,2,3,4,5,6,7,8¢]` at `$0.35` target size
  - `0.15 < bid <= 0.35`: 7 levels `[0,1,2,3,4,5,6¢]` at `$0.25`
  - `0.35 < bid <= 0.60`: 5 levels `[0,1,2,3,5¢]` at `$0.20`
  - `bid > 0.60`: 3 levels `[0,1,3¢]` at `$0.15`
- **Active order recycling / repricing:** every accumulation tick inspects open orders on both sides
  - Stale orders are cancelled when they are older than `early_entry_reprice_stale_after_seconds` (default `6s`) and no longer within `early_entry_reprice_price_tolerance` (default `1c`) of the desired ladder
  - Cancelled orders release reserved budget immediately and the ladder is reposted at current prices
  - Key logs: `stale_order_cancelled`, `budget_released`, `repriced_order_posted`, `v2_reprice_cycle`
- **Budget / accounting:** the hard cap is based on executable USD notional, not share count
  - `actual_notional_usd = actual_shares * actual_price`
  - `reserved_open_order_usd + filled_position_cost_usd + new_actual_notional_usd <= $50` per asset per window
  - Open-order reserve is released on fill, cancel, reject, and expiry
- **Paper verification path:** paper mode exposes an in-memory order client so V2 can exercise post/fill/cancel/release accounting without real-money execution
- **Sell-rotate:** still optional and gated by `EARLY_ENTRY_ROTATE_ENABLED`; not part of the repricing patch

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
| Models | S3 eu-west-1 (LightGBM BTC/SOL, trained on 22K windows) |
| AI | Bedrock (Haiku + Sonnet 4), eu-west-1 |
| Secrets | AWS Secrets Manager, eu-west-1 |

Deployments use `scripts/deploy_aws.sh`, which now registers a fresh ECS task definition revision before forcing the service deployment.

## Safety Guards

| Guard | Value |
|-------|-------|
| V2 per-asset notional cap | $50/window based on executable USD notional (`reserved_open_order_usd + filled_position_cost_usd`) |
| V2 open position | 70/30 to 50/50 main/hedge split (LGBM-driven) |
| V2 accounting basis | `actual_notional_usd = actual_shares * actual_price`, never raw target USD or share count |
| V2 stale repricing | every 3s tick, stale after 6s by default, 1c price tolerance |
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
uv run pytest tests/          # 620 tests
uv run python scripts/run.py  # Start 5min bot

# Deploy to AWS
bash scripts/deploy_aws.sh              # Bot (ECS)
bash scripts/deploy_dashboard_lambda.sh  # Dashboard (Lambda)

# Opportunity scanner
PYTHONPATH=src uv run python scripts/opportunity_bot.py
```

## Tech Stack

- Python 3.12, asyncio, uv
- py-clob-client (Polymarket CLOB SDK)
- Coinbase WebSocket (250ms price ticks)
- LightGBM (per-pair classifiers, trained on 22K Jon-Becker windows, AUC 0.73/0.77)
- AWS: ECS, DynamoDB, Bedrock (Haiku + Sonnet 4), Lambda, CloudFront, Secrets Manager
- structlog (JSON logging → CloudWatch)
- Auto-retrain every 4h (Jon-Becker base + live windows, AUC quality gate)
