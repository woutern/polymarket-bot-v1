"""Tests for smoke/checks.py and smoke/runner.py.

All network calls are stubbed — these tests run fully offline.
"""

from __future__ import annotations

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from polybot.smoke.checks import (
    CheckResult,
    check_clob_connectivity,
    check_controls_accessible,
    check_engine_tick,
    check_kill_switch_off,
    check_mm_store_accessible,
    check_mode_valid,
    check_pairs_configured,
    check_polymarket_creds,
    check_strategy_profiles_load,
    check_duplicate_ecs_tasks,
)
from polybot.smoke.runner import SmokeResult, run_smoke_tests


# ─── Settings stubs ──────────────────────────────────────────────────────────

class _Settings:
    polymarket_private_key = "0xdeadbeef"
    polymarket_api_key = "api_key"
    polymarket_api_secret = "api_secret"
    polymarket_api_passphrase = "passphrase"
    polymarket_chain_id = 137
    polymarket_funder = ""
    mode = "paper"
    pairs = "BTC_5M"


class _LiveSettings(_Settings):
    mode = "live"
    pairs = "BTC_5M"


class _EmptyCredsSettings(_Settings):
    polymarket_private_key = ""
    polymarket_api_key = ""


class _BadModeSettings(_Settings):
    mode = "staging"


class _LiveNoPairsSettings(_Settings):
    mode = "live"
    pairs = ""


# ─── CheckResult ─────────────────────────────────────────────────────────────

class TestCheckResult:
    def test_str_passed(self):
        r = CheckResult("my_check", True, True, "OK")
        assert "✓" in str(r)
        assert "my_check" in str(r)

    def test_str_critical_fail(self):
        r = CheckResult("my_check", False, True, "bad")
        assert "✗" in str(r)

    def test_str_warning(self):
        r = CheckResult("my_check", False, False, "meh")
        assert "⚠" in str(r)


# ─── Individual checks ───────────────────────────────────────────────────────

class TestCheckClobConnectivity:
    @pytest.mark.asyncio
    async def test_ok_on_200(self):
        mock_resp = MagicMock(status_code=200)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("polybot.smoke.checks.httpx.AsyncClient", return_value=mock_client):
            result = await check_clob_connectivity()
        assert result.passed is True
        assert result.will_halt is True

    @pytest.mark.asyncio
    async def test_fail_on_403(self):
        mock_resp = MagicMock(status_code=403)
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)
        with patch("polybot.smoke.checks.httpx.AsyncClient", return_value=mock_client):
            result = await check_clob_connectivity()
        assert result.passed is False
        assert result.will_halt is True
        assert "403" in result.message

    @pytest.mark.asyncio
    async def test_fail_on_exception(self):
        with patch("polybot.smoke.checks.httpx.AsyncClient", side_effect=Exception("timeout")):
            result = await check_clob_connectivity()
        assert result.passed is False
        assert result.will_halt is True


class TestCheckPolymarketCreds:
    @pytest.mark.asyncio
    async def test_pass_with_creds(self):
        result = await check_polymarket_creds(_Settings())
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_fail_missing_creds(self):
        result = await check_polymarket_creds(_EmptyCredsSettings())
        assert result.passed is False
        assert result.will_halt is True
        assert "private_key" in result.message
        assert "api_key" in result.message


class TestCheckModeValid:
    @pytest.mark.asyncio
    async def test_paper_valid(self):
        result = await check_mode_valid(_Settings())
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_live_valid(self):
        result = await check_mode_valid(_LiveSettings())
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_invalid_mode(self):
        result = await check_mode_valid(_BadModeSettings())
        assert result.passed is False
        assert result.will_halt is True


class TestCheckPairsConfigured:
    @pytest.mark.asyncio
    async def test_paper_no_pairs_ok(self):
        class S(_Settings):
            pairs = ""
        result = await check_pairs_configured(S())
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_live_with_pairs_ok(self):
        result = await check_pairs_configured(_LiveSettings())
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_live_no_pairs_fails(self):
        result = await check_pairs_configured(_LiveNoPairsSettings())
        assert result.passed is False
        assert result.will_halt is True


class TestCheckStrategyProfilesLoad:
    @pytest.mark.asyncio
    async def test_profiles_load(self):
        result = await check_strategy_profiles_load()
        assert result.passed is True
        assert result.will_halt is True
        assert "profiles loaded" in result.message


class TestCheckEngineTick:
    @pytest.mark.asyncio
    async def test_engine_ticks_ok(self):
        result = await check_engine_tick()
        assert result.passed is True
        assert result.will_halt is True


class TestCheckControlsAccessible:
    @pytest.mark.asyncio
    async def test_paper_mode_uses_in_memory(self):
        result = await check_controls_accessible(_Settings())
        assert result.passed is True
        assert "InMemoryControls" in result.message

    @pytest.mark.asyncio
    async def test_live_mode_uses_bot_controls(self):
        # BotControls falls back gracefully when DynamoDB unavailable
        result = await check_controls_accessible(_LiveSettings())
        assert result.passed is True


class TestCheckKillSwitchOff:
    @pytest.mark.asyncio
    async def test_paper_mode_skips(self):
        result = await check_kill_switch_off(_Settings())
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_live_kill_switch_off(self):
        from polybot.core.controls import BotControls
        mock_ctrl = MagicMock()
        mock_ctrl.kill_switch = False
        mock_ctrl._dynamo_available = False
        with patch("polybot.smoke.checks.BotControls", return_value=mock_ctrl):
            result = await check_kill_switch_off(_LiveSettings())
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_live_kill_switch_on_fails(self):
        from polybot.core.controls import BotControls
        mock_ctrl = MagicMock()
        mock_ctrl.kill_switch = True
        with patch("polybot.smoke.checks.BotControls", return_value=mock_ctrl):
            result = await check_kill_switch_off(_LiveSettings())
        assert result.passed is False
        assert result.will_halt is True


