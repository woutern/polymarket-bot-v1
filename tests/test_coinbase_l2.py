"""Tests for Coinbase level2 orderbook features."""

from __future__ import annotations

import time

from polybot.feeds.coinbase_ws import CoinbaseWS


def _make_ws() -> CoinbaseWS:
    return CoinbaseWS(assets=["BTC", "ETH", "SOL"])


class TestOFI:
    def test_returns_zero_when_empty(self):
        ws = _make_ws()
        assert ws.get_ofi_30s("BTC") == 0.0

    def test_computes_correctly(self):
        ws = _make_ws()
        now = time.time()
        # 3 buy events, 1 sell event
        ws._ofi_events["BTC"].append((now, 10.0, 0.0))
        ws._ofi_events["BTC"].append((now, 5.0, 0.0))
        ws._ofi_events["BTC"].append((now, 0.0, 5.0))
        ofi = ws.get_ofi_30s("BTC")
        # (10+5+0 - 0+0+5) / (10+5+0 + 0+0+5) = 10/20 = 0.5
        assert abs(ofi - 0.5) < 0.01

    def test_ignores_old_events(self):
        ws = _make_ws()
        old = time.time() - 60  # 60s ago — outside 30s window
        ws._ofi_events["BTC"].append((old, 100.0, 0.0))
        assert ws.get_ofi_30s("BTC") == 0.0

    def test_per_asset_independent(self):
        ws = _make_ws()
        now = time.time()
        ws._ofi_events["BTC"].append((now, 10.0, 0.0))
        ws._ofi_events["ETH"].append((now, 0.0, 10.0))
        assert ws.get_ofi_30s("BTC") > 0
        assert ws.get_ofi_30s("ETH") < 0


class TestBidAskSpread:
    def test_returns_zero_when_no_data(self):
        ws = _make_ws()
        assert ws.get_bid_ask_spread("BTC") == 0.0

    def test_correct_calculation(self):
        ws = _make_ws()
        ws.best_bids["BTC"] = 100.0
        ws.best_asks["BTC"] = 100.10
        spread = ws.get_bid_ask_spread("BTC")
        # (100.10 - 100.0) / 100.05 ≈ 0.001
        assert 0.0009 < spread < 0.0011


class TestDepthImbalance:
    def test_returns_zero_when_no_data(self):
        ws = _make_ws()
        assert ws.get_depth_imbalance("BTC") == 0.0

    def test_balanced_depth(self):
        ws = _make_ws()
        ws.bid_depth_5["BTC"] = 10.0
        ws.ask_depth_5["BTC"] = 10.0
        assert ws.get_depth_imbalance("BTC") == 0.0

    def test_bid_heavy(self):
        ws = _make_ws()
        ws.bid_depth_5["BTC"] = 20.0
        ws.ask_depth_5["BTC"] = 10.0
        imb = ws.get_depth_imbalance("BTC")
        # (20-10)/(20+10) = 0.333
        assert 0.32 < imb < 0.34

    def test_bounds(self):
        ws = _make_ws()
        ws.bid_depth_5["BTC"] = 100.0
        ws.ask_depth_5["BTC"] = 0.0
        assert ws.get_depth_imbalance("BTC") == 1.0
        ws.bid_depth_5["BTC"] = 0.0
        ws.ask_depth_5["BTC"] = 100.0
        assert ws.get_depth_imbalance("BTC") == -1.0


class TestTradeArrivalRate:
    def test_returns_zero_when_empty(self):
        ws = _make_ws()
        assert ws.get_trade_arrival_rate("BTC") == 0.0

    def test_correct_rate(self):
        ws = _make_ws()
        now = time.time()
        # 30 trades in last 30s = 1/s
        for i in range(30):
            ws._trade_times["BTC"].append(now - i)
        rate = ws.get_trade_arrival_rate("BTC")
        assert 0.9 < rate < 1.1


class TestL2Handler:
    def test_snapshot_populates_books(self):
        ws = _make_ws()
        msg = {
            "channel": "l2_data",
            "events": [{
                "type": "snapshot",
                "product_id": "BTC-USD",
                "updates": [
                    {"side": "bid", "price_level": "100.00", "new_quantity": "1.5"},
                    {"side": "bid", "price_level": "99.00", "new_quantity": "2.0"},
                    {"side": "offer", "price_level": "101.00", "new_quantity": "1.0"},
                    {"side": "offer", "price_level": "102.00", "new_quantity": "0.5"},
                ],
            }],
        }
        ws._handle_message(msg)
        assert ws.best_bids["BTC"] == 100.0
        assert ws.best_asks["BTC"] == 101.0
        assert ws.bid_depth_5["BTC"] == 3.5  # 1.5 + 2.0
        assert ws.ask_depth_5["BTC"] == 1.5  # 1.0 + 0.5

    def test_update_applies_deltas(self):
        ws = _make_ws()
        # First a snapshot
        snap = {
            "channel": "l2_data",
            "events": [{
                "type": "snapshot",
                "product_id": "BTC-USD",
                "updates": [
                    {"side": "bid", "price_level": "100.00", "new_quantity": "1.0"},
                    {"side": "offer", "price_level": "101.00", "new_quantity": "1.0"},
                ],
            }],
        }
        ws._handle_message(snap)
        assert ws.bid_depth_5["BTC"] == 1.0

        # Then an update
        update = {
            "channel": "l2_data",
            "events": [{
                "type": "update",
                "product_id": "BTC-USD",
                "updates": [
                    {"side": "bid", "price_level": "100.00", "new_quantity": "3.0"},  # increase
                ],
            }],
        }
        ws._handle_message(update)
        assert ws.bid_depth_5["BTC"] == 3.0

    def test_remove_level(self):
        ws = _make_ws()
        snap = {
            "channel": "l2_data",
            "events": [{
                "type": "snapshot",
                "product_id": "ETH-USD",
                "updates": [
                    {"side": "bid", "price_level": "3000.00", "new_quantity": "5.0"},
                    {"side": "offer", "price_level": "3001.00", "new_quantity": "2.0"},
                ],
            }],
        }
        ws._handle_message(snap)
        # Remove the bid level
        update = {
            "channel": "l2_data",
            "events": [{
                "type": "update",
                "product_id": "ETH-USD",
                "updates": [
                    {"side": "bid", "price_level": "3000.00", "new_quantity": "0"},
                ],
            }],
        }
        ws._handle_message(update)
        assert ws.bid_depth_5["ETH"] == 0.0

    def test_ofi_tracked_on_updates(self):
        ws = _make_ws()
        msg = {
            "channel": "l2_data",
            "events": [{
                "type": "update",
                "product_id": "BTC-USD",
                "updates": [
                    {"side": "bid", "price_level": "100.00", "new_quantity": "5.0"},
                ],
            }],
        }
        ws._handle_message(msg)
        # Should have recorded an OFI event (buy pressure)
        assert len(ws._ofi_events["BTC"]) > 0
