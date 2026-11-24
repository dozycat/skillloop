import json

import pytest

from skillloop.adapters.mock import MockAdapter
from skillloop.core.config import Config
from skillloop.core.jobs import NullContext
from skillloop.core.models import Case
from skillloop.core.project import Project
from skillloop.core.skill import Skill
from skillloop.devset import (
    DevsetPipeline,
    Rule,
    balance_cases,
    build_report,
    evolve_scenarios,
    extract_rules,
    filter_cases,
    generate_candidates,
)
from skillloop.devset.pipeline import similarity, tokens
from skillloop.eval.base import build_judge
from skillloop.eval.runner import Evaluator
from skillloop.llm.mock import MockLLM

REQ = """# 退款助手需求

背景说明,不是规则。

- [iso-dates] Always output dates in ISO-8601 format.
- [refund-window] Refunds are only allowed within 30 days of purchase.
- [polite-tone] Never blame the customer; apologise once.
- [currency] Quote all amounts in CNY with two decimals.
"""


@pytest.fixture
def llm():
    return MockLLM()


def test_stage_extract_and_generate(llm):
    rules = extract_rules(REQ, llm)
    assert [r.id for r in rules] == ["iso-dates", "refund-window", "polite-tone", "currency"]
    assert rules[0].text.startswith("Always output dates")
    assert [
        r.id for r in extract_rules("- same rule text here\n- same rule text here\n1. another one entirely", llm)
    ] == ["rule-1", "rule-3"]
    cases = generate_candidates(rules, REQ, llm, per_rule=3)
    assert len(cases) == 12 and len({c.id for c in cases}) == 12
    c = cases[0]
    assert c.tags == ["iso-dates", "easy"] and c.meta["rule"] == "iso-dates" and c.meta["origin"] == "generated"
    assert c.meta["requires"] == ["iso-dates"] and c.meta["hints"]["iso-dates"].startswith("Always") and c.expected


def test_stage_evolve(llm):
    rules = extract_rules(REQ, llm)
    cases = generate_candidates(rules, REQ, llm, per_rule=2)
    out = evolve_scenarios(cases, rules, llm, rounds=1)
    variants = out[len(cases) :]
    assert out[: len(cases)] == cases and len(variants) == len(cases)
    v = variants[0]
    assert v.meta["origin"].startswith("evolved:") and v.meta["parent"] == cases[0].id
    assert v.meta["requires"] == ["iso-dates"] and v.tags[0] == "iso-dates"
    assert len({c.id for c in out}) == len(out)
    assert len(evolve_scenarios(cases, rules, llm, rounds=2)) == 3 * len(cases)
    assert evolve_scenarios(cases, rules, llm, rounds=0) == cases


def test_similarity_handles_cjk():
    assert similarity(tokens("退款必须在三十天内"), tokens("退款必须在三十天内")) == 1.0
    assert similarity(tokens("退款必须在三十天内"), tokens("日期使用标准格式输出")) < 0.2
    assert similarity(tokens(""), tokens("x")) == 0.0


def test_stage_filter(llm):
    def mk(cid, inp, expected="some answer", rubric=""):
        return Case(id=cid, input=inp, expected=expected, rubric=rubric, tags=["r1", "easy"], meta={"rule": "r1"})

    cases = [
        mk("ok", "Please compute the refund for an order placed twelve days ago"),
        mk("dup", "please compute the refund for an order placed twelve days ago!"),
        mk("short", "hi"),
        mk("noexp", "A perfectly fine long question without any gold answer", expected=None),
        mk(
            "rubric",
            "Another perfectly distinct prompt about shipping parcels overseas",
            expected=None,
            rubric="must apologise",
        ),
        mk("leak", "Reply with the phrase refund approved today to the user", expected="refund approved today"),
        mk("exists", "An existing dataset question about invoices and taxes"),
        mk("bad", "INVALID totally different wording concerning warehouse inventory counts"),
    ]
    existing = [Case(id="old", input="an existing dataset question about invoices and taxes")]
    kept, dropped = filter_cases(cases, existing=existing, threshold=0.85)
    assert [c.id for c in kept] == ["ok", "rubric", "bad"]
    reasons = {d["case_id"]: d["reason"] for d in dropped}
    assert reasons["dup"] == "near-duplicate of ok" and reasons["exists"] == "near-duplicate of dataset:old"
    assert reasons["short"].startswith("schema") and reasons["noexp"].startswith("schema")
    assert reasons["leak"].startswith("answer leak") and all(d["stage"] == "filter" for d in dropped)
    kept, dropped = filter_cases(cases, existing=existing, llm=llm, rules=[Rule("r1", "rule one")])
    assert [c.id for c in kept] == ["ok", "rubric"]
    assert {d["case_id"]: d["reason"] for d in dropped}["bad"] == "llm audit: marked invalid"


