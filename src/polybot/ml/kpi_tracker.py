"""KPI tracker — computes edge metrics after every resolved trade."""

from __future__ import annotations

import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

logger = logging.getLogger(__name__)


def _safe_div(a: float, b: float, default: float = 0.0) -> float:
    return a / b if b != 0 else default


def _brier(probs: list[float], outcomes: list[int]) -> float:
    if not probs:
        return 0.25
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / len(probs)


def _auc(probs: list[float], outcomes: list[int]) -> float:
    if len(set(outcomes)) < 2 or len(probs) < 5:
        return 0.5
    try:
        from sklearn.metrics import roc_auc_score
        return roc_auc_score(outcomes, probs)
    except Exception:
        return 0.5


@dataclass
class SPRTTracker:
    """Sequential Probability Ratio Test for edge detection."""
    log_lambda: float = 0.0
    trades: int = 0
    A: float = field(default_factory=lambda: math.log((1 - 0.20) / 0.05))  # ~2.77
    B: float = field(default_factory=lambda: math.log(0.20 / (1 - 0.05)))  # ~-3.66

    def update(self, p1: float, p0: float, outcome: int):
        """Update SPRT after a resolved trade."""
        p1 = max(0.01, min(0.99, p1))
        p0 = max(0.01, min(0.99, p0))
        self.log_lambda += (
            outcome * math.log(p1 / p0) +
            (1 - outcome) * math.log((1 - p1) / (1 - p0))
        )
        self.trades += 1

    @property
    def status(self) -> str:
        if self.log_lambda >= self.A:
            return "EDGE_CONFIRMED"
        if self.log_lambda <= self.B:
            return "REASSESS"
        return "ACCUMULATING"

    @property
    def trades_to_significance(self) -> int:
        if self.trades < 2:
            return 400
        avg_increment = self.log_lambda / self.trades if self.trades > 0 else 0.01
        if avg_increment <= 0:
            return 999
        remaining = self.A - self.log_lambda
        return max(0, int(remaining / max(avg_increment, 0.001)))


