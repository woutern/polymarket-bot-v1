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
            db = boto3.resource("dynamodb", region_name=region)
            self._trades = db.Table("polymarket-bot-trades")
            self._windows = db.Table("polymarket-bot-windows")
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
