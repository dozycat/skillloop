"""Offline tests for the agent adapters (fake CLIs are tiny Python scripts; HTTP uses a local server)."""

from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from skillloop.adapters._proc import run_process
from skillloop.adapters.base import AgentRequest
from skillloop.adapters.callable import CallableAdapter, FunctionAdapter
from skillloop.adapters.claude_code import ClaudeCodeAdapter, parse_stream_json
from skillloop.adapters.codex import CodexAdapter, parse_codex_jsonl
from skillloop.adapters.command import CommandAdapter
from skillloop.adapters.http import HTTPAdapter
from skillloop.adapters.llm import LLMAdapter
from skillloop.adapters.nanobot import NanobotAdapter
from skillloop.core.models import Case, Step, Trajectory
from skillloop.core.registry import ADAPTERS
from skillloop.core.skill import Skill

PY = sys.executable
SKILL = Skill(
    "demo-skill",
    "Use for demo tasks",
    "# Demo\n\n## Rules\n\n- [r1] Always answer PONG.\n",
    files={"references/notes.md": "extra notes"},
)


def req(tmp_path: Path, input: str = "ping", timeout: float = 20.0, **kw) -> AgentRequest:
    wd = tmp_path / "work"
    wd.mkdir(exist_ok=True)
    return AgentRequest(case=Case(id="c1", input=input), skill=SKILL, workdir=wd, timeout_s=timeout, **kw)


def script(tmp_path: Path, body: str, name: str = "agent.py") -> str:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return str(p)


# --------------------------------------------------------------------------- _proc


def test_proc_ok_and_stream(tmp_path):
    lines = []
    r = run_process([PY, "-c", "import sys; print('a'); print('b'); sys.stderr.write('warn')"], on_line=lines.append)
    assert (r.returncode, r.status) == (0, "ok")
    assert r.stdout == "a\nb\n" and r.stderr == "warn" and lines == ["a", "b"]
    rc, out, err, status = r
    assert status == "ok"


def test_proc_error_timeout_missing(tmp_path):
    r = run_process([PY, "-c", "import sys; sys.exit(3)"])
    assert r.status == "error" and r.returncode == 3 and "exit code 3" in r.error
    t0 = time.time()
    r = run_process([PY, "-c", "import time; time.sleep(30)"], timeout_s=0.5)
    assert r.status == "timeout" and time.time() - t0 < 10
    r = run_process(["definitely-not-a-binary-xyz"])
    assert r.status == "error" and r.returncode == 127


def test_proc_cancel_kills_process_group(tmp_path):
    marker = tmp_path / "child_alive"
    # parent spawns a child that would write a marker after 2s; cancelling must kill both
    code = (
        f"import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', \"import time; time.sleep(2); open(r'{marker}', 'w').write('x')\"])\n"
        f"time.sleep(30)\n"
    )
    cancel = threading.Event()
    threading.Timer(0.4, cancel.set).start()
    t0 = time.time()
    r = run_process([PY, "-c", code], cancel=cancel)
    assert r.status == "cancelled" and time.time() - t0 < 10
    time.sleep(2.5)
    assert not marker.exists()


def test_proc_truncates_output():
    r = run_process(
        [PY, "-c", "print('x' * 1000 + '\\n') \nfor i in range(3000): print('y' * 100)"], max_capture=10_000
    )
    assert len(r.stdout) < 12_000 and "truncated" in r.stdout


# --------------------------------------------------------------------------- command


def test_command_inline_stdin(tmp_path):
    echo = script(
        tmp_path,
        "import sys; data = sys.stdin.read(); print('HAS_SKILL' if '[r1]' in data else 'NO'); print('got:' + data.strip()[-7:])",
    )
    a = CommandAdapter(command=[PY, echo], output="last_line")
    t = a.run(req(tmp_path))
    assert t.status == "ok" and t.case_id == "c1"
    assert t.output == "got:</task>" and "HAS_SKILL" in t.raw
    assert a.healthcheck()[0]


