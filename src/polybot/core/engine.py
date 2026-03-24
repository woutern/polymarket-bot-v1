"""MarketMaker engine — tick loop and window state machine.

Ties together:
- MarketMakerStrategy (what to do each tick)
- MMPaperClient (execute orders in paper mode)
- BotControls / InMemoryControls (pause / kill switch)
- Position (inventory tracking)

Window lifecycle:
    OPEN → ACCUMULATE → BUY_ONLY → COMMIT → RESOLVED

The engine drives the strategy tick-by-tick. In production the engine
runs inside the ECS task alongside the WebSocket price feed. In tests
and paper mode, MarketState is injected directly via run_tick().

Typical paper-mode usage:
    engine = Engine(pair="BTC_5M", mode="paper")
    for state in my_tick_feed:
        engine.run_tick(state)
    result = engine.window_result()
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Callable

from polybot.core.controls import InMemoryControls
from polybot.core.position import Position
from polybot.execution.mm_paper_client import MMPaperClient
from polybot.strategy.base import MarketState, StrategyAction
from polybot.strategy.market_maker import MarketMakerStrategy
from polybot.strategy.profile import StrategyProfile
from polybot.strategy.profiles import get_profile

logger = logging.getLogger(__name__)


class WindowPhase(Enum):
    OPEN = auto()       # T+0 to open_end
    ACCUMULATE = auto() # open_end to buy_only_start
    BUY_ONLY = auto()   # buy_only_start to commit_start
    COMMIT = auto()     # commit_start to resolution
    RESOLVED = auto()   # market settled


@dataclass
class TickRecord:
    """Log entry for one engine tick."""
    seconds: int
    phase: str
    action: StrategyAction
    position_snapshot: dict
    fills: list[dict]


@dataclass
class WindowResult:
    """Summary of a completed window."""
    pair: str
    profile_name: str
    total_ticks: int
    up_shares: int
    down_shares: int
    up_avg: float
    down_avg: float
    combined_avg: float
    payout_floor: int
    net_cost: float
    is_guaranteed_profit: bool
    sell_reasons: dict[str, int]    # reason → count
    fill_stats: dict
    tick_log: list[TickRecord] = field(default_factory=list)

    @property
    def pnl_if_up(self) -> float:
        """P&L if UP wins ($1/share)."""
        return self.up_shares * 1.0 - self.net_cost

    @property
    def pnl_if_down(self) -> float:
        """P&L if DOWN wins ($1/share)."""
        return self.down_shares * 1.0 - self.net_cost


class Engine:
    """Single-window tick loop for one trading pair.

    In production: one Engine per active window, driven by WebSocket feed.
    In paper/test: call run_tick() directly with synthetic MarketState.

    Args:
        pair:     Profile key, e.g. "BTC_5M"
        mode:     "paper" (in-memory orders) or "live" (real CLOB)
        controls: BotControls or InMemoryControls instance
        profile:  Override the default profile for this pair
        on_action: Optional callback invoked after each tick with the action
    """

    def __init__(
        self,
        pair: str = "BTC_5M",
        mode: str = "paper",
        controls=None,
        profile: StrategyProfile | None = None,
        on_action: Callable[[int, StrategyAction], None] | None = None,
    ):
        self.pair = pair
        self.mode = mode
        self.profile = profile or get_profile(pair)
        self.controls = controls if controls is not None else InMemoryControls()
        self.on_action = on_action

        self.position = Position()
        self.client = MMPaperClient(position=self.position)
        self.strategy = MarketMakerStrategy(profile=self.profile)

        self._phase = WindowPhase.OPEN
        self._tick_log: list[TickRecord] = []
        self._sell_reasons: dict[str, int] = {}
        self._tick_count = 0
        self._started_at: float = time.monotonic()

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run_tick(self, state: MarketState) -> StrategyAction:
        """Process one tick. Returns the action the strategy decided."""
        self._tick_count += 1

        # Kill switch — caller must handle this
        if self.controls.kill_switch:
            logger.warning("engine: kill switch active, no action")
            return StrategyAction(reason="KILL_SWITCH")

        # Update phase
        self._phase = self._compute_phase(state.seconds)

        if self._phase == WindowPhase.RESOLVED:
            return StrategyAction(reason="RESOLVED")

        # Let paper client fill pending orders against this tick's spread
        fills = self.client.tick(
            yes_bid=state.yes_bid,
            no_bid=state.no_bid,
            yes_ask=state.yes_ask,
            no_ask=state.no_ask,
            seconds=state.seconds,
        )

        # Ask strategy what to do
        budget = self.profile.budget - self.position.net_cost
        action = self.strategy.on_tick(state, self.position, budget)

        # Execute the action via the order client
        self._execute_action(action, state)

        # Track sell reasons
        if action.has_action() and action.reason:
            if action.sell_up_shares > 0 or action.sell_down_shares > 0:
                self._sell_reasons[action.reason] = (
                    self._sell_reasons.get(action.reason, 0) + 1
                )

        # Log
        record = TickRecord(
            seconds=state.seconds,
            phase=self._phase.name,
            action=action,
            position_snapshot=self._position_snapshot(),
            fills=[
                {"token": f.token, "side": f.side, "shares": f.filled_shares, "price": f.filled_price}
                for f in fills
            ],
        )
        self._tick_log.append(record)

        if self.on_action:
            self.on_action(state.seconds, action)

        return action

    def commit(self) -> None:
        """Cancel all open orders at window end (COMMIT phase)."""
        cancelled = self.client.cancel_all()
        if cancelled:
            logger.info("engine: commit — cancelled %d open orders", cancelled)

    def window_result(self) -> WindowResult:
        """Return summary of this window. Call after commit() or RESOLVED."""
        pos = self.position
        ca = pos.combined_avg
        return WindowResult(
            pair=self.pair,
            profile_name=self.profile.name,
            total_ticks=self._tick_count,
            up_shares=pos.up_shares,
            down_shares=pos.down_shares,
            up_avg=pos.up_avg,
            down_avg=pos.down_avg,
            combined_avg=ca,
            payout_floor=pos.payout_floor,
            net_cost=pos.net_cost,
            is_guaranteed_profit=pos.payout_floor > 0 and ca < 1.0,
            sell_reasons=dict(self._sell_reasons),
            fill_stats=self.client.stats(),
            tick_log=list(self._tick_log),
        )

    # ------------------------------------------------------------------
    # Phase computation
    # ------------------------------------------------------------------

    def _compute_phase(self, seconds: int) -> WindowPhase:
        p = self.profile
        commit_s = p.commit_seconds if hasattr(p, "commit_seconds") else 250
        buy_only_s = commit_s - 70  # 70s buy-only window before commit

        if seconds >= commit_s:
            return WindowPhase.COMMIT
        if seconds >= buy_only_s:
            return WindowPhase.BUY_ONLY
        open_end = int(p.budget * p.open_budget_pct / 0.5 * 5)  # rough heuristic
        if seconds < 15:
            return WindowPhase.OPEN
        return WindowPhase.ACCUMULATE

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    def _execute_action(self, action: StrategyAction, state: MarketState) -> None:
        """Translate StrategyAction into paper client orders."""
        if action.buy_up_shares > 0 and action.buy_up_price > 0:
            self.client.post_buy("YES", action.buy_up_shares, action.buy_up_price)

        if action.buy_down_shares > 0 and action.buy_down_price > 0:
            self.client.post_buy("NO", action.buy_down_shares, action.buy_down_price)

        if action.sell_up_shares > 0 and action.sell_up_price > 0:
            self.client.post_sell("YES", action.sell_up_shares, action.sell_up_price)

        if action.sell_down_shares > 0 and action.sell_down_price > 0:
            self.client.post_sell("NO", action.sell_down_shares, action.sell_down_price)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _position_snapshot(self) -> dict:
        p = self.position
        return {
            "up_shares": p.up_shares,
            "down_shares": p.down_shares,
            "up_avg": round(p.up_avg, 4),
            "down_avg": round(p.down_avg, 4),
            "combined_avg": round(p.combined_avg, 4),
            "payout_floor": p.payout_floor,
            "net_cost": round(p.net_cost, 4),
        }

    @property
    def phase(self) -> WindowPhase:
        return self._phase
