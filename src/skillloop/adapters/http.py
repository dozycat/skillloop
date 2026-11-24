"""HTTP adapter: POST each case to your own agent service.

Request body (JSON)::

    {"case_id": "...", "input": "...", "skill": {"name", "description", "markdown", "files"},
     "files": {...case files...}, "meta": {...}, "timeout_s": 600, "mcp_config": {...} | null}

Expected response (JSON; field names configurable)::

    {"output": "final answer", "steps": [{"kind": "tool_call", "name": "x", "content": "..."}],
     "usage": {"input_tokens": 1}, "artifacts": {"path": "text"}, "error": ""}

    [agent]
    adapter = "http"
    [agent.options]
    url = "http://localhost:8000/run"
    headers = { Authorization = "Bearer ${MY_AGENT_TOKEN}" }
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from ..core.models import Step, Trajectory
from ..core.registry import ADAPTERS
from ._proc import truncate
from .base import AgentAdapter, AgentRequest
from .command import dig

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env(value: str) -> str:
    return _ENV.sub(lambda m: os.environ.get(m.group(1), ""), str(value))


@ADAPTERS.register("http")
class HTTPAdapter(AgentAdapter):
    name = "http"

    def __init__(
        self,
        url: str = "",
        headers: dict[str, str] | None = None,
        method: str = "POST",
        output_path: str = "output",
        steps_path: str = "steps",
        usage_path: str = "usage",
        artifacts_path: str = "artifacts",
        error_path: str = "error",
        extra_body: dict[str, Any] | None = None,
        health_url: str = "",
        timeout_s: float = 0.0,
        **options: Any,
    ):
        super().__init__(**options)
        self.url, self.method = url, method.upper()
        self.headers = dict(headers or {})
        self.output_path, self.steps_path, self.usage_path = output_path, steps_path, usage_path
        self.artifacts_path, self.error_path = artifacts_path, error_path
        self.extra_body = dict(extra_body or {})
        self.health_url = health_url
        self.timeout_s = timeout_s  # 0 => use the per-request timeout

    def _payload(self, request: AgentRequest) -> dict[str, Any]:
        c, s = request.case, request.skill
        return {
            "case_id": c.id,
            "input": c.input,
            "files": c.files,
            "meta": c.meta,
            "tags": c.tags,
            "skill": {
                "name": s.name,
                "description": s.description,
                "markdown": s.to_markdown(),
                "files": s.files,
                "rendered": s.render_for_prompt(),
            },
            "timeout_s": request.timeout_s,
            "mcp_config": request.mcp_config,
            **self.extra_body,
        }

    def _post(self, body: bytes, timeout: float) -> tuple[int, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **{k: expand_env(v) for k, v in self.headers.items()},
        }
        req = urllib.request.Request(expand_env(self.url), data=body, headers=headers, method=self.method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", errors="replace")

    def run(self, request: AgentRequest) -> Trajectory:
        t0 = time.time()
        cid = request.case.id
        try:
            if not self.url:
                return Trajectory(case_id=cid, status="error", error="http adapter: no 'url' configured")
            timeout = self.timeout_s or request.timeout_s or 600.0
            body = json.dumps(self._payload(request), ensure_ascii=False, default=str).encode("utf-8")
            box: dict[str, Any] = {}

            def work() -> None:
                try:
                    box["res"] = self._post(body, timeout)
                except Exception as e:  # noqa: BLE001
                    box["exc"] = e

            th = threading.Thread(target=work, daemon=True, name="skillloop-http")
            th.start()
            while th.is_alive():  # urllib cannot be interrupted; on cancel we stop waiting for it
                if request.cancel.wait(0.05):
                    return Trajectory(case_id=cid, status="cancelled", error="cancelled", duration_s=time.time() - t0)
                if time.time() - t0 > timeout + 5:
                    return Trajectory(
                        case_id=cid,
                        status="timeout",
                        error=f"timed out after {timeout:.0f}s",
                        duration_s=time.time() - t0,
                    )
            dur = time.time() - t0
            if "exc" in box:
                e = box["exc"]
                is_timeout = isinstance(e, TimeoutError) or "timed out" in str(e).lower()
                return Trajectory(
                    case_id=cid,
                    status="timeout" if is_timeout else "error",
                    duration_s=dur,
                    error=f"timed out after {timeout:.0f}s" if is_timeout else f"{type(e).__name__}: {e}",
                )
            code, text = box["res"]
            traj = Trajectory(case_id=cid, raw=truncate(text), duration_s=dur)
            if code >= 400:
                traj.status, traj.error = "error", f"HTTP {code}: {text[:500]}"
                return traj
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                traj.output = text.strip()  # plain-text endpoints are fine too
                traj.steps.append(Step("message", traj.output))
                return traj
            out = dig(data, self.output_path)
            err = dig(data, self.error_path) if self.error_path else None
            if err:
                traj.status, traj.error = "error", str(err)
            elif out is None:
                traj.status, traj.error = "error", f"no {self.output_path!r} field in response"
            traj.output = "" if out is None else (out if isinstance(out, str) else json.dumps(out, ensure_ascii=False))
            for s in dig(data, self.steps_path) or [] if self.steps_path else []:
                if isinstance(s, dict):
                    traj.steps.append(
                        Step(
                            kind=str(s.get("kind") or s.get("type") or "message"),
                            content=str(s.get("content") or s.get("text") or ""),
                            name=str(s.get("name") or ""),
                            data=dict(s.get("data") or {}),
                        )
                    )
                else:
                    traj.steps.append(Step("message", str(s)))
            if not traj.steps and traj.output:
                traj.steps.append(Step("message", traj.output))
            usage = dig(data, self.usage_path) if self.usage_path else None
            if isinstance(usage, dict):
                traj.usage = {k: v for k, v in usage.items() if isinstance(v, (int, float))}
            arts = dig(data, self.artifacts_path) if self.artifacts_path else None
            if isinstance(arts, dict):
                traj.artifacts = {str(k): str(v) for k, v in arts.items()}
            return traj
        except Exception as e:  # noqa: BLE001 - adapters never raise
            return Trajectory(
                case_id=cid, status="error", error=f"{type(e).__name__}: {e}", duration_s=time.time() - t0
            )

    def healthcheck(self) -> tuple[bool, str]:
        if not self.url:
            return False, "no url configured"
        target = expand_env(self.health_url or self.url)
        try:
            req = urllib.request.Request(
                target,
                method="GET" if self.health_url else "OPTIONS",
                headers={k: expand_env(v) for k, v in self.headers.items()},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                return True, f"{target} -> HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            return True, f"{target} reachable (HTTP {e.code})"  # server answered; route may just reject the verb
        except Exception as e:  # noqa: BLE001
            return False, f"{target} unreachable: {e}"
