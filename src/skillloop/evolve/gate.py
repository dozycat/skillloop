"""提升最佳版本的闸门: a candidate replaces the best version only if every check passes.

Checks are explicit and recorded, so a promotion (or a rejection) can always be explained."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.config import EvolveSection
from ..core.models import Serializable, now
from .compare import EPS, Comparison


@dataclass
class GateDecision(Serializable):
    promoted: bool = False
    passed: bool = False  # all automatic checks passed (may still await human approval)
    needs_approval: bool = False
    reasons: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)  # {"name", "passed", "detail"}
    comparison: dict[str, Any] = field(default_factory=dict)
    test_comparison: dict[str, Any] = field(default_factory=dict)
    decided_at: float = field(default_factory=now)
    approved_by: str = ""


def decide(
    cmp: Comparison, cfg: EvolveSection, *, guard_problems: list[str] | None = None, test_cmp: Comparison | None = None
) -> GateDecision:
    d = GateDecision(comparison=cmp.to_dict(), test_comparison=test_cmp.to_dict() if test_cmp else {})

    def check(name: str, ok: bool, detail: str) -> None:
        d.checks.append({"name": name, "passed": bool(ok), "detail": detail})
        if not ok:
            d.reasons.append(f"{name}: {detail}")

    check("guardrails", not guard_problems, "; ".join(guard_problems or []) or "format, size and leakage checks clean")
    check("evidence", cmp.n_common > 0, f"{cmp.n_common} cases compared")
    check(
        "improvement",
        cmp.delta >= cfg.min_delta - EPS,
        f"{cfg.metric} {cmp.base_score:.3f} -> {cmp.cand_score:.3f} (Δ{cmp.delta:+.3f}, need ≥ {cfg.min_delta:+.3f})",
    )
    check(
        "regressions",
        len(cmp.regressed) <= cfg.max_regressions,
        f"{len(cmp.regressed)} case(s) regressed (allowed {cfg.max_regressions})"
        + (": " + ", ".join(cmp.regressed[:8]) if cmp.regressed else ""),
    )
    if cfg.protect_tags:
        check(
            "protected",
            not cmp.protected_regressions,
            "protected cases regressed: " + ", ".join(cmp.protected_regressions[:8])
            if cmp.protected_regressions
            else f"no regression on tags {cfg.protect_tags}",
        )
    if cfg.confirm_on_test and test_cmp is not None and test_cmp.n_common:
        check(
            "held_out",
            test_cmp.delta >= -EPS,
            f"held-out test {test_cmp.base_score:.3f} -> {test_cmp.cand_score:.3f} (Δ{test_cmp.delta:+.3f})",
        )
    d.passed = all(c["passed"] for c in d.checks)
    if d.passed and cfg.require_human_approval:
        d.needs_approval = True
        d.reasons.append("awaiting human approval")
    d.promoted = d.passed and not d.needs_approval
    if d.promoted:
        d.reasons.append(
            f"promoted: {cfg.metric} improved by {cmp.delta:+.3f} with "
            f"{len(cmp.fixed)} fixed / {len(cmp.regressed)} regressed"
        )
    return d
