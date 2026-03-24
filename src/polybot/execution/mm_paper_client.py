"""In-memory order client for MarketMaker paper trading.

Designed to be injected into the engine during tests and paper-mode runs.
Simulates GTC limit order lifecycle: LIVE → MATCHED/CANCELLED.

Fill logic:
- BUY order fills immediately if limit_price >= current ask.
- SELL order fills immediately if limit_price <= current bid.
- Otherwise order stays LIVE until cancelled or market closes.

The engine drives fills by calling tick(yes_bid, no_bid, yes_ask, no_ask)
each second.  Filled orders update a shared Position directly so the
strategy sees accurate inventory on the next tick.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from polybot.core.position import Position

Side = Literal["BUY", "SELL"]
Token = Literal["YES", "NO"]
Status = Literal["LIVE", "MATCHED", "CANCELLED", "REJECTED"]


@dataclass
class PaperOrder:
    order_id: str
    token: Token          # "YES" (UP) or "NO" (DOWN)
    side: Side
    price: float          # limit price
    shares: int
    status: Status = "LIVE"
    filled_shares: int = 0
    filled_price: float = 0.0


class MMPaperClient:
    """In-memory GTC order client for the MarketMaker engine.

    Tracks open orders and fills them against the live spread each tick.
    Maintains a Position so the engine and strategy see consistent state.
    """

    def __init__(self, position: Position | None = None):
        self.orders: dict[str, PaperOrder] = {}
        self.position: Position = position if position is not None else Position()
        self._fill_log: list[dict] = []

    # ------------------------------------------------------------------
    # Order lifecycle
    # ------------------------------------------------------------------

    def post_buy(self, token: Token, shares: int, limit_price: float) -> str:
        """Post a GTC buy limit order. Returns order_id."""
        if shares <= 0:
            raise ValueError(f"shares must be > 0, got {shares}")
        order_id = _new_id()
        self.orders[order_id] = PaperOrder(
            order_id=order_id,
            token=token,
            side="BUY",
            price=round(limit_price, 4),
            shares=shares,
        )
        return order_id

    def post_sell(self, token: Token, shares: int, limit_price: float) -> str:
        """Post a GTC sell limit order. Returns order_id."""
        if shares <= 0:
            raise ValueError(f"shares must be > 0, got {shares}")
        order_id = _new_id()
        self.orders[order_id] = PaperOrder(
            order_id=order_id,
            token=token,
            side="SELL",
            price=round(limit_price, 4),
            shares=shares,
        )
        return order_id

    def cancel(self, order_id: str) -> bool:
        """Cancel a LIVE order. Returns True if cancelled, False if already terminal."""
        order = self.orders.get(order_id)
        if order is None or order.status != "LIVE":
            return False
        order.status = "CANCELLED"
        return True

    def cancel_all(self) -> int:
        """Cancel all LIVE orders. Returns count cancelled."""
        count = 0
        for order in self.orders.values():
            if order.status == "LIVE":
                order.status = "CANCELLED"
                count += 1
        return count

    def get_order(self, order_id: str) -> PaperOrder | None:
        return self.orders.get(order_id)

    def live_orders(self) -> list[PaperOrder]:
        return [o for o in self.orders.values() if o.status == "LIVE"]

    def reserved_buy_usd(self) -> float:
        """Total USD reserved by LIVE buy orders (price × shares)."""
        return sum(o.price * o.shares for o in self.live_orders() if o.side == "BUY")

    # ------------------------------------------------------------------
    # Tick-driven fill simulation
    # ------------------------------------------------------------------

    def tick(
        self,
        yes_bid: float,
        no_bid: float,
        yes_ask: float,
        no_ask: float,
        seconds: int = 0,
    ) -> list[PaperOrder]:
        """Simulate fills for this tick's spread. Returns newly filled orders."""
        filled: list[PaperOrder] = []
        for order in list(self.orders.values()):
            if order.status != "LIVE":
                continue

            ask = yes_ask if order.token == "YES" else no_ask
            bid = yes_bid if order.token == "YES" else no_bid

            if order.side == "BUY" and order.price >= ask:
                # Fill at ask (taker)
                order.status = "MATCHED"
                order.filled_shares = order.shares
                order.filled_price = ask
                self._apply_fill(order, seconds)
                filled.append(order)

            elif order.side == "SELL" and order.price <= bid:
                # Fill at bid (taker)
                order.status = "MATCHED"
                order.filled_shares = order.shares
                order.filled_price = bid
                self._apply_fill(order, seconds)
                filled.append(order)

        return filled

    # ------------------------------------------------------------------
    # Position sync
    # ------------------------------------------------------------------

    def _apply_fill(self, order: PaperOrder, seconds: int) -> None:
        is_up = order.token == "YES"
        if order.side == "BUY":
            self.position.buy(is_up, order.filled_shares, order.filled_price)
        else:
            self.position.sell(is_up, order.filled_shares, order.filled_price)
        self._fill_log.append({
            "seconds": seconds,
            "token": order.token,
            "side": order.side,
            "shares": order.filled_shares,
            "price": order.filled_price,
            "order_id": order.order_id,
        })

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def fill_log(self) -> list[dict]:
        return list(self._fill_log)

    def stats(self) -> dict:
        orders = list(self.orders.values())
        return {
            "total": len(orders),
            "live": sum(1 for o in orders if o.status == "LIVE"),
            "filled": sum(1 for o in orders if o.status == "MATCHED"),
            "cancelled": sum(1 for o in orders if o.status == "CANCELLED"),
            "fills": len(self._fill_log),
        }


def _new_id() -> str:
    return f"paper_{uuid.uuid4().hex[:12]}"
