"""Web console: REST API, SSE, error mapping and request-safety checks (offline, mock agent + mock LLM)."""

import http.client
import json
import threading
import time

import pytest

from skillloop.web.server import create_server

RULES = {f"r{i}": f"When situation {i} occurs, apply procedure {i}." for i in range(4)}
SEED = "---\nname: demo\ndescription: Use for the demo tasks\n---\n\n# Demo\n\n## Rules\n\n- [r0] When situation 0 occurs, apply procedure 0.\n"


def mock_cases(n=24):
    return [
        {
            "id": f"case{i:02d}",
            "input": f"question {i} <script>alert(1)</script>",
            "expected": f"answer-{i}",
            "tags": [f"r{i % 4}"],
            "meta": {"requires": [f"r{i % 4}"], "hints": RULES},
        }
        for i in range(n)
    ]


class Client:
    def __init__(self, server, token=""):
        self.port = server.server_address[1]
        self.token = token

    def raw(self, method, path, body=None, headers=None, timeout=30):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        hdrs = dict(headers or {})
        data = None
        if body is not None:
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        if self.token:
            hdrs.setdefault("Authorization", f"Bearer {self.token}")
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp.status, dict(resp.getheaders()), payload

    def call(self, method, path, body=None, headers=None):
        status, _, payload = self.raw(method, path, body, headers)
        return status, json.loads(payload) if payload else None

    def ok(self, method, path, body=None):
        status, data = self.call(method, path, body)
        assert status in (200, 201), (status, data)
        return data

    def wait_job(self, project, job_id, timeout=60):
        deadline, after, events = time.time() + timeout, 0, []
        while time.time() < deadline:
            r = self.ok("GET", f"/api/projects/{project}/jobs/{job_id}/poll?after={after}")
            events += r["events"]
            after = r["last"]
            if r["final"] and not r["events"]:
                return r["job"], events
            time.sleep(0.05)
        raise AssertionError("job did not finish")


