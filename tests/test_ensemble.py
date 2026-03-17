"""Tests for the ensemble signal combiner."""

from __future__ import annotations

import time

import pytest

from polybot.models import Direction
from polybot.strategy.ai_signal import AISignalResult
from polybot.strategy.ensemble import EnsembleCombiner, EnsembleResult, SourceAccuracy


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ai_result(
    direction: Direction = Direction.UP,
    confidence: float = 0.8,
    asset: str = "BTC",
) -> AISignalResult:
    return AISignalResult(
        direction=direction,
        confidence=confidence,
        reasoning="test",
        timestamp=time.time(),
        asset=asset,
    )


# ---------------------------------------------------------------------------
# Weighting math
# ---------------------------------------------------------------------------

class TestWeighting:
    def test_basic_weighted_average_up(self):
        """AI says UP with 0.8 confidence => ai_prob = 0.5 + 0.4 = 0.9"""
        combiner = EnsembleCombiner(base_rate_weight=0.6, ai_weight=0.4)
        ai = _ai_result(Direction.UP, confidence=0.8)
        result = combiner.combine(base_rate_prob=0.6, ai_result=ai)

        assert result.ai_used is True
        # ai_prob = 0.5 + (0.8 * 0.5) = 0.9
        assert result.ai_prob == pytest.approx(0.9, abs=0.001)
        # combined = (0.6*0.6 + 0.4*0.9) / 1.0 = 0.36 + 0.36 = 0.72
        assert result.combined_prob == pytest.approx(0.72, abs=0.001)

    def test_basic_weighted_average_down(self):
        """AI says DOWN with 0.8 confidence => ai_prob = 0.5 - 0.4 = 0.1"""
        combiner = EnsembleCombiner(base_rate_weight=0.6, ai_weight=0.4)
        ai = _ai_result(Direction.DOWN, confidence=0.8)
        result = combiner.combine(base_rate_prob=0.6, ai_result=ai)

        assert result.ai_used is True
        # ai_prob = 0.5 - (0.8 * 0.5) = 0.1
        assert result.ai_prob == pytest.approx(0.1, abs=0.001)
        # combined = (0.6*0.6 + 0.4*0.1) / 1.0 = 0.36 + 0.04 = 0.40
        assert result.combined_prob == pytest.approx(0.40, abs=0.001)

    def test_equal_weights(self):
        combiner = EnsembleCombiner(base_rate_weight=0.5, ai_weight=0.5)
        ai = _ai_result(Direction.UP, confidence=1.0)
        result = combiner.combine(base_rate_prob=0.5, ai_result=ai)

        # ai_prob = 0.5 + (1.0 * 0.5) = 1.0
        # combined = (0.5*0.5 + 0.5*1.0) / 1.0 = 0.75
        assert result.combined_prob == pytest.approx(0.75, abs=0.001)

    def test_heavy_base_rate_weight(self):
        combiner = EnsembleCombiner(base_rate_weight=0.9, ai_weight=0.1)
        ai = _ai_result(Direction.UP, confidence=1.0)
        result = combiner.combine(base_rate_prob=0.5, ai_result=ai)

        # ai_prob = 1.0
        # combined = (0.9*0.5 + 0.1*1.0) / 1.0 = 0.55
        assert result.combined_prob == pytest.approx(0.55, abs=0.001)

    def test_combined_prob_clamped(self):
        combiner = EnsembleCombiner(base_rate_weight=0.5, ai_weight=0.5)
        ai = _ai_result(Direction.DOWN, confidence=1.0)
        # ai_prob = 0.0, base = 0.0 => combined would be 0.0, clamped to 0.001
        result = combiner.combine(base_rate_prob=0.0, ai_result=ai)
        assert result.combined_prob >= 0.001

    def test_combined_prob_clamped_high(self):
        combiner = EnsembleCombiner(base_rate_weight=0.5, ai_weight=0.5)
        ai = _ai_result(Direction.UP, confidence=1.0)
        result = combiner.combine(base_rate_prob=1.0, ai_result=ai)
        assert result.combined_prob <= 0.999


# ---------------------------------------------------------------------------
# Confidence threshold
# ---------------------------------------------------------------------------

