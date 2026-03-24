"""MarketMaker storage layer — tick_log, window_log, position_store.

Three DynamoDB tables (all eu-west-1):
  polymarket-mm-ticks       — per-tick records (high volume, TTL 7 days)
  polymarket-mm-windows     — per-window summaries (permanent)
  polymarket-mm-positions   — open position snapshots (overwritten each tick)

All operations fail silently — DynamoDB is best-effort.
The engine runs in-memory; storage is for dashboard and post-analysis.

Usage:
    store = MMStore()
    store.put_tick(window_id, tick_record)
    store.put_window(window_result)
    store.put_position(window_id, position_snapshot)

Local / paper mode:
    store = InMemoryMMStore()   # no AWS needed
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict
from decimal import Decimal
from typing import Any

from polybot.core.engine import WindowResult
from polybot.core.engine import TickRecord

logger = logging.getLogger(__name__)

_TICK_TTL_SECONDS = 7 * 24 * 3600   # 7 days
_REGION = "eu-west-1"

_TABLE_TICKS = "polymarket-mm-ticks"
_TABLE_WINDOWS = "polymarket-mm-windows"
_TABLE_POSITIONS = "polymarket-mm-positions"


# ─── Helpers ────────────────────────────────────────────────────────────────

def _to_decimal(obj: Any) -> Any:
    """Recursively convert floats → Decimal for DynamoDB."""
    if isinstance(obj, float):
        return Decimal(str(round(obj, 6)))
    if isinstance(obj, dict):
        return {k: _to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_to_decimal(v) for v in obj]
    return obj


def _action_summary(action: Any) -> dict:
    """Compact representation of a StrategyAction for storage."""
    return {
        "buy_up": action.buy_up_shares,
        "buy_up_price": round(action.buy_up_price, 4),
        "buy_dn": action.buy_down_shares,
        "buy_dn_price": round(action.buy_down_price, 4),
        "sell_up": action.sell_up_shares,
        "sell_up_price": round(action.sell_up_price, 4),
        "sell_dn": action.sell_down_shares,
        "sell_dn_price": round(action.sell_down_price, 4),
        "reason": action.reason,
    }


# ─── Live store (DynamoDB) ───────────────────────────────────────────────────

class MMStore:
    """DynamoDB-backed store for MarketMaker engine data.

    Falls back to no-op if DynamoDB is unavailable (local dev / tests).
    """

    def __init__(self, region: str = _REGION):
        self._region = region
        self._available = False
        self._ticks = None
        self._windows = None
        self._positions = None
        self._init()

    def _init(self) -> None:
        try:
            import boto3
            import os
            if not os.getenv("AWS_EXECUTION_ENV"):
                try:
                    session = boto3.Session(profile_name="playground", region_name=self._region)
                    session.client("sts").get_caller_identity()
                    db = session.resource("dynamodb")
                except Exception:
                    db = boto3.resource("dynamodb", region_name=self._region)
            else:
                db = boto3.resource("dynamodb", region_name=self._region)

            self._ticks = db.Table(_TABLE_TICKS)
            self._windows = db.Table(_TABLE_WINDOWS)
            self._positions = db.Table(_TABLE_POSITIONS)
            self._available = True
        except Exception as e:
            logger.debug("mm_store_init_failed", extra={"error": str(e)})

    # ------------------------------------------------------------------
    # Tick log
    # ------------------------------------------------------------------

    def put_tick(self, window_id: str, record: TickRecord) -> None:
        """Write one tick record. TTL = 7 days."""
        if not self._available:
            return
        try:
            item = {
                "window_id": window_id,
                "sort_key": f"{record.seconds:05d}",
                "seconds": record.seconds,
                "phase": record.phase,
                "action": _action_summary(record.action),
                "position": record.position_snapshot,
                "fills": record.fills,
                "ttl": int(time.time()) + _TICK_TTL_SECONDS,
            }
            self._ticks.put_item(Item=_to_decimal(item))
        except Exception as e:
            logger.debug("mm_store_put_tick_failed", extra={"error": str(e)})

    def get_ticks(self, window_id: str) -> list[dict]:
        """Return all tick records for a window, sorted by seconds."""
        if not self._available:
            return []
        try:
            from boto3.dynamodb.conditions import Key
            resp = self._ticks.query(
                KeyConditionExpression=Key("window_id").eq(window_id),
                ScanIndexForward=True,
            )
            return resp.get("Items", [])
        except Exception as e:
            logger.debug("mm_store_get_ticks_failed", extra={"error": str(e)})
            return []

    # ------------------------------------------------------------------
    # Window log
    # ------------------------------------------------------------------

    def put_window(self, window_id: str, result: WindowResult) -> None:
        """Write a completed window summary."""
        if not self._available:
            return
        try:
            item = {
                "window_id": window_id,
                "pair": result.pair,
                "profile": result.profile_name,
                "total_ticks": result.total_ticks,
                "up_shares": result.up_shares,
                "down_shares": result.down_shares,
                "up_avg": round(result.up_avg, 4),
                "down_avg": round(result.down_avg, 4),
                "combined_avg": round(result.combined_avg, 4),
                "payout_floor": result.payout_floor,
                "net_cost": round(result.net_cost, 4),
                "is_gp": result.is_guaranteed_profit,
                "pnl_if_up": round(result.pnl_if_up, 4),
                "pnl_if_down": round(result.pnl_if_down, 4),
                "sell_reasons": result.sell_reasons,
                "fill_stats": result.fill_stats,
                "ts": int(time.time()),
            }
            self._windows.put_item(Item=_to_decimal(item))
        except Exception as e:
            logger.debug("mm_store_put_window_failed", extra={"error": str(e)})

    def get_window(self, window_id: str) -> dict | None:
        """Fetch a window summary by ID."""
        if not self._available:
            return None
        try:
            resp = self._windows.get_item(Key={"window_id": window_id})
            return resp.get("Item")
        except Exception as e:
            logger.debug("mm_store_get_window_failed", extra={"error": str(e)})
            return None

    def get_recent_windows(self, limit: int = 50) -> list[dict]:
        """Scan recent windows (sorted by ts descending). Slow — for dashboard only."""
        if not self._available:
            return []
        try:
            resp = self._windows.scan(Limit=limit)
            items = resp.get("Items", [])
            return sorted(items, key=lambda x: int(x.get("ts", 0)), reverse=True)
        except Exception as e:
            logger.debug("mm_store_scan_windows_failed", extra={"error": str(e)})
            return []

    # ------------------------------------------------------------------
    # Position store (live snapshot, one item per window)
    # ------------------------------------------------------------------

    def put_position(self, window_id: str, snapshot: dict) -> None:
        """Overwrite the live position snapshot for a window."""
        if not self._available:
            return
        try:
            item = {
                "window_id": window_id,
                "ts": int(time.time()),
                **snapshot,
            }
            self._positions.put_item(Item=_to_decimal(item))
        except Exception as e:
            logger.debug("mm_store_put_position_failed", extra={"error": str(e)})

    def get_position(self, window_id: str) -> dict | None:
        """Fetch the latest position snapshot for a window."""
        if not self._available:
            return None
        try:
            resp = self._positions.get_item(Key={"window_id": window_id})
            return resp.get("Item")
        except Exception as e:
            logger.debug("mm_store_get_position_failed", extra={"error": str(e)})
            return None


# ─── In-memory store (tests / paper mode) ───────────────────────────────────

class InMemoryMMStore:
    """Drop-in replacement for MMStore. No AWS needed.

    All data lives in plain Python dicts — cleared when the object is
    garbage-collected. Use in tests and paper-mode runs.
    """

    def __init__(self):
        self._ticks: dict[str, list[dict]] = {}       # window_id → [tick, ...]
        self._windows: dict[str, dict] = {}            # window_id → summary
        self._positions: dict[str, dict] = {}          # window_id → snapshot

    # Tick log

    def put_tick(self, window_id: str, record: TickRecord) -> None:
        self._ticks.setdefault(window_id, []).append({
            "seconds": record.seconds,
            "phase": record.phase,
            "action": _action_summary(record.action),
            "position": record.position_snapshot,
            "fills": record.fills,
        })

    def get_ticks(self, window_id: str) -> list[dict]:
        return list(self._ticks.get(window_id, []))

    # Window log

    def put_window(self, window_id: str, result: WindowResult) -> None:
        self._windows[window_id] = {
            "window_id": window_id,
            "pair": result.pair,
            "profile": result.profile_name,
            "total_ticks": result.total_ticks,
            "up_shares": result.up_shares,
            "down_shares": result.down_shares,
            "up_avg": result.up_avg,
            "down_avg": result.down_avg,
            "combined_avg": result.combined_avg,
            "payout_floor": result.payout_floor,
            "net_cost": result.net_cost,
            "is_gp": result.is_guaranteed_profit,
            "pnl_if_up": result.pnl_if_up,
            "pnl_if_down": result.pnl_if_down,
            "sell_reasons": dict(result.sell_reasons),
            "fill_stats": dict(result.fill_stats),
        }

    def get_window(self, window_id: str) -> dict | None:
        return self._windows.get(window_id)

    def get_recent_windows(self, limit: int = 50) -> list[dict]:
        windows = list(self._windows.values())
        return windows[-limit:]

    # Position store

    def put_position(self, window_id: str, snapshot: dict) -> None:
        self._positions[window_id] = {"window_id": window_id, **snapshot}

    def get_position(self, window_id: str) -> dict | None:
        return self._positions.get(window_id)

    # Test helpers

    def all_window_ids(self) -> list[str]:
        return list(self._windows.keys())

    def tick_count(self, window_id: str) -> int:
        return len(self._ticks.get(window_id, []))
