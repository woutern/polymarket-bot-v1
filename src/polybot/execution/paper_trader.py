"""Paper trader: simulated fills against live orderbook."""

from __future__ import annotations

import time
import uuid

import structlog

from polybot.feeds.polymarket_rest import get_market_outcome
from polybot.models import Direction, OrderbookSnapshot, Signal, TradeRecord
from polybot.risk.manager import RiskManager
from polybot.storage.db import Database
from polybot.strategy.sizing import compute_size

logger = structlog.get_logger()


class PaperTrader:
    """Simulates trade execution using orderbook data.

    Fills at the best ask (for buys). Tracks positions and resolves
    them when the window closes.
    """

    def __init__(self, risk: RiskManager, db: Database):
        self.risk = risk
        self.db = db
        self.open_positions: list[TradeRecord] = []

    async def execute(self, signal: Signal) -> TradeRecord | None:
        """Execute a paper trade from a signal."""
        if not self.risk.can_trade():
            logger.warning("paper_trade_blocked", reason="circuit_breaker")
            return None

        # Dedup guard: only one trade per window_slug
        if any(p.window_slug == signal.window_slug for p in self.open_positions):
            logger.warning("paper_trade_dedup", slug=signal.window_slug)
            return None

        # Sanity check: reject suspiciously cheap fills (orderbook not yet initialized)
        MIN_FILL_PRICE = 0.20
        if signal.market_price < MIN_FILL_PRICE:
            logger.warning(
                "paper_trade_price_too_low",
                market_price=signal.market_price,
                min_allowed=MIN_FILL_PRICE,
                slug=signal.window_slug,
            )
            return None

        # Compute position size
        size = compute_size(
            model_prob=signal.model_prob,
            market_price=signal.market_price,
            bankroll=self.risk.bankroll,
            kelly_mult=0.25,
            max_position_pct=self.risk.max_position_pct,
            min_trade_usd=self.risk.min_trade_usd,
            max_trade_usd=self.risk.max_trade_usd,
        )
        if size <= 0:
            return None

        side = "YES" if signal.direction == Direction.UP else "NO"

        trade = TradeRecord(
            id=str(uuid.uuid4())[:8],
            timestamp=time.time(),
            window_slug=signal.window_slug,
            source=signal.source.value,
            direction=signal.direction.value,
            side=side,
            price=signal.market_price,
            size_usd=size,
            fill_price=signal.market_price,  # Paper: fill at ask
            asset=signal.asset,
            mode="paper",
            p_bayesian=signal.p_bayesian,
            p_ai=signal.p_ai,
            p_final=signal.model_prob,
            pct_move=signal.pct_move,
            seconds_remaining=signal.seconds_remaining,
            ev=signal.ev,
            outcome_source="coinbase_inferred",
        )

        self.open_positions.append(trade)

        await self.db.insert_trade(self._trade_to_dict(trade))

        logger.info(
            "paper_trade_executed",
            id=trade.id,
            side=trade.side,
            price=trade.price,
            size=trade.size_usd,
            source=trade.source,
            slug=trade.window_slug,
            p_bayesian=round(trade.p_bayesian, 4),
            p_ai=round(trade.p_ai, 4) if trade.p_ai is not None else None,
            p_final=round(trade.p_final, 4),
            pct_move=round(trade.pct_move, 4),
            ev=round(trade.ev, 4),
        )
        return trade

    async def resolve_window(self, window_slug: str, went_up: bool):
        """Resolve all open positions for a completed window (Coinbase-based)."""
        to_resolve = [p for p in self.open_positions if p.window_slug == window_slug]

        for trade in to_resolve:
            won = (trade.side == "YES" and went_up) or (trade.side == "NO" and not went_up)
            if won:
                # Bought at `price`, pays out $1 per share
                shares = trade.size_usd / trade.fill_price
                pnl = shares * (1.0 - trade.fill_price)
            else:
                pnl = -trade.size_usd

            trade.pnl = pnl
            trade.resolved = True
            self.risk.record_trade(pnl)

            await self.db.insert_trade(self._trade_to_dict(trade))

            logger.info(
                "paper_trade_resolved",
                id=trade.id,
                side=trade.side,
                won=pnl > 0,
                pnl=round(pnl, 4),
                slug=trade.window_slug,
                outcome_source=trade.outcome_source,
            )

        self.open_positions = [p for p in self.open_positions if p.window_slug != window_slug]

    async def verify_and_update(self, window_slug: str):
        """Query Polymarket Gamma API to verify actual outcome and update trade records.

        Called 30s after window close — overrides Coinbase-inferred resolution
        with the authoritative Chainlink-based result from Polymarket.
        """
        try:
            winner, source = await get_market_outcome(window_slug)
            if winner is None:
                logger.info("outcome_verification_pending", slug=window_slug)
                return

            trades = await self.db.get_trades(window_slug=window_slug)
            for t in trades:
                if not t.get("resolved"):
                    continue
                side = t.get("side", "")
                correct = (side == winner)
                await self.db.update_trade_outcome(
                    trade_id=t["id"],
                    polymarket_winner=winner,
                    correct_prediction=correct,
                    outcome_source=source,
                )
                logger.info(
                    "outcome_verified",
                    id=t["id"],
                    slug=window_slug,
                    polymarket_winner=winner,
                    our_side=side,
                    correct=correct,
                    source=source,
                )
        except Exception as e:
            logger.warning("verify_and_update_failed", slug=window_slug, error=str(e))

    def _trade_to_dict(self, trade: TradeRecord) -> dict:
        return {
            "id": trade.id,
            "timestamp": trade.timestamp,
            "window_slug": trade.window_slug,
            "source": trade.source,
            "direction": trade.direction,
            "side": trade.side,
            "price": trade.price,
            "size_usd": trade.size_usd,
            "fill_price": trade.fill_price,
            "pnl": trade.pnl,
            "resolved": int(trade.resolved),
            "mode": trade.mode,
            "asset": trade.asset,
            "p_bayesian": trade.p_bayesian,
            "p_ai": trade.p_ai,
            "p_final": trade.p_final,
            "pct_move": trade.pct_move,
            "seconds_remaining": trade.seconds_remaining,
            "ev": trade.ev,
            "outcome_source": trade.outcome_source,
            "polymarket_winner": trade.polymarket_winner,
            "correct_prediction": None if trade.correct_prediction is None else int(trade.correct_prediction),
        }
