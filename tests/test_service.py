import json
import time

import pytest

from skillloop import SkillLoop, Workspace
from skillloop.cli import main
from skillloop.core.sandbox import SandboxError
from skillloop.demo import REQUIREMENT, SEED_SKILL, demo_cases, demo_config
from skillloop.mcp.server import build_server


@pytest.fixture
def es(demo_project):
    s = SkillLoop(demo_project.root)
    yield s
    s.close()


def test_job_lifecycle_and_read_api(es):
    job_id = es.submit("evolve", max_iterations=2)
    with pytest.raises(RuntimeError):
        es.submit("evolve")  # one evolve job per project at a time
    job = es.jobs.wait(job_id, 60)
    assert job["status"] == "succeeded" and job["result"]["iterations"] == 2
    events = es.events(job_id)
    assert {e["type"] for e in events} >= {"job", "log", "progress", "audit"}
    info = es.info()
    assert info["best_version"] and info["splits"] == {"train": 8, "val": 8, "test": 8} and not info["active_jobs"]
    v = es.version()
    assert v["status"] == "best" and v["skill_md"].startswith("---") and len(v["lineage"]) >= 2
    assert es.diff(v["lineage"][0], "best")
    ev = es.eval(es.evals()[0]["eval_id"], with_trajectories=False)
    assert ev["summary"]["n_cases"] and "steps" not in ev["results"][0]["trajectory"]
    assert es.metrics() and es.history() and es.git_log() and es.artifacts(kind="skill")
    assert es.health()["agent"]["ok"]


def test_cancel_job(demo_project):
    cfg = demo_project.config
    cfg.agent.options = {"delay_s": 0.3}
    demo_project.save_config()
    es = SkillLoop(demo_project.root)
    job_id = es.submit("evaluate", split="train")
    time.sleep(0.4)
    assert es.cancel(job_id)
    assert es.jobs.wait(job_id, 30)["status"] == "cancelled"
    assert not es.cancel(job_id)
    es.close()


def test_manual_version_flow_and_reviews(es):
    es.bootstrap()
    v = es.import_skill(SEED_SKILL.to_markdown() + "\n- [tone] 先确认问题再给方案。\n", "hand edit")
    assert v.parent == "v001" and v.origin == "manual"
    with pytest.raises(ValueError):
        es.import_skill("no frontmatter, too short")
    cmp = es.compare("v001", v.id, "val")
    assert cmp.fixed and not cmp.regressed
    assert es.promote(v.id, "looks good").status == "best"
    with pytest.raises(ValueError):
        es.reject(v.id)
    assert es.reject("v001", "superseded").status == "rejected"
    # a human verdict overrides the automatic judgment everywhere that evaluation is used
    report = es.evaluate("best", "val")
    failing = report.failures()[0].case_id
    assert es.submit_review(report.id, failing, passed=True, comment="actually fine", reviewer="qa")
    after = es.eval(report.id)
    res = next(r for r in after["results"] if r["case_id"] == failing)
    assert res["judgment"]["passed"] and res["judgment"]["source"] == "human"
    assert after["summary"]["pass_rate"] > report.pass_rate
    sheet = es.export_review_sheet(report.id)
    assert sheet.exists() and failing in sheet.read_text(encoding="utf-8-sig")


def test_config_update_is_validated_and_sandboxed(es):
    assert es.update_config({"evolve": {"max_iterations": 3}})["evolve"]["max_iterations"] == 3
    assert SkillLoop(es.project.root).config()["evolve"]["max_iterations"] == 3
    with pytest.raises(ValueError):
        es.update_config({"evolve": {"metric": "vibes"}})
    with pytest.raises(SandboxError):
        es.update_config({"project": {"dataset": "../../outside.jsonl"}})
    with pytest.raises(SandboxError):
        es.read_artifact("../../../etc/passwd")
    assert es.add_cases([{"question": "新问题", "answer": "x", "id": "ticket-01"}]) == 1
    assert len({c["id"] for c in es.cases()}) == 25


