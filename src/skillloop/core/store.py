"""SQLite-backed persistent state: jobs, events (structured log + audit), artifact index, reviews.

One file per project (``.skillloop/state.db``). All access goes through a single connection
guarded by a lock, which is plenty for a local console and keeps the store dependency-free."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}', progress TEXT NOT NULL DEFAULT '{}',
    result TEXT, error TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL, pid INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL DEFAULT '', ts REAL NOT NULL,
    level TEXT NOT NULL DEFAULT 'info', type TEXT NOT NULL DEFAULT 'log',
    message TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS events_job ON events(job_id, seq);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, path TEXT NOT NULL,
    job_id TEXT NOT NULL DEFAULT '', version TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '',
    meta TEXT NOT NULL DEFAULT '{}', created_at REAL NOT NULL, UNIQUE(kind, path)
);
CREATE TABLE IF NOT EXISTS reviews (
    eval_id TEXT NOT NULL, case_id TEXT NOT NULL, trial INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending', auto_score REAL, auto_passed INTEGER, auto_reason TEXT NOT NULL DEFAULT '',
    human_score REAL, human_passed INTEGER, comment TEXT NOT NULL DEFAULT '', reviewer TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY (eval_id, case_id, trial)
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

JOB_ACTIVE = ("queued", "running", "cancelling")
JOB_FINAL = ("succeeded", "failed", "cancelled", "interrupted")


def _j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _exec(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._db.execute(sql, args)
            self._db.commit()
            return cur

    def _rows(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute(sql, args).fetchall()]

    # ---------------------------------------------------------------- jobs
    @staticmethod
    def _job(row: dict[str, Any]) -> dict[str, Any]:
        for k in ("params", "progress", "result"):
            row[k] = json.loads(row[k]) if row.get(k) else ({} if k != "result" else None)
        return row

    def create_job(self, job_id: str, kind: str, params: dict[str, Any], pid: int = 0) -> None:
        t = time.time()
        self._exec(
            "INSERT INTO jobs (id, kind, status, params, created_at, updated_at, pid) VALUES (?,?,?,?,?,?,?)",
            (job_id, kind, "queued", _j(params), t, t, pid),
        )

    def update_job(self, job_id: str, **fields: Any) -> None:
        if not fields:
            return
        cols, vals = [], []
        for k, v in fields.items():
            cols.append(f"{k} = ?")
            vals.append(_j(v) if k in ("params", "progress", "result") else v)
        cols.append("updated_at = ?")
        vals.append(time.time())
        self._exec(f"UPDATE jobs SET {', '.join(cols)} WHERE id = ?", (*vals, job_id))

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        rows = self._rows("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._job(rows[0]) if rows else None

    def list_jobs(self, limit: int = 100, kind: str = "", status: str = "") -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM jobs WHERE 1=1", []
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        return [self._job(r) for r in self._rows(sql, tuple(args))]

    def interrupt_stale_jobs(self, alive_pid: int) -> int:
        """Mark jobs left active by a dead process as interrupted (called at startup)."""
        cur = self._exec(
            "UPDATE jobs SET status = 'interrupted', error = 'process exited before the job finished', updated_at = ? "
            "WHERE status IN ('queued','running','cancelling') AND pid != ?",
            (time.time(), alive_pid),
        )
        return cur.rowcount

    # ---------------------------------------------------------------- events
    def add_event(
        self,
        job_id: str = "",
        level: str = "info",
        type: str = "log",
        message: str = "",
        data: dict[str, Any] | None = None,
    ) -> int:
        cur = self._exec(
            "INSERT INTO events (job_id, ts, level, type, message, data) VALUES (?,?,?,?,?,?)",
            (job_id, time.time(), level, type, message, _j(data or {})),
        )
        return int(cur.lastrowid or 0)

    def events(
        self, job_id: str | None = None, after: int = 0, limit: int = 500, type: str = ""
    ) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM events WHERE seq > ?", [after]
        if job_id is not None:
            sql += " AND job_id = ?"
            args.append(job_id)
        if type:
            sql += " AND type = ?"
            args.append(type)
        sql += " ORDER BY seq ASC LIMIT ?"
        args.append(limit)
        rows = self._rows(sql, tuple(args))
        for r in rows:
            r["data"] = json.loads(r["data"] or "{}")
        return rows

    def tail_events(self, limit: int = 100, job_id: str | None = None, type: str = "") -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM events WHERE 1=1", []
        if job_id is not None:
            sql += " AND job_id = ?"
            args.append(job_id)
        if type:
            sql += " AND type = ?"
            args.append(type)
        sql += " ORDER BY seq DESC LIMIT ?"
        args.append(limit)
        rows = self._rows(sql, tuple(args))[::-1]
        for r in rows:
            r["data"] = json.loads(r["data"] or "{}")
        return rows

    # ---------------------------------------------------------------- artifacts
    def index_artifact(
        self,
        kind: str,
        path: str,
        *,
        job_id: str = "",
        version: str = "",
        title: str = "",
        meta: dict[str, Any] | None = None,
    ) -> None:
        self._exec(
            "INSERT INTO artifacts (kind, path, job_id, version, title, meta, created_at) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(kind, path) DO UPDATE SET job_id=excluded.job_id, version=excluded.version, "
            "title=excluded.title, meta=excluded.meta, created_at=excluded.created_at",
            (kind, path, job_id, version, title, _j(meta or {}), time.time()),
        )

    def artifacts(self, kind: str = "", version: str = "", job_id: str = "", limit: int = 500) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM artifacts WHERE 1=1", []
        for col, val in (("kind", kind), ("version", version), ("job_id", job_id)):
            if val:
                sql += f" AND {col} = ?"
                args.append(val)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        rows = self._rows(sql, tuple(args))
        for r in rows:
            r["meta"] = json.loads(r["meta"] or "{}")
        return rows

    # ---------------------------------------------------------------- reviews (human judging queue)
    def upsert_review(
        self,
        eval_id: str,
        case_id: str,
        trial: int,
        *,
        auto_score: float | None = None,
        auto_passed: bool | None = None,
        auto_reason: str = "",
    ) -> None:
        t = time.time()
        self._exec(
            "INSERT INTO reviews (eval_id, case_id, trial, auto_score, auto_passed, auto_reason, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(eval_id, case_id, trial) DO UPDATE SET "
            "auto_score=excluded.auto_score, auto_passed=excluded.auto_passed, auto_reason=excluded.auto_reason",
            (eval_id, case_id, trial, auto_score, None if auto_passed is None else int(auto_passed), auto_reason, t, t),
        )

    def submit_review(
        self,
        eval_id: str,
        case_id: str,
        trial: int,
        *,
        score: float,
        passed: bool,
        comment: str = "",
        reviewer: str = "",
    ) -> bool:
        cur = self._exec(
            "UPDATE reviews SET status='done', human_score=?, human_passed=?, comment=?, reviewer=?, updated_at=? "
            "WHERE eval_id=? AND case_id=? AND trial=?",
            (score, int(passed), comment, reviewer, time.time(), eval_id, case_id, trial),
        )
        return cur.rowcount > 0

    def reviews(self, eval_id: str = "", status: str = "", limit: int = 1000) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM reviews WHERE 1=1", []
        if eval_id:
            sql += " AND eval_id = ?"
            args.append(eval_id)
        if status:
            sql += " AND status = ?"
            args.append(status)
        sql += " ORDER BY created_at DESC, case_id ASC LIMIT ?"
        args.append(limit)
        rows = self._rows(sql, tuple(args))
        for r in rows:
            for k in ("auto_passed", "human_passed"):
                r[k] = None if r[k] is None else bool(r[k])
        return rows

    # ---------------------------------------------------------------- kv
    def get(self, key: str, default: Any = None) -> Any:
        rows = self._rows("SELECT value FROM kv WHERE key = ?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key: str, value: Any) -> None:
        self._exec(
            "INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, _j(value)),
        )
