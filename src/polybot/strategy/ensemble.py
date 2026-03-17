"""Ensemble signal combiner — merges base_rate + AI signal probabilities.

Produces a weighted average of multiple signal sources, with
configurable weights and a minimum confidence gate for the AI signal.
Tracks per-source accuracy for future weight tuning.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import structlog

from polybot.models import Direction
from polybot.strategy.ai_signal import AISignalResult

logger = structlog.get_logger()


@dataclass
class SourceAccuracy:
    """Tracks prediction accuracy for a single signal source."""

    correct: int = 0
    total: int = 0

    @property
    def accuracy(self) -> float:
        if self.total == 0:
            return 0.0
        return self.correct / self.total

    def record(self, predicted_up: bool, actual_up: bool) -> None:
        self.total += 1
        if predicted_up == actual_up:
            self.correct += 1


@dataclass
class EnsembleResult:
    """Result of combining multiple signal sources."""

    combined_prob: float  # P(UP) after ensemble
    base_rate_prob: float
    ai_prob: float | None  # None if AI was not used
    ai_confidence: float | None
    ai_used: bool
    base_rate_weight: float
    ai_weight: float


class EnsembleCombiner:
    """Combines base_rate probability with AI signal probability.

    The AI signal is only used when its confidence exceeds a minimum
    threshold. Otherwise the ensemble falls back to pure base_rate.

    Weights:
        base_rate_weight + ai_weight = 1.0 (normalized internally)
        Default: 0.6 base_rate, 0.4 AI

    Usage:
        combiner = EnsembleCombiner(base_rate_weight=0.6, ai_weight=0.4)
        result = combiner.combine(
            base_rate_prob=0.65,
            ai_result=ai_signal_result,
            min_confidence=0.6,
        )
    """

    def __init__(
        self,
        base_rate_weight: float = 0.6,
        ai_weight: float = 0.4,
        min_confidence: float = 0.6,
    ):
        self.base_rate_weight = base_rate_weight
        self.ai_weight = ai_weight
        self.min_confidence = min_confidence

        # Accuracy tracking per source
        self.accuracy: dict[str, SourceAccuracy] = {
            "base_rate": SourceAccuracy(),
            "ai": SourceAccuracy(),
            "ensemble": SourceAccuracy(),
        }

        # History for analysis
        self._history: list[dict] = []

    def combine(
        self,
        base_rate_prob: float,
        ai_result: AISignalResult | None = None,
        min_confidence: float | None = None,
    ) -> EnsembleResult:
        """Combine base_rate and AI probabilities.

        Args:
            base_rate_prob: P(UP) from base_rate + Bayesian updater.
            ai_result: AI signal result, or None if unavailable.
            min_confidence: Override minimum confidence threshold.

        Returns:
            EnsembleResult with combined probability.
        """
        threshold = min_confidence if min_confidence is not None else self.min_confidence

        # Determine if AI signal qualifies
        ai_used = False
        ai_prob: float | None = None
        ai_confidence: float | None = None

        if ai_result is not None and ai_result.confidence >= threshold:
            ai_used = True
            ai_confidence = ai_result.confidence

            # Convert AI direction + confidence to P(UP)
            if ai_result.direction == Direction.UP:
                ai_prob = 0.5 + (ai_result.confidence * 0.5)
            else:
                ai_prob = 0.5 - (ai_result.confidence * 0.5)
        elif ai_result is not None:
            ai_confidence = ai_result.confidence
            logger.debug(
                "ai_signal_below_threshold",
                confidence=round(ai_result.confidence, 3),
                threshold=threshold,
                asset=ai_result.asset,
            )

        # Calculate combined probability
        if ai_used and ai_prob is not None:
            # Weighted average
            total_weight = self.base_rate_weight + self.ai_weight
            combined = (
                self.base_rate_weight * base_rate_prob
                + self.ai_weight * ai_prob
            ) / total_weight
        else:
            # Fall back to base_rate only
            combined = base_rate_prob

        combined = max(0.001, min(0.999, combined))

        result = EnsembleResult(
            combined_prob=combined,
            base_rate_prob=base_rate_prob,
            ai_prob=ai_prob,
            ai_confidence=ai_confidence,
            ai_used=ai_used,
            base_rate_weight=self.base_rate_weight,
            ai_weight=self.ai_weight,
        )

        logger.info(
            "ensemble_combined",
            base_rate_prob=round(base_rate_prob, 4),
            ai_prob=round(ai_prob, 4) if ai_prob is not None else None,
            ai_confidence=round(ai_confidence, 3) if ai_confidence is not None else None,
            ai_used=ai_used,
            combined_prob=round(combined, 4),
        )

        return result

    def record_outcome(
        self,
        base_rate_prob: float,
        ai_result: AISignalResult | None,
        ensemble_result: EnsembleResult,
        actual_up: bool,
    ) -> None:
        """Record the actual outcome to track accuracy of each source.

        Call this after a window resolves to update accuracy stats.
        """
        # Base rate accuracy
        base_predicted_up = base_rate_prob > 0.5
        self.accuracy["base_rate"].record(base_predicted_up, actual_up)

        # AI accuracy (only if it was used)
        if ai_result is not None:
            ai_predicted_up = ai_result.direction == Direction.UP
            self.accuracy["ai"].record(ai_predicted_up, actual_up)

        # Ensemble accuracy
        ensemble_predicted_up = ensemble_result.combined_prob > 0.5
        self.accuracy["ensemble"].record(ensemble_predicted_up, actual_up)

        # Store in history
        self._history.append(
            {
                "timestamp": time.time(),
                "base_rate_prob": base_rate_prob,
                "ai_direction": ai_result.direction.value if ai_result else None,
                "ai_confidence": ai_result.confidence if ai_result else None,
                "combined_prob": ensemble_result.combined_prob,
                "actual_up": actual_up,
            }
        )

        # Keep history bounded
        if len(self._history) > 1000:
            self._history = self._history[-500:]

        logger.info(
            "ensemble_outcome_recorded",
            actual_up=actual_up,
            base_rate_accuracy=round(self.accuracy["base_rate"].accuracy, 3),
            ai_accuracy=round(self.accuracy["ai"].accuracy, 3),
            ensemble_accuracy=round(self.accuracy["ensemble"].accuracy, 3),
            total_samples=self.accuracy["ensemble"].total,
        )

    def get_accuracy_report(self) -> dict:
        """Return a summary of per-source accuracy."""
        return {
            name: {
                "accuracy": round(acc.accuracy, 4),
                "correct": acc.correct,
                "total": acc.total,
            }
            for name, acc in self.accuracy.items()
        }
