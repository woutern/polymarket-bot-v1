"""Opportunity bot — 6 parallel category workers, tiered trading.

Workers run concurrently every 30 min via asyncio.gather().
Tier 1: auto-trade at $0.85-$0.95. Tier 2: AI-checked at $0.65-$0.85.
FOK taker orders only. Dedup via condition_id in DynamoDB.

Usage:
    PYTHONPATH=src uv run python scripts/opportunity_bot.py
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx
import structlog
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds, CreateOrderOptions, OrderArgs, OrderType

from polybot.config import Settings
from polybot.core.logging import setup_logging

logger = structlog.get_logger()

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

TIER0_SIZE = 10.00  # High-conviction: ask>=0.93, <=6h, vol>=5K
TIER1_SIZE = 5.00
TIER2_SIZE = 2.50
MIN_SHARES = 5
MIN_VOLUME = 1_000
MAX_BUDGET = 1_250.00  # Total max deployed across all opportunity trades
SKIP_SLUGS = {"5m", "15m", "updown"}
SKIP_KEYWORDS = {"esports", "lol", "league-of-legends", "dota", "cs2", "valorant", "gaming"}

# Data-driven filters (from opportunity_analysis.txt)
MIN_ASK_OPP = 0.85       # Below $0.85 loses money (71% WR, -$3.42)
MAX_ASK_OPP = 0.94       # Above $0.94 margin too thin (-$3.60)
SKIP_HOURS_UTC = {6, 7, 8, 9, 10, 11}  # Morning 06-12 UTC: 76% WR, -$12.12
SKIP_RESOLVE_HOURS = (6, 12)  # 6-12h resolution window: 80% WR, -$8.90

# Tag IDs from Gamma /tags endpoint (cached at startup)
WORKER_TAGS: dict[str, list[str]] = {}

# Worker definitions: name → tag slugs to resolve to IDs at startup
WORKER_TAG_SLUGS = {
    "crypto": ["crypto", "crypto-prices", "bitcoin", "ethereum", "solana", "xrp", "bitcoin-prices",
               "ethereum-prices", "solana-prices", "xrp-prices", "cryptocurrency", "hit-price"],
    "finance": ["finance", "economics", "economy", "stocks", "earnings", "daily-close",
                "finance-updown", "tsla", "nvda", "nflx", "aapl", "meta"],
    "fed": ["fed", "fed-rates", "fomc", "federal-reserve", "jerome-powell", "fed-chair",
            "economic-policy"],
    # politics worker removed — -$7.91 all-time, Trump tweet markets not AI-predictable
    # tweets worker removed — too noisy, low edge
    "geopolitics": ["geopolitics", "iran", "world", "middle-east", "war", "ukraine", "russia",
                    "us-iran", "ukraine-peace-deal", "israel", "strait-of-hormuz",
                    "north-korea", "lebanon", "khamenei"],
    "elections": ["elections", "world-elections", "global-elections", "french-elections",
                  "german-elections", "slovenia-elections", "denmark-elections",
                  "peru-elections", "mayoral-elections", "special-elections"],
    "tech": ["tech", "ai", "technology", "ai-development", "openai", "open-ai", "big-tech",
             "gta-vi"],
    # basketball worker removed — needs live games, rarely matches 48h window
    "weather": ["temperature", "weather", "daily", "precipitation"],
    "culture": ["culture", "entertainment", "awards", "mrbeast", "youtube",
                "prediction-markets", "recurring"],
    "economics": ["cpi", "inflation", "unemployment", "gdp", "jobs", "fed-decision",
                  "interest-rates", "economic-data"],
    "companies": ["earnings", "apple", "tesla", "nvidia", "amazon", "google",
                  "microsoft", "meta", "stock-price", "revenue", "ipo"],
    "health": ["fda", "cdc", "outbreak", "approval", "clinical-trial",
               "disease", "pandemic", "measles", "bird-flu"],
    "iran": ["iran", "khamenei", "irgc", "tehran", "ceasefire-iran",
             "hormuz", "oil-strike"],
    "whitehouse": ["white-house", "lid", "briefing", "executive-order",
                   "trump-action", "cabinet", "press-secretary"],
}

ESPN_NBA = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_NCAA = "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard"


@dataclass
class Opp:
    question: str
    slug: str
    condition_id: str
    side: str
    price: float
    yes_price: float
    no_price: float
    yes_token: str
    no_token: str
    volume: float
    hours_left: float
    category: str
    end_date: str
    neg_risk: bool
    tier: int = 0
    worker: str = ""
    ai_verdict: str = ""
    ai_confidence: float = 0.0
    ai_reasoning: str = ""
    ai_trade: bool = False
    ai_true_prob: float = 0.0
    ai_edge: float = 0.0
    ai_model: str = ""


# ── STARTUP: resolve tag slugs → IDs ─────────────────────────────────────────


async def init_tag_ids():
    """Fetch /tags and map our slugs to numeric IDs."""
    global WORKER_TAGS
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            all_tags = []
            offset = 0
            while offset < 6000:
                r = await c.get(f"{GAMMA}/tags", params={"limit": 100, "offset": offset})
                batch = r.json() if r.status_code == 200 else []
                if not batch:
                    break
                all_tags.extend(batch)
                if len(batch) < 100:
                    break
                offset += 100

        slug_to_id = {t["slug"]: t["id"] for t in all_tags if "slug" in t and "id" in t}

        for worker, slugs in WORKER_TAG_SLUGS.items():
            ids = [slug_to_id[s] for s in slugs if s in slug_to_id]
            WORKER_TAGS[worker] = ids
            logger.info("tags_resolved", worker=worker, count=len(ids))
    except Exception as e:
        logger.error("tag_init_failed", error=str(e)[:60])


# ── LIVE CONTEXT ──────────────────────────────────────────────────────────────

_crypto_cache: dict[str, float] = {}
_crypto_ts: float = 0


async def get_crypto_prices() -> dict[str, float]:
    global _crypto_cache, _crypto_ts
    if time.time() - _crypto_ts < 300 and _crypto_cache:
        return _crypto_cache
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            for coin, pair in [("Bitcoin", "BTC-USD"), ("Ethereum", "ETH-USD"),
                               ("Solana", "SOL-USD"), ("XRP", "XRP-USD"), ("Dogecoin", "DOGE-USD")]:
                try:
                    r = await c.get(f"https://api.coinbase.com/v2/prices/{pair}/spot")
                    if r.status_code == 200:
                        _crypto_cache[coin] = float(r.json()["data"]["amount"])
                except Exception:
                    pass
        _crypto_ts = time.time()
    except Exception:
        pass
    return _crypto_cache


async def get_stock_price(ticker: str) -> float | None:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}",
                            params={"interval": "1m", "range": "1d"},
                            headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                return float(r.json()["chart"]["result"][0]["meta"]["regularMarketPrice"])
    except Exception:
        pass
    return None


async def get_live_basketball() -> list[dict]:
    """Return only IN-PROGRESS basketball games from ESPN."""
    live = []
    async with httpx.AsyncClient(timeout=10) as c:
        for url in [ESPN_NBA, ESPN_NCAA]:
            try:
                r = await c.get(url)
                if r.status_code != 200:
                    continue
                for ev in r.json().get("events", []):
                    state = ev.get("status", {}).get("type", {}).get("state", "")
                    if state != "in":
                        continue
                    detail = ev.get("status", {}).get("type", {}).get("shortDetail", "")
                    comps = ev.get("competitions", [{}])[0].get("competitors", [])
                    teams = []
                    for comp in comps:
                        teams.append({
                            "name": comp.get("team", {}).get("displayName", "?"),
                            "abbr": comp.get("team", {}).get("abbreviation", "?"),
                            "score": comp.get("score", "0"),
                        })
                    live.append({
                        "name": ev.get("shortName", "?"),
                        "detail": detail,
                        "teams": teams,
                        "summary": " vs ".join(f"{t['abbr']} {t['score']}" for t in teams) + f" ({detail})",
                    })
            except Exception:
                pass
    return live


# ── SCAN ──────────────────────────────────────────────────────────────────────


async def fetch_worker_markets(tag_ids: list[str], max_hours: int = 48) -> list[dict]:
    now = datetime.now(timezone.utc)
    end_min = (now + timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_max = (now + timedelta(hours=max_hours)).strftime("%Y-%m-%dT%H:%M:%SZ")

    all_events = []
    async with httpx.AsyncClient(timeout=30) as c:
        for tid in tag_ids[:8]:  # Cap to avoid too many calls
            try:
                r = await c.get(f"{GAMMA}/events", params={
                    "active": "true", "closed": "false",
                    "end_date_max": end_max, "end_date_min": end_min,
                    "order": "volume", "ascending": "false",
                    "limit": 50, "tag_id": tid,
                })
                if r.status_code == 200:
                    all_events.extend(r.json())
            except Exception:
                pass
    return all_events


def parse_opps(events: list[dict], worker: str) -> list[Opp]:
    now = datetime.now(timezone.utc)
    seen = set()
    out = []

    for ev in events:
        cat = ""
        for tag in (ev.get("tags") or []):
            if isinstance(tag, dict):
                cat = tag.get("label", "")
                break

        for m in ev.get("markets", []):
            slug = m.get("slug", "")
            cid = m.get("conditionId", "")
            if slug in seen or not cid:
                continue
            seen.add(slug)

            if any(s in slug.lower() for s in SKIP_SLUGS):
                continue
            if any(k in slug.lower() for k in SKIP_KEYWORDS):
                continue
            vol = float(m.get("volume") or 0)
            if vol < MIN_VOLUME:
                continue

            prices = m.get("outcomePrices", [])
            if isinstance(prices, str):
                try: prices = json.loads(prices)
                except: continue
            if len(prices) < 2:
                continue

            yp, np = float(prices[0]), float(prices[1])
            if yp >= np and MIN_ASK_OPP <= yp <= MAX_ASK_OPP:
                side, price = "YES", yp
            elif MIN_ASK_OPP <= np <= MAX_ASK_OPP:
                side, price = "NO", np
            else:
                continue

            tokens = m.get("clobTokenIds", [])
            if isinstance(tokens, str):
                tokens = json.loads(tokens)

            end_str = m.get("endDate") or ev.get("endDate") or ""
            try:
                end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                hrs = (end_dt - now).total_seconds() / 3600
            except:
                continue
            if hrs < 0.5:
                continue

            # Skip 6-12h resolution window (80% WR, -$8.90)
            if SKIP_RESOLVE_HOURS[0] <= hrs < SKIP_RESOLVE_HOURS[1]:
                continue

            # Tier (0 = $10 high-conviction, 1 = $5 auto, 2 = $2.50 AI-checked)
            if price >= 0.93 and hrs <= 6 and vol >= 5_000:
                tier = 0
            elif MIN_ASK_OPP <= price <= MAX_ASK_OPP and hrs <= 24:
                tier = 1
            elif MIN_ASK_OPP <= price <= MAX_ASK_OPP and 24 < hrs <= 48:
                tier = 2
            else:
                continue

            out.append(Opp(
                question=m.get("question") or ev.get("title", "?"),
                slug=slug, condition_id=cid, side=side, price=price,
                yes_price=yp, no_price=np,
                yes_token=tokens[0] if tokens else "",
                no_token=tokens[1] if len(tokens) > 1 else "",
                volume=vol, hours_left=hrs, category=cat, end_date=end_str,
                neg_risk=m.get("negRisk", False) or ev.get("negRisk", False),
                tier=tier, worker=worker,
            ))

    out.sort(key=lambda x: (-x.tier, -x.volume))
    return out


# ── AI ────────────────────────────────────────────────────────────────────────


async def ai_assess(opp: Opp, bedrock, context: str) -> Opp:
    pct = round(opp.price * 100, 1)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    model = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
    opp.ai_model = "haiku"

    prompt = (
        f"Market: {opp.question}\n"
        f"Current price: {opp.side} at ${opp.price:.2f} ({pct}% implied probability)\n"
        f"Resolves in: {opp.hours_left:.1f} hours\n"
        f"Volume: ${opp.volume:,.0f}\n"
        f"Live context: {context}\n"
        f"Today's date: {today}\n\n"
        f"Estimate the TRUE probability of this outcome based on your "
        f"knowledge and the live context provided.\n"
        f"If true_probability > market_implied + 0.15 and you are "
        f"confident, set trade=true.\n"
        f"If you cannot verify with confidence, set trade=false.\n\n"
        f"JSON only, no markdown:\n"
        f'{{"verdict":"strong_yes"|"likely_yes"|"uncertain"|"avoid",'
        f'"reasoning":"1 sentence max",'
        f'"confidence":0.0-1.0,'
        f'"true_probability":0.0-1.0,'
        f'"edge":true_probability-{opp.price:.2f},'
        f'"trade":true|false}}'
    )

    try:
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31", "max_tokens": 200,
            "system": "You are a prediction market trader. Be concise and decisive.",
            "messages": [{"role": "user", "content": prompt}],
        })
        r = bedrock.invoke_model(modelId=model, body=body)
        text = json.loads(r["body"].read()).get("content", [{}])[0].get("text", "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        d = json.loads(text)
        opp.ai_verdict = d.get("verdict", "uncertain")
        opp.ai_confidence = float(d.get("confidence", 0))
        opp.ai_reasoning = d.get("reasoning", "")[:200]
        opp.ai_true_prob = float(d.get("true_probability", 0))
        opp.ai_edge = float(d.get("edge", 0))
        opp.ai_trade = bool(d.get("trade", False))
    except Exception as e:
        logger.warning("ai_err", q=opp.question[:30], error=str(e)[:60])
    return opp


# ── TRADE ─────────────────────────────────────────────────────────────────────


async def ai_sanity_check(opp: Opp, bedrock, context: str) -> Opp:
    """Quick Haiku check: is this Tier 1 outcome near-certain?"""
    try:
        pct = round(opp.price * 100, 1)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        prompt = (
            f"Market: {opp.question}\n"
            f"Price: {opp.side} at ${opp.price:.2f} ({pct}% implied)\n"
            f"Resolves in: {opp.hours_left:.1f}h\n"
            f"Live context: {context}\n"
            f"Today: {today}\n\n"
            f"Is this outcome near-certain?\n"
            f'JSON only: {{"trade":true|false,"confidence":0.0-1.0,"reasoning":"1 sentence"}}'
        )
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31", "max_tokens": 100,
            "system": "You are a prediction market trader. Be concise.",
            "messages": [{"role": "user", "content": prompt}],
        })
        r = bedrock.invoke_model(modelId=HAIKU_MODEL, body=body)
        text = json.loads(r["body"].read()).get("content", [{}])[0].get("text", "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        d = json.loads(text)
        opp.ai_trade = bool(d.get("trade", False))
        opp.ai_confidence = float(d.get("confidence", 0))
        opp.ai_reasoning = d.get("reasoning", "")[:200]
        opp.ai_model = "haiku_sanity"
    except Exception as e:
        # On error, allow trade (fail-open for Tier 1)
        opp.ai_trade = True
        opp.ai_confidence = 0.80
        opp.ai_reasoning = f"Sanity check error: {str(e)[:40]}"
        logger.warning("sanity_check_err", q=opp.question[:30], error=str(e)[:60])
    return opp


SONNET_MODEL = "eu.anthropic.claude-sonnet-4-20250514-v1:0"
HAIKU_MODEL = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"


async def ai_sonnet_confirm(opp: Opp, bedrock, context: str) -> Opp:
    """Sonnet devil's advocate for Tier 0 ($10) trades. Must approve after Haiku passes."""
    try:
        pct = round(opp.price * 100, 1)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        end_utc = opp.end_date.replace("Z", " UTC") if opp.end_date else "unknown"
        prompt = (
            f"You are a prediction market risk analyst. Your job is to find reasons this trade could LOSE.\n\n"
            f"Market: {opp.question}\n"
            f"Side: {opp.side} at ${opp.price:.2f} ({pct}% implied probability)\n"
            f"Resolves in: {opp.hours_left:.1f}h (at {end_utc})\n"
            f"Volume: ${opp.volume:,.0f}\n"
            f"Live context: {context}\n"
            f"Today: {today}\n\n"
            f"Haiku assessment: confidence={opp.ai_confidence:.2f}, reasoning=\"{opp.ai_reasoning}\"\n\n"
            f"This is a $10 high-conviction bet. Before we place it:\n"
            f"1. What could go wrong? (late-breaking news, data delays, oracle quirks)\n"
            f"2. Is the market pricing this correctly at {pct}%?\n"
            f"3. Is there any reason the remaining {opp.hours_left:.1f}h could change the outcome?\n\n"
            f'JSON only, no markdown:\n'
            f'{{"approve":true|false,"confidence":0.0-1.0,"risk":"1 sentence on biggest risk","reasoning":"1 sentence"}}'
        )
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31", "max_tokens": 200,
            "system": "You are a cautious prediction market risk analyst. Only approve if you cannot find a plausible way this trade loses. Be concise.",
            "messages": [{"role": "user", "content": prompt}],
        })
        r = bedrock.invoke_model(modelId=SONNET_MODEL, body=body)
        text = json.loads(r["body"].read()).get("content", [{}])[0].get("text", "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        d = json.loads(text)
        sonnet_approve = bool(d.get("approve", False))
        sonnet_conf = float(d.get("confidence", 0))
        sonnet_risk = d.get("risk", "")[:200]
        sonnet_reasoning = d.get("reasoning", "")[:200]

        if not sonnet_approve or sonnet_conf < 0.85:
            opp.ai_trade = False
            opp.ai_reasoning = f"Sonnet veto: {sonnet_risk}"
            opp.ai_model = "haiku+sonnet_veto"
            logger.info("tier0_sonnet_veto", q=opp.question[:35], conf=sonnet_conf,
                        risk=sonnet_risk[:60], reasoning=sonnet_reasoning[:60])
        else:
            opp.ai_confidence = min(opp.ai_confidence, sonnet_conf)
            opp.ai_reasoning = f"Haiku+Sonnet confirmed. Risk: {sonnet_risk}"
            opp.ai_model = "haiku+sonnet"
            logger.info("tier0_sonnet_ok", q=opp.question[:35], conf=sonnet_conf,
                        risk=sonnet_risk[:60])
    except Exception as e:
        # On Sonnet error, fall back to Haiku-only (don't block the trade)
        opp.ai_model = "haiku_only_sonnet_err"
        logger.warning("sonnet_err", q=opp.question[:30], error=str(e)[:60])
    return opp


async def place_fok(client: ClobClient, opp: Opp) -> dict:
    """FOK taker at best ask. Never limit/GTC."""
    token = opp.yes_token if opp.side == "YES" else opp.no_token
    if not token:
        return {"success": False, "error": "No token"}

    size = TIER0_SIZE if opp.tier == 0 else TIER1_SIZE if opp.tier == 1 else TIER2_SIZE
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(f"{CLOB}/book", params={"token_id": token})
            asks = r.json().get("asks", []) if r.status_code == 200 else []
            if not asks:
                return {"success": False, "error": "Empty book"}
    except Exception as e:
        return {"success": False, "error": str(e)[:40]}

    best = min(asks, key=lambda a: float(a["price"]))
    price = round(float(best["price"]), 2)
    avail = float(best["size"])
    if price <= 0 or price >= 1:
        return {"success": False, "error": f"Bad ask: {price}"}
    if price > MAX_ASK_OPP:
        return {"success": False, "error": f"Ask {price} > ${MAX_ASK_OPP} cap"}

    shares = max(round(size / price, 0), MIN_SHARES)
    if shares > avail:
        shares = max(int(avail), MIN_SHARES)
    cost = round(shares * price, 2)

    try:
        signed = client.create_order(
            OrderArgs(token_id=token, price=price, size=shares, side="BUY"),
            CreateOrderOptions(tick_size="0.01", neg_risk=opp.neg_risk),
        )
        resp = client.post_order(signed, OrderType.FOK)
        ok = resp.get("success", False) if resp else False
        return {"success": ok, "order_id": resp.get("orderID", "") if resp else "",
                "price": price, "shares": shares, "cost": cost,
                "error": resp.get("errorMsg", "") if resp and not ok else ""}
    except Exception as e:
        return {"success": False, "error": str(e)[:80]}


# ── DYNAMO ────────────────────────────────────────────────────────────────────


def _dynamo():
    try:
        import boto3
        p = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None
        return boto3.Session(profile_name=p, region_name="eu-west-1").resource("dynamodb").Table(
            "polymarket-bot-opportunity-trades")
    except:
        return None


def _dec(v):
    from decimal import Decimal
    return Decimal(str(round(v, 6))) if isinstance(v, float) else v


_traded_cids: set[str] = set()
_total_deployed: float = 0.0  # Running total across all trades


def dedup_put(table, opp: Opp, result: dict) -> bool:
    """Atomic conditional put. Returns True if new."""
    if opp.condition_id in _traded_cids:
        return False
    if not table:
        _traded_cids.add(opp.condition_id)
        return True
    try:
        table.put_item(
            Item={k: _dec(v) if isinstance(v, float) else v for k, v in {
                "slug": opp.slug, "condition_id": opp.condition_id,
                "timestamp": time.time(), "question": opp.question[:200],
                "category": opp.category, "side": opp.side,
                "ask_price": result.get("price", opp.price),
                "size_usd": result.get("cost", 0), "shares": result.get("shares", 0),
                "order_id": result.get("order_id", ""),
                "tier": opp.tier, "worker": opp.worker,
                "ai_model": opp.ai_model or f"tier{opp.tier}",
                "ai_verdict": opp.ai_verdict or f"tier{opp.tier}_auto",
                "ai_confidence": opp.ai_confidence,
                "ai_reasoning": opp.ai_reasoning[:200] or f"Tier {opp.tier}",
                "ai_true_prob": opp.ai_true_prob, "ai_edge": round(opp.ai_edge, 4),
                "hours_left": round(opp.hours_left, 2), "end_date": opp.end_date,
                "volume": opp.volume, "neg_risk": opp.neg_risk,
                "resolved": 0, "outcome": "pending", "pnl": 0.0,
            }.items()},
            ConditionExpression="attribute_not_exists(slug)",
        )
        _traded_cids.add(opp.condition_id)
        return True
    except Exception as e:
        if "ConditionalCheckFailedException" in type(e).__name__:
            _traded_cids.add(opp.condition_id)
            return False
        return False


# ── RESOLUTION ────────────────────────────────────────────────────────────────


async def resolve(opp: Opp, result: dict, table):
    try:
        end = datetime.fromisoformat(opp.end_date.replace("Z", "+00:00"))
    except:
        return
    wait = (end - datetime.now(timezone.utc)).total_seconds() + 90
    if wait > 0:
        await asyncio.sleep(wait)

    winner = None
    for _ in range(5):
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = await c.get(f"{GAMMA}/markets", params={"slug": opp.slug})
                if r.status_code != 200:
                    await asyncio.sleep(60); continue
                ms = r.json()
                if not ms or not ms[0].get("closed"):
                    await asyncio.sleep(60); continue
                ps = ms[0].get("outcomePrices", [])
                if isinstance(ps, str): ps = json.loads(ps)
                if len(ps) >= 2:
                    yf = float(ps[0])
                    if yf >= 0.99: winner = "YES"
                    elif yf <= 0.01: winner = "NO"
                if winner: break
        except:
            pass
        await asyncio.sleep(60)

    if winner and table:
        won = opp.side == winner
        p = result.get("price", opp.price)
        pnl = round(result.get("shares", 0) * (1.0 - p), 2) if won else -result.get("cost", 0)
        try:
            from decimal import Decimal
            table.update_item(
                Key={"slug": opp.slug},
                UpdateExpression="SET resolved=:r, outcome=:o, pnl=:p",
                ExpressionAttributeValues={":r": 1, ":o": "won" if won else "lost",
                                           ":p": Decimal(str(round(pnl, 4)))},
            )
            logger.info("resolved", slug=opp.slug, won=won, pnl=pnl)
        except Exception as e:
            logger.warning("resolve_err", error=str(e)[:60])


async def resolve_all_pending(table):
    """Immediately check and resolve ALL unresolved trades past end_date.

    Does NOT use asyncio.create_task — resolves directly and synchronously.
    Called on startup AND after every scan cycle.
    """
    if not table:
        return
    try:
        resp = table.scan(FilterExpression="resolved = :r", ExpressionAttributeValues={":r": 0})
        items = resp.get("Items", [])
        while "LastEvaluatedKey" in resp:
            resp = table.scan(FilterExpression="resolved = :r",
                              ExpressionAttributeValues={":r": 0},
                              ExclusiveStartKey=resp["LastEvaluatedKey"])
            items.extend(resp.get("Items", []))

        now = datetime.now(timezone.utc)
        resolved_count = 0

        async with httpx.AsyncClient(timeout=10) as c:
            for item in items:
                end_str = item.get("end_date", "")
                if not end_str:
                    continue
                try:
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    if end_dt > now:
                        continue  # Not past end_date yet
                except:
                    continue

                slug = str(item.get("slug", ""))
                if not slug:
                    continue

                try:
                    r = await c.get(f"{GAMMA}/markets", params={"slug": slug})
                    if r.status_code != 200:
                        continue
                    ms = r.json()
                    if not ms or not ms[0].get("closed"):
                        continue
                    ps = ms[0].get("outcomePrices", [])
                    if isinstance(ps, str):
                        ps = json.loads(ps)
                    if len(ps) < 2:
                        continue
                    yf = float(ps[0])
                    if yf >= 0.99:
                        winner = "YES"
                    elif yf <= 0.01:
                        winner = "NO"
                    else:
                        continue

                    side = str(item.get("side", ""))
                    won = side == winner
                    ask = float(item.get("ask_price", 0))
                    shares = float(item.get("shares", 0))
                    pnl = round(shares * (1.0 - ask), 2) if won else -float(item.get("size_usd", 0))

                    from decimal import Decimal
                    table.update_item(
                        Key={"slug": slug},
                        UpdateExpression="SET resolved=:r, outcome=:o, pnl=:p",
                        ExpressionAttributeValues={
                            ":r": 1, ":o": "won" if won else "lost",
                            ":p": Decimal(str(round(pnl, 4))),
                        },
                    )
                    resolved_count += 1
                except Exception:
                    pass

        if resolved_count:
            logger.info("resolve_sweep", resolved=resolved_count)
    except Exception as e:
        logger.warning("resolve_pending_err", error=str(e)[:60])


# ── WORKER ────────────────────────────────────────────────────────────────────


async def fetch_worker(name: str) -> list[Opp]:
    """Fetch and parse markets for one worker. No trading — just return the list."""
    tag_ids = WORKER_TAGS.get(name, [])
    if not tag_ids:
        return []

    events = await fetch_worker_markets(tag_ids)
    opps = parse_opps(events, name)

    return opps


async def get_context(opp: Opp, crypto_prices: dict) -> str:
    """Fetch live context for a market based on its worker category."""
    ctx = []
    q_low = opp.question.lower()

    # Crypto prices
    for coin, cp in crypto_prices.items():
        if coin.lower() in q_low:
            ctx.append(f"Current {coin}: ${cp:,.2f}")

    # Stock prices
    if opp.worker == "finance":
        for tk in ["TSLA", "NVDA", "MSFT", "PLTR", "AAPL", "GOOGL", "AMZN", "META"]:
            if tk.lower() in q_low:
                sp = await get_stock_price(tk)
                if sp:
                    ctx.append(f"{tk}: ${sp:,.2f}")

    # Live basketball scores
    if opp.worker == "basketball":
        live = await get_live_basketball()
        for g in live:
            if any(t["abbr"].lower() in q_low for t in g["teams"]):
                ctx.append(f"LIVE: {g['summary']}")

    return "; ".join(ctx) or "No live data."


# ── MAIN ──────────────────────────────────────────────────────────────────────


async def run_scan():
    global _total_deployed

    # Skip morning 06-12 UTC (76% WR, -$12.12 from analysis)
    utc_hour = datetime.now(timezone.utc).hour
    if utc_hour in SKIP_HOURS_UTC:
        logger.info("scan_skipped_morning", utc_hour=utc_hour)
        return

    logger.info("scan_start", workers=len(WORKER_TAG_SLUGS))

    table = _dynamo()

    # CLOB client
    client = None
    try:
        s = Settings()
        creds = ApiCreds(api_key=s.polymarket_api_key, api_secret=s.polymarket_api_secret,
                         api_passphrase=s.polymarket_api_passphrase)
        funder = s.polymarket_funder or None
        sig = 2 if funder else 0
        client = ClobClient(host=CLOB, chain_id=s.polymarket_chain_id,
                            key=s.polymarket_private_key, creds=creds, signature_type=sig, funder=funder)
    except Exception as e:
        logger.error("clob_err", error=str(e)[:60])

    # Bedrock
    bedrock = None
    try:
        import boto3
        p = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None
        bedrock = boto3.Session(profile_name=p, region_name="eu-west-1").client("bedrock-runtime")
    except Exception as e:
        logger.warning("bedrock_err", error=str(e)[:60])

    # Step 1: All workers fetch in parallel
    worker_results = await asyncio.gather(
        *[fetch_worker(name) for name in WORKER_TAG_SLUGS],
        return_exceptions=True,
    )

    # Step 2: Combine + dedup by condition_id
    seen_cids = set()
    all_opps = []
    worker_counts = {}
    for name, result in zip(WORKER_TAG_SLUGS, worker_results):
        if isinstance(result, Exception):
            logger.error("worker_fetch_err", worker=name, error=str(result)[:80])
            worker_counts[name] = 0
            continue
        worker_counts[name] = len(result)
        for opp in result:
            if opp.condition_id not in seen_cids and opp.condition_id not in _traded_cids:
                seen_cids.add(opp.condition_id)
                all_opps.append(opp)

    # Step 3: Sort by end_date ascending (soonest first)
    all_opps.sort(key=lambda o: o.end_date)

    logger.info("combined", total=len(all_opps), workers=worker_counts)

    # Crypto prices (shared across all markets)
    crypto_prices = await get_crypto_prices()

    # Check pause flag
    opp_paused = os.getenv("OPPORTUNITY_BOT_PAUSED", "").lower() == "true"
    if opp_paused:
        logger.info("opportunity_paused_mode", total=len(all_opps))

    # Step 4: Iterate in order — trade until budget hit
    traded = 0
    t0_count = 0
    t1_count = 0
    t2_count = 0

    for opp in all_opps:
        if _total_deployed >= MAX_BUDGET:
            logger.info("budget_maxed", deployed=round(_total_deployed, 2))
            break

        if opp.tier in (0, 1):
            if opp.tier == 0:
                t0_count += 1
            else:
                t1_count += 1
            if not client:
                continue
            # Haiku sanity check before auto-trading
            if bedrock:
                context = await get_context(opp, crypto_prices)
                opp = await ai_sanity_check(opp, bedrock, context)
                haiku_gate = 0.90 if opp.tier == 0 else 0.80
                if not opp.ai_trade or opp.ai_confidence < haiku_gate:
                    logger.info("haiku_veto", tier=opp.tier, q=opp.question[:35],
                                conf=opp.ai_confidence, reasoning=opp.ai_reasoning[:60])
                    continue
                # Tier 0: Sonnet devil's advocate confirmation
                if opp.tier == 0:
                    opp = await ai_sonnet_confirm(opp, bedrock, context)
                    if not opp.ai_trade:
                        continue
            if opp_paused:
                logger.info("opportunity_paused", tier=opp.tier, w=opp.worker,
                            q=opp.question[:35], s=opp.side, p=opp.price,
                            conf=opp.ai_confidence, model=opp.ai_model)
                continue
            if not dedup_put(table, opp, {"price": opp.price, "cost": 0, "shares": 0, "order_id": ""}):
                continue
            result = await place_fok(client, opp)
            if result["success"]:
                traded += 1
                _total_deployed += result.get("cost", 0)
                if table:
                    try:
                        from decimal import Decimal
                        table.update_item(
                            Key={"slug": opp.slug},
                            UpdateExpression="SET ask_price=:p, size_usd=:c, shares=:s, order_id=:o",
                            ExpressionAttributeValues={
                                ":p": Decimal(str(result["price"])), ":c": Decimal(str(result["cost"])),
                                ":s": Decimal(str(result["shares"])), ":o": result["order_id"]},
                        )
                    except: pass
                asyncio.create_task(resolve(opp, result, table))
                logger.info("T1", w=opp.worker, q=opp.question[:35], s=opp.side,
                            p=result["price"], cost=result["cost"])
            else:
                if table:
                    try: table.delete_item(Key={"slug": opp.slug})
                    except: pass
                _traded_cids.discard(opp.condition_id)

        elif opp.tier == 2 and bedrock:
            t2_count += 1
            context = await get_context(opp, crypto_prices)
            opp = await ai_assess(opp, bedrock, context)

            if opp.ai_trade and opp.ai_confidence >= 0.85 and opp.ai_edge >= 0.15:
                if opp_paused:
                    logger.info("opportunity_paused", tier=2, w=opp.worker,
                                q=opp.question[:35], s=opp.side, p=opp.price,
                                conf=opp.ai_confidence, edge=opp.ai_edge)
                    continue
                if not client or _total_deployed >= MAX_BUDGET:
                    continue
                if not dedup_put(table, opp, {"price": opp.price, "cost": 0, "shares": 0, "order_id": ""}):
                    continue
                result = await place_fok(client, opp)
                if result["success"]:
                    traded += 1
                    _total_deployed += result.get("cost", 0)
                    if table:
                        try:
                            from decimal import Decimal
                            table.update_item(
                                Key={"slug": opp.slug},
                                UpdateExpression="SET ask_price=:p, size_usd=:c, shares=:s, order_id=:o, "
                                                 "ai_verdict=:v, ai_confidence=:co, ai_reasoning=:r, ai_edge=:e, ai_model=:m",
                                ExpressionAttributeValues={
                                    ":p": Decimal(str(result["price"])), ":c": Decimal(str(result["cost"])),
                                    ":s": Decimal(str(result["shares"])), ":o": result["order_id"],
                                    ":v": opp.ai_verdict, ":co": Decimal(str(opp.ai_confidence)),
                                    ":r": opp.ai_reasoning[:200], ":e": Decimal(str(round(opp.ai_edge, 4))),
                                    ":m": opp.ai_model},
                            )
                        except: pass
                    asyncio.create_task(resolve(opp, result, table))
                    logger.info("T2", w=opp.worker, q=opp.question[:35], s=opp.side,
                                p=result["price"], cost=result["cost"], edge=round(opp.ai_edge, 2))
                else:
                    if table:
                        try: table.delete_item(Key={"slug": opp.slug})
                        except: pass
                    _traded_cids.discard(opp.condition_id)

    print(f"\n{'═' * 75}")
    print(f"  SCAN — {datetime.now(timezone.utc).strftime('%H:%M UTC')}")
    print(f"{'═' * 75}")
    for name in WORKER_TAG_SLUGS:
        print(f"  {name:<13} fetched={worker_counts.get(name, 0):>3}")
    print(f"  {'─' * 70}")
    print(f"  Combined: {len(all_opps)} unique | T0={t0_count} T1={t1_count} T2={t2_count} | Traded={traded} | Deployed=${_total_deployed:.2f}")

    logger.info("scan_done", scanned=len(all_opps), t0=t0_count, t1=t1_count, t2=t2_count, traded=traded,
                deployed=round(_total_deployed, 2))

    # Resolve any pending trades after every scan
    await resolve_all_pending(table)


async def main():
    setup_logging("INFO")
    logger.info("bot_starting")

    await init_tag_ids()

    table = _dynamo()
    await resolve_all_pending(table)

    # Load existing condition_ids for dedup + total deployed
    global _total_deployed
    if table:
        try:
            resp = table.scan(ProjectionExpression="condition_id, size_usd, resolved")
            for item in resp.get("Items", []):
                _traded_cids.add(item.get("condition_id", ""))
                if not int(item.get("resolved", 0)):
                    _total_deployed += float(item.get("size_usd", 0))
            logger.info("dedup_loaded", count=len(_traded_cids), deployed=round(_total_deployed, 2))
        except:
            pass

    await run_scan()

    while True:
        now = datetime.now(timezone.utc)
        if now.minute < 30:
            nxt = now.replace(minute=30, second=0, microsecond=0)
        else:
            nxt = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        wait = (nxt - now).total_seconds()
        logger.info("next_scan", time=nxt.strftime("%H:%M UTC"), wait_min=round(wait / 60, 1))
        await asyncio.sleep(wait)
        try:
            await run_scan()
        except Exception as e:
            logger.error("scan_err", error=str(e)[:100])


if __name__ == "__main__":
    asyncio.run(main())
