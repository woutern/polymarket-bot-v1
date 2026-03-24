#!/usr/bin/env python3
"""
BTC 5m Arb Sniper v2 — execution-aware live arb tester.

Goal:
  Buy N YES + N NO with same N and combined avg < 1.00, while minimizing one-leg risk.

Safety layers:
  1) Pre-trade checks (edge, depth, min-notional, budget)
  2) FOK dual submit (parallel)
  3) Post-trade accounting + one-leg tracking
"""

import argparse
import asyncio
import json
import math
import os
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen

try:
    import aiohttp

    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

try:
    from py_clob_client.client import ClobClient
    from py_clob_client.clob_types import ApiCreds, CreateOrderOptions, OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    HAS_CLOB = True
except ImportError:
    HAS_CLOB = False

CLOB_BASE = "https://clob.polymarket.com"
GAMMA_BASE = "https://gamma-api.polymarket.com"
CHAIN_ID = 137


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    print(f"[{ts}] {msg}", flush=True)


def api_get(url: str, timeout: float = 5.0):
    try:
        req = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "polybot-sniper/2.0",
            },
        )
        with urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def find_active_btc_5m() -> dict | None:
    now_ts = int(time.time())
    window_epoch = (now_ts // 300) * 300

    for epoch in [window_epoch, window_epoch + 300]:
        slug = f"btc-updown-5m-{epoch}"
        data = api_get(f"{GAMMA_BASE}/markets?slug={slug}")
        if not data or not isinstance(data, list):
            continue
        if len(data) == 0:
            continue

        market = data[0]
        raw_tokens = market.get("clobTokenIds", "")

        if isinstance(raw_tokens, str):
            try:
                tokens = json.loads(raw_tokens)
            except Exception:
                tokens = [
                    t.strip().strip('"')
                    for t in raw_tokens.strip("[]").split(",")
                    if t.strip()
                ]
        elif isinstance(raw_tokens, list):
            tokens = raw_tokens
        else:
            tokens = []

        if len(tokens) < 2:
            condition_id = market.get("conditionId", "")
            if condition_id:
                clob_market = api_get(f"{CLOB_BASE}/markets/{condition_id}")
                if isinstance(clob_market, list):
                    for cm in clob_market:
                        t = cm.get("tokens", [])
                        if len(t) >= 2:
                            tokens = [t[0].get("token_id", ""), t[1].get("token_id", "")]
                            break
                elif isinstance(clob_market, dict):
                    t = clob_market.get("tokens", [])
                    if len(t) >= 2:
                        tokens = [t[0].get("token_id", ""), t[1].get("token_id", "")]

        if len(tokens) >= 2 and tokens[0] and tokens[1]:
            return {
                "slug": slug,
                "epoch": epoch,
                "question": market.get("question", slug),
                "condition_id": market.get("conditionId", ""),
                "yes_token": tokens[0],
                "no_token": tokens[1],
            }

    return None


async def async_get_price(session, token_id: str, side: str) -> tuple[float, float]:
    url = f"{CLOB_BASE}/price?token_id={token_id}&side={side}"
    t0 = time.time()
    try:
        async with session.get(url) as resp:
            data = await resp.json()
            ms = round((time.time() - t0) * 1000, 1)
            return float(data.get("price", 0)), ms
    except Exception:
        return 0.0, round((time.time() - t0) * 1000, 1)


async def async_get_all_prices(session, yes_token: str, no_token: str) -> dict:
    t0 = time.time()
    results = await asyncio.gather(
        async_get_price(session, yes_token, "BUY"),
        async_get_price(session, no_token, "BUY"),
        async_get_price(session, yes_token, "SELL"),
        async_get_price(session, no_token, "SELL"),
    )
    total_ms = round((time.time() - t0) * 1000, 1)
    return {
        "yes_buy": results[0][0],
        "no_buy": results[1][0],
        "yes_sell": results[2][0],
        "no_sell": results[3][0],
        "yes_buy_ms": results[0][1],
        "no_buy_ms": results[1][1],
        "total_ms": total_ms,
    }


def sync_get_all_prices(yes_token: str, no_token: str) -> dict:
    t0 = time.time()
    yb = api_get(f"{CLOB_BASE}/price?token_id={yes_token}&side=BUY")
    t1 = time.time()
    nb = api_get(f"{CLOB_BASE}/price?token_id={no_token}&side=BUY")
    t2 = time.time()
    ys = api_get(f"{CLOB_BASE}/price?token_id={yes_token}&side=SELL")
    ns = api_get(f"{CLOB_BASE}/price?token_id={no_token}&side=SELL")
    t3 = time.time()
    return {
        "yes_buy": float((yb or {}).get("price", 0)),
        "no_buy": float((nb or {}).get("price", 0)),
        "yes_sell": float((ys or {}).get("price", 0)),
        "no_sell": float((ns or {}).get("price", 0)),
        "yes_buy_ms": round((t1 - t0) * 1000, 1),
        "no_buy_ms": round((t2 - t1) * 1000, 1),
        "total_ms": round((t3 - t0) * 1000, 1),
    }


def _best_ask_and_size(book: dict | None) -> tuple[float, float]:
    asks = (book or {}).get("asks", [])
    best_price = 0.0
    best_size = 0.0
    for a in asks:
        try:
            p = float(a.get("price", 0))
            s = float(a.get("size", 0))
        except Exception:
            continue
        if p <= 0 or s <= 0:
            continue
        if best_price == 0.0 or p < best_price:
            best_price = p
            best_size = s
    return best_price, best_size


async def async_get_top_ask(session, token_id: str) -> dict:
    url = f"{CLOB_BASE}/book?token_id={token_id}"
    t0 = time.time()
    try:
        async with session.get(url) as resp:
            data = await resp.json()
            p, s = _best_ask_and_size(data)
            return {"ask": p, "ask_size": s, "ms": round((time.time() - t0) * 1000, 1)}
    except Exception:
        return {"ask": 0.0, "ask_size": 0.0, "ms": round((time.time() - t0) * 1000, 1)}


def sync_get_top_ask(token_id: str) -> dict:
    t0 = time.time()
    data = api_get(f"{CLOB_BASE}/book?token_id={token_id}")
    p, s = _best_ask_and_size(data)
    return {"ask": p, "ask_size": s, "ms": round((time.time() - t0) * 1000, 1)}


class SafeArbExecutor:
    def __init__(self, live: bool, budget: float, max_per_snipe: float, cooldown: float, max_combined: float):
        self.live = live
        self.budget = budget
        self.max_per_snipe = max_per_snipe
        self.cooldown = cooldown
        self.max_combined = max_combined
        self.total_spent = 0.0
        self.total_profit_locked = 0.0
        self.snipe_count = 0
        self.failed_snipes = 0
        self.one_leg_events = 0
        self.one_leg_unwound = 0
        self.one_leg_held = 0
        self.bad_fill_events = 0
        self.rebalanced_pairs = 0
        self.flattened_skew = 0
        self.realized_pnl = 0.0
        self.last_snipe_ts = 0.0
        self.last_inventory_action_ts = 0.0
        self.client = None
        self.open_one_leg_positions = []

        if live:
            if not HAS_CLOB:
                raise RuntimeError("pip install py-clob-client")

            pk = os.environ.get("POLYMARKET_PK") or os.environ.get("POLYMARKET_PRIVATE_KEY")
            funder = os.environ.get("POLYMARKET_FUNDER") or None
            if not pk or not funder:
                raise RuntimeError("Set POLYMARKET_FUNDER and one of: POLYMARKET_PK or POLYMARKET_PRIVATE_KEY")

            chain_id = int(os.environ.get("POLYMARKET_CHAIN_ID", str(CHAIN_ID)))
            sig_type = 2 if funder else 0

            api_key = os.environ.get("POLYMARKET_API_KEY")
            api_secret = os.environ.get("POLYMARKET_API_SECRET")
            api_passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE")

            creds = None
            if api_key and api_secret and api_passphrase:
                creds = ApiCreds(
                    api_key=api_key,
                    api_secret=api_secret,
                    api_passphrase=api_passphrase,
                )

            self.client = ClobClient(
                host=CLOB_BASE,
                key=pk,
                chain_id=chain_id,
                creds=creds,
                signature_type=sig_type,
                funder=funder,
            )
            if creds is None:
                self.client.set_api_creds(self.client.create_or_derive_api_creds())

            log(
                f"✅ Live client ready (chain_id={chain_id}, sig_type={sig_type}, api_creds={'yes' if creds else 'derived'})"
            )

    async def _attempt_one_leg_unwind(
        self,
        token_id: str,
        shares: int,
        entry_price: float,
        target_profit_bps: float,
        max_loss_bps: float,
        retries: int,
        retry_delay_ms: int,
    ) -> dict:
        if not self.live or self.client is None:
            return {"closed": False, "reason": "not_live"}
        if shares <= 0:
            return {"closed": False, "reason": "zero_shares"}

        # Break-even + tiny profit target; never go below configured loss floor.
        target = round(entry_price + (target_profit_bps / 10000.0), 2)
        floor = round(max(0.01, entry_price - (max_loss_bps / 10000.0)), 2)
        step = 0.01
        attempts = max(retries, 1)

        options = CreateOrderOptions(tick_size="0.01", neg_risk=False)
        loop = asyncio.get_running_loop()

        for i in range(attempts):
            price = round(max(floor, target - (i * step)), 2)
            try:
                sell_args = OrderArgs(price=price, size=shares, side="SELL", token_id=token_id)
                signed = self.client.create_order(sell_args, options)
                raw = await loop.run_in_executor(None, lambda: self.client.post_order(signed, OrderType.FOK))
                resp = self._normalize_order_resp(raw)
                if resp["success"]:
                    fill_price = resp["avg_price"] or price
                    proceeds = round(shares * fill_price, 2)
                    return {
                        "closed": True,
                        "price": fill_price,
                        "proceeds": proceeds,
                        "order_id": resp["order_id"][:16],
                        "attempts": i + 1,
                    }
            except Exception:
                pass

            if i < attempts - 1 and retry_delay_ms > 0:
                await asyncio.sleep(retry_delay_ms / 1000.0)

        return {"closed": False, "reason": "unwind_not_filled", "attempts": attempts}

    @staticmethod
    def _to_float(value, default=0.0) -> float:
        try:
            if value is None:
                return default
            return float(value)
        except Exception:
            return default

    def _normalize_order_resp(self, raw) -> dict:
        if isinstance(raw, Exception):
            return {
                "success": False,
                "order_id": "",
                "error": str(raw)[:160],
                "filled_size": 0.0,
                "avg_price": None,
            }
        if not isinstance(raw, dict):
            return {
                "success": False,
                "order_id": "",
                "error": f"bad_response_type={type(raw).__name__}",
                "filled_size": 0.0,
                "avg_price": None,
            }

        order_id = str(raw.get("orderID") or raw.get("id") or "")
        explicit_success = raw.get("success")
        success = bool(explicit_success) if explicit_success is not None else bool(order_id)

        status = str(raw.get("status", "")).lower()
        if status in {"rejected", "cancelled", "canceled", "failed"}:
            success = False

        filled_size = self._to_float(raw.get("filledSize", raw.get("sizeMatched", raw.get("matchedSize", 0.0))), default=0.0)
        avg_price = self._to_float(raw.get("avgPrice", raw.get("averagePrice", raw.get("price"))), default=0.0)
        avg_price = avg_price if avg_price > 0 else None

        return {
            "success": success,
            "order_id": order_id,
            "error": str(raw.get("errorMsg") or raw.get("error") or "")[:160],
            "filled_size": filled_size,
            "avg_price": avg_price,
            "raw": raw,
        }

    def can_snipe(self) -> tuple[bool, str]:
        now = time.time()
        if now - self.last_snipe_ts < self.cooldown:
            return False, f"cooldown {self.cooldown - (now - self.last_snipe_ts):.1f}s"
        remaining = self.budget - self.total_spent
        if remaining < 2.0:
            return False, f"budget (${remaining:.2f} left)"
        return True, "ok"

    def compute_shares(
        self,
        yes_price: float,
        no_price: float,
        yes_ask_size: float,
        no_ask_size: float,
        min_leg_notional: float,
        min_shares: int,
    ) -> int:
        cost_per_pair = yes_price + no_price
        if cost_per_pair <= 0 or cost_per_pair >= 1.0:
            return 0

        remaining = self.budget - self.total_spent
        max_from_budget = int(remaining / cost_per_pair)
        max_from_limit = int(self.max_per_snipe / cost_per_pair)
        max_from_book = int(min(yes_ask_size, no_ask_size))
        max_shares = min(max_from_budget, max_from_limit, max_from_book)
        if max_shares <= 0:
            return 0

        min_for_yes = int(math.ceil(min_leg_notional / max(yes_price, 1e-9)))
        min_for_no = int(math.ceil(min_leg_notional / max(no_price, 1e-9)))
        min_required = max(min_shares, min_for_yes, min_for_no)

        if max_shares < min_required:
            return 0

        # Smallest executable N minimizes one-leg exposure.
        return min_required

    async def execute_snipe(self, yes_token: str, no_token: str, yes_price: float, no_price: float, shares: int) -> dict:
        self.last_snipe_ts = time.time()
        t0 = time.time()

        max_yes_price = yes_price
        max_no_price = no_price
        max_combined = max_yes_price + max_no_price

        if max_combined >= self.max_combined:
            return {
                "success": False,
                "reason": f"pre_trade_reject: combined ${max_combined:.4f} >= ${self.max_combined:.3f}",
                "exec_ms": round((time.time() - t0) * 1000, 1),
            }

        expected_cost = round(shares * max_combined, 2)
        expected_profit = round(shares * (1.0 - max_combined), 2)

        log(
            f"   LAYER 1 ✅ max_yes=${max_yes_price:.3f} max_no=${max_no_price:.3f} "
            f"combined=${max_combined:.4f} shares={shares} "
            f"cost=${expected_cost:.2f} profit=${expected_profit:.2f}"
        )

        if not self.live:
            await asyncio.sleep(0.001)
            self.total_spent += expected_cost
            self.total_profit_locked += expected_profit
            self.snipe_count += 1
            return {
                "success": True,
                "mode": "DRY_RUN",
                "shares": shares,
                "yes_price": max_yes_price,
                "no_price": max_no_price,
                "cost": expected_cost,
                "locked_profit": expected_profit,
                "exec_ms": 1.0,
            }

        try:
            options = CreateOrderOptions(tick_size="0.01", neg_risk=False)

            yes_args = OrderArgs(price=max_yes_price, size=shares, side=BUY, token_id=yes_token)
            no_args = OrderArgs(price=max_no_price, size=shares, side=BUY, token_id=no_token)

            yes_signed = self.client.create_order(yes_args, options)
            no_signed = self.client.create_order(no_args, options)

            t_signed = time.time()
            sign_ms = round((t_signed - t0) * 1000, 1)

            loop = asyncio.get_running_loop()
            yes_future = loop.run_in_executor(None, lambda: self.client.post_order(yes_signed, OrderType.FOK))
            no_future = loop.run_in_executor(None, lambda: self.client.post_order(no_signed, OrderType.FOK))
            yes_raw, no_raw = await asyncio.gather(yes_future, no_future, return_exceptions=True)

            t_done = time.time()
            exec_ms = round((t_done - t0) * 1000, 1)
            post_ms = round((t_done - t_signed) * 1000, 1)

            yes_resp = self._normalize_order_resp(yes_raw)
            no_resp = self._normalize_order_resp(no_raw)
            yes_oid = yes_resp["order_id"]
            no_oid = no_resp["order_id"]
            yes_filled = yes_resp["success"]
            no_filled = no_resp["success"]

            log(
                f"   LAYER 2 | YES={'✅' if yes_filled else '❌'} "
                f"NO={'✅' if no_filled else '❌'} | "
                f"sign={sign_ms:.0f}ms post={post_ms:.0f}ms total={exec_ms:.0f}ms"
            )
            if yes_resp["error"] or no_resp["error"]:
                log(f"   LAYER 2 details | YES_err='{yes_resp['error']}' NO_err='{no_resp['error']}'")

        except Exception as e:
            exec_ms = round((time.time() - t0) * 1000, 1)
            self.failed_snipes += 1
            return {"success": False, "reason": f"execution_error: {str(e)[:120]}", "exec_ms": exec_ms}

        if yes_filled and no_filled:
            yes_fill_price = yes_resp["avg_price"] or max_yes_price
            no_fill_price = no_resp["avg_price"] or max_no_price
            actual_combined = yes_fill_price + no_fill_price
            actual_cost = round(shares * actual_combined, 2)
            actual_profit = round(shares * (1.0 - actual_combined), 2)

            if actual_combined > self.max_combined:
                log(f"   LAYER 3 ⚠️  BAD FILL: combined ${actual_combined:.4f} > ${self.max_combined:.3f}")
                self.bad_fill_events += 1

            self.total_spent += actual_cost
            self.total_profit_locked += actual_profit
            self.snipe_count += 1

            log(f"   LAYER 3 ✅ PERFECT ARB | cost=${actual_cost:.2f} locked_profit=${actual_profit:.2f}")

            return {
                "success": True,
                "mode": "LIVE",
                "type": "PERFECT_ARB",
                "shares": shares,
                "yes_price": yes_fill_price,
                "no_price": no_fill_price,
                "cost": actual_cost,
                "locked_profit": actual_profit,
                "yes_order_id": yes_oid[:16],
                "no_order_id": no_oid[:16],
                "sign_ms": sign_ms,
                "post_ms": post_ms,
                "exec_ms": exec_ms,
            }

        if yes_filled and not no_filled:
            yes_fill_price = yes_resp["avg_price"] or max_yes_price
            one_leg_cost = round(shares * yes_fill_price, 2)
            self.total_spent += one_leg_cost
            self.one_leg_events += 1
            if getattr(self, "one_leg_unwind_enabled", False):
                unwind = await self._attempt_one_leg_unwind(
                    token_id=yes_token,
                    shares=shares,
                    entry_price=yes_fill_price,
                    target_profit_bps=self.one_leg_unwind_target_bps,
                    max_loss_bps=self.one_leg_unwind_max_loss_bps,
                    retries=self.one_leg_unwind_retries,
                    retry_delay_ms=self.one_leg_unwind_retry_delay_ms,
                )
                if unwind.get("closed"):
                    proceeds = unwind["proceeds"]
                    pnl = round(proceeds - one_leg_cost, 2)
                    self.total_spent = round(max(0.0, self.total_spent - one_leg_cost), 2)
                    self.one_leg_unwound += 1
                    log(
                        f"   LAYER 3 ♻️  ONE LEG UNWOUND (YES) | "
                        f"sell=${unwind['price']:.2f} pnl=${pnl:.2f} attempts={unwind['attempts']}"
                    )
                    return {
                        "success": False,
                        "mode": "LIVE",
                        "type": "ONE_LEG_YES_UNWOUND",
                        "shares": shares,
                        "cost": one_leg_cost,
                        "unwind_price": unwind["price"],
                        "unwind_pnl": pnl,
                        "exec_ms": exec_ms,
                    }

            self.one_leg_held += 1
            self.open_one_leg_positions.append(
                {
                    "side": "YES",
                    "token": yes_token,
                    "shares": shares,
                    "price": yes_fill_price,
                    "cost": one_leg_cost,
                    "order_id": yes_oid,
                    "ts": time.time(),
                }
            )
            log(
                f"   LAYER 3 ⚠️  ONE LEG: YES filled, NO rejected | "
                f"cost=${one_leg_cost:.2f} | holding YES to resolution"
            )
            return {
                "success": False,
                "mode": "LIVE",
                "type": "ONE_LEG_YES",
                "shares": shares,
                "yes_price": yes_fill_price,
                "cost": one_leg_cost,
                "yes_order_id": yes_oid[:16],
                "exec_ms": exec_ms,
            }

        if no_filled and not yes_filled:
            no_fill_price = no_resp["avg_price"] or max_no_price
            one_leg_cost = round(shares * no_fill_price, 2)
            self.total_spent += one_leg_cost
            self.one_leg_events += 1
            if getattr(self, "one_leg_unwind_enabled", False):
                unwind = await self._attempt_one_leg_unwind(
                    token_id=no_token,
                    shares=shares,
                    entry_price=no_fill_price,
                    target_profit_bps=self.one_leg_unwind_target_bps,
                    max_loss_bps=self.one_leg_unwind_max_loss_bps,
                    retries=self.one_leg_unwind_retries,
                    retry_delay_ms=self.one_leg_unwind_retry_delay_ms,
                )
                if unwind.get("closed"):
                    proceeds = unwind["proceeds"]
                    pnl = round(proceeds - one_leg_cost, 2)
                    self.total_spent = round(max(0.0, self.total_spent - one_leg_cost), 2)
                    self.one_leg_unwound += 1
                    log(
                        f"   LAYER 3 ♻️  ONE LEG UNWOUND (NO) | "
                        f"sell=${unwind['price']:.2f} pnl=${pnl:.2f} attempts={unwind['attempts']}"
                    )
                    return {
                        "success": False,
                        "mode": "LIVE",
                        "type": "ONE_LEG_NO_UNWOUND",
                        "shares": shares,
                        "cost": one_leg_cost,
                        "unwind_price": unwind["price"],
                        "unwind_pnl": pnl,
                        "exec_ms": exec_ms,
                    }

            self.one_leg_held += 1
            self.open_one_leg_positions.append(
                {
                    "side": "NO",
                    "token": no_token,
                    "shares": shares,
                    "price": no_fill_price,
                    "cost": one_leg_cost,
                    "order_id": no_oid,
                    "ts": time.time(),
                }
            )
            log(
                f"   LAYER 3 ⚠️  ONE LEG: NO filled, YES rejected | "
                f"cost=${one_leg_cost:.2f} | holding NO to resolution"
            )
            return {
                "success": False,
                "mode": "LIVE",
                "type": "ONE_LEG_NO",
                "shares": shares,
                "no_price": no_fill_price,
                "cost": one_leg_cost,
                "no_order_id": no_oid[:16],
                "exec_ms": exec_ms,
            }

        self.failed_snipes += 1
        log("   LAYER 3 ✅ Both rejected — no exposure, no cost")
        return {
            "success": False,
            "mode": "LIVE",
            "type": "BOTH_REJECTED",
            "exec_ms": exec_ms,
        }

    async def _post_fok_single(self, token_id: str, side: str, price: float, shares: int) -> dict:
        if not self.live or self.client is None:
            return {"success": False, "error": "not_live"}
        try:
            options = CreateOrderOptions(tick_size="0.01", neg_risk=False)
            args = OrderArgs(price=price, size=shares, side=side, token_id=token_id)
            signed = self.client.create_order(args, options)
            loop = asyncio.get_running_loop()
            raw = await loop.run_in_executor(None, lambda: self.client.post_order(signed, OrderType.FOK))
            return self._normalize_order_resp(raw)
        except Exception as e:
            return {"success": False, "error": str(e)[:160], "order_id": "", "filled_size": 0.0, "avg_price": None}

    async def manage_open_inventory(
        self,
        yes_token: str,
        no_token: str,
        yes_buy: float,
        no_buy: float,
        rebalance_max_combined: float,
        rebalance_cost_buffer_bps: float,
        flatten_on_fail: bool,
        flatten_target_bps: float,
        min_action_gap_s: float = 0.5,
    ) -> dict | None:
        if not self.open_one_leg_positions:
            return None
        now = time.time()
        if now - self.last_inventory_action_ts < min_action_gap_s:
            return None

        pos = self.open_one_leg_positions[0]
        side = pos.get("side")
        shares = int(pos.get("shares", 0))
        entry_price = float(pos.get("price", 0.0))
        token = pos.get("token")
        if shares <= 0 or entry_price <= 0 or not token:
            self.open_one_leg_positions.pop(0)
            return {"action": "DROP_BAD_POSITION"}

        missing_side = "YES" if side == "NO" else "NO"
        missing_token = yes_token if side == "NO" else no_token
        missing_buy = yes_buy if side == "NO" else no_buy

        if missing_buy > 0:
            pair_combined = round(entry_price + missing_buy, 4)
            effective_cap = rebalance_max_combined - (rebalance_cost_buffer_bps / 10000.0)
            if pair_combined <= effective_cap:
                self.last_inventory_action_ts = now
                reb = await self._post_fok_single(
                    token_id=missing_token,
                    side=BUY,
                    price=missing_buy,
                    shares=shares,
                )
                if reb.get("success"):
                    buy_fill = reb.get("avg_price") or missing_buy
                    add_cost = round(shares * buy_fill, 2)
                    self.total_spent += add_cost
                    locked = round(shares * (1.0 - (entry_price + buy_fill)), 2)
                    self.total_profit_locked += locked
                    self.rebalanced_pairs += 1
                    self.snipe_count += 1
                    self.open_one_leg_positions.pop(0)
                    return {
                        "action": "REBALANCED",
                        "side_filled": side,
                        "side_bought": missing_side,
                        "shares": shares,
                        "entry": entry_price,
                        "buy": buy_fill,
                        "combined": round(entry_price + buy_fill, 4),
                        "effective_cap": round(effective_cap, 4),
                        "locked": locked,
                    }

        if flatten_on_fail:
            self.last_inventory_action_ts = now
            unwind = await self._attempt_one_leg_unwind(
                token_id=token,
                shares=shares,
                entry_price=entry_price,
                target_profit_bps=flatten_target_bps,
                max_loss_bps=0.0,
                retries=2,
                retry_delay_ms=80,
            )
            if unwind.get("closed"):
                proceeds = float(unwind.get("proceeds", 0.0))
                leg_cost = round(shares * entry_price, 2)
                pnl = round(proceeds - leg_cost, 2)
                self.total_spent = round(max(0.0, self.total_spent - leg_cost), 2)
                self.realized_pnl += pnl
                self.flattened_skew += 1
                self.one_leg_unwound += 1
                self.open_one_leg_positions.pop(0)
                return {
                    "action": "FLATTENED",
                    "side": side,
                    "shares": shares,
                    "entry": entry_price,
                    "exit": unwind.get("price"),
                    "pnl": pnl,
                }

        return None

    def summary(self) -> dict:
        return {
            "snipes": self.snipe_count,
            "failed": self.failed_snipes,
            "one_leg": self.one_leg_events,
            "one_leg_unwound": self.one_leg_unwound,
            "one_leg_held": self.one_leg_held,
            "bad_fills": self.bad_fill_events,
            "rebalanced_pairs": self.rebalanced_pairs,
            "flattened_skew": self.flattened_skew,
            "realized_pnl": round(self.realized_pnl, 2),
            "spent": round(self.total_spent, 2),
            "budget": self.budget,
            "profit_locked": round(self.total_profit_locked, 2),
            "open_one_leg": len(self.open_one_leg_positions),
        }


async def run_sniper(args):
    mode = "LIVE" if args.live else "DRY RUN"
    log("=" * 80)
    log(f"BTC 5m ARB SNIPER v2 — {mode}")
    log("Safety: LIMIT orders + FOK + post-trade validation")
    log(f"Threshold: combined < ${args.threshold:.3f}")
    log(f"Min edge: {args.min_edge:.4f}")
    log(f"Max combined (post-trade): ${args.max_combined:.3f}")
    log(f"Budget: ${args.budget:.2f} | Max/snipe: ${args.max_per_snipe:.2f}")
    log(f"Cooldown: {args.cooldown}s")
    log(f"Min leg notional: ${args.min_leg_notional:.2f} | Min shares: {args.min_shares}")
    log(f"One-leg breaker/window: {args.max_one_leg_per_window}")
    log(f"Block while open one-leg: {'ON' if args.block_while_open_one_leg else 'OFF'}")
    log(
        f"One-leg unwind: {'ON' if args.one_leg_unwind else 'OFF'} | "
        f"target={args.one_leg_unwind_target_bps:.1f}bps "
        f"max_loss={args.one_leg_unwind_max_loss_bps:.1f}bps "
        f"retries={args.one_leg_unwind_retries}"
    )
    log(
        f"Rebalance guard: max_combined={args.rebalance_max_combined:.4f} "
        f"cost_buffer={args.rebalance_cost_buffer_bps:.1f}bps"
    )
    log("=" * 80)

    executor = SafeArbExecutor(
        live=args.live,
        budget=args.budget,
        max_per_snipe=args.max_per_snipe,
        cooldown=args.cooldown,
        max_combined=args.max_combined,
    )
    executor.one_leg_unwind_enabled = bool(args.one_leg_unwind)
    executor.one_leg_unwind_target_bps = float(args.one_leg_unwind_target_bps)
    executor.one_leg_unwind_max_loss_bps = float(args.one_leg_unwind_max_loss_bps)
    executor.one_leg_unwind_retries = int(args.one_leg_unwind_retries)
    executor.one_leg_unwind_retry_delay_ms = int(args.one_leg_unwind_retry_delay_ms)

    lats = []
    for _ in range(3):
        t0 = time.time()
        api_get(f"{CLOB_BASE}/time")
        lats.append(round((time.time() - t0) * 1000, 1))
    log(f"API latency: min={min(lats):.1f}ms avg={sum(lats)/len(lats):.1f}ms")

    market = find_active_btc_5m()
    if not market:
        log("No active BTC 5m market.")
        return

    yes_token = market["yes_token"]
    no_token = market["no_token"]
    log(f"📊 {market['question']}")
    log("")

    total_polls = 0
    arb_detected = 0
    poll_latencies = []
    last_slug = market["slug"]
    price_miss = 0
    budget_paused_logged = False

    start_time = time.time()
    end_time = start_time + (args.duration * 60)
    next_heartbeat_ts = start_time + args.heartbeat_seconds

    session = None
    if HAS_AIOHTTP:
        session = aiohttp.ClientSession(
            headers={"Accept": "application/json", "User-Agent": "polybot-sniper/2.0"},
            timeout=aiohttp.ClientTimeout(total=5),
        )
        log("⚡ Async HTTP (parallel price fetching)")
    else:
        log("🐌 Sync HTTP — install aiohttp for parallel speed")

    log("")

    try:
        while time.time() < end_time:
            loop_start = time.time()

            now_ts = int(time.time())
            window_epoch = (now_ts // 300) * 300
            expected_slug = f"btc-updown-5m-{window_epoch}"
            if expected_slug != last_slug:
                market = find_active_btc_5m()
                if not market:
                    await asyncio.sleep(2)
                    continue
                old_spent = executor.total_spent
                old_snipes = executor.snipe_count
                yes_token = market["yes_token"]
                no_token = market["no_token"]
                last_slug = market["slug"]
                budget_paused_logged = False
                if args.recycle_budget_per_window:
                    # Budget recycling: prior 5m window has resolved, so recycle cap for next window.
                    executor.total_spent = 0.0
                log(f"📊 NEW: {market['question']}")
                if args.recycle_budget_per_window:
                    log(
                        f"♻️ Budget reset: ${old_spent:.2f} recycled | "
                        f"{old_snipes} snipes lifetime | budget=${executor.budget:.2f}"
                    )

            if session:
                prices = await async_get_all_prices(session, yes_token, no_token)
            else:
                prices = sync_get_all_prices(yes_token, no_token)

            if prices["yes_buy"] == 0 or prices["no_buy"] == 0:
                price_miss += 1
                now = time.time()
                if now >= next_heartbeat_ts:
                    s = executor.summary()
                    log(
                        f"💓 alive | polls={total_polls} arbs={arb_detected} "
                        f"snipes={s['snipes']} misses={price_miss} "
                        f"slug={last_slug} | waiting for valid prices..."
                    )
                    next_heartbeat_ts = now + args.heartbeat_seconds
                await asyncio.sleep(args.interval)
                continue

            total_polls += 1
            poll_latencies.append(prices["total_ms"])
            combined = round(prices["yes_buy"] + prices["no_buy"], 4)
            spread = round(1.0 - combined, 4)
            seconds_in = now_ts - window_epoch

            inv_action = await executor.manage_open_inventory(
                yes_token=yes_token,
                no_token=no_token,
                yes_buy=prices["yes_buy"],
                no_buy=prices["no_buy"],
                rebalance_max_combined=args.rebalance_max_combined,
                rebalance_cost_buffer_bps=args.rebalance_cost_buffer_bps,
                flatten_on_fail=args.flatten_skew_on_fail,
                flatten_target_bps=args.flatten_target_bps,
            )
            if inv_action:
                if inv_action["action"] == "REBALANCED":
                    log(
                        f"♻️ REBALANCED {inv_action['side_filled']}→{inv_action['side_bought']} "
                        f"shares={inv_action['shares']} combined=${inv_action['combined']:.4f} "
                        f"(cap=${inv_action['effective_cap']:.4f}) "
                        f"locked=${inv_action['locked']:.2f}"
                    )
                elif inv_action["action"] == "FLATTENED":
                    log(
                        f"🧹 FLATTENED {inv_action['side']} shares={inv_action['shares']} "
                        f"entry=${inv_action['entry']:.3f} exit=${inv_action['exit']:.3f} "
                        f"pnl=${inv_action['pnl']:.2f}"
                    )

            if combined < args.threshold:
                arb_detected += 1
                can, reason = executor.can_snipe()

                if not can:
                    if "budget" in reason and not budget_paused_logged:
                        log(f"⏸ budget cap reached: {reason}. No more trades this run.")
                        budget_paused_logged = True
                    elif arb_detected % 20 == 0:
                        log(f"🎯 ARB ${spread:.4f} (skipped: {reason})")
                elif spread < args.min_edge:
                    if arb_detected % 20 == 0:
                        log(f"🎯 ARB ${spread:.4f} (skipped: edge<{args.min_edge:.4f})")
                else:
                    if args.block_while_open_one_leg and executor.open_one_leg_positions:
                        if arb_detected % 20 == 0:
                            log("🎯 ARB skipped: open one-leg inventory exists")
                        continue
                    # Exposure-based breaker: only block when there is currently open one-leg inventory.
                    # This avoids freezing the rest of the window after a one-leg was already flattened.
                    open_one_leg_now = len(executor.open_one_leg_positions)
                    if open_one_leg_now >= args.max_one_leg_per_window:
                        if arb_detected % 20 == 0:
                            log("🎯 ARB skipped: one-leg circuit breaker active for this window")
                    else:
                        if session:
                            yes_top, no_top = await asyncio.gather(
                                async_get_top_ask(session, yes_token),
                                async_get_top_ask(session, no_token),
                            )
                        else:
                            yes_top = sync_get_top_ask(yes_token)
                            no_top = sync_get_top_ask(no_token)

                        if yes_top["ask"] <= 0 or no_top["ask"] <= 0:
                            pass
                        elif abs(yes_top["ask"] - prices["yes_buy"]) > 0.02 or abs(no_top["ask"] - prices["no_buy"]) > 0.02:
                            pass
                        else:
                            shares = executor.compute_shares(
                                yes_price=prices["yes_buy"],
                                no_price=prices["no_buy"],
                                yes_ask_size=yes_top["ask_size"],
                                no_ask_size=no_top["ask_size"],
                                min_leg_notional=args.min_leg_notional,
                                min_shares=args.min_shares,
                            )

                            if shares > 0:
                                log(
                                    f"🎯 ARB detected | T+{seconds_in}s | "
                                    f"YES=${prices['yes_buy']:.3f} NO=${prices['no_buy']:.3f} "
                                    f"combined=${combined:.4f} spread=${spread:.4f} "
                                    f"shares={shares} | poll={prices['total_ms']:.0f}ms "
                                    f"| ask_sz Y={yes_top['ask_size']:.0f} N={no_top['ask_size']:.0f}"
                                )

                                result = await executor.execute_snipe(
                                    yes_token,
                                    no_token,
                                    prices["yes_buy"],
                                    prices["no_buy"],
                                    shares,
                                )

                                if result["success"]:
                                    log(
                                        f"🚀 SNIPE #{executor.snipe_count} | "
                                        f"${result['cost']:.2f} spent → "
                                        f"${result['locked_profit']:.2f} locked | "
                                        f"{result['exec_ms']:.0f}ms | {result.get('type', '')}"
                                    )
                                else:
                                    log(f"   Result: {result.get('type', result.get('reason', '?'))}")
                            elif arb_detected % 20 == 0:
                                log("🎯 ARB skipped: not executable for depth/notional/budget")
            else:
                if total_polls % max(int(30 / args.interval), 1) == 0:
                    s = executor.summary()
                    log(
                        f"  ... poll={total_polls} | ${combined:.4f} | T+{seconds_in}s | "
                        f"arbs={arb_detected} snipes={s['snipes']} 1-leg={s['one_leg']} | "
                        f"${prices['total_ms']:.0f}ms"
                    )

            now = time.time()
            if now >= next_heartbeat_ts:
                s = executor.summary()
                log(
                    f"💓 alive | poll={total_polls} T+{seconds_in}s "
                    f"yes=${prices['yes_buy']:.3f} no=${prices['no_buy']:.3f} "
                    f"combined=${combined:.4f} arbs={arb_detected} "
                    f"snipes={s['snipes']} 1-leg={s['one_leg']} lat={prices['total_ms']:.0f}ms"
                )
                next_heartbeat_ts = now + args.heartbeat_seconds

            elapsed = time.time() - loop_start
            await asyncio.sleep(max(args.interval - elapsed, 0))

    finally:
        if session:
            await session.close()

    total_time = time.time() - start_time
    s = executor.summary()

    log("")
    log("=" * 80)
    log("RESULTS")
    log("=" * 80)
    log(f"Duration:          {total_time/60:.1f} min")
    log(f"Polls:             {total_polls} ({total_polls/max(total_time,1):.1f}/s)")
    if poll_latencies:
        p95_idx = max(int(len(poll_latencies) * 0.95) - 1, 0)
        p95 = sorted(poll_latencies)[p95_idx]
        log(
            f"Latency:           min={min(poll_latencies):.0f}ms "
            f"avg={sum(poll_latencies)/len(poll_latencies):.0f}ms "
            f"p95={p95:.0f}ms"
        )
    log("")
    log("DETECTION")
    log(f"  Arbs found:      {arb_detected}")
    if total_time > 0:
        log(f"  Per hour:        {arb_detected/(total_time/3600):.0f}")
    log("")
    log("EXECUTION")
    log(f"  Perfect arbs:    {s['snipes']}")
    log(f"  Failed (both):   {s['failed']}")
    log(f"  One-leg:         {s['one_leg']}")
    log(f"  One-leg unwound: {s['one_leg_unwound']}")
    log(f"  One-leg held:    {s['one_leg_held']}")
    log(f"  Bad fills:       {s['bad_fills']}")
    log("")
    log("FINANCIALS")
    log(f"  Spent:           ${s['spent']:.2f} / ${s['budget']:.2f}")
    log(f"  Locked profit:   ${s['profit_locked']:.2f}")
    log(f"  Open 1-leg:      {s['open_one_leg']} positions")
    log("=" * 80)


def main():
    parser = argparse.ArgumentParser(description="BTC 5m Arb Sniper v2 — execution-aware")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Detection-only mode. Default if no mode selected.")

    parser.add_argument("--threshold", type=float, default=0.98, help="Detect when yes+no < threshold")
    parser.add_argument("--min-edge", type=float, default=0.015, help="Minimum edge (1-thresholded-combined)")
    parser.add_argument("--max-combined", type=float, default=0.995, help="Reject execution when combined >= this")

    parser.add_argument("--budget", type=float, default=25.0)
    parser.add_argument("--max-per-snipe", type=float, default=5.0)
    parser.add_argument("--cooldown", type=float, default=2.0)

    parser.add_argument("--min-leg-notional", type=float, default=1.0)
    parser.add_argument("--min-shares", type=int, default=1)
    parser.add_argument("--max-one-leg-per-window", type=int, default=1)
    parser.set_defaults(block_while_open_one_leg=True)
    parser.add_argument("--block-while-open-one-leg", dest="block_while_open_one_leg", action="store_true")
    parser.add_argument("--allow-trading-with-open-one-leg", dest="block_while_open_one_leg", action="store_false")
    parser.set_defaults(one_leg_unwind=True)
    parser.add_argument("--one-leg-unwind", dest="one_leg_unwind", action="store_true")
    parser.add_argument("--no-one-leg-unwind", dest="one_leg_unwind", action="store_false")
    parser.add_argument("--one-leg-unwind-target-bps", type=float, default=10.0)
    parser.add_argument("--one-leg-unwind-max-loss-bps", type=float, default=60.0)
    parser.add_argument("--one-leg-unwind-retries", type=int, default=6)
    parser.add_argument("--one-leg-unwind-retry-delay-ms", type=int, default=120)
    parser.add_argument("--rebalance-max-combined", type=float, default=0.985)
    parser.add_argument("--rebalance-cost-buffer-bps", type=float, default=10.0)
    parser.set_defaults(flatten_skew_on_fail=True)
    parser.add_argument("--flatten-skew-on-fail", dest="flatten_skew_on_fail", action="store_true")
    parser.add_argument("--no-flatten-skew-on-fail", dest="flatten_skew_on_fail", action="store_false")
    parser.add_argument("--flatten-target-bps", type=float, default=5.0)

    parser.add_argument("--interval", type=float, default=0.1)
    parser.add_argument("--duration", type=float, default=30)
    parser.add_argument("--heartbeat-seconds", type=float, default=5.0)
    parser.set_defaults(recycle_budget_per_window=True)
    parser.add_argument(
        "--recycle-budget-per-window",
        dest="recycle_budget_per_window",
        action="store_true",
        help="Reset spent budget at each new 5m window",
    )
    parser.add_argument(
        "--no-recycle-budget-per-window",
        dest="recycle_budget_per_window",
        action="store_false",
        help="Do not reset spent budget at window change",
    )

    args = parser.parse_args()

    if args.live and args.dry_run:
        parser.error("Choose either --live or --dry-run, not both.")
    if not args.live and not args.dry_run:
        args.dry_run = True

    if args.live:
        log("⚠️  LIVE MODE — Real limit FOK orders will be placed!")
        log("Safety: execution-aware filters + one-leg breaker + budget cap")
        log("Press Ctrl+C within 5 seconds to abort...")
        time.sleep(5)

    asyncio.run(run_sniper(args))


if __name__ == "__main__":
    main()
