"""Derive Polymarket L2 API credentials from your L1 private key.

For proxy/Gnosis-Safe wallets (standard Polymarket web accounts):
  signature_type=2 (GNOSIS_SAFE), funder=<proxy wallet address from polymarket.com/settings>

Run this once when credentials change. Paste output into .env.
"""

import sys
sys.path.insert(0, "src")

from py_clob_client.client import ClobClient

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137


def main():
    import os
    from dotenv import load_dotenv
    load_dotenv()

    key = os.getenv("POLYMARKET_PRIVATE_KEY")
    funder = os.getenv("POLYMARKET_FUNDER", "").strip() or None

    if not key:
        print("POLYMARKET_PRIVATE_KEY not set in .env")
        return

    if funder:
        print(f"Deriving credentials for proxy wallet (sig_type=2, funder={funder})")
        client = ClobClient(
            host=HOST,
            chain_id=CHAIN_ID,
            key=key,
            signature_type=2,
            funder=funder,
        )
    else:
        print("No POLYMARKET_FUNDER set — deriving EOA credentials (sig_type=0)")
        client = ClobClient(host=HOST, chain_id=CHAIN_ID, key=key)

    creds = client.create_or_derive_api_creds()

    print(f"\n# Paste these into .env:")
    print(f"POLYMARKET_API_KEY={creds.api_key}")
    print(f"POLYMARKET_API_SECRET={creds.api_secret}")
    print(f"POLYMARKET_API_PASSPHRASE={creds.api_passphrase}")


if __name__ == "__main__":
    main()
