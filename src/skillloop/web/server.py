"""Web 操作台: a dependency-free HTTP console over :class:`skillloop.service.Workspace`.

JSON REST API under ``/api`` (project-scoped as ``/api/projects/<name>/...``), Server-Sent Events
for live job logs, and a static single-page frontend. Because this console can launch agents on
the user's machine it is conservative by default: loopback bind, Host-header validation
(DNS-rebinding defence), cross-origin request rejection, JSON-only bodies, optional bearer token
(mandatory when binding a non-loopback address)."""

from __future__ import annotations

import hmac
import json
import re
import sys
import threading
import time
import traceback
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from ..core.sandbox import SandboxError
from ..core.skill import Skill
from ..service import JOB_KINDS, SkillLoop, Workspace

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 5 * 1024 * 1024
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
WILDCARD = {"0.0.0.0", "::", ""}
_STATIC_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
JOB_FINAL = ("succeeded", "failed", "cancelled", "interrupted")
SSE_HEARTBEAT_S = 10.0
SSE_POLL_S = 0.3


class HTTPError(Exception):
    def __init__(self, status: int, type_: str, message: str):
        super().__init__(message)
        self.status, self.type, self.message = status, type_, message


def _error_for(e: BaseException) -> tuple[int, str, str]:
    if isinstance(e, HTTPError):
        return e.status, e.type, e.message
    if isinstance(e, SandboxError):
        return 403, "forbidden", str(e)
    if isinstance(e, (KeyError, FileNotFoundError)):
        msg = e.args[0] if isinstance(e, KeyError) and e.args else str(e)
        return 404, "not_found", str(msg)
    if isinstance(e, (ValueError, TypeError)):
        return 400, "bad_request", str(e)
    if isinstance(e, RuntimeError):
        return 409, "conflict", str(e)
    return 500, "internal", "internal server error (see the server log)"


def _int(value: Any, name: str, default: int | None = None) -> int | None:
    if value in (None, ""):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise HTTPError(400, "bad_request", f"{name} must be an integer") from None


def _host_name(netloc: str) -> str:
    """Hostname part of a Host header / netloc, lower-cased, without port or IPv6 brackets."""
    netloc = netloc.strip().lower()
    if netloc.startswith("["):
        return netloc[1:].split("]", 1)[0]
    return netloc.rsplit(":", 1)[0] if ":" in netloc else netloc


class ConsoleServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple[str, int], workspace: Workspace, token: str = ""):
        super().__init__(addr, Handler)
        self.workspace = workspace
        self.token = token
        self.bind_host = addr[0]
        self.stopping = threading.Event()
        names = set(LOOPBACK)
        if addr[0] not in WILDCARD:
            names.add(addr[0].lower())
        self.allowed_hosts = None if addr[0] in WILDCARD else names  # wildcard bind relies on the token

    def shutdown(self) -> None:
        self.stopping.set()
        super().shutdown()

    def server_close(self) -> None:
        self.stopping.set()
        super().server_close()
        try:
            self.workspace.close()
        except Exception:
            traceback.print_exc(file=sys.stderr)


