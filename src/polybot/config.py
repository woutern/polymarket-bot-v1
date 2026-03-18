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
    min_ev_threshold: float = 0.05
    directional_entry_seconds: int = 60  # T-60s — enter before market reprices
    directional_min_move_pct: float = 0.08  # default; overridden per-asset below
    max_market_price: float = 0.75  # Don't buy if ask > 0.75 (market already priced in)
    assets: str = "BTC,ETH,SOL"  # Comma-separated asset list
    window_durations: str = "5m,15m"  # Comma-separated window durations

    # Per-asset move thresholds (research: SOL is ~1.8x more volatile than BTC)
    # Higher threshold = higher win rate but fewer trades
    min_move_btc_5m: float = 0.08   # BTC 5-min: 96.4% WR
    min_move_eth_5m: float = 0.10   # ETH 5-min: slightly more volatile
    min_move_sol_5m: float = 0.14   # SOL 5-min: ~1.8x BTC vol
    min_move_btc_15m: float = 0.12  # 15-min: market has more time to price in → stricter
    min_move_eth_15m: float = 0.14
    min_move_sol_15m: float = 0.18

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

    def min_move_for(self, asset: str, window_seconds: int) -> float:
        """Return the calibrated min_move_pct for a given asset × window size."""
        tf = "15m" if window_seconds == 900 else "5m"
        key = f"min_move_{asset.lower()}_{tf}"
        return getattr(self, key, self.directional_min_move_pct)
