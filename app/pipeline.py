"""Core stale-answer detection pipeline (no web framework dependencies).

1. Claim identification  - LLM (via Bifrost) extracts time-sensitive claims;
                           regex heuristics are the fallback if the LLM fails.
2. Freshness detection   - each claim is categorised (pricing, version, ...).
3. Live verification     - Tavily searches the web, LLM (via Bifrost) judges the
                           claim against the retrieved evidence.
4. Freshness scoring     - 0-100 per claim, aggregated per answer.

Every external call (LLM or search) has its own timeout, so one slow upstream
degrades a single claim to "unverifiable" instead of hanging the whole check.
The pipeline is meant to run OFF the request path (FastAPI BackgroundTasks or
an MCP tool call), see app/main.py.
"""
import asyncio
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

CATEGORIES = {"pricing", "version", "leadership", "law", "statistic", "event", "other"}
VERDICTS = {"current", "outdated", "unverifiable"}
MAX_ANSWER_CHARS = 8000


class LLM(Protocol):
    async def chat_json(self, system: str, user: str) -> Any: ...


class Search(Protocol):
    async def search(self, query: str) -> list: ...


@dataclass
class ClaimResult:
    text: str
    category: str = "other"
    reason: str = ""
    search_query: str = ""
    verdict: str = "unverifiable"
    confidence: float = 0.0
    freshness: int = 50
    corrected_fact: str = ""
    reasoning: str = ""
    sources: list = field(default_factory=list)


@dataclass
class Report:
    score: int
    is_stale: bool
    summary: str
    claims: list
    checked_at: str
    latency_ms: int
    used_fallback: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------- heuristics
_PATTERNS = [
    ("pricing", r"[$€£₹]\s?\d|\bpric(e|es|ing)\b|\bper (month|year|user|token|seat)\b|\bfree tier\b|\bcosts?\b"),
    ("leadership", r"\b(CEO|CTO|CFO|COO|chairman|president|prime minister|chancellor|founder|head of)\b"),
    ("version", r"\bv?\d+\.\d+(\.\d+)?\b|\bversion\b|\breleased?\b|\bdeprecated\b|\brate limit|\bapi\b|\bendpoint\b"),
    ("law", r"\b(law|laws|regulation|regulations|act|gdpr|compliance|tax|legal|banned|legalized)\b"),
    ("statistic", r"\b\d+(\.\d+)?\s?(%|percent|million|billion|users|customers)|\bpopulation\b|\bmarket share\b"),
    ("event", r"\b(20[1-3]\d)\b|\b(latest|newest|current|currently|recent|recently|now|today)\b"),
]


def heuristic_claims(text: str, limit: int = 5) -> list:
    out = []
    for sent in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        sent = sent.strip()
        if len(sent) < 12:
            continue
        for cat, pat in _PATTERNS:
            if re.search(pat, sent, flags=re.I):
                out.append(ClaimResult(text=sent, category=cat, reason="matched time-sensitive pattern",
                                       search_query=sent[:200]))
                break
        if len(out) >= limit:
            break
    return out


# ------------------------------------------------------------------- scoring
def score_claim(verdict: str, confidence: float, n_sources: int) -> int:
    c = max(0.0, min(1.0, float(confidence)))
    if verdict == "current":
        s = 0.5 + 0.5 * c
    elif verdict == "outdated":
        s = 0.5 - 0.5 * c
    else:
        s = 0.5
    if n_sources == 0:
        s = min(s, 0.3)
    return round(s * 100)


def aggregate(claims: list, threshold: int) -> tuple:
    if not claims:
        return 100, False, "No time-sensitive claims were found."
    scores = [c.freshness for c in claims]
    score = round(0.5 * (sum(scores) / len(scores)) + 0.5 * min(scores))
    outdated = [c for c in claims if c.verdict == "outdated"]
    is_stale = bool(outdated) or score < threshold
    if outdated:
        summary = f"{len(outdated)} of {len(claims)} claims appear outdated."
    elif is_stale:
        summary = "Claims could not be confirmed with current sources."
    else:
        summary = f"{len(claims)} time-sensitive claim(s) checked; no outdated claims found."
    return score, is_stale, summary