def test_command_dir_mode_placeholders_and_artifacts(tmp_path):
    body = (
        "import sys, json, pathlib\n"
        "skill = pathlib.Path(sys.argv[1]).read_text()\n"
        "mcp = json.load(open(sys.argv[3]))\n"
        "pathlib.Path('result.txt').write_text('made by agent')\n"
        "print(json.dumps({'result': {'answer': 'PONG' if 'PONG' in skill else 'no'}, 'prompt': sys.argv[2],\n"
        "  'trace': [{'kind': 'tool_call', 'name': 'calc', 'content': '1+1'}], 'usage': {'input_tokens': 5},\n"
        "  'servers': sorted(mcp['mcpServers'])}))\n"
    )
    a = CommandAdapter(
        command=[PY, script(tmp_path, body), "{skill_file}", "{prompt}", "{mcp_config_file}"],
        skill_mode="dir",
        skills_dir="my/skills",
        output="json",
        answer_path="result.answer",
        steps_path="trace",
        usage_path="usage",
    )
    r = req(tmp_path, mcp_config={"mcpServers": {"crm": {"command": "x"}}})
    t = a.run(r)
    assert t.status == "ok" and t.output == "PONG"
    assert (r.workdir / "my/skills/demo-skill/SKILL.md").is_file()
    assert (r.workdir / "my/skills/demo-skill/references/notes.md").is_file()
    assert t.steps[0].kind == "tool_call" and t.steps[0].name == "calc"
    assert t.usage == {"input_tokens": 5}
    assert t.artifacts == {"result.txt": "made by agent"}  # skill files are inputs, not artifacts
    assert json.loads(t.raw)["prompt"] == "ping" and json.loads(t.raw)["servers"] == ["crm"]


def test_command_file_mode_and_regex(tmp_path):
    body = "import pathlib; print('thinking...'); print('ANSWER: ' + ('yes' if '[r1]' in pathlib.Path('AGENTS.md').read_text() else 'no'))"
    a = CommandAdapter(
        command=f"{PY} {script(tmp_path, body)}",
        skill_mode="file",
        skill_file="AGENTS.md",
        answer_regex=r"ANSWER:\s*(\w+)",
    )
    t = a.run(req(tmp_path))
    assert t.status == "ok" and t.output == "yes"


def test_command_failures_never_raise(tmp_path):
    t = CommandAdapter(command=[PY, "-c", "import sys; print('partial'); sys.exit(2)"]).run(req(tmp_path))
    assert t.status == "error" and "exit code 2" in t.error and t.output == "partial"
    t = CommandAdapter(command=[PY, "-c", "import time; time.sleep(30)"]).run(req(tmp_path, timeout=0.5))
    assert t.status == "timeout"
    t = CommandAdapter(command=["no-such-agent-binary"]).run(req(tmp_path))
    assert t.status == "error" and "cannot start" in t.error
    assert not CommandAdapter(command=["no-such-agent-binary"]).healthcheck()[0]
    t = CommandAdapter().run(req(tmp_path))
    assert t.status == "error"
    t = CommandAdapter(command=[PY, "-c", "print('not json')"], output="json").run(req(tmp_path))
    assert t.status == "error" and "not JSON" in t.error
    t = CommandAdapter(command=[PY, "-c", "print('{\"x\": 1}')"], output="json").run(req(tmp_path))
    assert t.status == "error" and "output" in t.error


def test_command_cancel(tmp_path):
    r = req(tmp_path)
    threading.Timer(0.3, r.cancel.set).start()
    t0 = time.time()
    t = CommandAdapter(command=[PY, "-c", "import time; time.sleep(30)"]).run(r)
    assert t.status == "cancelled" and time.time() - t0 < 10


def test_command_concurrent(tmp_path):
    a = CommandAdapter(
        command=[PY, "-c", "import sys; print(sys.stdin.read()[-20:].strip())"], prompt_template="{input}"
    )
    out: dict[int, str] = {}

    def go(i: int) -> None:
        wd = tmp_path / f"w{i}"
        wd.mkdir()
        out[i] = a.run(AgentRequest(case=Case(id=f"c{i}", input=f"task-{i}"), skill=SKILL, workdir=wd)).output

    ths = [threading.Thread(target=go, args=(i,)) for i in range(6)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    assert out == {i: f"task-{i}" for i in range(6)}


# --------------------------------------------------------------------------- claude_code

CLAUDE_EVENTS = [
    {"type": "system", "subtype": "init", "model": "claude-x", "session_id": "s1", "skills": ["demo-skill"]},
    {
        "type": "assistant",
        "message": {
            "content": [
                {"type": "thinking", "thinking": "let me look"},
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.txt"}},
            ]
        },
    },
    {
        "type": "user",
        "message": {
            "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "file body"}]}
            ]
        },
    },
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "pong"}]}},
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "pong",
        "num_turns": 2,
        "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 10, "output_tokens": 3},
    },
]

