#!/usr/bin/env python3
"""CLI-only simulation of the current V2 5m execution path.

Runs the real `_v2_open_position`, `_v2_confirm`, `_v2_poll_fills`, and
`_v2_execution_tick` code against a synthetic BTC 5m market and a mocked local
exchange client. This avoids AWS, paper mode, and the live CLOB while still
exercising the production strategy logic.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from py_clob_client.order_builder.constants import BUY, SELL

from polybot.core.loop import AssetState, TradingLoop
from polybot.market.window_tracker import WindowTracker
from polybot.models import OrderbookSnapshot, Window
from polybot.strategy.base_rate import BaseRateTable
from polybot.strategy.bayesian import BayesianUpdater


@dataclass
class Checkpoint:
    second: int
    price: float
    prob_up: float
    yes_bid: float
    no_bid: float
    up_shares: int
    down_shares: int
    up_avg: float
    down_avg: float
    net_cost: float
    reserved: float
    remaining: float
    open_orders: int


class FakeClock:
    def __init__(self, start_ts: float):
        self.now = float(start_ts)

    def set(self, ts: float) -> None:
        self.now = float(ts)

    def time(self) -> float:
        return self.now


class SyntheticFiveMinuteMarket:
    def __init__(self, scenario: str, open_price: float, seed: int = 7):
        self.scenario = scenario
        self.open_price = float(open_price)
        self.rng = random.Random(seed)

    def _trend_move(self, second: int) -> float:
        progress = second / 300.0
        wobble = 0.0012 * math.sin(second / 11.0) + 0.0008 * math.sin(second / 29.0)
        if self.scenario == "up":
            return 0.0075 * progress + wobble
        if self.scenario == "down":
            return -0.0075 * progress + wobble
        return 0.0022 * math.sin(second / 17.0) + 0.0012 * math.sin(second / 41.0)

    def price_at(self, second: int) -> float:
        move = self._trend_move(second)
        return round(self.open_price * (1.0 + move), 2)

    def market_prob_at(self, second: int) -> float:
        move = self._trend_move(second)
        prob = 0.50 + (move * 30.0)
        return max(0.08, min(0.92, round(prob, 2)))

    def model_prob_at(self, second: int) -> float:
        move = self._trend_move(second)
        if self.scenario == "up":
            prob = 0.54 + 0.11 * min(second / 180.0, 1.0) + (move * 8.0)
        elif self.scenario == "down":
            prob = 0.46 - 0.11 * min(second / 180.0, 1.0) + (move * 8.0)
        else:
            prob = 0.50 + (move * 6.0)
        return max(0.20, min(0.80, round(prob, 2)))

    def orderbook_at(self, second: int) -> OrderbookSnapshot:
        fair_yes = self.market_prob_at(second)
        yes_bid = max(0.05, min(0.94, round(fair_yes - 0.01, 2)))
        no_bid = max(0.05, min(0.94, round(1.00 - yes_bid - 0.02, 2)))
        ob = OrderbookSnapshot()
        ob.yes_best_bid = yes_bid
        ob.no_best_bid = no_bid
        ob.yes_best_ask = round(min(0.95, yes_bid + 0.01), 2)
        ob.no_best_ask = round(min(0.95, no_bid + 0.01), 2)
        return ob

    def outcome(self) -> str:
        final_price = self.price_at(299)
        return "UP" if final_price >= self.open_price else "DOWN"


class FakeClobClient:
    def __init__(self, market: SyntheticFiveMinuteMarket, clock: FakeClock, window: Window, seed: int = 11):
        self.market = market
        self.clock = clock
        self.window = window
        self.rng = random.Random(seed)
        self.orders: dict[str, dict] = {}
        self.next_id = 1
        self.metrics = {
            "posted_gtc_buy": 0,
            "filled_gtc_buy": 0,
            "fok_sells": 0,
            "gtc_cancels": 0,
            "sell_proceeds": 0.0,
        }

    def _oid(self) -> str:
        oid = f"sim_{self.next_id:05d}"
        self.next_id += 1
        return oid

    def _side_up(self, token_id: str) -> bool:
        return token_id == self.window.yes_token_id

    def _book(self) -> OrderbookSnapshot:
        second = max(int(self.clock.time() - self.window.open_ts), 0)
        return self.market.orderbook_at(second)

    def create_order(self, args, options):
        return args

    def post_order(self, signed, order_type):
        order_type_name = getattr(order_type, "name", str(order_type))
        oid = self._oid()
        book = self._book()
        side_up = self._side_up(signed.token_id)
        bid = book.yes_best_bid if side_up else book.no_best_bid
        ask = book.yes_best_ask if side_up else book.no_best_ask

        order = {
            "order_id": oid,
            "token_id": signed.token_id,
            "price": float(signed.price),
            "size": int(signed.size),
            "side": signed.side,
            "type": order_type_name,
            "status": "OPEN",
            "created_at": self.clock.time(),
            "size_matched": 0,
        }

        if signed.side == BUY:
            if order_type_name == "GTC":
                self.metrics["posted_gtc_buy"] += 1
                self.orders[oid] = order
                return {"orderID": oid}
            if order["price"] >= ask:
                order["status"] = "FILLED"
                order["size_matched"] = order["size"]
                self.orders[oid] = order
                return {"orderID": oid}
            return {}

        if order_type_name == "FOK" and order["price"] <= bid:
            order["status"] = "FILLED"
            order["size_matched"] = order["size"]
            self.orders[oid] = order
            self.metrics["fok_sells"] += 1
            self.metrics["sell_proceeds"] += round(order["price"] * order["size"], 2)
            return {"orderID": oid}

        return {}

    def _maybe_fill_buy_gtc(self, order: dict) -> None:
        if order["status"] != "OPEN" or order["side"] != BUY:
            return
        book = self._book()
        side_up = self._side_up(order["token_id"])
        bid = book.yes_best_bid if side_up else book.no_best_bid
        ask = book.yes_best_ask if side_up else book.no_best_ask
        age = max(self.clock.time() - order["created_at"], 0.0)
        price = order["price"]

        fill_prob = 0.0
        if price >= ask:
            fill_prob = 1.0
        elif price >= bid:
            fill_prob = 0.20
        elif price >= round(bid - 0.01, 2):
            fill_prob = 0.07
        if age >= 6.0:
            fill_prob += 0.05

        if self.rng.random() < min(fill_prob, 1.0):
            order["status"] = "FILLED"
            order["size_matched"] = order["size"]
            self.metrics["filled_gtc_buy"] += 1

    def get_order(self, oid: str):
        order = self.orders.get(oid)
        if not order:
            return {"status": "UNKNOWN"}
        if order["type"] == "GTC" and order["side"] == BUY:
            self._maybe_fill_buy_gtc(order)
        status = order["status"]
        if status == "FILLED":
            return {"status": "FILLED", "size_matched": str(order["size_matched"])}
        if status == "CANCELED":
            return {"status": "CANCELED"}
        return {"status": "OPEN"}

    def cancel(self, oid: str):
        order = self.orders.get(oid)
        if order and order["status"] == "OPEN":
            order["status"] = "CANCELED"
            self.metrics["gtc_cancels"] += 1
        return {"success": True}

    cancel_order = cancel


def _make_window(asset: str = "BTC", open_ts: int = 1_000_000) -> Window:
    window = Window(open_ts=open_ts, close_ts=open_ts + 300, asset=asset)
    window.slug = f"{asset.lower()}-updown-5m-{open_ts}"
    window.yes_token_id = f"{asset.lower()}_yes"
    window.no_token_id = f"{asset.lower()}_no"
    window.open_price = 50_000.0
    return window


def _make_state(window: Window) -> AssetState:
    tracker = WindowTracker(entry_seconds=120, asset=window.asset, window_seconds=300)
    tracker.current = window
    state = AssetState(asset=window.asset, tracker=tracker, bayesian=BayesianUpdater(BaseRateTable()))
    state.price_history = deque([window.open_price] * 50, maxlen=200)
    state.vol_history = deque([0.0] * 12, maxlen=12)
    return state


def _make_settings(mode: str, budget: float):
    settings = MagicMock()
    settings.mode = mode
    settings.enabled_pairs = [("BTC", 300)]
    settings.early_entry_enabled = True
    settings.early_entry_max_bet = budget
    settings.early_entry_reprice_stale_after_seconds = 6.0
    settings.early_entry_reprice_price_tolerance = 0.01
    settings.bankroll = 1000.0
    settings.kelly_fraction = 0.25
    settings.min_trade_usd = 1.0
    settings.max_trade_usd = 10.0
    return settings


def _make_bot(market: SyntheticFiveMinuteMarket, clock: FakeClock, budget: float, window: Window) -> tuple[TradingLoop, FakeClobClient]:
    with patch("polybot.core.loop.TradingLoop.__init__", return_value=None):
        bot = TradingLoop.__new__(TradingLoop)

    bot.settings = _make_settings(mode="live", budget=budget)
    bot._early_traded_slugs = set()
    bot.asset_states = {"BTC": MagicMock()}

    client = FakeClobClient(market=market, clock=clock, window=window)
    bot.trader = SimpleNamespace(client=client)
    bot.model_server = SimpleNamespace(predict=lambda _asset, _features: market.model_prob_at(max(int(clock.time() - window.open_ts), 0)))
    bot.db = MagicMock()
    bot.dynamo = SimpleNamespace(_available=False, _trades=MagicMock())
    bot.risk = MagicMock()
    bot.risk.can_trade.return_value = True

    async def _noop(*_args, **_kwargs):
        return None

    bot._refresh_orderbook = _noop
    bot._v2_check_secrets_refresh = _noop
    bot._log_activity = lambda *_args, **_kwargs: None
    return bot, client


def _avg(cost: float, shares: float) -> float:
    return round(cost / shares, 3) if shares > 0 else 0.0


def _checkpoint(bot: TradingLoop, state: AssetState, market: SyntheticFiveMinuteMarket, second: int) -> Checkpoint:
    return Checkpoint(
        second=second,
        price=market.price_at(second),
        prob_up=market.model_prob_at(second),
        yes_bid=state.orderbook.yes_best_bid,
        no_bid=state.orderbook.no_best_bid,
        up_shares=int(state.early_up_shares),
        down_shares=int(state.early_down_shares),
        up_avg=_avg(state.early_up_cost, state.early_up_shares),
        down_avg=_avg(state.early_down_cost, state.early_down_shares),
        net_cost=round(bot._v2_filled_position_cost_usd(state), 2),
        reserved=round(bot._v2_reserved_open_order_usd(state), 2),
        remaining=round(bot._v2_remaining_budget(state), 2),
        open_orders=len(bot._v2_open_orders(state)),
    )


async def run_window(scenario: str, budget: float, disable_next_window_at: int | None) -> None:
    window = _make_window()
    clock = FakeClock(window.open_ts)
    market = SyntheticFiveMinuteMarket(scenario=scenario, open_price=window.open_price)
    bot, client = _make_bot(market=market, clock=clock, budget=budget, window=window)
    state = _make_state(window)

    checkpoints: dict[int, Checkpoint] = {}
    interesting_seconds = {5, 15, 60, 120, 180, 240, 250, 299}

    with patch("polybot.core.loop.time.time", new=clock.time):
        for second in range(0, 300):
            clock.set(window.open_ts + second)
            state.orderbook = market.orderbook_at(second)
            price = market.price_at(second)
            state.price_history.append(price)

            if second == 5:
                await bot._v2_open_position(state, price)

            if state.early_position and 5 <= second <= 270:
                await bot._v2_poll_fills(state)
                if second == 15 and not state.early_confirm_done:
                    state.early_confirm_done = True
                    await bot._v2_confirm(state, price)
                await bot._v2_execution_tick(state, price, float(second))
                await bot._v2_poll_fills(state)

            if disable_next_window_at is not None and second == disable_next_window_at:
                bot.settings.early_entry_enabled = False
                bot._v2_graceful_stop_requested = True

            if second in interesting_seconds:
                checkpoints[second] = _checkpoint(bot, state, market, second)

    outcome = market.outcome()
    up_payout = float(state.early_up_shares)
    down_payout = float(state.early_down_shares)
    net_cost = bot._v2_filled_position_cost_usd(state)
    up_pnl = round(up_payout - net_cost, 2)
    down_pnl = round(down_payout - net_cost, 2)

    print()
    print("=" * 88)
    print(f"SCENARIO {scenario.upper()} | budget=${budget:.2f} | outcome={outcome}")
    print("=" * 88)
    print(
        "orders_posted="
        f"{client.metrics['posted_gtc_buy']} "
        f"filled_buys={client.metrics['filled_gtc_buy']} "
        f"fok_sells={client.metrics['fok_sells']} "
        f"cancels={client.metrics['gtc_cancels']} "
        f"sell_proceeds=${client.metrics['sell_proceeds']:.2f}"
    )
    print(
        "final_position="
        f"UP {int(state.early_up_shares)} @ ${_avg(state.early_up_cost, state.early_up_shares):.3f}, "
        f"DOWN {int(state.early_down_shares)} @ ${_avg(state.early_down_cost, state.early_down_shares):.3f}, "
        f"net_cost=${net_cost:.2f}"
    )
    print(f"pnl_if_up=${up_pnl:.2f} | pnl_if_down=${down_pnl:.2f}")

    print()
    print("ROUTE TRACE")
    for second in sorted(checkpoints):
        cp = checkpoints[second]
        print(
            f"T+{cp.second:>3}: price={cp.price:>8.2f} prob_up={cp.prob_up:.2f} "
            f"YES={cp.yes_bid:.2f} NO={cp.no_bid:.2f} | "
            f"UP={cp.up_shares:>3} @{cp.up_avg:.3f} "
            f"DOWN={cp.down_shares:>3} @{cp.down_avg:.3f} | "
            f"net=${cp.net_cost:>6.2f} reserved=${cp.reserved:>5.2f} "
            f"remaining=${cp.remaining:>5.2f} open_orders={cp.open_orders}"
        )

    if disable_next_window_at is not None:
        print()
        print(
            f"graceful_stop_simulated_at=T+{disable_next_window_at} "
            f"early_entry_enabled={bot.settings.early_entry_enabled}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate the current V2 5m execution path locally.")
    parser.add_argument("--scenario", choices=["up", "down", "range"], default="up")
    parser.add_argument("--budget", type=float, default=50.0)
    parser.add_argument("--disable-next-window-at", type=int, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    asyncio.run(run_window(args.scenario, args.budget, args.disable_next_window_at))


if __name__ == "__main__":
    main()
