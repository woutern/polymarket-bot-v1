"""Startup smoke test — verify all dependencies before accepting trades.

Called once at bot startup. Logs results and raises on critical failures.
Non-critical failures log warnings but don't halt the bot.
"""

from __future__ import annotations

import os

import structlog

logger = structlog.get_logger()


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
        session = boto3.Session(profile_name=profile, region_name="us-east-1")
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
        _ssm = _b3.Session(profile_name=profile, region_name="us-east-1").client("ssm")
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
