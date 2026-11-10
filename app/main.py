"""FastAPI service: verification API, Bifrost-proxy interceptor, dashboard.

Run with:  uvicorn app.main:create_app --factory --port 8000
"""
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .bifrost import BifrostClient
from .config import Settings, load_settings
from .pipeline import Pipeline
from .store import Store
from .tavily import TavilyClient

STATIC = Path(__file__).parent / "static"


class VerifyRequest(BaseModel):
    answer: str = Field(min_length=1, max_length=20000)
    question: str = ""


def _text(content) -> str:
    if isinstance(content, list):
        return " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content or "")


def create_app(settings: Optional[Settings] = None, pipeline: Optional[Pipeline] = None,
               store: Optional[Store] = None, bifrost: Optional[BifrostClient] = None) -> FastAPI:
    s = settings or load_settings()
    bifrost = bifrost or BifrostClient(s)
    pipeline = pipeline or Pipeline(bifrost, TavilyClient(s), s.max_claims, s.stale_threshold)
    store = store or Store(s.db_path)
    app = FastAPI(title="Bifrost Stale-Answer Detector", version=__version__)

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__, "bifrost": s.bifrost_url,
                "tavily_configured": bool(s.tavily_api_key)}

    @app.get("/")
    async def dashboard():
        return FileResponse(STATIC / "index.html")

    @app.post("/v1/verify")
    async def verify(req: VerifyRequest):
        report = (await pipeline.run(req.answer, req.question)).to_dict()
        report["id"] = store.add("verify-api", req.question, req.answer, report)
        return report

    @app.post("/v1/chat/completions")
    async def chat_proxy(request: Request):
        """Drop-in replacement for Bifrost's chat endpoint: forwards the request to
        Bifrost, intercepts the response, verifies it, and attaches a report."""
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
        report = (await pipeline.run(answer, question)).to_dict()
        report["id"] = store.add("proxy", question, answer, report)
        data["stale_answer_report"] = report
        return JSONResponse(data, headers={"X-Freshness-Score": str(report["score"]),
                                           "X-Stale-Answer": str(report["is_stale"]).lower()})

    @app.get("/api/logs")
    async def logs(limit: int = 50):
        return store.logs(max(1, min(limit, 200)))

    @app.get("/api/metrics")
    async def metrics():
        return store.metrics()

    return app
