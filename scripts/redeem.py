"""Redeem resolved winning positions via Gnosis Safe execTransaction.

Works with proxy wallets — executes redeemPositions through the Safe,
not directly from the EOA.

Usage:
    uv run python scripts/redeem.py
    uv run python scripts/redeem.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
import time

sys.path.insert(0, "src")

import httpx
from web3 import Web3
from eth_account import Account

from polybot.config import Settings

NEG_RISK_CTF = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ZERO_ADDR = "0x" + "0" * 40
POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"

SAFE_ABI = json.loads(
    '[{"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},'
    '{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},'
    '{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},'
    '{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},'
    '{"name":"refundReceiver","type":"address"},{"name":"signatures","type":"bytes"}],'
    '"name":"execTransaction","outputs":[{"name":"success","type":"bool"}],'
    '"stateMutability":"payable","type":"function"},'
    '{"inputs":[],"name":"nonce","outputs":[{"type":"uint256"}],'
    '"stateMutability":"view","type":"function"},'
    '{"inputs":[{"name":"to","type":"address"},{"name":"value","type":"uint256"},'
    '{"name":"data","type":"bytes"},{"name":"operation","type":"uint8"},'
    '{"name":"safeTxGas","type":"uint256"},{"name":"baseGas","type":"uint256"},'
    '{"name":"gasPrice","type":"uint256"},{"name":"gasToken","type":"address"},'
    '{"name":"refundReceiver","type":"address"},{"name":"_nonce","type":"uint256"}],'
    '"name":"getTransactionHash","outputs":[{"type":"bytes32"}],'
    '"stateMutability":"view","type":"function"}]'
)

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
    }
]


def redeem_all(settings: Settings, dry_run: bool = False):
    proxy = Web3.to_checksum_address(settings.polymarket_funder)
    eoa = Account.from_key(settings.polymarket_private_key)
    w3 = Web3(Web3.HTTPProvider(POLYGON_RPC))

    print(f"Proxy (Safe): {proxy}")
    print(f"EOA (owner):  {eoa.address}")

    # Get claimable positions
    resp = httpx.get(
        "https://data-api.polymarket.com/positions",
        params={"user": proxy.lower(), "sizeThreshold": "0.01"},
        timeout=10,
    )
    positions = resp.json()
    claimable = [p for p in positions if p.get("currentValue", 0) > 0.5]

    if not claimable:
        print("No claimable positions.")
        return

    print(f"\nClaimable: {len(claimable)} positions")
    total_value = sum(p.get("currentValue", 0) for p in claimable)
    print(f"Total value: ${total_value:.2f}")

    safe = w3.eth.contract(address=proxy, abi=SAFE_ABI)
    ctf = w3.eth.contract(
        address=Web3.to_checksum_address(NEG_RISK_CTF), abi=REDEEM_ABI
    )

    for p in claimable:
        cid = p.get("conditionId", "")
        cv = p.get("currentValue", 0)
        outcome = p.get("outcome", "")

        if not cid.startswith("0x") or len(cid) < 66:
            print(f"  SKIP: invalid conditionId {cid[:20]}")
            continue

        print(f"\n  {outcome} ${cv:.2f} cid={cid[:16]}...")

        if dry_run:
            print(f"  [DRY RUN] Would redeem")
            continue

        try:
            cid_bytes = bytes.fromhex(cid[2:])
            call_data = ctf.encode_abi(
                "redeemPositions",
                args=[
                    Web3.to_checksum_address(USDC),
                    b"\x00" * 32,
                    cid_bytes,
                    [1, 2],
                ],
            )

            safe_nonce = safe.functions.nonce().call()
            tx_hash_safe = safe.functions.getTransactionHash(
                Web3.to_checksum_address(NEG_RISK_CTF),
                0,
                bytes.fromhex(call_data[2:]),
                0, 0, 0, 0,
                Web3.to_checksum_address(ZERO_ADDR),
                Web3.to_checksum_address(ZERO_ADDR),
                safe_nonce,
            ).call()

            sig = eoa.signHash(tx_hash_safe)
            signature = (
                sig.r.to_bytes(32, "big")
                + sig.s.to_bytes(32, "big")
                + bytes([sig.v])
            )

            tx = safe.functions.execTransaction(
                Web3.to_checksum_address(NEG_RISK_CTF),
                0,
                bytes.fromhex(call_data[2:]),
                0, 0, 0, 0,
                Web3.to_checksum_address(ZERO_ADDR),
                Web3.to_checksum_address(ZERO_ADDR),
                signature,
            ).build_transaction(
                {
                    "from": eoa.address,
                    "nonce": w3.eth.get_transaction_count(eoa.address),
                    "gas": 300_000,
                    "gasPrice": w3.eth.gas_price,
                    "chainId": 137,
                }
            )

            signed = eoa.sign_transaction(tx)
            tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
            print(f"  Sent tx: {tx_hash.hex()}")
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            print(f"  REDEEMED! status={receipt.status} gas={receipt.gasUsed}")

            # Wait for nonce to update before next tx
            time.sleep(2)

        except Exception as e:
            print(f"  FAILED: {e}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    s = Settings()
    if not s.polymarket_funder:
        print("POLYMARKET_FUNDER not set")
        sys.exit(1)

    redeem_all(s, dry_run=args.dry_run)
