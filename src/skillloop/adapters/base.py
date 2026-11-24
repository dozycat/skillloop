"""The single integration point for an agent system: implement :class:`AgentAdapter`.

Contract
--------
``run(request) -> Trajectory`` executes ONE case with ONE skill inside ``request.workdir``:

* make the skill available to the agent in whatever way that system supports
  (native skill dir, system prompt, AGENTS.md ...) - helpers below cover the common ways;
* honour ``request.timeout_s`` and ``request.cancel`` (kill subprocesses promptly);
* never raise for agent-side failures: return a Trajectory with ``status`` =
  ``"error" | "timeout" | "cancelled"`` and a useful ``error``;
* put the agent's final answer in ``Trajectory.output`` and whatever trace you can get in ``steps``.

Adapters must be safe to call concurrently from several threads (one call per case)."""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.config import AgentSection
from ..core.models import Case, Step, Trajectory
from ..core.registry import ADAPTERS
from ..core.skill import Skill


@dataclass
class AgentRequest:
    case: Case
    skill: Skill
    workdir: Path  # exists, empty except for case.files; the agent may write here freely
    timeout_s: float = 600.0
    cancel: threading.Event = field(default_factory=threading.Event)
    env: dict[str, str] = field(default_factory=dict)  # extra environment for subprocess agents
    mcp_config: dict[str, Any] | None = None  # {"mcpServers": {...}} to attach (e.g. mock MCP profiles)
    on_step: Callable[[Step], None] | None = None  # optional live trace callback
    # Reference directories (absolute paths). The evaluator already links each one into the workdir under
    # its own name; adapters whose agent has an access-control list should also allow the real paths.
    data_dirs: list[Path] = field(default_factory=list)


class AgentAdapter(ABC):
    name = "agent"
    cacheable = True  # False for adapters whose behaviour is not determined by their settings

    def __init__(self, **options: Any):
        self.options = options

    def cache_key(self) -> str:
        """Identity of this adapter's configuration for the run cache: two adapters with the same key
        are expected to behave the same on the same case and skill."""
        import json

        settings = {k: v for k, v in vars(self).items() if not k.startswith("_") and k != "options"}
        return json.dumps(
            [type(self).__qualname__, settings, self.options],
            sort_keys=True,
            default=lambda o: type(o).__qualname__,  # objects (clients, patterns) count by type, not address
        )

    @abstractmethod
    def run(self, request: AgentRequest) -> Trajectory: ...

    def healthcheck(self) -> tuple[bool, str]:
        """Cheap check that the agent system is reachable/installed. Shown in the console."""
        return True, "ok"

    def close(self) -> None:  # noqa: B027 - optional hook, intentionally not abstract
        """Release resources held by the adapter (optional)."""

    # ---- helpers for subclasses ----
    @staticmethod
    def install_skill(skill: Skill, skills_root: Path) -> Path:
        """Write the skill as ``<skills_root>/<skill.name>/SKILL.md`` (+ files). Returns the skill dir."""
        return skill.save(Path(skills_root) / skill.name)

    @staticmethod
    def compose_prompt(skill: Skill, case: Case) -> str:
        """Inline the skill ahead of the task, for agents with no skill mechanism at all."""
        return (
            "You have the following skill available. Follow it when it applies.\n\n"
            f"<skill>\n{skill.render_for_prompt()}</skill>\n\n<task>\n{case.input}\n</task>"
        )

    @staticmethod
    def collect_artifacts(
        workdir: Path, before: set[str] | None = None, max_files: int = 20, max_bytes: int = 100_000
    ) -> dict[str, str]:
        """Text files the agent created/changed in the workdir (relative path -> content)."""
        out: dict[str, str] = {}
        for f in sorted(Path(workdir).rglob("*")):
            rel = f.relative_to(workdir).as_posix()
            if not f.is_file() or rel.startswith(".") or "/." in rel or (before and rel in before):
                continue
            if f.stat().st_size > max_bytes:
                continue
            try:
                out[rel] = f.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if len(out) >= max_files:
                break
        return out


def build_adapter(section: AgentSection) -> AgentAdapter:
    """Instantiate the configured adapter from the ``[agent]`` config section."""
    return ADAPTERS.create(section.adapter, **section.options)
