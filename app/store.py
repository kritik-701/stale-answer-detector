"""SQLite log store with a pending -> done/failed lifecycle, plus aggregate metrics."""
import json
import sqlite3
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

_EMPTY_REPORT = json.dumps({"claims": [], "used_fallback": False})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
                summary TEXT, latency_ms INTEGER, report TEXT,
                status TEXT DEFAULT 'done', error TEXT DEFAULT '')"""
            )
            for col, ddl in (("status", "TEXT DEFAULT 'done'"), ("error", "TEXT DEFAULT ''")):
                try:  # upgrade databases created by v1
                    self._db.execute(f"ALTER TABLE checks ADD COLUMN {col} {ddl}")
                except sqlite3.OperationalError:
                    pass
            # background tasks die with the process; do not leave rows "pending" forever
            self._db.execute(
                "UPDATE checks SET status='failed', error='interrupted by restart',"
                " summary='Verification interrupted' WHERE status='pending'")
            self._db.commit()

    # ------------------------------------------------------------ lifecycle
    def create_pending(self, source: str, question: str, answer: str) -> int:
        with self._lock:
            cur = self._db.execute(
                "INSERT INTO checks(created_at,source,question,answer,score,is_stale,summary,latency_ms,report,status)"
                " VALUES(?,?,?,?,0,0,'Checking live sources…',0,?, 'pending')",
                (_now(), source, question, answer, _EMPTY_REPORT))
            self._db.commit()
            return cur.lastrowid

    def complete(self, check_id: int, report: dict) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE checks SET score=?, is_stale=?, summary=?, latency_ms=?, report=?, status='done', error=''"
                " WHERE id=?",
                (report["score"], int(report["is_stale"]), report["summary"], report["latency_ms"],
                 json.dumps(report), check_id))
            self._db.commit()

    def fail(self, check_id: int, error: str) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE checks SET status='failed', summary='Verification failed', error=? WHERE id=?",
                (error[:300], check_id))
            self._db.commit()

    def add(self, source: str, question: str, answer: str, report: dict) -> int:
        """Convenience: store an already-finished report."""
        cid = self.create_pending(source, question, answer)
        self.complete(cid, report)
        return cid

    # ---------------------------------------------------------------- reads
    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        d["report"] = json.loads(d["report"] or _EMPTY_REPORT)
        d["is_stale"] = bool(d["is_stale"])
        return d

    def get(self, check_id: int) -> Optional[dict]:
        with self._lock:
            r = self._db.execute("SELECT * FROM checks WHERE id=?", (check_id,)).fetchone()
        return self._row(r) if r else None

    def logs(self, limit: int = 50) -> list:
        with self._lock:
            rows = self._db.execute("SELECT * FROM checks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(r) for r in rows]

    def metrics(self) -> dict:
        rows = [r for r in self.logs(500) if r["status"] == "done"]
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
        with self._lock:
            pending = self._db.execute("SELECT COUNT(*) FROM checks WHERE status='pending'").fetchone()[0]
        return {
            "total_checks": n,
            "pending_checks": pending,
            "stale_answers": stale,
            "stale_rate": round(100 * stale / n, 1) if n else 0.0,
            "avg_score": round(sum(r["score"] for r in rows) / n, 1) if n else 0.0,
            "avg_latency_ms": round(sum(r["latency_ms"] for r in rows) / n) if n else 0,
            "outdated_claims": outdated,
            "by_category": dict(cats),
            "by_verdict": dict(verdicts),
            "top_sources": domains.most_common(5),
        }
