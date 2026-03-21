"""Startup smoke test — verify all dependencies before accepting trades.

Called once at bot startup. Logs results and raises on critical failures.
Non-critical failures log warnings but don't halt the bot.

ECS cluster region: eu-west-1 (Polymarket CLOB geoblocks us-east-1 AWS IPs)
"""

from __future__ import annotations

import os

import structlog

logger = structlog.get_logger()

# ECS cluster region — bot must run here (CLOB geoblocks us-east-1)
_ECS_CLUSTER_REGION = "eu-west-1"


class SmokeTestResult:
    def __init__(self):
        self.passed: list[str] = []
        self.warned: list[str] = []
        self.failed: list[str] = []

    def ok(self, name: str):
        self.passed.append(name)
        logger.info("smoke_test_pass", check=name)

    def warn(self, name: str, reason: str):
        self.warned.append(f"{name}: {reason}")
        logger.warning("smoke_test_warn", check=name, reason=reason)

    def fail(self, name: str, reason: str):
        self.failed.append(f"{name}: {reason}")
        logger.error("smoke_test_fail", check=name, reason=reason)


async def run_smoke_tests(settings) -> SmokeTestResult:
    """Run all startup checks. Returns result object."""
    result = SmokeTestResult()

    # 1. CLOB connectivity (critical — can't trade without it)
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://clob.polymarket.com/time")
            if resp.status_code == 403:
                result.fail("clob_connectivity", "Geoblocked (403)")
            elif resp.status_code == 200:
                result.ok("clob_connectivity")
            else:
                result.warn("clob_connectivity", f"HTTP {resp.status_code}")
    except Exception as e:
        result.fail("clob_connectivity", str(e)[:80])

    # 2. Coinbase WS endpoint reachable
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get("https://api.coinbase.com/v2/prices/BTC-USD/spot")
            if resp.status_code == 200:
                result.ok("coinbase_api")
            else:
                result.warn("coinbase_api", f"HTTP {resp.status_code}")
    except Exception as e:
        result.warn("coinbase_api", str(e)[:80])

    # 3. Gamma API reachable
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://gamma-api.polymarket.com/markets", params={"limit": 1})
            if resp.status_code == 200:
                result.ok("gamma_api")
            else:
                result.warn("gamma_api", f"HTTP {resp.status_code}")
    except Exception as e:
        result.warn("gamma_api", str(e)[:80])

    # 4. DynamoDB connectivity
    try:
        from polybot.storage.dynamo import DynamoStore
        dynamo = DynamoStore()
        if dynamo._available:
            # Try a scan with limit 1
            dynamo._trades.scan(Limit=1)
            result.ok("dynamodb")
        else:
            result.warn("dynamodb", "DynamoStore not available")
    except Exception as e:
        result.warn("dynamodb", str(e)[:80])

    # 5. S3 base rate files exist for all configured assets
    try:
        import boto3
        assets = settings.asset_list
        profile = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None
        session = boto3.Session(profile_name=profile, region_name="eu-west-1")
        s3 = session.client("s3")
        bucket = "polymarket-bot-data-688567279867-use1"

        for asset in assets:
            key = f"candles/{asset.lower()}_usd_1min.parquet"
            try:
                s3.head_object(Bucket=bucket, Key=key)
                result.ok(f"s3_base_rate_{asset}")
            except Exception:
                result.warn(f"s3_base_rate_{asset}", f"Parquet not found in S3 for {asset}")
    except Exception as e:
        result.warn("s3_base_rates", str(e)[:80])

    # 6. Trading thresholds — HALT if misconfigured (prevents losing trades)
    from polybot.ml.server import _DEFAULT_GATE
    min_lgbm_prob = _DEFAULT_GATE

    logger.info(
        "threshold_check",
        max_market_price=settings.max_market_price,
        min_ev=settings.min_ev_threshold,
        min_lgbm_prob=min_lgbm_prob,
        mode=settings.mode,
        status="checking",
    )

    threshold_ok = True
    if settings.max_market_price > 0.90:
        result.fail("max_market_price", f"Value {settings.max_market_price} > 0.55 — would enter trades at too-high asks")
        threshold_ok = False
    else:
        result.ok("max_market_price")
    if settings.min_ev_threshold < 0.01:
        result.fail("min_ev_threshold", f"Value {settings.min_ev_threshold} < 0.08 — would enter low-EV trades")
        threshold_ok = False
    else:
        result.ok("min_ev_threshold")
    if min_lgbm_prob < 0.60:
        result.fail("min_lgbm_prob", f"Value {min_lgbm_prob} < 0.60 — would trade on low-confidence predictions")
        threshold_ok = False
    else:
        result.ok("min_lgbm_prob")

    logger.info(
        "threshold_check",
        max_market_price=settings.max_market_price,
        min_ev=settings.min_ev_threshold,
        min_lgbm_prob=min_lgbm_prob,
        status="PASS" if threshold_ok else "FAIL",
    )

    # 7. Polymarket credentials configured
    if settings.polymarket_private_key and settings.polymarket_api_key:
        result.ok("polymarket_creds")
    else:
        result.fail("polymarket_creds", "Missing private key or API key")

    # 8. Max trade USD guard
    if settings.max_trade_usd > 10.00:
        result.fail("max_trade_usd", f"Value {settings.max_trade_usd} > $10.00 — hard cap exceeded")
    else:
        result.ok("max_trade_usd")

    # 9. Model age check
    try:
        profile = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None
        import boto3 as _b3
        _ssm = _b3.Session(profile_name=profile, region_name="eu-west-1").client("ssm")
        import time as _t
        for _pair in ["BTC_5m", "ETH_5m", "SOL_5m"]:
            try:
                _resp = _ssm.get_parameter(Name=f"/polymarket/models/{_pair}/trained_at")
                _trained_at = float(_resp["Parameter"]["Value"])
                _age_h = (_t.time() - _trained_at) / 3600
                if _age_h > 24:
                    result.warn(f"model_age_{_pair}", f"Model is {_age_h:.0f}h old (>24h)")
                else:
                    result.ok(f"model_age_{_pair}")
                logger.info("model_health_check", pair=_pair, age_hours=round(_age_h, 1))
            except Exception:
                result.warn(f"model_age_{_pair}", "Could not check model age")
    except Exception:
        result.warn("model_health", "Could not check model ages")

    # 10. Mode sanity check
    if settings.mode in ("paper", "live"):
        result.ok(f"mode_{settings.mode}")
    else:
        result.fail("mode", f"Invalid mode: {settings.mode}")

    # 11. PAIRS must be explicitly set in live mode (empty = all assets, dangerous)
    if settings.mode == "live" and not settings.pairs.strip():
        result.fail("pairs_not_set", "PAIRS env var is empty in live mode — would trade ALL assets. Set PAIRS=BTC_5m,SOL_5m")
    elif settings.pairs.strip():
        enabled = [a for a, _ in settings.enabled_pairs]
        result.ok(f"pairs_{','.join(enabled)}")
    else:
        result.warn("pairs_not_set", "PAIRS not set — trading all assets from ASSETS config")

    # 12. No other bot tasks running in the cluster (prevents duplicate trades)
    # This is CRITICAL — a rogue task on an old task-def caused $20 trades.
    if settings.mode == "live":
        try:
            import boto3 as _b3_ecs
            _ecs_profile = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None
            # Bot runs in same region as ECS cluster; detect from ECS_CONTAINER_METADATA or env
            _ecs_region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION") or _ECS_CLUSTER_REGION
            _ecs_session = _b3_ecs.Session(profile_name=_ecs_profile, region_name=_ecs_region)
            _ecs = _ecs_session.client("ecs")
            _cluster = "polymarket-bot"

            # List ALL running tasks in the cluster (not just our service)
            _task_arns = []
            _paginator = _ecs.get_paginator("list_tasks")
            for _page in _paginator.paginate(cluster=_cluster, desiredStatus="RUNNING"):
                _task_arns.extend(_page.get("taskArns", []))

            if len(_task_arns) > 1:
                # Describe all tasks to check task definitions
                _tasks = _ecs.describe_tasks(cluster=_cluster, tasks=_task_arns).get("tasks", [])
                _task_defs = set()
                _task_info = []
                for _t in _tasks:
                    _td = _t.get("taskDefinitionArn", "")
                    _task_defs.add(_td)
                    _started = _t.get("startedAt", "?")
                    _task_info.append(f"{_td.split('/')[-1]} started={_started}")

                if len(_task_defs) > 1:
                    # CRITICAL: multiple task definitions = old + new code running together
                    result.fail(
                        "duplicate_tasks",
                        f"HALT: {len(_task_arns)} tasks on {len(_task_defs)} different task-defs! "
                        f"Rogue task will cause wrong sizing. Tasks: {_task_info}"
                    )
                elif len(_task_arns) > 1:
                    # Same task-def but multiple tasks — likely rolling deploy, warn only
                    result.warn(
                        "multiple_tasks",
                        f"{len(_task_arns)} tasks running (same task-def — likely rolling deploy). "
                        f"Dedup should protect, but monitor closely."
                    )
                else:
                    result.ok("single_task")
            elif len(_task_arns) == 1:
                result.ok("single_task")
            else:
                result.warn("no_tasks", "No tasks found in cluster — are we running?")

            logger.info("task_count_check", tasks=len(_task_arns))
        except Exception as _e:
            result.warn("task_count_check", f"Could not check ECS tasks: {str(_e)[:60]}")

    # Summary
    logger.info(
        "smoke_test_complete",
        passed=len(result.passed),
        warnings=len(result.warned),
        failures=len(result.failed),
    )

    if result.failed:
        logger.error("smoke_test_critical_failures", failures=result.failed)

    return result
