"""API tests. Require the packages in requirements.txt (fastapi, httpx)."""
import json
import unittest

import httpx
from fastapi.testclient import TestClient

from app.bifrost import BifrostClient, parse_json
from app.config import Settings
from app.main import create_app
from app.pipeline import Pipeline
from app.store import Store
from tests.fakes import FakeLLM, FakeSearch


def make(transport=None):
    s = Settings(db_path=":memory:")
    bf = BifrostClient(s, transport=transport)
    pipe = Pipeline(FakeLLM(), FakeSearch())
    return TestClient(create_app(s, pipe, Store(":memory:"), bf))


class ParseJsonTests(unittest.TestCase):
    def test_fenced(self):
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_chatter(self):
        self.assertEqual(parse_json('Sure! {"a": 2} hope it helps'), {"a": 2})


class ApiTests(unittest.TestCase):
    def test_health_and_dashboard(self):
        c = make()
        self.assertEqual(c.get("/health").json()["status"], "ok")
        self.assertIn("Stale-Answer", c.get("/").text)

    def test_verify_logs_and_metrics(self):
        c = make()
        r = c.post("/v1/verify", json={"answer": "The Pro plan costs $20/month."})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["is_stale"])
        self.assertEqual(len(c.get("/api/logs").json()), 1)
        self.assertEqual(c.get("/api/metrics").json()["total_checks"], 1)

    def test_verify_rejects_empty(self):
        self.assertEqual(make().post("/v1/verify", json={"answer": ""}).status_code, 422)

    def test_proxy_intercepts_bifrost_response(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant",
                                  "content": "The Pro plan costs $20/month."}}]})
        c = make(httpx.MockTransport(handler))
        r = c.post("/v1/chat/completions", json={"model": "openai/gpt-4o-mini",
                   "messages": [{"role": "user", "content": "How much is Pro?"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["X-Stale-Answer"], "true")
        self.assertIn("stale_answer_report", r.json())

    def test_proxy_rejects_streaming(self):
        r = make().post("/v1/chat/completions", json={"stream": True, "messages": []})
        self.assertEqual(r.status_code, 400)

    def test_proxy_passes_through_upstream_errors(self):
        c = make(httpx.MockTransport(lambda req: httpx.Response(401, json={"error": "bad key"})))
        r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 401)


if __name__ == "__main__":
    unittest.main()
