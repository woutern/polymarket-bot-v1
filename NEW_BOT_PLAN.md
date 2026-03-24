# New Bot — Build Plan
> Single source of truth for the full rebuild. Read this before touching code.
> Updated: 2026-03-24

---

## What We're Building

A clean, testable, multi-pair Polymarket trading bot.

- Strategy: MarketMakerStrategy — buy both sides, recycle losing side, accumulate winning side
- Goal: combined_avg < $1.00 → guaranteed profit at resolution
- Budget: **$80 per window per pair** (start here, tune later)
- Pairs: BTC_5m first. Architecture supports BTC/ETH/SOL/XRP × 5m/1h from day 1.
- Live control: pause new windows without redeploy (env flag or DynamoDB flag)

---

## Design Principles

1. **Each file does one thing.** No 6000-line loop.py. Split by concern.
2. **Every function is pure or has one side effect.** Easy to test, easy to mock.
3. **No implicit state.** All state passed explicitly. No hidden globals.
4. **LLM-readable.** Short functions, clear names, docstrings on every public method.
5. **Fast.** Async everywhere. No blocking I/O in the tick loop.
6. **Debuggable.** Structured logs on every decision. DynamoDB tick log for replay.
7. **Safe by default.** Disable new windows via flag, not redeploy. Hard budget caps enforced in engine, not strategy.

---

## File Structure

```
src/polybot/
├── core/
│   ├── engine.py          # Tick loop: calls strategy, executes orders, logs state
│   ├── window.py          # Window lifecycle: open, accumulate, commit, resolve
│   ├── position.py        # Position state: shares, cost, avg, pnl, payout floor
│   └── controls.py        # Runtime flags: pause_new_windows, kill_switch (DynamoDB or env)
│
├── strategy/
│   ├── base.py            # StrategyAction, MarketState (shared types)
│   ├── profile.py         # StrategyProfile dataclass (per-pair config)
│   ├── profiles.py        # Pre-built profiles: BTC_5M, ETH_5M, SOL_5M, XRP_5M, BTC_1H …
│   ├── market_maker.py    # MarketMakerStrategy (BTC_5m, ETH_5m)
│   └── accumulate_only.py # AccumulateOnlyStrategy (SOL, XRP, 1h)
│
├── execution/
│   ├── order_client.py    # Thin wrapper: post_order, cancel_order, get_order_status
│   ├── paper_client.py    # In-memory paper trading (identical interface)
│   └── repricer.py        # Stale order detection and repricing (6s, 1c tolerance)
│
├── feeds/
│   ├── orderbook.py       # Polymarket CLOB orderbook (yes_bid, no_bid per second)
│   └── price.py           # Coinbase/BTC price feed (for model features)
│
├── storage/
│   ├── tick_log.py        # DynamoDB tick-by-tick log (used for replay + model training)
│   ├── window_log.py      # DynamoDB per-window summary
│   └── position_store.py  # DynamoDB position state (for crash recovery)
│
├── models/
│   ├── loader.py          # Load LightGBM from S3 via SSM path
│   └── predictor.py       # predict(pair, features) → prob_up
│
└── smoke/
    ├── checks.py          # All smoke test checks (connectivity, model, creds, rogue task)
    └── runner.py          # Run all checks at startup, halt on critical failure
```

---

## Runtime Control (no redeploy needed)

### Pause new windows
Set `PAUSE_NEW_WINDOWS=true` in AWS Secrets Manager or a DynamoDB control table.

Bot checks this flag at the start of each new window:
- If paused: do not enter new window, finish current window normally
- Current window is managed to completion (commit + hold)
- No orders cancelled mid-window

### Kill switch
Set `KILL_SWITCH=true` in Secrets Manager.
Bot exits cleanly after current window closes (no forced mid-window exit).

### How it works in code
```python
# core/controls.py
class BotControls:
    def refresh(self): ...         # reads from Secrets Manager / DynamoDB, max once/60s
    def pause_new_windows(self) -> bool: ...
    def kill_switch(self) -> bool: ...
```

The engine calls `controls.refresh()` once per window open. No redeploy needed.

---

## Budget

