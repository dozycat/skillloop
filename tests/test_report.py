import json
import time

import pytest

from skillloop.core.jobs import NullContext
from skillloop.core.models import Case
from skillloop.core.project import Project
from skillloop.core.skill import Skill
from skillloop.eval.runner import Evaluator
from skillloop.evolve.compare import compare_reports
from skillloop.evolve.errorbook import ErrorBook
from skillloop.report import (
    comparison_between,
    metrics_summary,
    render_comparison_md,
    render_errorbook_md,
    render_html_report,
    render_metrics_md,
    trend_chart,
    write_outputs,
)

XSS = "<script>alert('x')</script>"


def _skill(rules):
    body = "# Demo\n\n## Rules\n\n" + "".join(f"- [{r}] do {r}\n" for r in rules)
    return Skill("demo", "Use for the demo task", body)


@pytest.fixture()
def project(tmp_path):
    cases = [
        Case(
            id=f"c{i}",
            input=f"question {i} {XSS if i == 1 else ''}",
            expected=f"a{i}",
            split="val",
            tags=[f"t{i % 2}"],
            meta={"requires": [f"r{i % 3}"], "hints": {f"r{i % 3}": f"do r{i % 3}"}},
        )
        for i in range(6)
    ]
    p = Project.init(tmp_path / "proj", "demo", requirement="# Demo", seed_skill=_skill(["r0"]), cases=cases)
    ev = Evaluator.from_project(p)
    book = ErrorBook(p.errorbook_path)

    v1 = p.versions.create(_skill(["r0"]), origin="seed", summary="seed skill")
    r1 = ev.evaluate(p.versions.load_skill(v1.id), cases, version=v1.id, split="val")
    p.save_eval(r1)
    p.versions.record_score(v1.id, "val", r1.summary())
    p.versions.set_best(v1.id)
    book.record_eval(r1, cases, iteration=0)
    book.annotate(
        "c1",
        category="missing_rule",
        root_cause="no guidance for r1 | pipe",
        lesson="always do r1",
        suggested_fix="add rule r1",
    )

    v2 = p.versions.create(_skill(["r0", "r1"]), parent=v1.id, origin="evolve", summary="add rule r1", iteration=1)
    r2 = ev.evaluate(p.versions.load_skill(v2.id), cases, version=v2.id, split="val")
    p.save_eval(r2)
    p.versions.record_score(v2.id, "val", r2.summary())
    cmp = compare_reports(r1, r2)
    v2m = p.versions.get(v2.id)
    v2m.gate = {
        "promoted": True,
        "reasons": ["val improved"],
        "decided_at": time.time(),
        "checks": [{"name": "min_delta", "passed": True, "detail": "+0.333 >= 0.01"}],
        "comparison": cmp.to_dict(),
    }
    p.versions.update(v2m)
    p.versions.set_best(v2.id)
    transitions = book.record_eval(r2, cases, iteration=1)

    p.state.path("history.json").write_text(
        json.dumps(
            [
                {"iteration": 0, "parent": "", "best_version": v1.id, "best_score": r1.mean_score, "train_score": 0.3},
                {
                    "iteration": 1,
                    "parent": v1.id,
                    "best_version": v2.id,
                    "best_score": r2.mean_score,
                    "train_score": 0.6,
                    "candidates": [
                        {
                            "version": v2.id,
                            "summary": "add rule r1",
                            "val_score": r2.mean_score,
                            "delta": cmp.delta,
                            "promoted": True,
                            "reasons": [],
                        },
                        {"version": "v003", "promoted": False, "reasons": ["no improvement"]},
                    ],
                    "errorbook": transitions,
                    "ts": time.time(),
                },
                {"iteration": 2},  # partial record must be tolerated
            ]
        ),
        encoding="utf-8",
    )
    yield p
    p.close()


