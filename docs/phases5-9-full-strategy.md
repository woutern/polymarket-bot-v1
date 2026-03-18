
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
