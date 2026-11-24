"""Minimal MCP (Model Context Protocol) server layer over stdio, stdlib only.

Implements the tool-serving subset of MCP on JSON-RPC 2.0 with newline-delimited JSON framing
(the stdio transport): ``initialize``, ``notifications/*``, ``ping``, ``tools/list``,
``tools/call``, plus empty ``resources/list`` / ``prompts/list`` so strict clients stay happy.

Usage::

    server = ToolServer("my-server", "1.0")

    @server.tool("add", "Add two numbers", {"type": "object",
                 "properties": {"a": {"type": "number"}, "b": {"type": "number"}}, "required": ["a", "b"]})
    def add(a, b):
        return {"sum": a + b}

    server.serve_stdio()
"""

from __future__ import annotations

import json
import sys
import threading
import traceback
from collections.abc import Callable
from typing import Any, TextIO

# Protocol revisions this server can speak, newest first.
SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")
LATEST_PROTOCOL_VERSION = SUPPORTED_PROTOCOL_VERSIONS[0]

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

ToolFn = Callable[..., Any]


class ToolError(Exception):
    """Raise inside a tool to return an ``isError`` result with exactly this message."""


_TYPE_CHECKS: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool) or (isinstance(v, float) and v.is_integer()),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
    "null": lambda v: v is None,
}


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> list[str]:
    """Shallow JSON-Schema check of tool arguments: required, property types, enum,
    and ``additionalProperties: false``. Returns human-readable problems (empty == valid)."""
    problems: list[str] = []
    props = schema.get("properties") or {}
    for key in schema.get("required") or []:
        if key not in arguments:
            problems.append(f"missing required argument '{key}'")
    if schema.get("additionalProperties") is False:
        for key in arguments:
            if key not in props:
                problems.append(f"unexpected argument '{key}' (allowed: {', '.join(sorted(props)) or 'none'})")
    for key, value in arguments.items():
        spec = props.get(key)
        if not isinstance(spec, dict):
            continue
        types = spec.get("type")
        if types:
            types = [types] if isinstance(types, str) else list(types)
            known = [t for t in types if t in _TYPE_CHECKS]
            if known and not any(_TYPE_CHECKS[t](value) for t in known):
                problems.append(f"argument '{key}' must be of type {' | '.join(types)}, got {type(value).__name__}")
                continue
        if "enum" in spec and value not in spec["enum"]:
            problems.append(f"argument '{key}' must be one of {spec['enum']!r}, got {value!r}")
    return problems


