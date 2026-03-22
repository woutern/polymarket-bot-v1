"""Real-time price feeds from Coinbase Advanced Trade WebSocket."""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque

import structlog
import websockets

logger = structlog.get_logger()

WS_URL = "wss://advanced-trade-ws.coinbase.com"

# Coinbase product IDs for each asset
ASSET_PRODUCTS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
}


class CoinbaseWS:
    """Coinbase WebSocket client for real-time tickers + level2 orderbook.

    Subscribes to ticker (prices) and l2_data (orderbook depth) channels.
    Public channels — no auth required.
    """

    def __init__(self, assets: list[str] | None = None):
        self.assets = assets or ["BTC"]
        self.prices: dict[str, float] = {a: 0.0 for a in self.assets}
        self.last_updates: dict[str, float] = {a: 0.0 for a in self.assets}
        self._product_to_asset = {ASSET_PRODUCTS[a]: a for a in self.assets if a in ASSET_PRODUCTS}
        self._ws = None
        self._running = False

        # Level2 orderbook state (top of book only — we don't need full depth)
        self.best_bids: dict[str, float] = {a: 0.0 for a in self.assets}
        self.best_asks: dict[str, float] = {a: float("inf") for a in self.assets}
        self.bid_depth_5: dict[str, float] = {a: 0.0 for a in self.assets}  # sum of top-5 bid sizes
        self.ask_depth_5: dict[str, float] = {a: 0.0 for a in self.assets}  # sum of top-5 ask sizes

        # Full top-of-book for depth calculation
        self._bids: dict[str, dict[str, float]] = {a: {} for a in self.assets}  # price_str → size
        self._asks: dict[str, dict[str, float]] = {a: {} for a in self.assets}

        # OFI tracking: (timestamp, buy_vol_delta, sell_vol_delta)
        self._ofi_events: dict[str, deque] = {a: deque(maxlen=500) for a in self.assets}

        # Trade arrival tracking
        self._trade_times: dict[str, deque] = {a: deque(maxlen=200) for a in self.assets}

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
                async with websockets.connect(
                    WS_URL,
                    ping_interval=20,   # send WS ping every 20s
                    ping_timeout=30,    # disconnect if no pong within 30s
                ) as ws:
                    self._ws = ws
                    subscribe = {
                        "type": "subscribe",
                        "product_ids": product_ids,
                        "channel": "ticker",
                    }
                    await ws.send(json.dumps(subscribe))
                    # Subscribe to level2 for orderbook depth + OFI
                    subscribe_l2 = {
                        "type": "subscribe",
                        "product_ids": product_ids,
                        "channel": "l2_data",
                    }
                    await ws.send(json.dumps(subscribe_l2))
                    logger.info("coinbase_ws_connected", products=product_ids, channels=["ticker", "l2_data"])

                    while self._running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=30.0)
                            msg = json.loads(raw)
                            self._handle_message(msg)
                        except asyncio.TimeoutError:
                            # No message in 30s — connection is stale, force reconnect
                            logger.warning("coinbase_ws_stale_reconnecting")
                            break

            except (websockets.ConnectionClosed, OSError) as e:
                if not self._running:
                    break
                logger.warning("coinbase_ws_disconnected", error=str(e))
                await asyncio.sleep(2)
            except Exception as e:
                if not self._running:
                    break
                logger.warning("coinbase_ws_error", error=str(e))
                await asyncio.sleep(5)

    def _handle_message(self, msg: dict):
        channel = msg.get("channel", "")
        if channel == "ticker":
            events = msg.get("events", [])
            for event in events:
                tickers = event.get("tickers", [])
                for ticker in tickers:
                    product = ticker.get("product_id", "")
                    asset = self._product_to_asset.get(product)
                    if asset:
                        self.prices[asset] = float(ticker["price"])
                        self.last_updates[asset] = time.time()
                        # Track trade arrivals from ticker updates
                        self._trade_times[asset].append(time.time())
        elif channel == "l2_data":
            self._handle_l2(msg)

    def _handle_l2(self, msg: dict):
        """Process level2 orderbook snapshots and updates."""
        events = msg.get("events", [])
        now = time.time()
        for event in events:
            event_type = event.get("type", "")
            product = event.get("product_id", "")
            asset = self._product_to_asset.get(product)
            if not asset:
                continue

            updates = event.get("updates", [])
            if event_type == "snapshot":
                # Full book reset
                self._bids[asset].clear()
                self._asks[asset].clear()

            buy_delta = 0.0
            sell_delta = 0.0

            for u in updates:
                side = u.get("side", "")
                price_str = u.get("price_level", "0")
                new_qty = float(u.get("new_quantity", 0))

                if side == "bid":
                    old_qty = self._bids[asset].get(price_str, 0.0)
                    if new_qty > 0:
                        self._bids[asset][price_str] = new_qty
                    elif price_str in self._bids[asset]:
                        del self._bids[asset][price_str]
                    buy_delta += max(0, new_qty - old_qty)
                    sell_delta += max(0, old_qty - new_qty)  # bid removed = selling pressure
                elif side == "offer":
                    old_qty = self._asks[asset].get(price_str, 0.0)
                    if new_qty > 0:
                        self._asks[asset][price_str] = new_qty
                    elif price_str in self._asks[asset]:
                        del self._asks[asset][price_str]
                    sell_delta += max(0, new_qty - old_qty)
                    buy_delta += max(0, old_qty - new_qty)  # ask removed = buying pressure

            # Track OFI event
            if buy_delta > 0 or sell_delta > 0:
                self._ofi_events[asset].append((now, buy_delta, sell_delta))

            # Update top-of-book
            self._update_top_of_book(asset)

    def _update_top_of_book(self, asset: str):
        """Recompute best bid/ask and top-5 depth from full book."""
        bids = self._bids.get(asset, {})
        asks = self._asks.get(asset, {})

        if bids:
            sorted_bids = sorted(bids.items(), key=lambda x: float(x[0]), reverse=True)
            self.best_bids[asset] = float(sorted_bids[0][0])
            self.bid_depth_5[asset] = sum(v for _, v in sorted_bids[:5])
        else:
            self.best_bids[asset] = 0.0
            self.bid_depth_5[asset] = 0.0
        if asks:
            sorted_asks = sorted(asks.items(), key=lambda x: float(x[0]))
            self.best_asks[asset] = float(sorted_asks[0][0])
            self.ask_depth_5[asset] = sum(v for _, v in sorted_asks[:5])
        else:
            self.best_asks[asset] = float("inf")
            self.ask_depth_5[asset] = 0.0

    # --- Computed orderbook features ---

    def get_ofi_30s(self, asset: str) -> float:
        """Order Flow Imbalance: (buy_vol - sell_vol) / total_vol over last 30s."""
        cutoff = time.time() - 30
        events = [(t, b, s) for t, b, s in self._ofi_events.get(asset, []) if t > cutoff]
        if not events:
            return 0.0
        total_buy = sum(b for _, b, _ in events)
        total_sell = sum(s for _, _, s in events)
        total = total_buy + total_sell
        return (total_buy - total_sell) / total if total > 0 else 0.0

    def get_bid_ask_spread(self, asset: str) -> float:
        """Spread as fraction of mid price."""
        best_bid = self.best_bids.get(asset, 0.0)
        best_ask = self.best_asks.get(asset, float("inf"))
        if best_bid <= 0 or best_ask == float("inf"):
            return 0.0
        mid = (best_bid + best_ask) / 2
        return (best_ask - best_bid) / mid if mid > 0 else 0.0

    def get_depth_imbalance(self, asset: str) -> float:
        """(bid_depth_5 - ask_depth_5) / (bid_depth_5 + ask_depth_5). Range [-1, 1]."""
        bid_d = self.bid_depth_5.get(asset, 0.0)
        ask_d = self.ask_depth_5.get(asset, 0.0)
        total = bid_d + ask_d
        return (bid_d - ask_d) / total if total > 0 else 0.0

    def get_trade_arrival_rate(self, asset: str) -> float:
        """Trades per second over last 30s."""
        cutoff = time.time() - 30
        recent = [t for t in self._trade_times.get(asset, []) if t > cutoff]
        return len(recent) / 30.0

    async def close(self):
        self._running = False
        if self._ws:
            await self._ws.close()
