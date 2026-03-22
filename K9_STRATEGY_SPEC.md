K9 TRADER STRATEGY — COMPLETE IMPLEMENTATION SPEC
===================================================

We analyzed 3,500 trades from K9 (k9Q2mX4L8A7ZP3R), a trader 
with 13.4% ROI, $2,370 profit on 40 windows. This file contains 
the EXACT patterns from the data and how to implement them.

Our budget: $20/window total. K9 uses $705/window.
Scale everything proportionally.

=== K9 VERIFIED METRICS (from 3,501 trade records) ===

Both sides rate:        98% (buys UP and DOWN in same window)
GP rate:                69% (combined avg < $1.00)
Average combined avg:   $0.922
Average trades/window:  85
Average budget/window:  $705
Heavier side gets:      70% of budget (NOT 50/50)
First trade:            T+5-13s (median T+7s, NOT T+0)
Trade span:             209s average (continuous)
Cheap fills (<25¢):     31% of buys
Expensive fills (>40¢): 51% of buys
Hold rate:              85% of shares held to resolution
Zero-sell windows:      65%
Sell-at-loss rate:      61% (sells losing side to free capital)
Rebuy after sell:       median 2s, 37% same second, 66% under 5s

=== K9 BUY PRICE DISTRIBUTION ===

 1-9¢:   16% of buys — lottery tickets, avg timing T+172s
10-19¢:  16% of buys — cheap accumulation, avg T+149s
20-29¢:  10% of buys — mid accumulation, avg T+96s
30-39¢:  11% of buys — mid price, avg T+50s
40-49¢:  18% of buys — open baseline, avg T+25s
50-59¢:  10% of buys — open baseline, avg T+32s
60-79¢:  13% of buys — winning side buys, avg T+90s
80-99¢:   6% of buys — winning side guaranteed return

KEY INSIGHT: 51% of buys are OVER 40¢. K9 is NOT just buying 
cheap. He buys heavily at open near 50¢, then accumulates cheap 
throughout, AND buys the expensive winning side at 60-95¢.

=== K9 ORDER SIZE DISTRIBUTION ===

Min: $0.00, Median: $3.50, Average: $9.23, Max: $129.98

$0-$1:     23% of orders
$1-$2:     13%
$2-$5:     24%
$5-$10:    16%
$10-$50:   21%
$50-$500:   3%

At our $5/asset budget, scale to: $0.25-$1.00 per order.

=== K9 ORDER CLUSTERING ===

Max orders in single second: 33
Average batch size: 7.5 orders
This means: asyncio.gather, fire many orders in parallel.

=== K9 EXECUTION TIMELINE (per 5m window) ===

T+5-30s:   HEAVY LOAD — 40-100 orders, 60-80% of budget
           Buys BOTH sides at 40-55¢ (near market price)
           Heavier on predicted direction (70% of budget)
           This sets the baseline. At $5 budget → $3-4 here.

T+30-80s:  ACCUMULATE — prices start diverging
           Winning side goes up (55-70¢), losing drops (30-45¢)
           K9 buys BOTH sides continuously
           Starts getting cheaper fills on losing side

T+80-180s: CHEAP FILLS — losing side crashes to 5-20¢
           K9 floods orders on cheap side (20-58 orders/batch)
           Buys at 7¢, 8¢, 10¢, 12¢, 15¢ etc.
           ALSO buys winning side at 60-85¢ (guaranteed return)
           This is where the edge is built

T+180-260s: LATE ACCUMULATION — very cheap fills
           Losing side may hit 4-8¢
           K9 still buying with smaller sizes
           Still posting on BOTH sides

T+260-270s: WIND DOWN
           Cancel unfilled GTC orders
           Stop posting new orders

T+270-300s: HOLD TO RESOLUTION
           Zero trading. Hold everything.
           One side pays $1/share. Other pays $0.
           If combined avg < $1 → profit.

=== K9 SELL MECHANICS (35% of windows) ===

WHEN K9 sells:
- Only the side that is LOSING value (dropping in price)
- Entry was typically 30-50¢, now dropped to 15-25¢
- Sells to FREE CAPITAL, not to lock profit

WHAT HAPPENS AFTER SELL:
- 37% of time: rebuys in SAME SECOND (0s gap)
- 66% of time: rebuys within 5 seconds
- Rebuys either: same side at cheaper price, OR opposite side
- Sell + rebuy in same async call

WHAT K9 NEVER SELLS:
- Shares bought under 15¢ (lottery tickets)
- Anything after T+260s
- The winning side (unless swapping direction)

=== IMPLEMENTATION FOR OUR BOT ($20 BUDGET) ===

ASSETS: BTC_5m, ETH_5m, SOL_5m, XRP_5m
Per-asset budget: $20 / 4 = $5 per asset per window

--- PHASE 1: OPEN (T+5 to T+15s) ---

Wait until T+5s (let orderbook form).
Run LGBM model (fallback 0.50 if error).
Post GTC on BOTH sides:
  Main (LGBM direction): 60% of per-asset budget ($3.00)
  Hedge (opposite):      40% of per-asset budget ($2.00)
  Post at bid+1¢. Fallback to $0.49 if bid is 0.

LGBM confidence adjustments:
  lgbm >= 0.60: main=70%, hedge=30% ($3.50 / $1.50)
  lgbm 0.52-0.60: main=60%, hedge=40% ($3.00 / $2.00)
  lgbm < 0.52: main=50%, hedge=50% ($2.50 / $2.50)

