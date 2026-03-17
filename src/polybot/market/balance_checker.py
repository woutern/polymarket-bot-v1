"""Balance checker — queries CLOB, data-api, and on-chain Polygon USDC."""

from __future__ import annotations

import structlog
import httpx

logger = structlog.get_logger()

# Polygon USDC contract (bridged USDC — what Polymarket uses)
USDC_CONTRACT = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
# Public Polygon RPCs (fallback chain)
POLYGON_RPCS = [
    "https://rpc-mainnet.matic.quiknode.pro",
    "https://polygon.llamarpc.com",
    "https://rpc.ankr.com/polygon",
]
# ERC-20 balanceOf(address) selector
BALANCE_OF_SELECTOR = "0x70a08231"


def _encode_balance_of(address: str) -> str:
    """Encode balanceOf(address) call data."""
    # Pad address to 32 bytes
    addr = address.lower().replace("0x", "").zfill(64)
    return BALANCE_OF_SELECTOR + addr


class BalanceChecker:
    """Check wallet balances across CLOB API, data API, and Polygon on-chain."""

    async def check(self, address: str) -> dict:
        """Return balance dict with clob_usdc, polymarket_value, polygon_usdc."""
        results = {"clob_usdc": 0.0, "polymarket_value": 0.0, "polygon_usdc": 0.0}

        async with httpx.AsyncClient(timeout=10.0) as client:
            # 1. CLOB balance (py-clob-client compatible endpoint)
            try:
                resp = await client.get(
                    f"https://clob.polymarket.com/balance",
                    params={"address": address},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # Response may be a float/string or {"balance": ...}
                    if isinstance(data, (int, float)):
                        results["clob_usdc"] = float(data)
                    elif isinstance(data, dict):
                        results["clob_usdc"] = float(
                            data.get("balance", data.get("usdc", 0)) or 0
                        )
            except Exception as e:
                logger.warning("balance_clob_failed", error=str(e))

            # 2. Polymarket data-api — portfolio value (open positions + cash)
            try:
                resp = await client.get(
                    "https://data-api.polymarket.com/value",
                    params={"user": address},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    # Returns [{"user": "...", "value": 0}]
                    if isinstance(data, list) and data:
                        results["polymarket_value"] = float(data[0].get("value", 0) or 0)
                    elif isinstance(data, dict):
                        results["polymarket_value"] = float(data.get("value", 0) or 0)
                    elif isinstance(data, (int, float)):
                        results["polymarket_value"] = float(data)
            except Exception as e:
                logger.warning("balance_data_api_failed", error=str(e))

            # 3. On-chain Polygon USDC balance (bridged USDC = what Polymarket uses)
            call_data = _encode_balance_of(address)
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_call",
                "params": [{"to": USDC_CONTRACT, "data": call_data}, "latest"],
            }
            for rpc in POLYGON_RPCS:
                try:
                    resp = await client.post(rpc, json=payload, timeout=5.0)
                    if resp.status_code == 200:
                        rpc_data = resp.json()
                        hex_val = rpc_data.get("result", "0x0") or "0x0"
                        raw = int(hex_val, 16)
                        results["polygon_usdc"] = raw / 1_000_000
                        break
                except Exception as e:
                    logger.debug("rpc_failed", rpc=rpc, error=str(e))

        logger.info(
            "wallet_balance_checked",
            address=address[:10] + "...",
            **{k: round(v, 4) for k, v in results.items()},
        )
        return results
