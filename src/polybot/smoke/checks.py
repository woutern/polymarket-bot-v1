"""MarketMaker startup checks.

Each check is an async function that returns a CheckResult.
Checks are grouped:
  CRITICAL  — bot halts if any fail (will_halt=True)
  WARNING   — logged but bot continues

Checks:
  1.  clob_connectivity       CRITICAL — CLOB reachable, not geoblocked
  2.  polymarket_creds         CRITICAL — private key + API key present
  3.  mode_valid               CRITICAL — mode is "paper" or "live"
  4.  pairs_configured         CRITICAL — PAIRS set in live mode
  5.  strategy_profiles_load   CRITICAL — profiles instantiate without error
  6.  engine_tick              CRITICAL — engine ticks without exception
  7.  controls_accessible      CRITICAL — BotControls initialises (DynamoDB or fallback)
  8.  kill_switch_off          CRITICAL — kill switch not active at startup
  9.  mm_store_accessible      WARNING  — DynamoDB MM tables reachable
  10. gamma_api                WARNING  — Gamma API reachable
  11. clob_ws_reachable         WARNING  — WebSocket URL resolves
  12. model_configured          WARNING  — model available for pair (optional)
  13. duplicate_ecs_tasks       CRITICAL — no rogue ECS task (live mode only)
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field

import boto3
import httpx
import websockets

from polybot.core.controls import BotControls, InMemoryControls
from polybot.storage.mm_store import MMStore

logger = logging.getLogger(__name__)

CLOB_URL = "https://clob.polymarket.com"
CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
GAMMA_URL = "https://gamma-api.polymarket.com"
_ECS_CLUSTER = "polymarket-bot"
_ECS_REGION = "eu-west-1"


@dataclass
class CheckResult:
    name: str
    passed: bool
    will_halt: bool       # True → bot must not start if passed=False
    message: str = ""

    def __str__(self) -> str:
        icon = "✓" if self.passed else ("✗" if self.will_halt else "⚠")
        return f"  {icon}  {self.name:<35} {self.message}"


# ─── Individual checks ───────────────────────────────────────────────────────

async def check_clob_connectivity() -> CheckResult:
    name = "clob_connectivity"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            resp = await c.get(f"{CLOB_URL}/time")
        if resp.status_code == 403:
            return CheckResult(name, False, True, "Geoblocked (403) — bot cannot trade from this region")
        if resp.status_code == 200:
            return CheckResult(name, True, True, "OK")
        return CheckResult(name, False, True, f"HTTP {resp.status_code}")
    except Exception as exc:
        return CheckResult(name, False, True, str(exc)[:80])


async def check_polymarket_creds(settings) -> CheckResult:
    name = "polymarket_creds"
    has_key = bool(getattr(settings, "polymarket_private_key", ""))
    has_api = bool(getattr(settings, "polymarket_api_key", ""))
    if has_key and has_api:
        return CheckResult(name, True, True, "Private key + API key present")
    missing = []
    if not has_key:
        missing.append("private_key")
    if not has_api:
        missing.append("api_key")
    return CheckResult(name, False, True, f"Missing: {', '.join(missing)}")


async def check_mode_valid(settings) -> CheckResult:
    name = "mode_valid"
    mode = getattr(settings, "mode", "")
    if mode in ("paper", "live"):
        return CheckResult(name, True, True, f"mode={mode}")
    return CheckResult(name, False, True, f"Invalid mode={mode!r} — must be 'paper' or 'live'")


async def check_pairs_configured(settings) -> CheckResult:
    name = "pairs_configured"
    mode = getattr(settings, "mode", "paper")
    pairs = getattr(settings, "pairs", "").strip()
    if mode == "live" and not pairs:
        return CheckResult(name, False, True, "PAIRS env var empty in live mode — would trade ALL assets")
    if pairs:
        return CheckResult(name, True, True, f"PAIRS={pairs}")
    return CheckResult(name, True, False, "PAIRS not set (paper mode — OK)")


async def check_strategy_profiles_load() -> CheckResult:
    name = "strategy_profiles_load"
    try:
        from polybot.strategy.profiles import ALL_PROFILES, get_profile
        for key in ALL_PROFILES:
            get_profile(key)
        return CheckResult(name, True, True, f"{len(ALL_PROFILES)} profiles loaded")
    except Exception as exc:
        return CheckResult(name, False, True, str(exc)[:80])


async def check_engine_tick() -> CheckResult:
    name = "engine_tick"
    try:
        from polybot.core.engine import Engine
        from polybot.strategy.base import MarketState
        engine = Engine(pair="BTC_5M", mode="paper")
        state = MarketState(seconds=10, yes_bid=0.52, no_bid=0.48, yes_ask=0.53, no_ask=0.49, prob_up=0.55)
        action = engine.run_tick(state)
        assert hasattr(action, "buy_up_shares"), "action missing buy_up_shares"
        return CheckResult(name, True, True, "Engine tick OK")
    except Exception as exc:
        return CheckResult(name, False, True, str(exc)[:80])


async def check_controls_accessible(settings) -> CheckResult:
    name = "controls_accessible"
    mode = getattr(settings, "mode", "paper")
    try:
        if mode == "paper":
            ctrl = InMemoryControls()
            assert ctrl.kill_switch is False
            return CheckResult(name, True, True, "InMemoryControls OK (paper mode)")
        else:
            ctrl = BotControls()
            # Falls back gracefully when DynamoDB unavailable — still OK
            dynamo_ok = getattr(ctrl, "_dynamo_available", False)
            return CheckResult(name, True, True, f"BotControls OK (dynamo={dynamo_ok})")
    except Exception as exc:
        # Strip any structlog-style keyword args from the error message
        msg = str(exc)
        if "unexpected keyword argument" in msg:
            msg = "BotControls init: " + msg[:80]
        return CheckResult(name, False, True, msg[:80])


async def check_kill_switch_off(settings) -> CheckResult:
    name = "kill_switch_off"
    mode = getattr(settings, "mode", "paper")
    try:
        if mode == "paper":
            return CheckResult(name, True, True, "Paper mode — no kill switch")
        ctrl = BotControls()
        if ctrl.kill_switch:
            return CheckResult(name, False, True, "Kill switch is ACTIVE — clear DynamoDB flag before starting")
        return CheckResult(name, True, True, "Kill switch off")
    except Exception as exc:
        return CheckResult(name, True, False, f"Could not check kill switch: {exc!s:.60}")


async def check_mm_store_accessible() -> CheckResult:
    name = "mm_store_accessible"
    try:
        store = MMStore()
        if store._available:
            return CheckResult(name, True, False, "DynamoDB MM tables reachable")
        return CheckResult(name, False, False, "DynamoDB unavailable — window logs will be lost")
    except Exception as exc:
        return CheckResult(name, False, False, str(exc)[:80])


async def check_gamma_api() -> CheckResult:
    name = "gamma_api"
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            resp = await c.get(f"{GAMMA_URL}/markets", params={"limit": 1})
        if resp.status_code == 200:
            return CheckResult(name, True, False, "OK")
        return CheckResult(name, False, False, f"HTTP {resp.status_code}")
    except Exception as exc:
        return CheckResult(name, False, False, str(exc)[:80])


async def check_clob_ws_reachable() -> CheckResult:
    """Try a brief WebSocket handshake to confirm the URL resolves."""
    name = "clob_ws_reachable"
    try:
        async with websockets.connect(CLOB_WS_URL, open_timeout=5, close_timeout=2):
            pass
        return CheckResult(name, True, False, "WebSocket handshake OK")
    except Exception as exc:
        msg = str(exc)[:80]
        # Connection refused / timeout is a warning, not critical
        return CheckResult(name, False, False, msg)


async def check_duplicate_ecs_tasks(settings) -> CheckResult:
    name = "duplicate_ecs_tasks"
    mode = getattr(settings, "mode", "paper")
    if mode != "live":
        return CheckResult(name, True, True, "Paper mode — skipped")
    try:
        profile = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None
        region = os.getenv("AWS_REGION") or _ECS_REGION
        session = boto3.Session(profile_name=profile, region_name=region)
        ecs = session.client("ecs")

        arns: list[str] = []
        paginator = ecs.get_paginator("list_tasks")
        for page in paginator.paginate(cluster=_ECS_CLUSTER, desiredStatus="RUNNING"):
            arns.extend(page.get("taskArns", []))

        if not arns:
            return CheckResult(name, True, True, "No running tasks found")
        if len(arns) == 1:
            return CheckResult(name, True, True, "Single task running — OK")

        tasks = ecs.describe_tasks(cluster=_ECS_CLUSTER, tasks=arns).get("tasks", [])
        task_defs = {t.get("taskDefinitionArn", "") for t in tasks}
        if len(task_defs) > 1:
            return CheckResult(
                name, False, True,
                f"HALT: {len(arns)} tasks across {len(task_defs)} task-defs — rogue task detected"
            )
        return CheckResult(name, True, True, f"{len(arns)} tasks, same task-def (rolling deploy)")
    except Exception as exc:
        return CheckResult(name, True, False, f"Could not check ECS: {exc!s:.60}")