def test_metrics_summary(project):
    m = metrics_summary(project)
    assert m["best_version"] == "v002" and m["first_version"] == "v001"
    assert m["best_scores"]["val"]["mean_score"] == pytest.approx(0.6667, abs=1e-3)
    assert m["improvement"]["val"] == pytest.approx(0.3333, abs=1e-3)
    assert (m["promotions"], m["rejections"]) == (1, 1)
    assert m["n_versions"] == 2 and m["n_iterations"] == 3
    assert m["totals"]["n_evals"] == 2 and m["totals"]["n_runs"] == 12
    assert m["errorbook"]["fixed"] == 2 and m["errorbook"]["open"] == 2
    assert [t["best_score"] for t in m["trend"]][2] is None
    json.dumps(m)  # must be JSON-able
    md = render_metrics_md(project)
    assert "v002" in md and "0.667" in md and "add rule r1" in md


def test_comparison_md(project):
    v1, v2 = project.versions.get("v001"), project.versions.get("v002")
    cmp = comparison_between(project, v1, v2)
    assert cmp is not None and cmp.n_common == 6 and sorted(cmp.fixed) == ["c1", "c4"]
    md = render_comparison_md(cmp, v1, v2, project.versions.diff("v001", "v002"))
    assert "v001 → v002" in md and "+0.333" in md and "`c1`" in md and "`c4`" in md
    assert "min_delta" in md and "+- [r1] do r1" in md and "p 值" in md
    assert "t0" in md and "t1" in md
    # plain ids and an empty comparison also render
    assert "没有可比较" in render_comparison_md(type(cmp)(), "a", "b")


def test_errorbook_md(project):
    md = render_errorbook_md(ErrorBook(project.errorbook_path))
    assert "missing_rule" in md and "always do r1" in md and "add rule r1" in md
    assert "skillloop eval --version" in md and "已修复" in md and "### c2" in md
    assert XSS in md  # markdown keeps the raw input, inside a fenced block
    assert "错题库" in render_errorbook_md(ErrorBook(project.root / "nope.jsonl"))


def test_html_report_escapes_and_charts(project):
    # c1 was fixed; make an open entry carry the hostile input too
    book = ErrorBook(project.errorbook_path)
    book.get("c2").input = XSS
    book.annotate("c2", root_cause=XSS)
    page = render_html_report(project)
    assert "<script" not in page and "&lt;script&gt;" in page
    assert "<svg" in page and "<polyline" in page and "prefers-color-scheme:dark" in page
    assert "http://" not in page and "https://" not in page  # no external assets
    assert "v002" in page and "版本谱系" in page and "min_delta" in page
    assert "暂无迭代数据" in trend_chart([]) and "<svg" in trend_chart([{"iteration": 0, "best_score": 0.5}])


def test_write_outputs(project):
    ctx = NullContext(project.store)
    out = write_outputs(project, ctx)
    for name in (
        "best_skill",
        "errorbook_md",
        "errorbook_jsonl",
        "version_compare",
        "metrics_json",
        "metrics_md",
        "report_html",
    ):
        assert (project.root / out[name]).exists(), name
    assert (project.root / out["best_skill"] / "SKILL.md").read_text(encoding="utf-8").count("[r1]") == 1
    assert json.loads((project.root / out["metrics_json"]).read_text(encoding="utf-8"))["best_version"] == "v002"
    assert len((project.root / out["errorbook_jsonl"]).read_text(encoding="utf-8").splitlines()) == 4
    # review sheet is fork B's module: present -> file exists, absent -> explained
    if "review_sheet" in out:
        assert (project.root / out["review_sheet"]).exists()
    else:
        assert "review_sheet" in json.loads(out["skipped"])
    kinds = {a["kind"] for a in project.store.artifacts()}
    assert {"best_skill", "errorbook", "compare_report", "metrics", "report"} <= kinds
    assert write_outputs(project)["report_html"] == "outputs/report.html"  # idempotent, ctx optional


def test_empty_project(tmp_path):
    p = Project.init(tmp_path / "empty", "empty")
    out = write_outputs(p)
    skipped = json.loads(out["skipped"])
    assert {"best_skill", "review_sheet", "version_compare"} <= set(skipped)
    for name in ("errorbook_md", "metrics_json", "metrics_md", "report_html"):
        assert (p.root / out[name]).exists()
    assert metrics_summary(p)["n_versions"] == 0
    assert "尚无 Skill 版本" in render_metrics_md(p)
    p.close()
