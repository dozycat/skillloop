"""指标摘要: gather everything the reports need from a project, tolerating missing/partial data."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from ..core.models import EvalReport, SkillVersion
from ..evolve.compare import Comparison, compare_reports
from ..evolve.errorbook import ErrorBook

if TYPE_CHECKING:
    from ..core.project import Project

SPLIT_ORDER = ("val", "test", "train")
_SCORE_KEYS = ("pass_rate", "mean_score", "score_lcb", "n_cases", "n_runs", "eval_id")


def _num(v: Any, default: float | None = None) -> float | None:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else default


def load_history(project: Project) -> list[dict[str, Any]]:
    """Loop history (``.skillloop/history.json``); [] when absent or unreadable."""
    try:
        p = project.state.path("history.json")
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def safe_versions(project: Project) -> list[SkillVersion]:
    out = []
    try:
        ids = project.versions.ids()
    except OSError:
        return out
    for vid in ids:
        try:
            out.append(project.versions.get(vid))
        except (KeyError, ValueError, OSError):
            continue
    return out


def safe_best(project: Project) -> SkillVersion | None:
    try:
        return project.versions.best()
    except (KeyError, ValueError, OSError):
        return None


def _splits(scores: dict[str, Any]) -> list[str]:
    return [s for s in SPLIT_ORDER if s in scores] + sorted(s for s in scores if s not in SPLIT_ORDER)


def _slim(scores: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        s: {k: scores[s][k] for k in _SCORE_KEYS if k in scores[s]}
        for s in _splits(scores)
        if isinstance(scores.get(s), dict)
    }


def load_version_eval(project: Project, version: SkillVersion, split: str) -> EvalReport | None:
    eval_id = (version.scores.get(split) or {}).get("eval_id") if isinstance(version.scores.get(split), dict) else None
    if not eval_id:
        return None
    try:
        return project.load_eval(str(eval_id))
    except (KeyError, ValueError, OSError):
        return None


def latest_eval(project: Project, version: SkillVersion) -> EvalReport | None:
    """Most recent stored evaluation of ``version`` (artifact index first, then manifest scores)."""
    try:
        summaries = project.list_evals()
    except Exception:  # noqa: BLE001 - a broken index must not break reporting
        summaries = []
    ids = [s.get("eval_id") for s in summaries if isinstance(s, dict) and s.get("skill_version") == version.id]
    ids += [
        (version.scores.get(s) or {}).get("eval_id")
        for s in _splits(version.scores)
        if isinstance(version.scores.get(s), dict)
    ]
    for eval_id in ids:
        if not eval_id:
            continue
        try:
            return project.load_eval(str(eval_id))
        except (KeyError, ValueError, OSError):
            continue
    return None


def comparison_between(project: Project, base: SkillVersion, cand: SkillVersion) -> Comparison | None:
    """Paired comparison of two versions on the first split (val > test > train) both were evaluated on.
    Falls back to the comparison stored in ``cand.gate`` when it was made against ``base``."""
    cfg = project.config.evolve
    for split in _splits(cand.scores):
        b, c = load_version_eval(project, base, split), load_version_eval(project, cand, split)
        if b is not None and c is not None:
            cmp = compare_reports(b, c, metric=cfg.metric, protect_tags=cfg.protect_tags)
            if cmp.n_common:
                cmp.base_version, cmp.cand_version = cmp.base_version or base.id, cmp.cand_version or cand.id
                cmp.split = cmp.split or split
                return cmp
    stored = cand.gate.get("comparison") if isinstance(cand.gate, dict) else None
    if isinstance(stored, dict) and stored.get("base_version", base.id) == base.id:
        try:
            return Comparison.from_dict(stored)
        except (TypeError, ValueError):
            return None
    return None


def metrics_summary(project: Project) -> dict[str, Any]:
    """Key metrics and trends as a JSON-able dict (指标摘要)."""
    metric = project.config.evolve.metric
    versions = safe_versions(project)
    best = safe_best(project)
    history = load_history(project)
    first = versions[0] if versions else None

    trend = [
        {
            "iteration": r.get("iteration", i),
            "best_version": r.get("best_version", ""),
            "best_score": _num(r.get("best_score")),
            "train_score": _num(r.get("train_score")),
        }
        for i, r in enumerate(history)
    ]

    promoted = sum(1 for r in history for c in r.get("candidates") or [] if isinstance(c, dict) and c.get("promoted"))
    rejected = sum(
        1 for r in history for c in r.get("candidates") or [] if isinstance(c, dict) and c.get("promoted") is False
    )
    if not promoted and not rejected:
        gates = [v.gate for v in versions if isinstance(v.gate, dict) and "promoted" in v.gate]
        promoted, rejected = sum(1 for g in gates if g["promoted"]), sum(1 for g in gates if not g["promoted"])

    improvement = {}
    if best and first and best.id != first.id:
        for split in _splits(best.scores):
            b, f = _num((best.scores.get(split) or {}).get(metric)), _num((first.scores.get(split) or {}).get(metric))
            if b is not None and f is not None:
                improvement[split] = round(b - f, 4)

    try:
        evals = [e for e in project.list_evals() if isinstance(e, dict)]
    except Exception:  # noqa: BLE001
        evals = []
    usage: dict[str, float] = {}
    for e in evals:
        for k, v in (e.get("usage") or {}).items():
            if _num(v) is not None:
                usage[k] = round(usage.get(k, 0.0) + float(v), 6)

    try:
        errorbook = ErrorBook(project.errorbook_path).stats()
    except (OSError, ValueError):
        errorbook = {}

    return {
        "project": project.name,
        "generated_at": time.time(),
        "metric": metric,
        "best_version": best.id if best else "",
        "best_scores": _slim(best.scores) if best else {},
        "first_version": first.id if first else "",
        "first_scores": _slim(first.scores) if first else {},
        "improvement": improvement,
        "n_versions": len(versions),
        "n_iterations": len(history),
        "promotions": promoted,
        "rejections": rejected,
        "trend": trend,
        "versions": [
            {
                "id": v.id,
                "parent": v.parent,
                "origin": v.origin,
                "status": v.status,
                "iteration": v.iteration,
                "summary": v.summary,
                "scores": _slim(v.scores),
                "promoted": v.gate.get("promoted") if isinstance(v.gate, dict) else None,
                "created_at": v.created_at,
            }
            for v in versions
        ],
        "errorbook": errorbook,
        "totals": {
            "n_evals": len(evals),
            "n_runs": int(sum(_num(e.get("n_runs"), 0.0) or 0 for e in evals)),
            "duration_s": round(sum(_num(e.get("duration_s"), 0.0) or 0 for e in evals), 2),
            "cost_usd": round(usage.get("cost_usd", 0.0), 6),
            "usage": usage,
        },
    }
