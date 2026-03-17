"""Crypto news feed — polls CryptoPanic and CoinGecko for headlines.

Provides recent crypto headlines with timestamps for AI signal analysis.
Runs as an async background task, polling every 30 seconds.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

import httpx
import structlog

logger = structlog.get_logger()

CRYPTOPANIC_URL = (
    "https://cryptopanic.com/api/free/v1/posts/"
    "?auth_token=free&currencies=BTC,ETH,SOL&kind=news&filter=hot"
)
COINGECKO_TRENDING_URL = "https://api.coingecko.com/api/v3/search/trending"

# Keep headlines for 5 minutes max
HEADLINE_TTL_SECONDS = 300
REQUEST_TIMEOUT = 10.0


@dataclass
class Headline:
    """A single news headline."""

    title: str
    source: str
    url: str = ""
    timestamp: float = 0.0
    currencies: list[str] = field(default_factory=list)

    @property
    def age_seconds(self) -> float:
        return time.time() - self.timestamp


class NewsFeed:
    """Async news aggregator polling CryptoPanic and CoinGecko.

    Usage:
        feed = NewsFeed(poll_interval=30)
        asyncio.create_task(feed.start())
        headlines = feed.get_recent(max_age=120)
    """

    def __init__(self, poll_interval: int = 30):
        self.poll_interval = poll_interval
        self._headlines: list[Headline] = []
        self._running = False
        self._last_poll: float = 0.0
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        """Start polling in a loop."""
        self._running = True
        self._client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)
        logger.info("news_feed_starting", poll_interval=self.poll_interval)

        try:
            while self._running:
                await self._poll()
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            logger.info("news_feed_cancelled")
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Stop the feed and close the HTTP client."""
        self._running = False
        if self._client:
            await self._client.aclose()
            self._client = None
        logger.info("news_feed_stopped")

    def get_recent(self, max_age: float = 120.0) -> list[Headline]:
        """Return headlines newer than max_age seconds."""
        now = time.time()
        return [
            h for h in self._headlines if (now - h.timestamp) <= max_age
        ]

    def get_recent_for_asset(
        self, asset: str, max_age: float = 120.0
    ) -> list[Headline]:
        """Return headlines mentioning a specific asset."""
        asset_upper = asset.upper()
        return [
            h
            for h in self.get_recent(max_age)
            if asset_upper in h.currencies or asset_upper in h.title.upper()
        ]

    async def _poll(self) -> None:
        """Fetch from all sources and merge."""
        results = await asyncio.gather(
            self._fetch_cryptopanic(),
            self._fetch_coingecko_trending(),
            return_exceptions=True,
        )

        new_headlines: list[Headline] = []
        for result in results:
            if isinstance(result, Exception):
                logger.warning("news_fetch_error", error=str(result))
                continue
            if isinstance(result, list):
                new_headlines.extend(result)

        # Deduplicate by title (keep most recent)
        seen_titles: set[str] = set()
        deduped: list[Headline] = []
        for h in sorted(new_headlines, key=lambda x: x.timestamp, reverse=True):
            title_key = h.title.lower().strip()
            if title_key not in seen_titles:
                seen_titles.add(title_key)
                deduped.append(h)

        # Merge with existing, removing expired
        now = time.time()
        existing = [
            h for h in self._headlines if (now - h.timestamp) <= HEADLINE_TTL_SECONDS
        ]
        existing_titles = {h.title.lower().strip() for h in existing}

        for h in deduped:
            if h.title.lower().strip() not in existing_titles:
                existing.append(h)

        self._headlines = sorted(existing, key=lambda x: x.timestamp, reverse=True)
        self._last_poll = now

        logger.debug(
            "news_poll_complete",
            new_count=len(new_headlines),
            total_count=len(self._headlines),
        )

    async def _fetch_cryptopanic(self) -> list[Headline]:
        """Fetch from CryptoPanic free API."""
        if not self._client:
            return []

        try:
            resp = await self._client.get(CRYPTOPANIC_URL)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("cryptopanic_fetch_failed", error=str(e))
            return []

        headlines: list[Headline] = []
        for post in data.get("results", []):
            currencies = [
                c.get("code", "").upper()
                for c in post.get("currencies", [])
            ]
            headlines.append(
                Headline(
                    title=post.get("title", ""),
                    source="cryptopanic",
                    url=post.get("url", ""),
                    timestamp=time.time(),
                    currencies=currencies,
                )
            )

        return headlines

    async def _fetch_coingecko_trending(self) -> list[Headline]:
        """Fetch trending coins from CoinGecko and turn into pseudo-headlines."""
        if not self._client:
            return []

        try:
            resp = await self._client.get(COINGECKO_TRENDING_URL)
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("coingecko_fetch_failed", error=str(e))
            return []

        headlines: list[Headline] = []
        for item in data.get("coins", []):
            coin = item.get("item", {})
            symbol = coin.get("symbol", "").upper()
            name = coin.get("name", symbol)
            score = coin.get("score", 0)
            headlines.append(
                Headline(
                    title=f"{name} ({symbol}) trending on CoinGecko (rank #{score + 1})",
                    source="coingecko_trending",
                    timestamp=time.time(),
                    currencies=[symbol],
                )
            )

        return headlines
