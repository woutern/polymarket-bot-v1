"""Polymarket WebSocket for real-time orderbook updates."""

from __future__ import annotations

import asyncio
import json
import time

import structlog
import websockets

from polybot.models import OrderbookSnapshot

logger = structlog.get_logger()

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
PING_INTERVAL = 10  # seconds


class PolymarketWS:
    """WebSocket client for Polymarket orderbook updates."""

    def __init__(self):
        self.orderbook = OrderbookSnapshot()
        self._ws = None
        self._running = False
        self._subscribed_assets: list[str] = []

    async def connect(self, asset_ids: list[str]):
        """Connect and subscribe to orderbook updates for given assets."""
        self._subscribed_assets = asset_ids
        self._running = True

        while self._running:
            try:
                async with websockets.connect(WS_URL) as ws:
                    self._ws = ws
                    logger.info("polymarket_ws_connected")

                    # Subscribe to each asset
                    for asset_id in asset_ids:
                        sub_msg = {
                            "type": "market",
                            "assets_ids": [asset_id],
                        }
                        await ws.send(json.dumps(sub_msg))

                    # Run reader and pinger concurrently
                    await asyncio.gather(
                        self._read_loop(ws),
                        self._ping_loop(ws),
                    )

            except (websockets.ConnectionClosed, OSError) as e:
                if not self._running:
                    break
                logger.warning("polymarket_ws_disconnected", error=str(e))
                await asyncio.sleep(2)

    async def _read_loop(self, ws):
        async for raw in ws:
            if not self._running:
                break
            msg = json.loads(raw)
            self._handle_message(msg)

    async def _ping_loop(self, ws):
        while self._running:
            await asyncio.sleep(PING_INTERVAL)
            try:
                await ws.ping()
            except Exception:
                break

    def _handle_message(self, msg: dict):
        """Parse orderbook snapshot/delta messages."""
        # Polymarket sends different message types
        if "market" in msg:
            bids = msg.get("bids", [])
            asks = msg.get("asks", [])
            ts = time.time()

            # Update YES side (first token) or NO side (second token)
            # The asset_id in the message tells us which side
            if asks:
                best_ask = min(float(a["price"]) for a in asks if a.get("price"))
            else:
                best_ask = None

            if bids:
                best_bid = max(float(b["price"]) for b in bids if b.get("price"))
            else:
                best_bid = None

            # We track both sides via the snapshot
            if best_ask is not None:
                self.orderbook.yes_best_ask = best_ask
            if best_bid is not None:
                self.orderbook.yes_best_bid = best_bid
            self.orderbook.timestamp = ts

    async def close(self):
        self._running = False
        if self._ws:
            await self._ws.close()

    def update_from_books(self, yes_book: dict, no_book: dict):
        """Update orderbook from REST API snapshots."""
        ts = time.time()
        yes_asks = yes_book.get("asks", [])
        yes_bids = yes_book.get("bids", [])
        no_asks = no_book.get("asks", [])
        no_bids = no_book.get("bids", [])

        if yes_asks:
            self.orderbook.yes_best_ask = min(float(a["price"]) for a in yes_asks)
        if yes_bids:
            self.orderbook.yes_best_bid = max(float(b["price"]) for b in yes_bids)
        if no_asks:
            self.orderbook.no_best_ask = min(float(a["price"]) for a in no_asks)
        if no_bids:
            self.orderbook.no_best_bid = max(float(b["price"]) for b in no_bids)
        self.orderbook.timestamp = ts
