"""输出: write every deliverable into ``<project>/outputs`` and register it in the 产物索引."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from ..evolve.errorbook import ErrorBook
from .html import render_html_report
from .markdown import render_comparison_md, render_errorbook_md, render_metrics_md
from .metrics import comparison_between, latest_eval, metrics_summary, safe_best, safe_versions

if TYPE_CHECKING:
    from ..core.jobs import JobContext
    from ..core.project import Project


def write_outputs(project: Project, ctx: JobContext | None = None) -> dict[str, str]:
    """Write best/, review_sheet.csv, errorbook.md/.jsonl, version_compare.md, metrics_summary.json/.md
    and report.html. Returns {output_name: project-relative path}; outputs that cannot be produced
    are omitted and explained under the ``"skipped"`` key (a JSON object string) when any exist."""
    out_dir = project.outputs_dir
    best = safe_best(project)
    best_id = best.id if best else ""
    manifest: dict[str, str] = {}
    skipped: dict[str, str] = {}

    def done(name: str, kind: str, path: Path, title: str, version: str = "") -> None:
        rel = path.relative_to(project.root).as_posix()
        manifest[name] = rel
        if ctx is not None:
            ctx.artifact(kind, rel, version=version, title=title)

    def write(name: str, kind: str, filename: str, text: str, title: str, version: str = "") -> None:
        path = out_dir / filename
        path.write_text(text, encoding="utf-8")
        done(name, kind, path, title, version)

    # 最佳 SKILL.md
    if best is not None:
        try:
            best_dir = out_dir / "best"
            shutil.rmtree(best_dir, ignore_errors=True)
            project.versions.export(best.id, best_dir)
            done("best_skill", "best_skill", best_dir, f"最佳 Skill {best.id}", best.id)
        except (OSError, KeyError) as e:
            skipped["best_skill"] = f"could not export {best.id}: {e}"
    else:
        skipped["best_skill"] = "no best version yet"

    # Review 标注表
    report = latest_eval(project, best) if best is not None else None
    if report is None:
        skipped["review_sheet"] = "no stored evaluation of the best version"
    else:
        try:
            from ..eval.review import export_review_sheet

            path = Path(export_review_sheet(project, report, out_dir / "review_sheet.csv"))
            done("review_sheet", "review_sheet", path, f"Review 标注表 {best_id}", best_id)
        except ImportError:
            skipped["review_sheet"] = "skillloop.eval.review is not available"
        except Exception as e:  # noqa: BLE001 - one failing output must not block the others
            skipped["review_sheet"] = f"{type(e).__name__}: {e}"

    # 错题库
    book = ErrorBook(project.errorbook_path)
    write("errorbook_md", "errorbook", "errorbook.md", render_errorbook_md(book), "错题库")
    write(
        "errorbook_jsonl",
        "errorbook",
        "errorbook.jsonl",
        "".join(json.dumps(e.to_dict(), ensure_ascii=False) + "\n" for e in book.entries()),
        "错题库 (JSONL)",
    )

    # 版本比较报告
    if best is not None:
        versions = safe_versions(project)
        by_id = {v.id: v for v in versions}
        bases = [by_id[best.parent]] if best.parent in by_id else []
        if versions and versions[0].id != best.id and versions[0].id not in [b.id for b in bases]:
            bases.append(versions[0])
        sections = []
        for base in bases:
            cmp = comparison_between(project, base, best)
            if cmp is None:
                continue
            try:
                diff = project.versions.diff(base.id, best.id)
            except (OSError, KeyError):
                diff = ""
            sections.append(render_comparison_md(cmp, base, best, diff))
        if sections:
            write(
                "version_compare",
                "compare_report",
                "version_compare.md",
                "\n---\n\n".join(sections),
                f"版本比较报告 {best_id}",
                best_id,
            )
        else:
            skipped["version_compare"] = "best version has no earlier version with a comparable evaluation"
    else:
        skipped["version_compare"] = "no best version yet"

    # 指标摘要
    summary = metrics_summary(project)
    write(
        "metrics_json",
        "metrics",
        "metrics_summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2, default=str) + "\n",
        "指标摘要 (JSON)",
        best_id,
    )
    write("metrics_md", "metrics", "metrics_summary.md", render_metrics_md(project, summary), "指标摘要", best_id)
    write("report_html", "report", "report.html", render_html_report(project), "Skill 进化报告", best_id)

    if skipped:
        manifest["skipped"] = json.dumps(skipped, ensure_ascii=False)
        if ctx is not None:
            ctx.log("some outputs were skipped", level="warning", skipped=skipped)
    return manifest
