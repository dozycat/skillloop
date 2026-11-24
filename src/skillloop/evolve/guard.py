"""Deterministic guardrails applied to every candidate skill before it is spent evaluation budget on.

They catch the cheap, common ways an LLM-edited skill goes wrong: invalid format, runaway growth,
no-op edits, and - most importantly - *answer leakage*, where the "improvement" is just the gold
answers of the training cases pasted into the skill (which would score well and generalise to nothing)."""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ..core.config import EvolveSection
from ..core.models import Case
from ..core.skill import Skill

MIN_LEAK_LEN = 12  # shorter gold answers ("yes", "42") are too generic to flag by substring
MIN_INPUT_LEAK_LEN = 60


def _norm(s: Any) -> str:
    return " ".join(str(s).lower().split())


def _gold_strings(case: Case) -> Iterable[str]:
    exp = case.expected
    values = exp if isinstance(exp, list) else [exp]
    for v in values:
        if isinstance(v, (str, int, float)) and not isinstance(v, bool):
            yield str(v)


def find_leaks(candidate: Skill, parent: Skill | None, cases: list[Case]) -> list[str]:
    """Case ids whose gold answer (or full input) appears verbatim in the candidate but not in the parent."""
    new_text = _norm("\n".join(candidate.all_files().values()))
    old_text = _norm("\n".join(parent.all_files().values())) if parent else ""
    leaked = []
    for c in cases:
        hit = False
        for gold in _gold_strings(c):
            g = _norm(gold)
            if len(g) >= MIN_LEAK_LEN and g in new_text and g not in old_text:
                hit = True
        q = _norm(c.input)
        if len(q) >= MIN_INPUT_LEAK_LEN and q in new_text and q not in old_text:
            hit = True
        if hit:
            leaked.append(c.id)
    return leaked


def scrub_case_ids(skill: Skill, cases: list[Case]) -> Skill:
    """Remove literal case ids from the body: a skill must describe situations, not test cases."""
    ids = sorted((c.id for c in cases if len(c.id) >= 4), key=len, reverse=True)
    if not ids:
        return skill
    pattern = re.compile(r"\s*\(?\b(?:case|用例)?\s*(?:" + "|".join(re.escape(i) for i in ids) + r")\b\)?")
    out = skill.copy()
    out.body = pattern.sub("", out.body)
    return out


def check_candidate(candidate: Skill, parent: Skill, cases: list[Case], cfg: EvolveSection) -> list[str]:
    """Return the list of guard violations (empty == candidate may be evaluated)."""
    problems = list(candidate.validate(max_chars=cfg.max_skill_chars))
    if candidate.content_hash() == parent.content_hash():
        problems.append("candidate is identical to its parent (no edit applied)")
    if candidate.name != parent.name:
        problems.append(f"skill name changed from {parent.name!r} to {candidate.name!r}")
    psize = max(parent.size(), 400)
    if cfg.max_growth_ratio and candidate.size() > psize * cfg.max_growth_ratio:
        problems.append(
            f"candidate grew {candidate.size() / psize:.1f}x over its parent "
            f"(limit {cfg.max_growth_ratio}x) - likely case-specific bloat"
        )
    leaks = find_leaks(candidate, parent, cases) if cfg.leak_check else []
    if leaks:
        problems.append("answer leakage: gold answers/inputs of cases copied into the skill: " + ", ".join(leaks[:8]))
    return problems
