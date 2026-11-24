"""Claude Code (headless ``claude -p``) adapter.

The skill is installed natively at ``<workdir>/.claude/skills/<name>/`` and the run uses
``--output-format stream-json`` so thoughts, tool calls, tool results, cost and usage all land
in the trajectory.

    [agent]
    adapter = "claude_code"
    [agent.options]
    model = "sonnet"                 # optional; alias or full model id
    permission_mode = "acceptEdits"  # acceptEdits | auto | bypassPermissions | manual | dontAsk | plan
    allowed_tools = ["Read", "Write", "Bash(python *)"]
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..core.models import Step, Trajectory
from ..core.registry import ADAPTERS
from ..core.skill import SKILL_FILE
from ._proc import ProcResult, truncate
from .base import AgentRequest
from .command import CommandAdapter

_CLIP = 4000


def _clip(text: Any, n: int = _CLIP) -> str:
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, default=str)
    return text if len(text) <= n else text[:n] + f"... <{len(text) - n} chars elided>"


def _tool_result_text(content: Any) -> str:
    if isinstance(content, list):
        return "\n".join(str(b.get("text", "")) if isinstance(b, dict) else str(b) for b in content)
    return "" if content is None else str(content)


def parse_stream_json(stdout: str) -> Trajectory:
    """Turn Claude Code stream-json lines into a Trajectory (tolerant of unknown event types)."""
    traj = Trajectory()
    result_seen = False
    last_text = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind = ev.get("type")
        if kind == "system" and ev.get("subtype") == "init":
            traj.steps.append(
                Step(
                    "system",
                    f"session started (model {ev.get('model', '?')})",
                    data={
                        "skills": ev.get("skills") or [],
                        "mcp_servers": ev.get("mcp_servers") or [],
                        "session_id": ev.get("session_id", ""),
                    },
                )
            )
        elif kind == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                bt = block.get("type")
                if bt == "text" and block.get("text"):
                    last_text = block["text"]
                    traj.steps.append(Step("message", _clip(block["text"])))
                elif bt == "thinking" and block.get("thinking"):
                    traj.steps.append(Step("thought", _clip(block["thinking"])))
                elif bt == "tool_use":
                    traj.steps.append(
                        Step(
                            "tool_call",
                            _clip(block.get("input") or {}),
                            name=str(block.get("name", "")),
                            data={"id": block.get("id", "")},
                        )
                    )
        elif kind == "user":
            content = (ev.get("message") or {}).get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    traj.steps.append(
                        Step(
                            "error" if block.get("is_error") else "tool_result",
                            _clip(_tool_result_text(block.get("content"))),
                            data={"tool_use_id": block.get("tool_use_id", "")},
                        )
                    )
        elif kind == "result":
            result_seen = True
            traj.output = str(ev.get("result") or last_text or "")
            usage = ev.get("usage") or {}
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                if isinstance(usage.get(k), (int, float)):
                    traj.usage[k] = usage[k]
            if isinstance(ev.get("total_cost_usd"), (int, float)):
                traj.usage["cost_usd"] = ev["total_cost_usd"]
            if isinstance(ev.get("num_turns"), (int, float)):
                traj.usage["turns"] = ev["num_turns"]
            if ev.get("is_error") or str(ev.get("subtype", "success")) != "success":
                traj.status = "error"
                traj.error = f"claude result {ev.get('subtype', 'error')}: {_clip(ev.get('result') or '', 500)}".strip(
                    ": "
                )
    if not result_seen:
        traj.output = last_text
        traj.status, traj.error = "error", "no result event in claude output"
    return traj


@ADAPTERS.register("claude_code")
class ClaudeCodeAdapter(CommandAdapter):
    name = "claude_code"

    def __init__(
        self,
        binary: str = "claude",
        model: str = "",
        permission_mode: str = "acceptEdits",
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        tools: list[str] | None = None,
        max_turns: int = 0,
        max_budget_usd: float = 0.0,
        effort: str = "",
        append_system_prompt: str = "",
        strict_mcp_config: bool = True,
        setting_sources: str = "",
        bare: bool = False,
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        **options: Any,
    ):
        super().__init__(command=[binary], skill_mode="dir", skills_dir=".claude/skills", env=env, **options)
        self.model, self.permission_mode = model, permission_mode
        self.allowed_tools, self.disallowed_tools, self.tools = allowed_tools or [], disallowed_tools or [], tools
        self.max_turns, self.max_budget_usd, self.effort = max_turns, max_budget_usd, effort
        self.append_system_prompt = append_system_prompt
        self.strict_mcp_config, self.setting_sources, self.bare = strict_mcp_config, setting_sources, bare
        self.extra_args = [str(a) for a in (extra_args or [])]

    def build_prompt(self, request: AgentRequest) -> str:
        return request.case.input

    def build_command(self, request: AgentRequest, values: dict[str, str]) -> list[str]:
        cmd = [self.command[0], "-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence"]
        if self.permission_mode:
            cmd += ["--permission-mode", self.permission_mode]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort:
            cmd += ["--effort", self.effort]
        if self.allowed_tools:
            cmd += ["--allowed-tools", ",".join(self.allowed_tools)]
        if self.disallowed_tools:
            cmd += ["--disallowed-tools", ",".join(self.disallowed_tools)]
        if self.tools is not None:
            cmd += ["--tools", ",".join(self.tools)]
        if self.max_turns:
            cmd += ["--max-turns", str(self.max_turns)]
        if self.max_budget_usd:
            cmd += ["--max-budget-usd", str(self.max_budget_usd)]
        if self.append_system_prompt:
            cmd += ["--append-system-prompt", self.append_system_prompt]
        if self.setting_sources:
            cmd += ["--setting-sources", self.setting_sources]
        if self.bare:
            cmd.append("--bare")
        for d in request.data_dirs:  # the links in the workdir resolve outside it; allow the real locations
            cmd += ["--add-dir", str(d)]
        if request.mcp_config and request.mcp_config.get("mcpServers"):
            cmd += ["--mcp-config", values["mcp_config_file"]]
        if self.strict_mcp_config:
            cmd.append("--strict-mcp-config")
        return cmd + self.extra_args

    def parse(self, proc: ProcResult, request: AgentRequest) -> Trajectory:
        traj = parse_stream_json(proc.stdout)
        traj.raw = truncate(proc.stdout)
        if traj.status == "error" and proc.stderr.strip() and "no result event" in traj.error:
            traj.error += f" (stderr: {proc.stderr.strip()[-300:]})"
        return traj

    def run(self, request: AgentRequest) -> Trajectory:
        traj = super().run(request)
        # the skill copy is an input, not something the agent produced
        prefix = f".claude/skills/{request.skill.name}/"
        traj.artifacts = {k: v for k, v in traj.artifacts.items() if not k.startswith(prefix)}
        return traj

    def skill_path(self, workdir: Path, name: str) -> Path:
        return Path(workdir) / ".claude" / "skills" / name / SKILL_FILE
