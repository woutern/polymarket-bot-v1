"""Risk manager: daily P&L tracking, circuit breaker, slippage."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

import structlog

logger = structlog.get_logger()


@dataclass
class RiskManager:
    """Tracks daily P&L and enforces risk limits."""

    bankroll: float = 1000.0
    daily_loss_cap_pct: float = 0.05
    max_position_pct: float = 0.01
    min_trade_usd: float = 1.0
    max_trade_usd: float = 10.0

    # Internal state
    daily_pnl: float = 0.0
    daily_trades: int = 0
    _current_date: str = ""
    _circuit_breaker_active: bool = False
    _slippage_total: float = 0.0
    _slippage_count: int = 0

    def _check_new_day(self):
        today = date.today().isoformat()
        if today != self._current_date:
            self._current_date = today
            self.daily_pnl = 0.0
            self.daily_trades = 0
            self._circuit_breaker_active = False
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
        return True

    def max_position_size(self) -> float:
        """Maximum USD we can risk on a single trade."""
        return self.bankroll * self.max_position_pct

    def record_trade(self, pnl: float, slippage: float = 0.0):
        """Record a completed trade."""
        self._check_new_day()
        self.daily_pnl += pnl
        self.daily_trades += 1
        self.bankroll += pnl

        if slippage != 0:
            self._slippage_total += abs(slippage)
            self._slippage_count += 1

        # Check circuit breaker
        loss_limit = self.bankroll * self.daily_loss_cap_pct
        if self.daily_pnl < -loss_limit:
            self._circuit_breaker_active = True
            logger.warning(
                "circuit_breaker_triggered",
                daily_pnl=round(self.daily_pnl, 2),
                loss_limit=round(loss_limit, 2),
                bankroll=round(self.bankroll, 2),
            )

        logger.info(
            "trade_recorded",
            pnl=round(pnl, 4),
            daily_pnl=round(self.daily_pnl, 4),
            bankroll=round(self.bankroll, 2),
            daily_trades=self.daily_trades,
        )

    @property
    def avg_slippage(self) -> float:
        if self._slippage_count == 0:
            return 0.0
        return self._slippage_total / self._slippage_count
