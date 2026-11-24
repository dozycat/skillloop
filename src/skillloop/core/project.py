"""项目管理: a project is a directory holding inputs (需求文档 / 初版 Skill / 测试集), the config,
and a private ``.skillloop/`` state dir. Everything the framework writes stays inside the project."""

from __future__ import annotations

import json
from functools import cached_property
from pathlib import Path
from typing import Any

from .config import CONFIG_FILE, Config
from .dataset import load_cases, save_cases
from .models import Case, EvalReport
from .sandbox import Sandbox, ensure_within, safe_name
from .skill import SKILL_FILE, Skill
from .store import Store
from .versions import VersionStore

STATE_DIR = ".skillloop"


class Project:
    def __init__(self, root: str | Path, config: Config | None = None):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"project directory not found: {self.root}")
        self.config = config or Config.load(self.root)
        self.sandbox = Sandbox(self.root)
        self.state = Sandbox(self.root / STATE_DIR)

    # ---------------------------------------------------------------- lifecycle
    @classmethod
    def open(cls, root: str | Path = ".") -> Project:
        return cls(root)

    @classmethod
    def init(
        cls,
        root: str | Path,
        name: str = "",
        *,
        config: Config | None = None,
        requirement: str = "",
        seed_skill: Skill | None = None,
        cases: list[Case] | None = None,
        overwrite: bool = False,
    ) -> Project:
        """Create (or complete) a project directory with config and input stubs."""
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        cfg = config or Config()
        if name:
            cfg.project.name = name
        elif cfg.project.name == "skillloop-project":
            cfg.project.name = root.resolve().name
        if overwrite or not (root / CONFIG_FILE).exists():
            cfg.save(root)
        project = cls(root, cfg if overwrite or config else None)
        req = project.path(project.config.project.requirement)
        if requirement and (overwrite or not req.exists()):
            req.write_text(requirement, encoding="utf-8")
        if seed_skill is not None:
            seed_skill.save(project.path(project.config.project.seed_skill))
        if cases is not None:
            project.save_cases(cases)
        gi = root / ".gitignore"
        if not gi.exists():
            gi.write_text(f"{STATE_DIR}/\n", encoding="utf-8")
        return project

    def load_plugins(self) -> list[str]:
        """Make the project directory importable and import the modules listed in ``[project] plugins``.

        This is how a project ships its own adapter/judge next to ``skillloop.toml`` without packaging it.
        Raises ImportError naming the plugin when one cannot be imported."""
        import importlib
        import sys

        root = str(self.root)
        if root not in sys.path:
            sys.path.insert(0, root)
        loaded = []
        for name in self.config.project.plugins:
            try:
                importlib.import_module(name)
            except Exception as e:
                raise ImportError(f"cannot import plugin {name!r} listed in [project] plugins: {e}") from e
            loaded.append(name)
        return loaded

    def reload_config(self) -> Config:
        self.config = Config.load(self.root)
        return self.config

    def save_config(self, config: Config | None = None) -> None:
        if config is not None:
            self.config = config
        self.config.save(self.root)

    @property
    def name(self) -> str:
        return self.config.project.name

    # ---------------------------------------------------------------- paths (all sandboxed)
    def path(self, rel: str | Path) -> Path:
        """Resolve a project-relative (or absolute-but-inside) path; escapes raise SandboxError."""
        return ensure_within(self.root, rel)

    @cached_property
    def store(self) -> Store:
        return Store(self.state.path("state.db"))

    @cached_property
    def versions(self) -> VersionStore:
        return VersionStore(self.state.mkdir("versions"))

    @property
    def logs_dir(self) -> Path:
        return self.state.mkdir("logs")

    @property
    def outputs_dir(self) -> Path:
        d = self.root / "outputs"
        d.mkdir(exist_ok=True)
        return d

    @property
    def errorbook_path(self) -> Path:
        return self.state.mkdir("errorbook") / "errors.jsonl"

    @property
    def devset_dir(self) -> Path:
        return self.state.mkdir("devset")

    def run_dir(self, run_id: str) -> Path:
        """Scratch area for one evaluation run; agent workdirs live below it."""
        return self.state.mkdir("runs", safe_name(run_id))

    # ---------------------------------------------------------------- inputs
    def load_requirement(self) -> str:
        p = self.path(self.config.project.requirement)
        return p.read_text(encoding="utf-8") if p.is_file() else ""

    def load_seed_skill(self) -> Skill | None:
        p = self.path(self.config.project.seed_skill)
        if p.is_file() or (p / SKILL_FILE).is_file():
            return Skill.load(p)
        return None

    def load_task(self) -> str:
        """The fixed task framing from ``[agent] task_file`` ("" when not configured)."""
        rel = self.config.agent.task_file
        if not rel:
            return ""
        p = self.path(rel)
        if not p.is_file():
            raise FileNotFoundError(f"[agent] task_file not found: {rel}")
        return p.read_text(encoding="utf-8").strip()

    def data_dirs(self) -> list[Path]:
        """Resolved ``[agent] data_dirs``; each must be an existing directory inside the project."""
        out = []
        for rel in self.config.agent.data_dirs:
            p = self.path(rel)
            if not p.is_dir():
                raise FileNotFoundError(f"[agent] data_dirs entry is not a directory: {rel}")
            out.append(p)
        names = [p.name for p in out]
        if len(set(names)) != len(names):
            raise ValueError(f"[agent] data_dirs must have distinct directory names, got {names}")
        return out

    def load_cases(self) -> list[Case]:
        return load_cases(self.path(self.config.project.dataset), self.config.dataset)

    def save_cases(self, cases: list[Case]) -> Path:
        return save_cases(cases, self.path(self.config.project.dataset))

    # ---------------------------------------------------------------- eval reports
    def save_eval(self, report: EvalReport) -> Path:
        d = self.state.mkdir("evals")
        p = d / f"{safe_name(report.id)}.json"
        p.write_text(json.dumps(report.to_dict(), ensure_ascii=False), encoding="utf-8")
        self.store.index_artifact(
            "eval",
            str(p.relative_to(self.root)),
            version=report.skill_version,
            title=f"{report.label or report.split or 'eval'} {report.skill_version}",
            meta=report.summary(),
        )
        return p

    def load_eval(self, eval_id: str) -> EvalReport:
        p = self.state.path("evals", f"{safe_name(eval_id)}.json")
        if not p.exists():
            raise KeyError(f"unknown eval {eval_id!r}")
        return EvalReport.from_dict(json.loads(p.read_text(encoding="utf-8")))

    def list_evals(self) -> list[dict[str, Any]]:
        """Summaries of stored evaluations, newest first (from the artifact index)."""
        return [a["meta"] for a in self.store.artifacts(kind="eval")]

    def info(self) -> dict[str, Any]:
        best = self.versions.best()
        return {
            "name": self.name,
            "root": str(self.root),
            "has_requirement": bool(self.load_requirement().strip()),
            "has_seed_skill": self.load_seed_skill() is not None,
            "n_cases": len(self.load_cases()),
            "n_versions": len(self.versions.ids()),
            "best_version": best.id if best else "",
            "best_scores": best.scores if best else {},
            "agent": self.config.agent.adapter,
            "llm": self.config.llm.provider,
            "judge": self.config.eval.judge,
        }

    def close(self) -> None:
        if "store" in self.__dict__:
            self.store.close()