# ------------------------------------------------------------------- prompts
EXTRACT_SYSTEM = (
    "You extract time-sensitive factual claims from an AI-generated answer. "
    "Time-sensitive means it could have changed: pricing, software/API versions, "
    "rate limits, laws/regulations, who holds a role, statistics, product availability. "
    "Ignore timeless facts and opinions. Reply ONLY with JSON: "
    '{"claims":[{"text":"<claim, self-contained>","category":"pricing|version|leadership|law|statistic|event|other",'
    '"reason":"<why it may be stale>","search_query":"<good web search query to verify it>"}]}'
)

JUDGE_SYSTEM = (
    "You are a fact-freshness judge. Using ONLY the search evidence provided and today's date, "
    "decide whether the claim is still accurate. Reply ONLY with JSON: "
    '{"verdict":"current|outdated|unverifiable","confidence":<0..1>,'
    '"corrected_fact":"<up-to-date fact if outdated, else empty>","reasoning":"<one or two sentences>"}'
)


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


class Pipeline:
    def __init__(self, llm, search, max_claims: int = 5, stale_threshold: int = 40,
                 concurrency: int = 4, step_timeout: float = 25.0):
        self.llm, self.search = llm, search
        self.max_claims, self.threshold = max_claims, stale_threshold
        self.concurrency, self.step_timeout = concurrency, step_timeout

    async def _bounded(self, coro):
        """Run one external call with a hard time limit."""
        return await asyncio.wait_for(coro, timeout=self.step_timeout)

    async def extract(self, answer: str) -> tuple:
        answer = (answer or "")[:MAX_ANSWER_CHARS]
        try:
            data = await self._bounded(
                self.llm.chat_json(EXTRACT_SYSTEM, f"Today: {_today()}\n\nANSWER:\n{answer}"))
            claims = []
            for c in (data.get("claims") or [])[: self.max_claims]:
                text = str(c.get("text", "")).strip()
                if not text:
                    continue
                cat = c.get("category") if c.get("category") in CATEGORIES else "other"
                claims.append(ClaimResult(text=text, category=cat, reason=str(c.get("reason", "")),
                                          search_query=str(c.get("search_query") or text)))
            return claims, False
        except Exception:
            return heuristic_claims(answer, self.max_claims), True

    async def verify(self, claim: ClaimResult, sem: asyncio.Semaphore) -> ClaimResult:
        async with sem:
            try:
                results = await self._bounded(self.search.search(claim.search_query or claim.text))
            except Exception as e:
                claim.reasoning = f"Live search failed: {type(e).__name__}: {e}"[:300]
                claim.freshness = score_claim("unverifiable", 0, 0)
                return claim
            claim.sources = results
            evidence = "\n".join(f"[{i+1}] {r['title']} ({r['url']}): {r['snippet']}"
                                 for i, r in enumerate(results))
            try:
                data = await self._bounded(self.llm.chat_json(
                    JUDGE_SYSTEM,
                    f"Today: {_today()}\nCLAIM: {claim.text}\n\nEVIDENCE:\n{evidence or '(none)'}",
                ))
                v = data.get("verdict")
                claim.verdict = v if v in VERDICTS else "unverifiable"
                claim.confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
                claim.corrected_fact = str(data.get("corrected_fact", "") or "")
                claim.reasoning = str(data.get("reasoning", "") or "")
            except Exception as e:
                claim.reasoning = f"Judging failed: {type(e).__name__}"
            claim.freshness = score_claim(claim.verdict, claim.confidence, len(claim.sources))
            return claim

    async def run(self, answer: str, question: str = "") -> Report:
        t0 = time.perf_counter()
        claims, fallback = await self.extract(answer)
        sem = asyncio.Semaphore(self.concurrency)
        claims = list(await asyncio.gather(*(self.verify(c, sem) for c in claims)))
        score, stale, summary = aggregate(claims, self.threshold)
        return Report(
            score=score, is_stale=stale, summary=summary,
            claims=[asdict(c) for c in claims],
            checked_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            latency_ms=round((time.perf_counter() - t0) * 1000),
            used_fallback=fallback,
        )
