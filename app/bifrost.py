"""Client for the Bifrost AI Gateway (OpenAI-compatible chat endpoint).

Every LLM call in this project (claim extraction, claim judging, and the
transparent proxy) goes through Bifrost, so routing, fallbacks, caching,
budgets and observability configured in Bifrost apply automatically.
"""
import json
import re
from typing import Any, Optional

import httpx

from .config import Settings


def parse_json(text: str) -> Any:
    """Parse JSON from an LLM reply, tolerating code fences and chatter."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


class BifrostClient:
    def __init__(self, settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.s = settings
        self._transport = transport

    def _headers(self, extra: Optional[dict] = None) -> dict:
        h = {"Content-Type": "application/json"}
        if self.s.bifrost_api_key:
            h["Authorization"] = f"Bearer {self.s.bifrost_api_key}"
        if extra:
            h.update(extra)
        return h

    async def post_chat(self, payload: dict, extra_headers: Optional[dict] = None) -> httpx.Response:
        url = self.s.bifrost_url + self.s.bifrost_chat_path
        async with httpx.AsyncClient(timeout=self.s.request_timeout, transport=self._transport) as c:
            return await c.post(url, json=payload, headers=self._headers(extra_headers))

    async def chat_json(self, system: str, user: str) -> Any:
        payload = {
            "model": self.s.analysis_model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        r = await self.post_chat(payload)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
        return parse_json(content)
