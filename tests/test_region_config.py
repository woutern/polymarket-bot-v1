"""Tests to ensure all AWS resources are consolidated in eu-west-1."""

from __future__ import annotations

import os
import inspect

SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")


def _read_py_files():
    """Yield (relative_path, content) for every .py under src/."""
    for root, _, files in os.walk(SRC_DIR):
        for f in files:
            if f.endswith(".py") and "__pycache__" not in root:
                path = os.path.join(root, f)
                rel = os.path.relpath(path, os.path.join(SRC_DIR, ".."))
                with open(path) as fh:
                    yield rel, fh.read()


class TestAllInEuWest1:
    """All AWS resources must be in eu-west-1."""

    def test_no_us_east_1_in_executable_code(self):
        """No hardcoded 'us-east-1' in executable Python code (comments OK)."""
        violations = []
        for path, content in _read_py_files():
            in_docstring = False
            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if '"""' in stripped or "'''" in stripped:
                    count = stripped.count('"""') + stripped.count("'''")
                    if count == 1:
                        in_docstring = not in_docstring
                    continue
                if in_docstring or stripped.startswith("#"):
                    continue
                if "us-east-1" in line and ("region" in line.lower() or "=" in line or "(" in line):
                    violations.append(f"{path}:{i}: {stripped}")
        assert not violations, f"us-east-1 references found:\n" + "\n".join(violations)

    def test_dynamo_default_region_euw1(self):
        """DynamoStore default region should be eu-west-1."""
        from polybot.storage.dynamo import DynamoStore
        source = inspect.getsource(DynamoStore.__init__)
        assert "eu-west-1" in source
        assert "us-east-1" not in source

    def test_s3_bucket_is_euw1(self):
        """S3 bucket should be the eu-west-1 variant."""
        from polybot.core.loop import S3_BUCKET
        assert "euw1" in S3_BUCKET

    def test_bedrock_region_euw1(self):
        """Bedrock signal must use eu-west-1."""
        import polybot.strategy.bedrock_signal as bs
        source = inspect.getsource(bs)
        # Must have eu-west-1 somewhere in the module
        assert "eu-west-1" in source

    def test_model_server_region_euw1(self):
        """ModelServer must default to eu-west-1 for SSM + S3."""
        from polybot.ml.server import ModelServer
        ms = ModelServer()
        assert ms._region == "eu-west-1"

    def test_model_server_ssm_uses_bot_region(self):
        """SSM client in load_models must use the same region as the bot."""
        source = inspect.getsource(__import__("polybot.ml.server", fromlist=["ModelServer"]).ModelServer.load_models)
        # SSM client must be created with self._region, not a hardcoded region
        assert "self._region" in source
        assert "us-east-1" not in source

    def test_ssm_paths_use_polymarket_prefix(self):
        """SSM parameter paths must use /polymarket/models/ prefix."""
        source = inspect.getsource(__import__("polybot.ml.server", fromlist=["ModelServer"]).ModelServer.load_models)
        assert "/polymarket/models/" in source

    def test_trainer_no_us_east_1_client(self):
        """Trainer must not create us-east-1 boto3 clients in executable code."""
        from polybot.ml import trainer
        source = inspect.getsource(trainer.train_all)
        # S3 is global — no need for us-east-1 client
        assert "us-east-1" not in source
