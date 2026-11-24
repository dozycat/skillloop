"""Core data contracts shared by every module.

Everything here is a plain dataclass that round-trips through JSON via
``to_dict`` / ``from_dict`` so it can be persisted, sent over MCP/HTTP, or
handed to an adapter living in another process.
"""

from __future__ import annotations

import dataclasses
import math
import time
import types
import typing
import uuid
from dataclasses import dataclass, field
from typing import Any, Union


def error_message(e: BaseException) -> str:
    """Human-readable message of an exception (``str(KeyError)`` would add quotes around it)."""
    if isinstance(e, KeyError) and e.args and isinstance(e.args[0], str):
        return e.args[0]
    return str(e)


def now() -> float:
    return time.time()


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _build(tp: Any, value: Any) -> Any:
    """Rebuild ``value`` according to type hint ``tp`` (dataclasses, lists, dicts, optionals)."""
    if value is None:
        return None
    origin = typing.get_origin(tp)
    if origin in (Union, types.UnionType):
        for arg in typing.get_args(tp):
            if arg is type(None):
                continue
            return _build(arg, value)
        return value
    if origin in (list, tuple):
        (arg,) = typing.get_args(tp)[:1] or (Any,)
        return [_build(arg, v) for v in value]
    if origin is dict:
        args = typing.get_args(tp)
        vt = args[1] if len(args) == 2 else Any
        return {k: _build(vt, v) for k, v in value.items()}
    if dataclasses.is_dataclass(tp) and isinstance(value, dict):
        return tp.from_dict(value)  # type: ignore[union-attr]
    return value


class Serializable:
    """Mixin giving dataclasses tolerant JSON round-tripping (unknown keys are ignored)."""

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)  # type: ignore[call-overload]

    @classmethod
    def from_dict(cls, data: dict[str, Any]):
        hints = typing.get_type_hints(cls)
        kwargs = {}
        for f in dataclasses.fields(cls):  # type: ignore[arg-type]
            if f.name in data:
                kwargs[f.name] = _build(hints.get(f.name, Any), data[f.name])
        return cls(**kwargs)


# --------------------------------------------------------------------------- dataset


@dataclass
class Case(Serializable):
    """One evaluation case: what the agent is asked, and how to tell if it did well."""

    id: str
    input: str
    expected: Any = None  # gold answer; None when only a rubric applies
    rubric: str = ""  # natural-language acceptance criteria (LLM / human judges)
    tags: list[str] = field(default_factory=list)
    split: str = ""  # "train" | "val" | "test" | "" (unassigned)
    files: dict[str, str] = field(default_factory=dict)  # relpath -> text, materialised in the workdir
    meta: dict[str, Any] = field(default_factory=dict)
    weight: float = 1.0
    judge: str = ""  # per-case judge override (registry name)


# --------------------------------------------------------------------------- execution


@dataclass
class Step(Serializable):
    """One event of an agent trajectory."""

    kind: str  # "thought" | "tool_call" | "tool_result" | "message" | "error" | "system"
    content: str = ""
    name: str = ""  # tool name, when relevant
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Trajectory(Serializable):
    """What an adapter returns for one case. Adapters never raise for agent-side failures;
    they set ``status`` instead."""

    case_id: str = ""
    output: str = ""  # the agent's final answer
    steps: list[Step] = field(default_factory=list)
    status: str = "ok"  # "ok" | "error" | "timeout" | "cancelled"
    error: str = ""
    duration_s: float = 0.0
    usage: dict[str, float] = field(default_factory=dict)  # input_tokens, output_tokens, cost_usd, turns...
    artifacts: dict[str, str] = field(default_factory=dict)  # relpath -> text of files the agent produced
    raw: str = ""  # raw transcript / stdout, truncated by the adapter
    full_output: str = ""  # the complete final reply when `output` was narrowed by eval.answer_regex
    attempts: int = 1  # how many times the case was run (eval.max_retries)

    def render(self, max_chars: int = 6000) -> str:
        """Compact human/LLM-readable transcript."""
        lines = []
        for s in self.steps:
            head = f"[{s.kind}{':' + s.name if s.name else ''}]"
            lines.append(f"{head} {s.content}".rstrip())
        if self.error:
            lines.append(f"[status:{self.status}] {self.error}")
        lines.append(f"[final] {self.output}")
        text = "\n".join(lines)
        if len(text) > max_chars:
            half = max_chars // 2
            text = text[:half] + f"\n... <{len(text) - max_chars} chars elided> ...\n" + text[-half:]
        return text


