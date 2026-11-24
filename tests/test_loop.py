import threading

import pytest

from skillloop.core.jobs import JobCancelled, NullContext
from skillloop.core.models import Case
from skillloop.core.project import Project
from skillloop.core.skill import Skill
from skillloop.evolve.errorbook import ErrorBook
from skillloop.evolve.loop import EvolutionLoop, load_history
from skillloop.llm.mock import MockLLM


def test_loop_improves_and_reports(demo_project):
    logs = []
    ctx = NullContext(demo_project.store, echo=lambda r: logs.append(r))
    res = EvolutionLoop(demo_project, ctx=ctx).run()
    vs = demo_project.versions
    assert res.best_version == vs.best_id() != res.baseline_version
    assert res.best_scores["val"]["mean_score"] == 1.0 > res.baseline_scores["val"]["mean_score"]
    assert res.best_scores["test"]["mean_score"] > res.baseline_scores["test"]["mean_score"]  # held-out generalisation
    assert "target score" in res.stop_reason and res.promotions[-1] == res.best_version
    # the harmful "full" candidate (mock deletes a rule every 3rd analyst call) was stopped by the gate
    rejected = [
        c for rec in load_history(demo_project) for c in rec["candidates"] if not c["promoted"] and c.get("regressed")
    ]
    assert rejected and any("regress" in r for c in rejected for r in c["reasons"])
    book = ErrorBook(demo_project.errorbook_path)
    assert len(book) > 0 and not book.open_entries() and book.stats()["fixed"] == len(book)
    assert all(e.category == "missing_rule" for e in book.entries())
    for name in (
        "best/SKILL.md",
        "errorbook.md",
        "version_compare.md",
        "metrics_summary.json",
        "report.html",
        "review_sheet.csv",
    ):
        assert (demo_project.outputs_dir / name).exists(), name
    assert any(r["type"] == "audit" and r["message"] == "version.promote" for r in logs)
    assert {a["kind"] for a in demo_project.store.artifacts()} >= {"skill", "eval", "report"}


def test_loop_is_resumable_without_rerunning_evals(demo_project):
    EvolutionLoop(demo_project).run(max_iterations=2)
    n_versions, n_evals = len(demo_project.versions.ids()), len(demo_project.list_evals())
    best = demo_project.versions.best_id()
    again = EvolutionLoop(demo_project).run(max_iterations=0)  # nothing new to do: all evals are reused
    assert again.best_version == best and len(demo_project.versions.ids()) == n_versions
    assert len(demo_project.list_evals()) == n_evals
    res = EvolutionLoop(demo_project).run()
    assert res.best_scores["val"]["mean_score"] == 1.0
    its = [r["iteration"] for r in load_history(demo_project)]
    assert its == sorted(set(its))  # iteration numbering continues, no duplicates


def test_human_approval_blocks_auto_promotion(demo_project):
    demo_project.config.evolve.require_human_approval = True
    res = EvolutionLoop(demo_project).run(max_iterations=1)
    assert res.best_version == res.baseline_version and res.pending_approval
    assert demo_project.versions.get(res.pending_approval[0]).status == "pending_approval"


def test_bootstrap_without_seed(tmp_path):
    cases = [
        Case(id=f"c{i}", input=f"q{i}", expected=f"a{i}", meta={"requires": ["r1"], "hints": {"r1": "Do r1."}})
        for i in range(8)
    ]
    p = Project.init(tmp_path / "p", "boot-proj", requirement="# Handle things\n- Do r1.", cases=cases)
    res = EvolutionLoop(p).run()
    first = p.versions.get(res.baseline_version)
    assert first.origin == "bootstrap" and p.versions.load_skill(first.id).name == "boot-proj"
    assert res.best_scores["val"]["mean_score"] == 1.0
    p.close()


def test_leaky_candidate_is_blocked(demo_project):
    case = next(c for c in demo_project.load_cases())
    gold = "the definitive gold answer string"
    cases = demo_project.load_cases()
    for c in cases:
        c.expected = f"{gold} {c.id}"
    demo_project.save_cases(cases)

    def leaky_analyst(llm, prompt, system):
        return {
            "diagnoses": [],
            "patch": {
                "summary": "paste answers",
                "edits": [{"op": "append", "section": "## Rules", "content": f"- Answer with: {gold} {cases[0].id}"}],
            },
        }

    llm = MockLLM(handlers={"analyst_error": leaky_analyst})
    res = EvolutionLoop(demo_project, llm=llm).run(max_iterations=1)
    assert res.best_version == res.baseline_version
    cands = load_history(demo_project)[0]["candidates"]
    assert cands and any("leak" in r for r in cands[0]["reasons"])
    assert demo_project.versions.get(cands[0]["version"]).status == "rejected"
    assert case is not None


def test_cancel_stops_the_loop(demo_project):
    demo_project.config.agent.options = {"delay_s": 0.2}
    ctx = NullContext(demo_project.store)
    threading.Timer(0.3, ctx.cancel_event.set).start()
    with pytest.raises(JobCancelled):
        EvolutionLoop(demo_project, ctx=ctx).run()


def test_empty_dataset_is_a_clear_error(tmp_path):
    p = Project.init(tmp_path / "p", requirement="x", seed_skill=Skill("s", "d", "# body body body body body"))
    with pytest.raises(ValueError, match="dataset is empty"):
        EvolutionLoop(p).run()
    p.close()
