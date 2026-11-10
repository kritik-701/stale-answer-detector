"""Tavily live-web search client."""
from typing import Optional

import httpx

from .config import Settings


class TavilyClient:
    URL = "https://api.tavily.com/search"

    def __init__(self, settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.s = settings
        self._transport = transport

    async def search(self, query: str) -> list[dict]:
        if not self.s.tavily_api_key:
            raise RuntimeError("TAVILY_API_KEY is not set")
        payload = {
            "query": query[:400],
            "search_depth": self.s.tavily_depth,
            "max_results": self.s.max_sources,
            "include_answer": False,
        }
        headers = {"Authorization": f"Bearer {self.s.tavily_api_key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=self.s.request_timeout, transport=self._transport) as c:
            r = await c.post(self.URL, json=payload, headers=headers)
            r.raise_for_status()
        return [
            {
                "title": x.get("title", ""),
                "url": x.get("url", ""),
                "snippet": (x.get("content") or "")[:600],
                "score": x.get("score"),
            }
            for x in r.json().get("results", [])
        ]
