"""Configuration loaded from environment / .env file."""

from pydantic_settings import BaseSettings

# Module-level constant — cannot be overridden by env var or Secrets Manager
HARDCODED_MAX_BET = 8.00


class Settings(BaseSettings):
    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

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
    late_entry_seconds: int = 240   # Enter at T+4min (240s into 5min window)
    late_entry_min_ask: float = 0.50  # Follow whichever side is higher
    late_entry_max_ask: float = 0.78  # Don't buy fully-priced moves (EV negative above this)
    assets: str = "BTC,ETH,SOL"  # Comma-separated asset list
    # Enabled pairs — granular control over which asset×timeframe combos are active.
    # Default "" means all combinations of assets (5m only).
    # Set to e.g. "BTC_5m,ETH_5m" to enable only those pairs.
    pairs: str = ""

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

    @property
    def enabled_pairs(self) -> list[tuple[str, int]]:
        """Return list of (asset, window_seconds) for all enabled pairs.

        Only 5m (300s) windows are supported.
        If `pairs` is empty, returns all assets with 5m windows.
        Otherwise, parses e.g. "BTC_5m,ETH_5m" into [("BTC", 300), ("ETH", 300)].
        """
        if self.pairs.strip():
            result = []
            for p in self.pairs.split(","):
                p = p.strip()
                if not p:
                    continue
                # Accept "BTC_5m", "BTC_5M", "btc_5m", "BTC 5m"
                p = p.replace(" ", "_").upper()
                parts = p.rsplit("_", 1)
                if len(parts) == 2:
                    asset = parts[0]
                    tf_key = parts[1].lower()
                    if tf_key == "5m":
                        result.append((asset, 300))
                else:
                    # Bare asset name, assume 5m
                    result.append((p, 300))
            return result
        # Default: all assets × 5m
        return [(a, 300) for a in self.asset_list]

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
