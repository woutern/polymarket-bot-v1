"""DynamoDB storage — persistent trades and windows for dashboard access."""

from __future__ import annotations

import boto3
from decimal import Decimal


def _to_decimal(obj):
    """Convert floats to Decimal for DynamoDB."""
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    return obj


class DynamoStore:
    def __init__(self, region: str = "eu-west-1"):
        self.db = boto3.resource("dynamodb", region_name=region)
        self.trades = self.db.Table("polymarket-bot-trades")
        self.windows = self.db.Table("polymarket-bot-windows")

    def put_trade(self, trade: dict):
        self.trades.put_item(Item=_to_decimal(trade))

    def put_window(self, window: dict):
        self.windows.put_item(Item=_to_decimal(window))

    def get_recent_trades(self, limit: int = 100) -> list[dict]:
        resp = self.trades.scan(Limit=limit)
        return resp.get("Items", [])

    def get_trades_for_window(self, window_slug: str) -> list[dict]:
        from boto3.dynamodb.conditions import Key
        resp = self.trades.query(
            IndexName="window-index",
            KeyConditionExpression=Key("window_slug").eq(window_slug),
        )
        return resp.get("Items", [])
