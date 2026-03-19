"""Configuration loaded from environment / .env file."""

from pydantic_settings import BaseSettings


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
    daily_loss_cap_pct: float = 0.05
    kelly_fraction: float = 0.25
    min_trade_usd: float = 1.0    # Floor — always bet at least this when there's edge (Polymarket min is $1)
    max_trade_usd: float = 10.0   # Hard cap per trade
    min_ev_threshold: float = 0.08  # EV > 8% required — blocks low-confidence entries
    directional_entry_seconds: int = 120  # T-120s — Tier B primary entry
    directional_min_move_pct: float = 0.03  # default; overridden per-asset below
    max_market_price: float = 0.55  # Only enter when ask is genuinely cheap
    assets: str = "BTC,ETH,SOL"  # Comma-separated asset list
    window_durations: str = "5m,15m"  # Comma-separated window durations
    # Enabled pairs — granular control over which asset×timeframe combos are active.
    # Default "" means all combinations of assets × durations are enabled.
    # Set to e.g. "BTC_5m,ETH_5m,SOL_15m" to enable only those pairs.
    pairs: str = ""

    # Per-asset move thresholds (T+2s-T+15s early entry with quality filters)
    min_move_btc_5m: float = 0.02   # BTC 5m: lowered to see more signals
    min_move_eth_5m: float = 0.02   # ETH 5m: same
    min_move_sol_5m: float = 0.02   # SOL 5m: same
    min_move_btc_15m: float = 0.15  # 15m: high threshold needed — direction holds 74% at 0.15%
    min_move_eth_15m: float = 0.15  # 15m: 72.9% WR at 0.15% (backtested)
    min_move_sol_15m: float = 0.15  # 15m: 72.5% WR at 0.15% (backtested)

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
    def duration_list(self) -> list[int]:
        mapping = {"5m": 300, "15m": 900}
        return [mapping[d.strip()] for d in self.window_durations.split(",") if d.strip() in mapping]

    @property
    def enabled_pairs(self) -> list[tuple[str, int]]:
        """Return list of (asset, window_seconds) for all enabled pairs.

        If `pairs` is empty, returns all combinations of assets × durations.
        Otherwise, parses e.g. "BTC_5m,ETH_15m" into [("BTC", 300), ("ETH", 900)].
        """
        dur_map = {"5m": 300, "15m": 900}
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
                    if tf_key in dur_map:
                        result.append((asset, dur_map[tf_key]))
            return result
        # Default: all combinations
        return [(a, d) for d in self.duration_list for a in self.asset_list]

    def min_move_for(self, asset: str, window_seconds: int) -> float:
        """Return the calibrated min_move_pct for a given asset × window size."""
        tf = "15m" if window_seconds == 900 else "5m"
        key = f"min_move_{asset.lower()}_{tf}"
        return getattr(self, key, self.directional_min_move_pct)

    def pair_config(self, asset: str, window_seconds: int) -> dict:
        """Return the full strategy config for a specific pair."""
        tf = "15m" if window_seconds == 900 else "5m"
        return {
            "pair": f"{asset} {tf}",
            "asset": asset,
            "timeframe": tf,
            "window_seconds": window_seconds,
            "min_move_pct": self.min_move_for(asset, window_seconds),
            "min_ev_threshold": self.min_ev_threshold,
            "max_market_price": self.max_market_price,
            "entry_seconds": self.directional_entry_seconds,
            "kelly_fraction": self.kelly_fraction,
            "min_trade_usd": self.min_trade_usd,
            "max_trade_usd": self.max_trade_usd,
        }
