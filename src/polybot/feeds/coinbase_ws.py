"""Real-time price feeds from Coinbase Advanced Trade WebSocket."""

from __future__ import annotations

import asyncio
import json
import time

import structlog
import websockets

logger = structlog.get_logger()

WS_URL = "wss://advanced-trade-ws.coinbase.com"

# Coinbase product IDs for each asset
ASSET_PRODUCTS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
}


class CoinbaseWS:
    """Coinbase WebSocket client for real-time tickers.

    Subscribes to multiple products. Public channel — no auth required.
    """

    def __init__(self, assets: list[str] | None = None):
        self.assets = assets or ["BTC"]
        self.prices: dict[str, float] = {a: 0.0 for a in self.assets}
        self.last_updates: dict[str, float] = {a: 0.0 for a in self.assets}
        self._product_to_asset = {ASSET_PRODUCTS[a]: a for a in self.assets if a in ASSET_PRODUCTS}
        self._ws = None
        self._running = False

    @property
    def price(self) -> float:
        """Backward compat: return BTC price."""
        return self.prices.get("BTC", 0.0)

    def get_price(self, asset: str) -> float:
        return self.prices.get(asset.upper(), 0.0)

    async def connect(self):
        """Connect and subscribe to tickers for all assets."""
        self._running = True
        product_ids = [ASSET_PRODUCTS[a] for a in self.assets if a in ASSET_PRODUCTS]
        if not product_ids:
            logger.error("no_valid_products", assets=self.assets)
            return

        while self._running:
            try:
                async with websockets.connect(WS_URL) as ws:
                    self._ws = ws
                    subscribe = {
                        "type": "subscribe",
                        "product_ids": product_ids,
                        "channel": "ticker",
                    }
                    await ws.send(json.dumps(subscribe))
                    logger.info("coinbase_ws_connected", products=product_ids)

                    async for raw in ws:
                        if not self._running:
                            break
                        msg = json.loads(raw)
                        self._handle_message(msg)

            except (websockets.ConnectionClosed, OSError) as e:
                if not self._running:
                    break
                logger.warning("coinbase_ws_disconnected", error=str(e))
                await asyncio.sleep(2)

    def _handle_message(self, msg: dict):
        if msg.get("channel") == "ticker":
            events = msg.get("events", [])
            for event in events:
                tickers = event.get("tickers", [])
                for ticker in tickers:
                    product = ticker.get("product_id", "")
                    asset = self._product_to_asset.get(product)
                    if asset:
                        self.prices[asset] = float(ticker["price"])
                        self.last_updates[asset] = time.time()

    async def close(self):
        self._running = False
        if self._ws:
            await self._ws.close()
