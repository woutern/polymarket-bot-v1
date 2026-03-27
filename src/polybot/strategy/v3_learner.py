"""V3Learner — auto-tunes V3Params based on window outcomes.

After every BATCH_SIZE windows it evaluates:
  - dump_accuracy: % of dump decisions where signal was correct
  - dump_rate:     % of rebalance moments that triggered a dump
  - avg_combined_avg: quality of neutral windows

Then applies simple hill-climbing adjustments to V3Params.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

BATCH_SIZE = 3   # evaluate and potentially adjust after this many windows


@dataclass
class V3WindowRecord:
    """Per-window outcome record for the learner."""
    window_id: str
    went_up: bool | None           # actual Coinbase direction
    net_cost: float
    up_shares: int
    down_shares: int
    combined_avg: float
    rebalance_log: list[dict]      # from V3SimpleStrategy.rebalance_log()
    pnl: float                     # computed after resolution


class V3Learner:
    """Tracks window outcomes and adjusts V3Params to improve win rate.

    Usage:
        learner = V3Learner(params)
        ...
        learner.record(record)          # after each window
        summary = learner.maybe_tune()  # returns summary dict if batch complete
    """

    def __init__(self, params):
        from polybot.strategy.v3_simple import V3Params  # avoid circular
        self.params = params
        self._history: list[V3WindowRecord] = []
        self._windows_since_last_tune: int = 0
        self._tune_count: int = 0

    def record(self, rec: V3WindowRecord) -> None:
        self._history.append(rec)
        self._windows_since_last_tune += 1

    def maybe_tune(self) -> dict | None:
        """Evaluate and optionally adjust params. Returns summary if batch complete."""
        if self._windows_since_last_tune < BATCH_SIZE:
            return None

        batch = self._history[-BATCH_SIZE:]
        self._windows_since_last_tune = 0
        self._tune_count += 1

        summary = self._evaluate(batch)
        self._adjust(summary)
        return summary

    # ── Evaluation ───────────────────────────────────────────────────────────

    def _evaluate(self, batch: list[V3WindowRecord]) -> dict:
        dump_correct = 0
        dump_wrong = 0
        neutral_count = 0
        total_pnl = 0.0
        combined_avgs = []

        for rec in batch:
            total_pnl += rec.pnl
            if rec.combined_avg > 0:
                combined_avgs.append(rec.combined_avg)

            # Classify each rebalance decision
            for rb in rec.rebalance_log:
                decision = rb.get("decision", "none")
                if decision == "none":
                    continue

                if decision.startswith("dump"):
                    if rec.went_up is None:
                        continue
                    # dump_no = we went YES = correct if price went UP
                    # dump_yes = we went NO = correct if price went DOWN
                    if decision == "dump_no":
                        if rec.went_up:
                            dump_correct += 1
                        else:
                            dump_wrong += 1
                    elif decision == "dump_yes":
                        if not rec.went_up:
                            dump_correct += 1
                        else:
                            dump_wrong += 1
                elif decision.startswith("neutral"):
                    neutral_count += 1

        total_dump = dump_correct + dump_wrong
        dump_accuracy = dump_correct / total_dump if total_dump > 0 else None
        dump_rate = total_dump / max(len(batch) * len(self.params.rebalance_times), 1)
        avg_combined_avg = sum(combined_avgs) / len(combined_avgs) if combined_avgs else 0.0
        win_count = sum(1 for r in batch if r.pnl > 0)
        win_rate = win_count / len(batch) if batch else 0.0

        return {
            "batch": self._tune_count,
            "windows": len(batch),
            "win_rate": round(win_rate, 3),
            "total_pnl": round(total_pnl, 2),
            "dump_accuracy": round(dump_accuracy, 3) if dump_accuracy is not None else None,
            "dump_correct": dump_correct,
            "dump_wrong": dump_wrong,
            "dump_rate": round(dump_rate, 3),
            "neutral_count": neutral_count,
            "avg_combined_avg": round(avg_combined_avg, 4),
            "dump_threshold_up": self.params.dump_threshold_up,
            "dump_threshold_down": self.params.dump_threshold_down,
            "rebalance_usd": self.params.rebalance_usd,
        }

    # ── Adjustment ───────────────────────────────────────────────────────────

    def _adjust(self, summary: dict) -> None:
        p = self.params
        changes = []

        dump_accuracy = summary["dump_accuracy"]
        dump_correct = summary["dump_correct"]
        dump_wrong = summary["dump_wrong"]
        dump_rate = summary["dump_rate"]

        # Not enough dump samples to evaluate — don't adjust yet
        if (dump_correct + dump_wrong) < 2:
            logger.info("v3_learner: too few dumps (%d+%d) to adjust thresholds", dump_correct, dump_wrong)
        else:
            # Dump accuracy too low → require stronger signal (small step: 0.01)
            if dump_accuracy is not None and dump_accuracy < 0.52:
                old = p.dump_threshold_up
                p.dump_threshold_up = min(0.85, round(p.dump_threshold_up + 0.01, 3))
                p.dump_threshold_down = max(0.15, round(p.dump_threshold_down - 0.01, 3))
                changes.append(f"dump_threshold {old:.2f}→{p.dump_threshold_up:.2f} (accuracy={dump_accuracy:.1%})")

            # Dump accuracy high → lower bar, trigger more often (small step: 0.01)
            elif dump_accuracy is not None and dump_accuracy > 0.65 and dump_rate < 0.30:
                old = p.dump_threshold_up
                p.dump_threshold_up = max(0.60, round(p.dump_threshold_up - 0.01, 3))
                p.dump_threshold_down = min(0.40, round(p.dump_threshold_down + 0.01, 3))
                changes.append(f"dump_threshold {old:.2f}→{p.dump_threshold_up:.2f} (accuracy={dump_accuracy:.1%}, low rate)")

        # Under-deploying (combined_avg too high → not buying enough cheap side)
        avg_ca = summary["avg_combined_avg"]
        if avg_ca > 0 and avg_ca > 0.94 and summary["neutral_count"] > 2:
            old = p.rebalance_usd
            p.rebalance_usd = min(20.0, round(p.rebalance_usd + 1.5, 1))
            changes.append(f"rebalance_usd {old:.1f}→{p.rebalance_usd:.1f} (avg_combined_avg={avg_ca:.3f})")

        # Over-spending (budget exhausted, not getting to rebalance moments)
        if avg_ca > 0 and avg_ca < 0.80 and summary["win_rate"] < 0.45:
            old = p.rebalance_usd
            p.rebalance_usd = max(6.0, round(p.rebalance_usd - 1.0, 1))
            changes.append(f"rebalance_usd {old:.1f}→{p.rebalance_usd:.1f} (low combined_avg but losing)")

        if changes:
            logger.info("v3_learner_adjusted batch=%d changes=%s", self._tune_count, "; ".join(changes))
        else:
            logger.info("v3_learner_no_changes batch=%d win_rate=%.1f%% dump_accuracy=%s",
                        self._tune_count, summary["win_rate"] * 100,
                        f"{dump_accuracy:.1%}" if dump_accuracy else "N/A")

    def full_stats(self) -> dict:
        """Overall stats across all recorded windows."""
        if not self._history:
            return {}
        wins = sum(1 for r in self._history if r.pnl > 0)
        total_pnl = sum(r.pnl for r in self._history)
        return {
            "total_windows": len(self._history),
            "wins": wins,
            "win_rate": round(wins / len(self._history), 3),
            "total_pnl": round(total_pnl, 2),
            "tune_count": self._tune_count,
            "current_dump_threshold_up": self.params.dump_threshold_up,
            "current_dump_threshold_down": self.params.dump_threshold_down,
            "current_rebalance_usd": self.params.rebalance_usd,
        }
