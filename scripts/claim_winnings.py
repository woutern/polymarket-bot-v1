"""Auto-claim resolved winning positions from Polymarket CTF contracts.

Polymarket winnings are held as conditional tokens in the CTF Exchange
on Polygon. This script redeems all winning positions for the funder wallet.

Usage:
    uv run python scripts/claim_winnings.py
    uv run python scripts/claim_winnings.py --dry-run
"""

from __future__ import annotations

import argparse
import sys
sys.path.insert(0, "src")

import httpx
from web3 import Web3

from polybot.config import Settings

# CTF Exchange contract on Polygon (Polymarket's binary markets)
CTF_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"

# Neg-Risk CTF Exchange (used by 5-min crypto markets)
NEG_RISK_CTF_ADDRESS = "0xC5d563A36AE78145C45a50134d48A1215220f80a"

# Bridged USDC on Polygon
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

# ABI for redeemPositions
REDEEM_ABI = [
    {
        "name": "redeemPositions",
        "type": "function",
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "outputs": [],
        "stateMutability": "nonpayable",
    },
    {
        "name": "balanceOfBatch",
        "type": "function",
        "inputs": [
            {"name": "accounts", "type": "address[]"},
            {"name": "ids", "type": "uint256[]"},
        ],
        "outputs": [{"name": "", "type": "uint256[]"}],
        "stateMutability": "view",
    },
]

POLYGON_RPC = "https://polygon-rpc.com"


def get_claimable_positions(funder_address: str) -> list[dict]:
    """Query Polymarket data-api for positions that are redeemable."""
    try:
        resp = httpx.get(
            "https://data-api.polymarket.com/positions",
            params={"user": funder_address, "sizeThreshold": "0.01"},
            timeout=10.0,
        )
        if resp.status_code != 200:
            print(f"data-api returned {resp.status_code}")
            return []
        positions = resp.json()
        if isinstance(positions, list):
            return positions
        return []
    except Exception as e:
        print(f"Failed to get positions: {e}")
        return []


def get_resolved_markets(condition_ids: list[str]) -> dict[str, bool]:
    """Check which markets resolved and which side won. Returns {condition_id: yes_won}."""
    resolved = {}
    for cid in condition_ids:
        try:
            resp = httpx.get(
                "https://gamma-api.polymarket.com/markets",
                params={"conditionId": cid},
                timeout=10.0,
            )
            if resp.status_code != 200:
                continue
            markets = resp.json()
            if not markets:
                continue
            m = markets[0]
            prices = m.get("outcomePrices", [])
            if len(prices) >= 2:
                yes_price = float(prices[0])
                if yes_price >= 0.99:
                    resolved[cid] = True   # YES won
                elif yes_price <= 0.01:
                    resolved[cid] = False  # NO won
        except Exception:
            continue
    return resolved


def claim_all(settings: Settings, dry_run: bool = False) -> None:
    funder = settings.polymarket_funder
    private_key = settings.polymarket_private_key

    print(f"Checking claimable positions for {funder[:10]}...")
    positions = get_claimable_positions(funder)

    if not positions:
        print("No positions found on data-api.")
        return

    condition_ids = list({p.get("conditionId") for p in positions if p.get("conditionId")})
    print(f"Found {len(positions)} positions across {len(condition_ids)} markets.")

    resolved = get_resolved_markets(condition_ids)
    claimable = {cid: yes_won for cid, yes_won in resolved.items()}

    if not claimable:
        print("No resolved markets with claimable positions.")
        return

    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))
    account = w3.eth.account.from_key(private_key)

    for cid, yes_won in claimable.items():
        index_set = 1 if yes_won else 2  # YES=1, NO=2
        side = "YES" if yes_won else "NO"

        # Find position size for logging
        pos = next((p for p in positions if p.get("conditionId") == cid), {})
        size = pos.get("size", 0)
        market_slug = pos.get("market", {}).get("slug", cid[:12]) if isinstance(pos.get("market"), dict) else cid[:12]

        print(f"  {market_slug}: {side} won, position size={size:.4f}")

        if dry_run:
            print(f"  [DRY RUN] Would redeem conditionId={cid[:12]}... indexSet=[{index_set}]")
            continue

        try:
            ctf = w3.eth.contract(
                address=Web3.to_checksum_address(NEG_RISK_CTF_ADDRESS),
                abi=REDEEM_ABI,
            )
            nonce = w3.eth.get_transaction_count(account.address)
            gas_price = w3.eth.gas_price

            tx = ctf.functions.redeemPositions(
                Web3.to_checksum_address(USDC_ADDRESS),
                b"\x00" * 32,  # parentCollectionId = zero (top-level condition)
                bytes.fromhex(cid.replace("0x", "")),
                [index_set],
            ).build_transaction({
                "from": account.address,
                "nonce": nonce,
                "gas": 200_000,
                "gasPrice": gas_price,
                "chainId": 137,
            })

            signed = account.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.rawTransaction)
            print(f"  Claimed! tx={tx_hash.hex()}")

        except Exception as e:
            print(f"  Failed to claim {cid[:12]}...: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Print what would be claimed without executing")
    args = parser.parse_args()

    s = Settings()
    if not s.polymarket_funder:
        print("POLYMARKET_FUNDER not set in .env")
        sys.exit(1)

    claim_all(s, dry_run=args.dry_run)
