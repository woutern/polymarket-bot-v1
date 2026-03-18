"""Risk manager: daily P&L tracking, circuit breakers, streak detection."""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date

import structlog

logger = structlog.get_logger()


@dataclass
class RiskManager:
    """Tracks daily P&L and enforces risk limits with circuit breakers."""

    bankroll: float = 1000.0
    daily_loss_cap_pct: float = 0.10  # 10% daily loss cap
    max_position_pct: float = 0.01
    min_trade_usd: float = 1.0
    max_trade_usd: float = 10.0

    # Internal state
    daily_pnl: float = 0.0
    daily_trades: int = 0
    _day_start_bankroll: float = 0.0
    _current_date: str = ""
    _circuit_breaker_active: bool = False
    _streak_pause_until: float = 0.0  # Unix timestamp when streak pause ends
    _slippage_total: float = 0.0
    _slippage_count: int = 0
    # Track recent trade outcomes for streak detection
    _recent_outcomes: deque = field(default_factory=lambda: deque(maxlen=20))
    _consecutive_losses: int = 0
    _reduced_sizing: bool = False  # True when 5/20 losses triggers $1 flat

    def __post_init__(self):
        self._day_start_bankroll = self.bankroll

    def _check_new_day(self):
        today = date.today().isoformat()
        if today != self._current_date:
            self._current_date = today
            self._day_start_bankroll = self.bankroll
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self._circuit_breaker_active = False
            self._streak_pause_until = 0.0
            self._consecutive_losses = 0
            self._reduced_sizing = False
            self._slippage_total = 0.0
            self._slippage_count = 0
            logger.info("risk_new_day", date=today, bankroll=self.bankroll)

    @property
    def circuit_breaker_active(self) -> bool:
        self._check_new_day()
        return self._circuit_breaker_active

    def can_trade(self) -> bool:
        """Check if we're allowed to trade."""
        self._check_new_day()
        if self._circuit_breaker_active:
            return False
        # Streak pause: 3 consecutive losses → pause 15 min
        if time.time() < self._streak_pause_until:
            return False
        return True

    def get_bet_size(self) -> float:
        """Compute current bet size based on bankroll and risk state.

        Returns 1% of bankroll, bounded by min/max, reduced to $1 if losing streak.
        """
        if self._reduced_sizing:
            return self.min_trade_usd  # $1 flat during losing streak

        base = round(self.bankroll * self.max_position_pct, 2)
        return max(self.min_trade_usd, min(self.max_trade_usd, base))

    def max_position_size(self) -> float:
        return self.bankroll * self.max_position_pct

    def record_trade(self, pnl: float, slippage: float = 0.0):
        """Record a completed trade and check all circuit breakers."""
        self._check_new_day()
        self.daily_pnl += pnl
        self.daily_trades += 1
        self.bankroll += pnl

        if slippage != 0:
            self._slippage_total += abs(slippage)
            self._slippage_count += 1

        # Track outcome
        won = pnl > 0
        self._recent_outcomes.append(won)

        # Circuit breaker 1: 3 consecutive losses → pause 15 min
        if won:
            self._consecutive_losses = 0
        else:
            self._consecutive_losses += 1
            if self._consecutive_losses >= 3:
                self._streak_pause_until = time.time() + 900  # 15 minutes
                logger.warning(
                    "streak_pause_triggered",
                    consecutive_losses=self._consecutive_losses,
                    pause_minutes=15,
                    bankroll=round(self.bankroll, 2),
                )

        # Circuit breaker 2: 5 losses in any 20 trades → drop to $1 flat
        if len(self._recent_outcomes) >= 10:
            recent_losses = sum(1 for o in self._recent_outcomes if not o)
            if recent_losses >= 5 and not self._reduced_sizing:
                self._reduced_sizing = True
                logger.warning(
                    "reduced_sizing_triggered",
                    losses_in_20=recent_losses,
                    total_recent=len(self._recent_outcomes),
                    bankroll=round(self.bankroll, 2),
                )
            elif recent_losses < 3 and self._reduced_sizing:
                self._reduced_sizing = False
                logger.info("reduced_sizing_lifted", losses_in_20=recent_losses)

        # Circuit breaker 3: daily loss > 10% of day-start balance
        if self._day_start_bankroll > 0:
            loss_limit = self._day_start_bankroll * self.daily_loss_cap_pct
            if self.daily_pnl < -loss_limit:
                self._circuit_breaker_active = True
                logger.warning(
                    "daily_circuit_breaker",
                    daily_pnl=round(self.daily_pnl, 2),
                    loss_limit=round(loss_limit, 2),
                    day_start=round(self._day_start_bankroll, 2),
                    bankroll=round(self.bankroll, 2),
                )

        logger.info(
            "trade_recorded",
            pnl=round(pnl, 4),
            daily_pnl=round(self.daily_pnl, 4),
            bankroll=round(self.bankroll, 2),
            daily_trades=self.daily_trades,
            consecutive_losses=self._consecutive_losses,
            reduced_sizing=self._reduced_sizing,
        )

    @property
    def avg_slippage(self) -> float:
        if self._slippage_count == 0:
            return 0.0
        return self._slippage_total / self._slippage_count
