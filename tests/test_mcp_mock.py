import json
import subprocess
import sys

import pytest

from skillloop.core.config import Config
from skillloop.core.project import Project
from skillloop.mcp.mock_server import build_server, fill, matches, resolve
from skillloop.mcp.profiles import build_mcp_config, list_profiles, read_call_log, validate_profile

PROFILE = {
    "tools": [
        {
            "name": "lookup_order",
            "description": "Look up an order",
            "input_schema": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}, "note": {"type": "string"}},
                "required": ["order_id"],
            },
            "responses": [
                {"when": {"order_id": "42"}, "result": {"status": "shipped", "id": "{{order_id}}"}},
                {"when": {"order_id": {"regex": "^9"}}, "error": "order {{order_id}} not found"},
                {"when": {"note": {"contains": "urgent"}}, "result": "queued {{order_id}}"},
            ],
            "default": "unknown order {{order_id}}",
        },
        {"name": "strict", "responses": [{"when": {"x": 1}, "result": "one"}]},
    ]
}


def test_matching_and_fill():
    assert matches(None, {}) and matches({}, {"a": 1})
    assert matches({"a": 1}, {"a": 1}) and matches({"a": "1"}, {"a": 1}) and not matches({"a": 2}, {"a": 1})
    assert not matches({"a": 1}, {}) and not matches({"a": True}, {"a": "True"})
    assert matches({"a": {"regex": "^ab"}}, {"a": "abc"}) and not matches({"a": {"regex": "^b"}}, {"a": "abc"})
    assert matches({"a": {"contains": "b"}}, {"a": "abc"}) and matches({"a": {"k": 1}}, {"a": {"k": 1}})
    assert fill({"x": ["{{a}} and {{ b }}", "{{zz}}"], "n": 3}, {"a": 1, "b": "two"}) == {
        "x": ["1 and two", "{{zz}}"],
        "n": 3,
    }
    assert resolve(PROFILE["tools"][0], {"order_id": "7"})[:2] == (-1, "unknown order 7")
    assert resolve(PROFILE["tools"][1], {"x": 2})[0] == -2


def test_validate_profile():
    assert validate_profile(PROFILE) == []
    assert validate_profile({}) and validate_profile({"tools": []}) and validate_profile([])
    bad = {
        "tools": [
            {"name": "bad name!"},
            {"name": "t", "responses": [{"when": {"a": {"regex": "("}}}]},
            {"name": "t", "input_schema": {"type": "string"}, "responses": [{"result": 1, "delay_s": -1}]},
        ]
    }
    text = " | ".join(validate_profile(bad))
    for needle in (
        "tools[0].name",
        "needs a 'result' or an 'error'",
        "invalid regex",
        "more than once",
        "type 'object'",
        "delay_s",
    ):
        assert needle in text, needle


def test_in_process_server_logs_calls(tmp_path):
    log = tmp_path / "calls.jsonl"
    server = build_server(PROFILE, "crm", log)
    ok = server.call_tool("lookup_order", {"order_id": "42"})
    assert ok["structuredContent"] == {"status": "shipped", "id": "42"}
    err = server.call_tool("lookup_order", {"order_id": "91"})
    assert err["isError"] and err["content"][0]["text"] == "order 91 not found"
    records = [json.loads(x) for x in log.read_text().splitlines()]
    assert [r["matched_index"] for r in records] == [0, 1]
    assert records[0]["result"]["status"] == "shipped" and records[1]["error"] == "order 91 not found"
    assert (
        records[0]["tool"] == "lookup_order" and records[0]["arguments"] == {"order_id": "42"} and records[0]["ts"] > 0
    )


class Client:
    def __init__(self, args):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "skillloop.mcp.mock_server", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self._id = 0

    def notify(self, method, params=None):
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self._id += 1
        self.proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}) + "\n"
        )
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, "server closed stdout: " + self.proc.stderr.read()
        msg = json.loads(line)
        assert msg["id"] == self._id
        return msg

    def call(self, name, arguments):
        return self.request("tools/call", {"name": name, "arguments": arguments})["result"]

    def close(self):
        self.proc.stdin.close()
        code = self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.proc.stderr.close()
        return code


