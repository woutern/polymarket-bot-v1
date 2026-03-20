"""Tests to ensure us-east-1 migration is complete — no eu-west-1 references remain in hot path."""

from __future__ import annotations

import ast
import os
import re

import pytest

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src", "polybot")


def _read_py_files():
    """Yield (relative_path, content) for all .py files in src/polybot/."""
    for root, _, files in os.walk(SRC_DIR):
        for f in files:
            if f.endswith(".py"):
                path = os.path.join(root, f)
                rel = os.path.relpath(path, os.path.join(SRC_DIR, ".."))
                with open(path) as fh:
                    yield rel, fh.read()


class TestNoEuWest1InSource:
    """After migration, no source code should reference eu-west-1."""

    def test_no_eu_west_1_in_executable_code(self):
        """No hardcoded 'eu-west-1' in executable Python code (comments/docstrings OK).

        Allowlist: smoke_test.py _ECS_CLUSTER_REGION constant is legitimate —
        the ECS cluster MUST be in eu-west-1 (CLOB geoblocks us-east-1).
        """
        # Files + patterns that legitimately need eu-west-1
        allowlist = {
            "polybot/core/smoke_test.py": "_ECS_CLUSTER_REGION",
        }
        violations = []
        for path, content in _read_py_files():
            in_docstring = False
            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                # Track triple-quote docstrings
                if '"""' in stripped or "'''" in stripped:
                    count = stripped.count('"""') + stripped.count("'''")
                    if count == 1:
                        in_docstring = not in_docstring
                    continue
                if in_docstring or stripped.startswith("#"):
                    continue
                # Check for region in executable code
                if 'eu-west-1' in line and ('region' in line.lower() or '=' in line or '(' in line):
                    # Check allowlist
                    allowed = False
                    for apath, apattern in allowlist.items():
                        if apath in path and apattern in stripped:
                            allowed = True
                            break
                    if not allowed:
                        violations.append(f"{path}:{i}: {stripped}")
        assert not violations, f"eu-west-1 references found:\n" + "\n".join(violations)

    def test_no_eu_prefix_bedrock_model(self):
        """Bedrock model ID should NOT have 'eu.' prefix (cross-region)."""
        violations = []
        for path, content in _read_py_files():
            for i, line in enumerate(content.splitlines(), 1):
                if "eu.anthropic" in line and not line.strip().startswith("#"):
                    violations.append(f"{path}:{i}: {line.strip()}")
        assert not violations, f"Cross-region Bedrock model IDs found:\n" + "\n".join(violations)


class TestBedrockConfig:
    def test_model_id_is_native_us_east_1(self):
        """bedrock_signal._MODEL_ID should be native (no eu. prefix)."""
        from polybot.strategy.bedrock_signal import _MODEL_ID
        assert not _MODEL_ID.startswith("eu."), f"Model ID still has eu. prefix: {_MODEL_ID}"
        assert "anthropic.claude" in _MODEL_ID


class TestDynamoConfig:
    def test_default_region_is_us_east_1(self):
        """DynamoStore default region should be us-east-1."""
        import inspect
        from polybot.storage.dynamo import DynamoStore
        sig = inspect.signature(DynamoStore.__init__)
        default_region = sig.parameters["region"].default
        assert default_region == "us-east-1", f"DynamoStore default region is {default_region}"


class TestS3Config:
    def test_s3_bucket_is_us_east_1(self):
        """S3 bucket should be the us-east-1 variant."""
        from polybot.core.loop import S3_BUCKET
        assert "use1" in S3_BUCKET, f"S3 bucket doesn't look like us-east-1: {S3_BUCKET}"


class TestSwitchScript:
    def test_switch_sh_uses_us_east_1(self):
        """switch.sh should reference us-east-1, not eu-west-1."""
        script_path = os.path.join(os.path.dirname(__file__), "..", "scripts", "switch.sh")
        with open(script_path) as f:
            content = f.read()
        assert 'REGION="us-east-1"' in content, "switch.sh REGION is not us-east-1"
        # eu-west-1 is OK for the SSM command (dashboard EC2 stays in eu-west-1)
        # but REGION variable must be us-east-1
        region_line = [l for l in content.splitlines() if l.startswith("REGION=")]
        assert region_line, "No REGION= line found in switch.sh"
        assert region_line[0] == 'REGION="us-east-1"', f"switch.sh REGION is wrong: {region_line[0]}"
