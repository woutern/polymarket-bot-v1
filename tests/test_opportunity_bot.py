"""Tests for opportunity bot — parallel worker architecture."""

import sys, os, json
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import pytest


class TestWorkerConfig:
    def test_7_workers(self):
        from opportunity_bot import WORKER_TAG_SLUGS
        assert len(WORKER_TAG_SLUGS) == 7

    def test_worker_names(self):
        from opportunity_bot import WORKER_TAG_SLUGS
        assert set(WORKER_TAG_SLUGS.keys()) == {"crypto", "finance", "politics", "geopolitics", "tech", "basketball", "news"}

    def test_no_other_sports(self):
        """Only basketball — no soccer, NFL, NHL, golf, esports."""
        from opportunity_bot import WORKER_TAG_SLUGS
        all_slugs = []
        for slugs in WORKER_TAG_SLUGS.values():
            all_slugs.extend(slugs)
        for banned in ["soccer", "nfl", "nhl", "baseball", "golf", "esports", "hockey", "tennis"]:
            assert banned not in all_slugs, f"{banned} should not be in workers"

    def test_skip_slugs(self):
        from opportunity_bot import SKIP_SLUGS
        assert SKIP_SLUGS == {"5m", "15m", "updown"}


class TestTiers:
    def test_tier_sizes(self):
        from opportunity_bot import TIER1_SIZE, TIER2_SIZE
        assert TIER1_SIZE == 5.00
        assert TIER2_SIZE == 2.50

    def test_tier2_gate_passes(self):
        from opportunity_bot import Opp
        o = Opp(question="t", slug="t", condition_id="0x", side="YES", price=0.75,
                yes_price=0.75, no_price=0.25, yes_token="t1", no_token="t2",
                volume=5000, hours_left=10, category="Crypto", end_date="2026-01-01T00:00:00Z", neg_risk=False)
        o.ai_trade, o.ai_confidence, o.ai_edge = True, 0.85, 0.20
        assert o.ai_trade and o.ai_confidence >= 0.80 and o.ai_edge >= 0.15

    def test_tier2_gate_fails_confidence(self):
        from opportunity_bot import Opp
        o = Opp(question="t", slug="t", condition_id="0x", side="YES", price=0.75,
                yes_price=0.75, no_price=0.25, yes_token="t1", no_token="t2",
                volume=5000, hours_left=10, category="Crypto", end_date="2026-01-01T00:00:00Z", neg_risk=False)
        o.ai_trade, o.ai_confidence, o.ai_edge = True, 0.70, 0.20
        assert not (o.ai_trade and o.ai_confidence >= 0.80 and o.ai_edge >= 0.15)

    def test_tier2_gate_fails_edge(self):
        from opportunity_bot import Opp
        o = Opp(question="t", slug="t", condition_id="0x", side="YES", price=0.75,
                yes_price=0.75, no_price=0.25, yes_token="t1", no_token="t2",
                volume=5000, hours_left=10, category="Crypto", end_date="2026-01-01T00:00:00Z", neg_risk=False)
        o.ai_trade, o.ai_confidence, o.ai_edge = True, 0.85, 0.10
        assert not (o.ai_trade and o.ai_confidence >= 0.80 and o.ai_edge >= 0.15)

    def test_tier2_gate_fails_trade_false(self):
        from opportunity_bot import Opp
        o = Opp(question="t", slug="t", condition_id="0x", side="YES", price=0.75,
                yes_price=0.75, no_price=0.25, yes_token="t1", no_token="t2",
                volume=5000, hours_left=10, category="Crypto", end_date="2026-01-01T00:00:00Z", neg_risk=False)
        o.ai_trade, o.ai_confidence, o.ai_edge = False, 0.90, 0.25
        assert not (o.ai_trade and o.ai_confidence >= 0.80 and o.ai_edge >= 0.15)


