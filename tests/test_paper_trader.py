"""Tests for PaperTrader execution guards."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from polybot.execution.paper_trader import PaperTrader
from polybot.models import Direction, Signal, SignalSource


def _make_signal(slug="btc-5m-1000", market_price=0.65, direction=Direction.UP, asset="BTC"):
    return Signal(
        source=SignalSource.DIRECTIONAL,
        direction=direction,
        model_prob=0.85,
        market_price=market_price,
        ev=0.31,
        window_slug=slug,
        asset=asset,
    )


def _make_trader():
    risk = MagicMock()
    risk.can_trade.return_value = True
    risk.bankroll = 1000.0
    risk.max_position_pct = 0.01
    risk.get_bet_size = lambda lgbm_prob=0.5: 1.50
    risk.min_trade_usd = 1.0
    risk.max_trade_usd = 10.0
    db = MagicMock()
    db.insert_trade = AsyncMock()
    return PaperTrader(risk=risk, db=db)


async def test_dedup_same_window():
    """Second trade on same window_slug is rejected."""
    trader = _make_trader()
    sig = _make_signal(slug="btc-5m-1000", market_price=0.65)

    first = await trader.execute(sig)
    assert first is not None

    second = await trader.execute(sig)
    assert second is None


async def test_dedup_different_windows():
    """Trades on different slugs are both accepted."""
    trader = _make_trader()
    sig1 = _make_signal(slug="btc-5m-1000", market_price=0.65)
    sig2 = _make_signal(slug="btc-5m-1300", market_price=0.60)

    first = await trader.execute(sig1)
    second = await trader.execute(sig2)
    assert first is not None
    assert second is not None


async def test_price_too_low_rejected():
    """Trade with market_price < 0.20 is rejected (uninitialized orderbook guard)."""
    trader = _make_trader()
    sig = _make_signal(slug="btc-5m-1000", market_price=0.14)
    result = await trader.execute(sig)
    assert result is None


async def test_price_at_floor_accepted():
    """Trade with market_price = 0.20 is allowed."""
    trader = _make_trader()
    sig = _make_signal(slug="btc-5m-1000", market_price=0.20)
    result = await trader.execute(sig)
    assert result is not None


async def test_circuit_breaker_blocks():
    """Circuit breaker blocks execution."""
    trader = _make_trader()
    trader.risk.can_trade.return_value = False
    sig = _make_signal()
    result = await trader.execute(sig)
    assert result is None
