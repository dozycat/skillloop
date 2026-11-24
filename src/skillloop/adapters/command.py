"""Generic template adapter: wrap ANY command-line agent without writing Python.

    [agent]
    adapter = "command"
    [agent.options]
    command = ["my-agent", "--workspace", "{workdir}", "--message", "{prompt}"]
    skill_mode = "dir"            # "inline" | "dir" | "file"
    skills_dir = ".agent/skills"  # for skill_mode = "dir" (relative to workdir)
    output = "text"               # "text" | "json" | "last_line"

Placeholders: {prompt} {prompt_file} {skill_dir} {skill_file} {skill_name} {workdir} {mcp_config_file}
{case_id}. If no placeholder mentions the prompt, it is sent on stdin."""

from __future__ import annotations

import json
import re
import shlex
import shutil
from pathlib import Path
from typing import Any

from ..core.models import Step, Trajectory
from ..core.registry import ADAPTERS
from ..core.skill import SKILL_FILE
from ._proc import ProcResult, run_process, truncate
from .base import AgentAdapter, AgentRequest

_PLACEHOLDER = re.compile(r"\{(prompt|prompt_file|skill_dir|skill_file|skill_name|workdir|mcp_config_file|case_id)\}")


def dig(data: Any, path: str) -> Any:
    """Follow a dotted path (``result.answer``, ``choices.0.text``) through dicts/lists; None if absent."""
    cur = data
    for part in [p for p in path.split(".") if p]:
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def parse_json_output(stdout: str) -> Any:
    """Whole-stdout JSON, else the last line that parses as JSON."""
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip()
        if line.startswith(("{", "[")):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return None


