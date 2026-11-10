import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    bifrost_url: str = "http://localhost:8080"
    bifrost_chat_path: str = "/v1/chat/completions"
    bifrost_api_key: str = ""
    analysis_model: str = "openai/gpt-4o-mini"
    tavily_api_key: str = ""
    tavily_depth: str = "basic"
    max_claims: int = 5
    max_sources: int = 5
    stale_threshold: int = 40
    db_path: str = "data/checks.db"
    request_timeout: float = 60.0


def load_settings() -> Settings:
    g = os.environ.get
    return Settings(
        bifrost_url=g("BIFROST_URL", "http://localhost:8080").rstrip("/"),
        bifrost_chat_path=g("BIFROST_CHAT_PATH", "/v1/chat/completions"),
        bifrost_api_key=g("BIFROST_API_KEY", ""),
        analysis_model=g("ANALYSIS_MODEL", "openai/gpt-4o-mini"),
        tavily_api_key=g("TAVILY_API_KEY", ""),
        tavily_depth=g("TAVILY_DEPTH", "basic"),
        max_claims=int(g("MAX_CLAIMS", "5")),
        max_sources=int(g("MAX_SOURCES", "5")),
        stale_threshold=int(g("STALE_THRESHOLD", "40")),
        db_path=g("DB_PATH", "data/checks.db"),
        request_timeout=float(g("REQUEST_TIMEOUT", "60")),
    )
