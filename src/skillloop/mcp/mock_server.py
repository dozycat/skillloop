"""Mock MCP server: serves the canned tools of a *Mock MCP Profile* over stdio, so an agent under
evaluation talks to deterministic fakes instead of real systems.

    python -m skillloop.mcp.mock_server --profile profile.json [--log calls.jsonl] [--name crm]

Profile shape (same as ``[mcp_profiles.<name>]`` in skillloop.toml)::

    {"tools": [{
        "name": "lookup_order", "description": "...", "input_schema": {...},
        "responses": [
            {"when": {"order_id": "42"}, "result": {"status": "shipped"}},
            {"when": {"order_id": {"regex": "^9"}}, "error": "order not found"},
            {"when": {"note": {"contains": "urgent"}}, "result": "queued {{order_id}}", "delay_s": 0.1}
        ],
        "default": "no such order {{order_id}}",      # optional
        "default_error": "backend unavailable"         # optional (used when no default)
    }]}

The first response whose ``when`` matches wins (an empty/missing ``when`` always matches).
``{{arg}}`` placeholders inside string results are filled from the call arguments. Every call
is appended to the ``--log`` file as one JSON line: {ts, tool, arguments, matched_index, result|error}.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .protocol import ToolError, ToolServer

_PLACEHOLDER = re.compile(r"\{\{\s*([\w.-]+)\s*\}\}")
_MISSING = object()


def _as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def matches(when: Any, arguments: dict[str, Any]) -> bool:
    """True when every condition in ``when`` holds for ``arguments``."""
    if not when:
        return True
    if not isinstance(when, dict):
        return False
    for key, cond in when.items():
        actual = arguments.get(key, _MISSING)
        if actual is _MISSING:
            return False
        if isinstance(cond, dict) and "regex" in cond and len(cond) == 1:
            try:
                if not re.search(str(cond["regex"]), _as_text(actual)):
                    return False
            except re.error:
                return False
        elif isinstance(cond, dict) and "contains" in cond and len(cond) == 1:
            if str(cond["contains"]) not in _as_text(actual):
                return False
        elif actual != cond and not (
            not isinstance(cond, (dict, list))
            and not isinstance(actual, (dict, list, bool))
            and not isinstance(cond, bool)
            and str(actual) == str(cond)
        ):
            return False
    return True


def fill(value: Any, arguments: dict[str, Any]) -> Any:
    """Recursively substitute ``{{arg}}`` placeholders in strings; unknown names are left as-is."""
    if isinstance(value, str):
        return _PLACEHOLDER.sub(
            lambda m: _as_text(arguments[m.group(1)]) if m.group(1) in arguments else m.group(0), value
        )
    if isinstance(value, list):
        return [fill(v, arguments) for v in value]
    if isinstance(value, dict):
        return {k: fill(v, arguments) for k, v in value.items()}
    return value


class CallLog:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        if self.path is None:
            return
        with self._lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def resolve(tool: dict[str, Any], arguments: dict[str, Any]) -> tuple[int, Any, str, float]:
    """Pick the response for a call: (matched_index, result, error, delay_s).
    matched_index is the response position, -1 for the default, -2 when nothing applies."""
    for i, resp in enumerate(tool.get("responses") or []):
        if isinstance(resp, dict) and matches(resp.get("when"), arguments):
            delay = float(resp.get("delay_s") or 0)
            if resp.get("error"):
                return i, None, str(fill(str(resp["error"]), arguments)), delay
            return i, fill(resp.get("result", ""), arguments), "", delay
    if "default" in tool:
        return -1, fill(tool["default"], arguments), "", 0.0
    if tool.get("default_error"):
        return -1, None, str(fill(str(tool["default_error"]), arguments)), 0.0
    return (
        -2,
        None,
        (
            f"mock tool '{tool.get('name')}' has no response configured for arguments "
            f"{json.dumps(arguments, ensure_ascii=False, default=str)} (add a matching "
            f"'responses' entry or a 'default' to the mock MCP profile)"
        ),
        0.0,
    )


def build_server(profile: dict[str, Any], name: str = "mock", log_path: str | Path | None = None) -> ToolServer:
    server = ToolServer(name, "0.1.0", instructions=str(profile.get("instructions") or ""))
    log = CallLog(log_path)

    def make(tool: dict[str, Any]):
        def fn(**arguments: Any) -> Any:
            index, result, error, delay = resolve(tool, arguments)
            if delay > 0:
                time.sleep(delay)
            record: dict[str, Any] = {
                "ts": time.time(),
                "tool": tool["name"],
                "arguments": arguments,
                "matched_index": index,
            }
            record["error" if error else "result"] = error or result
            log.write(record)
            if error:
                raise ToolError(error)
            return result

        return fn

    for tool in profile.get("tools") or []:
        server.add_tool(
            str(tool["name"]),
            str(tool.get("description") or f"Mock tool {tool['name']}"),
            tool.get("input_schema") or tool.get("inputSchema") or {"type": "object"},
            make(tool),
        )
    return server


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="skillloop.mcp.mock_server", description=__doc__.split("\n\n")[0])
    ap.add_argument("--profile", required=True, help="path to a mock MCP profile JSON file")
    ap.add_argument("--log", default="", help="append every tool call to this JSONL file")
    ap.add_argument("--name", default="mock", help="server name reported to the client")
    args = ap.parse_args(argv)
    try:
        profile = json.loads(Path(args.profile).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"cannot read profile {args.profile}: {e}", file=sys.stderr)
        return 2
    from .profiles import validate_profile

    problems = validate_profile(profile)
    if problems:
        print("invalid mock MCP profile: " + "; ".join(problems), file=sys.stderr)
        return 2
    build_server(profile, args.name, args.log or None).serve_stdio()
    return 0


if __name__ == "__main__":
    sys.exit(main())
