import io
import json
import threading
import time

from skillloop.mcp.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    LATEST_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    ToolError,
    ToolServer,
    validate_arguments,
)


def make_server() -> ToolServer:
    s = ToolServer("test", "1.2.3", instructions="be nice")

    @s.tool(
        "add",
        "Add numbers",
        {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
            "required": ["a", "b"],
            "additionalProperties": False,
        },
    )
    def add(a, b):
        return {"sum": a + b}

    @s.tool(
        "echo",
        "Echo",
        {"type": "object", "properties": {"text": {"type": "string"}, "mode": {"type": "string", "enum": ["x", "y"]}}},
    )
    def echo(text="", mode="x"):
        return text

    @s.tool("items", "List")
    def items():
        return [1, 2]

    @s.tool("boom", "Fails")
    def boom():
        raise RuntimeError("kaput")

    @s.tool("soft", "Fails nicely")
    def soft():
        raise ToolError("nope")

    @s.tool("slow", "Sleeps", {"type": "object", "properties": {"s": {"type": "number"}}})
    def slow(s=0.3):
        time.sleep(s)
        return "done"

    return s


def req(method, params=None, id=1):
    m = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        m["params"] = params
    return m


def test_initialize_negotiates_version():
    s = make_server()
    r = s.handle(req("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "c"}}))
    assert r["result"]["protocolVersion"] == "2024-11-05"
    assert r["result"]["serverInfo"] == {"name": "test", "version": "1.2.3"}
    assert "tools" in r["result"]["capabilities"] and r["result"]["instructions"] == "be nice"
    r = s.handle(req("initialize", {"protocolVersion": "1999-01-01"}))
    assert r["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION


def test_notifications_get_no_response():
    s = make_server()
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert s.initialized
    assert s.handle({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}) is None
    assert s.handle({"jsonrpc": "2.0", "method": "no/such/notification"}) is None
    assert s.handle({"jsonrpc": "2.0", "id": 5, "result": {}}) is None


def test_ping_and_lists():
    s = make_server()
    assert s.handle(req("ping"))["result"] == {}
    tools = s.handle(req("tools/list"))["result"]["tools"]
    assert [t["name"] for t in tools] == ["add", "echo", "items", "boom", "soft", "slow"]
    assert tools[0]["inputSchema"]["required"] == ["a", "b"] and "fn" not in tools[0]
    assert tools[2]["inputSchema"] == {"type": "object", "properties": {}}
    assert s.handle(req("resources/list"))["result"] == {"resources": []}
    assert s.handle(req("prompts/list"))["result"] == {"prompts": []}


def test_call_results():
    s = make_server()
    r = s.handle(req("tools/call", {"name": "add", "arguments": {"a": 1, "b": 2}}))["result"]
    assert r["isError"] is False and r["structuredContent"] == {"sum": 3}
    assert json.loads(r["content"][0]["text"]) == {"sum": 3}
    r = s.handle(req("tools/call", {"name": "echo", "arguments": {"text": "hi"}}))["result"]
    assert r["content"] == [{"type": "text", "text": "hi"}] and "structuredContent" not in r
    r = s.handle(req("tools/call", {"name": "items"}))["result"]
    assert r["content"][0]["text"] == "[1, 2]" and "structuredContent" not in r


def test_call_errors_are_results():
    s = make_server()
    r = s.handle(req("tools/call", {"name": "boom", "arguments": {}}))
    assert "error" not in r and r["result"]["isError"] is True and "kaput" in r["result"]["content"][0]["text"]
    r = s.handle(req("tools/call", {"name": "soft"}))["result"]
    assert r["isError"] and r["content"][0]["text"] == "nope"


def test_argument_validation():
    s = make_server()
    r = s.handle(req("tools/call", {"name": "add", "arguments": {"a": "1"}}))["result"]
    text = r["content"][0]["text"]
    assert r["isError"] and "missing required argument 'b'" in text and "'a' must be of type number" in text
    r = s.handle(req("tools/call", {"name": "add", "arguments": {"a": 1, "b": 2, "c": 3}}))["result"]
    assert r["isError"] and "unexpected argument 'c'" in r["content"][0]["text"]
    r = s.handle(req("tools/call", {"name": "echo", "arguments": {"mode": "z"}}))["result"]
    assert r["isError"] and "must be one of" in r["content"][0]["text"]
    assert validate_arguments({"properties": {"n": {"type": "integer"}}}, {"n": True}) != []
    assert validate_arguments({"properties": {"n": {"type": ["integer", "null"]}}}, {"n": None}) == []


def test_protocol_errors():
    s = make_server()
    assert s.handle(req("tools/call", {"name": "nope"}))["error"]["code"] == INVALID_PARAMS
    assert s.handle(req("tools/call", {"arguments": {}}))["error"]["code"] == INVALID_PARAMS
    assert s.handle(req("tools/call", {"name": "add", "arguments": [1]}))["error"]["code"] == INVALID_PARAMS
    assert s.handle(req("bogus/method"))["error"]["code"] == METHOD_NOT_FOUND
    assert s.handle("hello")["error"]["code"] == INVALID_REQUEST
    assert s.handle({"id": 1, "method": "ping"})["error"]["code"] == INVALID_REQUEST
    assert s.handle({"jsonrpc": "2.0", "id": 1, "method": 7})["error"]["code"] == INVALID_REQUEST
    r = s.handle({"jsonrpc": "2.0", "id": 3, "method": "ping", "params": [1]})
    assert r["error"]["code"] == INVALID_PARAMS and r["id"] == 3


def run_stdio(server, lines):
    out = io.StringIO()
    server.serve_stdio(io.StringIO("".join(line + "\n" for line in lines)), out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


def test_stdio_roundtrip_and_parse_error():
    s = make_server()
    msgs = run_stdio(
        s,
        [
            json.dumps(req("initialize", {"protocolVersion": LATEST_PROTOCOL_VERSION}, id=1)),
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            "{not json",
            "",
            json.dumps(req("tools/call", {"name": "add", "arguments": {"a": 2, "b": 3}}, id="x")),
            json.dumps(
                [req("ping", id=10), {"jsonrpc": "2.0", "method": "notifications/progress"}, req("ping", id=11)]
            ),
        ],
    )
    by_id = {m["id"]: m for m in msgs if isinstance(m, dict)}
    assert by_id[1]["result"]["protocolVersion"] == LATEST_PROTOCOL_VERSION
    assert by_id[None]["error"]["code"] == PARSE_ERROR
    assert by_id["x"]["result"]["structuredContent"] == {"sum": 5}
    batch = next(m for m in msgs if isinstance(m, list))
    assert [m["id"] for m in batch] == [10, 11]
    assert len(msgs) == 4


def test_slow_tool_does_not_block_ping():
    s = make_server()
    r, w = _pipe()
    out = _Collector()
    t = threading.Thread(target=s.serve_stdio, args=(r, out), daemon=True)
    t.start()
    w.write(json.dumps(req("tools/call", {"name": "slow", "arguments": {"s": 0.6}}, id=1)) + "\n")
    w.write(json.dumps(req("ping", id=2)) + "\n")
    w.flush()
    assert out.wait(1, timeout=0.4), "ping should answer while the slow tool is still running"
    assert out.messages[0]["id"] == 2
    w.close()
    t.join(timeout=5)
    assert not t.is_alive()
    assert [m["id"] for m in out.messages] == [2, 1]
    assert out.messages[1]["result"]["content"][0]["text"] == "done"


def _pipe():
    import os

    rfd, wfd = os.pipe()
    return os.fdopen(rfd, "r", encoding="utf-8"), os.fdopen(wfd, "w", encoding="utf-8")


class _Collector:
    def __init__(self):
        self.messages, self._buf, self._cv = [], "", threading.Condition()

    def write(self, s):
        with self._cv:
            self._buf += s
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line.strip():
                    self.messages.append(json.loads(line))
            self._cv.notify_all()

    def flush(self):
        pass

    def wait(self, n, timeout):
        with self._cv:
            return self._cv.wait_for(lambda: len(self.messages) >= n, timeout)
