"""Backfill Coinbase 1-min candles for Jon-Becker 5m windows.

Downloads BTC/ETH/SOL 1-minute candles from Coinbase REST API
for Dec 2025 - Jan 2026 (where 99% of Jon-Becker data is).
Saves to S3 as parquet for joining with market data.

Usage:
    uv run python scripts/backfill_coinbase_candles.py
"""

from __future__ import annotations

import io
import os
import time
from datetime import datetime, timezone, timedelta

import boto3
import httpx
import pandas as pd

S3_BUCKET = "polymarket-bot-training-data-688567279867"
S3_PREFIX = "jon-becker/coinbase"
PROFILE = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None

COINBASE_URL = "https://api.exchange.coinbase.com/products/{pair}/candles"
PAIRS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}

# Date range: Dec 1 2025 to Feb 10 2026 (covers all Jon-Becker 5m data)
START = datetime(2025, 12, 1, tzinfo=timezone.utc)
END = datetime(2026, 2, 10, tzinfo=timezone.utc)


def fetch_candles(client: httpx.Client, pair: str, start: datetime, end: datetime) -> list[dict]:
    """Fetch 1-min candles from Coinbase. Returns up to 300 candles."""
    url = COINBASE_URL.format(pair=pair)
    params = {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "granularity": 60,  # 1 minute
    }
    r = client.get(url, params=params)
    if r.status_code != 200:
        return []

    # Coinbase returns: [[timestamp, low, high, open, close, volume], ...]
    rows = []
    for candle in r.json():
        if len(candle) >= 6:
            rows.append({
                "timestamp": int(candle[0]),
                "low": float(candle[1]),
                "high": float(candle[2]),
                "open": float(candle[3]),
                "close": float(candle[4]),
                "volume": float(candle[5]),
            })
    return rows


def backfill_asset(asset: str) -> pd.DataFrame:
    """Backfill all 1-min candles for one asset."""
    pair = PAIRS[asset]
    print(f"\n{'='*50}")
    print(f"Backfilling {asset} ({pair})")
    print(f"Range: {START.date()} to {END.date()}")
    print(f"{'='*50}")

    all_candles = []
    current = START
    chunk = timedelta(hours=5)  # 300 candles at 1min = 5 hours
    total_calls = 0
    errors = 0

    with httpx.Client(timeout=15) as client:
        while current < END:
            chunk_end = min(current + chunk, END)
            candles = fetch_candles(client, pair, current, chunk_end)
            total_calls += 1

            if candles:
                all_candles.extend(candles)
            else:
                errors += 1

            if total_calls % 50 == 0:
                print(f"  {asset}: {total_calls} calls, {len(all_candles):,} candles, {errors} errors, at {current.strftime('%Y-%m-%d %H:%M')}")

            current = chunk_end
            time.sleep(0.15)  # Rate limit: ~6 req/sec

    print(f"  {asset} done: {total_calls} calls, {len(all_candles):,} candles, {errors} errors")

    df = pd.DataFrame(all_candles)
    if len(df) > 0:
        df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
        df["asset"] = asset
        df["dt"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        print(f"  Unique candles: {len(df):,}")
        print(f"  Range: {df.dt.min()} to {df.dt.max()}")

    return df


def compute_window_features(candles: pd.DataFrame, window_ts: int) -> dict | None:
    """Compute price features for one 5m window from 1-min candles.

    window_ts = end of window. open = window_ts - 300.
    """
    open_ts = window_ts - 300
    # Get candles within this window
    mask = (candles["timestamp"] >= open_ts) & (candles["timestamp"] < window_ts)
    wc = candles[mask].sort_values("timestamp")

    if len(wc) < 3:
        return None

    open_price = wc.iloc[0]["open"]
    if open_price <= 0:
        return None

    # move_pct_15s: price change in first ~15s (use first candle close vs open)
    first_close = wc.iloc[0]["close"]
    move_pct_15s = (first_close - open_price) / open_price * 100

    # realized_vol_5m: std of 1-min returns
    closes = wc["close"].values
    if len(closes) >= 2:
        returns = [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1] > 0]
        realized_vol_5m = float(pd.Series(returns).std()) if returns else 0.0
    else:
        realized_vol_5m = 0.0

    # vol_ratio: current window volume / avg of prior 5 windows
    # (We'll compute this in the join phase since we need multiple windows)
    total_vol = wc["volume"].sum()

    # body_ratio: |close - open| / (high - low) of the window
    window_high = wc["high"].max()
    window_low = wc["low"].min()
    window_close = wc.iloc[-1]["close"]
    body = abs(window_close - open_price)
    wick = window_high - window_low
    body_ratio = body / wick if wick > 0 else 0.0

    # Price at various offsets for entry timing analysis
    close_price = wc.iloc[-1]["close"]
    went_up = close_price >= open_price

    # Price at T+180s, T+210s, T+240s (for optimal entry analysis)
    prices_at = {}
    for offset in [60, 120, 180, 210, 240, 270]:
        target_ts = open_ts + offset
        # Find closest candle
        closest = wc.iloc[(wc["timestamp"] - target_ts).abs().argsort()[:1]]
        if len(closest) > 0:
            prices_at[f"price_t{offset}"] = float(closest.iloc[0]["close"])
            prices_at[f"move_t{offset}"] = (float(closest.iloc[0]["close"]) - open_price) / open_price * 100

    return {
        "window_ts": window_ts,
        "open_ts": open_ts,
        "open_price": open_price,
        "close_price": close_price,
        "went_up": int(went_up),
        "move_pct_15s": move_pct_15s,
        "realized_vol_5m": realized_vol_5m,
        "body_ratio": body_ratio,
        "window_volume": total_vol,
        "window_high": window_high,
        "window_low": window_low,
        **prices_at,
    }


