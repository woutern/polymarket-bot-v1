"""Polymarket Real-Time Data Socket (RTDS) — streams Chainlink oracle prices.

Connects to wss://ws-live-data.polymarket.com and subscribes to
crypto_prices_chainlink for BTC, ETH, SOL. Compares against Coinbase
real-time prices to measure oracle lag — the primary edge signal.

Oracle lag = Coinbase leads Chainlink by 15-45 seconds.
When |lag| > 0.3%, there's a dislocation we can trade.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque
from dataclasses import dataclass, field

import structlog

logger = structlog.get_logger()

RTDS_URL = "wss://ws-live-data.polymarket.com"
SYMBOLS = {"BTC": "btc/usd", "ETH": "eth/usd", "SOL": "sol/usd"}
DISLOCATION_THRESHOLD = 0.003  # 0.3%


@dataclass
class OracleState:
    """Per-asset oracle state — tracks Chainlink price and lag vs Coinbase."""

    asset: str
    chainlink_price: float = 0.0
    chainlink_ts: float = 0.0  # Unix ms from RTDS
    history: deque = field(default_factory=lambda: deque(maxlen=60))

    # Computed from comparison with Coinbase
    oracle_lag_pct: float = 0.0
    oracle_lag_ms: float = 0.0
    dislocation: bool = False

    @property
    def lag_mean(self) -> float:
        if not self.history:
            return 0.0
        return sum(h["lag_ms"] for h in self.history) / len(self.history)

    @property
    def lag_p50(self) -> float:
        if not self.history:
            return 0.0
        vals = sorted(h["lag_ms"] for h in self.history)
        return vals[len(vals) // 2]

    @property
    def lag_p95(self) -> float:
        if not self.history:
            return 0.0
        vals = sorted(h["lag_ms"] for h in self.history)
        idx = int(len(vals) * 0.95)
        return vals[min(idx, len(vals) - 1)]

    def update_chainlink(self, price: float, ts_ms: float):
        """Update Chainlink price from RTDS message."""
        self.chainlink_price = price
        self.chainlink_ts = ts_ms

    def compute_lag(self, coinbase_price: float):
        """Compute oracle lag vs current Coinbase price."""
        if self.chainlink_price <= 0 or coinbase_price <= 0:
            return
        self.oracle_lag_pct = (coinbase_price - self.chainlink_price) / self.chainlink_price
        self.oracle_lag_ms = (time.time() * 1000) - self.chainlink_ts if self.chainlink_ts > 0 else 0
        self.dislocation = abs(self.oracle_lag_pct) > DISLOCATION_THRESHOLD

        self.history.append({
            "lag_ms": self.oracle_lag_ms,
            "lag_pct": self.oracle_lag_pct,
            "chainlink": self.chainlink_price,
            "coinbase": coinbase_price,
            "ts": time.time(),
        })


def compute_oracle_probability(
    spot_price: float,
    strike: float,
    realized_vol: float,
    seconds_remaining: float,
) -> float:
    """Black-Scholes binary option probability: P(spot >= strike at expiry).

    Args:
        spot_price: Current price (Coinbase real-time).
        strike: Window open price (the threshold to beat).
        realized_vol: Annualized realized volatility.
        seconds_remaining: Seconds until window close.

    Returns:
        Probability that spot >= strike at expiry (YES probability).
    """
    if strike <= 0 or spot_price <= 0 or realized_vol <= 0 or seconds_remaining <= 0:
        return 0.5  # neutral

    from scipy.stats import norm

    time_to_expiry = seconds_remaining / 31_536_000  # seconds to years
    sqrt_t = math.sqrt(time_to_expiry)

    d2 = math.log(spot_price / strike) / (realized_vol * sqrt_t)
    return float(norm.cdf(d2))


def compute_realized_vol(prices: list[float], tick_interval_seconds: float = 0.25) -> float:
    """Compute annualized realized volatility from a price series.

    Args:
        prices: List of prices (e.g., last 100 Coinbase ticks at 250ms).
        tick_interval_seconds: Time between ticks (0.25s for 250ms).

    Returns:
        Annualized realized volatility.
    """
    if len(prices) < 10:
        return 0.0

    log_returns = [math.log(prices[i] / prices[i - 1]) for i in range(1, len(prices)) if prices[i - 1] > 0]
    if len(log_returns) < 5:
        return 0.0

    mean_ret = sum(log_returns) / len(log_returns)
    variance = sum((r - mean_ret) ** 2 for r in log_returns) / (len(log_returns) - 1)
    std_per_tick = math.sqrt(variance)

    ticks_per_year = 365 * 24 * 3600 / tick_interval_seconds
    return std_per_tick * math.sqrt(ticks_per_year)


class RTDSClient:
    """Persistent WebSocket client for Polymarket RTDS Chainlink price feed."""

    def __init__(self, assets: list[str] | None = None):
        self.assets = assets or ["BTC", "ETH", "SOL"]
        self.oracle_states: dict[str, OracleState] = {
            a: OracleState(asset=a) for a in self.assets
        }
        self._running = False
        self._connected = False

    def get_state(self, asset: str) -> OracleState:
        return self.oracle_states.get(asset, OracleState(asset=asset))

    async def connect(self):
        """Connect and maintain RTDS WebSocket with auto-reconnect."""
        import websockets

        self._running = True
        while self._running:
            try:
                async with websockets.connect(RTDS_URL, ping_interval=10) as ws:
                    self._connected = True
                    logger.info("rtds_connected", url=RTDS_URL)

                    # Subscribe to Chainlink prices for each asset
                    for asset in self.assets:
                        symbol = SYMBOLS.get(asset)
                        if not symbol:
                            continue
                        sub = {
                            "action": "subscribe",
                            "subscriptions": [{
                                "topic": "crypto_prices_chainlink",
                                "type": "*",
                                "filters": json.dumps({"symbol": symbol}),
                            }],
                        }
                        await ws.send(json.dumps(sub))
                        logger.info("rtds_subscribed", asset=asset, symbol=symbol)

                    # Process messages
                    async for msg in ws:
                        if not self._running:
                            break
                        if not msg or not isinstance(msg, str):
                            continue
                        try:
                            self._handle_message(msg)
                        except Exception as e:
                            logger.debug("rtds_parse_error", error=str(e))

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                logger.warning("rtds_disconnected", error=str(e))
                if self._running:
                    await asyncio.sleep(5)

    def _handle_message(self, raw: str):
        """Parse RTDS message and update oracle state."""
        if not raw.strip():
            return
        data = json.loads(raw)

        # Error responses
        if data.get("statusCode", 200) >= 400:
            logger.debug("rtds_error", body=str(data.get("body", ""))[:100])
            return

        payload = data.get("payload")
        if not payload:
            return

        topic = data.get("topic", "")
        if "chainlink" not in topic:
            return

        # Parse price data — comes as batch array
        price_data = payload.get("data", [])
        if not price_data:
            # Single value format (some messages)
            value = payload.get("value")
            ts = data.get("timestamp", 0)
            if value is not None:
                self._route_price(float(value), float(ts))
            return

        # Batch format: [{timestamp, value}, ...]
        # Use the latest entry
        latest = max(price_data, key=lambda d: d.get("timestamp", 0))
        price = float(latest["value"])
        ts_ms = float(latest["timestamp"])

        # Route to correct asset based on filters/topic
        # RTDS sends per-subscription, but we need to figure out which asset
        # For now, match by price magnitude (BTC > 10000, ETH > 100, SOL < 1000)
        # TODO: better routing when we have per-asset subscriptions
        for asset, state in self.oracle_states.items():
            if asset == "BTC" and price > 10000:
                state.update_chainlink(price, ts_ms)
                break
            elif asset == "ETH" and 100 < price < 10000:
                state.update_chainlink(price, ts_ms)
                break
            elif asset == "SOL" and price < 500:
                state.update_chainlink(price, ts_ms)
                break

    def _route_price(self, price: float, ts_ms: float):
        """Route a single price to the correct asset by magnitude."""
        for asset, state in self.oracle_states.items():
            if asset == "BTC" and price > 10000:
                state.update_chainlink(price, ts_ms)
                break
            elif asset == "ETH" and 100 < price < 10000:
                state.update_chainlink(price, ts_ms)
                break
            elif asset == "SOL" and price < 500:
                state.update_chainlink(price, ts_ms)
                break

    async def close(self):
        self._running = False
