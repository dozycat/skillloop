"""Task framing, shared reference directories, dataset column mapping, answer extraction, retries, budget."""

import pytest

from skillloop.adapters.callable import FunctionAdapter
from skillloop.adapters.claude_code import ClaudeCodeAdapter
from skillloop.adapters.mock import MockAdapter
from skillloop.core.config import Config
from skillloop.core.dataset import load_cases, save_cases
from skillloop.core.models import Case, Trajectory
from skillloop.core.project import Project
from skillloop.core.skill import Skill
from skillloop.eval.answer import extract_numbers, match_answer
from skillloop.eval.runner import Evaluator
from skillloop.evolve.loop import EvolutionLoop
from skillloop.service import SkillLoop

SKILL = Skill("s", "d", "# body body body body body")

CSV = 'uid,question,answer,difficulty,source_files\nU1,How much in 1940?,"2,602",hard,a.txt\nU2,Which country?,Zaire,easy,b.txt\n'


def _project(tmp_path, **agent):
    root = tmp_path / "p"
    (root / "corpus").mkdir(parents=True)
    (root / "corpus" / "a.txt").write_text("National defense 1940 ... 2,602\n")
    (root / "data.csv").write_text(CSV)
    (root / "task.md").write_text("You are an analyst.\nEnd with: FINAL ANSWER: <answer>")
    cfg = Config()
    cfg.project.dataset = "data.csv"
    cfg.dataset.id_column, cfg.dataset.input_column, cfg.dataset.expected_column = "uid", "question", "answer"
    cfg.dataset.tag_columns = ["difficulty"]
    cfg.agent.task_file, cfg.agent.data_dirs = "task.md", ["corpus"]
    cfg.eval.judge, cfg.eval.answer_regex = "answer", r"FINAL ANSWER:\s*(.+)"
    for k, v in agent.items():
        setattr(cfg.eval, k, v)
    return Project.init(root, "p", config=cfg, requirement="req", seed_skill=SKILL)


def test_dataset_column_mapping(tmp_path):
    p = _project(tmp_path)
    cases = p.load_cases()
    assert [(c.id, c.input, c.expected, c.tags) for c in cases] == [
        ("U1", "How much in 1940?", "2,602", ["hard"]),
        ("U2", "Which country?", "Zaire", ["easy"]),
    ]
    assert cases[0].meta == {"difficulty": "hard", "source_files": "a.txt"}  # unmapped columns are kept
    p.config.dataset.input_column = "nope"
    with pytest.raises(ValueError, match="no column 'nope'"):
        p.load_cases()
    with pytest.raises(ValueError, match="only be written to a .jsonl"):
        save_cases(cases, tmp_path / "x.csv")
    assert len(load_cases(tmp_path / "p" / "data.csv")) == 2  # aliases still work without a mapping
    p.close()


def test_agent_sees_task_corpus_and_bare_case_is_stored(tmp_path):
    p = _project(tmp_path)
    seen = {}

    def agent(req):
        seen["prompt"] = req.case.input
        seen["corpus_text"] = (req.workdir / "corpus" / "a.txt").read_text()
        seen["data_dirs"] = req.data_dirs
        (req.workdir / "notes.txt").write_text("scratch")
        return "I searched the file.\nFINAL ANSWER: 2602 million"

    ev = Evaluator.from_project(p, adapter=FunctionAdapter(agent))
    report = ev.evaluate(SKILL, p.load_cases()[:1])
    assert seen["prompt"].startswith("You are an analyst.") and seen["prompt"].endswith("How much in 1940?")
    assert "./corpus/" in seen["prompt"] and "2,602" in seen["corpus_text"]
    assert seen["data_dirs"] == [(tmp_path / "p" / "corpus").resolve()]
    res = report.results[0]
    assert res.judgment.passed and res.trajectory.output == "2602 million"
    assert res.trajectory.full_output.startswith("I searched")
    assert (tmp_path / "p" / "corpus" / "a.txt").exists()  # cleaning the workdir never touches the corpus
    assert ev.compose_input(Case(id="x", input="Q?")).count("Q?") == 1
    ev.task_prompt = "Question: {input}\nAnswer briefly."
    assert ev.compose_input(Case(id="x", input="Q?")).startswith("Question: Q?\nAnswer briefly.")
    p.close()


def test_missing_final_answer_is_labelled(tmp_path):
    p = _project(tmp_path)
    ev = Evaluator.from_project(p, adapter=FunctionAdapter(lambda req: "it was 2,602 in 1940"))
    res = ev.evaluate(SKILL, p.load_cases()[:1]).results[0]
    assert res.judgment.passed and "no_final_answer" in res.judgment.labels  # still judged on the whole reply
    p.close()


def test_retries_rerun_failed_attempts_in_a_clean_workdir(tmp_path):
    p = _project(tmp_path, max_retries=2)
    calls = []

    def flaky(req):
        calls.append(sorted(f.name for f in req.workdir.iterdir()))
        (req.workdir / "leftover.txt").write_text("x")
        if len(calls) < 3:
            return Trajectory(status="timeout" if len(calls) == 1 else "error", error="boom")
        return "FINAL ANSWER: 2,602"

    res = Evaluator.from_project(p, adapter=FunctionAdapter(flaky)).evaluate(SKILL, p.load_cases()[:1]).results[0]
    assert res.judgment.passed and res.trajectory.attempts == 3
    assert calls == [["corpus"], ["corpus"], ["corpus"]]
    p.config.eval.max_retries = 0
    calls.clear()
    res = Evaluator.from_project(p, adapter=FunctionAdapter(flaky)).evaluate(SKILL, p.load_cases()[:1]).results[0]
    assert res.trajectory.status == "timeout" and res.trajectory.attempts == 1
    p.close()


