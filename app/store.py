"""SQLite log store + aggregate metrics."""
import json
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


class Store:
    def __init__(self, path: str):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute(
                """CREATE TABLE IF NOT EXISTS checks(
                id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, source TEXT,
                question TEXT, answer TEXT, score INTEGER, is_stale INTEGER,
                summary TEXT, latency_ms INTEGER, report TEXT)"""
            )
            self._db.commit()

    def add(self, source: str, question: str, answer: str, report: dict) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO checks(created_at,source,question,answer,score,is_stale,summary,latency_ms,report)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), source, question, answer,
                 report["score"], int(report["is_stale"]), report["summary"], report["latency_ms"],
                 json.dumps(report)),
            )
            self._db.commit()
            return cur.lastrowid

    def logs(self, limit: int = 50) -> list:
        with self._lock:
            rows = self._db.execute("SELECT * FROM checks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["report"] = json.loads(d["report"])
            d["is_stale"] = bool(d["is_stale"])
            out.append(d)
        return out

    def metrics(self) -> dict:
        rows = self.logs(500)
        n = len(rows)
        cats, verdicts, domains = Counter(), Counter(), Counter()
        outdated = 0
        for r in rows:
            for c in r["report"]["claims"]:
                cats[c["category"]] += 1
                verdicts[c["verdict"]] += 1
                outdated += c["verdict"] == "outdated"
                for s in c["sources"]:
                    host = urlparse(s.get("url", "")).netloc
                    if host:
                        domains[host] += 1
        stale = sum(r["is_stale"] for r in rows)
        return {
            "total_checks": n,
            "stale_answers": stale,
            "stale_rate": round(100 * stale / n, 1) if n else 0.0,
            "avg_score": round(sum(r["score"] for r in rows) / n, 1) if n else 0.0,
            "avg_latency_ms": round(sum(r["latency_ms"] for r in rows) / n) if n else 0,
            "outdated_claims": outdated,
            "by_category": dict(cats),
            "by_verdict": dict(verdicts),
            "top_sources": domains.most_common(5),
        }
