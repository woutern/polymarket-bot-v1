"""Tests for latency instrumentation — ensure timing fields are stored with trades."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from polybot.execution.paper_trader import PaperTrader
from polybot.models import Direction, Signal, SignalSource, TradeRecord


def _make_signal():
    return Signal(
        source=SignalSource.DIRECTIONAL,
        direction=Direction.UP,
        model_prob=0.80,
        market_price=0.55,
        ev=0.45,
        window_slug="btc-5m-1000",
        asset="BTC",
        p_bayesian=0.80,
    )


def _make_trader():
    risk = MagicMock()
    risk.can_trade.return_value = True
    risk.bankroll = 100.0
    risk.max_position_pct = 0.01
    risk.min_trade_usd = 1.0
    risk.max_trade_usd = 10.0
    db = MagicMock()
    db.insert_trade = AsyncMock()
    return PaperTrader(risk=risk, db=db)


class TestLatencyFields:
    async def test_latency_written_to_db(self):
        """Signal and bedrock latency are passed through to DB write."""
        trader = _make_trader()
        sig = _make_signal()

        result = await trader.execute(sig, signal_ms=42.5, bedrock_ms=180.3)
        assert result is not None

        call_kwargs = trader.db.insert_trade.call_args[0][0]
        assert call_kwargs["latency_signal_ms"] == 42.5
        assert call_kwargs["latency_bedrock_ms"] == 180.3
        assert call_kwargs["latency_order_ms"] == 0.0  # paper trader, no order round-trip

    async def test_zero_latency_when_not_provided(self):
        """Default latency is 0 when not passed."""
        trader = _make_trader()
        sig = _make_signal()

        result = await trader.execute(sig)
        assert result is not None

        call_kwargs = trader.db.insert_trade.call_args[0][0]
        assert call_kwargs["latency_signal_ms"] == 0.0
        assert call_kwargs["latency_bedrock_ms"] == 0.0

    def test_trade_record_has_latency_fields(self):
        """TradeRecord dataclass has latency fields with defaults."""
        t = TradeRecord()
        assert t.latency_signal_ms == 0.0
        assert t.latency_order_ms == 0.0
        assert t.latency_bedrock_ms == 0.0

    def test_bedrock_get_last_latency(self):
        """get_last_latency returns 0 for unknown window keys."""
        from polybot.strategy.bedrock_signal import get_last_latency
        assert get_last_latency("unknown-window-key") == 0.0
