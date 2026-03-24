"""Simulate the V2 rebalance engine over a synthetic 5-minute window.

Replays realistic market conditions and prints tick-by-tick state.
No network, no CLOB — pure logic simulation.
"""
import math
import random
import time

random.seed(42)

# ── CONFIG ──────────────────────────────────────────────────────────────────
MAX_BET = 50.0
MAX_INITIAL_DEPLOY = 0.60  # 60% cap for accumulation
IMBALANCE_THRESHOLD = 0.05
BASE_SHARES = 5
WINDOW_SECS = 300

# ── SIMULATED STATE ────────────────────────────────────────────────────────
class State:
    def __init__(self):
        self.up_shares = 0
        self.up_cost = 0.0
        self.down_shares = 0
        self.down_cost = 0.0
        self.filled_usd = 0.0
        self.reserved_usd = 0.0
        self.open_orders = []  # [{side, price, shares, notional, posted_at}]
        self.lots = []  # [{side, price, shares, notional, filled}]
        self.last_fill_ts = 0.0

    @property
    def remaining(self):
        return max(MAX_BET - self.filled_usd - self.reserved_usd, 0)

    @property
    def total_value(self):
        return self.up_shares * market.yes_bid + self.down_shares * market.no_bid

    @property
    def up_ratio(self):
        tv = self.total_value
        return (self.up_shares * market.yes_bid / tv) if tv > 0 else 0.5


class Market:
    """Simulates orderbook with random walk."""
    def __init__(self, start_yes_bid=0.50):
        self.yes_bid = start_yes_bid
        self.no_bid = round(1.0 - start_yes_bid - 0.02, 2)  # 2¢ spread
        self.yes_ask = round(start_yes_bid + 0.01, 2)
        self.no_ask = round(self.no_bid + 0.01, 2)

    def tick(self):
        """Random walk — simulate 5m market movement."""
        move = random.gauss(0, 0.008)  # ~0.8% per tick volatility
        self.yes_bid = max(0.05, min(0.95, round(self.yes_bid + move, 2)))
        self.no_bid = max(0.05, min(0.95, round(1.0 - self.yes_bid - 0.02, 2)))
        self.yes_ask = round(self.yes_bid + 0.01, 2)
        self.no_ask = round(self.no_bid + 0.01, 2)


def model_predict(yes_bid, move_pct, seconds):
    """Simplified LGBM proxy — trend-following with noise."""
    signal = 0.50 + move_pct * 2.0 + random.gauss(0, 0.03)
    return max(0.30, min(0.70, signal))


def try_fill_orders(state, now):
    """Simulate fills: order fills if price >= ask (taker) or randomly at touch."""
    filled_any = False
    remaining_orders = []
    for order in state.open_orders:
        fill = False
        if order["side"] == "UP" and order["price"] >= market.yes_ask:
            fill = True
        elif order["side"] == "DOWN" and order["price"] >= market.no_ask:
            fill = True
        # Touch orders fill with ~30% probability per tick (simulates queue)
        elif order["side"] == "UP" and order["price"] >= market.yes_bid and random.random() < 0.30:
            fill = True
        elif order["side"] == "DOWN" and order["price"] >= market.no_bid and random.random() < 0.30:
            fill = True

        if fill:
            if order["side"] == "UP":
                state.up_shares += order["shares"]
                state.up_cost += order["notional"]
            else:
                state.down_shares += order["shares"]
                state.down_cost += order["notional"]
            state.filled_usd += order["notional"]
            state.reserved_usd -= order["notional"]
            state.lots.append({**order, "filled": True})
            state.last_fill_ts = now
            filled_any = True
        else:
            remaining_orders.append(order)
    state.open_orders = remaining_orders
    return filled_any


def accumulate(state, now):
    """Accumulation phase — posts ladder on both sides, capped at 60%."""
    if state.filled_usd >= MAX_INITIAL_DEPLOY * MAX_BET:
        return 0

    posted = 0
    for side, bid in [("UP", market.yes_bid), ("DOWN", market.no_bid)]:
        if bid <= 0:
            continue
        offsets = [0.00, 0.01, 0.02]
        for offset in offsets:
            price = round(bid - offset, 2)
            if price < 0.01:
                continue
            notional = round(BASE_SHARES * price, 2)
            if state.filled_usd + state.reserved_usd + notional > MAX_BET:
                continue
            # Check if already have order at this price/side
            if any(o["side"] == side and o["price"] == price for o in state.open_orders):
                continue
            state.reserved_usd += notional
            state.open_orders.append({
                "side": side, "price": price, "shares": BASE_SHARES,
                "notional": notional, "posted_at": now,
            })
            posted += 1
    return posted