class TestConfidenceThreshold:
    def test_low_confidence_ignored(self):
        combiner = EnsembleCombiner(min_confidence=0.6)
        ai = _ai_result(Direction.UP, confidence=0.5)  # Below threshold
        result = combiner.combine(base_rate_prob=0.65, ai_result=ai)

        assert result.ai_used is False
        assert result.ai_prob is None
        # Falls back to base_rate only
        assert result.combined_prob == pytest.approx(0.65, abs=0.001)

    def test_exactly_at_threshold_accepted(self):
        combiner = EnsembleCombiner(min_confidence=0.6)
        ai = _ai_result(Direction.UP, confidence=0.6)  # Exactly at threshold
        result = combiner.combine(base_rate_prob=0.5, ai_result=ai)

        assert result.ai_used is True
        assert result.ai_prob is not None

    def test_override_threshold(self):
        combiner = EnsembleCombiner(min_confidence=0.6)
        ai = _ai_result(Direction.UP, confidence=0.3)
        # Override to lower threshold
        result = combiner.combine(base_rate_prob=0.5, ai_result=ai, min_confidence=0.2)

        assert result.ai_used is True

    def test_none_ai_result(self):
        combiner = EnsembleCombiner()
        result = combiner.combine(base_rate_prob=0.7, ai_result=None)

        assert result.ai_used is False
        assert result.ai_prob is None
        assert result.ai_confidence is None
        assert result.combined_prob == pytest.approx(0.7, abs=0.001)


# ---------------------------------------------------------------------------
# Accuracy tracking
# ---------------------------------------------------------------------------

class TestAccuracyTracking:
    def test_source_accuracy_basic(self):
        acc = SourceAccuracy()
        acc.record(predicted_up=True, actual_up=True)
        acc.record(predicted_up=True, actual_up=False)
        assert acc.accuracy == pytest.approx(0.5)
        assert acc.total == 2
        assert acc.correct == 1

    def test_source_accuracy_empty(self):
        acc = SourceAccuracy()
        assert acc.accuracy == 0.0

    def test_record_outcome_all_sources(self):
        combiner = EnsembleCombiner()
        ai = _ai_result(Direction.UP, confidence=0.8)
        result = combiner.combine(base_rate_prob=0.7, ai_result=ai)

        combiner.record_outcome(
            base_rate_prob=0.7,
            ai_result=ai,
            ensemble_result=result,
            actual_up=True,
        )

        report = combiner.get_accuracy_report()
        assert report["base_rate"]["total"] == 1
        assert report["base_rate"]["correct"] == 1
        assert report["ai"]["total"] == 1
        assert report["ai"]["correct"] == 1
        assert report["ensemble"]["total"] == 1

    def test_record_outcome_without_ai(self):
        combiner = EnsembleCombiner()
        result = combiner.combine(base_rate_prob=0.3, ai_result=None)

        combiner.record_outcome(
            base_rate_prob=0.3,
            ai_result=None,
            ensemble_result=result,
            actual_up=True,
        )

        report = combiner.get_accuracy_report()
        assert report["base_rate"]["total"] == 1
        assert report["base_rate"]["correct"] == 0  # predicted down (0.3 < 0.5)
        assert report["ai"]["total"] == 0  # AI was not used

    def test_accuracy_after_multiple_outcomes(self):
        combiner = EnsembleCombiner()

        for actual_up in [True, True, True, False, False]:
            ai = _ai_result(Direction.UP, confidence=0.8)
            result = combiner.combine(base_rate_prob=0.7, ai_result=ai)
            combiner.record_outcome(0.7, ai, result, actual_up)

        report = combiner.get_accuracy_report()
        # base_rate predicted UP (0.7>0.5) every time, actual UP 3/5
        assert report["base_rate"]["accuracy"] == pytest.approx(0.6)
        assert report["base_rate"]["total"] == 5


# ---------------------------------------------------------------------------
# EnsembleResult fields
# ---------------------------------------------------------------------------

class TestEnsembleResult:
    def test_result_fields_with_ai(self):
        combiner = EnsembleCombiner(base_rate_weight=0.6, ai_weight=0.4)
        ai = _ai_result(Direction.UP, confidence=0.9)
        result = combiner.combine(base_rate_prob=0.55, ai_result=ai)

        assert result.base_rate_prob == 0.55
        assert result.ai_prob is not None
        assert result.ai_confidence == 0.9
        assert result.ai_used is True
        assert result.base_rate_weight == 0.6
        assert result.ai_weight == 0.4

    def test_result_fields_without_ai(self):
        combiner = EnsembleCombiner()
        result = combiner.combine(base_rate_prob=0.55, ai_result=None)

        assert result.base_rate_prob == 0.55
        assert result.ai_prob is None
        assert result.ai_confidence is None
        assert result.ai_used is False