def test_doctor_reports_missing_task_inputs(tmp_path):
    p = _project(tmp_path)
    p.config.agent.data_dirs = ["missing_dir"]
    p.save_config()
    es = SkillLoop(p.root)
    assert any("missing_dir" in m for m in es.health()["input_problems"])
    with pytest.raises(Exception, match="escapes sandbox"):
        es.update_config({"agent": {"data_dirs": ["../.."]}}) and es.project.data_dirs()
    es.close()
    p.close()


def test_claude_code_allows_the_real_data_dirs(tmp_path):
    from skillloop.adapters.base import AgentRequest

    req = AgentRequest(case=Case(id="c", input="q"), skill=SKILL, workdir=tmp_path, data_dirs=[tmp_path / "corpus"])
    cmd = ClaudeCodeAdapter().build_command(req, {"mcp_config_file": ""})
    assert cmd[cmd.index("--add-dir") + 1] == str(tmp_path / "corpus")


@pytest.mark.parametrize(
    "expected,reply,tol,ok",
    [
        ("2,602", "expenditures were $2,602 million (p. 15)", 0, True),
        ("2,602", "2,600", 0, False),
        ("2,602", "2,600", 0.01, True),
        ("36080 million", "36,080", 0, True),
        ("36080 million", "36.08 billion", 0, True),
        ("36080 million", "36080 billion", 0, False),
        ("6.1596", "about 6.16", 0, False),
        ("3", "In 1997 the claims were 3 million", 0, True),
        ("3", "1997", 0, False),
        ("March 1977", "April 1977", 0, False),
        ("1977", "the year 1977", 0, True),
        ("Zaire", "Zambia", 0, False),
        ("12%", "0.12", 0, True),
        ("10 and 20", "10 only", 0, False),
        ("1.5万", "15000", 0, True),
    ],
)
def test_answer_matching(expected, reply, tol, ok):
    assert match_answer(expected, reply, tol)[0] is ok


def test_answer_judge_partial_credit_and_alternatives(tmp_path):
    from skillloop.eval.base import build_judge

    judge = build_judge("answer", {"tolerance": 0.0, "tolerances": [0.01, 0.05]})
    near = judge.judge(Case(id="c", input="q", expected="100"), Trajectory(output="100.5"))
    assert not near.passed and near.score == pytest.approx(2 / 3)
    assert judge.judge(Case(id="c", input="q", expected=["Zaire", "DRC"]), Trajectory(output="the DRC")).passed
    assert judge.judge(Case(id="c", input="q"), Trajectory(output="x")).needs_review
    assert [n.scaled for n in extract_numbers("$1.5 billion and (2,000)")] == [1.5e9, 2000.0]


class PricedMock(MockAdapter):
    def run(self, request):
        traj = super().run(request)
        traj.usage["cost_usd"] = 0.5
        return traj


def test_cost_limit_stops_the_loop(demo_project):
    demo_project.config.evolve.max_cost_usd = 10.0  # baseline val (8 runs = $4) + iteration 1 exceeds it
    res = EvolutionLoop(demo_project, adapter=PricedMock()).run()
    assert "cost limit" in res.stop_reason and res.iterations == 1
    assert res.best_version  # still finishes cleanly with outputs


class CountingMock(MockAdapter):
    runs = 0

    def run(self, request):
        type(self).runs += 1
        traj = super().run(request)
        traj.usage["cost_usd"] = 0.25
        return traj


def test_run_cache_reuses_finished_runs_and_respects_identity(demo_project):
    cases = demo_project.load_cases()[:4]
    skill = demo_project.load_seed_skill()
    CountingMock.runs = 0
    first = Evaluator.from_project(demo_project, adapter=CountingMock()).evaluate(skill, cases)
    again = Evaluator.from_project(demo_project, adapter=CountingMock()).evaluate(skill, cases)  # e.g. after a restart
    assert CountingMock.runs == 4
    assert [r.judgment.passed for r in again.results] == [r.judgment.passed for r in first.results]
    assert first.usage()["cost_usd"] == 1.0 and again.usage()["cost_usd"] == 0.0 and again.usage()["cached"] == 4

    changed = skill.copy()
    changed.body += "\n- [tone] 先确认问题再给方案。\n"
    Evaluator.from_project(demo_project, adapter=CountingMock()).evaluate(changed, cases)
    assert CountingMock.runs == 8  # different skill content: nothing reused
    Evaluator.from_project(demo_project, adapter=CountingMock(seed=5)).evaluate(skill, cases)
    assert CountingMock.runs == 12  # different adapter settings: nothing reused
    demo_project.config.eval.cache = False
    Evaluator.from_project(demo_project, adapter=CountingMock()).evaluate(skill, cases)
    assert CountingMock.runs == 16


def test_failed_runs_are_not_cached(tmp_path):
    p = _project(tmp_path)

    class Broken(MockAdapter):
        def run(self, request):
            return Trajectory(status="error", error="down")

    cases = p.load_cases()[:1]
    assert Evaluator.from_project(p, adapter=Broken()).evaluate(SKILL, cases).results[0].trajectory.status == "error"
    assert not any((p.state.path("cache")).rglob("*.json"))
    p.close()
