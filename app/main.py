"""FastAPI service.

* POST /v1/verify            queue a background freshness check (returns 202 + id)
* GET  /v1/checks/{id}       poll a check
* POST /v1/chat/completions  optional proxy: returns Bifrost's answer immediately and
                             verifies it in the background (no added latency)
* POST /mcp                  MCP server (Streamable HTTP) so Bifrost's MCP gateway can
                             register this service and expose its tools
* /                          dashboard (static files)

Run with:  uvicorn app.main:create_app --factory --port 8000
"""
import asyncio
import hmac
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__
from .bifrost import BifrostClient
from .config import Settings, load_settings
from .mcp import McpHandler
from .pipeline import Pipeline
from .store import Store
from .tavily import TavilyClient

STATIC_DIR = Path(__file__).parent / "static"


class VerifyRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=20000)
    question: str = ""


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


def _view(rec: dict) -> dict:
    """Public JSON for one check (full report once done)."""
    out = {"id": rec["id"], "status": rec["status"], "created_at": rec["created_at"]}
    if rec["status"] == "done":
        out.update(rec["report"])
    elif rec["status"] == "failed":
        out["error"] = rec.get("error", "")
    return out


def create_app(settings: Optional[Settings] = None, pipeline: Optional[Pipeline] = None,
               store: Optional[Store] = None, bifrost: Optional[BifrostClient] = None) -> FastAPI:
    s = settings or load_settings()
    bifrost = bifrost or BifrostClient(s)
    pipeline = pipeline or Pipeline(bifrost, TavilyClient(s), s.max_claims, s.stale_threshold,
                                    step_timeout=s.step_timeout)
    store = store or Store(s.db_path)
    app = FastAPI(title="Bifrost Stale-Answer Detector", version=__version__)
    live_tasks: set = set()  # strong refs so asyncio does not garbage-collect running checks

    async def execute_check(check_id: int, answer: str, question: str) -> None:
        """The slow part (Tavily + Bifrost calls). Always ends in 'done' or 'failed'."""
        try:
            report = await asyncio.wait_for(pipeline.run(answer, question), timeout=s.check_timeout)
            store.complete(check_id, report.to_dict())
        except Exception as e:  # includes TimeoutError
            store.fail(check_id, f"{type(e).__name__}: {e}")

    async def submit_task(answer: str, question: str) -> tuple:
        """Used by the MCP tool: start a check and hand back (id, task) so it can wait briefly."""
        check_id = store.create_pending("mcp", question, answer)
        task = asyncio.create_task(execute_check(check_id, answer, question))
        live_tasks.add(task)
        task.add_done_callback(live_tasks.discard)
        return check_id, task

    mcp = McpHandler(submit_task, store.get, __version__)

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__, "bifrost": s.bifrost_url,
                "tavily_configured": bool(s.tavily_api_key)}

    @app.post("/v1/verify", status_code=202)
    async def verify(req: VerifyRequest, background: BackgroundTasks):
        check_id = store.create_pending("verify-api", req.question, req.answer)
        background.add_task(execute_check, check_id, req.answer, req.question)
        return {"id": check_id, "status": "pending", "poll": f"/v1/checks/{check_id}"}

    @app.get("/v1/checks/{check_id}")
    async def get_check(check_id: int):
        rec = store.get(check_id)
        if not rec:
            raise HTTPException(404, "unknown check id")
        return _view(rec)

    @app.post("/v1/chat/completions")
    async def chat_proxy(request: Request, background: BackgroundTasks):
        """Forward to Bifrost, return its answer immediately, verify in the background."""
        body = await request.json()
        if body.get("stream"):
            raise HTTPException(400, "stream=true is not supported by the verifying proxy")
        fwd = {}
        if request.headers.get("authorization"):
            fwd["Authorization"] = request.headers["authorization"]
        up = await bifrost.post_chat(body, fwd)
        try:
            data = up.json()
        except ValueError:
            return JSONResponse({"error": up.text[:500]}, status_code=up.status_code)
        if up.status_code >= 400:
            return JSONResponse(data, status_code=up.status_code)
        try:
            answer = _text(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError):
            return JSONResponse(data)
        users = [m for m in body.get("messages", []) if m.get("role") == "user"]
        question = _text(users[-1].get("content")) if users else ""
        check_id = store.create_pending("proxy", question, answer)
        background.add_task(execute_check, check_id, answer, question)
        data["stale_answer_report"] = {"id": check_id, "status": "pending", "poll": f"/v1/checks/{check_id}"}
        return JSONResponse(data, headers={"X-Check-Id": str(check_id)}, background=background)

    @app.post("/mcp")
    async def mcp_endpoint(request: Request):
        if s.mcp_auth_token:
            supplied = request.headers.get("authorization", "")
            if not hmac.compare_digest(supplied, f"Bearer {s.mcp_auth_token}"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            msg = await request.json()
        except ValueError:
            return JSONResponse({"jsonrpc": "2.0", "id": None,
                                 "error": {"code": -32700, "message": "Parse error"}}, status_code=400)
        if isinstance(msg, list):
            replies = [r for r in [await mcp.handle(m) for m in msg] if r is not None]
            return JSONResponse(replies) if replies else Response(status_code=202)
        reply = await mcp.handle(msg)
        return Response(status_code=202) if reply is None else JSONResponse(reply)

    @app.get("/api/logs")
    async def logs(limit: int = 50):
        return store.logs(max(1, min(limit, 200)))

    @app.get("/api/metrics")
    async def metrics():
        return store.metrics()

    # Registered LAST so every API route above takes precedence over the static catch-all.
    # html=True serves index.html at "/". StaticFiles raises at startup if the folder is missing.
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="dashboard")
    return app