@ADAPTERS.register("command")
class CommandAdapter(AgentAdapter):
    name = "command"

    def __init__(
        self,
        command: list[str] | str = "",
        skill_mode: str = "inline",
        skills_dir: str = "skills",
        skill_file: str = "AGENTS.md",
        output: str = "text",
        answer_path: str = "output",
        steps_path: str = "",
        usage_path: str = "",
        answer_regex: str = "",
        prompt_template: str = "",
        stdin: bool | None = None,
        env: dict[str, str] | None = None,
        ok_exit_codes: list[int] | None = None,
        strip_ansi: bool = True,
        **options: Any,
    ):
        super().__init__(**options)
        self.command = shlex.split(command) if isinstance(command, str) else [str(c) for c in command]
        if skill_mode not in ("inline", "dir", "file"):
            raise ValueError("skill_mode must be 'inline', 'dir' or 'file'")
        if output not in ("text", "json", "last_line"):
            raise ValueError("output must be 'text', 'json' or 'last_line'")
        self.skill_mode, self.skills_dir, self.skill_file = skill_mode, skills_dir, skill_file
        self.output, self.answer_path, self.steps_path, self.usage_path = output, answer_path, steps_path, usage_path
        self.answer_regex = re.compile(answer_regex, re.S) if answer_regex else None
        self.prompt_template = prompt_template  # may use {input} and {skill}
        self.stdin = stdin
        self.env = dict(env or {})
        self.ok_exit_codes = set(ok_exit_codes or [0])
        self.strip_ansi = strip_ansi

    # ---- overridable pieces (subclasses for specific CLIs reuse the machinery) ----
    def deliver_skill(self, request: AgentRequest) -> tuple[Path, Path]:
        """Make the skill visible to the agent. Returns (skill_dir, skill_file)."""
        wd = request.workdir
        if self.skill_mode == "dir":
            d = self.install_skill(request.skill, wd / self.skills_dir)
            return d, d / SKILL_FILE
        if self.skill_mode == "file":
            f = wd / self.skill_file
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(request.skill.render_for_prompt(), encoding="utf-8")
            return f.parent, f
        d = request.skill.save(wd / ".skillloop_skill" / request.skill.name)  # inline: still on disk for {skill_file}
        return d, d / SKILL_FILE

    def build_prompt(self, request: AgentRequest) -> str:
        if self.prompt_template:
            return self.prompt_template.replace("{input}", request.case.input).replace(
                "{skill}", request.skill.render_for_prompt()
            )
        if self.skill_mode == "inline":
            return self.compose_prompt(request.skill, request.case)
        return request.case.input

    def build_command(self, request: AgentRequest, values: dict[str, str]) -> list[str]:
        return [_PLACEHOLDER.sub(lambda m: values[m.group(1)], part) for part in self.command]

    def parse(self, proc: ProcResult, request: AgentRequest) -> Trajectory:
        out = proc.stdout
        if self.strip_ansi:
            out = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out)
        traj = Trajectory(raw=truncate(out))
        answer: Any = out.strip()
        if self.output == "last_line":
            lines = [ln for ln in out.strip().splitlines() if ln.strip()]
            answer = lines[-1].strip() if lines else ""
        elif self.output == "json":
            data = parse_json_output(out)
            if data is None:
                traj.status, traj.error = "error", "agent output is not JSON"
                answer = out.strip()
            else:
                answer = dig(data, self.answer_path) if self.answer_path else data
                for s in (dig(data, self.steps_path) or []) if self.steps_path else []:
                    if isinstance(s, dict):
                        traj.steps.append(
                            Step(
                                kind=str(s.get("kind") or s.get("type") or "message"),
                                content=str(s.get("content") or s.get("text") or ""),
                                name=str(s.get("name") or ""),
                            )
                        )
                    else:
                        traj.steps.append(Step("message", str(s)))
                usage = dig(data, self.usage_path) if self.usage_path else None
                if isinstance(usage, dict):
                    traj.usage = {k: v for k, v in usage.items() if isinstance(v, (int, float))}
                if answer is None:
                    traj.status, traj.error = "error", f"no {self.answer_path!r} field in agent JSON output"
                    answer = ""
        if not isinstance(answer, str):
            answer = json.dumps(answer, ensure_ascii=False)
        if self.answer_regex is not None:
            m = self.answer_regex.search(answer)
            if m:
                answer = (m.group(1) if m.groups() else m.group(0)).strip()
        traj.output = answer
        if not traj.steps and answer:
            traj.steps.append(Step("message", answer))
        return traj

    # ---- AgentAdapter ----
    def run(self, request: AgentRequest) -> Trajectory:
        try:
            if not self.command:
                return Trajectory(
                    case_id=request.case.id, status="error", error="command adapter: no 'command' configured"
                )
            wd = Path(request.workdir)
            wd.mkdir(parents=True, exist_ok=True)
            skill_dir, skill_file = self.deliver_skill(request)
            before = {f.relative_to(wd).as_posix() for f in wd.rglob("*") if f.is_file()}
            prompt = self.build_prompt(request)
            meta = wd / ".skillloop"
            meta.mkdir(exist_ok=True)
            prompt_file = meta / "prompt.txt"
            prompt_file.write_text(prompt, encoding="utf-8")
            mcp_file = meta / "mcp.json"
            mcp_file.write_text(json.dumps(request.mcp_config or {"mcpServers": {}}), encoding="utf-8")
            values = {
                "prompt": prompt,
                "prompt_file": str(prompt_file),
                "skill_dir": str(skill_dir),
                "skill_file": str(skill_file),
                "skill_name": request.skill.name,
                "workdir": str(wd),
                "mcp_config_file": str(mcp_file),
                "case_id": request.case.id,
            }
            cmd = self.build_command(request, values)
            mentions_prompt = any("{prompt}" in p or "{prompt_file}" in p for p in self.command)
            use_stdin = self.stdin if self.stdin is not None else not mentions_prompt
            steps: list[Step] = []
            on_line = self._line_callback(request, steps)
            proc = run_process(
                cmd,
                cwd=wd,
                env={**self.env, **request.env},
                stdin=prompt if use_stdin else None,
                timeout_s=request.timeout_s,
                cancel=request.cancel,
                on_line=on_line,
            )
            if proc.status == "error" and proc.returncode in self.ok_exit_codes:
                proc.status, proc.error = "ok", ""
            if proc.status in ("timeout", "cancelled") or proc.returncode == 127:
                traj = Trajectory(raw=truncate(proc.stdout), status=proc.status, error=proc.error)
                if proc.stdout.strip():  # keep whatever trace we got before the kill
                    try:
                        partial = self.parse(proc, request)
                        traj.steps, traj.output, traj.usage = partial.steps, partial.output, partial.usage
                    except Exception:
                        pass
            else:
                traj = self.parse(proc, request)
                if proc.status == "error" and traj.status == "ok":
                    traj.status, traj.error = "error", proc.error
                elif traj.status == "error" and proc.stderr.strip():
                    traj.error += f" (stderr: {proc.stderr.strip()[-300:]})"
            traj.case_id = request.case.id
            traj.duration_s = proc.duration_s
            traj.artifacts = self.collect_artifacts(wd, before)
            return traj
        except Exception as e:  # noqa: BLE001 - adapters never raise
            return Trajectory(case_id=request.case.id, status="error", error=f"{type(e).__name__}: {e}")

    def _line_callback(self, request: AgentRequest, steps: list[Step]):
        return None

    def binary(self) -> str:
        return self.command[0] if self.command else ""

    def healthcheck(self) -> tuple[bool, str]:
        b = self.binary()
        if not b:
            return False, "no command configured"
        path = shutil.which(b)
        if path is None and not Path(b).exists():
            return False, f"binary {b!r} not found on PATH"
        return True, f"found {path or b}"