@pytest.fixture
def server(tmp_path):
    srv = create_server(tmp_path / "ws", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()
    t.join(5)


@pytest.fixture
def client(server):
    return Client(server)


def create_project(client, name="demo", config=None, cases=None):
    return client.ok(
        "POST",
        "/api/projects",
        {
            "name": name,
            "requirement": "# Demo\n- be good",
            "skill_md": SEED,
            "cases": mock_cases() if cases is None else cases,
            "config": config or {},
        },
    )


def test_static_and_meta(client):
    status, headers, body = client.raw("GET", "/")
    assert status == 200 and b"skillloop" in body and "script-src 'self'" in headers["Content-Security-Policy"]
    for name in ("app.js", "style.css"):
        assert client.raw("GET", f"/static/{name}")[0] == 200
    assert client.raw("GET", "/static/..%2Fserver.py")[0] == 404
    assert client.raw("GET", "/static/nope.js")[0] == 404
    assert client.raw("GET", "/etc/passwd")[0] == 404
    assert client.ok("GET", "/api/meta")["job_kinds"]
    assert client.ok("GET", "/api/projects") == {"projects": [], "single": False}


def test_full_loop_through_api(client):
    info = create_project(client)
    assert info["n_cases"] == 24 and info["has_seed_skill"] and info["has_requirement"]
    assert [p["name"] for p in client.ok("GET", "/api/projects")["projects"]] == ["demo"]
    P = "/api/projects/demo"
    assert client.ok("GET", P + "/health")["agent"]["ok"]

    job_id = client.ok("POST", P + "/jobs", {"kind": "evolve", "params": {"max_iterations": 4}})["job_id"]
    # SSE: stream until the terminal "end" event
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=60)
    conn.request("GET", f"{P}/jobs/{job_id}/events")
    resp = conn.getresponse()
    assert resp.status == 200 and resp.getheader("Content-Type").startswith("text/event-stream")
    stream = resp.read().decode()
    conn.close()
    assert "event: end" in stream and "event: progress" in stream and "event: job" in stream
    ids = [int(line[4:]) for line in stream.splitlines() if line.startswith("id: ")]
    assert ids == sorted(ids) and len(ids) > 5
    # resumable: asking after the last id yields only the end marker
    conn = http.client.HTTPConnection("127.0.0.1", client.port, timeout=30)
    conn.request("GET", f"{P}/jobs/{job_id}/events", headers={"Last-Event-ID": str(ids[-1])})
    tail = conn.getresponse().read().decode()
    conn.close()
    assert "event: end" in tail and "id: " not in tail

    job, events = client.wait_job("demo", job_id)
    assert job["status"] == "succeeded", job
    assert job["result"]["best_version"] and events
    assert client.ok("GET", f"{P}/jobs/{job_id}")["status"] == "succeeded"
    assert client.ok("GET", P + "/jobs")["jobs"][0]["id"] == job_id

    versions = client.ok("GET", P + "/versions")
    assert len(versions["versions"]) >= 2 and versions["best"] == job["result"]["best_version"]
    best = client.ok("GET", f"{P}/versions/{versions['best']}")
    assert "## Rules" in best["skill_md"] and best["lineage"][0] == "v001"
    assert client.ok("GET", f"{P}/versions/best")["id"] == versions["best"]
    diff = client.ok("GET", f"{P}/diff?a=v001&b={versions['best']}")["diff"]
    assert "+- [r" in diff
    cmp = client.ok("POST", P + "/compare", {"base": "v001", "cand": versions["best"], "split": "val"})
    assert cmp["delta"] > 0 and cmp["fixed"] and cmp["verdict"] == "better"

    evals = client.ok("GET", P + "/evals")["evals"]
    assert evals
    detail = client.ok("GET", f"{P}/evals/{evals[0]['eval_id']}")
    assert detail["results"] and "steps" in detail["results"][0]["trajectory"]
    slim = client.ok("GET", f"{P}/evals/{evals[0]['eval_id']}?trajectories=0")
    assert "steps" not in slim["results"][0]["trajectory"]

    errors = client.ok("GET", P + "/errors")["errors"]
    assert errors
    stats = client.ok("GET", P + "/errors/stats")
    assert stats["total"] == len(errors)
    cid = errors[0]["case_id"]
    assert client.ok(
        "POST", f"{P}/errors/{cid}/annotate", {"category": "missing_rule", "root_cause": "x", "status": "wontfix"}
    )["ok"]
    assert [e for e in client.ok("GET", P + "/errors?status=wontfix")["errors"] if e["case_id"] == cid]
    assert client.call("POST", f"{P}/errors/nope/annotate", {"category": "x"})[0] == 404
    assert client.call("POST", f"{P}/errors/{cid}/annotate", {"status": "bogus"})[0] == 400

    assert client.ok("GET", P + "/history")["history"]
    assert client.ok("GET", P + "/metrics")
    assert client.ok("GET", P + "/artifacts")["artifacts"]
    assert client.ok("GET", P + "/gitlog")["log"]
    assert len(client.ok("GET", P + "/cases?split=val")["cases"]) > 0
    assert client.ok("POST", P + "/outputs", {})["outputs"]
    status, headers, body = client.raw("GET", P + "/report")
    assert status == 200 and headers["Content-Security-Policy"].startswith("sandbox")
    assert b"<script>alert(1)</script>" not in body
    assert "content" in client.ok("GET", P + "/file?path=outputs/metrics_summary.json")

    # reviews: human verdict + sheet export
    ev = evals[0]["eval_id"]
    case_id = detail["results"][0]["case_id"]
    assert client.ok(
        "POST", P + "/reviews", {"eval_id": ev, "case_id": case_id, "trial": 0, "passed": False, "comment": "no"}
    )["ok"]
    done = client.ok("GET", P + "/reviews?status=done")["reviews"]
    assert done and done[0]["human_passed"] is False and "input" in done[0] and "output" in done[0]
    assert client.call("POST", P + "/reviews", {"eval_id": ev, "case_id": case_id, "passed": "yes"})[0] == 400
    assert client.call("POST", P + "/reviews", {"eval_id": "ev_missing", "case_id": case_id, "passed": True})[0] == 404
    status, headers, body = client.raw("GET", f"{P}/review-sheet?eval_id={ev}")
    assert status == 200 and headers["Content-Type"].startswith("text/csv") and b"case_id" in body

    # manual versions: import, promote, reject
    new = client.ok(
        "POST", P + "/versions/import", {"skill_md": SEED + "\n- extra guidance line\n", "summary": "hand edit"}
    )
    assert new["origin"] == "manual"
    assert client.call("POST", P + "/versions/import", {"skill_md": "/etc/hosts"})[0] == 400  # text, never a path
    assert client.call("POST", P + "/versions/import", {"skill_md": ""})[0] == 400
    assert client.ok("POST", f"{P}/versions/{new['id']}/promote", {"reason": "try"})["status"] == "best"
    assert client.call("POST", f"{P}/versions/{new['id']}/reject", {})[0] == 400  # cannot reject the best
    assert client.ok("POST", f"{P}/versions/{versions['best']}/promote", {})["status"] == "best"
    assert client.ok("POST", f"{P}/versions/{new['id']}/reject", {"reason": "worse"})["status"] == "rejected"