--- PHASE 2: CONFIRM (T+15 to T+20s, once) ---

Re-run LGBM with 15s of price data.
If confirmed: post 10% more on main ($0.50).
If flipped: swap main/hedge labels for accumulation.

--- PHASE 3: ACCUMULATE (every 3s from T+5 to T+cutoff) ---

cutoff = window_seconds - 30 (270s for 5m)

Every 3 seconds, post GTC ladder on BOTH sides:

For EACH side (yes_token, no_token):
  Read current bid for that token.
  
  If bid <= 0.15 (very cheap — lottery zone):
    Post 7 levels: bid, bid-1¢, bid-2¢, bid-3¢, bid-4¢, bid-5¢, bid-6¢
    Size: $0.50 each (these are 14x return at 7¢)
  
  If bid > 0.15 and bid <= 0.35 (cheap — accumulation zone):
    Post 5 levels: bid, bid-1¢, bid-2¢, bid-4¢, bid-6¢
    Size: $0.35 each
  
  If bid > 0.35 and bid <= 0.60 (mid — baseline zone):
    Post 3 levels: bid, bid-2¢, bid-5¢
    Size: $0.25 each
  
  If bid > 0.60 (expensive — winning side):
    Post 2 levels: bid, bid-3¢
    Size: $0.50 each (guaranteed 20-40% return if this side wins)
    Only after T+60s (wait for direction to be clearer)

All orders: GTC, parallel posting (asyncio.gather).
All orders cost $0 until filled. Only fills count against budget.

BUDGET CAP: Track early_cheap_filled per window per asset.
Before posting: if early_cheap_filled + size > per_asset_budget, skip.
The open position ($5) counts toward budget.
Accumulation has remaining budget ($5 - open_spend).

--- PHASE 4: SELL-ROTATE (optional, 35% of windows) ---

Check every 15s from T+30 to T+240:
  Only consider selling if ALL true:
    1. Entry price of the position was > 40¢
    2. Current bid for that side dropped > 25% from entry
    3. seconds_since_open between 30 and 240
  
  If selling:
    Sell via FOK at current bid
    IMMEDIATELY (same async call, 0 seconds):
      Rebuy same side at current ask (if ask < 40¢)
      OR rebuy opposite side (if that's cheaper)
    Log sell + rebuy as one atomic operation
  
  NEVER sell:
    Shares where entry was under 25¢
    Any shares after T+240s
    The winning side (bid > entry price)

--- PHASE 5: CANCEL + HOLD (T+cutoff to resolution) ---

At T+cutoff (T+270s for 5m):
  Cancel all unfilled GTC orders
  Stop posting new orders
  Hold ALL shares to resolution
  Zero trading after cutoff

=== FILL POLLING ===

Every 3s (same tick as accumulate):
  For each tracked order in early_dca_orders:
    Call get_order(oid)
    If status MATCHED or FILLED:
      Mark order as filled
      Increment early_cheap_filled by order size
      Map side to UP/DOWN shares:
        "main" + direction_up=True → UP
        "main" + direction_up=False → DOWN
        "hedge" + direction_up=True → DOWN
        "hedge" + direction_up=False → UP
        "UP" → UP
        "DOWN" → DOWN
      Log v2_fill_detected
    Catch and LOG all exceptions (never bare except:pass)

=== STATE TRACKING ===

Reset ALL per window per asset on window_opened:
  early_position = None
  early_dca_orders = []
  early_cheap_filled = 0.0
  early_cheap_posted = 0.0
  early_up_shares = 0.0
  early_down_shares = 0.0
  early_up_cost = 0.0
  early_down_cost = 0.0
  early_accum_ticks = set()
  early_confirm_done = False

=== STATUS LOG (every 15s) ===

Log v2_status with:
  asset, seconds_since_open, up_shares, up_cost, up_avg,
  down_shares, down_cost, down_avg, combined_avg,
  margin_pct, orders_posted, orders_filled,
  budget_remaining

=== WHAT TO KEEP ===

DO NOT change:
  - Coinbase WebSocket price feeds
  - Polymarket CLOB client + order execution
  - DynamoDB storage + resolution logic
  - LGBM model server + auto-retrain
  - Dashboard (live + overview tabs)
  - Shadow tracker (competitor polling)
  - OrderbookSnapshot + refresh logic
  - Risk manager

=== WHAT TO DELETE ===

Remove all old strategy code:
  - _early_entry_tick (old T+14-18s single-directional)
  - Old DCA rounds (_early_dca_round)
  - Old Scenario C scan logic (_scan_tick)
  - Old checkpoint sell logic that sells cheap shares
  Keep _early_checkpoint but rewrite per Phase 4 above

=== CONFIG (Secrets Manager) ===

EARLY_ENTRY_ENABLED=false (manual enable after review)
EARLY_ENTRY_MAX_BET=20.00
PAIRS=BTC_5m,ETH_5m,SOL_5m,XRP_5m

=== BUILD + DEPLOY ===

Run all tests (including test_v2_strategy.py).
Build with: docker build --platform linux/amd64
Push to ECR. Deploy to ECS.
Keep EARLY_ENTRY_ENABLED=false.
Commit: "V2 rewrite: K9-style both-sides accumulate-and-hold"