def test_stage_balance_is_deterministic_and_balanced():
    cases = []
    for r in ("a", "b", "c"):
        for i in range(9):
            d = ("easy", "medium", "hard")[i % 3]
            cases.append(Case(id=f"{r}{i}", input="x", tags=[r, d], meta={"rule": r, "difficulty": d}))
    kept, dropped = balance_cases(cases, target_size=10, max_per_rule=5)
    assert len(kept) == 10 and len(kept) + len(dropped) == 27
    per_rule = {r: sum(1 for c in kept if c.meta["rule"] == r) for r in "abc"}
    assert max(per_rule.values()) - min(per_rule.values()) <= 1
    diffs = {d: sum(1 for c in kept if c.meta["difficulty"] == d) for d in ("easy", "medium", "hard")}
    assert diffs == {"easy": 3, "medium": 4, "hard": 3}
    again, _ = balance_cases(list(reversed(cases)), target_size=10, max_per_rule=5)
    assert [c.id for c in again] == [c.id for c in kept]
    kept, dropped = balance_cases(cases, target_size=100, max_per_rule=4)
    assert len(kept) == 12 and all("max_per_rule" in d["reason"] for d in dropped)
    assert balance_cases([], target_size=5) == ([], [])


def test_report_lists_uncovered_rules():
    rules = [Rule("a", "rule a | with pipe", "format"), Rule("b", "rule b")]
    cases = [
        Case(id="a1", input="x", tags=["a", "easy"], meta={"rule": "a", "difficulty": "easy", "origin": "generated"})
    ]
    md = build_report(
        rules,
        cases,
        [{"stage": "filter", "case_id": "z", "reason": "near-duplicate of a1"}],
        {"规则抽取 rules": 2},
        title="demo",
    )
    assert "开发集报告: demo" in md and "| a | format | 1 | 0 | 0 | 1 |" in md and "\\|" in md
    assert "- **b**: rule b" in md and "near-duplicate of …" in md and "| generated | 1 |" in md


def make_project(tmp_path, **devset):
    cfg = Config()
    for k, v in devset.items():
        setattr(cfg.devset, k, v)
    return Project.init(
        tmp_path / "proj",
        "demo",
        config=cfg,
        requirement=REQ,
        cases=[Case(id="dev_iso-dates_1", input="a hand written seed case about holidays", expected="x")],
    )


def test_pipeline_end_to_end(tmp_path, llm):
    project = make_project(tmp_path, target_size=12, max_per_rule=4)
    events = []
    result = DevsetPipeline(project, llm).run(NullContext(project.store, echo=events.append), merge=True)
    assert len(result.rules) == 4 and len(result.cases) == 12 and result.merged == 12
    stages = {e["data"].get("stage") for e in events if e["type"] == "progress"}
    assert stages == {
        "devset.extract_rules",
        "devset.generate_candidates",
        "devset.evolve_scenarios",
        "devset.filter_cases",
        "devset.balance_cases",
        "devset.report",
    }
    assert {rule: 3 for rule in ("iso-dates", "refund-window", "polite-tone", "currency")} == {
        r: sum(1 for c in result.cases if c.meta["rule"] == r)
        for r in ("iso-dates", "refund-window", "polite-tone", "currency")
    }
    for name in ("rules", "candidates", "devset", "report"):
        assert (project.root / result.paths[name]).is_file()
    assert len(json.loads((project.root / result.paths["rules"]).read_text())) == 4
    assert "规则覆盖" in result.report_md and "全部规则均有用例覆盖" in result.report_md
    assert {a["kind"] for a in project.store.artifacts()} >= {
        "devset",
        "devset_rules",
        "devset_candidates",
        "devset_report",
    }
    dataset = project.load_cases()
    assert len(dataset) == 13 and len({c.id for c in dataset}) == 13  # id collision with the seed case resolved
    assert result.stage_counts["候选生成 candidates"] == 12 and result.stage_counts["场景演化 after evolution"] == 24

    # generated cases are playable by the mock agent: fail without the rule, pass with it
    ev = Evaluator(MockAdapter(), build_judge("exact"), work_root=tmp_path / "work")
    bare = Skill("demo", "Use for demo", "# Demo\n\n## Rules\n\n- nothing yet, just be nice to users\n")
    assert ev.evaluate(bare, result.cases).pass_rate == 0.0
    full = Skill(
        "demo", "Use for demo", "# Demo\n\n## Rules\n\n" + "".join(f"- [{r.id}] {r.text}\n" for r in result.rules)
    )
    assert ev.evaluate(full, result.cases).pass_rate == 1.0

    # deterministic: a second project yields the same case ids; a re-run never re-adds what the dataset has
    other = DevsetPipeline(make_project(tmp_path / "again", target_size=12, max_per_rule=4), MockLLM()).run()
    assert [c.id for c in other.cases] == [c.id for c in result.cases] and other.merged == 0
    rerun = DevsetPipeline(project, MockLLM()).run(merge=True)
    assert not {c.input for c in rerun.cases} & {c.input for c in dataset}
    assert any(x["reason"].startswith("near-duplicate of dataset:") for x in rerun.dropped)
    merged = project.load_cases()
    assert len(merged) == 13 + rerun.merged and len({c.id for c in merged}) == len(merged)
    project.close()


def test_pipeline_requires_requirement_and_honours_cancel(tmp_path, llm):
    project = Project.init(tmp_path / "empty", "demo")
    with pytest.raises(ValueError, match="requirement"):
        DevsetPipeline(project, llm).run()
    project.close()
    project = make_project(tmp_path)
    ctx = NullContext()
    ctx.cancel_event.set()
    from skillloop.core.jobs import JobCancelled

    with pytest.raises(JobCancelled):
        DevsetPipeline(project, llm).run(ctx)
    project.close()
