import asyncio
import unittest

from app.pipeline import Pipeline, aggregate, heuristic_claims, score_claim, ClaimResult
from app.store import Store
from tests.fakes import FakeLLM, FakeSearch


def run(coro):
    return asyncio.run(coro)


class ScoringTests(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(score_claim("current", 1, 3), 100)
        self.assertEqual(score_claim("outdated", 1, 3), 0)
        self.assertEqual(score_claim("unverifiable", 0, 3), 50)

    def test_no_sources_is_capped(self):
        self.assertEqual(score_claim("current", 1, 0), 30)

    def test_confidence_clamped(self):
        self.assertEqual(score_claim("current", 5, 2), 100)
        self.assertEqual(score_claim("outdated", -3, 2), 50)

    def test_aggregate_empty(self):
        self.assertEqual(aggregate([], 40)[:2], (100, False))

    def test_aggregate_flags_outdated(self):
        c = ClaimResult(text="x", verdict="outdated", freshness=10)
        score, stale, _ = aggregate([c], 40)
        self.assertTrue(stale)
        self.assertEqual(score, 10)


class HeuristicTests(unittest.TestCase):
    def test_detects_leadership_and_price(self):
        got = heuristic_claims("The CEO of Acme is Jane Doe. The sky is blue today in general. It costs $20 per month.")
        cats = {c.category for c in got}
        self.assertIn("leadership", cats)
        self.assertIn("pricing", cats)

    def test_ignores_timeless(self):
        self.assertEqual(heuristic_claims("Water boils when heated enough."), [])


class PipelineTests(unittest.TestCase):
    def test_outdated_claim_is_flagged(self):
        r = run(Pipeline(FakeLLM(), FakeSearch()).run("The Pro plan costs $20/month."))
        self.assertTrue(r.is_stale)
        self.assertEqual(r.claims[0]["verdict"], "outdated")
        self.assertEqual(r.claims[0]["sources"][0]["url"], "https://example.com/pricing")
        self.assertFalse(r.used_fallback)

    def test_current_claim_scores_high(self):
        r = run(Pipeline(FakeLLM("current", 0.9), FakeSearch()).run("x"))
        self.assertFalse(r.is_stale)
        self.assertGreaterEqual(r.score, 90)

    def test_llm_failure_uses_heuristics(self):
        r = run(Pipeline(FakeLLM(fail_extract=True), FakeSearch()).run("The CEO of Acme is Jane Doe."))
        self.assertTrue(r.used_fallback)
        self.assertEqual(len(r.claims), 1)

    def test_search_failure_degrades_gracefully(self):
        r = run(Pipeline(FakeLLM(), FakeSearch(fail=True)).run("The Pro plan costs $20/month."))
        self.assertEqual(r.claims[0]["verdict"], "unverifiable")
        self.assertLessEqual(r.claims[0]["freshness"], 30)

    def test_bad_verdict_becomes_unverifiable(self):
        r = run(Pipeline(FakeLLM("banana", 1), FakeSearch()).run("x"))
        self.assertEqual(r.claims[0]["verdict"], "unverifiable")


class StoreTests(unittest.TestCase):
    def test_metrics_roundtrip(self):
        store = Store(":memory:")
        rep = run(Pipeline(FakeLLM(), FakeSearch()).run("The Pro plan costs $20/month.")).to_dict()
        store.add("test", "q", "a", rep)
        m = store.metrics()
        self.assertEqual(m["total_checks"], 1)
        self.assertEqual(m["stale_answers"], 1)
        self.assertEqual(m["by_category"], {"pricing": 1})
        self.assertEqual(m["top_sources"][0][0], "example.com")
        self.assertEqual(store.logs(5)[0]["report"]["claims"][0]["verdict"], "outdated")


if __name__ == "__main__":
    unittest.main()