| Setting | Value |
|---------|-------|
| Budget per window | $80 (set in StrategyProfile) |
| Hard cap enforcement | Engine level (not strategy) |
| Budget scale by confidence | 0.35x (weak) → 1.0x (strong) |
| Effective range | $28–$80 per window |

---

## Multi-Pair Architecture

Each pair gets its own `StrategyProfile`. The engine creates one instance of the right strategy per pair. All pairs share the same execution engine.

```python
PROFILES = {
    "BTC_5m":  (MarketMakerStrategy,    BTC_5M_PROFILE),
    "ETH_5m":  (MarketMakerStrategy,    ETH_5M_PROFILE),
    "SOL_5m":  (AccumulateOnlyStrategy, SOL_5M_PROFILE),
    "XRP_5m":  (AccumulateOnlyStrategy, XRP_5M_PROFILE),
    "BTC_1h":  (AccumulateOnlyStrategy, BTC_1H_PROFILE),
    "ETH_1h":  (AccumulateOnlyStrategy, ETH_1H_PROFILE),
    "SOL_1h":  (AccumulateOnlyStrategy, SOL_1H_PROFILE),
    "XRP_1h":  (AccumulateOnlyStrategy, XRP_1H_PROFILE),
}
```

Active pairs controlled by env var `PAIRS=BTC_5m` (comma-separated).

---

## Tick Log (DynamoDB → model training)

Every tick logs:
```json
{
  "window_id": "BTC_5m_20260324_1430",
  "seconds": 120,
  "yes_bid": 0.62,
  "no_bid": 0.38,
  "prob_up": 0.64,
  "up_shares": 80,
  "up_avg": 0.54,
  "down_shares": 40,
  "down_avg": 0.41,
  "net_cost": 59.6,
  "action": "BUY_UP",
  "action_price": 0.62,
  "action_shares": 5,
  "sell_reason": null,
  "direction_source": "market_strong",
  "payout_floor": 40,
  "combined_avg": 0.95
}
```

This feeds:
1. Replay simulator (test any strategy against real ticks)
2. Model training (features already in tick log)
3. Post-mortem debugging

---

## Test Coverage Requirements

### Unit tests (pure functions — no mocks needed)
- `StrategyProfile` defaults and overrides
- `MarketMakerStrategy.on_tick()` — all scenarios: UP, DOWN, REVERSAL, DEAD_SIDE, LATE_DUMP
- `Position` — buy, sell, combined_avg, payout_floor, pnl_if_up/down, is_gp
- `_budget_curve()` at T+0, 60, 120, 180, 250
- `_determine_direction()` — all 6 market edge cases
- `_allocation_split()` — all confidence levels
- `_detect_reversal()` — direction flip, momentum shift, chop detection
- `_decide_sell()` — DEAD_SIDE, REVERSAL, UNFAVORED_RICH, LATE_DUMP (each trigger)
- `_decide_buy()` — balance cap, dying side block, hard cap, no-trade zone
- Payout floor excess sell — PAYOUT_FLOOR trigger
- Hold value calculation per side

### Integration tests (paper client, no live money)
- Full window simulation: UP trend, DOWN trend, REVERSAL, WHIPSAW
- Budget stays within hard cap across all ticks
- Sell-and-rebuy fires correctly after sell
- Stale order repricing fires at T+6s
- Window ends cleanly at T+250 (commit)
- DynamoDB tick log writes correct schema

### Smoke tests (run at startup, halt on failure)
- Polymarket CLOB reachable
- Model loads from S3 (BTC model)
- Model predicts non-0.5 (not fallback)
- API key + private key present
- PAIRS env var set
- No duplicate ECS tasks running
- DynamoDB table accessible
- Controls table readable (pause flag)

### Simulator tests (scripts/replay_simulator.py)
- 500 synthetic windows: all 8 scenario types pass positive EV
- 49 real windows: avg deployed > $60 of $80
- Reversal win rate > 50%
- Whipsaw win rate > 50%
- Overall win rate > 60%

---

## Build Order (strict)