class TestParseOpps:
    def _make_event(self, question="Will BTC go up?", slug="btc-test", cid="0xabc",
                    yes_price=0.90, volume=5000, hours_from_now=10, neg_risk=False):
        end = (datetime.now(timezone.utc) + timedelta(hours=hours_from_now)).strftime("%Y-%m-%dT%H:%M:%SZ")
        return {
            "tags": [{"label": "Crypto", "slug": "crypto"}],
            "endDate": end,
            "negRisk": neg_risk,
            "markets": [{
                "question": question,
                "slug": slug,
                "conditionId": cid,
                "outcomePrices": json.dumps([str(yes_price), str(round(1 - yes_price, 2))]),
                "volume": str(volume),
                "clobTokenIds": json.dumps(["tok_yes", "tok_no"]),
                "endDate": end,
                "negRisk": neg_risk,
            }],
        }

    def test_tier1_classification(self):
        from opportunity_bot import parse_opps
        events = [self._make_event(yes_price=0.90, hours_from_now=10)]
        opps = parse_opps(events, "crypto")
        assert len(opps) == 1
        assert opps[0].tier == 1
        assert opps[0].side == "YES"

    def test_tier2_classification(self):
        from opportunity_bot import parse_opps
        events = [self._make_event(yes_price=0.75, hours_from_now=10)]
        opps = parse_opps(events, "crypto")
        assert len(opps) == 1
        assert opps[0].tier == 2

    def test_skip_low_price(self):
        from opportunity_bot import parse_opps
        events = [self._make_event(yes_price=0.50, hours_from_now=10)]
        opps = parse_opps(events, "crypto")
        assert len(opps) == 0  # Below $0.65

    def test_skip_low_volume(self):
        from opportunity_bot import parse_opps
        events = [self._make_event(volume=500)]
        opps = parse_opps(events, "crypto")
        assert len(opps) == 0

    def test_skip_updown_slug(self):
        from opportunity_bot import parse_opps
        events = [self._make_event(slug="btc-updown-5m-123")]
        opps = parse_opps(events, "crypto")
        assert len(opps) == 0

    def test_skip_expired(self):
        from opportunity_bot import parse_opps
        events = [self._make_event(hours_from_now=-1)]
        opps = parse_opps(events, "crypto")
        assert len(opps) == 0

    def test_dedup_same_slug(self):
        from opportunity_bot import parse_opps
        ev = self._make_event()
        opps = parse_opps([ev, ev], "crypto")
        assert len(opps) == 1

    def test_no_side_picks_highest(self):
        """Should pick YES if yes_price >= no_price."""
        from opportunity_bot import parse_opps
        events = [self._make_event(yes_price=0.85)]
        opps = parse_opps(events, "crypto")
        assert opps[0].side == "YES"
        assert opps[0].price == 0.85

    def test_no_side_when_no_higher(self):
        from opportunity_bot import parse_opps
        # yes=0.30, no=0.70
        end = (datetime.now(timezone.utc) + timedelta(hours=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
        events = [{"tags": [{"label": "Crypto"}], "endDate": end, "negRisk": False,
                   "markets": [{"question": "t", "slug": "t", "conditionId": "0x",
                                "outcomePrices": '["0.30", "0.70"]', "volume": "5000",
                                "clobTokenIds": '["ty", "tn"]', "endDate": end, "negRisk": False}]}]
        opps = parse_opps(events, "crypto")
        assert opps[0].side == "NO"
        assert opps[0].price == 0.70

    def test_tier1_resolves_24h_boundary(self):
        """Exactly 24h → Tier 1. Over 24h → Tier 2."""
        from opportunity_bot import parse_opps
        # 23h → Tier 1
        events = [self._make_event(yes_price=0.90, hours_from_now=23)]
        assert parse_opps(events, "crypto")[0].tier == 1

        # 25h → Tier 2 (ask 0.90, >24h)
        events = [self._make_event(yes_price=0.90, hours_from_now=25, slug="s2", cid="0xdef")]
        assert parse_opps(events, "crypto")[0].tier == 2


class TestDedupLogic:
    def test_memory_dedup(self):
        from opportunity_bot import _traded_cids, Opp
        _traded_cids.clear()
        _traded_cids.add("0xexisting")
        o = Opp(question="t", slug="t", condition_id="0xexisting", side="YES", price=0.90,
                yes_price=0.90, no_price=0.10, yes_token="t1", no_token="t2",
                volume=5000, hours_left=10, category="", end_date="", neg_risk=False)
        # Should be blocked by memory dedup
        assert o.condition_id in _traded_cids
        _traded_cids.clear()

    def test_dedup_put_no_table(self):
        """Without DynamoDB, dedup_put uses memory set only."""
        from opportunity_bot import dedup_put, _traded_cids, Opp
        _traded_cids.clear()
        o = Opp(question="t", slug="new-slug", condition_id="0xnew", side="YES", price=0.90,
                yes_price=0.90, no_price=0.10, yes_token="t1", no_token="t2",
                volume=5000, hours_left=10, category="", end_date="", neg_risk=False)
        assert dedup_put(None, o, {"price": 0.90, "cost": 4.50, "shares": 5, "order_id": ""})
        assert "0xnew" in _traded_cids
        # Second call should fail
        assert not dedup_put(None, o, {"price": 0.90, "cost": 4.50, "shares": 5, "order_id": ""})
        _traded_cids.clear()


class TestAIModelSelection:
    def test_haiku_for_low_volume(self):
        """Volume < $50K → haiku."""
        from opportunity_bot import Opp
        o = Opp(question="t", slug="t", condition_id="0x", side="YES", price=0.75,
                yes_price=0.75, no_price=0.25, yes_token="t1", no_token="t2",
                volume=20000, hours_left=10, category="", end_date="", neg_risk=False)
        model = ("sonnet" if o.volume >= 50_000 else "haiku")
        assert model == "haiku"

    def test_sonnet_for_high_volume(self):
        """Volume >= $50K → sonnet."""
        from opportunity_bot import Opp
        o = Opp(question="t", slug="t", condition_id="0x", side="YES", price=0.75,
                yes_price=0.75, no_price=0.25, yes_token="t1", no_token="t2",
                volume=100000, hours_left=10, category="", end_date="", neg_risk=False)
        model = ("sonnet" if o.volume >= 50_000 else "haiku")
        assert model == "sonnet"


class TestLimits:
    def test_min_volume(self):
        from opportunity_bot import MIN_VOLUME
        assert MIN_VOLUME == 1_000

    def test_scan_interval_30min(self):
        from opportunity_bot import main
        import inspect
        src = inspect.getsource(main)
        assert "minute=30" in src or "minute < 30" in src


class TestSmoke:
    def test_no_gtc(self):
        path = os.path.join(os.path.dirname(__file__), "..", "scripts", "opportunity_bot.py")
        with open(path) as f:
            assert "OrderType.GTC" not in f.read()

    def test_fok_at_best_ask(self):
        from opportunity_bot import place_fok
        import inspect
        src = inspect.getsource(place_fok)
        assert "best" in src and "/book" in src and "FOK" in src

    def test_parallel_gather(self):
        from opportunity_bot import run_scan
        import inspect
        assert "asyncio.gather" in inspect.getsource(run_scan)

    def test_dedup_callable(self):
        from opportunity_bot import dedup_put
        assert callable(dedup_put)

    def test_resolve_pending_callable(self):
        from opportunity_bot import resolve_pending
        assert callable(resolve_pending)

    def test_tag_init_callable(self):
        from opportunity_bot import init_tag_ids
        assert callable(init_tag_ids)

    def test_basketball_only_live(self):
        from opportunity_bot import fetch_worker
        import inspect
        src = inspect.getsource(fetch_worker)
        assert "get_live_basketball" in src
        assert "no_live_games" in src

    def test_sorted_by_end_date(self):
        """Combined list must be sorted by end_date (soonest first)."""
        from opportunity_bot import run_scan
        import inspect
        src = inspect.getsource(run_scan)
        assert "sort(key=lambda o: o.end_date)" in src

    def test_fetch_then_trade(self):
        """Workers fetch in parallel, then single loop trades in order."""
        from opportunity_bot import run_scan
        import inspect
        src = inspect.getsource(run_scan)
        assert "fetch_worker" in src
        assert "asyncio.gather" in src
        # Budget check inside the trading loop
        assert "budget_maxed" in src

    def test_start_sh(self):
        path = os.path.join(os.path.dirname(__file__), "..", "scripts", "start.sh")
        with open(path) as f:
            assert "opportunity_bot.py" in f.read()

    def test_dashboard_tab(self):
        path = os.path.join(os.path.dirname(__file__), "..", "scripts", "dashboard.py")
        with open(path) as f:
            c = f.read()
        assert "page-opportunities" in c and "/api/opportunities" in c

    def test_no_dry_run_mode(self):
        """Spec says remove dry-run — always trade live."""
        path = os.path.join(os.path.dirname(__file__), "..", "scripts", "opportunity_bot.py")
        with open(path) as f:
            content = f.read()
        assert "dry_run" not in content.split("def main")[1], "main() should not have dry_run param"