def test_workspace_isolation(tmp_path):
    ws = Workspace(tmp_path)
    cases = [c.to_dict() for c in demo_cases()]
    a = ws.create(
        "alpha", requirement=REQUIREMENT, skill_md=SEED_SKILL.to_markdown(), cases=cases, config=demo_config().to_dict()
    )
    ws.create("beta", requirement="other")
    assert ws.names() == ["alpha", "beta"]
    with pytest.raises(ValueError):
        ws.create("alpha")
    for bad in ("../x", "a/b", ""):
        with pytest.raises(KeyError):
            ws.create(bad)
    with pytest.raises(KeyError):
        ws.get()  # ambiguous
    a.evolve(max_iterations=1)
    assert ws.get("alpha").info()["n_versions"] >= 2 and ws.get("beta").info()["n_versions"] == 0
    assert not (tmp_path / "beta" / ".skillloop" / "versions" / "v001").exists()
    ws.close()
    single = Workspace(tmp_path / "alpha")
    assert single.names() == ["alpha"] and single.get().info()["n_versions"] >= 2
    single.close()


def _call(server, tool, /, **arguments):
    resp = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
    )
    result = resp["result"]
    text = result["content"][0]["text"]
    if result.get("isError"):
        return {"__error__": text}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def test_mcp_server_drives_the_whole_loop(tmp_path):
    ws = Workspace(tmp_path)
    server = build_server(ws)
    init = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 0,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "0"},
            },
        }
    )
    assert init["result"]["serverInfo"]["name"] == "skillloop"
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    assert len(tools) >= 25 and all(t["inputSchema"]["type"] == "object" and t["description"] for t in tools)
    created = _call(
        server,
        "create_project",
        name="demo",
        requirement=REQUIREMENT,
        skill_md=SEED_SKILL.to_markdown(),
        cases=[c.to_dict() for c in demo_cases()],
        config=demo_config().to_dict(),
    )
    assert created["n_cases"] == 24
    assert _call(server, "project_status")["health"]["agent"]["ok"]
    job_id = _call(server, "evolve", max_iterations=3)["job_id"]
    seq = 0
    for _ in range(200):
        st = _call(server, "job_status", job_id=job_id, after_seq=seq)
        seq = st["last_seq"]
        if st["status"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(0.05)
    assert st["status"] == "succeeded", st
    versions = _call(server, "list_versions")
    best = next(v for v in versions if v["status"] == "best")
    assert best["scores"]["val"]["mean_score"] > versions[0]["scores"]["val"]["mean_score"]
    assert "[order-lookup]" in _call(server, "get_skill")["skill_md"]
    assert "+" in _call(server, "diff_versions", base="v001", cand="best")
    assert _call(server, "compare_versions", base="v001", cand="best")["fixed"]
    assert _call(server, "get_errorbook")["stats"]["total"] > 0
    assert _call(server, "get_metrics") and _call(server, "get_history") and _call(server, "list_evals")
    assert "report_html" in _call(server, "write_outputs")
    assert "# " in _call(server, "read_artifact", path="outputs/metrics_summary.md")
    # errors come back as tool errors the calling model can act on, never as crashes
    assert "__error__" in _call(server, "read_artifact", path="../../etc/passwd")
    assert "__error__" in _call(server, "get_skill", version="v999")
    assert "__error__" in _call(server, "job_status", job_id="nope")
    assert "__error__" in _call(server, "project_status", project="ghost")
    ws.close()


def test_cli_end_to_end(tmp_path, capsys):
    d = str(tmp_path / "demo")
    main(["init", d, "--demo"])
    main(["doctor", "-p", d])
    main(["evolve", "-p", d, "-n", "2", "-q"])
    out = capsys.readouterr().out
    assert "Best version: v" in out and "report_html" in out
    main(["versions", "-p", d, "--json"])
    versions = json.loads(capsys.readouterr().out)
    assert sum(v["status"] == "best" for v in versions) == 1
    for cmd in (
        ["status"],
        ["show"],
        ["errors"],
        ["jobs"],
        ["logs", "--audit"],
        ["diff", "v001", "best"],
        ["compare", "v001", "best"],
        ["eval", "--split", "test", "-q"],
        ["outputs"],
        ["review", "list"],
    ):
        main([*cmd, "-p", d])
    assert capsys.readouterr().out.strip()
    main(["export", str(tmp_path / "exported"), "-p", d])
    assert (tmp_path / "exported" / "SKILL.md").exists()
    with pytest.raises(SystemExit) as e:
        main(["show", "v999", "-p", d])
    assert "error" in str(e.value)
    with pytest.raises(SystemExit):
        main(["status", "-p", str(tmp_path / "nowhere")])


def test_config_options_replace_and_import_never_reads_paths(es, tmp_path):
    es.update_config({"agent": {"options": {"delay_s": 0.1, "seed": 3}}, "mcp_profiles": {"a": {"tools": []}}})
    cfg = es.update_config({"agent": {"options": {"seed": 4}}, "mcp_profiles": {}})
    assert cfg["agent"]["options"] == {"seed": 4} and cfg["mcp_profiles"] == {} and cfg["agent"]["adapter"] == "mock"
    secret = tmp_path / "secret" / "SKILL.md"
    secret.parent.mkdir()
    secret.write_text(SEED_SKILL.to_markdown())
    with pytest.raises(ValueError):
        es.import_skill(str(secret.parent))  # a string is SKILL.md text, never a path to load
    assert es.import_skill(secret.parent).origin == "manual"  # an explicit Path is
    es.write_outputs()
    assert es.read_artifact("outputs/best").startswith("---")


def test_workspace_key_is_directory_name_even_when_config_name_differs(demo_project):
    ws = Workspace(demo_project.root)  # dir is "demo", configured name is "support-ticket-demo"
    listed = ws.list()
    assert [p["name"] for p in listed] == ["demo"] and listed[0]["display_name"] == "support-ticket-demo"
    assert ws.get(listed[0]["name"]).info()["n_cases"] == 24
    ws.close()


def test_human_review_updates_stored_version_score(es):
    report = es.evaluate("best", "val")
    vid = report.skill_version
    before = es.project.versions.get(vid).scores["val"]["pass_rate"]
    es.submit_review(report.id, report.failures()[0].case_id, passed=True)
    assert es.project.versions.get(vid).scores["val"]["pass_rate"] > before


def test_error_messages_keep_their_quotes(es, capsys):
    server = build_server(Workspace(es.project.root))
    es.bootstrap()
    assert _call(server, "get_skill", version="v999")["__error__"].endswith("'v999'")
    with pytest.raises(SystemExit) as e:
        main(["show", "v999", "-p", str(es.project.root)])
    assert str(e.value).endswith("'v999'")


def test_mcp_job_status_seq_advances_past_progress_events(es):
    server = build_server(Workspace(es.project.root))
    job_id = _call(server, "evaluate", split="train")["job_id"]
    es2 = server  # same workspace service runs the job
    for _ in range(200):
        st = _call(es2, "job_status", job_id=job_id)
        if st["status"] == "succeeded":
            break
        time.sleep(0.05)
    assert st["status"] == "succeeded"
    assert all(e["type"] != "progress" for e in st["events"])
    again = _call(server, "job_status", job_id=job_id, after_seq=st["last_seq"])
    assert again["events"] == [] and again["last_seq"] == st["last_seq"]


def test_cli_json_output_is_clean_and_review_paths_are_cwd_relative(tmp_path, capsys, monkeypatch):
    d = tmp_path / "demo"
    main(["init", str(d), "--demo"])
    capsys.readouterr()
    main(["bootstrap", "-p", str(d), "--json"])
    assert json.loads(capsys.readouterr().out)["id"] == "v001"  # logs went to stderr
    main(["eval", "-p", str(d), "--split", "val", "-q"])
    monkeypatch.chdir(d)
    main(["review", "export", "sheet.csv"])
    assert (d / "sheet.csv").exists()


def test_cli_reset_keeps_inputs_and_optionally_the_cache(tmp_path, capsys):
    d = tmp_path / "demo"
    main(["init", str(d), "--demo"])
    main(["evolve", "-p", str(d), "-n", "1", "-q"])
    assert (d / ".skillloop" / "cache").exists() and (d / "outputs").exists()
    with pytest.raises(SystemExit, match="--yes"):  # never deletes without confirmation
        main(["reset", "-p", str(d)])
    assert (d / ".skillloop" / "versions").exists()
    main(["reset", "-p", str(d), "--yes", "--keep-cache"])
    assert not (d / ".skillloop" / "versions").exists() and not (d / "outputs").exists()
    assert any((d / ".skillloop" / "cache").rglob("*.json"))
    assert (d / "dataset.jsonl").exists() and (d / "seed_skill" / "SKILL.md").exists()
    main(["reset", "-p", str(d), "-y"])
    assert not (d / ".skillloop").exists()
    main(["evolve", "-p", str(d), "-n", "1", "-q"])  # the project still works afterwards
    assert "Best version" in capsys.readouterr().out
