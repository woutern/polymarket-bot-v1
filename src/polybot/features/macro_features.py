"""Macro features: Fear & Greed, SOL funding rate, SOL open interest.

All external APIs are cached and fail gracefully — if an API is down,
the last cached value is used. Trading never stops due to missing macro data.
"""

from __future__ import annotations

import time

import structlog

logger = structlog.get_logger()


class MacroFeatures:
    """Fetches and caches macro-level features for SOL model."""

    def __init__(self):
        self._cache: dict[str, tuple[dict, float]] = {}  # key → (value, timestamp)

    def _get_cached(self, key: str, ttl: float) -> dict | None:
        cached = self._cache.get(key)
        if cached and time.time() - cached[1] < ttl:
            return cached[0]
        return None

    def _set_cached(self, key: str, value: dict):
        self._cache[key] = (value, time.time())

    def get_fear_greed(self) -> dict:
        """Crypto Fear & Greed Index (0-100). Cached 1 hour."""
        cached = self._get_cached("fear_greed", 3600)
        if cached:
            return cached

        try:
            import httpx
            resp = httpx.get("https://api.alternative.me/fng/?limit=1", timeout=5)
            if resp.status_code == 200:
                data = resp.json().get("data", [{}])[0]
                value = int(data.get("value", 50))
                zone = 0 if value < 25 else (1 if value < 50 else (2 if value < 75 else 3))
                result = {"fear_greed_value": value, "fear_greed_zone": zone}
                self._set_cached("fear_greed", result)
                return result
        except Exception as e:
            logger.debug("fear_greed_fetch_failed", error=str(e)[:40])

        # Fallback to cache or default
        return self._cache.get("fear_greed", ({"fear_greed_value": 50, "fear_greed_zone": 1}, 0))[0]

    def get_sol_funding(self) -> dict:
        """SOL perpetual funding rate from Binance. Cached 1 hour."""
        cached = self._get_cached("sol_funding", 3600)
        if cached:
            return cached

        try:
            import httpx
            resp = httpx.get(
                "https://fapi.binance.com/fapi/v1/fundingRate",
                params={"symbol": "SOLUSDT", "limit": 1},
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data:
                    rate = float(data[0].get("fundingRate", 0))
                    result = {
                        "sol_funding_rate": rate,
                        "sol_funding_direction": 1 if rate > 0 else -1,
                    }
                    self._set_cached("sol_funding", result)
                    return result
        except Exception as e:
            logger.debug("sol_funding_fetch_failed", error=str(e)[:40])

        return self._cache.get("sol_funding", ({"sol_funding_rate": 0.0, "sol_funding_direction": 0}, 0))[0]

    def get_sol_open_interest(self) -> dict:
        """SOL open interest from Binance. Cached 5 minutes."""
        cached = self._get_cached("sol_oi", 300)
        if cached:
            return cached

        try:
            import httpx
            resp = httpx.get(
                "https://fapi.binance.com/fapi/v1/openInterest",
                params={"symbol": "SOLUSDT"},
                timeout=5,
            )
            if resp.status_code == 200:
                data = resp.json()
                oi = float(data.get("openInterest", 0))

                # Compute change vs last cached value
                prev = self._cache.get("sol_oi", ({"sol_oi": oi}, 0))[0]
                prev_oi = prev.get("sol_oi", oi)
                oi_change = (oi - prev_oi) / prev_oi * 100 if prev_oi > 0 else 0

                result = {
                    "sol_oi": oi,
                    "oi_change_1h": round(oi_change, 4),
                    "oi_expanding": 1 if oi_change > 0 else 0,
                }
                self._set_cached("sol_oi", result)
                return result
        except Exception as e:
            logger.debug("sol_oi_fetch_failed", error=str(e)[:40])

        return self._cache.get("sol_oi", ({"sol_oi": 0, "oi_change_1h": 0, "oi_expanding": 0}, 0))[0]

    def get_all(self) -> dict:
        """Get all macro features as a flat dict."""
        result = {}
        result.update(self.get_fear_greed())
        result.update(self.get_sol_funding())
        result.update(self.get_sol_open_interest())
        return result
