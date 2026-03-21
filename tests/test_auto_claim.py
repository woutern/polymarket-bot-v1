"""Smoke tests for auto-claim script (claim_winnings.js)."""

import os
import json
import subprocess

import pytest

SCRIPTS_DIR = os.path.join(os.path.dirname(__file__), "..", "scripts")
ROOT_DIR = os.path.join(os.path.dirname(__file__), "..")


class TestAutoClaimScript:
    """The claim script must exist and be well-formed."""

    def test_script_exists(self):
        path = os.path.join(SCRIPTS_DIR, "claim_winnings.js")
        assert os.path.exists(path), "claim_winnings.js must exist"

    def test_script_has_redeem_function(self):
        path = os.path.join(SCRIPTS_DIR, "claim_winnings.js")
        with open(path) as f:
            content = f.read()
        assert "claimWinnings" in content
        assert "redeemPositions" in content

    def test_script_uses_official_packages(self):
        """Must use @polymarket/builder-relayer-client, not third-party."""
        path = os.path.join(SCRIPTS_DIR, "claim_winnings.js")
        with open(path) as f:
            content = f.read()
        assert "@polymarket/builder-relayer-client" in content
        assert "@polymarket/builder-signing-sdk" in content

    def test_script_handles_neg_risk(self):
        """Must use different contract for negRisk markets."""
        path = os.path.join(SCRIPTS_DIR, "claim_winnings.js")
        with open(path) as f:
            content = f.read()
        assert "NEG_RISK_CTF_EXCHANGE" in content
        assert "negRisk" in content

    def test_script_no_hardcoded_keys(self):
        """Builder API keys must come from env vars, not hardcoded."""
        path = os.path.join(SCRIPTS_DIR, "claim_winnings.js")
        with open(path) as f:
            content = f.read()
        assert "process.env.POLY_BUILDER_API_KEY" in content
        assert "process.env.POLY_BUILDER_SECRET" in content
        assert "process.env.POLY_BUILDER_PASSPHRASE" in content

    def test_script_checks_redeemable_endpoint(self):
        """Must query data-api for redeemable positions."""
        path = os.path.join(SCRIPTS_DIR, "claim_winnings.js")
        with open(path) as f:
            content = f.read()
        assert "redeemable=true" in content
        assert "data-api.polymarket.com/positions" in content

    def test_script_has_test_mode(self):
        """Must support --test flag for dry run."""
        path = os.path.join(SCRIPTS_DIR, "claim_winnings.js")
        with open(path) as f:
            content = f.read()
        assert "--test" in content
        assert "DRY RUN" in content

    def test_script_deduplicates_by_condition_id(self):
        """Must dedup claims by conditionId."""
        path = os.path.join(SCRIPTS_DIR, "claim_winnings.js")
        with open(path) as f:
            content = f.read()
        assert "conditionId" in content
        assert "seen" in content or "byCondition" in content

    def test_script_uses_correct_contracts(self):
        """Must reference correct Polymarket contract addresses."""
        path = os.path.join(SCRIPTS_DIR, "claim_winnings.js")
        with open(path) as f:
            content = f.read()
        # USDC on Polygon
        assert "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174" in content
        # CTF (regular)
        assert "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045" in content
        # NegRisk CTF Exchange
        assert "0xC5d563A36AE78145C45a50134d48A1215220f80a" in content

    def test_script_uses_relayer(self):
        """Must use the official Polymarket relayer."""
        path = os.path.join(SCRIPTS_DIR, "claim_winnings.js")
        with open(path) as f:
            content = f.read()
        assert "relayer-v2.polymarket.com" in content


class TestAutoClaimInStartSh:
    """Auto-claim must be configured in start.sh."""

    def test_start_sh_has_claim(self):
        path = os.path.join(SCRIPTS_DIR, "start.sh")
        with open(path) as f:
            content = f.read()
        assert "claim_winnings.js" in content

    def test_start_sh_runs_on_loop(self):
        """Must run on a timer, not just once."""
        path = os.path.join(SCRIPTS_DIR, "start.sh")
        with open(path) as f:
            content = f.read()
        assert "while true" in content
        assert "sleep" in content


class TestAutoClaimNodeDeps:
    """Node.js dependencies must be declared."""

    def test_package_json_exists(self):
        path = os.path.join(ROOT_DIR, "package.json")
        assert os.path.exists(path)

    def test_package_json_has_deps(self):
        path = os.path.join(ROOT_DIR, "package.json")
        with open(path) as f:
            pkg = json.load(f)
        deps = pkg.get("dependencies", {})
        assert "@polymarket/builder-relayer-client" in deps
        assert "viem" in deps
        assert "axios" in deps

    def test_dockerfile_has_nodejs(self):
        path = os.path.join(ROOT_DIR, "Dockerfile")
        with open(path) as f:
            content = f.read()
        assert "nodejs" in content
        assert "npm" in content or "npm ci" in content or "npm install" in content
