"""Build enriched training dataset by joining training data + signals.

Merges:
- Dataset A: 26,649 BTC/SOL 5m windows (outcomes, prices)
- Dataset B: 2,240 windows with ask data (signals)

Stores in polymarket-bot-training-enriched DynamoDB table.

Usage:
    uv run python scripts/build_enriched_training.py
"""

import sys
sys.path.insert(0, "src")

import boto3
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal


def main():
    session = boto3.Session(profile_name="playground", region_name="eu-west-1")

    # Load training data
    print("Loading training data...")
    td_table = session.resource("dynamodb").Table("polymarket-bot-training-data")
    all_td = []
    resp = td_table.scan()
    all_td.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = td_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        all_td.extend(resp.get("Items", []))

    training = {}
    for i in all_td:
        if i.get("timeframe") != "5m" or i.get("asset") not in ("BTC", "SOL"):
            continue
        wid = i.get("window_id", "")
        slug = wid.split("_", 2)[-1] if "_" in wid else wid
        if not slug:
            continue
        ts = float(i.get("timestamp", 0))
        utc_hour = datetime.fromtimestamp(ts, tz=timezone.utc).hour
        dow = datetime.fromtimestamp(ts, tz=timezone.utc).weekday()
        training[slug] = {
            "slug": slug,
            "asset": i.get("asset"),
            "timestamp": ts,
            "open_price": float(i.get("open_price", 0)),
            "close_price": float(i.get("close_price", 0)),
            "outcome": int(float(i.get("outcome", 0))),
            "pct_move": float(i.get("pct_move", 0)),
            "utc_hour": utc_hour,
            "day_of_week": dow,
            "weak_hours": (utc_hour < 9) or (utc_hour >= 21) or (utc_hour == 12),
        }

    print(f"  BTC+SOL 5m windows: {len(training)}")

    # Load signals — pick best per window (closest to T+210s)
    print("Loading signals...")
    sig_table = session.resource("dynamodb").Table("polymarket-bot-signals")
    all_sig = []
    resp = sig_table.scan()
    all_sig.extend(resp.get("Items", []))
    while "LastEvaluatedKey" in resp:
        resp = sig_table.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
        all_sig.extend(resp.get("Items", []))

    by_slug = defaultdict(list)
    for s in all_sig:
        if s.get("timeframe") != "5m" or s.get("asset") not in ("BTC", "SOL"):
            continue
        slug = s.get("window_slug", "")
        if slug and s.get("yes_ask") and s.get("no_ask"):
            by_slug[slug].append(s)

    signals = {}
    for slug, sl in by_slug.items():
        best = min(sl, key=lambda s: abs(float(s.get("seconds_remaining", 999)) - 90))
        if float(best.get("seconds_remaining", 999)) > 200:
            continue
        signals[slug] = best

    print(f"  Unique windows with ask data: {len(signals)}")

    # Merge
    print("Merging...")
    enriched = []
    matched = 0

    for slug, td in training.items():
        record = {**td, "enriched": False}

        if slug in signals:
            s = signals[slug]
            ya = float(s.get("yes_ask", 0))
            na = float(s.get("no_ask", 0))
            ask = ya if ya >= na else na
            side = "YES" if ya >= na else "NO"
            won = (side == "YES" and td["outcome"] == 1) or (side == "NO" and td["outcome"] == 0)

            record.update({
                "enriched": True,
                "yes_ask_210s": ya,
                "no_ask_210s": na,
                "ask_at_210s": ask,
                "dominant_side": side,
                "won": won,
                "lgbm_prob": float(s.get("lgbm_prob", 0) or 0),
                "p_bayesian": float(s.get("p_bayesian", 0) or 0),
                "realized_vol": float(s.get("realized_vol", 0) or 0),
                "btc_move_pct": float(s.get("btc_move_pct", 0) or 0),
                "scan_best_ask": float(s.get("scan_best_ask", 0) or 0),
                "scan_duration_s": float(s.get("scan_duration_s", 0) or 0),
                "direction_flipped": bool(s.get("direction_flipped", False)),
                "window_high": float(s.get("window_high", 0) or 0),
                "window_low": float(s.get("window_low", 0) or 0),
                "current_price": float(s.get("current_price", 0) or 0),
                "tier": str(s.get("tier", "")),
                "seconds_remaining": float(s.get("seconds_remaining", 0)),
            })
            matched += 1

        enriched.append(record)

    print(f"  Total enriched records: {len(enriched)}")
    print(f"  Fully enriched (with asks): {matched}")
    print(f"  Partial (outcomes only): {len(enriched) - matched}")

    # Write to DynamoDB
    print("Writing to training_enriched...")
    table = session.resource("dynamodb").Table("polymarket-bot-training-enriched")
    written = 0
    skipped = 0

    for r in enriched:
        try:
            item = {}
            for k, v in r.items():
                if isinstance(v, float):
                    item[k] = Decimal(str(round(v, 6)))
                elif isinstance(v, bool):
                    item[k] = v
                else:
                    item[k] = v

            table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(slug)",
            )
            written += 1
        except Exception as e:
            if "ConditionalCheckFailedException" in type(e).__name__:
                skipped += 1
            else:
                skipped += 1

        if (written + skipped) % 1000 == 0:
            print(f"  Progress: {written} written, {skipped} skipped")

    print(f"  Done: {written} written, {skipped} skipped")

    # Quick analysis
    enriched_only = [r for r in enriched if r.get("enriched")]
    print(f"\n  Analysis on {len(enriched_only)} enriched windows:")
    wins = len([r for r in enriched_only if r.get("won")])
    print(f"  WR: {wins}/{len(enriched_only)} = {wins/len(enriched_only)*100:.1f}%")

    for asset in ["BTC", "SOL"]:
        at = [r for r in enriched_only if r["asset"] == asset]
        aw = len([r for r in at if r.get("won")])
        print(f"  {asset}: {aw}/{len(at)} = {aw/len(at)*100:.1f}%")


if __name__ == "__main__":
    main()
