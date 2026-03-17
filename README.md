# Polymarket Multi-Asset Trading Bot

Automated trading bot for Polymarket BTC/ETH/SOL 5-minute and 15-minute Up/Down prediction markets. Runs three strategies in priority order: Latency Arbitrage, Classic Arbitrage, and Bayesian Directional.

---

## What It Does

Polymarket lists binary prediction markets such as "Will BTC be higher in 5 minutes?" (YES/NO). This bot:

1. Streams real-time prices from Coinbase WebSocket (BTC-USD, ETH-USD, SOL-USD).
2. Polls the Polymarket orderbook for the corresponding Up/Down markets.
3. Fires buy orders when one of the three strategies detects an edge.
4. Resolves positions at window close and tracks P&L.

Markets supported:
- BTC, ETH, SOL — 5-minute windows (300 s)
- BTC, ETH, SOL — 15-minute windows (900 s)

---

## Strategies

### 1. Latency Arbitrage (primary)

Polymarket market makers take 200–800 ms to reprice after a Coinbase tick. When BTC moves ≥ 0.03% from the window open price but the YES token is still priced below $0.65, we buy before the market reprices.

- Fires every 250 ms tick
- Signal source: `directional` (latency variant)
- Parameters: `min_move_pct`, `max_cheap_price`, `min_profit_margin`

### 2. Classic Arbitrage

When `YES_ask + NO_ask < 1.00`, buying both sides locks in a guaranteed profit. The bot detects this and places both legs.

- Signal source: `arbitrage`
- Runs every tick, skipped if already traded this window

### 3. Bayesian Directional

Uses a Bayesian updater seeded with historical base rates (% move → P(UP) table built from Coinbase candles). During the entry zone (configurable seconds before close), if `model_prob - market_price > min_ev_threshold` and the market is not already priced in (`yes_ask < max_market_price`), a directional bet is placed.

- Signal source: `directional`
- Base rates loaded from `data/candles/btc_usd_1min.parquet` or S3

---

## Architecture

```
Coinbase WS ──────────────────────────────────────┐
  (BTC-USD, ETH-USD, SOL-USD ticks @ 250ms)       │
                                                    ▼
                                         ┌──────────────────┐
                                         │   TradingLoop    │
                                         │  (core/loop.py)  │
                                         └────────┬─────────┘
                                                  │
                           ┌──────────────────────┼──────────────────────┐
                           ▼                      ▼                      ▼
                   WindowTracker           Strategies              CopyTrader
                   (per asset×dur)   1. check_latency_arb      (wallet monitor)
                   clock.py          2. check_arbitrage
                   window_tracker.py 3. generate_directional_signal
                           │                      │
                           └──────────────────────┘
                                                  │ Signal
                                                  ▼
                                         ┌─────────────────┐
                                         │   Risk Manager  │
                                         │  circuit breaker│
                                         │  Kelly sizing   │
                                         └────────┬────────┘
                                                  │
                               ┌──────────────────┴──────────────────┐
                               ▼                                      ▼
                       PaperTrader                             LiveTrader
                       (paper mode)                      (py-clob-client FOK)
                               │                                      │
                               └──────────────────┬──────────────────┘
                                                  ▼
                                         ┌─────────────────┐
                                         │    Database      │
                                         │  SQLite (local)  │
                                         │  DynamoDB (AWS)  │
                                         └─────────────────┘
                                                  │
                                                  ▼
                                         ┌─────────────────┐
                                         │   Dashboard      │
                                         │  FastAPI + HTML  │
                                         │  localhost:8888  │
                                         └─────────────────┘
```

---

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- A Polymarket account with funds deposited via the Polymarket UI

### Install

```bash
uv sync
```

### Configure

```bash
cp .env.example .env
```

