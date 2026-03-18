"""Paper trader: simulated fills against live orderbook."""

from __future__ import annotations

import time
import uuid

import structlog

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
        )
        if size <= 0:
            return None

        # Determine which side to buy
        if signal.source.value == "arbitrage":
            # For arbitrage, we'd buy both YES and NO — simplified here
            side = "YES"
        else:
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
        )

        self.open_positions.append(trade)

        await self.db.insert_trade(
            {
                "id": trade.id,
                "timestamp": trade.timestamp,
                "window_slug": trade.window_slug,
                "source": trade.source,
                "direction": trade.direction,
                "side": trade.side,
                "price": trade.price,
                "size_usd": trade.size_usd,
                "fill_price": trade.fill_price,
                "pnl": None,
                "resolved": 0,
                "mode": "paper",
                "asset": trade.asset,
            }
        )

        logger.info(
            "paper_trade_executed",
            id=trade.id,
            side=trade.side,
            price=trade.price,
            size=trade.size_usd,
            source=trade.source,
            slug=trade.window_slug,
        )
        return trade

    async def resolve_window(self, window_slug: str, went_up: bool):
        """Resolve all open positions for a completed window."""
        to_resolve = [p for p in self.open_positions if p.window_slug == window_slug]

        for trade in to_resolve:
            # Did we win?
            if trade.source == "arbitrage":
                # Arbitrage always wins: profit = 1.0 - total_cost
                pnl = (1.0 - trade.price) * (trade.size_usd / trade.price)
            else:
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

            await self.db.insert_trade(
                {
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
                    "resolved": 1,
                    "mode": "paper",
                    "asset": trade.asset,
                }
            )

            logger.info(
                "paper_trade_resolved",
                id=trade.id,
                side=trade.side,
                won=pnl > 0,
                pnl=round(pnl, 4),
                slug=trade.window_slug,
            )

        self.open_positions = [p for p in self.open_positions if p.window_slug != window_slug]
