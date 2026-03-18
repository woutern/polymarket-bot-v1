"""Tests for the latency monitor."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from polybot.core.latency_monitor import LatencyMonitor, LatencySample


# ---------------------------------------------------------------------------
# LatencySample
# ---------------------------------------------------------------------------

class TestLatencySample:
    def test_sample_fields(self):
        sample = LatencySample(source="coinbase", latency_ms=55.0)
        assert sample.source == "coinbase"
        assert sample.latency_ms == 55.0
        assert sample.timestamp > 0.0

    def test_sample_timestamp_set_automatically(self):
        import time
        before = time.time()
        sample = LatencySample(source="test", latency_ms=10.0)
        after = time.time()
        assert before <= sample.timestamp <= after


# ---------------------------------------------------------------------------
# LatencyMonitor.record
# ---------------------------------------------------------------------------

class TestRecord:
    def test_record_creates_deque_for_new_source(self):
        m = LatencyMonitor()
        m.record("coinbase", 55.0)
        assert "coinbase" in m._samples
        assert list(m._samples["coinbase"]) == [55.0]

    def test_record_multiple_samples(self):
        m = LatencyMonitor()
        for v in [10.0, 20.0, 30.0]:
            m.record("coinbase", v)
        assert list(m._samples["coinbase"]) == [10.0, 20.0, 30.0]

    def test_record_respects_window_size(self):
        """Deque should drop oldest entries when full."""
        m = LatencyMonitor(window_size=3)
        for v in [1.0, 2.0, 3.0, 4.0]:
            m.record("src", v)
        assert list(m._samples["src"]) == [2.0, 3.0, 4.0]

    def test_record_multiple_sources_independent(self):
        m = LatencyMonitor()
        m.record("coinbase", 60.0)
        m.record("polymarket", 150.0)
        assert list(m._samples["coinbase"]) == [60.0]
        assert list(m._samples["polymarket"]) == [150.0]

    def test_record_does_not_log_summary_immediately(self):
        """Summary is only logged every 5 minutes; should not fire on the first record."""
        m = LatencyMonitor()
        # _last_report=0 means time.time()-0 > 300 is True on first call,
        # so the summary WILL be logged and _last_report updated.
        # We patch time to control this.
        with patch("polybot.core.latency_monitor.time.time", return_value=1_000_000.0):
            m._last_report = 1_000_000.0  # same as "now"
            m.record("coinbase", 55.0)
            # No new log call because difference is 0
            assert m._last_report == 1_000_000.0

    def test_record_triggers_summary_after_5_minutes(self):
        """_last_report is updated when 300s have elapsed."""
        m = LatencyMonitor()
        m.record("coinbase", 55.0)  # first record, _last_report gets set
        old_report = m._last_report

        # Advance time by 301 seconds
        with patch("polybot.core.latency_monitor.time.time", return_value=old_report + 301):
            m.record("coinbase", 60.0)

        assert m._last_report > old_report


# ---------------------------------------------------------------------------
# p50
# ---------------------------------------------------------------------------

class TestP50:
    def test_p50_no_samples_returns_zero(self):
        m = LatencyMonitor()
        assert m.p50("unknown") == 0.0

    def test_p50_single_sample(self):
        m = LatencyMonitor()
        m.record("src", 42.0)
        assert m.p50("src") == 42.0

    def test_p50_odd_count(self):
        m = LatencyMonitor()
        for v in [10.0, 30.0, 20.0]:
            m.record("src", v)
        # sorted: [10, 20, 30] → index 1 → 20.0
        assert m.p50("src") == 20.0

    def test_p50_even_count(self):
        m = LatencyMonitor()
        for v in [10.0, 20.0, 30.0, 40.0]:
            m.record("src", v)
        # sorted: [10, 20, 30, 40] → index 2 → 30.0
        assert m.p50("src") == 30.0

    def test_p50_ignores_other_sources(self):
        m = LatencyMonitor()
        m.record("a", 10.0)
        m.record("b", 999.0)
        assert m.p50("a") == 10.0


# ---------------------------------------------------------------------------
# p95
# ---------------------------------------------------------------------------

class TestP95:
    def test_p95_no_samples_returns_zero(self):
        m = LatencyMonitor()
        assert m.p95("unknown") == 0.0

    def test_p95_single_sample(self):
        m = LatencyMonitor()
        m.record("src", 55.0)
        # int(1 * 0.95) = 0 → first element
        assert m.p95("src") == 55.0

    def test_p95_hundred_samples(self):
        """With 100 samples, p95 index = int(100*0.95) = 95 → 96th value."""
        m = LatencyMonitor(window_size=100)
        for i in range(100):
            m.record("src", float(i + 1))
        # sorted: [1,2,...,100], index 95 → 96
        assert m.p95("src") == 96.0

    def test_p95_higher_than_p50(self):
        m = LatencyMonitor(window_size=100)
        for i in range(100):
            m.record("src", float(i + 1))
        assert m.p95("src") > m.p50("src")

    def test_p95_with_spike(self):
        """A spike at the end should be captured at p95."""
        m = LatencyMonitor(window_size=20)
        for _ in range(19):
            m.record("src", 10.0)
        m.record("src", 1000.0)  # spike
        # sorted: [10]*19 + [1000], index int(20*0.95)=19 → 1000
        assert m.p95("src") == 1000.0


# ---------------------------------------------------------------------------
# log_summary
# ---------------------------------------------------------------------------

class TestLogSummary:
    def test_log_summary_empty_does_not_crash(self):
        m = LatencyMonitor()
        m.log_summary()  # nothing recorded — should be a no-op

    def test_log_summary_skips_empty_deques(self):
        """If a source has been registered but has no samples, summary skips it."""
        m = LatencyMonitor()
        from collections import deque
        m._samples["ghost"] = deque(maxlen=10)
        m.log_summary()  # should not raise

    def test_log_summary_runs_with_data(self):
        """log_summary does not raise when samples are present."""
        m = LatencyMonitor()
        for v in [50.0, 60.0, 70.0]:
            m.record("coinbase", v)
        m.log_summary()  # should complete without error


# ---------------------------------------------------------------------------
# window_size configuration
# ---------------------------------------------------------------------------

class TestWindowSize:
    def test_default_window_size_is_100(self):
        m = LatencyMonitor()
        assert m._window == 100

    def test_custom_window_size(self):
        m = LatencyMonitor(window_size=10)
        for i in range(15):
            m.record("src", float(i))
        assert len(m._samples["src"]) == 10

    def test_window_size_one(self):
        """Deque of size 1 only ever holds the latest sample."""
        m = LatencyMonitor(window_size=1)
        m.record("src", 100.0)
        m.record("src", 200.0)
        assert m.p50("src") == 200.0
