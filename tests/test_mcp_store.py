"""Stdlib-only tests: MCP handler, store lifecycle, pipeline timeouts."""
import asyncio
import json
import unittest

from app.mcp import McpHandler, TOOLS
from app.pipeline import Pipeline
from app.store import Store
from tests.fakes import FakeLLM, FakeSearch


def run(coro):
    return asyncio.run(coro)


class Env:
    """In-memory stand-in for the service's submit/get callables."""

    def __init__(self, delay=0.0):
        self.db, self.delay = {}, delay

    async def submit(self, answer, question):
        cid = len(self.db) + 1
        self.db[cid] = {"id": cid, "status": "pending"}

        async def work():
            await asyncio.sleep(self.delay)
            self.db[cid] = {"id": cid, "status": "done", "score": 10, "is_stale": True,
                            "summary": "1 of 1 claims appear outdated.",
                            "report": {"claims": [{"text": "x", "category": "pricing", "verdict": "outdated",
                                                   "freshness": 10, "corrected_fact": "y", "reasoning": "r",
                                                   "sources": [{"url": "https://a.com"}]}]}}
        return cid, asyncio.create_task(work())

    def get(self, cid):
        return self.db.get(cid)


def call(h, method, params=None, mid=1):
    msg = {"jsonrpc": "2.0", "method": method}
    if mid is not None:
        msg["id"] = mid
    if params is not None:
        msg["params"] = params
    return run(h.handle(msg))


class McpTests(unittest.TestCase):
    def setUp(self):
        self.env = Env()
        self.h = McpHandler(self.env.submit, self.env.get)

    def test_initialize_negotiates_version(self):
        r = call(self.h, "initialize", {"protocolVersion": "2025-03-26"})
        self.assertEqual(r["result"]["protocolVersion"], "2025-03-26")
        r = call(self.h, "initialize", {"protocolVersion": "2099-01-01"})
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r["result"]["capabilities"])

    def test_notification_gets_no_response(self):
        self.assertIsNone(call(self.h, "notifications/initialized", mid=None))

    def test_ping_and_list(self):
        self.assertEqual(call(self.h, "ping")["result"], {})
        names = [t["name"] for t in call(self.h, "tools/list")["result"]["tools"]]
        self.assertEqual(names, [t["name"] for t in TOOLS])
        self.assertEqual(names, ["verify_freshness", "get_freshness_report"])

    def test_unknown_method_and_tool(self):
        self.assertEqual(call(self.h, "nope")["error"]["code"], -32601)
        r = call(self.h, "tools/call", {"name": "nope", "arguments": {}})
        self.assertEqual(r["error"]["code"], -32602)

    def test_verify_returns_finished_report(self):
        r = call(self.h, "tools/call", {"name": "verify_freshness", "arguments": {"answer": "a $20 plan"}})
        res = r["result"]
        self.assertFalse(res["isError"])
        body = json.loads(res["content"][0]["text"])
        self.assertEqual(body["status"], "done")
        self.assertTrue(body["is_stale"])
        self.assertEqual(body["claims"][0]["sources"], ["https://a.com"])

    def test_verify_validates_input(self):
        r = call(self.h, "tools/call", {"name": "verify_freshness", "arguments": {"answer": "  "}})
        self.assertTrue(r["result"]["isError"])

    def test_slow_check_returns_pending_then_polls(self):
        env = Env(delay=0.3)
        h = McpHandler(env.submit, env.get)

        async def scenario():
            first = await h.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": "verify_freshness",
                                               "arguments": {"answer": "x", "wait_seconds": 0.05}}})
            pending = json.loads(first["result"]["content"][0]["text"])
            await asyncio.sleep(0.5)
            second = await h.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                     "params": {"name": "get_freshness_report",
                                                "arguments": {"check_id": pending["check_id"]}}})
            return pending, json.loads(second["result"]["content"][0]["text"])

        pending, done = run(scenario())
        self.assertEqual(pending["status"], "pending")
        self.assertEqual(done["status"], "done")

    def test_get_unknown_id(self):
        r = call(self.h, "tools/call", {"name": "get_freshness_report", "arguments": {"check_id": 99}})
        self.assertTrue(r["result"]["isError"])

    def test_wait_is_capped_below_bifrost_default_timeout(self):
        from app.mcp import MAX_WAIT
        self.assertLess(MAX_WAIT, 30)


class StoreLifecycleTests(unittest.TestCase):
    def test_pending_done_failed(self):
        st = Store(":memory:")
        a = st.create_pending("t", "q", "a")
        b = st.create_pending("t", "q", "b")
        self.assertEqual(st.get(a)["status"], "pending")
        self.assertEqual(st.metrics()["pending_checks"], 2)
        rep = run(Pipeline(FakeLLM(), FakeSearch()).run("The Pro plan costs $20/month.")).to_dict()
        st.complete(a, rep)
        st.fail(b, "boom")
        self.assertEqual(st.get(a)["status"], "done")
        self.assertTrue(st.get(a)["is_stale"])
        self.assertEqual(st.get(b)["status"], "failed")
        m = st.metrics()
        self.assertEqual((m["total_checks"], m["pending_checks"]), (1, 0))
        self.assertIsNone(st.get(999))

    def test_restart_sweeps_pending(self):
        import os, tempfile
        path = os.path.join(tempfile.mkdtemp(), "t.db")
        st = Store(path)
        cid = st.create_pending("t", "q", "a")
        st2 = Store(path)  # simulates a restart
        self.assertEqual(st2.get(cid)["status"], "failed")


class SlowSearch:
    async def search(self, query):
        await asyncio.sleep(1)
        return []


class TimeoutTests(unittest.TestCase):
    def test_slow_search_degrades_not_hangs(self):
        pipe = Pipeline(FakeLLM(), SlowSearch(), step_timeout=0.05)
        r = run(pipe.run("The Pro plan costs $20/month."))
        self.assertEqual(r.claims[0]["verdict"], "unverifiable")
        self.assertIn("TimeoutError", r.claims[0]["reasoning"])
        self.assertLess(r.latency_ms, 800)


if __name__ == "__main__":
    unittest.main()