FAKE_CLAUDE = """
import sys, json, os, pathlib
args = sys.argv[1:]
prompt = sys.stdin.read()
pathlib.Path('argv.json').write_text(json.dumps({'args': args, 'prompt': prompt,
    'skill': os.path.exists('.claude/skills/demo-skill/SKILL.md')}))
if 'FAIL' in prompt:
    print(json.dumps({'type': 'result', 'subtype': 'error_max_turns', 'is_error': True, 'result': ''}))
    sys.exit(1)
for ev in json.loads(os.environ['FAKE_EVENTS']):
    print(json.dumps(ev))
"""


def fake_bin(tmp_path: Path, body: str, name: str) -> str:
    src = script(tmp_path, body, name + ".py")
    exe = tmp_path / name
    exe.write_text(f'#!/bin/sh\nexec {PY} {src} "$@"\n')
    exe.chmod(0o755)
    return str(exe)


def test_parse_stream_json():
    t = parse_stream_json("\n".join(json.dumps(e) for e in CLAUDE_EVENTS) + "\nnoise line\n")
    assert t.status == "ok" and t.output == "pong"
    assert [s.kind for s in t.steps] == ["system", "thought", "tool_call", "tool_result", "message"]
    assert t.steps[2].name == "Read" and "a.txt" in t.steps[2].content and t.steps[3].content == "file body"
    assert t.usage == {"input_tokens": 10, "output_tokens": 3, "cost_usd": 0.0123, "turns": 2}
    assert parse_stream_json("garbage").status == "error"


def test_claude_code_adapter(tmp_path):
    binary = fake_bin(tmp_path, FAKE_CLAUDE, "claude")
    a = ClaudeCodeAdapter(
        binary=binary,
        model="haiku",
        allowed_tools=["Read", "Bash(ls *)"],
        max_turns=3,
        env={"FAKE_EVENTS": json.dumps(CLAUDE_EVENTS)},
    )
    r = req(tmp_path, mcp_config={"mcpServers": {"crm": {"command": "x", "args": []}}})
    t = a.run(r)
    assert t.status == "ok" and t.output == "pong" and t.usage["cost_usd"] == 0.0123
    seen = json.loads((r.workdir / "argv.json").read_text())
    args = seen["args"]
    assert seen["prompt"] == "ping" and seen["skill"] is True
    assert args[:4] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    assert args[args.index("--model") + 1] == "haiku"
    assert args[args.index("--allowed-tools") + 1] == "Read,Bash(ls *)"
    assert args[args.index("--max-turns") + 1] == "3"
    mcp = json.loads(Path(args[args.index("--mcp-config") + 1]).read_text())
    assert "crm" in mcp["mcpServers"] and "--strict-mcp-config" in args
    assert "--dangerously-skip-permissions" not in args
    assert all(not k.startswith(".claude/") for k in t.artifacts) and "argv.json" in t.artifacts
    assert a.healthcheck()[0]

    t = a.run(req(tmp_path, input="please FAIL"))
    assert t.status == "error" and "error_max_turns" in t.error
    assert not ClaudeCodeAdapter(binary="no-such-claude").healthcheck()[0]


# --------------------------------------------------------------------------- codex

