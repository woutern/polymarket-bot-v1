"""Analyze Jon-Becker 5m market data from S3.

Reads processed parquet from S3, runs strategy analysis, and outputs
actionable insights for BTC/ETH/SOL 5-minute trading.

Usage:
    uv run python scripts/analyze_jbecker.py
"""

from __future__ import annotations

import io
import json
import os
import sys

import boto3
import pandas as pd
import pyarrow.parquet as pq

S3_BUCKET = "polymarket-bot-training-data-688567279867"
S3_PREFIX = "jon-becker"
PROFILE = "playground" if not os.getenv("AWS_EXECUTION_ENV") else None


def read_s3_parquet(key: str) -> pd.DataFrame:
    """Read a parquet file from S3 into a DataFrame."""
    session = boto3.Session(profile_name=PROFILE)
    s3 = session.client("s3")
    obj = s3.get_object(Bucket=S3_BUCKET, Key=key)
    return pd.read_parquet(io.BytesIO(obj["Body"].read()))


def load_data() -> tuple[pd.DataFrame, pd.DataFrame | None]:
    """Load markets and trades from S3."""
    print("Loading markets from S3...")
    markets = read_s3_parquet(f"{S3_PREFIX}/markets_5m.parquet")
    print(f"  {len(markets):,} markets loaded")

    trades = None
    try:
        print("Loading trades from S3...")
        trades = read_s3_parquet(f"{S3_PREFIX}/trades_5m.parquet")
        print(f"  {len(trades):,} trades loaded")
    except Exception as e:
        print(f"  No trades file: {e}")

    return markets, trades


