"""Live CLOB order client for MarketMaker GTC orders.

Wraps py_clob_client to post, track, and cancel GTC limit orders.
Same interface as MMPaperClient so the engine can swap between paper/live
by injecting a different client.

GTC order lifecycle:
  POST → LIVE → (fill) MATCHED
                (cancel) CANCELLED
                (expire) CANCELLED

The engine calls:
  post_buy(token, shares, price)    → order_id
  post_sell(token, shares, price)   → order_id
  get_status(order_id)              → "LIVE" | "MATCHED" | "CANCELLED" | ...
  cancel(order_id)                  → bool
  cancel_all()                      → count cancelled

Orders are tracked in memory for fast status checks. Status is refreshed
from CLOB every 5 seconds (not on every call) to avoid rate limits.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Literal

import structlog

logger = structlog.get_logger()

Side = Literal["BUY", "SELL"]
Token = Literal["YES", "NO"]

POLYMARKET_HOST = "https://clob.polymarket.com"
_STATUS_REFRESH_INTERVAL = 1.0   # seconds between CLOB status polls
_TERMINAL = {"MATCHED", "FILLED", "CANCELLED", "CANCELED", "REJECTED", "EXPIRED"}


@dataclass
class LiveOrder:
    order_id: str
    token: Token
    token_id: str
    side: Side
    price: float
    shares: int
    status: str = "LIVE"
    filled_price: float = 0.0
    filled_shares: int = 0
    posted_at: float = field(default_factory=time.monotonic)
    last_checked: float = 0.0


class MMLiveClient:
    """Real GTC order client — executes on Polymarket CLOB.

    Args:
        yes_token_id: Token ID for the YES (UP) side of the window.
        no_token_id:  Token ID for the NO (DOWN) side.
        settings:     Bot settings (private key, API creds, chain ID).
    """

    def __init__(self, yes_token_id: str, no_token_id: str, settings):
        self._yes_token_id = yes_token_id
        self._no_token_id = no_token_id
        self._token_ids: dict[Token, str] = {
            "YES": yes_token_id,
            "NO": no_token_id,
        }
        self.orders: dict[str, LiveOrder] = {}
        self._client = self._build_clob_client(settings)

    # ------------------------------------------------------------------
    # Order lifecycle
    # ------------------------------------------------------------------

    def post_buy(self, token: Token, shares: int, limit_price: float) -> str | None:
        """Post a GTC buy limit order. Returns order_id or None on failure."""
        return self._post("BUY", token, shares, limit_price)

    def post_sell(self, token: Token, shares: int, limit_price: float) -> str | None:
        """Post a GTC sell limit order. Returns order_id or None on failure."""
        return self._post("SELL", token, shares, limit_price)

    def cancel(self, order_id: str) -> bool:
        """Cancel a single order. Returns True if CLOB accepted the cancel."""
        order = self.orders.get(order_id)
        if order is None or order.status in _TERMINAL:
            return False
        try:
            resp = self._client.cancel(order_id)
            if resp and resp.get("canceled"):
                order.status = "CANCELLED"
                logger.info("mm_live_cancelled", order_id=order_id)
                return True
            return False
        except Exception as exc:
            logger.warning("mm_live_cancel_failed", order_id=order_id, error=str(exc)[:80])
            return False

    def cancel_all(self) -> int:
        """Cancel all LIVE orders. Returns count successfully cancelled."""
        live_ids = [o.order_id for o in self.orders.values() if o.status not in _TERMINAL]
        count = 0
        for oid in live_ids:
            if self.cancel(oid):
                count += 1
        return count

    def get_status(self, order_id: str) -> str:
        """Return cached order status. Refreshes from CLOB if stale."""
        order = self.orders.get(order_id)
        if order is None:
            return "UNKNOWN"
        if order.status not in _TERMINAL:
            self._maybe_refresh(order)
        return order.status

    def live_orders(self) -> list[LiveOrder]:
        return [o for o in self.orders.values() if o.status not in _TERMINAL]

    def reserved_buy_usd(self) -> float:
        # Reserve only the UNFILLED portion of each live buy order.
        # Partial fills reduce the outstanding reservation so budget is freed immediately.
        return sum(
            o.price * max(o.shares - o.filled_shares, 0)
            for o in self.live_orders()
            if o.side == "BUY"
        )

    def stats(self) -> dict:
        all_orders = list(self.orders.values())
        return {
            "total": len(all_orders),
            "live": sum(1 for o in all_orders if o.status not in _TERMINAL),
            "filled": sum(1 for o in all_orders if o.status in {"MATCHED", "FILLED"}),
            "cancelled": sum(1 for o in all_orders if o.status in {"CANCELLED", "CANCELED"}),
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _post(self, side: Side, token: Token, shares: int, price: float) -> str | None:
        if shares <= 0:
            raise ValueError(f"shares must be > 0, got {shares}")
        token_id = self._token_ids[token]
        price = round(price, 2)  # CLOB tick size 0.01

        try:
            from py_clob_client.clob_types import OrderArgs, CreateOrderOptions, OrderType

            order_args = OrderArgs(
                token_id=token_id,
                price=price,
                size=float(shares),
                side=side,
            )
            options = CreateOrderOptions(tick_size="0.01", neg_risk=False)
            signed = self._client.create_order(order_args, options)
            resp = self._client.post_order(signed, OrderType.GTC)

            order_id = resp.get("orderID", "") if resp else ""
            success = resp.get("success", False) if resp else False

            if not success or not order_id:
                logger.warning(
                    "mm_live_post_failed",
                    token=token, side=side, price=price, shares=shares,
                    error=resp.get("errorMsg", "") if resp else "no response",
                )
                return None

            order = LiveOrder(
                order_id=order_id,
                token=token,
                token_id=token_id,
                side=side,
                price=price,
                shares=shares,
            )
            self.orders[order_id] = order
            logger.info(
                "mm_live_posted",
                order_id=order_id, token=token, side=side, price=price, shares=shares,
            )
            return order_id

        except Exception as exc:
            logger.error(
                "mm_live_post_exception",
                token=token, side=side, price=price, shares=shares, error=str(exc)[:120],
            )
            return None

    def _maybe_refresh(self, order: LiveOrder) -> None:
        now = time.monotonic()
        if (now - order.last_checked) < _STATUS_REFRESH_INTERVAL:
            return
        try:
            resp = self._client.get_order(order.order_id)
            if resp:
                remote_status = resp.get("status", order.status)
                order.status = remote_status
                size_matched = resp.get("size_matched", "0")
                if size_matched:
                    order.filled_shares = int(float(size_matched))
                price_used = resp.get("price", order.price)
                if price_used:
                    order.filled_price = float(price_used)
            order.last_checked = now
        except Exception as exc:
            logger.debug("mm_live_status_check_failed", order_id=order.order_id, error=str(exc)[:80])

    @staticmethod
    def _build_clob_client(settings):
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        creds = ApiCreds(
            api_key=settings.polymarket_api_key,
            api_secret=settings.polymarket_api_secret,
            api_passphrase=settings.polymarket_api_passphrase,
        )
        funder = getattr(settings, "polymarket_funder", None) or None
        sig_type = 2 if funder else 0  # GNOSIS_SAFE for MetaMask wallets, EOA otherwise
        return ClobClient(
            host=POLYMARKET_HOST,
            chain_id=settings.polymarket_chain_id,
            key=settings.polymarket_private_key,
            creds=creds,
            signature_type=sig_type,
            funder=funder,
        )
