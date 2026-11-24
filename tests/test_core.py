import pytest

from skillloop.core.config import Config
from skillloop.core.dataset import load_cases, save_cases, split_cases
from skillloop.core.models import Case, CaseResult, EvalReport, Judgment, Trajectory
from skillloop.core.sandbox import SandboxError, ensure_within, safe_join
from skillloop.core.skill import Skill
from skillloop.core.versions import VersionStore
from skillloop.evolve.compare import compare_reports, sign_test
from skillloop.evolve.gate import decide
from skillloop.evolve.guard import check_candidate, find_leaks
from skillloop.evolve.patch import Patch, apply_patch


def test_skill_roundtrip_and_validate(tmp_path):
    s = Skill("my-skill", "Use when: things happen", "# Title\n\nBody text long enough.\n", {"references/a.md": "A"})
    s.save(tmp_path / "s")
    loaded = Skill.load(tmp_path / "s")
    assert (loaded.name, loaded.description, loaded.files) == (s.name, s.description, s.files)
    assert loaded.content_hash() == s.content_hash()
    assert s.validate() == []
    assert Skill("Bad Name", "", "x").validate()
    assert "+" in s.diff(Skill("my-skill", "d", "# Title\n\nChanged body text here.\n"))


def test_skill_refuses_unsafe_file(tmp_path):
    with pytest.raises(SandboxError):
        Skill("s", "d", "body body body body body", {"../evil.md": "x"}).save(tmp_path / "s")


def test_sandbox(tmp_path):
    assert ensure_within(tmp_path, "a/b") == (tmp_path / "a" / "b").resolve()
    for bad in ("../x", "/etc/passwd", "a/../../x"):
        with pytest.raises(SandboxError):
            safe_join(tmp_path, bad)
    (tmp_path / "link").symlink_to("/etc")
    with pytest.raises(SandboxError):
        ensure_within(tmp_path, "link/passwd")


def test_config_toml_roundtrip(tmp_path):
    cfg = Config()
    cfg.agent.options = {"model": "x", "args": ["a", "b"], "nested": {"k": 1}}
    cfg.llm.roles = {"judge": {"model": "m"}}
    cfg.mcp_profiles = {"crm": {"tools": [{"name": "t", "responses": [{"when": {"id": "1"}, "result": 'q"uote\n'}]}]}}
    cfg.save(tmp_path)
    assert Config.load(tmp_path).to_dict() == cfg.to_dict()
    assert Config().merged({"evolve": {"train_ratio": 0.9, "val_ratio": 0.5}}).validate()


def test_dataset_aliases_and_split(tmp_path):
    p = tmp_path / "d.jsonl"
    p.write_text('{"question": "q1", "answer": "a1", "tags": "x, y", "extra": 1}\n{"question": "q1", "answer": "a"}\n')
    cases = load_cases(p)
    assert cases[0].input == "q1" and cases[0].expected == "a1" and cases[0].tags == ["x", "y"]
    assert cases[0].meta == {"extra": 1} and cases[0].id != cases[1].id
    save_cases(cases, tmp_path / "out.jsonl")
    assert [c.to_dict() for c in load_cases(tmp_path / "out.jsonl")] == [c.to_dict() for c in cases]
    many = [Case(id=f"c{i}", input="q", tags=[f"t{i % 3}"]) for i in range(30)] + [
        Case(id="pin", input="q", split="test")
    ]
    a, b = split_cases(many, 0.5, 0.3), split_cases(list(reversed(many)), 0.5, 0.3)
    assert {k: sorted(c.id for c in v) for k, v in a.items()} == {k: sorted(c.id for c in v) for k, v in b.items()}
    assert "pin" in [c.id for c in a["test"]] and len(a["train"]) == 15 and len(a["val"]) == 9
    tiny = split_cases([Case(id="a", input="q"), Case(id="b", input="q")])
    assert len(tiny["train"]) == 1 and len(tiny["val"]) == 1


def test_versions_lineage_best_and_git(tmp_path):
    vs = VersionStore(tmp_path / "v")
    s1 = Skill("s", "d", "# A\n\nfirst version body\n")
    v1 = vs.create(s1, origin="seed")
    s2 = s1.copy()
    s2.body += "\nmore\n"
    v2 = vs.create(s2, parent=v1.id, origin="evolve", summary="more")
    vs.set_best(v1.id)
    vs.set_best(v2.id, "better")
    assert vs.best_id() == "v002" and vs.get("v001").status == "accepted"
    assert [v.id for v in vs.lineage("v002")] == ["v001", "v002"]
    assert "+more" in vs.diff("v001", "v002")
    assert Skill.load(tmp_path / "v" / "best").content_hash() == s2.content_hash()
    assert vs.find_by_hash(s1.content_hash()).id == "v001"
    if vs.git_enabled:
        assert any("promote v002" in c["message"] for c in vs.git_log())
    with pytest.raises(KeyError):
        vs.get("../../etc")