class TestCheckMMStoreAccessible:
    @pytest.mark.asyncio
    async def test_store_available(self):
        from polybot.storage.mm_store import MMStore
        mock_store = MagicMock()
        mock_store._available = True
        with patch("polybot.smoke.checks.MMStore", return_value=mock_store):
            result = await check_mm_store_accessible()
        assert result.passed is True
        assert result.will_halt is False  # warning only

    @pytest.mark.asyncio
    async def test_store_unavailable_is_warning(self):
        mock_store = MagicMock()
        mock_store._available = False
        with patch("polybot.smoke.checks.MMStore", return_value=mock_store):
            result = await check_mm_store_accessible()
        assert result.passed is False
        assert result.will_halt is False  # warning, not halt


class TestCheckDuplicateEcsTasks:
    @pytest.mark.asyncio
    async def test_paper_mode_skipped(self):
        result = await check_duplicate_ecs_tasks(_Settings())
        assert result.passed is True
        assert "skipped" in result.message.lower()

    @pytest.mark.asyncio
    async def test_single_task_ok(self):
        mock_ecs = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [{"taskArns": ["arn:aws:ecs:eu-west-1:1234:task/abc"]}]
        mock_ecs.get_paginator.return_value = mock_paginator
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ecs
        with patch("polybot.smoke.checks.boto3.Session", return_value=mock_session):
            result = await check_duplicate_ecs_tasks(_LiveSettings())
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_multiple_task_defs_halts(self):
        mock_ecs = MagicMock()
        mock_paginator = MagicMock()
        mock_paginator.paginate.return_value = [
            {"taskArns": ["arn:task/a", "arn:task/b"]}
        ]
        mock_ecs.get_paginator.return_value = mock_paginator
        mock_ecs.describe_tasks.return_value = {
            "tasks": [
                {"taskDefinitionArn": "arn:taskdef/bot:1"},
                {"taskDefinitionArn": "arn:taskdef/bot:2"},
            ]
        }
        mock_session = MagicMock()
        mock_session.client.return_value = mock_ecs
        with patch("polybot.smoke.checks.boto3.Session", return_value=mock_session):
            result = await check_duplicate_ecs_tasks(_LiveSettings())
        assert result.passed is False
        assert result.will_halt is True


# ─── SmokeResult ─────────────────────────────────────────────────────────────

class TestSmokeResult:
    def test_ok_to_start_when_no_failures(self):
        r = SmokeResult()
        r.checks = [CheckResult("a", True, True, "ok"), CheckResult("b", True, False, "ok")]
        assert r.ok_to_start is True

    def test_not_ok_when_critical_failure(self):
        r = SmokeResult()
        r.checks = [
            CheckResult("a", True, True, "ok"),
            CheckResult("b", False, True, "fail"),
        ]
        assert r.ok_to_start is False

    def test_ok_when_only_warnings(self):
        r = SmokeResult()
        r.checks = [
            CheckResult("a", True, True, "ok"),
            CheckResult("b", False, False, "warning only"),
        ]
        assert r.ok_to_start is True

    def test_passed_warnings_failures_counts(self):
        r = SmokeResult()
        r.checks = [
            CheckResult("a", True, True, "ok"),
            CheckResult("b", False, False, "warn"),
            CheckResult("c", False, True, "fail"),
        ]
        assert len(r.passed) == 1
        assert len(r.warnings) == 1
        assert len(r.failures) == 1


# ─── run_smoke_tests integration ────────────────────────────────────────────

class TestRunSmokeTests:
    @pytest.mark.asyncio
    async def test_all_pass_in_paper_mode_offline(self):
        """Paper mode with mocked network — all critical checks should pass."""
        mock_resp_200 = MagicMock(status_code=200)
        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        mock_http_client.get = AsyncMock(return_value=mock_resp_200)

        mock_ws = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=None)

        mock_store = MagicMock()
        mock_store._available = True

        with (
            patch("polybot.smoke.checks.httpx.AsyncClient", return_value=mock_http_client),
            patch("polybot.smoke.checks.websockets.connect", return_value=mock_ws),
            patch("polybot.smoke.checks.MMStore", return_value=mock_store),
        ):
            result = await run_smoke_tests(_Settings())

        assert result.ok_to_start is True
        assert len(result.failures) == 0

    @pytest.mark.asyncio
    async def test_critical_failure_when_clob_403(self):
        mock_resp_403 = MagicMock(status_code=403)
        mock_http_client = AsyncMock()
        mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
        mock_http_client.__aexit__ = AsyncMock(return_value=None)
        mock_http_client.get = AsyncMock(return_value=mock_resp_403)

        mock_ws = AsyncMock()
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=None)

        mock_store = MagicMock()
        mock_store._available = False

        with (
            patch("polybot.smoke.checks.httpx.AsyncClient", return_value=mock_http_client),
            patch("polybot.smoke.checks.websockets.connect", return_value=mock_ws),
            patch("polybot.smoke.checks.MMStore", return_value=mock_store),
        ):
            result = await run_smoke_tests(_Settings())

        assert result.ok_to_start is False
        failure_names = [f.name for f in result.failures]
        assert "clob_connectivity" in failure_names
