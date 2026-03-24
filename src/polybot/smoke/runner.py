"""Smoke test runner — run all checks at startup and halt on critical failures.

Usage (from bot entrypoint):
    from polybot.smoke.runner import run_smoke_tests

    result = await run_smoke_tests(settings)
    if not result.ok_to_start:
        sys.exit(f"Smoke tests failed: {result.failures}")

Or from CLI for manual verification:
    uv run python -m polybot.smoke.runner
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field

from polybot.smoke.checks import (
    CheckResult,
    check_clob_connectivity,
    check_clob_ws_reachable,
    check_controls_accessible,
    check_duplicate_ecs_tasks,
    check_engine_tick,
    check_gamma_api,
    check_kill_switch_off,
    check_mm_store_accessible,
    check_mode_valid,
    check_pairs_configured,
    check_polymarket_creds,
    check_strategy_profiles_load,
)

logger = logging.getLogger(__name__)


@dataclass
class SmokeResult:
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def passed(self) -> list[CheckResult]:
        return [c for c in self.checks if c.passed]

    @property
    def warnings(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed and not c.will_halt]

    @property
    def failures(self) -> list[CheckResult]:
        return [c for c in self.checks if not c.passed and c.will_halt]

    @property
    def ok_to_start(self) -> bool:
        return len(self.failures) == 0

    def print_summary(self) -> None:
        print()
        print("=" * 55)
        print("  MarketMaker Smoke Tests")
        print("=" * 55)
        for check in self.checks:
            print(check)
        print("=" * 55)
        print(f"  Passed:   {len(self.passed)}")
        print(f"  Warnings: {len(self.warnings)}")
        print(f"  Failures: {len(self.failures)}")
        print(f"  Status:   {'✓ OK TO START' if self.ok_to_start else '✗ HALT'}")
        print("=" * 55)
        print()


async def run_smoke_tests(settings) -> SmokeResult:
    """Run all startup checks concurrently. Returns SmokeResult."""
    result = SmokeResult()

    # Run connectivity checks concurrently (network-bound)
    connectivity = await asyncio.gather(
        check_clob_connectivity(),
        check_gamma_api(),
        check_clob_ws_reachable(),
        return_exceptions=True,
    )

    # Run local checks (fast, no network)
    local = await asyncio.gather(
        check_polymarket_creds(settings),
        check_mode_valid(settings),
        check_pairs_configured(settings),
        check_strategy_profiles_load(),
        check_engine_tick(),
        check_controls_accessible(settings),
        check_kill_switch_off(settings),
        check_mm_store_accessible(),
        check_duplicate_ecs_tasks(settings),
        return_exceptions=True,
    )

    for item in list(connectivity) + list(local):
        if isinstance(item, Exception):
            result.checks.append(CheckResult(
                name="unknown_check",
                passed=False,
                will_halt=False,
                message=f"Check raised exception: {item!s:.80}",
            ))
        else:
            result.checks.append(item)

    # Log summary
    logger.info(
        "smoke_tests_complete passed=%d warnings=%d failures=%d ok=%s",
        len(result.passed), len(result.warnings), len(result.failures), result.ok_to_start,
    )

    if result.failures:
        for f in result.failures:
            logger.error("smoke_critical_failure check=%s: %s", f.name, f.message)

    return result


def run_and_exit_on_failure(settings) -> SmokeResult:
    """Synchronous wrapper. Runs smoke tests and sys.exit(1) on critical failure."""
    result = asyncio.run(run_smoke_tests(settings))
    result.print_summary()
    if not result.ok_to_start:
        sys.exit(1)
    return result


# ─── CLI entry point ─────────────────────────────────────────────────────────

async def _cli_main() -> None:
    """Run smoke tests with minimal settings (for manual verification)."""
    import os

    class _MinimalSettings:
        polymarket_private_key = os.getenv("POLYMARKET_PRIVATE_KEY", "")
        polymarket_api_key = os.getenv("POLYMARKET_API_KEY", "")
        polymarket_api_secret = os.getenv("POLYMARKET_API_SECRET", "")
        polymarket_api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE", "")
        polymarket_chain_id = int(os.getenv("POLYMARKET_CHAIN_ID", "137"))
        polymarket_funder = os.getenv("POLYMARKET_FUNDER", "")
        mode = os.getenv("MODE", "paper")
        pairs = os.getenv("PAIRS", "")

    result = await run_smoke_tests(_MinimalSettings())
    result.print_summary()
    sys.exit(0 if result.ok_to_start else 1)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(_cli_main())
