"""Bayesian probability updater.

Chains real-time Coinbase price ticks as evidence to update
P(UP) in real time within each 5-min window.
"""

from __future__ import annotations

import math

from polybot.strategy.base_rate import BaseRateTable


class BayesianUpdater:
    """Updates P(UP) using each new price tick as evidence.

    Prior: base rate from historical data at window start.
    Likelihood: P(observe this tick | UP) vs P(observe this tick | DOWN).

    We model the likelihood ratio based on the magnitude and direction
    of each tick relative to the open price.
    """

    def __init__(self, base_rate_table: BaseRateTable):
        self.base_rate_table = base_rate_table
        self.log_odds: float = 0.0
        self._open_price: float = 0.0
        self._initialized: bool = False

    def reset(self, open_price: float, prior: float = 0.5):
        """Reset for a new window."""
        self._open_price = open_price
        # Convert prior to log-odds
        prior = max(0.001, min(0.999, prior))
        self.log_odds = math.log(prior / (1 - prior))
        self._initialized = True

    def update(self, current_price: float, seconds_remaining: float) -> float:
        """Update with a new price tick. Returns updated P(UP).

        The evidence strength is the base rate lookup for the current
        state (pct_move, seconds_remaining).
        """
        if not self._initialized or self._open_price <= 0:
            return 0.5

        pct_move = (current_price - self._open_price) / self._open_price * 100
        secs = max(5, int(seconds_remaining))

        # Look up the historical base rate for this state
        p_up_given_state = self.base_rate_table.lookup(pct_move, secs)

        # Convert to log-odds and use as our updated belief
        # We blend the base-rate evidence with our running log-odds
        # using a weighted update (0.3 = evidence weight per tick)
        p_up_given_state = max(0.001, min(0.999, p_up_given_state))
        evidence_log_odds = math.log(p_up_given_state / (1 - p_up_given_state))

        # Exponential moving blend toward the base rate evidence
        alpha = 0.3
        self.log_odds = (1 - alpha) * self.log_odds + alpha * evidence_log_odds

        return self.probability

    @property
    def probability(self) -> float:
        """Current P(UP) estimate."""
        return 1.0 / (1.0 + math.exp(-self.log_odds))