def main():
    start_time = time.time()

    # Step 1: Download candles
    all_dfs = []
    for asset in ["BTC", "ETH", "SOL"]:
        df = backfill_asset(asset)
        if len(df) > 0:
            all_dfs.append(df)

    if not all_dfs:
        print("No candles downloaded!")
        return

    candles = pd.concat(all_dfs, ignore_index=True)
    print(f"\nTotal candles: {len(candles):,}")

    # Step 2: Upload raw candles to S3
    session = boto3.Session(profile_name=PROFILE)
    s3 = session.client("s3")

    for asset in ["BTC", "ETH", "SOL"]:
        ac = candles[candles.asset == asset]
        if len(ac) == 0:
            continue
        buf = io.BytesIO()
        ac.to_parquet(buf, index=False)
        buf.seek(0)
        key = f"{S3_PREFIX}/candles_{asset.lower()}.parquet"
        s3.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.read())
        print(f"  Uploaded {key} ({len(ac):,} candles)")

    # Step 3: Compute window features from Jon-Becker market data
    print("\nLoading Jon-Becker markets for feature computation...")
    try:
        import pyarrow.parquet as pq
        import glob

        # Try S3 first (EC2 path), then local (dev path)
        files = []
        try:
            obj = s3.get_object(Bucket=S3_BUCKET, Key="jon-becker/markets_5m.parquet")
            markets = pd.read_parquet(io.BytesIO(obj["Body"].read()))
        except Exception:
            files = sorted(glob.glob("/tmp/jbecker-markets/data/polymarket/markets/*.parquet"))
            if files:
                dfs = []
                for f in files:
                    t = pq.read_table(f).to_pandas()
                    mask = t["slug"].str.contains("-5m-", na=False)
                    dfs.append(t[mask])
                markets = pd.concat(dfs, ignore_index=True)
            else:
                raise FileNotFoundError("No markets data found on S3 or locally")

        print(f"  {len(markets):,} 5m markets loaded")

        # Compute features for each window
        all_features = []
        for asset in ["BTC", "ETH", "SOL"]:
            asset_candles = candles[candles.asset == asset].copy()
            if len(asset_candles) == 0:
                continue

            asset_lower = asset.lower()
            asset_markets = markets[markets["slug"].str.contains(asset_lower, na=False)].copy()

            # Extract window_ts from slug
            def get_window_ts(slug):
                parts = str(slug).split("-")
                for p in parts:
                    if p.isdigit() and len(p) >= 10:
                        return int(p)
                return None

            asset_markets["window_ts"] = asset_markets["slug"].apply(get_window_ts)
            asset_markets = asset_markets.dropna(subset=["window_ts"])

            print(f"\n  Computing {asset} features for {len(asset_markets):,} windows...")
            computed = 0
            for _, row in asset_markets.iterrows():
                wts = int(row["window_ts"])
                feats = compute_window_features(asset_candles, wts)
                if feats:
                    feats["asset"] = asset
                    feats["slug"] = row["slug"]
                    feats["condition_id"] = row.get("condition_id", "")
                    feats["market_volume"] = row.get("volume", 0)
                    feats["market_liquidity"] = row.get("liquidity", 0)
                    all_features.append(feats)
                    computed += 1

            print(f"  {asset}: {computed:,} windows with features")

        if all_features:
            features_df = pd.DataFrame(all_features)

            # Add time features
            features_df["dt"] = pd.to_datetime(features_df["open_ts"], unit="s", utc=True)
            features_df["hour"] = features_df["dt"].dt.hour
            features_df["dow"] = features_df["dt"].dt.dayofweek

            # Upload enriched features
            buf = io.BytesIO()
            features_df.to_parquet(buf, index=False)
            buf.seek(0)
            key = "jon-becker/enriched_5m_features.parquet"
            s3.put_object(Bucket=S3_BUCKET, Key=key, Body=buf.read())
            print(f"\n  Uploaded {key} ({len(features_df):,} enriched windows)")

            # Quick stats
            print(f"\n{'='*50}")
            print("ENRICHED DATASET SUMMARY")
            print(f"{'='*50}")
            print(f"Total windows with features: {len(features_df):,}")
            for asset in ["BTC", "ETH", "SOL"]:
                af = features_df[features_df.asset == asset]
                print(f"  {asset}: {len(af):,} windows, UP rate: {af.went_up.mean():.1%}")

    except Exception as e:
        print(f"  Feature computation skipped: {e}")

    elapsed = time.time() - start_time
    print(f"\nDone in {elapsed/60:.1f} minutes")


if __name__ == "__main__":
    main()
