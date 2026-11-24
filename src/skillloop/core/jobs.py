"""Task state & progress: persistent, observable, cancellable background jobs.

A job is any callable ``fn(ctx: JobContext) -> jsonable``. Long-running code calls
``ctx.progress(...)`` / ``ctx.log(...)`` and polls ``ctx.check_cancelled()`` at safe points;
adapters additionally receive ``ctx.cancel_event`` so they can kill subprocesses promptly."""

from __future__ import annotations

import json
import os
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from .models import new_id
from .store import JOB_FINAL, Store


class JobCancelled(Exception):
    """Raised inside a job when the user cancelled it."""


class JobContext:
    def __init__(
        self,
        job_id: str,
        kind: str,
        params: dict[str, Any],
        store: Store,
        log_path: Path | None = None,
        echo: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.job_id = job_id
        self.kind = kind
        self.params = params
        self.store = store
        self.cancel_event = threading.Event()
        self._log_path = log_path
        self._echo = echo
        self._progress: dict[str, Any] = {}
        self._lock = threading.Lock()

    # ---- cancellation ----
    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise JobCancelled(self.job_id)

    # ---- observability ----
    def _emit(self, level: str, type: str, message: str, data: dict[str, Any]) -> None:
        rec = {"ts": time.time(), "job_id": self.job_id, "level": level, "type": type, "message": message, "data": data}
        self.store.add_event(self.job_id, level, type, message, data)
        if self._log_path is not None:
            with self._lock, self._log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
        if self._echo is not None:
            try:
                self._echo(rec)
            except Exception:
                pass

    def log(self, message: str, level: str = "info", **data: Any) -> None:
        self._emit(level, "log", message, data)

    def audit(self, action: str, **data: Any) -> None:
        """Record a state-changing operation (promotion, version write, config change...)."""
        self._emit("info", "audit", action, data)

    def progress(self, stage: str, current: float = 0, total: float = 0, message: str = "", **extra: Any) -> None:
        """Update the job's progress bar. ``stage`` names the pipeline step (e.g. ``"evaluate"``)."""
        with self._lock:
            self._progress = {
                "stage": stage,
                "current": current,
                "total": total,
                "message": message,
                "percent": round(100.0 * current / total, 1) if total else None,
                **extra,
            }
            snapshot = dict(self._progress)
        self.store.update_job(self.job_id, progress=snapshot)
        self._emit("info", "progress", message or stage, snapshot)

    def artifact(self, kind: str, path: str | Path, *, version: str = "", title: str = "", **meta: Any) -> None:
        """Register a produced file in the 产物索引."""
        self.store.index_artifact(kind, str(path), job_id=self.job_id, version=version, title=title, meta=meta)


class NullContext(JobContext):
    """Context for running pipeline code outside the job system (tests, library use)."""

    def __init__(self, store: Store | None = None, echo: Callable[[dict[str, Any]], None] | None = None):
        self.job_id, self.kind, self.params = "", "inline", {}
        self.store = store  # type: ignore[assignment]
        self.cancel_event = threading.Event()
        self._log_path, self._echo, self._progress = None, echo, {}
        self._lock = threading.Lock()

    def _emit(self, level: str, type: str, message: str, data: dict[str, Any]) -> None:
        if self.store is not None:
            self.store.add_event("", level, type, message, data)
        if self._echo is not None:
            self._echo(
                {"ts": time.time(), "job_id": "", "level": level, "type": type, "message": message, "data": data}
            )

    def progress(self, stage: str, current: float = 0, total: float = 0, message: str = "", **extra: Any) -> None:
        self._emit("info", "progress", message or stage, {"stage": stage, "current": current, "total": total, **extra})

    def artifact(self, kind: str, path: str | Path, *, version: str = "", title: str = "", **meta: Any) -> None:
        if self.store is not None:
            self.store.index_artifact(kind, str(path), version=version, title=title, meta=meta)


class JobManager:
    """Runs jobs on a small thread pool and persists their state in the project store."""

    def __init__(
        self,
        store: Store,
        log_dir: Path | None = None,
        max_workers: int = 2,
        echo: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.store = store
        self.log_dir = log_dir
        if log_dir is not None:
            log_dir.mkdir(parents=True, exist_ok=True)
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="skillloop-job")
        self._ctx: dict[str, JobContext] = {}
        self._done: dict[str, threading.Event] = {}
        self._echo = echo
        self._lock = threading.Lock()
        store.interrupt_stale_jobs(os.getpid())

    def submit(self, kind: str, fn: Callable[[JobContext], Any], params: dict[str, Any] | None = None) -> str:
        job_id = new_id("job_")
        params = params or {}
        self.store.create_job(job_id, kind, params, pid=os.getpid())
        log_path = self.log_dir / f"{job_id}.jsonl" if self.log_dir else None
        ctx = JobContext(job_id, kind, params, self.store, log_path, self._echo)
        done = threading.Event()
        with self._lock:
            self._ctx[job_id], self._done[job_id] = ctx, done
        self._pool.submit(self._run, ctx, fn, done)
        return job_id

    def run_inline(
        self, kind: str, fn: Callable[[JobContext], Any], params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Run synchronously in the caller's thread (CLI), still recorded as a job."""
        job_id = new_id("job_")
        self.store.create_job(job_id, kind, params or {}, pid=os.getpid())
        log_path = self.log_dir / f"{job_id}.jsonl" if self.log_dir else None
        ctx = JobContext(job_id, kind, params or {}, self.store, log_path, self._echo)
        done = threading.Event()
        with self._lock:
            self._ctx[job_id], self._done[job_id] = ctx, done
        self._run(ctx, fn, done)
        return self.get(job_id)  # type: ignore[return-value]

    def _run(self, ctx: JobContext, fn: Callable[[JobContext], Any], done: threading.Event) -> None:
        try:
            if ctx.cancelled:
                raise JobCancelled(ctx.job_id)
            self.store.update_job(ctx.job_id, status="running")
            ctx._emit("info", "job", "started", {"kind": ctx.kind, "params": ctx.params})
            result = fn(ctx)
            if ctx.cancelled:
                raise JobCancelled(ctx.job_id)
            self.store.update_job(ctx.job_id, status="succeeded", result=result)
            ctx._emit("info", "job", "succeeded", {})
        except JobCancelled:
            self.store.update_job(ctx.job_id, status="cancelled", error="cancelled by user")
            ctx._emit("warning", "job", "cancelled", {})
        except BaseException as e:  # noqa: BLE001 - a job must never take the worker thread down silently
            self.store.update_job(ctx.job_id, status="failed", error=f"{type(e).__name__}: {e}")
            ctx._emit(
                "error", "job", "failed", {"error": f"{type(e).__name__}: {e}", "traceback": traceback.format_exc()}
            )
            if isinstance(e, KeyboardInterrupt):
                raise
        finally:
            done.set()
            with self._lock:
                self._ctx.pop(ctx.job_id, None)

    def cancel(self, job_id: str) -> bool:
        """Request cancellation. Returns False when the job is unknown or already finished."""
        with self._lock:
            ctx = self._ctx.get(job_id)
        if ctx is None:
            return False
        ctx.cancel_event.set()
        self.store.update_job(job_id, status="cancelling")
        ctx._emit("warning", "job", "cancel requested", {})
        return True

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self.store.get_job(job_id)

    def list(self, limit: int = 100, kind: str = "", status: str = "") -> list[dict[str, Any]]:
        return self.store.list_jobs(limit, kind, status)

    def events(self, job_id: str, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return self.store.events(job_id, after, limit)

    def wait(self, job_id: str, timeout: float | None = None) -> dict[str, Any] | None:
        with self._lock:
            done = self._done.get(job_id)
        if done is not None:
            done.wait(timeout)
        return self.get(job_id)

    def is_final(self, job_id: str) -> bool:
        job = self.get(job_id)
        return bool(job) and job["status"] in JOB_FINAL

    def shutdown(self, cancel: bool = True) -> None:
        if cancel:
            with self._lock:
                ctxs = list(self._ctx.values())
            for c in ctxs:
                c.cancel_event.set()
        self._pool.shutdown(wait=True)
