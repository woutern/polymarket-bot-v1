"""One-time backfill: update coinbase_inferred trades with Gamma API truth.

For each trade with outcome_source='coinbase_inferred', query Gamma API
for the actual market outcome. If conclusive (Up=1.0 or Down=1.0),
update the DynamoDB record to polymarket_verified.

Safe to run while bot is trading — only updates old records.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal

import boto3
import httpx
from boto3.dynamodb.conditions import Attr


async def main():
    session = boto3.Session(profile_name="playground", region_name="us-east-1")
    table = session.resource("dynamodb").Table("polymarket-bot-trades")

    # Get all coinbase_inferred trades
    resp = table.scan(
        FilterExpression=Attr("outcome_source").eq("coinbase_inferred"),
    )
    items = resp["Items"]
    print(f"Found {len(items)} coinbase_inferred trades to check")

    # Group by slug to minimize API calls
    by_slug: dict[str, list[dict]] = {}
    for item in items:
        slug = item["window_slug"]
        by_slug.setdefault(slug, []).append(item)

    print(f"Across {len(by_slug)} unique slugs")

    updated = 0
    skipped = 0
    errors = 0

    async with httpx.AsyncClient(timeout=10.0) as client:
        for slug, trades in sorted(by_slug.items()):
            try:
                resp_api = await client.get(
                    "https://gamma-api.polymarket.com/markets",
                    params={"slug": slug},
                )
                if resp_api.status_code != 200 or not resp_api.json():
                    print(f"  {slug}: no Gamma data, skipping")
                    skipped += len(trades)
                    continue

                m = resp_api.json()[0]
                if not m.get("closed"):
                    print(f"  {slug}: not closed, skipping")
                    skipped += len(trades)
                    continue

                outcomes = m.get("outcomes", [])
                if isinstance(outcomes, str):
                    outcomes = json.loads(outcomes)
                prices = m.get("outcomePrices", [])
                if isinstance(prices, str):
                    prices = json.loads(prices)

                if len(outcomes) < 2 or len(prices) < 2:
                    skipped += len(trades)
                    continue

                outcome_map = dict(zip(outcomes, prices))
                up_price = float(outcome_map.get("Up", 0))

                if up_price >= 0.99:
                    winner = "YES"
                elif up_price <= 0.01:
                    winner = "NO"
                else:
                    print(f"  {slug}: ambiguous (up={up_price:.3f}), skipping")
                    skipped += len(trades)
                    continue

                # Update each trade for this slug
                for t in trades:
                    trade_id = t["id"]
                    side = t.get("side", "")
                    correct = side == winner

                    table.update_item(
                        Key={"id": trade_id},
                        UpdateExpression="SET outcome_source = :s, polymarket_winner = :w, correct_prediction = :c",
                        ExpressionAttributeValues={
                            ":s": "polymarket_verified",
                            ":w": winner,
                            ":c": Decimal(str(int(correct))),
                        },
                    )
                    updated += 1

                print(f"  {slug}: {winner} → updated {len(trades)} trade(s)")
                await asyncio.sleep(0.2)

            except Exception as e:
                print(f"  {slug}: ERROR {e}")
                errors += len(trades)

    print()
    print(f"Done: {updated} updated, {skipped} skipped, {errors} errors")


if __name__ == "__main__":
    asyncio.run(main())
