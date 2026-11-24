"""人工判分 / Review 标注表: queue results for human review, export/import a CSV annotation sheet,
fold human verdicts back into reports, and measure judge-vs-human agreement."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..core.models import EvalReport

if TYPE_CHECKING:
    from ..core.project import Project

COLUMNS = [
    "eval_id",
    "case_id",
    "trial",
    "input",
    "expected",
    "output",
    "auto_score",
    "auto_passed",
    "auto_reason",
    "judge",
    "labels",
    "human_score",
    "human_passed",
    "comment",
    "reviewer",
]
_TRUE = {"1", "true", "yes", "y", "pass", "passed", "t", "是", "对", "通过", "✓"}
_FALSE = {"0", "false", "no", "n", "fail", "failed", "f", "否", "错", "不通过", "✗"}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _bool_cell(value: bool | None) -> str:
    return "" if value is None else ("true" if value else "false")


def _parse_bool(raw: str) -> bool | None:
    s = raw.strip().lower()
    if not s:
        return None
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    raise ValueError(f"cannot read {raw!r} as pass/fail")


def enqueue_reviews(
    project: Project, report: EvalReport, *, mode: str = "needs_review", sample_rate: float = 0.0, seed: int = 0
) -> int:
    """Put results into the review queue. ``mode``: "needs_review" (flagged by the judge), "failures"
    (flagged + every failed result) or "all". ``sample_rate`` additionally samples the remaining results
    (deterministically) so judge quality can be audited. Returns the number of queued results."""
    if mode not in ("needs_review", "failures", "all"):
        raise ValueError("mode must be 'needs_review', 'failures' or 'all'")
    n = 0
    for r in report.results:
        j = r.judgment
        pick = mode == "all" or j.needs_review or (mode == "failures" and not j.passed)
        if not pick and sample_rate > 0:
            h = hashlib.sha1(f"{seed}:{report.id}:{r.case_id}:{r.trial}".encode()).hexdigest()
            pick = int(h[:8], 16) / 0xFFFFFFFF < sample_rate
        if pick:
            project.store.upsert_review(
                report.id, r.case_id, r.trial, auto_score=j.score, auto_passed=j.passed, auto_reason=j.reason
            )
            n += 1
    return n


def _resolve(project: Project, path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else project.path(p)


def export_review_sheet(project: Project, report: EvalReport, path: str | Path) -> Path:
    """Write the Review 标注表 (CSV, utf-8-sig) for every result of ``report``; stored human reviews are
    prefilled. Reviewers fill ``human_score`` and/or ``human_passed`` (+ ``comment``, ``reviewer``)."""
    cases = {c.id: c for c in project.load_cases()}
    done = {
        (r["case_id"], r["trial"]): r for r in project.store.reviews(eval_id=report.id, status="done", limit=100000)
    }
    p = _resolve(project, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in report.results:
            case, j, h = cases.get(r.case_id), r.judgment, done.get((r.case_id, r.trial))
            auto = j if j.source != "human" else None
            w.writerow(
                {
                    "eval_id": report.id,
                    "case_id": r.case_id,
                    "trial": r.trial,
                    "input": case.input if case else "",
                    "expected": _text(case.expected) if case else "",
                    "output": r.trajectory.output,
                    "auto_score": (h or {}).get("auto_score") if auto is None else round(j.score, 4),
                    "auto_passed": _bool_cell((h or {}).get("auto_passed") if auto is None else j.passed),
                    "auto_reason": (h or {}).get("auto_reason", "") if auto is None else j.reason,
                    "judge": j.judge,
                    "labels": ";".join(j.labels),
                    "human_score": "" if not h or h["human_score"] is None else h["human_score"],
                    "human_passed": _bool_cell(h["human_passed"]) if h else "",
                    "comment": h["comment"] if h else "",
                    "reviewer": h["reviewer"] if h else "",
                }
            )
    return p


def import_review_sheet(project: Project, path: str | Path) -> dict[str, Any]:
    """Read a filled sheet back. Rows with ``human_score`` or ``human_passed`` are stored as human reviews.
    Returns ``{"imported", "unfilled", "skipped": [{"row", "reason"}], "eval_ids"}``."""
    p = _resolve(project, path)
    threshold = project.config.eval.pass_threshold
    out: dict[str, Any] = {"imported": 0, "unfilled": 0, "skipped": [], "eval_ids": []}
    existing: dict[str, set[tuple[str, int]]] = {}
    with p.open(newline="", encoding="utf-8-sig") as f:
        for i, row in enumerate(csv.DictReader(f), start=2):
            row = {k: (v or "").strip() for k, v in row.items() if k}
            hs, hp = row.get("human_score", ""), row.get("human_passed", "")
            if not hs and not hp:
                out["unfilled"] += 1
                continue
            eval_id, case_id = row.get("eval_id", ""), row.get("case_id", "")
            if not eval_id or not case_id:
                out["skipped"].append({"row": i, "reason": "missing eval_id or case_id"})
                continue
            try:
                trial = int(row.get("trial") or 0)
                passed = _parse_bool(hp)
                score = float(hs) if hs else (1.0 if passed else 0.0)
                if not 0.0 <= score <= 1.0:
                    raise ValueError(f"human_score {score} is outside 0..1")
                if passed is None:
                    passed = score >= threshold
            except ValueError as e:
                out["skipped"].append({"row": i, "reason": str(e)})
                continue
            if eval_id not in existing:
                existing[eval_id] = {
                    (r["case_id"], r["trial"]) for r in project.store.reviews(eval_id=eval_id, limit=100000)
                }
            if (case_id, trial) not in existing[eval_id]:
                try:
                    a_score = float(row["auto_score"]) if row.get("auto_score") else None
                    a_passed = _parse_bool(row.get("auto_passed", ""))
                except ValueError:
                    a_score = a_passed = None
                project.store.upsert_review(
                    eval_id,
                    case_id,
                    trial,
                    auto_score=a_score,
                    auto_passed=a_passed,
                    auto_reason=row.get("auto_reason", ""),
                )
                existing[eval_id].add((case_id, trial))
            project.store.submit_review(
                eval_id,
                case_id,
                trial,
                score=score,
                passed=passed,
                comment=row.get("comment", ""),
                reviewer=row.get("reviewer", ""),
            )
            out["imported"] += 1
            if eval_id not in out["eval_ids"]:
                out["eval_ids"].append(eval_id)
    return out


def apply_reviews(project: Project, report: EvalReport) -> EvalReport:
    """Copy of ``report`` in which stored human reviews override the automatic judgments."""
    out = EvalReport.from_dict(report.to_dict())
    done = {
        (r["case_id"], r["trial"]): r for r in project.store.reviews(eval_id=report.id, status="done", limit=100000)
    }
    for res in out.results:
        h = done.get((res.case_id, res.trial))
        if h is None or h["human_score"] is None:
            continue
        j = res.judgment
        if j.source != "human":
            j.details = {
                **j.details,
                "auto": {"score": j.score, "passed": j.passed, "reason": j.reason, "judge": j.judge},
            }
        j.score, j.passed = float(h["human_score"]), bool(h["human_passed"])
        j.reason = h["comment"] or f"human review ({'pass' if j.passed else 'fail'})"
        if h["reviewer"]:
            j.details = {**j.details, "reviewer": h["reviewer"]}
        j.source, j.needs_review = "human", False
    return out


def agreement(project: Project, eval_id: str = "") -> dict[str, Any]:
    """Judge-vs-human calibration over completed reviews (optionally of one evaluation)."""
    rows = [
        r
        for r in project.store.reviews(eval_id=eval_id, status="done", limit=100000)
        if r["auto_passed"] is not None and r["human_passed"] is not None
    ]
    n = len(rows)
    out: dict[str, Any] = {
        "n": n,
        "pass_agreement": None,
        "kappa": None,
        "mean_abs_score_diff": None,
        "disagreements": [],
    }
    if not n:
        return out
    agree = sum(1 for r in rows if r["auto_passed"] == r["human_passed"])
    po = agree / n
    pa, ph = sum(r["auto_passed"] for r in rows) / n, sum(r["human_passed"] for r in rows) / n
    pe = pa * ph + (1 - pa) * (1 - ph)
    diffs = [
        abs(r["auto_score"] - r["human_score"])
        for r in rows
        if r["auto_score"] is not None and r["human_score"] is not None
    ]
    out.update(
        pass_agreement=round(po, 4),
        kappa=round((po - pe) / (1 - pe), 4) if pe < 1 else (1.0 if po == 1 else 0.0),
        mean_abs_score_diff=round(sum(diffs) / len(diffs), 4) if diffs else None,
        disagreements=[
            {
                "eval_id": r["eval_id"],
                "case_id": r["case_id"],
                "trial": r["trial"],
                "auto_passed": r["auto_passed"],
                "human_passed": r["human_passed"],
                "auto_score": r["auto_score"],
                "human_score": r["human_score"],
                "auto_reason": r["auto_reason"],
                "comment": r["comment"],
            }
            for r in rows
            if r["auto_passed"] != r["human_passed"]
        ],
    )
    return out