def test_subprocess_stdio_end_to_end(tmp_path):
    profile, log = tmp_path / "p.json", tmp_path / "logs" / "calls.jsonl"
    profile.write_text(json.dumps(PROFILE), encoding="utf-8")
    c = Client(["--profile", str(profile), "--log", str(log), "--name", "crm"])
    try:
        init = c.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}},
        )["result"]
        assert init["serverInfo"]["name"] == "crm" and init["protocolVersion"] == "2025-06-18"
        c.notify("notifications/initialized")
        tools = c.request("tools/list")["result"]["tools"]
        assert [t["name"] for t in tools] == ["lookup_order", "strict"]
        assert tools[0]["inputSchema"]["required"] == ["order_id"]

        matched = c.call("lookup_order", {"order_id": "42"})
        assert matched["isError"] is False and matched["structuredContent"] == {"status": "shipped", "id": "42"}
        default = c.call("lookup_order", {"order_id": "7"})
        assert default["content"][0]["text"] == "unknown order 7"
        placeholder = c.call("lookup_order", {"order_id": "8", "note": "very urgent please"})
        assert placeholder["content"][0]["text"] == "queued 8"
        error = c.call("lookup_order", {"order_id": "99"})
        assert error["isError"] is True and error["content"][0]["text"] == "order 99 not found"
        nomatch = c.call("strict", {"x": 5})
        assert nomatch["isError"] is True and "no response configured" in nomatch["content"][0]["text"]
        invalid = c.call("lookup_order", {})
        assert invalid["isError"] is True and "order_id" in invalid["content"][0]["text"]
        assert c.request("tools/call", {"name": "ghost", "arguments": {}})["error"]["code"] == -32602
        assert c.request("ping")["result"] == {}
    finally:
        assert c.close() == 0
    records = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()]
    assert [(r["tool"], r["matched_index"]) for r in records] == [
        ("lookup_order", 0),
        ("lookup_order", -1),
        ("lookup_order", 2),
        ("lookup_order", 1),
        ("strict", -2),
    ]
    assert "error" in records[3] and "error" in records[4] and records[2]["result"] == "queued 8"


def test_bad_profile_exits_nonzero(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"tools": []}))
    r = subprocess.run(
        [sys.executable, "-m", "skillloop.mcp.mock_server", "--profile", str(p)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert r.returncode == 2 and r.stdout == "" and "invalid mock MCP profile" in r.stderr


def test_project_profiles(tmp_path):
    cfg = Config()
    cfg.mcp_profiles = {"crm": PROFILE, "broken": {"tools": []}}
    cfg.agent.mcp_profile = "crm"
    project = Project.init(tmp_path / "proj", "demo", config=cfg)
    project = Project.open(tmp_path / "proj")  # profile must survive the TOML round trip
    assert validate_profile(project.config.mcp_profiles["crm"]) == []

    listed = {p["name"]: p for p in list_profiles(project)}
    assert (
        listed["crm"]["tools"] == ["lookup_order", "strict"]
        and listed["crm"]["active"]
        and not listed["crm"]["problems"]
    )
    assert listed["broken"]["problems"]

    conf = build_mcp_config(project, "crm")
    spec = conf["mcpServers"]["crm"]
    assert spec["command"] == sys.executable and spec["args"][:2] == ["-m", "skillloop.mcp.mock_server"]
    profile_path = spec["args"][spec["args"].index("--profile") + 1]
    assert (
        project.state.contains(profile_path)
        and json.loads(open(profile_path).read())["tools"][0]["name"] == "lookup_order"
    )

    assert read_call_log(project, "crm") == []
    c = Client(spec["args"][2:])
    try:
        c.request("initialize", {"protocolVersion": "2024-11-05"})
        c.call("lookup_order", {"order_id": "42"})
        c.call("lookup_order", {"order_id": "1"})
    finally:
        c.close()
    calls = read_call_log(project, "crm")
    assert [r["matched_index"] for r in calls] == [0, -1]
    assert len(read_call_log(project, "crm", limit=1)) == 1

    with pytest.raises(ValueError, match="unknown mock MCP profile"):
        build_mcp_config(project, "nope")
    with pytest.raises(ValueError, match="non-empty 'tools'"):
        build_mcp_config(project, "broken")
    project.close()
