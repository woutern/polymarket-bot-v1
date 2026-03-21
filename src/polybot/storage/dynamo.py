"""DynamoDB storage — persistent trades and windows for dashboard access.

All operations fail silently: DynamoDB is a best-effort mirror.
SQLite is the source of truth.
"""

from __future__ import annotations

import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


def _to_decimal(obj):
    """Convert floats to Decimal for DynamoDB."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    return obj


class DynamoStore:
    def __init__(self, region: str = "eu-west-1"):
        self._region = region
        self._trades = None
        self._windows = None
        self._available = False
        try:
            import boto3
            import os
            # Use playground profile locally, fall back to instance/task role on AWS
            if not os.getenv("AWS_EXECUTION_ENV"):
                try:
                    session = boto3.Session(profile_name="playground", region_name=region)
                    session.client("sts").get_caller_identity()
                    db = session.resource("dynamodb")
                except Exception:
                    db = boto3.resource("dynamodb", region_name=region)
            else:
                db = boto3.resource("dynamodb", region_name=region)
            self._trades = db.Table("polymarket-bot-trades")
            self._windows = db.Table("polymarket-bot-windows")
            self._signals = db.Table("polymarket-bot-signals")
            self._training = db.Table("polymarket-bot-training-data")
            self._available = True
        except Exception as e:
            logger.debug("dynamo_init_failed", extra={"error": str(e)})

    def put_trade(self, trade: dict):
        if not self._available:
            return
        try:
            self._trades.put_item(Item=_to_decimal(trade))
        except Exception as e:
            logger.debug("dynamo_put_trade_failed", extra={"error": str(e)})

    def put_window(self, window: dict):
        if not self._available:
            return
        try:
            self._windows.put_item(Item=_to_decimal(window))
        except Exception as e:
            logger.debug("dynamo_put_window_failed", extra={"error": str(e)})

    def get_recent_trades(self, limit: int = 100) -> list[dict]:
        if not self._available:
            return []
        try:
            resp = self._trades.scan(Limit=limit)
            return resp.get("Items", [])
        except Exception as e:
            logger.debug("dynamo_get_trades_failed", extra={"error": str(e)})
            return []

    def put_signal(self, signal: dict):
        if not self._available:
            return
        try:
            self._signals.put_item(Item=_to_decimal(signal))
        except Exception as e:
            logger.debug("dynamo_put_signal_failed", extra={"error": str(e)})

    def get_recent_signals(self, limit: int = 100) -> list[dict]:
        if not self._available:
            return []
        try:
            resp = self._signals.scan(Limit=limit)
            return resp.get("Items", [])
        except Exception as e:
            logger.debug("dynamo_get_signals_failed", extra={"error": str(e)})
            return []

    def put_training_data(self, record: dict):
        if not self._available:
            return
        try:
            self._training.put_item(Item=_to_decimal(record))
        except Exception as e:
            logger.debug("dynamo_put_training_failed", extra={"error": str(e)})

    def update_trade_resolved(self, trade_id: str, pnl: float, polymarket_winner: str, correct_prediction: bool, outcome_source: str):
        if not self._available:
            return
        try:
            from decimal import Decimal
            self._trades.update_item(
                Key={"id": trade_id},
                UpdateExpression="SET resolved = :r, pnl = :p, polymarket_winner = :w, correct_prediction = :c, outcome_source = :s",
                ExpressionAttributeValues={
                    ":r": 1,
                    ":p": Decimal(str(round(pnl, 6))),
                    ":w": polymarket_winner,
                    ":c": int(correct_prediction),
                    ":s": outcome_source,
                },
            )
        except Exception as e:
            logger.debug("dynamo_update_trade_failed", extra={"error": str(e)})

    def claim_slug(self, window_slug: str) -> bool:
        """Atomically claim a window slug to prevent duplicate trades across containers.

        Uses DynamoDB conditional put on the windows table (not trades) to avoid
        polluting trade queries with claim records.
        Returns True if claimed, False if already taken.
        """
        if not self._available or not self._windows:
            return True  # optimistic if DynamoDB unavailable
        try:
            import time
            self._windows.put_item(
                Item={
                    "slug": f"claim_{window_slug}",
                    "open_ts": int(time.time()),
                    "claimed_by": "dedup",
                },
                ConditionExpression="attribute_not_exists(slug)",
            )
            return True
        except self._windows.meta.client.exceptions.ConditionalCheckFailedException:
            logger.info("dedup_claim_exists", slug=window_slug)
            return False
        except Exception as e:
            logger.warning("dedup_claim_error", slug=window_slug, error=str(e)[:60])
            return True  # optimistic on error

    def get_trades_for_window(self, window_slug: str) -> list[dict]:
        if not self._available:
            return []
        try:
            from boto3.dynamodb.conditions import Key
            resp = self._trades.query(
                IndexName="window-index",
                KeyConditionExpression=Key("window_slug").eq(window_slug),
            )
            return resp.get("Items", [])
        except Exception as e:
            logger.debug("dynamo_query_failed", extra={"error": str(e)})
            return []