class KPITracker:
    """Computes and stores KPI snapshots after every resolved trade."""

    def __init__(self):
        self.sprt_overall = SPRTTracker()
        self.sprt_per_pair: dict[str, SPRTTracker] = {}
        self._dynamo_table = None

    def _get_table(self):
        if self._dynamo_table is None:
            try:
                import os
                import boto3
                profile = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None
                session = boto3.Session(profile_name=profile, region_name="us-east-1")
                self._dynamo_table = session.resource("dynamodb").Table("polymarket-bot-kpi-snapshots")
            except Exception:
                pass
        return self._dynamo_table

    def compute_snapshot(self, trades: list[dict]) -> dict:
        """Compute full KPI snapshot from resolved trades."""
        resolved = [t for t in trades if t.get("resolved") and t.get("outcome_source") == "polymarket_verified"]
        if len(resolved) < 2:
            return {"status": "insufficient_data", "trades_total": len(resolved)}

        # Extract arrays
        pnls = [float(t.get("pnl", 0) or 0) for t in resolved]
        lgbm_probs = [float(t.get("p_final", 0.5) or 0.5) for t in resolved]
        market_prices = [float(t.get("price", 0.5) or 0.5) for t in resolved]
        outcomes = [1 if float(t.get("pnl", 0) or 0) > 0 else 0 for t in resolved]
        fill_prices = [float(t.get("fill_price", 0) or 0) for t in resolved]

        n = len(resolved)
        last_50 = resolved[-50:]
        last_20 = resolved[-20:]

        wins = sum(outcomes)
        wins_50 = sum(1 for t in last_50 if float(t.get("pnl", 0) or 0) > 0)
        wins_20 = sum(1 for t in last_20 if float(t.get("pnl", 0) or 0) > 0)

        # Brier scores
        our_brier = _brier(lgbm_probs, outcomes)
        market_brier = _brier(market_prices, outcomes)
        bss = 1 - _safe_div(our_brier, market_brier, 1.0)

        # Edge per trade
        edges = [p - m for p, m in zip(lgbm_probs, market_prices)]
        win_edges = [e for e, o in zip(edges, outcomes) if o == 1]
        loss_edges = [e for e, o in zip(edges, outcomes) if o == 0]

        # Sharpe (daily P&L based)
        daily_pnls = defaultdict(float)
        for t in resolved:
            from datetime import datetime, timezone
            ts = float(t.get("timestamp", 0) or 0)
            if ts > 0:
                day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                daily_pnls[day] += float(t.get("pnl", 0) or 0)
        daily_vals = list(daily_pnls.values()) if daily_pnls else [0]
        mean_daily = sum(daily_vals) / len(daily_vals) if daily_vals else 0
        std_daily = (sum((d - mean_daily) ** 2 for d in daily_vals) / max(len(daily_vals) - 1, 1)) ** 0.5 if len(daily_vals) > 1 else 1
        sharpe = _safe_div(mean_daily, std_daily) * math.sqrt(365)

        # Max drawdown (rolling)
        cumulative = []
        running = 0
        for p in pnls[-50:]:
            running += p
            cumulative.append(running)
        peak = 0
        max_dd = 0
        for c in cumulative:
            if c > peak:
                peak = c
            dd = peak - c
            if dd > max_dd:
                max_dd = dd

        # Model quality
        lgbm_auc = _auc(lgbm_probs[-100:], outcomes[-100:])
        win_probs = [p for p, o in zip(lgbm_probs, outcomes) if o == 1]
        loss_probs = [p for p, o in zip(lgbm_probs, outcomes) if o == 0]
        avg_prob_wins = sum(win_probs) / len(win_probs) if win_probs else 0.5
        avg_prob_losses = sum(loss_probs) / len(loss_probs) if loss_probs else 0.5

        # Per pair
        pair_stats = {}
        for t in resolved:
            asset = t.get("asset", "BTC")
            slug = t.get("window_slug", "")
            pair = f"{asset}_5m"
            if pair not in pair_stats:
                pair_stats[pair] = {"trades": 0, "wins": 0, "pnl": 0.0, "entries": [], "probs": []}
            pair_stats[pair]["trades"] += 1
            pnl = float(t.get("pnl", 0) or 0)
            pair_stats[pair]["pnl"] += pnl
            if pnl > 0:
                pair_stats[pair]["wins"] += 1
            fp = float(t.get("fill_price", 0) or 0)
            if fp > 0:
                pair_stats[pair]["entries"].append(fp)
            pair_stats[pair]["probs"].append(float(t.get("p_final", 0.5) or 0.5))

        pair_summary = {}
        for pair, ps in pair_stats.items():
            pair_summary[pair] = {
                "trades": ps["trades"],
                "wins": ps["wins"],
                "win_rate": round(_safe_div(ps["wins"], ps["trades"]), 3),
                "avg_entry": round(sum(ps["entries"]) / len(ps["entries"]), 3) if ps["entries"] else 0,
                "avg_lgbm_prob": round(sum(ps["probs"]) / len(ps["probs"]), 3) if ps["probs"] else 0.5,
                "total_pnl": round(ps["pnl"], 2),
            }

        # Today's P&L
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_pnl_today = daily_pnls.get(today, 0.0)

        snapshot = {
            "snapshot_id": f"kpi_{int(time.time())}",
            "timestamp": time.time(),
            "trades_total": n,
            "trades_last_50": len(last_50),
            "brier_score": round(our_brier, 6),
            "brier_skill_score": round(bss, 6),
            "market_brier": round(market_brier, 6),
            "win_rate_total": round(_safe_div(wins, n), 4),
            "win_rate_last_50": round(_safe_div(wins_50, len(last_50)), 4),
            "win_rate_last_20": round(_safe_div(wins_20, len(last_20)), 4),
            "edge_per_trade": round(sum(edges) / len(edges), 6) if edges else 0,
            "edge_per_trade_wins": round(sum(win_edges) / len(win_edges), 6) if win_edges else 0,
            "edge_per_trade_losses": round(sum(loss_edges) / len(loss_edges), 6) if loss_edges else 0,
            "avg_entry_price": round(sum(fill_prices[-50:]) / len(fill_prices[-50:]), 4) if fill_prices else 0,
            "sharpe_ratio": round(sharpe, 4),
            "max_drawdown": round(max_dd, 4),
            "daily_pnl_today": round(daily_pnl_today, 4),
            "lgbm_auc_live": round(lgbm_auc, 4),
            "lgbm_avg_prob_wins": round(avg_prob_wins, 4),
            "lgbm_avg_prob_losses": round(avg_prob_losses, 4),
            "lgbm_separation": round(avg_prob_wins - avg_prob_losses, 4),
            "sprt_log_lambda": round(self.sprt_overall.log_lambda, 6),
            "sprt_status": self.sprt_overall.status,
            "sprt_trades": self.sprt_overall.trades,
            "trades_to_significance": self.sprt_overall.trades_to_significance,
            "pair_stats": pair_summary,
        }
        return snapshot

    def on_trade_resolved(self, trade: dict, all_trades: list[dict]):
        """Called after every resolved trade. Updates SPRT and stores snapshot."""
        # Update SPRT
        p1 = float(trade.get("p_final", 0.5) or 0.5)
        p0 = float(trade.get("price", 0.5) or 0.5)
        outcome = 1 if float(trade.get("pnl", 0) or 0) > 0 else 0

        self.sprt_overall.update(p1, p0, outcome)

        asset = trade.get("asset", "BTC")
        pair = f"{asset}_5m"
        if pair not in self.sprt_per_pair:
            self.sprt_per_pair[pair] = SPRTTracker()
        self.sprt_per_pair[pair].update(p1, p0, outcome)

        # Compute and store snapshot
        snapshot = self.compute_snapshot(all_trades)
        if snapshot.get("status") == "insufficient_data":
            return snapshot

        # Add per-pair SPRT
        for p, tracker in self.sprt_per_pair.items():
            if p in snapshot.get("pair_stats", {}):
                snapshot["pair_stats"][p]["sprt_log_lambda"] = round(tracker.log_lambda, 4)
                snapshot["pair_stats"][p]["sprt_status"] = tracker.status

        # Store to DynamoDB
        try:
            table = self._get_table()
            if table:
                from polybot.storage.dynamo import _to_decimal
                table.put_item(Item=_to_decimal(snapshot))
        except Exception as e:
            logger.debug(f"kpi_store_failed: {e}")

        # Alerts
        self._check_alerts(snapshot)

        return snapshot

    def _check_alerts(self, snapshot: dict):
        wr20 = snapshot.get("win_rate_last_20", 1.0)
        if wr20 < 0.55 and snapshot.get("trades_total", 0) >= 20:
            logger.warning("ALERT_LOW_WIN_RATE", win_rate_20=wr20)
        dd = snapshot.get("max_drawdown", 0)
        if dd > 0.15:
            logger.warning("ALERT_HIGH_DRAWDOWN", max_drawdown=dd)
        status = snapshot.get("sprt_status", "")
        if status == "REASSESS":
            logger.warning("ALERT_NO_EDGE_DETECTED", sprt=snapshot.get("sprt_log_lambda"))
        if status == "EDGE_CONFIRMED":
            logger.info("ALERT_EDGE_CONFIRMED", sprt=snapshot.get("sprt_log_lambda"))
        sep = snapshot.get("lgbm_separation", 0)
        if sep < 0.05 and snapshot.get("trades_total", 0) >= 20:
            logger.warning("ALERT_MODEL_NOT_DISCRIMINATING", separation=sep)