### Phase 1 — Strategy (no live, no AWS)
1. `strategy/base.py` — StrategyAction, MarketState, Position (already done in strategies.py)
2. `strategy/profile.py` — StrategyProfile (already done)
3. `strategy/market_maker.py` — MarketMakerStrategy + payout floor sell (port from strategies.py + add missing mechanics)
4. `strategy/accumulate_only.py` — AccumulateOnlyStrategy (port from strategies.py)
5. Write unit tests for all of the above (100% line coverage target)
6. Run 500 synthetic + 49 real windows via simulator, all pass

### Phase 2 — Engine (paper mode)
7. `core/position.py` — Position state (already in Position class)
8. `core/window.py` — Window lifecycle (open/accumulate/commit/resolve phases)
9. `core/controls.py` — Pause flag, kill switch
10. `execution/paper_client.py` — In-memory order book (fill at bid, instant)
11. `core/engine.py` — Tick loop: calls strategy, executes paper orders, logs
12. Write integration tests against paper client
13. Run 10 paper windows manually, inspect logs

### Phase 3 — Storage + Logging
14. `storage/tick_log.py` — DynamoDB tick log schema
15. `storage/window_log.py` — Window summary schema
16. `storage/position_store.py` — Crash recovery
17. Test: tick log writes, window summary writes, crash recovery reads back correctly

### Phase 4 — Live Execution
18. `execution/order_client.py` — Polymarket CLOB wrapper (post, cancel, status)
19. `execution/repricer.py` — Stale order repricing (6s, 1c)
20. `smoke/checks.py` + `smoke/runner.py` — All smoke tests
21. End-to-end live test: 1 window, $80, BTC_5m, watch logs manually

### Phase 5 — Expand pairs
22. Add ETH_5m (same MarketMakerStrategy, different profile)
23. Add SOL_5m, XRP_5m (AccumulateOnlyStrategy)
24. Add 1h profiles for all 4 pairs
25. Test each pair against simulator before enabling in prod

---

## Missing Mechanics Still to Implement

These are in the old bot but not yet in the new strategy. Implement in Phase 1 step 3.

| # | Mechanic | Description |
|---|----------|-------------|
| A | Payout floor excess sell | Sell shares above min(up, down) when bid > hold_value |
| B | Hold value calculation | hold_value = prob_up × $1.00 per UP share, (1-prob_up) × $1.00 per DOWN share |
| C | Budget boost toward favored side | Transfer budget from under-allocated side when model edge > 10% and favored isn't already ahead |
| D | Anti-churn for unfavored side | Block rebuy of unfavored side above last sell price. Favored side: no restriction. |
| E | Min hedge (keep ≥5 unfavored shares) | Never go to 0 on either side — keeps insurance |
| F | Time-varying expensive-side cap | 82c → 75c → 70c → 65c as window progresses (T+0/60/120/180) |
| G | Bid-dependent ladder sizes | lottery (≤15c): 9 levels; cheap (≤35c): 7; mid (≤60c): 5; winning (>60c): 3 |
| H | Stale order repricing (live only) | Cancel orders stale >6s and repost at current bid, 1c tolerance |
| I | Window loss limit | Stop trading in window if loss > $25; sell losing side, keep winning side |

---

## What NOT to Build (keeps it simple)

- No Bayesian updater (adds complexity, model is already 64% accurate)
- No Binance long/short ratio (external signal, adds API dep, marginal benefit)
- No BAD_PAIR EV sell (was causing churn; K9 says just sell losing side cleanly)
- No pair_risk_limits guard on buys (was blocking 90% of orders in old bot)
- No cross-market exposure caps (one pair at a time for now)
- No session/daily loss cap (window loss limit is sufficient for now)

---

## Go-Live Checklist

- [ ] All unit tests passing (100% strategy coverage)
- [ ] All smoke tests passing
- [ ] 500 synthetic windows: reversal > 50%, whipsaw > 50%, overall > 60%
- [ ] 49 real windows: avg deployed > $60 of $80
- [ ] Budget = $80 in BTC_5M_PROFILE
- [ ] PAIRS=BTC_5m in Secrets Manager
- [ ] PAUSE_NEW_WINDOWS=false in controls
- [ ] Stale repricing wired and tested in paper mode
- [ ] Tick log writing to DynamoDB confirmed
- [ ] Single ECS task verified before first trade
- [ ] Watch first 3 windows manually before leaving unattended
- [ ] Window loss limit active ($25 stop)
