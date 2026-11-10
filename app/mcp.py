"""Minimal MCP (Model Context Protocol) server over Streamable HTTP, tools only.

Bifrost's MCP gateway connects to this as an HTTP MCP client, discovers the two
tools below, and offers them to models/agents that go through the gateway.

Framework-free on purpose: `McpHandler.handle()` takes a decoded JSON-RPC message
and returns a response dict (or None when no response is due), so it is unit-testable.
"""
import asyncio
import json
from typing import Awaitable, Callable, Optional

SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
DEFAULT_WAIT = 20.0
# Bifrost's default tool-call timeout is 30 s; stay safely below it.
MAX_WAIT = 25.0
MAX_ANSWER = 20000

INSTRUCTIONS = (
    "Use verify_freshness on a draft answer BEFORE presenting it whenever it contains prices, "
    "software/API versions, laws, who holds a role, or statistics. Fix or caveat any claim "
    "marked 'outdated' using its corrected_fact."
)

TOOLS = [
    {
        "name": "verify_freshness",
        "description": (
            "Check an AI-generated answer for stale facts. Extracts time-sensitive claims "
            "(pricing, versions, laws, leadership, statistics), re-verifies each against live "
            "web sources (Tavily) and returns a 0-100 freshness score per claim with corrected "
            "facts and source URLs. If it takes longer than wait_seconds it returns status "
            "'pending' with a check_id; poll get_freshness_report."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "answer": {"type": "string", "description": "The answer text to verify."},
                "question": {"type": "string", "description": "The original user question (optional)."},
                "wait_seconds": {"type": "number", "description": "Max seconds to wait for the result (0-25, default 20)."},
            },
            "required": ["answer"],
        },
    },
    {
        "name": "get_freshness_report",
        "description": "Fetch the result of a freshness check started earlier by verify_freshness.",
        "inputSchema": {
            "type": "object",
            "properties": {"check_id": {"type": "integer", "description": "check_id returned by verify_freshness."}},
            "required": ["check_id"],
        },
    },
]


class _RpcError(Exception):
    def __init__(self, code: int, message: str):
        self.code, self.message = code, message


def _err(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _tool_result(obj, is_error: bool = False) -> dict:
    text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def compact(rec: dict) -> dict:
    """Token-friendly view of a stored check for LLM consumption."""
    out = {"check_id": rec["id"], "status": rec["status"]}
    if rec["status"] == "done":
        out.update(
            score=rec["score"], is_stale=rec["is_stale"], summary=rec["summary"],
            claims=[{
                "claim": c["text"], "category": c["category"], "verdict": c["verdict"],
                "freshness": c["freshness"], "corrected_fact": c["corrected_fact"],
                "reasoning": c["reasoning"], "sources": [s["url"] for s in c["sources"][:3]],
            } for c in rec["report"]["claims"]],
        )
    elif rec["status"] == "failed":
        out["error"] = rec.get("error", "")
    else:
        out["hint"] = "Still verifying. Call get_freshness_report with this check_id in a few seconds."
    return out


class McpHandler:
    def __init__(self, submit: Callable[[str, str], Awaitable[tuple]],
                 get: Callable[[int], Optional[dict]], server_version: str = "1.0.0"):
        """submit(answer, question) -> (check_id, asyncio.Task); get(check_id) -> record or None."""
        self.submit, self.get, self.server_version = submit, get, server_version

    async def handle(self, msg) -> Optional[dict]:
        if not isinstance(msg, dict):
            return _err(None, -32600, "Invalid Request")
        method, mid = msg.get("method"), msg.get("id")
        if method is None:
            return None  # a client response/ack; nothing to answer
        notification = "id" not in msg
        try:
            if method == "initialize":
                result = self._initialize(msg.get("params") or {})
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = await self._call(msg.get("params") or {})
            elif notification:
                return None  # e.g. notifications/initialized
            else:
                return _err(mid, -32601, f"Method not found: {method}")
        except _RpcError as e:
            return None if notification else _err(mid, e.code, e.message)
        return None if notification else {"jsonrpc": "2.0", "id": mid, "result": result}

    def _initialize(self, params: dict) -> dict:
        wanted = params.get("protocolVersion")
        return {
            "protocolVersion": wanted if wanted in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0],
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "stale-answer-detector", "version": self.server_version},
            "instructions": INSTRUCTIONS,
        }

    async def _call(self, params: dict) -> dict:
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "verify_freshness":
            answer = args.get("answer")
            if not isinstance(answer, str) or not answer.strip():
                return _tool_result("`answer` must be a non-empty string.", True)
            if len(answer) > MAX_ANSWER:
                return _tool_result(f"`answer` is too long (max {MAX_ANSWER} characters).", True)
            try:
                wait = max(0.0, min(float(args.get("wait_seconds", DEFAULT_WAIT)), MAX_WAIT))
            except (TypeError, ValueError):
                wait = DEFAULT_WAIT
            check_id, task = await self.submit(answer, str(args.get("question") or ""))
            if wait:
                try:
                    # shield: a timeout here must NOT cancel the background check itself
                    await asyncio.wait_for(asyncio.shield(task), wait)
                except asyncio.TimeoutError:
                    pass
                except Exception:
                    pass  # failures are recorded on the check row
            return _tool_result(compact(self.get(check_id)))
        if name == "get_freshness_report":
            try:
                check_id = int(args.get("check_id"))
            except (TypeError, ValueError):
                return _tool_result("`check_id` must be an integer.", True)
            rec = self.get(check_id)
            return _tool_result(compact(rec)) if rec else _tool_result(f"Unknown check_id {check_id}.", True)
        raise _RpcError(-32602, f"Unknown tool: {name}")
