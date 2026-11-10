class FakeLLM:
    """Deterministic stand-in for Bifrost: answers extraction and judging prompts."""

    def __init__(self, verdict="outdated", confidence=0.9, fail_extract=False):
        self.verdict, self.confidence, self.fail_extract = verdict, confidence, fail_extract

    async def chat_json(self, system, user):
        if "fact-freshness judge" in system:
            return {"verdict": self.verdict, "confidence": self.confidence,
                    "corrected_fact": "The plan now costs $25/month.", "reasoning": "Pricing page shows $25."}
        if self.fail_extract:
            raise RuntimeError("gateway down")
        return {"claims": [{"text": "The Pro plan costs $20/month.", "category": "pricing",
                            "reason": "prices change", "search_query": "Pro plan price"}]}


class FakeSearch:
    def __init__(self, fail=False):
        self.fail = fail

    async def search(self, query):
        if self.fail:
            raise RuntimeError("tavily down")
        return [{"title": "Pricing", "url": "https://example.com/pricing", "snippet": "Pro is $25/month", "score": 0.9}]
