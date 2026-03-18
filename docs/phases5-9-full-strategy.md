
# Phases 5–9: Strategy Upgrade

Saved from Wouter's instructions. Execute after dashboard rewrite is validated.
See full plan in Claude Code conversation context.

## Summary:
- Phase 5: RTDS feed + oracle lag signal (Black-Scholes, Tier A entry) — DONE
- Phase 6: Claude regime classifier (replaces per-trade Bedrock, adds Tier B entry)
- Phase 7: LightGBM + River + conformal prediction (40+ features, Thompson Sampling bandit)
- Phase 8: Kelly sizing + SPRT + meta-learning loop
- Phase 9: Edge Analytics dashboard (page 4, 8 sections)
- Phase 10: 48h paper validation + go/no-go checklist

## Amendments from Wouter (2026-03-18):

### Phase 6 amendment: Add Regime F (OVERREACTION)
- Condition: move >0.3% in first 120s of window AND yes_ask >0.75
- Bias: FADE (bet OPPOSITE direction to the move)
- Tier B fires in reverse direction under Regime F
- Rationale: market overprices early momentum, mean-reversion opportunity

### Phase 7 amendment: Replace realized_vol_5m with GARCH(1,1)
- Do NOT use simple realized_vol_5m in feature engineering
- Use GARCH(1,1) volatility estimate instead
- GARCH captures volatility clustering better than rolling window std
- Implementation: arch library (pip install arch), fit on 1-min returns

### Phase 7 amendment: EV formula and capital reserve
1. Replace current EV formula everywhere with:
   ```
   payout = 1 / market_price
   ev = (model_prob - market_price) * payout
   MIN_EV = 0.10 (raise from current 0.06)
   ```
   At $0.60 entry with 97% WR: ev = (0.97-0.60)*(1/0.60) = +0.62 per dollar
   At $0.95 entry with 97% WR: ev = (0.97-0.95)*(1/0.95) = +0.02 per dollar

2. Add 30% capital reserve rule:
   - Never commit more than 70% of bankroll across all open positions combined
   - Check before every trade: sum(open_position_sizes) + new_trade_size <= bankroll * 0.70
   - If breached, skip trade with rejection_reason="capital_reserve"

### Backtest finding (2026-03-18): DIRECTION IS SOLVED, PRICE IS THE PROBLEM
30-day BTC 5m backtest (4,065 signals):
- T-60s entry: 97.3% WR but ask is $0.95+ (EV: +$0.02)
- T-120s entry: 96.2% WR and ask should be $0.55-0.65 (EV: +$0.62)
- T-180s entry: 92.3% WR
- T-240s entry: 86.3% WR (cheapest ask but lower WR)
Conclusion: Tier B (T-240s to T-120s) is the PRIMARY strategy.
LightGBM goal: predict at T-240s whether move will exceed 0.08% by T-60s.
