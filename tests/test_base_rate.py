"""Tests for base rate calculator."""

from polybot.strategy.base_rate import BaseRateTable


def _make_candles(windows: list[list[float]]) -> list[dict]:
    """Create synthetic candle data.

    Each window is 5 candles (1 per minute). Each entry in `windows`
    is a list of 6 prices: [open, c1, c2, c3, c4, close].
    """
    candles = []
    for i, prices in enumerate(windows):
        base_ts = i * 300  # Each window starts at a multiple of 300
        for j in range(5):
            candles.append(
                {
                    "start": base_ts + j * 60,
                    "open": prices[j],
                    "high": max(prices[j], prices[j + 1]) if j + 1 < len(prices) else prices[j],
                    "low": min(prices[j], prices[j + 1]) if j + 1 < len(prices) else prices[j],
                    "close": prices[j + 1] if j + 1 < len(prices) else prices[j],
                    "volume": 1.0,
                }
            )
    return candles


def test_base_rate_strong_up():
    """When BTC moves up consistently, P(up) should be high."""
    # 50 windows where price goes up throughout
    windows = [[100, 100.1, 100.2, 100.3, 100.4, 100.5]] * 50
    table = BaseRateTable()
    table.build_from_candles(_make_candles(windows))
    # T-10s uses candle[4].open = 100.4, so pct_move=0.4% → bin (0.3, 10)
    p = table.lookup(0.4, 10)
    assert p > 0.8, f"Expected P(up) > 0.8 for consistent uptrend, got {p}"


def test_base_rate_strong_down():
    """When BTC moves down, P(up) should be low."""
    windows = [[100, 99.9, 99.8, 99.7, 99.6, 99.5]] * 50
    table = BaseRateTable()
    table.build_from_candles(_make_candles(windows))
    # Data produces -0.5% at T-10s
    p = table.lookup(-0.5, 10)
    assert p < 0.2, f"Expected P(up) < 0.2 for consistent downtrend, got {p}"


def test_base_rate_no_data_returns_neutral():
    """With no data, should return 0.5."""
    table = BaseRateTable()
    p = table.lookup(0.1, 10)
    assert p == 0.5


def test_base_rate_summary():
    windows = [[100, 100.1, 100.2, 100.3, 100.4, 100.5]] * 30
    table = BaseRateTable()
    table.build_from_candles(_make_candles(windows))
    summary = table.summary()
    assert len(summary) > 0
    for entry in summary:
        assert "p_up" in entry
        assert entry["total"] >= 20
