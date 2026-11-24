"""Deterministic mock LLM. Together with :mod:`skillloop.adapters.mock` it forms a small simulated
world in which the whole evolution loop runs offline, reproducibly and for free:

* the mock *agent* answers a case correctly iff the skill text mentions every marker in
  ``case.meta["requires"]``; otherwise its trajectory says ``MISSING_GUIDANCE: <marker> :: <hint>``;
* the mock *analyst* reads those lines and proposes a patch adding the missing rules
  (at most ``max_fixes_per_call`` per call, so progress takes several iterations);
* every ``regress_every``-th analyst call it also deletes an existing rule, producing a harmful
  candidate that the promotion gate must catch.

Other modules register handlers for their own roles with :func:`mock_handler`."""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from typing import Any

from ..core.registry import LLMS
from .base import LLMClient, LLMResponse, extract_tag, extract_tag_json

Handler = Callable[["MockLLM", str, str], Any]
MOCK_HANDLERS: dict[str, Handler] = {}
MISSING = re.compile(r"MISSING_GUIDANCE:\s*(\S+)(?:\s*::\s*(.*))?")
RULE_LINE = re.compile(r"^- \[([^\]]+)\] .*$", re.M)


def mock_handler(role: str) -> Callable[[Handler], Handler]:
    """Register a deterministic reply for a role: ``fn(llm, prompt, system) -> str | jsonable``."""

    def deco(fn: Handler) -> Handler:
        MOCK_HANDLERS[role] = fn
        return fn

    return deco


def norm(s: Any) -> str:
    return " ".join(str(s).lower().split())


@LLMS.register("mock")
class MockLLM(LLMClient):
    name = "mock"

    def __init__(
        self,
        model: str = "mock",
        max_fixes_per_call: int = 2,
        regress_every: int = 0,
        handlers: dict[str, Handler] | None = None,
        **options: Any,
    ):
        super().__init__(model, **options)
        self.max_fixes_per_call = max_fixes_per_call
        self.regress_every = regress_every
        self.handlers = dict(handlers or {})
        self.calls: list[dict[str, str]] = []  # for assertions in tests
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()

    def count(self, key: str) -> int:
        """Increment and return a per-instance counter (lets handlers vary behaviour over time)."""
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1
            return self._counters[key]

    def complete(
        self,
        prompt: str,
        *,
        system: str = "",
        max_tokens: int = 16000,
        json_schema: dict[str, Any] | None = None,
        role: str = "",
    ) -> LLMResponse:
        with self._lock:
            self.calls.append({"role": role, "prompt": prompt, "system": system})
        handler = self.handlers.get(role) or MOCK_HANDLERS.get(role)
        if handler is None:
            out: Any = {} if json_schema else f"[mock:{role or 'default'}] ok"
        else:
            out = handler(self, prompt, system)
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        return LLMResponse(
            text=text, model=self.model, usage={"input_tokens": len(prompt) // 4, "output_tokens": len(text) // 4}
        )


# --------------------------------------------------------------------------- built-in role handlers


@mock_handler("judge")
def _judge(llm: MockLLM, prompt: str, system: str) -> dict[str, Any]:
    expected, output = extract_tag(prompt, "expected"), extract_tag(prompt, "output")
    ok = bool(norm(expected)) and norm(expected) in norm(output)
    return {
        "score": 1.0 if ok else 0.0,
        "passed": ok,
        "reason": "output contains the expected answer" if ok else "expected answer not found in output",
        "labels": [] if ok else ["wrong_answer"],
        "confidence": 1.0,
    }


@mock_handler("analyst_error")
def _analyst_error(llm: MockLLM, prompt: str, system: str) -> dict[str, Any]:
    failures = extract_tag_json(prompt, "failures", []) or []
    skill_text = extract_tag(prompt, "skill")
    diagnoses, edits, evidence, fixed = [], [], [], set()
    for f in failures:
        found = MISSING.findall(json.dumps(f, ensure_ascii=False).replace("\\n", "\n"))
        markers = [(m, h.strip().rstrip('"').strip()) for m, h in found]
        diagnoses.append(
            {
                "case_id": f.get("case_id", ""),
                "category": "missing_rule" if markers else "unknown",
                "root_cause": ("skill lacks guidance for: " + ", ".join(m for m, _ in markers))
                if markers
                else "could not determine the cause from the trajectory",
                "lesson": "; ".join(h or m for m, h in markers),
            }
        )
        for marker, hint in markers:
            if marker in fixed or len(fixed) >= llm.max_fixes_per_call:
                continue
            fixed.add(marker)
            evidence.append(str(f.get("case_id", "")))
            edits.append(
                {
                    "op": "append",
                    "section": "## Rules",
                    "content": f"- [{marker}] {hint or 'Handle ' + marker + ' explicitly.'}",
                }
            )
    n = llm.count("analyst_error")
    if llm.regress_every and n % llm.regress_every == 0:
        existing = RULE_LINE.search(skill_text)
        if existing:  # a plausible-looking but harmful "simplification"
            edits.append({"op": "delete", "old": existing.group(0)})
    return {
        "diagnoses": diagnoses,
        "patch": {
            "summary": "add rules for: " + ", ".join(sorted(fixed)) if fixed else "no actionable finding",
            "rationale": "failures show the agent had no guidance for these situations",
            "edits": edits,
            "evidence": evidence,
        },
    }


@mock_handler("analyst_success")
def _analyst_success(llm: MockLLM, prompt: str, system: str) -> dict[str, Any]:
    return {"patch": {"summary": "", "edits": []}, "keep": []}


@mock_handler("merger")
def _merger(llm: MockLLM, prompt: str, system: str) -> dict[str, Any]:
    patches = extract_tag_json(prompt, "patches", []) or []
    edits, seen, evidence, summaries = [], set(), [], []
    for p in patches:
        for e in p.get("edits") or []:
            key = json.dumps(e, sort_keys=True, ensure_ascii=False)
            if key not in seen:
                seen.add(key)
                edits.append(e)
        evidence += [x for x in p.get("evidence") or [] if x not in evidence]
        if p.get("summary"):
            summaries.append(p["summary"])
    return {
        "summary": "; ".join(dict.fromkeys(summaries)),
        "rationale": "merged and de-duplicated",
        "edits": edits,
        "evidence": evidence,
    }


@mock_handler("builder")
def _builder(llm: MockLLM, prompt: str, system: str) -> str:
    name = extract_tag(prompt, "skill_name").strip() or "generated-skill"
    req = extract_tag(prompt, "requirement").strip()
    first = next((ln.strip("# ").strip() for ln in req.splitlines() if ln.strip()), "the task")
    return (
        f"---\nname: {name}\ndescription: Use when working on {first[:120]}\n---\n\n"
        f"# {name}\n\n## Overview\n\n{first}\n\n## Rules\n\n- Read the task carefully and answer concisely.\n"
    )
