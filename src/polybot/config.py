"""Configuration loaded from environment / .env file."""

from pydantic_settings import BaseSettings

# Module-level constant — cannot be overridden by env var or Secrets Manager
HARDCODED_MAX_BET = 10.00


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    # Polymarket
    polymarket_private_key: str = ""
    polymarket_api_key: str = ""
    polymarket_api_secret: str = ""
    polymarket_api_passphrase: str = ""
    polymarket_chain_id: int = 137
    polymarket_funder: str = ""  # Proxy/funder wallet address (shown in Polymarket UI)

    # Trading
    mode: str = "paper"  # "paper" or "live"
    bankroll: float = 1000.0
    max_position_pct: float = 0.01
    daily_loss_cap_pct: float = 0.20  # Raised for late-entry 24hr test
    kelly_fraction: float = 0.25
    min_trade_usd: float = 1.0    # Floor — always bet at least this when there's edge (Polymarket min is $1)
    max_trade_usd: float = 8.00   # SOL-focused: dynamic Kelly up to $8
    min_ev_threshold: float = 0.08  # EV > 8% required — blocks low-confidence entries
    directional_entry_seconds: int = 120  # T-120s — Tier B primary entry
    directional_min_move_pct: float = 0.03  # default; overridden per-asset below
    max_market_price: float = 0.88  # Late-entry: allow up to $0.88
    scenario_c_enabled: bool = False  # Disable Scenario C order execution (keep scan for data)
    late_entry_seconds: int = 210   # Enter at T+3.5min (210s into 5min window)
    late_entry_min_ask: float = 0.50  # Follow whichever side is higher
    late_entry_max_ask: float = 0.78  # Don't buy fully-priced moves
    assets: str = "BTC,ETH,SOL"  # Comma-separated asset list
    # Enabled pairs — granular control over which asset×timeframe combos are active.
    # Default "" means all combinations of assets (5m only).
    # Set to e.g. "BTC_5m,ETH_5m" to enable only those pairs.
    pairs: str = ""
    # Watch-only pairs — tracked for window resolution + training-data collection,
    # but never used for live trading decisions.
    watch_pairs: str = ""

    # Early entry strategy (T+14-18s, independent from Scenario C)
    early_entry_enabled: bool = False
    early_entry_max_bet: float = 4.20       # Total budget per window (main + hedge)
    early_entry_lgbm_threshold: float = 0.62
    early_entry_max_ask: float = 0.55
    early_entry_min_ask: float = 0.40
    early_entry_use_limit: bool = True
    early_entry_limit_offset: float = 0.02  # post at best_bid + this
    early_entry_limit_wait_seconds: float = 8.0
    # DCA + hedge config
    early_entry_main_pct: float = 0.83      # 83% of budget on main ($3.50 of $4.20)
    early_entry_hedge_pct: float = 0.17     # 17% on hedge ($0.70 of $4.20)
    early_entry_dca_t1_pct: float = 0.70    # T+15s initial buy: 70% of main ($8.71)
    early_entry_dca_t2_pct: float = 0.18    # T+45s dip buy: 18% of main ($2.24)
    early_entry_dca_t3_pct: float = 0.12    # T+90s remainder: 12% of main ($1.49)
    early_entry_rotate_enabled: bool = False  # Stop-and-rotate: sell → buy back cheap on same side
    early_entry_rotate_max_ask: float = 0.25  # Only rotate/accumulate if ask < 25¢
    early_entry_cheap_buy_size: float = 2.00  # Size per cheap-side limit order
    early_entry_reprice_stale_after_seconds: float = 6.0  # recycle stale accum orders after 2 ticks
    early_entry_reprice_price_tolerance: float = 0.01  # keep orders within 1 tick of desired ladder

    # Per-asset move thresholds (T+2s-T+15s early entry with quality filters)
    min_move_btc_5m: float = 0.02   # BTC 5m: lowered to see more signals
    min_move_eth_5m: float = 0.02   # ETH 5m: same
    min_move_sol_5m: float = 0.015  # SOL 5m: lowered for more trades

    # Logging
    log_level: str = "INFO"

    # Coinbase WS (public, no auth needed)
    coinbase_ws_url: str = "wss://advanced-trade-ws.coinbase.com"
    coinbase_rest_url: str = "https://api.coinbase.com"

    # Polymarket WS
    polymarket_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polymarket_rest_url: str = "https://clob.polymarket.com"
    polymarket_gamma_url: str = "https://gamma-api.polymarket.com"

    @property
    def asset_list(self) -> list[str]:
        return [a.strip().upper() for a in self.assets.split(",") if a.strip()]

    def _parse_pairs(self, raw_pairs: str, *, default_all_5m: bool) -> list[tuple[str, int]]:
        if raw_pairs.strip():
            result = []
            for p in raw_pairs.split(","):
                p = p.strip()
                if not p:
                    continue
                p = p.replace(" ", "_").upper()
                parts = p.rsplit("_", 1)
                if len(parts) == 2:
                    asset = parts[0]
                    tf_key = parts[1].lower()
                    if tf_key == "5m":
                        result.append((asset, 300))
                    elif tf_key == "15m":
                        result.append((asset, 900))
                    elif tf_key == "1h":
                        result.append((asset, 3600))
                else:
                    result.append((p, 300))
            return result
        if default_all_5m:
            return [(a, 300) for a in self.asset_list]
        return []

    @property
    def enabled_pairs(self) -> list[tuple[str, int]]:
        """Return list of (asset, window_seconds) for all enabled pairs.

        Only 5m (300s) windows are supported.
        If `pairs` is empty, returns all assets with 5m windows.
        Otherwise, parses e.g. "BTC_5m,ETH_5m" into [("BTC", 300), ("ETH", 300)].
        """
        return self._parse_pairs(self.pairs, default_all_5m=True)

    @property
    def watch_pair_list(self) -> list[tuple[str, int]]:
        """Return pairs that should be tracked for data collection only."""
        return self._parse_pairs(self.watch_pairs, default_all_5m=False)

    def min_move_for(self, asset: str, window_seconds: int = 300) -> float:
        """Return the calibrated min_move_pct for a given asset (5m only)."""
        key = f"min_move_{asset.lower()}_5m"
        return getattr(self, key, self.directional_min_move_pct)

    def pair_config(self, asset: str, window_seconds: int = 300) -> dict:
        """Return the full strategy config for a specific pair (5m only)."""
        return {
            "pair": f"{asset} 5m",
            "asset": asset,
            "timeframe": "5m",
            "window_seconds": 300,
            "min_move_pct": self.min_move_for(asset),
            "min_ev_threshold": self.min_ev_threshold,
            "max_market_price": self.max_market_price,
            "entry_seconds": self.directional_entry_seconds,
            "kelly_fraction": self.kelly_fraction,
            "min_trade_usd": self.min_trade_usd,
            "max_trade_usd": self.max_trade_usd,
        }