class Handler(BaseHTTPRequestHandler):
    server: ConsoleServer
    protocol_version = "HTTP/1.1"
    server_version = "skillloop-console"

    # ---------------------------------------------------------------- plumbing
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - quiet by default
        pass

    def _send(self, status: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data: Any, status: int = 200) -> None:
        self._send(
            status, json.dumps(data, ensure_ascii=False, default=str).encode("utf-8"), "application/json; charset=utf-8"
        )

    def _fail(self, e: BaseException) -> None:
        status, type_, message = _error_for(e)
        if status == 500:
            traceback.print_exception(type(e), e, e.__traceback__, file=sys.stderr)
        try:
            self._json({"error": {"type": type_, "message": message}}, status)
        except OSError:
            pass

    def _body(self) -> dict[str, Any]:
        length = _int(self.headers.get("Content-Length"), "Content-Length", 0) or 0
        if length > MAX_BODY:
            self.close_connection = True
            raise HTTPError(413, "too_large", f"request body over {MAX_BODY} bytes")
        if length == 0:
            return {}
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self.rfile.read(length)
            raise HTTPError(415, "unsupported_media_type", "Content-Type must be application/json")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise HTTPError(400, "bad_request", f"invalid JSON body: {e}") from None
        if not isinstance(data, dict):
            raise HTTPError(400, "bad_request", "JSON body must be an object")
        return data

    # ---------------------------------------------------------------- security checks
    def _check_host(self) -> None:
        allowed = self.server.allowed_hosts
        if allowed is not None and _host_name(self.headers.get("Host") or "") not in allowed:
            raise HTTPError(403, "forbidden", "Host header not allowed")

    def _check_origin(self) -> None:
        origin = self.headers.get("Origin")
        if origin is not None:
            host = (self.headers.get("Host") or "").strip().lower()
            if origin == "null" or urlsplit(origin).netloc.lower() != host:
                raise HTTPError(403, "forbidden", "cross-origin request rejected")
        if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
            raise HTTPError(403, "forbidden", "cross-site request rejected")

    def _check_token(self, query: dict[str, list[str]]) -> None:
        token = self.server.token
        if not token:
            return
        auth = self.headers.get("Authorization") or ""
        if auth.lower().startswith("bearer "):  # header values arrive latin-1 decoded
            given = auth[7:].strip().encode("latin-1", "replace")
        else:
            given = (query.get("token") or [""])[0].encode("utf-8")
        if not hmac.compare_digest(given, token.encode("utf-8")):
            raise HTTPError(401, "unauthorized", "missing or invalid token")

    # ---------------------------------------------------------------- dispatch
    def _dispatch(self) -> None:
        try:
            url = urlsplit(self.path)
            query = parse_qs(url.query, keep_blank_values=True)
            self._check_host()
            parts = [unquote(p) for p in url.path.split("/") if p]
            if parts[:1] == ["api"]:
                self._check_origin()
                self._check_token(query)
                body = self._body() if self.command in ("POST", "PUT", "PATCH") else {}
                self._api(parts[1:], {k: v[-1] for k, v in query.items()}, body)
            elif self.command in ("GET", "HEAD"):
                self._static(parts)
            else:
                raise HTTPError(405, "method_not_allowed", "method not allowed")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except BaseException as e:  # noqa: BLE001 - every failure becomes a uniform JSON error
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            self._fail(e)

    do_GET = do_POST = do_PUT = do_PATCH = do_HEAD = do_DELETE = _dispatch

    def _static(self, parts: list[str]) -> None:
        if not parts:
            name = "index.html"
        elif len(parts) == 2 and parts[0] == "static":
            name = parts[1]
        else:
            raise HTTPError(404, "not_found", "not found")
        target = STATIC_DIR / name
        if not _STATIC_NAME.match(name) or not target.is_file():
            raise HTTPError(404, "not_found", "not found")
        csp = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "frame-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
        )
        self._send(
            200,
            target.read_bytes(),
            _MIME.get(target.suffix, "application/octet-stream"),
            {"Content-Security-Policy": csp},
        )

    # ---------------------------------------------------------------- API
    def _api(self, parts: list[str], q: dict[str, str], body: dict[str, Any]) -> None:
        m, ws = self.command, self.server.workspace
        if m == "HEAD":
            m = "GET"
        if parts == ["projects"]:
            if m == "GET":
                return self._json({"projects": ws.list(), "single": ws.single})
            if m == "POST":
                cases = body.get("cases")
                if cases is not None and not isinstance(cases, list):
                    raise HTTPError(400, "bad_request", "cases must be a list of records")
                svc = ws.create(
                    str(body.get("name") or ""),
                    requirement=str(body.get("requirement") or ""),
                    skill_md=str(body.get("skill_md") or ""),
                    cases=cases,
                    config=body.get("config") if isinstance(body.get("config"), dict) else None,
                )
                return self._json(svc.info(), 201)
        elif len(parts) >= 2 and parts[0] == "projects":
            handler = self._project(ws.get(parts[1]), m, parts[2:], q, body)
            if handler is not None:
                return handler()
        elif parts == ["meta"] and m == "GET":
            return self._json(
                {"job_kinds": list(JOB_KINDS), "single": ws.single, "token_required": bool(self.server.token)}
            )
        raise HTTPError(
            404 if m in ("GET", "POST", "PUT", "PATCH") else 405,
            "not_found",
            f"no route for {self.command} /api/{'/'.join(parts)}",
        )

    def _project(
        self,
        svc: SkillLoop,
        m: str,
        p: list[str],
        q: dict[str, str],
        body: dict[str, Any],
    ) -> Callable[[], None] | None:
        """Return a thunk for the matched route, or None."""
        J = self._json
        n = len(p)
        if n == 0 and m == "GET":
            return lambda: J(svc.info())
        if n == 1:
            simple: dict[tuple[str, str], Callable[[], Any]] = {
                ("GET", "info"): svc.info,
                ("GET", "health"): svc.health,
                ("GET", "config"): svc.config,
                ("PATCH", "config"): lambda: svc.update_config(body),
                ("GET", "requirement"): lambda: {"text": svc.requirement()},
                ("PUT", "requirement"): lambda: (svc.set_requirement(str(body.get("text") or "")), {"ok": True})[1],
                ("GET", "cases"): lambda: {"cases": svc.cases(q.get("split", ""))},
                ("POST", "cases"): lambda: {"added": svc.add_cases(self._records(body))},
                ("GET", "jobs"): lambda: {"jobs": svc.jobs.list(limit=_int(q.get("limit"), "limit", 100) or 100)},
                ("POST", "jobs"): lambda: self._submit(svc, body),
                ("GET", "versions"): lambda: {"versions": svc.versions(), "best": svc.project.versions.best_id()},
                ("GET", "diff"): lambda: {
                    "a": q.get("a", ""),
                    "b": q.get("b", ""),
                    "diff": svc.diff(self._need(q, "a"), self._need(q, "b")),
                },
                ("POST", "compare"): lambda: self._compare(svc, body),
                ("GET", "evals"): lambda: {"evals": svc.evals()},
                ("GET", "errors"): lambda: {"errors": svc.errors(q.get("status", ""))},
                ("GET", "reviews"): lambda: {"reviews": self._reviews(svc, q)},
                ("POST", "reviews"): lambda: self._submit_review(svc, body),
                ("GET", "history"): lambda: {"history": svc.history()},
                ("GET", "metrics"): svc.metrics,
                ("GET", "artifacts"): lambda: {"artifacts": svc.artifacts(q.get("kind", ""), q.get("version", ""))},
                ("GET", "file"): lambda: {
                    "path": self._need(q, "path"),
                    "content": svc.read_artifact(self._need(q, "path")),
                },
                ("GET", "gitlog"): lambda: {"log": svc.git_log(_int(q.get("limit"), "limit", 50) or 50)},
                ("POST", "outputs"): lambda: {"outputs": svc.write_outputs()},
            }
            fn = simple.get((m, p[0]))
            if fn is not None:
                return lambda: J(fn(), 201 if (m, p[0]) == ("POST", "jobs") else 200)
            if (m, p[0]) == ("GET", "review-sheet"):
                return lambda: self._review_sheet(svc, q)
            if (m, p[0]) == ("GET", "report"):
                return lambda: self._report(svc)
            return None
        a, b = p[0], p[1]
        if a == "jobs":
            if n == 2 and m == "GET":
                return lambda: J(self._job(svc, b))
            if n == 3 and p[2] == "cancel" and m == "POST":
                return lambda: (self._job(svc, b), J({"cancelled": svc.cancel(b), "job": svc.job(b)}))[1]
            if n == 3 and p[2] == "events" and m == "GET":
                return lambda: self._sse(svc, b, q)
            if n == 3 and p[2] == "poll" and m == "GET":
                return lambda: self._poll(svc, b, q)
        elif a == "versions":
            if n == 2 and b == "import" and m == "POST":
                return lambda: J(self._import(svc, body), 201)
            if n == 2 and m == "GET":
                return lambda: J(svc.version(b))
            if n == 3 and p[2] == "promote" and m == "POST":
                return lambda: J(
                    svc.promote(
                        b, str(body.get("reason") or "manual promotion"), str(body.get("by") or "web")
                    ).to_dict()
                )
            if n == 3 and p[2] == "reject" and m == "POST":
                return lambda: J(svc.reject(b, str(body.get("reason") or "")).to_dict())
        elif a == "evals" and n == 2 and m == "GET":
            return lambda: J(svc.eval(b, q.get("trajectories", "1") not in ("0", "false")))
        elif a == "errors":
            if n == 2 and b == "stats" and m == "GET":
                return lambda: J(svc.error_stats())
            if n == 3 and p[2] == "annotate" and m == "POST":
                return lambda: self._annotate(svc, b, body)
        return None

    # ---------------------------------------------------------------- route helpers
    @staticmethod
    def _need(q: dict[str, Any], name: str) -> str:
        v = q.get(name)
        if v in (None, ""):
            raise HTTPError(400, "bad_request", f"missing parameter {name!r}")
        return str(v)

    @staticmethod
    def _records(body: dict[str, Any]) -> list[dict[str, Any]]:
        cases = body.get("cases")
        if not isinstance(cases, list) or not all(isinstance(c, dict) for c in cases):
            raise HTTPError(400, "bad_request", 'body must be {"cases": [ {...}, ... ]}')
        return cases

    @staticmethod
    def _job(svc: SkillLoop, job_id: str) -> dict[str, Any]:
        job = svc.job(job_id)
        if job is None:
            raise HTTPError(404, "not_found", f"unknown job {job_id!r}")
        return job

    def _submit(self, svc: SkillLoop, body: dict[str, Any]) -> dict[str, Any]:
        kind = str(body.get("kind") or "")
        raw = body.get("params") or {}
        if not isinstance(raw, dict):
            raise HTTPError(400, "bad_request", "params must be an object")
        params: dict[str, Any] = {}
        if raw.get("max_iterations") not in (None, ""):
            params["max_iterations"] = _int(raw["max_iterations"], "max_iterations")
            if params["max_iterations"] < 0 or params["max_iterations"] > 1000:
                raise HTTPError(400, "bad_request", "max_iterations out of range")
        for k in ("version", "split"):
            if raw.get(k):
                params[k] = str(raw[k])
        if params.get("split") not in (None, "train", "val", "test"):
            raise HTTPError(400, "bad_request", "split must be train, val, test or empty")
        if raw.get("case_ids"):
            if not isinstance(raw["case_ids"], list):
                raise HTTPError(400, "bad_request", "case_ids must be a list")
            params["case_ids"] = [str(c) for c in raw["case_ids"]]
        if "merge" in raw:
            params["merge"] = bool(raw["merge"])
        job_id = svc.submit(kind, **params)
        return {"job_id": job_id, "job": svc.job(job_id)}

    def _compare(self, svc: SkillLoop, body: dict[str, Any]) -> dict[str, Any]:
        split = str(body.get("split") or "val")
        if split not in ("train", "val", "test"):
            raise HTTPError(400, "bad_request", "split must be train, val or test")
        cmp = svc.compare(self._need(body, "base"), self._need(body, "cand"), split)
        return {**cmp.to_dict(), "verdict": cmp.verdict}

    def _import(self, svc: SkillLoop, body: dict[str, Any]) -> dict[str, Any]:
        text = str(body.get("skill_md") or "")
        if not text.strip():
            raise HTTPError(400, "bad_request", "skill_md is empty")
        # Always parse as text: a bare string must never be interpreted as a filesystem path.
        v = svc.import_skill(
            Skill.from_markdown(text), str(body.get("summary") or "manual edit"), str(body.get("parent") or "")
        )
        return v.to_dict()

    def _annotate(self, svc: SkillLoop, case_id: str, body: dict[str, Any]) -> None:
        fields = {
            k: str(body[k])
            for k in ("category", "root_cause", "lesson", "suggested_fix", "status")
            if body.get(k) not in (None, "")
        }
        if fields.get("status") not in (None, "open", "fixed", "regressed", "wontfix"):
            raise HTTPError(400, "bad_request", "status must be open, fixed, regressed or wontfix")
        if not svc.annotate_error(case_id, **fields):
            raise HTTPError(404, "not_found", f"no error-book entry for case {case_id!r}")
        self._json({"ok": True})

    def _reviews(self, svc: SkillLoop, q: dict[str, str]) -> list[dict[str, Any]]:
        rows = svc.reviews(q.get("status", ""), q.get("eval_id", ""))
        if q.get("detail", "1") in ("0", "false"):
            return rows
        cases = {c["id"]: c for c in svc.cases()}
        evals: dict[str, dict[tuple[str, int], dict[str, Any]]] = {}
        for r in rows:
            eid = r["eval_id"]
            if eid not in evals:
                try:
                    data = svc.project.load_eval(eid)
                    evals[eid] = {
                        (x.case_id, x.trial): {
                            "output": x.trajectory.output[:8000],
                            "judge": x.judgment.judge,
                            "skill_version": x.skill_version,
                        }
                        for x in data.results
                    }
                except KeyError:
                    evals[eid] = {}
            c = cases.get(r["case_id"], {})
            r.update(
                {
                    "input": c.get("input", ""),
                    "expected": c.get("expected"),
                    "rubric": c.get("rubric", ""),
                    **evals[eid].get((r["case_id"], r["trial"]), {}),
                }
            )
        return rows

    def _submit_review(self, svc: SkillLoop, body: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(body.get("passed"), bool):
            raise HTTPError(400, "bad_request", "passed must be true or false")
        score = body.get("score")
        if score is not None:
            try:
                score = float(score)
            except (TypeError, ValueError):
                raise HTTPError(400, "bad_request", "score must be a number") from None
            if not 0.0 <= score <= 1.0:
                raise HTTPError(400, "bad_request", "score must be between 0 and 1")
        eval_id = self._need(body, "eval_id")
        svc.project.load_eval(eval_id)  # 404 for unknown evaluations
        ok = svc.submit_review(
            eval_id,
            self._need(body, "case_id"),
            _int(body.get("trial"), "trial", 0) or 0,
            passed=body["passed"],
            score=score,
            comment=str(body.get("comment") or ""),
            reviewer=str(body.get("reviewer") or "web"),
        )
        return {"ok": ok}

    def _review_sheet(self, svc: SkillLoop, q: dict[str, str]) -> None:
        path = svc.export_review_sheet(q.get("eval_id", ""))
        self._send(
            200,
            path.read_bytes(),
            "text/csv; charset=utf-8",
            {"Content-Disposition": f'attachment; filename="{re.sub(r"[^A-Za-z0-9._-]", "_", path.name)}"'},
        )

    def _report(self, svc: SkillLoop) -> None:
        try:
            html = svc.read_artifact("outputs/report.html", max_chars=8_000_000)
        except FileNotFoundError:
            raise HTTPError(404, "not_found", "report.html has not been generated yet (run 生成输出)") from None
        # The report embeds data from agent runs: render it in an opaque origin with scripts disabled.
        self._send(
            200,
            html.encode("utf-8"),
            "text/html; charset=utf-8",
            {"Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'; img-src data:"},
        )

    # ---------------------------------------------------------------- job events
    def _after(self, q: dict[str, str]) -> int:
        return _int(q.get("after") or self.headers.get("Last-Event-ID"), "after", 0) or 0

    def _poll(self, svc: SkillLoop, job_id: str, q: dict[str, str]) -> None:
        job = self._job(svc, job_id)
        events = svc.events(job_id, self._after(q), _int(q.get("limit"), "limit", 500) or 500)
        self._json(
            {
                "job": job,
                "events": events,
                "last": events[-1]["seq"] if events else self._after(q),
                "final": job["status"] in JOB_FINAL,
            }
        )

    def _sse(self, svc: SkillLoop, job_id: str, q: dict[str, str]) -> None:
        self._job(svc, job_id)
        after = self._after(q)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def write(text: str) -> None:
            self.wfile.write(text.encode("utf-8"))
            self.wfile.flush()

        last_beat = time.monotonic()
        try:
            write("retry: 2000\n\n")
            while not self.server.stopping.is_set():
                final = (svc.job(job_id) or {}).get("status") in JOB_FINAL  # read BEFORE draining
                events = svc.events(job_id, after, 500)
                for ev in events:
                    after = ev["seq"]
                    write(
                        f"id: {after}\nevent: {ev['type']}\ndata: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n"
                    )
                if events:
                    last_beat = time.monotonic()
                    continue
                if final:
                    write(f"event: end\ndata: {json.dumps(svc.job(job_id), ensure_ascii=False, default=str)}\n\n")
                    return
                if time.monotonic() - last_beat >= SSE_HEARTBEAT_S:
                    write(": ping\n\n")
                    last_beat = time.monotonic()
                self.server.stopping.wait(SSE_POLL_S)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


# --------------------------------------------------------------------------- entry points


def create_server(
    workspace_root: str | Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str = "",
) -> ConsoleServer:
    """Build (but do not start) the console server. ``server.serve_forever()`` runs it;
    ``server.shutdown(); server.server_close()`` stops it and closes the workspace."""
    if host.lower() not in LOOPBACK and not token:
        raise ValueError(
            f"refusing to listen on non-loopback address {host!r} without a token: this console can run agents "
            "on this machine. Pass token=... (CLI: --token) or bind 127.0.0.1."
        )
    workspace = Workspace(workspace_root)
    try:
        return ConsoleServer((host, port), workspace, token)
    except BaseException:
        workspace.close()
        raise


def serve(
    workspace_root: str | Path = ".",
    host: str = "127.0.0.1",
    port: int = 8765,
    token: str = "",
    open_browser: bool = False,
) -> None:
    server = create_server(workspace_root, host, port, token)
    shown = "localhost" if host in WILDCARD else host
    url = f"http://{shown}:{server.server_address[1]}/" + (f"?token={token}" if token else "")
    print(f"skillloop console: {url}  (workspace: {server.workspace.root})", file=sys.stderr)
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
