"""OpenAI Codex CLI (``codex exec``) adapter.

The skill is delivered as ``AGENTS.md`` in the workdir (Codex reads it automatically) plus a copy
of the full skill directory under ``.codex/skills/<name>/`` for supporting files. Events come
from ``--json`` (JSONL); the final answer from ``--output-last-message``.

    [agent]
    adapter = "codex"
    [agent.options]
    model = "gpt-5-codex"      # optional
    sandbox = "workspace-write"  # read-only | workspace-write | danger-full-access
"""

from __future__ import annotations

import json
from typing import Any

from ..core.models import Step, Trajectory
from ..core.registry import ADAPTERS
from ._proc import ProcResult, truncate
from .base import AgentRequest
from .claude_code import _clip
from .command import CommandAdapter

_LAST = ".skillloop/last_message.txt"


def _toml(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False)  # JSON strings/arrays/inline scalars are valid TOML values here


def parse_codex_jsonl(stdout: str) -> Trajectory:
    """Parse ``codex exec --json`` events (tolerant: unknown items become generic steps)."""
    traj = Trajectory()
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        et = str(ev.get("type", ""))
        item = ev.get("item") if isinstance(ev.get("item"), dict) else None
        if item is not None and et == "item.completed":
            it = str(item.get("type") or item.get("item_type") or "")
            if it in ("agent_message", "assistant_message"):
                traj.output = str(item.get("text", ""))
                traj.steps.append(Step("message", _clip(traj.output)))
            elif it == "reasoning":
                traj.steps.append(Step("thought", _clip(item.get("text", ""))))
            elif it == "command_execution":
                traj.steps.append(Step("tool_call", _clip(item.get("command", "")), name="shell"))
                traj.steps.append(
                    Step(
                        "tool_result",
                        _clip(item.get("aggregated_output", "")),
                        name="shell",
                        data={"exit_code": item.get("exit_code")},
                    )
                )
            elif it == "mcp_tool_call":
                name = f"{item.get('server', '')}.{item.get('tool', '')}".strip(".")
                traj.steps.append(Step("tool_call", _clip(item.get("arguments") or {}), name=name))
                if item.get("result") is not None or item.get("error") is not None:
                    traj.steps.append(
                        Step(
                            "error" if item.get("error") else "tool_result",
                            _clip(item.get("error") or item.get("result")),
                            name=name,
                        )
                    )
            elif it == "file_change":
                traj.steps.append(Step("tool_call", _clip(item.get("changes") or []), name="file_change"))
            elif it == "error":
                traj.steps.append(Step("error", _clip(item.get("message", ""))))
            else:
                traj.steps.append(Step("system", _clip({k: v for k, v in item.items() if k != "id"}), name=it))
        elif et == "turn.completed":
            usage = ev.get("usage") or {}
            for k, v in usage.items():
                if isinstance(v, (int, float)):
                    traj.usage[k] = traj.usage.get(k, 0) + v
            traj.usage["turns"] = traj.usage.get("turns", 0) + 1
        elif et in ("error", "turn.failed"):
            msg = (
                ev.get("message") or (ev.get("error") or {}).get("message")
                if isinstance(ev.get("error"), dict)
                else ev.get("message")
            )
            traj.status, traj.error = "error", _clip(msg or json.dumps(ev, ensure_ascii=False), 800)
            traj.steps.append(Step("error", traj.error))
    return traj


@ADAPTERS.register("codex")
class CodexAdapter(CommandAdapter):
    name = "codex"

    def __init__(
        self,
        binary: str = "codex",
        model: str = "",
        sandbox: str = "workspace-write",
        profile: str = "",
        config: dict[str, Any] | None = None,
        ephemeral: bool = True,
        ignore_user_config: bool = False,
        bypass_approvals_and_sandbox: bool = False,
        extra_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        **options: Any,
    ):
        super().__init__(command=[binary], skill_mode="file", skill_file="AGENTS.md", env=env, **options)
        self.model, self.sandbox, self.profile = model, sandbox, profile
        self.config = dict(config or {})  # extra `-c key=value` overrides
        self.ephemeral, self.ignore_user_config = ephemeral, ignore_user_config
        self.bypass = bypass_approvals_and_sandbox
        self.extra_args = [str(a) for a in (extra_args or [])]

    def deliver_skill(self, request: AgentRequest):
        skill = request.skill
        d = self.install_skill(skill, request.workdir / ".codex" / "skills")
        f = request.workdir / "AGENTS.md"
        note = f"\n\nSupporting files of this skill are in `.codex/skills/{skill.name}/`.\n" if skill.files else ""
        f.write_text(
            "# Agent instructions\n\nYou have the following skill. Follow it whenever it applies.\n\n"
            + skill.render_for_prompt()
            + note,
            encoding="utf-8",
        )
        return d, f

    def build_prompt(self, request: AgentRequest) -> str:
        return request.case.input

    def build_command(self, request: AgentRequest, values: dict[str, str]) -> list[str]:
        cmd = [
            self.command[0],
            "exec",
            "--json",
            "--skip-git-repo-check",
            "--color",
            "never",
            "-C",
            values["workdir"],
            "-o",
            str(request.workdir / _LAST),
        ]
        if self.bypass:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        elif self.sandbox:
            cmd += ["-s", self.sandbox]
        if self.model:
            cmd += ["-m", self.model]
        if self.profile:
            cmd += ["-p", self.profile]
        if self.ephemeral:
            cmd.append("--ephemeral")
        if self.ignore_user_config:
            cmd.append("--ignore-user-config")
        for k, v in self.config.items():
            cmd += ["-c", f"{k}={_toml(v)}"]
        for name, spec in ((request.mcp_config or {}).get("mcpServers") or {}).items():
            for key in ("command", "args", "env", "url"):
                if spec.get(key) not in (None, "", [], {}):
                    val = spec[key]
                    if key == "env":
                        val = "{" + ", ".join(f"{_toml(k)} = {_toml(str(x))}" for k, x in val.items()) + "}"
                        cmd += ["-c", f"mcp_servers.{name}.env={val}"]
                    else:
                        cmd += ["-c", f"mcp_servers.{name}.{key}={_toml(val)}"]
        return cmd + self.extra_args + ["-"]  # "-" => read the prompt from stdin

    def parse(self, proc: ProcResult, request: AgentRequest) -> Trajectory:
        traj = parse_codex_jsonl(proc.stdout)
        traj.raw = truncate(proc.stdout)
        last = request.workdir / _LAST
        if last.is_file():
            text = last.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                traj.output = text
        if traj.status == "ok" and not traj.output and not traj.steps:
            traj.status = "error"
            traj.error = "no agent message in codex output" + (
                f" (stderr: {proc.stderr.strip()[-300:]})" if proc.stderr.strip() else ""
            )
        return traj

    def run(self, request: AgentRequest) -> Trajectory:
        traj = super().run(request)
        traj.artifacts = {k: v for k, v in traj.artifacts.items() if k != "AGENTS.md"}
        return traj
