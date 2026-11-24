"""nanobot (HKUDS/nanobot) adapter, built on the generic command machinery.

nanobot loads skills from ``<workspace>/skills/<name>/SKILL.md`` and runs one-shot with
``nanobot agent -m "<message>"``. Each case gets its own workspace (the case workdir), so runs
are isolated from ``~/.nanobot/workspace``. Every piece is overridable because CLI flags differ
between nanobot versions:

    [agent]
    adapter = "nanobot"
    [agent.options]
    binary = "nanobot"
    args = ["agent", "--workspace", "{workdir}", "--no-markdown", "-m", "{prompt}"]   # full argv after the binary
    skills_dir = "skills"            # relative to the workdir/workspace
    config_path = ""                 # if set, appended as: --config <path>
"""

from __future__ import annotations

import re
import shutil
from typing import Any

from ..core.models import Trajectory
from ..core.registry import ADAPTERS
from ._proc import ProcResult
from .base import AgentRequest
from .command import CommandAdapter

DEFAULT_ARGS = ["agent", "--workspace", "{workdir}", "--no-markdown", "-m", "{prompt}"]
_BANNER = re.compile(r"^\s*(🐈|nanobot\b|Using config|Loading|╭|╰|│\s*$)", re.I)


@ADAPTERS.register("nanobot")
class NanobotAdapter(CommandAdapter):
    name = "nanobot"

    def __init__(
        self,
        binary: str = "nanobot",
        args: list[str] | None = None,
        skills_dir: str = "skills",
        config_path: str = "",
        config_flag: str = "--config",
        strip_banner: bool = True,
        extra_args: list[str] | None = None,
        **options: Any,
    ):
        argv = [binary, *(args if args is not None else DEFAULT_ARGS)]
        if config_path:
            argv += [config_flag, config_path]
        argv += [str(a) for a in (extra_args or [])]
        options.setdefault("skill_mode", "dir")
        options.setdefault("stdin", False)
        super().__init__(command=argv, skills_dir=skills_dir, **options)
        self.strip_banner = strip_banner

    def build_prompt(self, request: AgentRequest) -> str:
        if self.skill_mode == "inline":
            return super().build_prompt(request)
        return request.case.input

    def parse(self, proc: ProcResult, request: AgentRequest) -> Trajectory:
        if self.strip_banner and self.output == "text":
            lines = proc.stdout.splitlines()
            while lines and (not lines[0].strip() or _BANNER.match(lines[0])):
                lines.pop(0)
            proc = ProcResult(proc.returncode, "\n".join(lines), proc.stderr, proc.status, proc.duration_s, proc.error)
        return super().parse(proc, request)

    def run(self, request: AgentRequest) -> Trajectory:
        traj = super().run(request)
        prefix = f"{self.skills_dir.strip('/')}/{request.skill.name}/"
        traj.artifacts = {k: v for k, v in traj.artifacts.items() if not k.startswith(prefix)}
        return traj

    def healthcheck(self) -> tuple[bool, str]:
        b = self.binary()
        if shutil.which(b) is None:
            return False, (
                f"nanobot binary {b!r} not found on PATH - install it (pip install nanobot-ai) "
                'or set [agent.options] binary = "/path/to/nanobot"'
            )
        return True, f"found {shutil.which(b)}"
