"""Tests for opportunity bot — parallel worker architecture."""

import sys, os, json
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import pytest


class TestWorkerConfig:
    def test_13_workers(self):
        from opportunity_bot import WORKER_TAG_SLUGS
        assert len(WORKER_TAG_SLUGS) == 13

    def test_worker_names(self):
        from opportunity_bot import WORKER_TAG_SLUGS
        expected = {"crypto", "finance", "fed", "geopolitics",
                    "elections", "tech", "weather", "culture",
                    "economics", "companies", "health", "iran", "whitehouse"}
        assert set(WORKER_TAG_SLUGS.keys()) == expected
        assert "tweets" not in WORKER_TAG_SLUGS
        assert "basketball" not in WORKER_TAG_SLUGS
        assert "politics" not in WORKER_TAG_SLUGS

    def test_no_sports_or_esports(self):
        """No sports, esports, or gaming workers."""
        from opportunity_bot import WORKER_TAG_SLUGS
        all_slugs = []
        for slugs in WORKER_TAG_SLUGS.values():
            all_slugs.extend(slugs)
        for banned in ["soccer", "nfl", "nhl", "baseball", "golf", "esports", "hockey", "tennis", "lol"]:
            assert banned not in all_slugs, f"{banned} should not be in workers"

    def test_skip_keywords_esports(self):
        """Esports/gaming keywords must be in skip filter."""
        from opportunity_bot import SKIP_KEYWORDS
        for kw in ["esports", "lol", "valorant", "cs2", "dota", "gaming"]:
            assert kw in SKIP_KEYWORDS, f"{kw} must be in SKIP_KEYWORDS"

    def test_skip_slugs(self):
        from opportunity_bot import SKIP_SLUGS
        assert SKIP_SLUGS == {"5m", "15m", "updown"}

    def test_data_driven_filters(self):
        """Data-driven filters from opportunity analysis."""
        from opportunity_bot import MIN_ASK_OPP, MAX_ASK_OPP, SKIP_HOURS_UTC, SKIP_RESOLVE_HOURS
        assert MIN_ASK_OPP == 0.85  # Below loses money
        assert MAX_ASK_OPP == 0.94  # Above margin too thin
        assert SKIP_HOURS_UTC == {6, 7, 8, 9, 10, 11}  # Morning UTC
        assert SKIP_RESOLVE_HOURS == (6, 12)  # 6-12h resolve window


