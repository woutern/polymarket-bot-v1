"""MarketMaker orderbook WebSocket — live bid/ask feed for YES and NO tokens.

Connects to wss://ws-subscriptions-clob.polymarket.com/ws/market and subscribes
to the 'book' channel for the YES and NO token IDs of an active window.

Message types handled:
  - book          initial full orderbook snapshot
  - price_change  incremental best bid/ask update (most frequent)

The client maintains a live OrderbookState with best bid/ask for both tokens.
The engine reads this every second to build MarketState for on_tick().

Auto-reconnects with 3s backoff. Keeps last known prices on disconnect so
the engine doesn't go blind during a brief blip.

Usage:
    feed = MMOrderbookWS(yes_token_id=..., no_token_id=...)
    asyncio.create_task(feed.connect())
    # ... later each tick:
    state = feed.market_state(seconds=t, prob_up=model_prob)
    action = engine.run_tick(state)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from polybot.strategy.base import MarketState

logger = logging.getLogger(__name__)

CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
_RECONNECT_DELAY = 3.0
_STALE_THRESHOLD = 10.0  # warn if no update for this many seconds


@dataclass
class TokenBook:
    """Best bid/ask for one token (YES or NO)."""
    token_id: str
    best_bid: float = 0.50
    best_ask: float = 0.51
    last_update: float = field(default_factory=time.monotonic)

    @property
    def is_stale(self) -> bool:
        return (time.monotonic() - self.last_update) > _STALE_THRESHOLD

    def update(self, bid: float | None, ask: float | None) -> None:
        if bid is not None and 0 < bid < 1:
            self.best_bid = round(bid, 4)
        if ask is not None and 0 < ask < 1:
            self.best_ask = round(ask, 4)
        self.last_update = time.monotonic()


class MMOrderbookWS:
    """Persistent WebSocket feed for one window's YES+NO orderbook.

    Provides market_state() for the engine to call each tick.
    """

    def __init__(self, yes_token_id: str, no_token_id: str):
        self.yes = TokenBook(token_id=yes_token_id)
        self.no = TokenBook(token_id=no_token_id)
        self._token_map: dict[str, TokenBook] = {
            yes_token_id: self.yes,
            no_token_id: self.no,
        }
        self._running = False
        self.connected = False
        self.message_count = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def market_state(self, seconds: int, prob_up: float = 0.50) -> MarketState:
        """Build a MarketState from the latest orderbook snapshot."""
        if self.yes.is_stale:
            logger.warning("orderbook_stale token=YES seconds_since=%.1f", time.monotonic() - self.yes.last_update)
        if self.no.is_stale:
            logger.warning("orderbook_stale token=NO seconds_since=%.1f", time.monotonic() - self.no.last_update)

        return MarketState(
            seconds=seconds,
            yes_bid=self.yes.best_bid,
            no_bid=self.no.best_bid,
            yes_ask=self.yes.best_ask,
            no_ask=self.no.best_ask,
            prob_up=prob_up,
        )

    # ------------------------------------------------------------------
    # WebSocket connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect and maintain WebSocket with auto-reconnect. Run as a task."""
        import websockets

        self._running = True
        while self._running:
            try:
                async with websockets.connect(
                    CLOB_WS_URL,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self.connected = True
                    logger.info("mm_orderbook_connected url=%s", CLOB_WS_URL)
                    await self._subscribe(ws)

                    while self._running:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=_STALE_THRESHOLD)
                        except asyncio.TimeoutError:
                            logger.warning("mm_orderbook_stale_reconnect no_message_for=%.0fs", _STALE_THRESHOLD)
                            break  # drop connection, outer loop will reconnect
                        try:
                            self._handle(raw)
                        except Exception as exc:
                            logger.debug("mm_orderbook_parse_error error=%s", str(exc)[:80])

            except asyncio.CancelledError:
                break
            except Exception as exc:
                self.connected = False
                logger.warning("mm_orderbook_disconnected error=%s", str(exc)[:100])
                if self._running:
                    await asyncio.sleep(_RECONNECT_DELAY)

        self.connected = False

    async def close(self) -> None:
        self._running = False

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _subscribe(self, ws) -> None:
        """Subscribe to both YES and NO tokens in one message."""
        token_ids = list(self._token_map.keys())
        sub = {
            "auth": {},
            "markets": [],
            "assets_ids": token_ids,
            "type": "Market",
        }
        await ws.send(json.dumps(sub))
        logger.info("mm_orderbook_subscribed tokens=%s", [t[:16] for t in token_ids])

    def _handle(self, raw: str) -> None:
        if not raw or not isinstance(raw, str):
            return
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return

        if isinstance(msg, list):
            # Initial book snapshot: list of {asset_id, bids: [{price, size}], asks: [...]}
            for item in msg:
                if not isinstance(item, dict):
                    continue
                asset_id = item.get("asset_id", "")
                book = self._token_map.get(asset_id)
                if book is None:
                    continue
                best_bid = _best_bid(item.get("bids", []))
                best_ask = _best_ask(item.get("asks", []))
                book.update(best_bid, best_ask)
                self.message_count += 1

        elif isinstance(msg, dict):
            # Incremental updates: {price_changes: [{asset_id, best_bid, best_ask, ...}]}
            for change in msg.get("price_changes", []):
                asset_id = change.get("asset_id", "")
                book = self._token_map.get(asset_id)
                if book is None:
                    continue
                best_bid = _safe_float(change.get("best_bid"))
                best_ask = _safe_float(change.get("best_ask"))
                book.update(best_bid, best_ask)
                self.message_count += 1


# ─── Helpers ────────────────────────────────────────────────────────────────

def _safe_float(val) -> float | None:
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _best_bid(bids: list) -> float | None:
    """Return the highest bid price from a bids array."""
    best = None
    for entry in bids:
        if isinstance(entry, dict):
            p = _safe_float(entry.get("price"))
        else:
            p = _safe_float(entry)
        if p is not None and (best is None or p > best):
            best = p
    return best


def _best_ask(asks: list) -> float | None:
    """Return the lowest ask price from an asks array."""
    best = None
    for entry in asks:
        if isinstance(entry, dict):
            p = _safe_float(entry.get("price"))
        else:
            p = _safe_float(entry)
        if p is not None and (best is None or p < best):
            best = p
    return best
