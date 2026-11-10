"""API tests. Require the packages in requirements.txt (fastapi, httpx)."""
import unittest

import httpx
from fastapi.testclient import TestClient

from app.bifrost import BifrostClient, parse_json
from app.config import Settings
from app.main import create_app
from app.pipeline import Pipeline
from app.store import Store
from tests.fakes import FakeLLM, FakeSearch


def make(transport=None, **cfg):
    s = Settings(db_path=":memory:", **cfg)
    bf = BifrostClient(s, transport=transport)
    return TestClient(create_app(s, Pipeline(FakeLLM(), FakeSearch()), Store(":memory:"), bf))


class ParseJsonTests(unittest.TestCase):
    def test_fenced(self):
        self.assertEqual(parse_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_chatter(self):
        self.assertEqual(parse_json('Sure! {"a": 2} hope it helps'), {"a": 2})


class ApiTests(unittest.TestCase):
    def test_health_and_static_dashboard(self):
        c = make()
        self.assertEqual(c.get("/health").json()["status"], "ok")
        r = c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Stale-Answer", r.text)

    def test_verify_is_async_then_pollable(self):
        c = make()
        r = c.post("/v1/verify", json={"answer": "The Pro plan costs $20/month."})
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.json()["status"], "pending")
        # TestClient runs background tasks before returning, so the check is finished now
        done = c.get(r.json()["poll"]).json()
        self.assertEqual(done["status"], "done")
        self.assertTrue(done["is_stale"])
        self.assertEqual(c.get("/api/metrics").json()["total_checks"], 1)

    def test_verify_rejects_empty_and_unknown_id(self):
        c = make()
        self.assertEqual(c.post("/v1/verify", json={"answer": ""}).status_code, 422)
        self.assertEqual(c.get("/v1/checks/999").status_code, 404)

    def test_proxy_returns_immediately_and_verifies_in_background(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"choices": [{"message": {
                "role": "assistant", "content": "The Pro plan costs $20/month."}}]})
        c = make(httpx.MockTransport(handler))
        r = c.post("/v1/chat/completions", json={"model": "m", "messages": [{"role": "user", "content": "Price?"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["stale_answer_report"]["status"], "pending")
        done = c.get(f"/v1/checks/{r.headers['X-Check-Id']}").json()
        self.assertEqual(done["status"], "done")

    def test_proxy_rejects_streaming_and_passes_errors(self):
        self.assertEqual(make().post("/v1/chat/completions", json={"stream": True}).status_code, 400)
        c = make(httpx.MockTransport(lambda req: httpx.Response(401, json={"error": "bad key"})))
        r = c.post("/v1/chat/completions", json={"messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 401)

    def test_mcp_endpoint(self):
        c = make()
        init = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                    "params": {"protocolVersion": "2025-06-18"}})
        self.assertEqual(init.json()["result"]["serverInfo"]["name"], "stale-answer-detector")
        self.assertEqual(c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code, 202)
        tools = c.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).json()["result"]["tools"]
        self.assertEqual(len(tools), 2)
        call = c.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                    "params": {"name": "verify_freshness",
                                               "arguments": {"answer": "The Pro plan costs $20/month."}}})
        self.assertIn('"status": "done"', call.json()["result"]["content"][0]["text"])

    def test_mcp_auth_token(self):
        c = make(mcp_auth_token="s3cret")
        body = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        self.assertEqual(c.post("/mcp", json=body).status_code, 401)
        self.assertEqual(c.post("/mcp", json=body, headers={"Authorization": "Bearer s3cret"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