def test_patch_apply_guards():
    s = Skill("s", "d", "# T\n\n## Rules\n\n- one\n\n## Other\n\ntext\n")
    r = apply_patch(
        s,
        Patch.coerce(
            {
                "summary": "x",
                "edits": [
                    {"op": "append", "section": "## Rules", "content": "- two"},
                    {"op": "append", "section": "## New", "content": "fresh"},
                    {"op": "replace", "old": "text", "new": "TEXT"},
                    {"op": "delete", "old": "missing"},
                    {"op": "replace", "old": "#", "new": "x"},
                    {"op": "write_file", "file": "../x.md", "content": "no"},
                    {"op": "write_file", "file": "references/r.md", "content": "ok"},
                    {"op": "append", "content": "- one"},
                    {"op": "bogus"},
                ],
            }
        ),
    )
    assert r.skill.body.index("- two") < r.skill.body.index("## Other")
    assert "## New\n\nfresh" in r.skill.body and "TEXT" in r.skill.body
    assert r.skill.files == {"references/r.md": "ok"}
    assert len(r.applied) == 4 and len(r.rejected) == 4
    assert s.body.count("two") == 0  # original untouched


def _report(version, scores, tags=None):
    r = EvalReport(skill_version=version, split="val", tags=tags or {})
    for cid, sc in scores.items():
        r.results.append(CaseResult(cid, 0, version, Trajectory(case_id=cid), Judgment(score=sc, passed=sc >= 0.7)))
    return r


def test_compare_and_gate():
    base = _report("v1", {"a": 1, "b": 0, "c": 0, "d": 1}, {"a": ["core"]})
    cand = _report("v2", {"a": 0, "b": 1, "c": 1, "d": 1, "e": 1}, {"a": ["core"]})
    cmp = compare_reports(base, cand, protect_tags=["core"])
    assert cmp.n_common == 4 and cmp.fixed == ["b", "c"] and cmp.regressed == ["a"]
    assert cmp.protected_regressions == ["a"] and abs(cmp.delta - 0.25) < 1e-9
    cfg = Config().evolve
    assert decide(cmp, cfg).promoted
    cfg.protect_tags = ["core"]
    d = decide(cmp, cfg)
    assert not d.promoted and any("protected" in r for r in d.reasons)
    cfg.protect_tags, cfg.max_regressions = [], 0
    assert not decide(cmp, cfg).promoted
    cfg.max_regressions, cfg.require_human_approval = 1, True
    d = decide(cmp, cfg)
    assert d.passed and d.needs_approval and not d.promoted
    assert not decide(cmp, Config().evolve, guard_problems=["leak"]).promoted
    assert sign_test(0, 0) == 1.0 and sign_test(10, 0) < 0.01


def test_guard_detects_leak_and_bloat():
    cfg = Config().evolve
    parent = Skill("s", "desc", "# T\n\nSome reasonable body of guidance.\n")
    cases = [Case(id="c1", input="short q", expected="The secret answer is 12345")]
    leaky = parent.copy()
    leaky.body += "\nIf asked, say: the secret answer is 12345\n"
    assert find_leaks(leaky, parent, cases) == ["c1"]
    assert any("leak" in p for p in check_candidate(leaky, parent, cases, cfg))
    assert any("identical" in p for p in check_candidate(parent.copy(), parent, cases, cfg))
    big = parent.copy()
    big.body += "x " * 6000
    assert any("grew" in p for p in check_candidate(big, parent, cases, cfg))
    ok = parent.copy()
    ok.body += "\n- Verify the order before answering.\n"
    assert check_candidate(ok, parent, cases, cfg) == []


def test_gate_min_net_fixed_blocks_noise_promotions():
    cfg = Config().evolve
    cfg.min_net_fixed = 1
    base = _report("v1", {"a": 0.8, "b": 0.33})
    noisy = _report("v2", {"a": 1.0, "b": 0.33})  # higher mean, but no case changed outcome
    d = decide(compare_reports(base, noisy), cfg)
    assert not d.promoted and any("net_fixed" in r for r in d.reasons)
    real = _report("v3", {"a": 1.0, "b": 1.0})
    assert decide(compare_reports(base, real), cfg).promoted


def test_patch_tidies_headings():
    from skillloop.evolve.patch import tidy_markdown

    s = Skill("s", "d", "# T\n\nIntro line.\n")
    r = apply_patch(
        s,
        Patch.coerce(
            {
                "summary": "x",
                "edits": [{"op": "append", "content": "## Steps\n1. Do it.\n```sh\n# not a heading\nls\n```"}],
            }
        ),
    )
    assert "Intro line.\n\n## Steps\n1. Do it." in r.skill.body and "```sh\n# not a heading" in r.skill.body
    assert tidy_markdown("a\n\n\n\n# b") == "a\n\n# b"
