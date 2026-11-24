"""Mock MCP Profiles: turn ``[mcp_profiles.<name>]`` config into a runnable mock MCP server
config (``{"mcpServers": {...}}``) that adapters attach to the agent under evaluation."""

from __future__ import annotations

import json
import re
import sys
from typing import TYPE_CHECKING, Any

from ..core.sandbox import safe_name

if TYPE_CHECKING:
    from ..core.project import Project

_TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")


def validate_profile(profile: Any) -> list[str]:
    """Return a list of problems with a mock MCP profile (empty == valid)."""
    if not isinstance(profile, dict):
        return ["profile must be a table/object with a 'tools' list"]
    tools = profile.get("tools")
    if not isinstance(tools, list) or not tools:
        return ["profile needs a non-empty 'tools' list"]
    problems: list[str] = []
    seen: set[str] = set()
    for i, tool in enumerate(tools):
        where = f"tools[{i}]"
        if not isinstance(tool, dict):
            problems.append(f"{where} must be a table/object")
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not _TOOL_NAME.match(name):
            problems.append(f"{where}.name must be 1-128 chars of letters, digits, '_', '-', '.'")
        else:
            where = f"tool '{name}'"
            if name in seen:
                problems.append(f"{where} is defined more than once")
            seen.add(name)
        schema = tool.get("input_schema", tool.get("inputSchema"))
        if schema is not None and (not isinstance(schema, dict) or schema.get("type", "object") != "object"):
            problems.append(f"{where}: input_schema must be a JSON schema of type 'object'")
        responses = tool.get("responses", [])
        if not isinstance(responses, list):
            problems.append(f"{where}: 'responses' must be a list")
            continue
        for j, resp in enumerate(responses):
            rw = f"{where} responses[{j}]"
            if not isinstance(resp, dict):
                problems.append(f"{rw} must be a table/object")
                continue
            if "result" not in resp and not resp.get("error"):
                problems.append(f"{rw} needs a 'result' or an 'error'")
            when = resp.get("when")
            if when is not None and not isinstance(when, dict):
                problems.append(f"{rw}.when must be a table/object of argument conditions")
            elif when:
                for arg, cond in when.items():
                    if isinstance(cond, dict) and set(cond) == {"regex"}:
                        try:
                            re.compile(str(cond["regex"]))
                        except re.error as e:
                            problems.append(f"{rw}.when.{arg}: invalid regex ({e})")
            delay = resp.get("delay_s", 0)
            if not isinstance(delay, (int, float)) or isinstance(delay, bool) or delay < 0:
                problems.append(f"{rw}.delay_s must be a non-negative number")
    return problems


def _get_profile(project: Project, profile_name: str) -> dict[str, Any]:
    profiles = project.config.mcp_profiles or {}
    if profile_name not in profiles:
        raise ValueError(
            f"unknown mock MCP profile {profile_name!r}; defined profiles: {', '.join(sorted(profiles)) or '(none)'}"
        )
    return profiles[profile_name]


def _log_path(project: Project, profile_name: str):
    return project.state.mkdir("mcp_logs") / f"{safe_name(profile_name)}.jsonl"


def build_mcp_config(project: Project, profile_name: str) -> dict[str, Any]:
    """Validate the named profile, persist it under the project state dir and return an
    ``{"mcpServers": {name: {command, args}}}`` config launching the mock server for it."""
    profile = _get_profile(project, profile_name)
    problems = validate_profile(profile)
    if problems:
        raise ValueError(f"invalid mock MCP profile {profile_name!r}: " + "; ".join(problems))
    path = project.state.mkdir("mcp_profiles") / f"{safe_name(profile_name)}.json"
    path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "mcpServers": {
            profile_name: {
                "command": sys.executable,
                "args": [
                    "-m",
                    "skillloop.mcp.mock_server",
                    "--profile",
                    str(path),
                    "--log",
                    str(_log_path(project, profile_name)),
                    "--name",
                    profile_name,
                ],
            }
        }
    }


def list_profiles(project: Project) -> list[dict[str, Any]]:
    out = []
    for name, profile in sorted((project.config.mcp_profiles or {}).items()):
        tools = profile.get("tools") if isinstance(profile, dict) else None
        out.append(
            {
                "name": name,
                "tools": [str(t.get("name", "")) for t in tools or [] if isinstance(t, dict)],
                "problems": validate_profile(profile),
                "active": project.config.agent.mcp_profile == name,
            }
        )
    return out


def read_call_log(project: Project, profile_name: str, limit: int = 200) -> list[dict[str, Any]]:
    """Most recent ``limit`` tool calls recorded by the mock server for this profile (oldest first)."""
    path = _log_path(project, profile_name)
    if not path.exists():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:] if limit > 0 else []:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def clear_call_log(project: Project, profile_name: str) -> None:
    _log_path(project, profile_name).unlink(missing_ok=True)