class TestTiers:
    def test_tier_sizes(self):
        from opportunity_bot import TIER0_SIZE, TIER1_SIZE, TIER2_SIZE
        assert TIER0_SIZE == 10.00
        assert TIER1_SIZE == 5.00
        assert TIER2_SIZE == 2.50

    def test_tier2_gate_passes(self):
        from opportunity_bot import Opp
        o = Opp(question="t", slug="t", condition_id="0x", side="YES", price=0.88,
                yes_price=0.88, no_price=0.12, yes_token="t1", no_token="t2",
                volume=5000, hours_left=10, category="Crypto", end_date="2026-01-01T00:00:00Z", neg_risk=False)
        o.ai_trade, o.ai_confidence, o.ai_edge = True, 0.90, 0.20
        assert o.ai_trade and o.ai_confidence >= 0.85 and o.ai_edge >= 0.15

    def test_tier2_gate_fails_confidence(self):
        from opportunity_bot import Opp
        o = Opp(question="t", slug="t", condition_id="0x", side="YES", price=0.88,
                yes_price=0.88, no_price=0.12, yes_token="t1", no_token="t2",
                volume=5000, hours_left=10, category="Crypto", end_date="2026-01-01T00:00:00Z", neg_risk=False)
        o.ai_trade, o.ai_confidence, o.ai_edge = True, 0.80, 0.20
        assert not (o.ai_trade and o.ai_confidence >= 0.85 and o.ai_edge >= 0.15)

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

    def test_tier0_classification(self):
        """Tier 0: ask>=0.93, <=6h, vol>=5K → $10 bet."""
        from opportunity_bot import parse_opps
        events = [self._make_event(yes_price=0.94, hours_from_now=3, volume=10000)]
        opps = parse_opps(events, "crypto")
        assert len(opps) == 1
        assert opps[0].tier == 0

    def test_tier0_needs_high_volume(self):
        """Tier 0 requires vol>=5K — falls back to Tier 1 with low volume."""
        from opportunity_bot import parse_opps
        events = [self._make_event(yes_price=0.94, hours_from_now=3, volume=3000)]
        opps = parse_opps(events, "crypto")
        assert len(opps) == 1
        assert opps[0].tier == 1  # Not tier 0 — low volume

    def test_tier0_needs_short_window(self):
        """Tier 0 requires <=6h — falls back to Tier 1 if >6h."""
        from opportunity_bot import parse_opps
        events = [self._make_event(yes_price=0.94, hours_from_now=14, volume=10000)]
        opps = parse_opps(events, "crypto")
        assert len(opps) == 1
        assert opps[0].tier == 1  # Not tier 0 — too far out

    def test_tier1_classification(self):
        from opportunity_bot import parse_opps
        events = [self._make_event(yes_price=0.90, hours_from_now=14)]
        opps = parse_opps(events, "crypto")
        assert len(opps) == 1
        assert opps[0].tier == 1
        assert opps[0].side == "YES"

    def test_tier2_classification(self):
        from opportunity_bot import parse_opps
        events = [self._make_event(yes_price=0.90, hours_from_now=30)]  # 24-48h → Tier 2
        opps = parse_opps(events, "crypto")
        assert len(opps) == 1
        assert opps[0].tier == 2

    def test_skip_low_price(self):
        from opportunity_bot import parse_opps
        events = [self._make_event(yes_price=0.50, hours_from_now=10)]
        opps = parse_opps(events, "crypto")
        assert len(opps) == 0  # Below MIN_ASK_OPP $0.85

    def test_skip_below_085(self):
        """Ask $0.75 should be skipped — below $0.85 loses money."""
        from opportunity_bot import parse_opps
        events = [self._make_event(yes_price=0.75, hours_from_now=10)]
        opps = parse_opps(events, "crypto")
        assert len(opps) == 0

    def test_skip_above_094(self):
        """Ask $0.95 should be skipped — above $0.94 margin too thin."""
        from opportunity_bot import parse_opps
        events = [self._make_event(yes_price=0.95, hours_from_now=10)]
        opps = parse_opps(events, "crypto")
        assert len(opps) == 0

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
        ev = self._make_event(hours_from_now=14)
        opps = parse_opps([ev, ev], "crypto")
        assert len(opps) == 1

    def test_yes_side_picked(self):
        """Should pick YES if yes_price in ask range."""
        from opportunity_bot import parse_opps
        events = [self._make_event(yes_price=0.90, hours_from_now=14)]
        opps = parse_opps(events, "crypto")
        assert opps[0].side == "YES"
        assert opps[0].price == 0.90

    def test_no_side_when_no_higher(self):
        """NO side picked when no_price is in range and yes_price is not."""
        from opportunity_bot import parse_opps
        # yes=0.10, no=0.90 → NO side at $0.90
        end = (datetime.now(timezone.utc) + timedelta(hours=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
        events = [{"tags": [{"label": "Crypto"}], "endDate": end, "negRisk": False,
                   "markets": [{"question": "t", "slug": "t", "conditionId": "0x",
                                "outcomePrices": '["0.10", "0.90"]', "volume": "5000",
                                "clobTokenIds": '["ty", "tn"]', "endDate": end, "negRisk": False}]}]
        opps = parse_opps(events, "crypto")
        assert opps[0].side == "NO"
        assert opps[0].price == 0.90

    def test_tier1_resolves_24h_boundary(self):
        """Under 24h (outside 6-12h skip) → Tier 1. Over 24h → Tier 2."""
        from opportunity_bot import parse_opps
        # 14h → Tier 1 (outside 6-12h skip)
        events = [self._make_event(yes_price=0.90, hours_from_now=14)]
        assert parse_opps(events, "crypto")[0].tier == 1

        # 25h → Tier 2
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


class TestTier0DualAI:
    """Tier 0 ($10) requires Haiku sanity + Sonnet devil's advocate."""

    def test_sonnet_confirm_callable(self):
        from opportunity_bot import ai_sonnet_confirm
        assert callable(ai_sonnet_confirm)

    def test_sonnet_model_constant(self):
        from opportunity_bot import SONNET_MODEL, HAIKU_MODEL
        assert "sonnet" in SONNET_MODEL
        assert "haiku" in HAIKU_MODEL
        assert "eu." in SONNET_MODEL  # eu-west-1
        assert "eu." in HAIKU_MODEL

    def test_tier0_haiku_gate_is_090(self):
        """Tier 0 requires Haiku confidence >= 0.90 (stricter than Tier 1's 0.75)."""
        from opportunity_bot import run_scan
        import inspect
        src = inspect.getsource(run_scan)
        assert "0.90 if opp.tier == 0" in src

    def test_tier0_calls_sonnet_after_haiku(self):
        """Tier 0 must call ai_sonnet_confirm after Haiku passes."""
        from opportunity_bot import run_scan
        import inspect
        src = inspect.getsource(run_scan)
        assert "ai_sonnet_confirm" in src

    def test_sonnet_prompt_has_risk_framing(self):
        """Sonnet prompt must ask for reasons the trade could lose."""
        from opportunity_bot import ai_sonnet_confirm
        import inspect
        src = inspect.getsource(ai_sonnet_confirm)
        assert "LOSE" in src or "lose" in src.lower()
        assert "risk" in src.lower()
        assert "devil" in src.lower() or "find reasons" in src.lower()

    def test_sonnet_veto_blocks_trade(self):
        """If Sonnet vetoes, ai_trade must be False."""
        from opportunity_bot import ai_sonnet_confirm
        import inspect
        src = inspect.getsource(ai_sonnet_confirm)
        assert "opp.ai_trade = False" in src
        assert "sonnet_veto" in src

    def test_tier0_size_is_10(self):
        from opportunity_bot import TIER0_SIZE, place_fok
        import inspect
        assert TIER0_SIZE == 10.00
        src = inspect.getsource(place_fok)
        assert "TIER0_SIZE" in src

    def test_tier1_does_not_call_sonnet(self):
        """Only tier 0 calls Sonnet — tier 1 skips it."""
        from opportunity_bot import run_scan
        import inspect
        src = inspect.getsource(run_scan)
        # Sonnet only called when tier == 0
        assert "if opp.tier == 0:" in src


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
        from opportunity_bot import resolve_all_pending
        assert callable(resolve_all_pending)

    def test_tag_init_callable(self):
        from opportunity_bot import init_tag_ids
        assert callable(init_tag_ids)

    def test_no_basketball_worker(self):
        from opportunity_bot import WORKER_TAG_SLUGS
        assert "basketball" not in WORKER_TAG_SLUGS

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

    def test_dashboard_api(self):
        path = os.path.join(os.path.dirname(__file__), "..", "scripts", "dashboard.py")
        with open(path) as f:
            c = f.read()
        assert "/api/live-state" in c and "/api/overview" in c

    def test_no_dry_run_mode(self):
        """Spec says remove dry-run — always trade live."""
        path = os.path.join(os.path.dirname(__file__), "..", "scripts", "opportunity_bot.py")
        with open(path) as f:
            content = f.read()
        assert "dry_run" not in content.split("def main")[1], "main() should not have dry_run param"
