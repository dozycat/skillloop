"""Judge contract. A judge turns (case, trajectory) into a :class:`Judgment` (score 0..1 + pass flag).

Register implementations with ``@JUDGES.register("name")``; they are constructed as
``Judge(**options)`` and then bound to a :class:`JudgeContext`."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..core.models import Case, Judgment, Trajectory
from ..core.registry import JUDGES
from ..llm.base import LLMClient


@dataclass
class JudgeContext:
    llm: LLMClient | None = None  # for LLM-backed judges
    pass_threshold: float = 0.7  # graded score >= threshold counts as pass
    project_root: Path | None = None  # for script judges resolving relative paths


class Judge(ABC):
    name = "judge"

    def __init__(self, **options: Any):
        self.options = options
        self.ctx = JudgeContext()

    def bind(self, ctx: JudgeContext) -> Judge:
        self.ctx = ctx
        return self

    @abstractmethod
    def judge(self, case: Case, trajectory: Trajectory) -> Judgment:
        """Score a finished (status == "ok") trajectory. Agent errors/timeouts never reach the judge."""

    def verdict(self, score: float, reason: str = "", **kw: Any) -> Judgment:
        """Convenience: clamp the score and derive ``passed`` from the context threshold."""
        score = max(0.0, min(1.0, float(score)))
        passed = kw.pop("passed", None)
        return Judgment(
            score=score,
            passed=score >= self.ctx.pass_threshold if passed is None else bool(passed),
            reason=reason,
            judge=self.name,
            **kw,
        )


def build_judge(name: str, options: dict[str, Any] | None = None, ctx: JudgeContext | None = None) -> Judge:
    return JUDGES.create(name, **(options or {})).bind(ctx or JudgeContext())
