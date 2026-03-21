#!/usr/bin/env python3
"""Process Jon-Becker prediction market dataset on EC2.

Downloads data.tar.zst (~36GB), extracts 5m BTC/SOL/ETH markets + trades,
joins them, and uploads processed parquet to S3.

Run on EC2 r6i.xlarge with 250GB EBS in us-east-1.
"""

import subprocess
import sys
import os
import json
import time
from datetime import datetime

S3_BUCKET = "s3://polymarket-bot-training-data-688567279867/jon-becker"
DATA_URL = "https://s3.jbecker.dev/data.tar.zst"
WORK_DIR = "/tmp/jbecker"

def run(cmd, **kwargs):
    print(f">>> {cmd}", flush=True)
    return subprocess.run(cmd, shell=True, check=True, **kwargs)

def main():
    start = time.time()
    print(f"=== Jon-Becker dataset processing started at {datetime.utcnow().isoformat()} ===", flush=True)

    # Install dependencies
    print("\n=== Installing dependencies ===", flush=True)
    run("pip3 install pyarrow pandas boto3 zstandard")

    # Download and extract
    os.makedirs(WORK_DIR, exist_ok=True)
    os.chdir(WORK_DIR)

    print("\n=== Downloading data.tar.zst (~36GB) ===", flush=True)
    if not os.path.exists(f"{WORK_DIR}/data.tar.zst"):
        run(f"curl -L -o {WORK_DIR}/data.tar.zst {DATA_URL}")
    else:
        print("Already downloaded, skipping", flush=True)

    print("\n=== Extracting ===", flush=True)
    if not os.path.exists(f"{WORK_DIR}/data/polymarket/markets"):
        run(f"cd {WORK_DIR} && zstd -d data.tar.zst -o data.tar --memory=2048MB 2>/dev/null || zstd -d data.tar.zst -o data.tar")
        run(f"cd {WORK_DIR} && tar xf data.tar")
        # Remove tar files to free space
        run(f"rm -f {WORK_DIR}/data.tar.zst {WORK_DIR}/data.tar")
    else:
        print("Already extracted, skipping", flush=True)

    print("\n=== Processing 5m markets ===", flush=True)
    import pyarrow.parquet as pq
    import pyarrow as pa
    import pandas as pd
    import glob

    # Step 1: Find all 5m markets and their token IDs
    market_files = sorted(glob.glob(f"{WORK_DIR}/data/polymarket/markets/*.parquet"))
    print(f"Found {len(market_files)} market files", flush=True)

    markets_5m = []
    for f in market_files:
        t = pq.read_table(f)
        for i in range(len(t)):
            slug = t.column('slug')[i].as_py() or ''
            if '-5m-' not in slug.lower():
                continue
            asset = None
            sl = slug.lower()
            if 'btc' in sl: asset = 'BTC'
            elif 'sol' in sl: asset = 'SOL'
            elif 'eth' in sl: asset = 'ETH'
            if not asset:
                continue

            tokens_str = t.column('clob_token_ids')[i].as_py() or '[]'
            try:
                tokens = json.loads(tokens_str)
            except:
                continue

            # Extract window timestamp from slug (e.g., btc-updown-5m-1766038500)
            parts = slug.split('-')
            window_ts = None
            for p in parts:
                if p.isdigit() and len(p) >= 10:
                    window_ts = int(p)
                    break

            outcomes_str = t.column('outcome_prices')[i].as_py() or '[]'
            try:
                outcome_prices = json.loads(outcomes_str)
            except:
                outcome_prices = []

            markets_5m.append({
                'slug': slug,
                'asset': asset,
                'condition_id': t.column('condition_id')[i].as_py(),
                'question': t.column('question')[i].as_py(),
                'volume': t.column('volume')[i].as_py(),
                'liquidity': t.column('liquidity')[i].as_py(),
                'end_date': str(t.column('end_date')[i].as_py()),
                'created_at': str(t.column('created_at')[i].as_py()),
                'active': t.column('active')[i].as_py(),
                'closed': t.column('closed')[i].as_py(),
                'yes_token': tokens[0] if len(tokens) > 0 else '',
                'no_token': tokens[1] if len(tokens) > 1 else '',
                'outcome_prices': outcomes_str,
                'yes_price': float(outcome_prices[0]) if len(outcome_prices) > 0 else None,
                'no_price': float(outcome_prices[1]) if len(outcome_prices) > 1 else None,
                'window_ts': window_ts,
            })

    print(f"Found {len(markets_5m)} 5-minute markets", flush=True)
    markets_df = pd.DataFrame(markets_5m)
    print(f"  BTC: {len(markets_df[markets_df.asset=='BTC']):,}", flush=True)
    print(f"  ETH: {len(markets_df[markets_df.asset=='ETH']):,}", flush=True)
    print(f"  SOL: {len(markets_df[markets_df.asset=='SOL']):,}", flush=True)

    # Build token → market lookup
    token_to_market = {}
    for m in markets_5m:
        if m['yes_token']:
            token_to_market[m['yes_token']] = (m['slug'], 'YES', m['asset'], m['window_ts'])
        if m['no_token']:
            token_to_market[m['no_token']] = (m['slug'], 'NO', m['asset'], m['window_ts'])

    print(f"Token lookup built: {len(token_to_market):,} tokens", flush=True)

    # Step 2: Scan trades for matching token IDs
    trade_dirs = ['trades', 'legacy_trades']
    matched_trades = []

    for tdir in trade_dirs:
        trade_files = sorted(glob.glob(f"{WORK_DIR}/data/polymarket/{tdir}/*.parquet"))
        print(f"\nScanning {len(trade_files)} files in {tdir}/", flush=True)

        for fi, f in enumerate(trade_files):
            if fi % 50 == 0:
                print(f"  Processing {fi}/{len(trade_files)} ({len(matched_trades):,} matches so far)", flush=True)

            try:
                t = pq.read_table(f)
            except Exception as e:
                print(f"  Error reading {f}: {e}", flush=True)
                continue

            # Check which columns contain token IDs
            cols = t.column_names
            maker_col = 'maker_asset_id' if 'maker_asset_id' in cols else None
            taker_col = 'taker_asset_id' if 'taker_asset_id' in cols else None

            if not maker_col and not taker_col:
                continue

            for i in range(len(t)):
                maker_token = str(t.column(maker_col)[i].as_py()) if maker_col else ''
                taker_token = str(t.column(taker_col)[i].as_py()) if taker_col else ''

                match = token_to_market.get(maker_token) or token_to_market.get(taker_token)
                if not match:
                    continue

                slug, side, asset, window_ts = match

                # Extract trade details
                row = {'slug': slug, 'side': side, 'asset': asset, 'window_ts': window_ts}
                for col in cols:
                    try:
                        row[col] = t.column(col)[i].as_py()
                    except:
                        pass

                # Calculate seconds into window (if we have block timestamp)
                if window_ts and 'block_number' in row:
                    row['block_number'] = int(row.get('block_number', 0))

                matched_trades.append(row)

    print(f"\n=== Total matched trades: {len(matched_trades):,} ===", flush=True)

    if matched_trades:
        trades_df = pd.DataFrame(matched_trades)
        print(f"Trade columns: {list(trades_df.columns)}", flush=True)
        print(f"By asset:", flush=True)
        print(trades_df.groupby('asset').size(), flush=True)

        # Step 3: Save to parquet
        os.makedirs(f"{WORK_DIR}/output", exist_ok=True)

        markets_df.to_parquet(f"{WORK_DIR}/output/markets_5m.parquet", index=False)
        trades_df.to_parquet(f"{WORK_DIR}/output/trades_5m.parquet", index=False)

        # Also save per-asset
        for asset in ['BTC', 'ETH', 'SOL']:
            m = markets_df[markets_df.asset == asset]
            m.to_parquet(f"{WORK_DIR}/output/markets_5m_{asset.lower()}.parquet", index=False)
            t = trades_df[trades_df.asset == asset]
            t.to_parquet(f"{WORK_DIR}/output/trades_5m_{asset.lower()}.parquet", index=False)

        # Step 4: Quick analysis
        print("\n=== Quick Analysis ===", flush=True)
        for asset in ['BTC', 'ETH', 'SOL']:
            am = markets_df[markets_df.asset == asset]
            at = trades_df[trades_df.asset == asset]
            print(f"\n{asset}:", flush=True)
            print(f"  Markets: {len(am):,}", flush=True)
            print(f"  Trades: {len(at):,}", flush=True)
            if len(am) > 0:
                print(f"  Avg volume: ${am.volume.mean():,.0f}", flush=True)
                print(f"  Date range: {am.end_date.min()[:10]} to {am.end_date.max()[:10]}", flush=True)
    else:
        # Still save markets even if no trades matched
        os.makedirs(f"{WORK_DIR}/output", exist_ok=True)
        markets_df.to_parquet(f"{WORK_DIR}/output/markets_5m.parquet", index=False)
        for asset in ['BTC', 'ETH', 'SOL']:
            m = markets_df[markets_df.asset == asset]
            m.to_parquet(f"{WORK_DIR}/output/markets_5m_{asset.lower()}.parquet", index=False)
        print("No trades matched — uploading markets only", flush=True)

    # Step 5: Upload to S3
    print(f"\n=== Uploading to {S3_BUCKET} ===", flush=True)
    run(f"aws s3 sync {WORK_DIR}/output/ {S3_BUCKET}/ --quiet")

    elapsed = time.time() - start
    print(f"\n=== Done in {elapsed/60:.1f} minutes ===", flush=True)
    print(f"Data at: {S3_BUCKET}/", flush=True)

    # Signal completion
    run(f"aws s3 cp - {S3_BUCKET}/_DONE <<< 'completed at {datetime.utcnow().isoformat()}'")


if __name__ == "__main__":
    main()