@dataclass
class Judgment(Serializable):
    score: float = 0.0  # 0..1
    passed: bool = False
    reason: str = ""
    judge: str = ""
    labels: list[str] = field(default_factory=list)  # e.g. failure categories suggested by the judge
    details: dict[str, Any] = field(default_factory=dict)
    needs_review: bool = False  # ask a human (low confidence / judge disagreement)
    source: str = "auto"  # "auto" | "human"


@dataclass
class CaseResult(Serializable):
    case_id: str
    trial: int = 0
    skill_version: str = ""
    trajectory: Trajectory = field(default_factory=Trajectory)
    judgment: Judgment = field(default_factory=Judgment)


@dataclass
class EvalReport(Serializable):
    """All results of evaluating one skill version on one set of cases."""

    id: str = field(default_factory=lambda: new_id("ev_"))
    skill_version: str = ""
    split: str = ""
    label: str = ""
    results: list[CaseResult] = field(default_factory=list)
    started_at: float = field(default_factory=now)
    finished_at: float = 0.0
    weights: dict[str, float] = field(default_factory=dict)  # case_id -> weight
    tags: dict[str, list[str]] = field(default_factory=dict)  # case_id -> tags
    cancelled: bool = False

    # ---- aggregation (a case with several trials is averaged first) ----
    def case_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for r in self.results:
            seen.setdefault(r.case_id)
        return list(seen)

    def trials_of(self, case_id: str) -> list[CaseResult]:
        return [r for r in self.results if r.case_id == case_id]

    def case_scores(self) -> dict[str, float]:
        out: dict[str, list[float]] = {}
        for r in self.results:
            out.setdefault(r.case_id, []).append(r.judgment.score)
        return {k: sum(v) / len(v) for k, v in out.items()}

    def case_pass_rates(self) -> dict[str, float]:
        out: dict[str, list[float]] = {}
        for r in self.results:
            out.setdefault(r.case_id, []).append(1.0 if r.judgment.passed else 0.0)
        return {k: sum(v) / len(v) for k, v in out.items()}

    def case_passed(self) -> dict[str, bool]:
        """A case passes when the majority of its trials pass (ties count as fail)."""
        return {k: v > 0.5 for k, v in self.case_pass_rates().items()}

    def _weighted(self, per_case: dict[str, float]) -> float:
        if not per_case:
            return 0.0
        total = sum(self.weights.get(c, 1.0) for c in per_case)
        if total <= 0:
            return 0.0
        return sum(v * self.weights.get(c, 1.0) for c, v in per_case.items()) / total

    @property
    def pass_rate(self) -> float:
        return self._weighted(self.case_pass_rates())

    @property
    def mean_score(self) -> float:
        return self._weighted(self.case_scores())

    @property
    def score_lcb(self) -> float:
        """95% lower confidence bound of the mean per-case score (normal approximation)."""
        scores = list(self.case_scores().values())
        n = len(scores)
        if n == 0:
            return 0.0
        mean = sum(scores) / n
        if n == 1:
            return mean
        var = sum((s - mean) ** 2 for s in scores) / (n - 1)
        return max(0.0, mean - 1.96 * math.sqrt(var / n))

    def failures(self) -> list[CaseResult]:
        """Worst trial of every case that did not pass."""
        passed = self.case_passed()
        out = []
        for cid in self.case_ids():
            if passed.get(cid):
                continue
            trials = self.trials_of(cid)
            out.append(min(trials, key=lambda r: r.judgment.score))
        return out

    def successes(self) -> list[CaseResult]:
        passed = self.case_passed()
        return [max(self.trials_of(c), key=lambda r: r.judgment.score) for c in self.case_ids() if passed.get(c)]

    def by_tag(self) -> dict[str, dict[str, float]]:
        scores, passes = self.case_scores(), self.case_pass_rates()
        groups: dict[str, list[str]] = {}
        for cid in scores:
            for t in self.tags.get(cid, []) or ["(untagged)"]:
                groups.setdefault(t, []).append(cid)
        return {
            t: {
                "n": len(cids),
                "pass_rate": sum(passes[c] for c in cids) / len(cids),
                "mean_score": sum(scores[c] for c in cids) / len(cids),
            }
            for t, cids in sorted(groups.items())
        }

    def usage(self) -> dict[str, float]:
        total: dict[str, float] = {}
        for r in self.results:
            for k, v in r.trajectory.usage.items():
                if isinstance(v, (int, float)):
                    total[k] = total.get(k, 0.0) + v
        return total

    def summary(self) -> dict[str, Any]:
        statuses: dict[str, int] = {}
        for r in self.results:
            statuses[r.trajectory.status] = statuses.get(r.trajectory.status, 0) + 1
        return {
            "eval_id": self.id,
            "skill_version": self.skill_version,
            "split": self.split,
            "label": self.label,
            "n_cases": len(self.case_ids()),
            "n_runs": len(self.results),
            "pass_rate": round(self.pass_rate, 4),
            "mean_score": round(self.mean_score, 4),
            "score_lcb": round(self.score_lcb, 4),
            "n_needs_review": sum(1 for r in self.results if r.judgment.needs_review),
            "statuses": statuses,
            "duration_s": round(max(0.0, self.finished_at - self.started_at), 2),
            "usage": self.usage(),
            "cancelled": self.cancelled,
        }


