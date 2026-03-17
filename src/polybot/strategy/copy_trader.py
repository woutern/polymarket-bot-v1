"""Copy trading: monitor profitable Polymarket wallets and follow their trades.

Watches on-chain activity of known profitable wallets via Polymarket's
public API. When a tracked wallet enters a position, we follow.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import structlog

from polybot.models import Direction, Signal, SignalSource

logger = structlog.get_logger()

CLOB_URL = "https://clob.polymarket.com"
GAMMA_URL = "https://gamma-api.polymarket.com"

# Profitable wallets to track (add more as discovered)
TRACKED_WALLETS = [
    # Awful-Alfalfa: $245K profit, 11567 trades, latency arb specialist
    "0x1f0ebc543B2d411f66947041625c0Aa1ce61CF86",
]


class CopyTrader:
    """Monitors tracked wallets for new positions and generates follow signals."""

    def __init__(self, wallets: list[str] | None = None, poll_interval: int = 15):
        self.wallets = wallets or TRACKED_WALLETS
        self.poll_interval = poll_interval
        self._last_positions: dict[str, set[str]] = {w: set() for w in self.wallets}
        self._running = False
        self.pending_signals: list[Signal] = []

    async def start(self):
        """Poll tracked wallets for new positions."""
        self._running = True
        logger.info("copy_trader_started", wallets=len(self.wallets))

        while self._running:
            for wallet in self.wallets:
                try:
                    await self._check_wallet(wallet)
                except Exception as e:
                    logger.warning("copy_check_failed", wallet=wallet[:10], error=str(e))

            await asyncio.sleep(self.poll_interval)

    async def stop(self):
        self._running = False

    async def _check_wallet(self, wallet: str):
        """Check a wallet's current positions for new entries."""
        async with httpx.AsyncClient(timeout=10) as client:
            # Get wallet's open positions via Polymarket profile API
            resp = await client.get(
                f"{GAMMA_URL}/query",
                params={
                    "query": wallet,
                    "type": "profile",
                },
            )
            if resp.status_code != 200:
                return

            # Also check recent trades
            resp = await client.get(
                f"{CLOB_URL}/trades",
                params={
                    "maker_address": wallet,
                    "limit": 20,
                },
            )
            if resp.status_code != 200:
                return

            trades = resp.json()
            if not isinstance(trades, list):
                return

            known = self._last_positions[wallet]
            for trade in trades:
                trade_id = trade.get("id", "")
                if trade_id in known:
                    continue

                # New trade detected
                known.add(trade_id)
                asset_id = trade.get("asset_id", "")
                side = trade.get("side", "")
                price = float(trade.get("price", 0))
                size = float(trade.get("size", 0))

                if not side or price <= 0:
                    continue

                # Determine direction from side
                direction = Direction.UP if side.upper() == "BUY" else Direction.DOWN

                # Only follow if the entry price looks reasonable (not buying at $0.95+)
                if price > 0.85:
                    continue

                logger.info(
                    "copy_signal_detected",
                    wallet=wallet[:10],
                    asset_id=asset_id[:10],
                    side=side,
                    price=round(price, 4),
                    size=round(size, 2),
                )

                signal = Signal(
                    source=SignalSource.WALLET,
                    direction=direction,
                    model_prob=0.65,  # Moderate confidence in whale
                    market_price=price,
                    ev=(0.65 - price) / price if price > 0 else 0,
                    asset="BTC",  # Will be resolved from asset_id
                )
                self.pending_signals.append(signal)

    def drain_signals(self) -> list[Signal]:
        """Get and clear pending signals."""
        signals = self.pending_signals.copy()
        self.pending_signals.clear()
        return signals
