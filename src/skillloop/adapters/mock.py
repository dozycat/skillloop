"""Simulated agent for offline runs, demos and tests (see :mod:`skillloop.llm.mock` for the world rules).

A case succeeds iff the skill text contains every marker listed in ``case.meta["requires"]``
(as ``[marker]``). ``case.meta["hints"]`` maps marker -> the guidance that would fix it; the
failing trajectory exposes it the way a real trace exposes what the agent was missing."""

from __future__ import annotations

import hashlib
import time
from typing import Any

from ..core.models import Step, Trajectory
from ..core.registry import ADAPTERS
from .base import AgentAdapter, AgentRequest


@ADAPTERS.register("mock")
class MockAdapter(AgentAdapter):
    name = "mock"

    def __init__(self, flakiness: float = 0.0, delay_s: float = 0.0, seed: int = 0, **options: Any):
        super().__init__(**options)
        self.flakiness = flakiness  # probability that an otherwise-correct answer goes wrong
        self.delay_s = delay_s
        self.seed = seed
        self._calls: dict[str, int] = {}

    def run(self, request: AgentRequest) -> Trajectory:
        case, skill = request.case, request.skill
        t0 = time.time()
        if self.delay_s and request.cancel.wait(self.delay_s):
            return Trajectory(case_id=case.id, status="cancelled", error="cancelled", duration_s=time.time() - t0)
        text = skill.render_for_prompt().lower()
        requires = [str(m) for m in case.meta.get("requires", [])]
        hints = case.meta.get("hints", {}) or {}
        missing = [m for m in requires if f"[{m.lower()}]" not in text]
        steps = [Step("thought", f"Task received. Consulting skill '{skill.name}'.")]
        for m in requires:
            if m in missing:
                steps.append(Step("thought", f"MISSING_GUIDANCE: {m} :: {hints.get(m, '')}".rstrip(" :")))
            else:
                steps.append(Step("thought", f"Applying rule [{m}] from the skill."))
        n = self._calls[case.id] = self._calls.get(case.id, 0) + 1
        roll = int(hashlib.sha1(f"{self.seed}:{case.id}:{n}".encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
        flaked = not missing and roll < self.flakiness
        if missing or flaked:
            output = str(case.meta.get("wrong_answer", "I am not sure how to handle this."))
            if flaked:
                steps.append(Step("thought", "Got distracted and answered carelessly."))
        else:
            output = str(case.expected if case.expected is not None else "done")
        steps.append(Step("message", output))
        return Trajectory(
            case_id=case.id,
            output=output,
            steps=steps,
            status="ok",
            duration_s=time.time() - t0,
            usage={"input_tokens": len(text) // 4, "output_tokens": len(output) // 4, "turns": 1},
        )