# --------------------------------------------------------------------------- versions


@dataclass
class SkillVersion(Serializable):
    """Manifest of one stored skill version (the skill files live next to it)."""

    id: str  # "v001"
    parent: str = ""
    origin: str = "manual"  # "seed" | "bootstrap" | "evolve" | "manual" | "import"
    status: str = "candidate"  # "candidate" | "accepted" | "rejected" | "best"
    summary: str = ""  # what changed vs parent, human readable
    iteration: int = 0
    content_hash: str = ""
    created_at: float = field(default_factory=now)
    scores: dict[str, dict[str, Any]] = field(default_factory=dict)  # split -> EvalReport.summary()
    patches: list[dict[str, Any]] = field(default_factory=list)  # edits that produced this version
    gate: dict[str, Any] = field(default_factory=dict)  # promotion-gate decision
    tags: list[str] = field(default_factory=list)

    def score(self, split: str = "val", key: str = "mean_score") -> float | None:
        s = self.scores.get(split)
        return None if not s else s.get(key)


# --------------------------------------------------------------------------- error book


@dataclass
class ErrorEntry(Serializable):
    """One entry of the 错题库: a failed case with enough context to reproduce and learn from."""

    id: str = field(default_factory=lambda: new_id("err_"))
    case_id: str = ""
    skill_version: str = ""
    eval_id: str = ""
    iteration: int = 0
    input: str = ""
    expected: Any = None
    output: str = ""
    score: float = 0.0
    judge_reason: str = ""
    trajectory_excerpt: str = ""
    status: str = "open"  # "open" | "fixed" | "regressed" | "wontfix"
    category: str = ""  # root-cause category, e.g. "missing_rule", "format", "tool_misuse"
    root_cause: str = ""
    lesson: str = ""  # reusable lesson distilled from the failure
    suggested_fix: str = ""
    tags: list[str] = field(default_factory=list)
    occurrences: int = 1  # how many evaluations this case failed in
    first_seen: float = field(default_factory=now)
    last_seen: float = field(default_factory=now)
    fixed_in: str = ""  # skill version that first passed it again
    repro: str = ""  # how to reproduce (command / steps)