def test_config_requirement_cases_and_devset(client):
    create_project(client, cases=[])
    P = "/api/projects/demo"
    cfg = client.ok("GET", P + "/config")
    assert cfg["agent"]["adapter"] == "mock"
    new = client.ok("PATCH", P + "/config", {"evolve": {"max_iterations": 3, "min_delta": 0.02}, "eval": {"trials": 2}})
    assert new["evolve"]["max_iterations"] == 3 and new["eval"]["trials"] == 2
    assert client.ok("GET", P + "/config")["evolve"]["min_delta"] == 0.02
    assert client.call("PATCH", P + "/config", {"evolve": {"metric": "bogus"}})[0] == 400
    assert client.call("PATCH", P + "/config", {"project": {"dataset": "../../outside.jsonl"}})[0] == 403
    client.ok("PUT", P + "/requirement", {"text": "# New\n- rule one\n- rule two"})
    assert "rule two" in client.ok("GET", P + "/requirement")["text"]
    assert client.ok("POST", P + "/cases", {"cases": mock_cases(4)})["added"] == 4
    assert len(client.ok("GET", P + "/cases")["cases"]) == 4
    assert client.call("POST", P + "/cases", {"cases": "nope"})[0] == 400
    job_id = client.ok("POST", P + "/jobs", {"kind": "devset", "params": {"merge": False}})["job_id"]
    job, _ = client.wait_job("demo", job_id)
    assert job["status"] == "succeeded", job
    ev_job = client.ok("POST", P + "/jobs", {"kind": "bootstrap"})["job_id"]
    assert client.wait_job("demo", ev_job)[0]["status"] == "succeeded"
    ev_job = client.ok("POST", P + "/jobs", {"kind": "evaluate", "params": {"split": "train"}})["job_id"]
    job, _ = client.wait_job("demo", ev_job)
    assert job["status"] == "succeeded" and job["result"]["eval_id"]


def test_cancel_slow_job(client):
    create_project(client, config={"agent": {"options": {"delay_s": 0.5}}, "eval": {"concurrency": 1}})
    P = "/api/projects/demo"
    job_id = client.ok("POST", P + "/jobs", {"kind": "evolve"})["job_id"]
    status, data = client.call("POST", P + "/jobs", {"kind": "evolve"})
    assert status == 409 and data["error"]["type"] == "conflict"
    time.sleep(0.3)
    assert client.ok("POST", f"{P}/jobs/{job_id}/cancel", {})["cancelled"] is True
    job, events = client.wait_job("demo", job_id, timeout=30)
    assert job["status"] == "cancelled"
    assert any(e["type"] == "job" and e["message"] == "cancelled" for e in events)
    assert client.ok("POST", f"{P}/jobs/{job_id}/cancel", {})["cancelled"] is False


