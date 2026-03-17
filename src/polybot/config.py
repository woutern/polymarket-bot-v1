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
    min_ev_threshold: float = 0.05
    directional_entry_seconds: int = 60  # T-60s — enter before market reprices
    directional_min_move_pct: float = 0.08  # 0.08% — lower for more opportunities
    max_market_price: float = 0.85  # Don't buy if ask > 0.85 (market already priced in)
    assets: str = "BTC,ETH,SOL"  # Comma-separated asset list
    window_durations: str = "5m,15m"  # Comma-separated window durations

    # AI Signal
    ai_signal_enabled: bool = True
    ai_base_rate_weight: float = 0.6
    ai_signal_weight: float = 0.4
    ai_min_confidence: float = 0.6
    news_poll_interval: int = 30

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
