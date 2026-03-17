"""Setup Polymarket USDC allowances via the Relayer API.

This script approves the CTF Exchange to spend USDC from the proxy wallet,
which is required before the CLOB will accept orders.

Uses Polymarket's Relayer (gasless — no MATIC needed in proxy wallet).
"""

import json
import os
import sys

import requests
from dotenv import load_dotenv
from eth_account import Account
from eth_abi import encode
from eth_utils import keccak, to_bytes, to_checksum_address

load_dotenv()

RELAYER_URL = "https://relayer-v2.polymarket.com"
CLOB_URL = "https://clob.polymarket.com"

PROXY_WALLET = "0x5ca439d661c9b44337E91fC681ec4b006C473610"
SIGNER_ADDRESS = "0x7DfC1BDC5817ac0C6CbC8960A3FaBa261b440BDA"

# From py_clob_client config
CTF_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
USDC = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

RELAYER_API_KEY = "019cfe12-78c9-7859-82bf-eba141799a34"
PRIVATE_KEY = os.getenv("POLYMARKET_PRIVATE_KEY")

MAX_UINT256 = 2**256 - 1


def encode_approve(spender: str, amount: int) -> str:
    """Encode ERC-20 approve(spender, amount) calldata."""
    # approve(address,uint256) selector = 0x095ea7b3
    selector = bytes.fromhex("095ea7b3")
    params = encode(
        ["address", "uint256"],
        [to_checksum_address(spender), amount],
    )
    return "0x" + (selector + params).hex()


def encode_set_approval_for_all(operator: str, approved: bool) -> str:
    """Encode ERC-1155 setApprovalForAll(operator, approved) calldata."""
    # setApprovalForAll(address,bool) selector = 0xa22cb465
    selector = bytes.fromhex("a22cb465")
    params = encode(
        ["address", "bool"],
        [to_checksum_address(operator), approved],
    )
    return "0x" + (selector + params).hex()


def sign_proxy_transaction(private_key: str, to: str, data: str, nonce: int, proxy_wallet: str) -> str:
    """Sign a Polymarket PROXY-type relayer transaction.

    Polymarket PROXY transactions are signed as:
    keccak256(abi.encodePacked(to, data_bytes, nonce_bytes32, proxy_wallet))
    """
    to_bytes_val = bytes.fromhex(to_checksum_address(to)[2:].zfill(40))
    data_bytes = bytes.fromhex(data[2:]) if data.startswith("0x") else bytes.fromhex(data)
    nonce_bytes = nonce.to_bytes(32, "big")
    proxy_bytes = bytes.fromhex(to_checksum_address(proxy_wallet)[2:].zfill(40))

    msg_hash = keccak(to_bytes_val + data_bytes + nonce_bytes + proxy_bytes)
    account = Account.from_key(private_key)
    signed = Account._sign_hash(msg_hash, private_key)
    return "0x" + signed.signature.hex()


def relayer_headers():
    return {
        "RELAYER_API_KEY": RELAYER_API_KEY,
        "RELAYER_API_KEY_ADDRESS": SIGNER_ADDRESS,
        "Content-Type": "application/json",
    }


def submit_relayer_tx(to: str, data: str, nonce: int, label: str):
    """Submit a gasless transaction via the Polymarket Relayer."""
    signature = sign_proxy_transaction(PRIVATE_KEY, to, data, nonce, PROXY_WALLET)

    payload = {
        "from": SIGNER_ADDRESS,
        "to": to_checksum_address(to),
        "proxyWallet": PROXY_WALLET,
        "data": data,
        "nonce": str(nonce),
        "signature": signature,
        "type": "PROXY",
        "signatureParams": {
            "gasPrice": "0",
            "operation": "0",
            "safeTxnGas": "0",
            "baseGas": "0",
            "gasToken": "0x0000000000000000000000000000000000000000",
            "refundReceiver": "0x0000000000000000000000000000000000000000",
        },
    }

    print(f"\n[{label}]")
    print(f"  to: {to}")
    print(f"  data: {data[:66]}...")
    print(f"  nonce: {nonce}")

    resp = requests.post(
        f"{RELAYER_URL}/submit",
        json=payload,
        headers=relayer_headers(),
    )
    print(f"  status: {resp.status_code}")
    try:
        result = resp.json()
        print(f"  response: {json.dumps(result, indent=2)}")
        return result
    except Exception:
        print(f"  raw: {resp.text}")
        return None


def check_balance_allowance():
    """Check CLOB balance/allowance after setup."""
    sys.path.insert(0, "src")
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, AssetType

    creds = ApiCreds(
        api_key=os.getenv("POLYMARKET_API_KEY"),
        api_secret=os.getenv("POLYMARKET_API_SECRET"),
        api_passphrase=os.getenv("POLYMARKET_API_PASSPHRASE"),
    )
    client = ClobClient(
        host=CLOB_URL,
        chain_id=137,
        key=PRIVATE_KEY,
        creds=creds,
        signature_type=1,
        funder=PROXY_WALLET,
    )
    bal = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    print(f"\nCLOB balance/allowance after setup: {bal}")
    return bal


def main():
    print("Setting up Polymarket USDC allowances via Relayer...")
    print(f"  Proxy wallet: {PROXY_WALLET}")
    print(f"  Signer (MetaMask): {SIGNER_ADDRESS}")

    if not PRIVATE_KEY:
        print("ERROR: POLYMARKET_PRIVATE_KEY not set in .env")
        return

    # Transaction 1: Approve CTF Exchange to spend USDC from proxy wallet
    approve_data = encode_approve(CTF_EXCHANGE, MAX_UINT256)
    result1 = submit_relayer_tx(USDC, approve_data, 0, "USDC.approve(CTF_EXCHANGE, MAX)")

    # Transaction 2: Approve Neg Risk Exchange too
    approve_data2 = encode_approve(NEG_RISK_EXCHANGE, MAX_UINT256)
    result2 = submit_relayer_tx(USDC, approve_data2, 1, "USDC.approve(NEG_RISK_EXCHANGE, MAX)")

    print("\n--- Done submitting ---")
    if result1 or result2:
        print("Check status via txID if needed.")
        print("\nNow checking CLOB balance...")
        check_balance_allowance()


if __name__ == "__main__":
    main()