def test_error_mapping(client):
    create_project(client)
    P = "/api/projects/demo"
    for method, path, body, want in [
        ("GET", "/api/projects/nope/info", None, 404),
        ("GET", "/api/projects/..%2F..%2Fetc/info", None, 404),
        ("GET", P + "/versions/v999", None, 404),
        ("GET", P + "/versions/..%2Fx", None, 404),
        ("GET", P + "/evals/ev_missing", None, 404),
        ("GET", P + "/jobs/job_missing", None, 404),
        ("GET", P + "/jobs/job_missing/events", None, 404),
        ("POST", P + "/jobs/job_missing/cancel", {}, 404),
        ("GET", P + "/nothing", None, 404),
        ("GET", P + "/report", None, 404),
        ("GET", P + "/diff?a=v001", None, 400),
        ("POST", P + "/jobs", {"kind": "rm-rf"}, 400),
        ("POST", P + "/jobs", {"kind": "evaluate", "params": {"split": "bogus"}}, 400),
        ("POST", P + "/jobs", {"kind": "evolve", "params": {"max_iterations": "many"}}, 400),
        ("POST", "/api/projects", {"name": "demo"}, 400),
        ("POST", "/api/projects", {"name": "../evil"}, 404),
        ("POST", "/api/projects", {"name": "x", "cases": "nope"}, 400),
        ("GET", P + "/file?path=../../../../etc/passwd", None, 403),
        ("GET", P + "/file?path=/etc/passwd", None, 403),
        ("GET", P + "/file?path=missing.txt", None, 404),
        ("GET", P + "/file", None, 400),
    ]:
        status, data = client.call(method, path, body)
        assert status == want, (method, path, status, data)
        assert set(data["error"]) == {"type", "message"} and "Traceback" not in data["error"]["message"]
    assert client.raw("POST", P + "/jobs", b"{not json")[0] == 400
    assert client.raw("POST", P + "/jobs", b"[1]")[0] == 400
    assert client.raw("POST", P + "/jobs", b'{"kind":"outputs"}', {"Content-Type": "text/plain"})[0] == 415
    assert client.raw("POST", P + "/jobs", b"x", {"Content-Length": str(6 * 1024 * 1024)})[0] == 413


def test_host_and_origin_checks(client):
    create_project(client)
    P = "/api/projects/demo"
    port = client.port
    assert client.call("GET", "/api/projects", headers={"Host": "evil.example.com"})[0] == 403
    assert client.raw("GET", "/", headers={"Host": f"evil.example.com:{port}"})[0] == 403
    assert client.call("GET", "/api/projects", headers={"Host": f"localhost:{port}"})[0] == 200
    same = {"Origin": f"http://127.0.0.1:{port}"}
    assert client.call("POST", P + "/outputs", {}, same)[0] == 200
    for bad in (
        {"Origin": "http://evil.example.com"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Origin": f"http://127.0.0.1:{port + 1}"},
    ):
        status, data = client.call("POST", P + "/jobs", {"kind": "evolve"}, bad)
        assert status == 403 and data["error"]["type"] == "forbidden", bad
        assert client.call("GET", P + "/info", headers=bad)[0] == 403
    assert client.ok("GET", P + "/jobs")["jobs"] == [] or all(
        j["kind"] == "outputs" for j in client.ok("GET", P + "/jobs")["jobs"]
    )


def test_token(tmp_path):
    with pytest.raises(ValueError, match="token"):
        create_server(tmp_path / "ws", host="0.0.0.0", port=0)
    srv = create_server(tmp_path / "ws", port=0, token="s3cret-✓")
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        anon, auth = Client(srv), Client(srv, "s3cret-✓".encode().decode("latin-1"))
        assert anon.raw("GET", "/")[0] == 200  # the shell is public, every API route is not
        assert anon.call("GET", "/api/projects")[0] == 401
        assert anon.call("GET", "/api/meta")[0] == 401
        assert anon.call("GET", "/api/projects", headers={"Authorization": "Bearer wrong"})[0] == 401
        assert anon.call("GET", "/api/projects?token=wrong")[0] == 401
        assert auth.call("GET", "/api/projects")[0] == 200
        assert anon.call("GET", "/api/projects?token=s3cret-%E2%9C%93")[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)


def test_single_project_workspace(tmp_path):
    from skillloop.core.project import Project

    Project.init(tmp_path / "solo", "solo", requirement="# r").close()
    srv = create_server(tmp_path / "solo", port=0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        c = Client(srv)
        data = c.ok("GET", "/api/projects")
        assert data["single"] and [p["name"] for p in data["projects"]] == ["solo"]
        assert c.ok("GET", "/api/projects/solo/info")["name"] == "solo"
        assert c.call("POST", "/api/projects", {"name": "other"})[0] == 400
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(5)
