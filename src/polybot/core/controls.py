"""BotControls — runtime control flags without redeployment.

Backed by DynamoDB table `polymarket-bot-controls` (eu-west-1).
Falls back to in-memory defaults if DynamoDB is unavailable (paper mode / tests).

Usage:
    controls = BotControls()
    if controls.kill_switch:
        sys.exit("kill switch active")
    if controls.pause_new_windows:
        return  # skip opening new windows, finish existing ones
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_TABLE_NAME = "polymarket-bot-controls"
_PARTITION_KEY = "bot"
_TTL_FIELD = "ttl"

# How often to re-fetch from DynamoDB (seconds)
_REFRESH_INTERVAL = 10


@dataclass
class ControlState:
    kill_switch: bool = False          # hard stop — exit process immediately
    pause_new_windows: bool = False    # finish open windows, skip new ones
    max_windows_override: int | None = None  # override max concurrent windows
    note: str = ""                     # operator note (why this flag was set)


class BotControls:
    """Runtime control flags, refreshed from DynamoDB every 10 seconds.

    Thread-safe for read access. Write via AWS Console or CLI:

        aws dynamodb put-item \\
            --table-name polymarket-bot-controls \\
            --item '{"bot":{"S":"bot"},"kill_switch":{"BOOL":true},"note":{"S":"manual stop"}}' \\
            --region eu-west-1
    """

    def __init__(self, *, table_name: str = _TABLE_NAME, region: str = "eu-west-1"):
        self._table_name = table_name
        self._region = region
        self._state = ControlState()
        self._last_fetch: float = 0.0
        self._dynamo: Any = None
        self._dynamo_available = True

        # Attempt immediate fetch so controls are live before first tick
        self._maybe_refresh(force=True)

    # ------------------------------------------------------------------
    # Public properties — the engine reads these
    # ------------------------------------------------------------------

    @property
    def kill_switch(self) -> bool:
        self._maybe_refresh()
        return self._state.kill_switch

    @property
    def pause_new_windows(self) -> bool:
        self._maybe_refresh()
        return self._state.pause_new_windows

    @property
    def max_windows_override(self) -> int | None:
        self._maybe_refresh()
        return self._state.max_windows_override

    @property
    def note(self) -> str:
        return self._state.note

    def snapshot(self) -> ControlState:
        """Return a copy of the current state (no network call)."""
        return ControlState(
            kill_switch=self._state.kill_switch,
            pause_new_windows=self._state.pause_new_windows,
            max_windows_override=self._state.max_windows_override,
            note=self._state.note,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _maybe_refresh(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last_fetch) < _REFRESH_INTERVAL:
            return
        self._last_fetch = now
        self._fetch()

    def _fetch(self) -> None:
        if not self._dynamo_available:
            return
        try:
            client = self._get_client()
            resp = client.get_item(
                TableName=self._table_name,
                Key={"bot": {"S": "bot"}},
            )
            item = resp.get("Item", {})
            self._state = _parse_item(item)
        except Exception as exc:
            if self._dynamo_available:
                logger.warning("controls: DynamoDB unavailable, using defaults: %s", str(exc)[:80])
            self._dynamo_available = False

    def _get_client(self) -> Any:
        if self._dynamo is None:
            import boto3
            self._dynamo = boto3.client("dynamodb", region_name=self._region)
        return self._dynamo


def _parse_item(item: dict) -> ControlState:
    """Convert a raw DynamoDB item dict to ControlState."""
    def _bool(key: str) -> bool:
        v = item.get(key, {})
        return v.get("BOOL", False)

    def _int_or_none(key: str) -> int | None:
        v = item.get(key, {})
        n = v.get("N")
        return int(n) if n is not None else None

    def _str(key: str) -> str:
        return item.get(key, {}).get("S", "")

    return ControlState(
        kill_switch=_bool("kill_switch"),
        pause_new_windows=_bool("pause_new_windows"),
        max_windows_override=_int_or_none("max_windows_override"),
        note=_str("note"),
    )


class InMemoryControls:
    """Drop-in replacement for tests and paper mode — no AWS needed.

    Pass an instance of this wherever BotControls is expected.
    Mutate flags directly: controls.kill_switch = True
    """

    def __init__(self):
        self.kill_switch: bool = False
        self.pause_new_windows: bool = False
        self.max_windows_override: int | None = None
        self.note: str = ""

    def snapshot(self) -> ControlState:
        return ControlState(
            kill_switch=self.kill_switch,
            pause_new_windows=self.pause_new_windows,
            max_windows_override=self.max_windows_override,
            note=self.note,
        )