def _error(msg_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": msg_id, "error": err}


def _result(msg_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


class ToolServer:
    """A set of tools exposed over MCP. ``handle`` is transport-agnostic; ``serve_stdio`` runs it."""

    def __init__(self, name: str, version: str = "0.1.0", instructions: str = ""):
        self.name = name
        self.version = version
        self.instructions = instructions
        self.protocol_version = LATEST_PROTOCOL_VERSION  # negotiated on initialize
        self.initialized = False
        self._tools: dict[str, dict[str, Any]] = {}

    # ---------------------------------------------------------------- registration
    def add_tool(self, name: str, description: str, input_schema: dict[str, Any] | None, fn: ToolFn) -> None:
        schema = dict(input_schema or {})
        schema.setdefault("type", "object")
        schema.setdefault("properties", {})
        self._tools[name] = {"name": name, "description": description, "inputSchema": schema, "fn": fn}

    def tool(
        self,
        name: str,
        description: str = "",
        input_schema: dict[str, Any] | None = None,
    ) -> Callable[[ToolFn], ToolFn]:
        def deco(fn: ToolFn) -> ToolFn:
            self.add_tool(name, description or (fn.__doc__ or "").strip(), input_schema, fn)
            return fn

        return deco

    def tool_names(self) -> list[str]:
        return list(self._tools)

    def list_tools(self) -> list[dict[str, Any]]:
        return [{k: v for k, v in t.items() if k != "fn"} for t in self._tools.values()]

    # ---------------------------------------------------------------- tool execution
    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run a tool and return an MCP ``CallToolResult`` dict. Raises KeyError for unknown tools;
        everything else (bad arguments, tool exceptions) becomes an ``isError`` result."""
        tool = self._tools[name]
        arguments = arguments or {}
        problems = validate_arguments(tool["inputSchema"], arguments)
        if problems:
            return self._tool_error(f"Invalid arguments for tool '{name}': " + "; ".join(problems))
        try:
            out = tool["fn"](**arguments)
        except ToolError as e:
            return self._tool_error(str(e))
        except Exception as e:  # noqa: BLE001 - tool failures are results, not protocol errors
            print(f"[{self.name}] tool {name} raised:\n{traceback.format_exc()}", file=sys.stderr)
            return self._tool_error(f"{type(e).__name__}: {e}")
        if isinstance(out, str):
            return {"content": [{"type": "text", "text": out}], "isError": False}
        text = json.dumps(out, ensure_ascii=False, default=str)
        result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": False}
        if isinstance(out, dict):
            result["structuredContent"] = json.loads(text)
        return result

    @staticmethod
    def _tool_error(message: str) -> dict[str, Any]:
        return {"content": [{"type": "text", "text": message}], "isError": True}

    # ---------------------------------------------------------------- JSON-RPC dispatch
    def handle(self, message: Any) -> dict[str, Any] | None:
        """Process one JSON-RPC message; returns the response, or None for notifications/responses."""
        if not isinstance(message, dict):
            return _error(None, INVALID_REQUEST, "request must be a JSON object")
        msg_id = message.get("id")
        has_id = "id" in message
        method = message.get("method")
        if method is None and has_id and ("result" in message or "error" in message):
            return None  # a response to something we never asked; ignore
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return _error(
                msg_id if isinstance(msg_id, (str, int)) else None,
                INVALID_REQUEST,
                "invalid JSON-RPC 2.0 request (need jsonrpc='2.0' and a string method)",
            )
        if has_id and not isinstance(msg_id, (str, int)) or isinstance(msg_id, bool):
            return _error(None, INVALID_REQUEST, "request id must be a string or integer")
        params = message.get("params")
        if params is None:
            params = {}
        if not has_id:  # notification: act, never answer
            if method == "notifications/initialized":
                self.initialized = True
            return None
        if not isinstance(params, dict):
            return _error(msg_id, INVALID_PARAMS, "params must be an object")
        try:
            return self._dispatch(msg_id, method, params)
        except Exception as e:  # noqa: BLE001
            print(f"[{self.name}] internal error in {method}:\n{traceback.format_exc()}", file=sys.stderr)
            return _error(msg_id, INTERNAL_ERROR, f"internal error: {type(e).__name__}: {e}")

    def _dispatch(self, msg_id: Any, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            requested = params.get("protocolVersion")
            self.protocol_version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else LATEST_PROTOCOL_VERSION
            result: dict[str, Any] = {
                "protocolVersion": self.protocol_version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "version": self.version},
            }
            if self.instructions:
                result["instructions"] = self.instructions
            return _result(msg_id, result)
        if method == "ping":
            return _result(msg_id, {})
        if method == "tools/list":
            return _result(msg_id, {"tools": self.list_tools()})
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments")
            if not isinstance(name, str) or not name:
                return _error(msg_id, INVALID_PARAMS, "tools/call requires a string 'name'")
            if name not in self._tools:
                return _error(msg_id, INVALID_PARAMS, f"Unknown tool: {name}")
            if arguments is not None and not isinstance(arguments, dict):
                return _error(msg_id, INVALID_PARAMS, "tools/call 'arguments' must be an object")
            return _result(msg_id, self.call_tool(name, arguments))
        if method == "resources/list":
            return _result(msg_id, {"resources": []})
        if method == "resources/templates/list":
            return _result(msg_id, {"resourceTemplates": []})
        if method == "prompts/list":
            return _result(msg_id, {"prompts": []})
        return _error(msg_id, METHOD_NOT_FOUND, f"Method not found: {method}")

    # ---------------------------------------------------------------- stdio transport
    def serve_stdio(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> None:
        """Serve newline-delimited JSON-RPC until EOF. Tool calls run on worker threads so a
        slow tool never blocks ``ping`` or other requests. Only protocol messages go to stdout."""
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        write_lock = threading.Lock()
        workers: list[threading.Thread] = []

        def send(response: Any) -> None:
            if response is None:
                return
            line = json.dumps(response, ensure_ascii=False, default=str)
            with write_lock:
                try:
                    stdout.write(line + "\n")
                    stdout.flush()
                except (BrokenPipeError, ValueError):
                    pass

        def process(message: Any) -> None:
            if isinstance(message, list):  # JSON-RPC batch (2025-03-26)
                if not message:
                    send(_error(None, INVALID_REQUEST, "empty batch"))
                    return
                responses = [r for r in (self.handle(m) for m in message) if r is not None]
                send(responses or None)
            else:
                send(self.handle(message))

        def is_slow(message: Any) -> bool:
            if isinstance(message, list):
                return any(is_slow(m) for m in message)
            return isinstance(message, dict) and message.get("method") == "tools/call"

        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError as e:
                send(_error(None, PARSE_ERROR, f"Parse error: {e}"))
                continue
            if is_slow(message):
                t = threading.Thread(target=process, args=(message,), daemon=True, name=f"{self.name}-tool")
                t.start()
                workers.append(t)
                workers = [w for w in workers if w.is_alive()]
            else:
                process(message)
        for w in workers:  # EOF: let in-flight calls answer before exiting
            w.join(timeout=30)