Edit `.env` and fill in your credentials (see [Config Variables](#config-variables) below).

### Derive API Keys

Run once after setting `POLYMARKET_PRIVATE_KEY` (and `POLYMARKET_FUNDER` for proxy wallets):

```bash
uv run python scripts/derive_api_keys.py
```

Paste the output `POLYMARKET_API_KEY`, `POLYMARKET_API_SECRET`, `POLYMARKET_API_PASSPHRASE` into `.env`.

### Run (paper mode)

```bash
uv run python scripts/run.py
```

### Dashboard

In a second terminal:

```bash
uv run python scripts/dashboard.py
```

Open http://localhost:8888

---

## Paper vs Live Mode

Set `MODE=paper` (default) or `MODE=live` in `.env`.

- **Paper**: `PaperTrader` simulates fills at the current ask. No real orders. Full P&L tracking in SQLite.
- **Live**: `LiveTrader` places real FOK (Fill-or-Kill) orders via `py-clob-client`. Orders either fill immediately or are cancelled — no hanging limit orders.

---

## Backfilling Historical Data

```bash
# Backfill Coinbase 1-min candles (used for base rate table)
uv run python scripts/backfill_coinbase.py

# Backfill Polymarket market metadata
uv run python scripts/backfill_polymarket.py
```

## Validate Edge

```bash
uv run python scripts/validate_edge.py
```

---

## AWS Deployment

The bot runs on ECS Fargate (eu-west-1). DynamoDB mirrors all trades and windows; the dashboard can read from DynamoDB when no local SQLite file is present.

### Build and Deploy

```bash
bash scripts/deploy_aws.sh
```

This script:
1. Authenticates to ECR (`688567279867.dkr.ecr.eu-west-1.amazonaws.com/polymarket-bot`)
2. Builds a `linux/amd64` Docker image
3. Pushes to ECR
4. Forces a new ECS deployment on the `polymarket-bot` cluster

### Manual Docker build

```bash
docker build --platform linux/amd64 -t polymarket-bot .
docker run --env-file .env polymarket-bot
```

### Required AWS Resources

| Resource | Name |
|---|---|
| ECS Cluster | `polymarket-bot` |
| ECS Service | `polymarket-bot-service` |
| ECR Repository | `polymarket-bot` |
| DynamoDB Table | `polymarket-bot-trades` (partition key: `id`) |
| DynamoDB Table | `polymarket-bot-windows` (partition key: `slug`) |
| DynamoDB GSI | `window-index` on `trades` table (key: `window_slug`) |
| CloudWatch Log Group | `/polymarket-bot` |
| S3 Bucket | `polymarket-bot-data-688567279867` |

---

## Config Variables

All variables are set in `.env` (or as environment variables for Docker/ECS).

| Variable | Default | Description |
|---|---|---|
| `POLYMARKET_PRIVATE_KEY` | — | L1 EOA private key (hex, with `0x`) |
| `POLYMARKET_API_KEY` | — | L2 API key (from `derive_api_keys.py`) |
| `POLYMARKET_API_SECRET` | — | L2 API secret |
| `POLYMARKET_API_PASSPHRASE` | — | L2 API passphrase |
| `POLYMARKET_CHAIN_ID` | `137` | Polygon mainnet |
| `POLYMARKET_FUNDER` | — | Proxy/funder wallet address (from Polymarket UI settings). Required for standard web accounts. |
| `MODE` | `paper` | `paper` or `live` |
| `BANKROLL` | `1000.0` | Starting bankroll in USD |
| `MAX_POSITION_PCT` | `0.01` | Max fraction of bankroll per trade (1%) |
| `DAILY_LOSS_CAP_PCT` | `0.05` | Circuit breaker: halt trading if daily P&L drops below -5% of bankroll |
| `KELLY_FRACTION` | `0.25` | Fraction of full Kelly criterion to use (quarter-Kelly) |
| `MIN_EV_THRESHOLD` | `0.05` | Minimum expected value to trigger a directional trade |
| `DIRECTIONAL_ENTRY_SECONDS` | `60` | Enter directional trades when ≤ this many seconds remain in window |
| `DIRECTIONAL_MIN_MOVE_PCT` | `0.08` | Minimum % price move to trigger directional strategy |
| `MAX_MARKET_PRICE` | `0.85` | Skip trades if ask > this (market already priced in) |
| `ASSETS` | `BTC,ETH,SOL` | Comma-separated list of assets to trade |
| `WINDOW_DURATIONS` | `5m,15m` | Comma-separated window durations (`5m`, `15m`) |
| `AI_SIGNAL_ENABLED` | `true` | Enable AI/news signal blending |
| `AI_BASE_RATE_WEIGHT` | `0.6` | Weight of Bayesian base rate in ensemble |
| `AI_SIGNAL_WEIGHT` | `0.4` | Weight of AI/news signal in ensemble |
| `AI_MIN_CONFIDENCE` | `0.6` | Minimum confidence to act on AI signal |
| `NEWS_POLL_INTERVAL` | `30` | Seconds between news feed polls |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`) |

---

## Project Structure

```
src/polybot/
  config.py              # Pydantic settings (loaded from .env)
  models.py              # Core dataclasses: Window, Signal, TradeRecord
  core/
    clock.py             # Window boundary helpers
    loop.py              # Main async orchestrator (TradingLoop)
    logging.py           # structlog setup
  execution/
    live_trader.py       # Real order placement via py-clob-client
    paper_trader.py      # Simulated fills
  feeds/
    coinbase_ws.py       # Coinbase WebSocket price feed
    coinbase_rest.py     # Coinbase REST candle fetcher
    polymarket_rest.py   # Polymarket orderbook / market data
    polymarket_ws.py     # Polymarket WebSocket feed
    news_feed.py         # News/sentiment feed
  market/
    balance_checker.py   # On-chain USDC + Polymarket portfolio value
    market_resolver.py   # Resolve market condition IDs
    window_tracker.py    # Window state machine (OPEN / ENTRY_ZONE / CLOSED)
  risk/
    manager.py           # Daily P&L tracking, circuit breaker, Kelly sizing
  storage/
    db.py                # SQLite (aiosqlite)
    dynamo.py            # DynamoDB mirror (best-effort)
  strategy/
    latency.py           # Latency arbitrage signal
    arbitrage.py         # Classic YES+NO arb signal
    bayesian.py          # Bayesian P(UP) updater
    directional.py       # Directional signal generator
    base_rate.py         # Historical base rate table
    sizing.py            # Kelly position sizing
    ensemble.py          # Signal ensemble blending
    copy_trader.py       # Wallet copy trading monitor
    ai_signal.py         # AI/news signal

scripts/
  run.py                 # Bot entry point
  dashboard.py           # FastAPI dashboard (port 8888)
  derive_api_keys.py     # One-time L2 credential derivation
  backfill_coinbase.py   # Historical candle backfill
  backfill_polymarket.py # Historical market data backfill
  validate_edge.py       # Edge validation / backtesting
  deploy_aws.sh          # ECR push + ECS force-deploy
```

---

## Risk Warning

This bot places real money on prediction markets. Prediction markets can resolve against you even with a positive-EV strategy due to variance, stale orderbook data, fills at unfavorable prices, or bugs. Start with `MODE=paper` and a small bankroll. The circuit breaker (`DAILY_LOSS_CAP_PCT`) limits daily losses but does not protect against individual large losses or bugs. Use at your own risk.