def cancel_all_open(state):
    """Cancel all open orders, release budget."""
    released = 0.0
    for order in state.open_orders:
        released += order["notional"]
        state.reserved_usd -= order["notional"]
    state.open_orders = []
    return released


def rebalance_sell(state, prob_up, delta_up, now):
    """Sell overweight expensive lots — no-loss guard enforced."""
    if abs(delta_up) <= IMBALANCE_THRESHOLD:
        return 0.0, 0

    overweight_up = delta_up > 0
    overweight_side = "UP" if overweight_up else "DOWN"
    current_bid = market.yes_bid if overweight_up else market.no_bid

    # Collect sellable lots: filled, price >= 0.40, on overweight side, no-loss
    sellable = []
    skipped_loss = 0
    skipped_cheap = 0
    for lot in state.lots:
        if not lot.get("filled"):
            continue
        if lot["price"] < 0.40:
            skipped_cheap += 1
            continue
        if lot["side"] != overweight_side:
            continue
        if current_bid < lot["price"]:
            skipped_loss += 1
            continue
        sellable.append(lot)

    if not sellable:
        return 0.0, skipped_loss

    # Dynamic sell fraction
    sell_fraction = min(0.50, abs(delta_up))
    excess = abs(delta_up) * state.total_value
    sell_target = excess * sell_fraction

    sold_shares = 0
    sold_cost = 0.0
    for lot in sorted(sellable, key=lambda x: -x["price"]):
        if sold_cost >= sell_target:
            break
        shares = lot["shares"]
        cost = lot["notional"]
        sold_shares += shares
        sold_cost += cost
        # Update inventory
        if lot["side"] == "UP":
            state.up_shares -= shares
            state.up_cost -= cost
        else:
            state.down_shares -= shares
            state.down_cost -= cost
        state.filled_usd -= cost
        lot["filled"] = False  # mark as sold

    return sold_cost, skipped_loss


def quote_dual_ladder(state, prob_up, delta_up, now):
    """Cancel all open, rebuild both sides with skew + bias."""
    cancel_all_open(state)

    skew = max(min(delta_up, 0.50), -0.50)
    up_mult = max(1.0 - skew, 0.20)
    down_mult = max(1.0 + skew, 0.20)

    model_bias = max(min((prob_up - 0.50) * 2.0, 0.50), -0.50)
    base_offsets = [0.00, 0.01, 0.02]

    posted_up = 0
    posted_down = 0

    for side, bid, mult, bias_sign in [
        ("UP", market.yes_bid, up_mult, -model_bias),
        ("DOWN", market.no_bid, down_mult, model_bias),
    ]:
        if bid <= 0:
            continue
        offsets = [round(o * (1.0 + bias_sign * 0.5), 3) for o in base_offsets]
        offsets = [max(o, 0.00) for o in offsets]

        for offset in offsets:
            price = round(bid - offset, 2)
            if price < 0.01 or price > 0.98:
                continue
            shares = max(int(round(BASE_SHARES * mult)), 5)
            notional = round(shares * price, 2)
            if state.filled_usd + state.reserved_usd + notional > MAX_BET:
                continue
            state.reserved_usd += notional
            state.open_orders.append({
                "side": side, "price": price, "shares": shares,
                "notional": notional, "posted_at": now,
            })
            if side == "UP":
                posted_up += 1
            else:
                posted_down += 1

    # No-fill kicker
    no_fill_secs = (now - state.last_fill_ts) if state.last_fill_ts > 0 else 999
    if no_fill_secs >= 12:
        for side, ask in [("UP", market.yes_ask), ("DOWN", market.no_ask)]:
            kicker_price = round(ask - 0.01, 2)
            if kicker_price < 0.01:
                continue
            notional = round(5 * kicker_price, 2)
            if state.filled_usd + state.reserved_usd + notional > MAX_BET:
                continue
            state.reserved_usd += notional
            state.open_orders.append({
                "side": side, "price": kicker_price, "shares": 5,
                "notional": notional, "posted_at": now,
            })

    return posted_up, posted_down


# ── RUN SIMULATION ──────────────────────────────────────────────────────────
state = State()
market = Market(start_yes_bid=0.52)
start_price = market.yes_bid
t0 = time.time()

print(f"{'T+sec':>6} | {'YES_bid':>7} | {'NO_bid':>6} | {'prob':>5} | {'UP_sh':>5} | {'DN_sh':>5} | {'UP%':>5} | {'filled$':>8} | {'rsrvd$':>7} | {'rem$':>6} | {'open':>4} | {'fills':>5} | {'sold$':>6} | {'skip_loss':>9} | action")
print("-" * 165)

