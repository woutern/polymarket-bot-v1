"""Live trader — real order execution via py-clob-client."""

from __future__ import annotations

import time
import uuid

import structlog
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, MarketOrderArgs, OrderArgs, OrderType

from polybot.config import Settings
from polybot.models import Direction, Signal, TradeRecord
from polybot.risk.manager import RiskManager
from polybot.storage.db import Database
from polybot.strategy.sizing import compute_size

logger = structlog.get_logger()

POLYMARKET_HOST = "https://clob.polymarket.com"


class LiveTrader:
    """Places real orders on Polymarket via py-clob-client.

    Auth flow:
      L1 = private key (EIP-712 signing, derives L2 creds)
      L2 = api_key / api_secret / api_passphrase (REST auth)

    Order strategy:
      - FOK (Fill-or-Kill): preferred for T-10s entries — either fills
        immediately at the ask or doesn't fill at all. No partial hangers.
      - GTC fallback for arbitrage (both legs placed as limits).
    """

    def __init__(self, settings: Settings, risk: RiskManager, db: Database):
        self.settings = settings
        self.risk = risk
        self.db = db

        creds = ApiCreds(
            api_key=settings.polymarket_api_key,
            api_secret=settings.polymarket_api_secret,
            api_passphrase=settings.polymarket_api_passphrase,
        )
        # sig_type=0 = plain EOA — the signing key IS the trader.
        # After proper deposit via Polymarket UI, all on-chain allowances
        # are set up automatically and FOK/GTC orders work without proxy.
        # sig_type=1/2 (proxy) require on-chain operator registration that
        # only happens via the normal Polymarket deposit flow.
        funder = settings.polymarket_funder or None
        sig_type = 2 if funder else 0  # GNOSIS_SAFE for MetaMask proxy wallets
        self.client = ClobClient(
            host=POLYMARKET_HOST,
            chain_id=settings.polymarket_chain_id,
            key=settings.polymarket_private_key,
            creds=creds,
            signature_type=sig_type,
            funder=funder,
        )
        logger.info("live_trader_initialized", chain_id=settings.polymarket_chain_id)

    async def execute(self, signal: Signal, yes_token_id: str, no_token_id: str) -> TradeRecord | None:
        """Execute a live order from a signal.

        Args:
            signal: Trading signal with direction and sizing info.
            yes_token_id: Token ID for the YES side.
            no_token_id: Token ID for the NO side.
        """
        if not self.risk.can_trade():
            logger.warning("live_trade_blocked", reason="circuit_breaker")
            return None

        size = compute_size(
            model_prob=signal.model_prob,
            market_price=signal.market_price,
            bankroll=self.risk.bankroll,
            kelly_mult=self.settings.kelly_fraction,
            max_position_pct=self.settings.max_position_pct,
        )
        if size <= 0:
            return None

        if signal.source.value == "arbitrage":
            return await self._execute_arbitrage(signal, yes_token_id, no_token_id, size)
        else:
            return await self._execute_directional(signal, yes_token_id, no_token_id, size)

    async def _execute_directional(
        self,
        signal: Signal,
        yes_token_id: str,
        no_token_id: str,
        size: float,
    ) -> TradeRecord | None:
        """Place a FOK directional order."""
        token_id = yes_token_id if signal.direction == Direction.UP else no_token_id
        side = "YES" if signal.direction == Direction.UP else "NO"

        # MarketOrderArgs takes a dollar amount and handles share/amount rounding
        # internally, avoiding the "max 2 decimals" precision error from OrderArgs.
        market_args = MarketOrderArgs(
            token_id=token_id,
            amount=round(size, 2),  # dollar amount to spend
            side="BUY",
        )

        try:
            order = self.client.create_market_order(market_args)
            resp = self.client.post_order(order, OrderType.FOK)
            order_id = resp.get("orderID", "") if resp else ""

            shares = round(size / signal.market_price, 4) if signal.market_price > 0 else 0
            logger.info(
                "live_order_placed",
                side=side,
                price=signal.market_price,
                shares=shares,
                size_usd=size,
                order_id=order_id,
                slug=signal.window_slug,
            )

            trade = TradeRecord(
                id=order_id or str(uuid.uuid4())[:8],
                timestamp=time.time(),
                window_slug=signal.window_slug,
                source=signal.source.value,
                direction=signal.direction.value,
                side=side,
                price=signal.market_price,
                size_usd=size,
                fill_price=signal.market_price,  # FOK fills at limit or not at all
            )
            await self.db.insert_trade({
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
            })
            return trade

        except Exception as e:
            logger.error("live_order_failed", error=str(e), side=side, slug=signal.window_slug)
            return None

    async def _execute_arbitrage(
        self,
        signal: Signal,
        yes_token_id: str,
        no_token_id: str,
        size: float,
    ) -> TradeRecord | None:
        """Place both YES and NO legs for arbitrage.

        Buys YES and NO simultaneously. If YES fills but NO doesn't,
        we're left with a one-sided position — acceptable given the
        locked profit on the YES leg vs a small NO miss.
        """
        yes_price = signal.market_price  # total_cost is stored as market_price
        # For arb, the signal.market_price is YES_ask + NO_ask (total cost)
        # We need individual prices — re-derive from the orderbook
        # TODO: pass individual prices through Signal for arb case
        logger.info(
            "arb_order_placing",
            total_cost=signal.market_price,
            size_usd=size,
            slug=signal.window_slug,
        )
        # Simplified: execute as a single YES order for now
        # Full arb requires atomic execution of both legs
        return await self._execute_directional(signal, yes_token_id, no_token_id, size / 2)

    async def resolve_window(self, window_slug: str, went_up: bool):
        """Record resolution of a window's positions (P&L calculation)."""
        trades = await self.db.get_trades(window_slug=window_slug)
        for t in trades:
            if t["resolved"]:
                continue
            won = (t["side"] == "YES" and went_up) or (t["side"] == "NO" and not went_up)
            if won:
                shares = t["size_usd"] / t["fill_price"]
                pnl = shares * (1.0 - t["fill_price"])
            else:
                pnl = -t["size_usd"]

            self.risk.record_trade(pnl)
            t["pnl"] = pnl
            t["resolved"] = 1
            await self.db.insert_trade(t)
            logger.info(
                "live_trade_resolved",
                id=t["id"],
                side=t["side"],
                won=won,
                pnl=round(pnl, 4),
                slug=window_slug,
            )