CODEX_EVENTS = [
    {"type": "thread.started", "thread_id": "th1"},
    {"type": "turn.started"},
    {"type": "item.completed", "item": {"id": "i0", "type": "reasoning", "text": "thinking"}},
    {
        "type": "item.completed",
        "item": {
            "id": "i1",
            "type": "command_execution",
            "command": "ls",
            "aggregated_output": "a.txt\n",
            "exit_code": 0,
        },
    },
    {"type": "item.completed", "item": {"id": "i2", "type": "agent_message", "text": "pong"}},
    {"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 7}},
]

FAKE_CODEX = """
import sys, json, os, pathlib
args = sys.argv[1:]
prompt = sys.stdin.read()
wd = args[args.index('-C') + 1]
pathlib.Path(wd, 'argv.json').write_text(json.dumps({'args': args, 'prompt': prompt,
    'agents': pathlib.Path(wd, 'AGENTS.md').read_text()}))
for ev in json.loads(os.environ['FAKE_EVENTS']):
    print(json.dumps(ev))
pathlib.Path(args[args.index('-o') + 1]).write_text('pong-final')
"""


def test_parse_codex_jsonl():
    t = parse_codex_jsonl("\n".join(json.dumps(e) for e in CODEX_EVENTS))
    assert t.status == "ok" and t.output == "pong"
    assert [s.kind for s in t.steps] == ["thought", "tool_call", "tool_result", "message"]
    assert t.usage["input_tokens"] == 100 and t.usage["turns"] == 1
    t = parse_codex_jsonl(json.dumps({"type": "error", "message": "boom"}))
    assert t.status == "error" and "boom" in t.error


def test_codex_adapter(tmp_path):
    binary = fake_bin(tmp_path, FAKE_CODEX, "codex")
    a = CodexAdapter(
        binary=binary,
        model="gpt-x",
        config={"model_reasoning_effort": "low"},
        env={"FAKE_EVENTS": json.dumps(CODEX_EVENTS)},
    )
    r = req(
        tmp_path, mcp_config={"mcpServers": {"crm": {"command": "python", "args": ["-m", "srv"], "env": {"K": "V"}}}}
    )
    t = a.run(r)
    assert t.status == "ok" and t.output == "pong-final"
    seen = json.loads((r.workdir / "argv.json").read_text())
    args = seen["args"]
    assert args[0] == "exec" and "--json" in args and "--skip-git-repo-check" in args and args[-1] == "-"
    assert args[args.index("-s") + 1] == "workspace-write" and args[args.index("-m") + 1] == "gpt-x"
    assert seen["prompt"] == "ping" and "[r1] Always answer PONG" in seen["agents"]
    cfg = [args[i + 1] for i, x in enumerate(args) if x == "-c"]
    assert 'model_reasoning_effort="low"' in cfg
    assert 'mcp_servers.crm.command="python"' in cfg and 'mcp_servers.crm.args=["-m", "srv"]' in cfg
    assert 'mcp_servers.crm.env={"K" = "V"}' in cfg
    assert "AGENTS.md" not in t.artifacts
    assert (r.workdir / ".codex/skills/demo-skill/references/notes.md").is_file()


# --------------------------------------------------------------------------- nanobot


def test_nanobot_adapter(tmp_path):
    body = (
        "import sys, os, pathlib\n"
        "a = sys.argv[1:]\n"
        "ws = a[a.index('--workspace') + 1]\n"
        "print('🐈 nanobot')\n"
        "print('skill-ok' if os.path.exists(os.path.join(ws, 'skills/demo-skill/SKILL.md')) else 'skill-missing')\n"
        "print('msg=' + a[a.index('-m') + 1])\n"
    )
    binary = fake_bin(tmp_path, body, "nanobot")
    r = req(tmp_path)
    t = NanobotAdapter(binary=binary).run(r)
    assert t.status == "ok" and t.output == "skill-ok\nmsg=ping" and t.artifacts == {}
    t = NanobotAdapter(
        binary=binary,
        args=["agent", "--workspace", "{workdir}", "-m", "{prompt}"],
        skills_dir="custom/skills",
        config_path="/tmp/cfg.json",
    ).run(r)
    assert t.status == "ok" and (r.workdir / "custom/skills/demo-skill/SKILL.md").is_file()
    assert all(not k.startswith("custom/skills/") for k in t.artifacts)
    ok, msg = NanobotAdapter(binary="nanobot-not-installed-xyz").healthcheck()
    assert not ok and "not found" in msg
    assert NanobotAdapter(binary="nanobot-not-installed-xyz").run(r).status == "error"


# --------------------------------------------------------------------------- http


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.seen.append({"path": self.path, "auth": self.headers.get("Authorization"), "body": body})
        if self.path == "/slow":
            time.sleep(3)
        if self.path == "/boom":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"kaboom")
            return
        if self.path == "/text":
            payload = b"plain answer"
        elif self.path == "/apperr":
            payload = json.dumps({"output": "", "error": "agent crashed"}).encode()
        else:
            payload = json.dumps(
                {
                    "data": {"answer": "PONG" if "[r1]" in body["skill"]["markdown"] else "no"},
                    "steps": [{"kind": "tool_call", "name": "t", "content": "x"}, "free text"],
                    "usage": {"input_tokens": 3, "note": "ignored"},
                    "artifacts": {"a.txt": "A"},
                }
            ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture()
def server():
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    srv.seen = []
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    yield srv, f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


def test_http_adapter(tmp_path, server, monkeypatch):
    srv, base = server
    monkeypatch.setenv("AGENT_TOKEN", "sekret")
    a = HTTPAdapter(url=base + "/run", headers={"Authorization": "Bearer ${AGENT_TOKEN}"}, output_path="data.answer")
    t = a.run(req(tmp_path))
    assert t.status == "ok" and t.output == "PONG"
    assert [s.kind for s in t.steps] == ["tool_call", "message"] and t.usage == {"input_tokens": 3}
    assert t.artifacts == {"a.txt": "A"}
    seen = srv.seen[-1]
    assert seen["auth"] == "Bearer sekret" and seen["body"]["input"] == "ping" and seen["body"]["case_id"] == "c1"
    assert seen["body"]["skill"]["files"] == {"references/notes.md": "extra notes"}
    assert a.healthcheck()[0]

    assert HTTPAdapter(url=base + "/text").run(req(tmp_path)).output == "plain answer"
    t = HTTPAdapter(url=base + "/boom").run(req(tmp_path))
    assert t.status == "error" and "HTTP 500" in t.error
    t = HTTPAdapter(url=base + "/apperr").run(req(tmp_path))
    assert t.status == "error" and t.error == "agent crashed"
    t = HTTPAdapter(url=base + "/run").run(req(tmp_path))
    assert t.status == "error" and "'output'" in t.error
    t = HTTPAdapter(url=base + "/slow").run(req(tmp_path, timeout=0.5))
    assert t.status == "timeout"
    r = req(tmp_path)
    threading.Timer(0.3, r.cancel.set).start()
    t0 = time.time()
    t = HTTPAdapter(url=base + "/slow").run(r)
    assert t.status == "cancelled" and time.time() - t0 < 2
    t = HTTPAdapter(url="http://127.0.0.1:9/none").run(req(tmp_path, timeout=2))
    assert t.status == "error"
    assert not HTTPAdapter().healthcheck()[0] and HTTPAdapter().run(req(tmp_path)).status == "error"


# --------------------------------------------------------------------------- llm / callable / registry


def test_llm_adapter(tmp_path):
    from skillloop.llm.mock import MockLLM

    seen = {}

    def agent(llm, prompt, system):
        seen.update(prompt=prompt, system=system)
        return "PONG" if "[r1]" in system else "no"

    a = LLMAdapter(client=MockLLM(handlers={"agent": agent}))
    t = a.run(req(tmp_path))
    assert t.status == "ok" and t.output == "PONG" and seen["prompt"] == "ping" and t.usage["turns"] == 1

    def boom(llm, prompt, system):
        raise RuntimeError("model down")

    t = LLMAdapter(client=MockLLM(handlers={"agent": boom})).run(req(tmp_path))
    assert t.status == "error" and "model down" in t.error
    assert ADAPTERS.create("llm", provider="mock").run(req(tmp_path)).status == "ok"


def sample_agent(request: AgentRequest):
    return {"output": f"echo:{request.case.input}", "usage": {"turns": 2}}


class CustomAdapter(FunctionAdapter):
    def __init__(self, **options):
        super().__init__(lambda r: "custom", **options)


def test_callable_adapters(tmp_path):
    t = (
        CallableAdapter(target="tests.test_adapters:sample_agent").run(req(tmp_path))
        if False
        else CallableAdapter(target=f"{__name__}:sample_agent").run(req(tmp_path))
    )
    assert t.status == "ok" and t.output == "echo:ping" and t.usage == {"turns": 2} and t.case_id == "c1"
    bad = CallableAdapter(target="nope.module:fn")
    assert not bad.healthcheck()[0] and bad.run(req(tmp_path)).status == "error"
    assert FunctionAdapter(lambda r: "plain").run(req(tmp_path)).output == "plain"
    assert FunctionAdapter(lambda r: {"a": 1}).run(req(tmp_path)).output == '{"a": 1}'
    t = FunctionAdapter(lambda r: Trajectory(output="t", steps=[Step("thought", "x")])).run(req(tmp_path))
    assert t.output == "t" and t.steps[0].kind == "thought"
    t = FunctionAdapter(lambda r: 1 / 0).run(req(tmp_path))
    assert t.status == "error" and "ZeroDivisionError" in t.error
    r = req(tmp_path)
    r.cancel.set()
    assert FunctionAdapter(lambda r: "x").run(r).status == "cancelled"


def test_registry_lookup(tmp_path):
    names = ADAPTERS.names()
    for n in ("mock", "command", "claude_code", "codex", "nanobot", "http", "llm", "callable"):
        assert n in names
    assert isinstance(ADAPTERS.create("command", command=["x"]), CommandAdapter)
    a = ADAPTERS.create(f"{__name__}:CustomAdapter")
    assert a.run(req(tmp_path)).output == "custom"
    with pytest.raises(KeyError):
        ADAPTERS.create("does-not-exist")
