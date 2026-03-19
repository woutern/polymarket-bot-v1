"""Live trader — real order execution via py-clob-client."""

from __future__ import annotations

import asyncio
import time
import uuid

import structlog
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, CreateOrderOptions, MarketOrderArgs, OrderArgs, OrderType

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
        self._traded_slugs: set[str] = set()  # fast in-memory dedup
        self._trade_lock = asyncio.Lock()  # prevent concurrent same-window trades
        self._dynamo = None  # set externally for DynamoDB dedup

        logger.info("live_trader_initialized", chain_id=settings.polymarket_chain_id)

        # Connectivity test — halt if CLOB is geoblocked
        self._test_clob_connectivity()

    def _test_clob_connectivity(self):
        """Test that CLOB API is reachable (not geoblocked). Halt if 403."""
        import httpx
        try:
            resp = httpx.get(f"{POLYMARKET_HOST}/time", timeout=10)
            if resp.status_code == 403:
                logger.error(
                    "clob_connectivity_blocked",
                    status=403,
                    message="Polymarket CLOB geoblocked from this region. Trading halted.",
                )
                raise RuntimeError("Polymarket CLOB geoblocked (403). Cannot trade from this region.")
            logger.info("clob_connectivity", status="ok", code=resp.status_code)
        except httpx.HTTPError as e:
            logger.error("clob_connectivity_failed", error=str(e))
            raise RuntimeError(f"Cannot reach Polymarket CLOB: {e}")

    async def execute(self, signal: Signal, yes_token_id: str, no_token_id: str, signal_ms: float = 0, bedrock_ms: float = 0) -> TradeRecord | None:
        """Execute a live order from a signal.

        Args:
            signal: Trading signal with direction and sizing info.
            yes_token_id: Token ID for the YES side.
            no_token_id: Token ID for the NO side.
        """
        if not self.risk.can_trade():
            logger.warning("live_trade_blocked", reason="circuit_breaker")
            return None

        # Lock prevents concurrent execution across pairs hitting same window
        async with self._trade_lock:
            # DEDUP: one trade per window_slug (memory first, then DynamoDB)
            if signal.window_slug in self._traded_slugs:
                logger.info("dedup_blocked", slug=signal.window_slug, source="memory")
                return None

            # DynamoDB check — survives restarts, authoritative
            if self._dynamo:
                try:
                    existing = self._dynamo.get_trades_for_window(signal.window_slug)
                    if existing:
                        self._traded_slugs.add(signal.window_slug)
                        logger.info("dedup_blocked", slug=signal.window_slug, source="dynamodb", existing=len(existing))
                        return None
                except Exception as e:
                    logger.warning("dedup_dynamo_check_failed", slug=signal.window_slug, error=str(e)[:60])

            self._traded_slugs.add(signal.window_slug)

            # Dynamic Kelly sizing based on model confidence
            size = self.risk.get_bet_size(lgbm_prob=signal.model_prob)
            if size <= 0:
                return None

            return await self._execute_directional(signal, yes_token_id, no_token_id, size, signal_ms, bedrock_ms)

    async def _execute_directional(
        self,
        signal: Signal,
        yes_token_id: str,
        no_token_id: str,
        size: float,
        signal_ms: float = 0,
        bedrock_ms: float = 0,
    ) -> TradeRecord | None:
        """Place a FOK directional order.

        Uses create_order with explicit tick_size='0.01' because short-lived
        5/15-min markets return 404 from get_tick_size (CLOB doesn't index them).
        """
        token_id = yes_token_id if signal.direction == Direction.UP else no_token_id
        side = "YES" if signal.direction == Direction.UP else "NO"

        # Calculate integer shares from dollar amount
        price = round(signal.market_price, 2)
        if price <= 0 or price >= 1:
            return None
        shares = round(size / price, 0)  # integer shares for 2-decimal precision
        if shares < 1:
            shares = 1.0
        actual_cost = round(shares * price, 2)

        order_args = OrderArgs(
            token_id=token_id,
            price=price,
            size=shares,
            side="BUY",
        )
        options = CreateOrderOptions(tick_size="0.01", neg_risk=False)

        try:
            t_order_start = time.time()
            signed = self.client.create_order(order_args, options)
            resp = self.client.post_order(signed, OrderType.FOK)
            order_ms = (time.time() - t_order_start) * 1000
            order_id = resp.get("orderID", "") if resp else ""
            success = resp.get("success", False) if resp else False

            if not success:
                logger.warning(
                    "live_order_not_matched",
                    side=side,
                    price=price,
                    shares=shares,
                    error_msg=resp.get("errorMsg", ""),
                    slug=signal.window_slug,
                )
                return None
            logger.info(
                "live_order_placed",
                side=side,
                price=price,
                shares=shares,
                size_usd=actual_cost,
                order_id=order_id,
                slug=signal.window_slug,
                latency_signal_ms=round(signal_ms, 1),
                latency_order_ms=round(order_ms, 1),
                latency_bedrock_ms=round(bedrock_ms, 1),
            )

            trade = TradeRecord(
                id=order_id or str(uuid.uuid4())[:8],
                timestamp=time.time(),
                window_slug=signal.window_slug,
                source=signal.source.value,
                direction=signal.direction.value,
                side=side,
                price=price,
                size_usd=actual_cost,
                fill_price=price,
                asset=signal.asset,
                mode="live",
                p_bayesian=signal.p_bayesian,
                p_ai=signal.p_ai,
                p_final=signal.model_prob,
                pct_move=signal.pct_move,
                seconds_remaining=signal.seconds_remaining,
                ev=signal.ev,
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
                "mode": "live",
                "asset": trade.asset,
                "p_bayesian": trade.p_bayesian,
                "p_ai": trade.p_ai,
                "p_final": trade.p_final,
                "pct_move": trade.pct_move,
                "seconds_remaining": trade.seconds_remaining,
                "ev": trade.ev,
                "latency_signal_ms": round(signal_ms, 1),
                "latency_order_ms": round(order_ms, 1),
                "latency_bedrock_ms": round(bedrock_ms, 1),
            })
            return trade

        except Exception as e:
            logger.error("live_order_failed", error=str(e), side=side, slug=signal.window_slug)
            return None

    async def resolve_window(self, window_slug: str, went_up: bool):
        """Record resolution of a window's positions (P&L calculation).

        NOTE: went_up is from Coinbase price comparison — NOT Chainlink oracle.
        We verify against Polymarket's actual outcome before marking resolved.
        """
        trades = await self.db.get_trades(window_slug=window_slug)
        if not trades:
            return

        # Verify actual market outcome via Gamma API (Chainlink-based resolution)
        actual_went_up = await self._verify_market_outcome(window_slug, went_up)

        for t in trades:
            if t["resolved"]:
                continue
            won = (t["side"] == "YES" and actual_went_up) or (t["side"] == "NO" and not actual_went_up)
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
                verified_direction="UP" if actual_went_up else "DOWN",
            )

    async def _verify_market_outcome(self, window_slug: str, fallback_went_up: bool) -> bool:
        """Query Polymarket Gamma API for actual market resolution outcome.

        Returns True if YES (Up) won, False if NO (Down) won.
        Falls back to Coinbase-based direction if market not yet resolved.
        """
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"slug": window_slug},
                )
                if resp.status_code != 200:
                    return fallback_went_up
                markets = resp.json()
                if not markets:
                    return fallback_went_up
                m = markets[0]
                prices = m.get("outcomePrices", [])
                if len(prices) >= 2:
                    yes_price = float(prices[0])
                    # If prices are conclusive (near 0 or 1), use them even if not
                    # officially "closed" yet — Gamma API can lag on the closed flag
                    if yes_price >= 0.99:
                        return True
                    if yes_price <= 0.01:
                        return False
                if not m.get("closed"):
                    # Market not closed yet and prices ambiguous — don't resolve
                    logger.info("market_not_closed_yet", slug=window_slug)
                    return fallback_went_up
                # Market closed, prices between 0.01–0.99 (rare edge case)
                if len(prices) >= 2:
                    yes_price = float(prices[0])
                    return yes_price >= 0.5
        except Exception as e:
            logger.warning("market_outcome_verify_failed", slug=window_slug, error=str(e))
        return fallback_went_up
