"""错题库: a persistent, per-project record of failed cases - context, root cause, lesson, status.

One entry per case id. Each evaluation of a lineage version is folded in with
:meth:`ErrorBook.record_eval`: failing cases are added/updated (``open`` or ``regressed``), and
cases that now pass are marked ``fixed``. Analysts attach root causes via :meth:`annotate`."""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from ..core.models import Case, ErrorEntry, EvalReport


class ErrorBook:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._entries: dict[str, ErrorEntry] = {}
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    e = ErrorEntry.from_dict(json.loads(line))
                    self._entries[e.case_id] = e

    # ---------------------------------------------------------------- persistence
    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                "".join(json.dumps(e.to_dict(), ensure_ascii=False) + "\n" for e in self._entries.values()),
                encoding="utf-8",
            )
            tmp.replace(self.path)

    # ---------------------------------------------------------------- queries
    def entries(self, status: str = "") -> list[ErrorEntry]:
        with self._lock:
            items = list(self._entries.values())
        if status:
            items = [e for e in items if e.status == status]
        return sorted(items, key=lambda e: (-e.occurrences, e.case_id))

    def get(self, case_id: str) -> ErrorEntry | None:
        return self._entries.get(case_id)

    def open_entries(self) -> list[ErrorEntry]:
        return [e for e in self.entries() if e.status in ("open", "regressed")]

    def __len__(self) -> int:
        return len(self._entries)

    # ---------------------------------------------------------------- updates
    def record_eval(
        self, report: EvalReport, cases: list[Case], iteration: int = 0, repro: str = ""
    ) -> dict[str, list[str]]:
        """Fold one evaluation in. Returns case ids by transition: new / recurring / fixed / regressed."""
        by_id = {c.id: c for c in cases}
        passed = report.case_passed()
        failures = {r.case_id: r for r in report.failures()}
        out: dict[str, list[str]] = {"new": [], "recurring": [], "fixed": [], "regressed": []}
        now = time.time()
        with self._lock:
            for cid, res in failures.items():
                case = by_id.get(cid)
                e = self._entries.get(cid)
                if e is None:
                    e = self._entries[cid] = ErrorEntry(case_id=cid, first_seen=now, occurrences=0)
                    out["new"].append(cid)
                elif e.status == "fixed":
                    e.status = "regressed"
                    out["regressed"].append(cid)
                else:
                    out["recurring"].append(cid)
                if e.status not in ("regressed", "wontfix"):
                    e.status = "open"
                e.occurrences += 1
                e.last_seen = now
                e.skill_version, e.eval_id, e.iteration = report.skill_version, report.id, iteration
                e.score = res.judgment.score
                e.judge_reason = res.judgment.reason
                e.output = res.trajectory.output[:4000]
                e.trajectory_excerpt = res.trajectory.render(3000)
                if case is not None:
                    e.input, e.expected, e.tags = case.input, case.expected, list(case.tags)
                e.repro = repro or f"skillloop eval --version {report.skill_version} --case {cid}"
                for label in res.judgment.labels:
                    if label not in e.tags:
                        e.tags.append(label)
            for cid, ok in passed.items():
                e = self._entries.get(cid)
                if ok and e is not None and e.status in ("open", "regressed"):
                    e.status, e.fixed_in = "fixed", report.skill_version
                    out["fixed"].append(cid)
            self.save()
        return out

    def annotate(
        self,
        case_id: str,
        *,
        category: str = "",
        root_cause: str = "",
        lesson: str = "",
        suggested_fix: str = "",
        status: str = "",
    ) -> bool:
        with self._lock:
            e = self._entries.get(case_id)
            if e is None:
                return False
            e.category = category or e.category
            e.root_cause = root_cause or e.root_cause
            e.lesson = lesson or e.lesson
            e.suggested_fix = suggested_fix or e.suggested_fix
            if status:
                e.status = status
            self.save()
            return True

    # ---------------------------------------------------------------- statistics (统计根因)
    def stats(self) -> dict[str, Any]:
        items = self.entries()
        open_items = [e for e in items if e.status in ("open", "regressed")]
        return {
            "total": len(items),
            "open": sum(1 for e in items if e.status == "open"),
            "regressed": sum(1 for e in items if e.status == "regressed"),
            "fixed": sum(1 for e in items if e.status == "fixed"),
            "wontfix": sum(1 for e in items if e.status == "wontfix"),
            "by_category": dict(Counter(e.category or "(unanalysed)" for e in open_items).most_common()),
            "by_tag": dict(Counter(t for e in open_items for t in e.tags).most_common(20)),
            "top_recurring": [
                {"case_id": e.case_id, "occurrences": e.occurrences, "category": e.category, "root_cause": e.root_cause}
                for e in open_items[:10]
            ],
            "lessons": list(dict.fromkeys(e.lesson for e in items if e.lesson))[:50],
        }