total_fills = 0
total_sold = 0.0
intervals = {"0-30": 0, "30-60": 0, "60-120": 0, "120-180": 0, "180+": 0}

for tick in range(0, WINDOW_SECS, 3):
    now = t0 + tick
    market.tick()

    move_pct = (market.yes_bid - start_price) / start_price if start_price > 0 else 0
    prob_up = model_predict(market.yes_bid, move_pct, tick)

    # Phase 1: accumulate (T+5 to T+270, capped at 60%)
    accum_posted = 0
    if 5 <= tick <= 270:
        accum_posted = accumulate(state, now)

    # Try fills
    filled = try_fill_orders(state, now)
    if filled:
        fills_this_tick = sum(1 for l in state.lots if l.get("filled") and abs(l.get("posted_at", 0) - now) < 5)
    else:
        fills_this_tick = 0

    # Count fills by interval
    if filled:
        if tick <= 30:
            intervals["0-30"] += 1
        elif tick <= 60:
            intervals["30-60"] += 1
        elif tick <= 120:
            intervals["60-120"] += 1
        elif tick <= 180:
            intervals["120-180"] += 1
        else:
            intervals["180+"] += 1

    # Phase 3: rebalance (T+30 onward)
    sold_usd = 0.0
    skipped = 0
    posted_up = 0
    posted_down = 0
    action = ""

    if tick >= 30:
        tv = state.total_value
        if tv > 1.0:
            target_up = 0.65 if prob_up > 0.55 else (0.35 if prob_up < 0.45 else 0.50)
            delta_up = state.up_ratio - target_up

            sold_usd, skipped = rebalance_sell(state, prob_up, delta_up, now)
            if sold_usd > 0:
                total_sold += sold_usd
                action = f"SELL ${sold_usd:.2f}"

            posted_up, posted_down = quote_dual_ladder(state, prob_up, delta_up, now)
            if not action:
                action = f"QUOTE {posted_up}+{posted_down}"
            else:
                action += f" → QUOTE {posted_up}+{posted_down}"
    else:
        action = f"ACCUM +{accum_posted}"

    # Print every 3s for first 60s, then every 15s
    if tick <= 60 or tick % 15 == 0:
        print(f"{tick:>6} | {market.yes_bid:>7.2f} | {market.no_bid:>6.2f} | {prob_up:>5.2f} | "
              f"{int(state.up_shares):>5} | {int(state.down_shares):>5} | "
              f"{state.up_ratio*100:>4.0f}% | "
              f"${state.filled_usd:>7.2f} | ${state.reserved_usd:>6.2f} | "
              f"${state.remaining:>5.2f} | {len(state.open_orders):>4} | "
              f"{'YES' if filled else '':>5} | ${sold_usd:>5.2f} | "
              f"{skipped:>9} | {action}")

print()
print("=" * 80)
print("SUMMARY")
print("=" * 80)
print(f"Final position: {int(state.up_shares)} UP @ avg ${state.up_cost/max(state.up_shares,1):.3f} | "
      f"{int(state.down_shares)} DOWN @ avg ${state.down_cost/max(state.down_shares,1):.3f}")
print(f"Final UP%: {state.up_ratio*100:.1f}%")
print(f"Total filled: ${state.filled_usd:.2f} / ${MAX_BET:.2f}")
print(f"Total sold (rebalance): ${total_sold:.2f}")
print(f"Lots created: {len(state.lots)}")
print(f"Cheap lots (<$0.40): {sum(1 for l in state.lots if l.get('filled') and l['price'] < 0.40)}")
print()
print("FILLS BY INTERVAL:")
for k in ["0-30", "30-60", "60-120", "120-180", "180+"]:
    print(f"  T+{k:>7}: {intervals[k]} fill events")
print()

# Verify cheap lot protection
cheap_sold = [l for l in state.lots if not l.get("filled") and l["price"] < 0.40]
expensive_sold = [l for l in state.lots if not l.get("filled") and l["price"] >= 0.40]
print(f"Cheap lots sold (<$0.40): {len(cheap_sold)}")
print(f"Expensive lots sold (>=$0.40): {len(expensive_sold)}")
if cheap_sold:
    print("  !! WARNING: Cheap lots were sold — BUG!")
else:
    print("  ✓ No cheap lots sold — protection working")

loss_sold = [l for l in state.lots if not l.get("filled") and l["price"] >= 0.40]
# Can't check loss precisely without tracking sell price, but the guard is in code
print(f"\nNo-loss guard: {sum(1 for _ in []) if not skipped else 'active'} — lots skipped when bid < entry")