def analyze_markets(markets: pd.DataFrame):
    """Analyze market metadata — volume, pricing, time patterns."""
    print("\n" + "=" * 80)
    print("MARKET ANALYSIS — Jon-Becker 5m Dataset")
    print("=" * 80)

    # Basic counts
    print(f"\nTotal 5m markets: {len(markets):,}")
    for asset in ["BTC", "ETH", "SOL"]:
        am = markets[markets.asset == asset]
        print(f"  {asset}: {len(am):,} markets")

    # Parse end_date for time analysis
    markets = markets.copy()
    markets["end_dt"] = pd.to_datetime(markets["end_date"], errors="coerce", utc=True)
    markets = markets.dropna(subset=["end_dt"])
    markets["hour"] = markets["end_dt"].dt.hour
    markets["dow"] = markets["end_dt"].dt.dayofweek  # 0=Mon, 6=Sun
    markets["date"] = markets["end_dt"].dt.date
    markets["is_weekend"] = markets["dow"] >= 5

    # Date range
    print(f"\nDate range: {markets.end_dt.min().strftime('%Y-%m-%d')} to {markets.end_dt.max().strftime('%Y-%m-%d')}")
    print(f"Total days: {(markets.end_dt.max() - markets.end_dt.min()).days}")

    # Volume analysis
    print("\n--- VOLUME ANALYSIS ---")
    for asset in ["BTC", "ETH", "SOL"]:
        am = markets[markets.asset == asset]
        print(f"\n{asset}:")
        print(f"  Mean volume:   ${am.volume.mean():>10,.0f}")
        print(f"  Median volume: ${am.volume.median():>10,.0f}")
        print(f"  P25 volume:    ${am.volume.quantile(0.25):>10,.0f}")
        print(f"  P75 volume:    ${am.volume.quantile(0.75):>10,.0f}")
        print(f"  Max volume:    ${am.volume.max():>10,.0f}")
        print(f"  Zero volume:   {(am.volume == 0).sum():,} ({(am.volume == 0).mean()*100:.1f}%)")

    # Price analysis (yes_price = implied probability of UP)
    print("\n--- YES PRICE DISTRIBUTION (implied P(up)) ---")
    for asset in ["BTC", "ETH", "SOL"]:
        am = markets[(markets.asset == asset) & (markets.yes_price.notna())]
        if len(am) == 0:
            continue
        print(f"\n{asset} (N={len(am):,}):")
        print(f"  Mean yes_price:   {am.yes_price.mean():.3f}")
        print(f"  Median yes_price: {am.yes_price.median():.3f}")
        # Bucket distribution
        buckets = pd.cut(am.yes_price, bins=[0, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
        dist = am.groupby(buckets, observed=True).size()
        for bucket, count in dist.items():
            pct = count / len(am) * 100
            print(f"  {str(bucket):>15s}: {count:>5,} ({pct:>5.1f}%)")

    # Volume by hour
    print("\n--- VOLUME BY UTC HOUR ---")
    for asset in ["BTC"]:  # Just BTC for brevity
        am = markets[(markets.asset == asset) & (markets.volume > 0)]
        hourly = am.groupby("hour").agg(
            count=("volume", "size"),
            mean_vol=("volume", "mean"),
            median_vol=("volume", "median"),
        ).sort_index()
        print(f"\n{asset} volume by hour:")
        print(f"  {'Hour':>4}  {'Count':>6}  {'Mean Vol':>10}  {'Med Vol':>10}")
        for h, row in hourly.iterrows():
            bar = "█" * int(row["mean_vol"] / hourly["mean_vol"].max() * 20)
            print(f"  {h:>4}  {row['count']:>6,}  ${row['mean_vol']:>9,.0f}  ${row['median_vol']:>9,.0f}  {bar}")

    # Weekday vs Weekend
    print("\n--- WEEKDAY vs WEEKEND ---")
    for asset in ["BTC", "ETH", "SOL"]:
        am = markets[(markets.asset == asset) & (markets.volume > 0)]
        wd = am[~am.is_weekend]
        we = am[am.is_weekend]
        print(f"\n{asset}:")
        print(f"  Weekday: {len(wd):>6,} markets, mean vol ${wd.volume.mean():>8,.0f}, median ${wd.volume.median():>8,.0f}")
        print(f"  Weekend: {len(we):>6,} markets, mean vol ${we.volume.mean():>8,.0f}, median ${we.volume.median():>8,.0f}")
        if len(wd) > 0 and len(we) > 0:
            ratio = we.volume.mean() / wd.volume.mean()
            print(f"  Weekend/Weekday ratio: {ratio:.2f}x")

    # Liquidity analysis
    print("\n--- LIQUIDITY ANALYSIS ---")
    for asset in ["BTC", "ETH", "SOL"]:
        am = markets[(markets.asset == asset) & (markets.liquidity.notna()) & (markets.liquidity > 0)]
        if len(am) == 0:
            continue
        print(f"\n{asset}:")
        print(f"  Mean liquidity:   ${am.liquidity.mean():>10,.0f}")
        print(f"  Median liquidity: ${am.liquidity.median():>10,.0f}")

    return markets


def analyze_trades(trades: pd.DataFrame, markets: pd.DataFrame):
    """Analyze trade patterns — timing, fill prices, volume profiles."""
    print("\n" + "=" * 80)
    print("TRADE ANALYSIS — 5m Window Trades")
    print("=" * 80)

    print(f"\nTotal trades: {len(trades):,}")
    for asset in ["BTC", "ETH", "SOL"]:
        at = trades[trades.asset == asset]
        print(f"  {asset}: {len(at):,} trades")

    # Trade columns available
    print(f"\nTrade columns: {list(trades.columns)}")

    # Trades per market
    trades_per_market = trades.groupby("slug").size()
    print(f"\nTrades per market:")
    print(f"  Mean:   {trades_per_market.mean():.1f}")
    print(f"  Median: {trades_per_market.median():.1f}")
    print(f"  Max:    {trades_per_market.max()}")

    # If we have maker_amount/taker_amount, analyze fill sizes
    if "maker_amount" in trades.columns:
        trades = trades.copy()
        trades["maker_amount"] = pd.to_numeric(trades["maker_amount"], errors="coerce")
        trades["taker_amount"] = pd.to_numeric(trades["taker_amount"], errors="coerce")

        # Token amounts are in wei-like units on Polygon (6 decimals for USDC)
        if trades["maker_amount"].max() > 1_000_000:
            trades["maker_usd"] = trades["maker_amount"] / 1e6
            trades["taker_usd"] = trades["taker_amount"] / 1e6
        else:
            trades["maker_usd"] = trades["maker_amount"]
            trades["taker_usd"] = trades["taker_amount"]

        print(f"\n--- TRADE SIZES ---")
        for asset in ["BTC", "ETH", "SOL"]:
            at = trades[trades.asset == asset]
            if len(at) == 0:
                continue
            col = "taker_usd" if "taker_usd" in at.columns else "taker_amount"
            print(f"\n{asset}:")
            print(f"  Mean size:   ${at[col].mean():>10,.2f}")
            print(f"  Median size: ${at[col].median():>10,.2f}")
            print(f"  Max size:    ${at[col].max():>10,.2f}")

    # If we have block timestamps or window_ts, analyze entry timing
    if "window_ts" in trades.columns and "block_number" in trades.columns:
        print(f"\n--- ENTRY TIMING ---")
        # We can't calculate exact seconds without block timestamps
        # but we can look at trade distribution within windows
        for asset in ["BTC"]:
            at = trades[(trades.asset == asset) & (trades.window_ts.notna())]
            markets_with_trades = at.slug.nunique()
            total_markets = len(markets[markets.asset == asset])
            print(f"\n{asset}:")
            print(f"  Markets with trades: {markets_with_trades:,} / {total_markets:,} ({markets_with_trades/total_markets*100:.1f}%)")


def analyze_outcomes(markets: pd.DataFrame):
    """Analyze outcomes from final yes_price — near 0 or 1 means resolved."""
    print("\n" + "=" * 80)
    print("OUTCOME ANALYSIS — Resolved Markets")
    print("=" * 80)

    markets = markets.copy()

    # Markets where yes_price is near 0 or 1 are resolved
    resolved = markets[(markets.yes_price.notna()) & ((markets.yes_price >= 0.95) | (markets.yes_price <= 0.05))]
    up_wins = resolved[resolved.yes_price >= 0.95]  # YES won = price went UP
    down_wins = resolved[resolved.yes_price <= 0.05]  # NO won = price went DOWN

    print(f"\nResolved markets (yes_price >= 0.95 or <= 0.05): {len(resolved):,}")
    print(f"  UP wins (yes >= 0.95):   {len(up_wins):,} ({len(up_wins)/len(resolved)*100:.1f}%)")
    print(f"  DOWN wins (yes <= 0.05): {len(down_wins):,} ({len(down_wins)/len(resolved)*100:.1f}%)")

    for asset in ["BTC", "ETH", "SOL"]:
        ar = resolved[resolved.asset == asset]
        au = up_wins[up_wins.asset == asset]
        ad = down_wins[down_wins.asset == asset]
        if len(ar) == 0:
            continue
        up_pct = len(au) / len(ar) * 100
        print(f"\n{asset} (N={len(ar):,}):")
        print(f"  UP:   {len(au):>5,} ({up_pct:.1f}%)")
        print(f"  DOWN: {len(ad):>5,} ({100-up_pct:.1f}%)")

    # UP bias by hour
    if "hour" in markets.columns:
        print("\n--- UP WIN RATE BY UTC HOUR (BTC) ---")
        btc_resolved = resolved[resolved.asset == "BTC"].copy()
        btc_resolved["up_win"] = btc_resolved.yes_price >= 0.95
        if "hour" not in btc_resolved.columns:
            btc_resolved["end_dt"] = pd.to_datetime(btc_resolved["end_date"], errors="coerce", utc=True)
            btc_resolved["hour"] = btc_resolved["end_dt"].dt.hour
        hourly = btc_resolved.groupby("hour").agg(
            total=("up_win", "size"),
            wins=("up_win", "sum"),
        )
        hourly["wr"] = hourly["wins"] / hourly["total"]
        print(f"  {'Hour':>4}  {'Total':>6}  {'UP':>5}  {'WR':>6}  Bar")
        for h, row in hourly.iterrows():
            bar = "█" * int(row["wr"] * 40)
            marker = " ◄ STRONG" if row["wr"] > 0.55 else " ◄ WEAK" if row["wr"] < 0.45 else ""
            print(f"  {h:>4}  {row['total']:>6,.0f}  {row['wins']:>5,.0f}  {row['wr']:>5.1%}  {bar}{marker}")

    # UP bias by day of week
    if "dow" in markets.columns:
        print("\n--- UP WIN RATE BY DAY OF WEEK ---")
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        for asset in ["BTC", "ETH", "SOL"]:
            ar = resolved[resolved.asset == asset].copy()
            ar["up_win"] = ar.yes_price >= 0.95
            if "dow" not in ar.columns:
                ar["end_dt"] = pd.to_datetime(ar["end_date"], errors="coerce", utc=True)
                ar["dow"] = ar["end_dt"].dt.dayofweek
            daily = ar.groupby("dow").agg(total=("up_win", "size"), wins=("up_win", "sum"))
            daily["wr"] = daily["wins"] / daily["total"]
            print(f"\n{asset}:")
            for d, row in daily.iterrows():
                wd = "WE" if d >= 5 else "WD"
                print(f"  {days[d]:>3} ({wd}): {row['total']:>5,.0f} markets, UP {row['wr']:.1%}")


def print_strategy_insights(markets: pd.DataFrame):
    """Print actionable strategy insights."""
    print("\n" + "=" * 80)
    print("STRATEGY INSIGHTS")
    print("=" * 80)

    markets = markets.copy()
    if "end_dt" not in markets.columns:
        markets["end_dt"] = pd.to_datetime(markets["end_date"], errors="coerce", utc=True)
    if "hour" not in markets.columns:
        markets["hour"] = markets["end_dt"].dt.hour
    if "dow" not in markets.columns:
        markets["dow"] = markets["end_dt"].dt.dayofweek
    if "is_weekend" not in markets.columns:
        markets["is_weekend"] = markets["dow"] >= 5

    resolved = markets[(markets.yes_price.notna()) & ((markets.yes_price >= 0.95) | (markets.yes_price <= 0.05))].copy()
    resolved["up_win"] = resolved.yes_price >= 0.95

    # Best/worst hours
    print("\n1. BEST TRADING HOURS (highest directional bias):")
    for asset in ["BTC", "SOL"]:
        ar = resolved[resolved.asset == asset]
        hourly = ar.groupby("hour").agg(n=("up_win", "size"), wr=("up_win", "mean"))
        hourly = hourly[hourly.n >= 50]  # Min sample size
        best = hourly.nlargest(3, "wr")
        worst = hourly.nsmallest(3, "wr")
        print(f"\n  {asset} — Best hours for UP bias:")
        for h, row in best.iterrows():
            print(f"    {h:02d}:00 UTC: {row['wr']:.1%} UP (N={row['n']:.0f})")
        print(f"  {asset} — Worst hours (DOWN bias or flat):")
        for h, row in worst.iterrows():
            print(f"    {h:02d}:00 UTC: {row['wr']:.1%} UP (N={row['n']:.0f})")

    # Volume sweet spot
    print("\n2. VOLUME vs WIN RATE:")
    for asset in ["BTC", "SOL"]:
        ar = resolved[(resolved.asset == asset) & (resolved.volume > 0)]
        vol_buckets = pd.qcut(ar.volume, q=5, duplicates="drop")
        vr = ar.groupby(vol_buckets, observed=True).agg(n=("up_win", "size"), wr=("up_win", "mean"))
        print(f"\n  {asset}:")
        for bucket, row in vr.iterrows():
            print(f"    Vol {str(bucket):>25s}: WR {row['wr']:.1%} (N={row['n']:.0f})")

    # Weekend effect
    print("\n3. WEEKEND EFFECT:")
    for asset in ["BTC", "ETH", "SOL"]:
        ar = resolved[resolved.asset == asset]
        wd = ar[~ar.is_weekend]
        we = ar[ar.is_weekend]
        wd_wr = wd.up_win.mean() if len(wd) > 0 else 0
        we_wr = we.up_win.mean() if len(we) > 0 else 0
        print(f"  {asset}: Weekday UP {wd_wr:.1%} (N={len(wd):,}) | Weekend UP {we_wr:.1%} (N={len(we):,})")


def save_summary(markets: pd.DataFrame):
    """Save analysis summary to S3."""
    summary = {
        "total_markets": len(markets),
        "by_asset": {
            asset: {
                "count": int(len(markets[markets.asset == asset])),
                "mean_volume": float(markets[markets.asset == asset].volume.mean()),
            }
            for asset in ["BTC", "ETH", "SOL"]
        },
        "date_range": {
            "min": str(markets.end_date.min())[:10],
            "max": str(markets.end_date.max())[:10],
        },
    }

    session = boto3.Session(profile_name=PROFILE)
    s3 = session.client("s3")
    s3.put_object(
        Bucket=S3_BUCKET,
        Key=f"{S3_PREFIX}/analysis_summary.json",
        Body=json.dumps(summary, indent=2),
    )
    print(f"\nSummary saved to s3://{S3_BUCKET}/{S3_PREFIX}/analysis_summary.json")


def main():
    markets, trades = load_data()

    markets = analyze_markets(markets)
    analyze_outcomes(markets)
    print_strategy_insights(markets)

    if trades is not None and len(trades) > 0:
        analyze_trades(trades, markets)

    save_summary(markets)

    print("\n" + "=" * 80)
    print("DONE")
    print("=" * 80)


if __name__ == "__main__":
    main()
