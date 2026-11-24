"""Project configuration (``skillloop.toml``). Every section has safe defaults, so an empty
file is a valid config that runs fully offline against the mock agent/LLM."""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import Serializable

CONFIG_FILE = "skillloop.toml"
REPLACE_KEYS = (
    ("agent", "options"),
    ("llm", "options"),
    ("llm", "roles"),
    ("eval", "judge_options"),
    ("eval", "judges"),
    ("mcp_profiles", ""),
)


@dataclass
class ProjectSection(Serializable):
    name: str = "skillloop-project"
    requirement: str = "requirement.md"  # 需求文档
    seed_skill: str = "seed_skill"  # 初版 Skill (dir or SKILL.md); may not exist -> bootstrap
    dataset: str = "dataset.jsonl"  # 测试集 / 标准答案
    skill_name: str = ""  # name for a bootstrapped skill; default derives from project name
    # project-local modules imported at startup so their @ADAPTERS/@JUDGES/@LLMS.register decorators run
    plugins: list[str] = field(default_factory=list)


@dataclass
class AgentSection(Serializable):
    """The agent system under test. ``adapter`` is a registry name or ``"pkg.module:Class"``."""

    adapter: str = "mock"
    options: dict[str, Any] = field(default_factory=dict)
    mcp_profile: str = ""  # name under [mcp_profiles] to attach as mock MCP servers


@dataclass
class LLMSection(Serializable):
    """The LLM used by the framework itself (judge, analyst, merger, builder, devset...)."""

    provider: str = "mock"  # "anthropic" | "openai" | "cli" | "mock" | "pkg.module:Class"
    model: str = ""
    options: dict[str, Any] = field(default_factory=dict)
    roles: dict[str, dict[str, Any]] = field(default_factory=dict)  # role -> {provider, model, options}


@dataclass
class EvalSection(Serializable):
    judge: str = "exact"  # registry name; "composite" combines several
    judge_options: dict[str, Any] = field(default_factory=dict)
    judges: dict[str, dict[str, Any]] = field(default_factory=dict)  # options for per-case judge overrides, by name
    trials: int = 1  # repeats per case (variance control)
    concurrency: int = 4
    timeout_s: float = 600.0
    pass_threshold: float = 0.7  # score >= threshold counts as pass for graded judges
    keep_workdirs: bool = False


@dataclass
class EvolveSection(Serializable):
    max_iterations: int = 8
    candidates_per_iteration: int = 2
    patience: int = 3  # stop after this many iterations without promotion
    target_score: float = 1.0  # stop once the best val score reaches this
    frontier_size: int = 3
    train_ratio: float = 0.5
    val_ratio: float = 0.3  # remainder is the held-out test split
    split_seed: int = 7
    analyst_batch_size: int = 4  # failures per analyst call
    merge_batch_size: int = 6  # patches per merge call (hierarchical)
    max_failures_per_iteration: int = 24
    use_success_analysis: bool = True
    # promotion gate
    metric: str = "mean_score"  # "mean_score" | "pass_rate"
    min_delta: float = 0.01  # required val improvement over the current best
    max_regressions: int = 1  # cases passing under best that may fail under the candidate
    protect_tags: list[str] = field(default_factory=list)  # cases with these tags may never regress
    require_human_approval: bool = False
    confirm_on_test: bool = False  # also require no drop on the held-out test split
    max_skill_chars: int = 24000
    # reject candidates that copy gold answers / inputs into the skill. Turn off only for skills whose job
    # is to carry reference knowledge the answers are made of (glossaries, lookup tables).
    leak_check: bool = True
    max_growth_ratio: float = 2.5  # candidate may not be more than this x the parent size


@dataclass
class DevsetSection(Serializable):
    target_size: int = 24
    candidates_per_rule: int = 3
    evolve_rounds: int = 1  # 场景演化 rounds
    max_per_rule: int = 6  # 平衡: cap per rule
    difficulty_mix: dict[str, float] = field(default_factory=lambda: {"easy": 0.3, "medium": 0.4, "hard": 0.3})
    dedupe_threshold: float = 0.85


@dataclass
class Config(Serializable):
    project: ProjectSection = field(default_factory=ProjectSection)
    agent: AgentSection = field(default_factory=AgentSection)
    llm: LLMSection = field(default_factory=LLMSection)
    eval: EvalSection = field(default_factory=EvalSection)
    evolve: EvolveSection = field(default_factory=EvolveSection)
    devset: DevsetSection = field(default_factory=DevsetSection)
    # name -> {"tools": [{name, description, input_schema, responses: [{when: {...}, result: ...}], default: ...}]}
    mcp_profiles: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> Config:
        p = Path(path)
        if p.is_dir():
            p = p / CONFIG_FILE
        if not p.exists():
            return cls()
        with p.open("rb") as f:
            return cls.from_dict(tomllib.load(f))

    def save(self, path: str | Path) -> Path:
        p = Path(path)
        if p.is_dir():
            p = p / CONFIG_FILE
        p.write_text(dump_toml(self.to_dict()), encoding="utf-8")
        return p

    def merged(self, overrides: dict[str, Any]) -> Config:
        """Return a copy with a nested dict of overrides applied (used by the web console / MCP)."""
        data = _deep_merge(self.to_dict(), overrides)
        for section, key in REPLACE_KEYS:  # free-form tables: a merge could never delete a key
            src = overrides.get(section) if key else overrides
            if isinstance(src, dict) and (key or section) in src:
                if key:
                    data[section][key] = src[key]
                else:
                    data[section] = src[section]
        return Config.from_dict(data)

    def validate(self) -> list[str]:
        e, problems = self.evolve, []
        if not 0 < e.train_ratio < 1 or not 0 <= e.val_ratio < 1 or e.train_ratio + e.val_ratio > 1:
            problems.append("evolve.train_ratio / val_ratio must be fractions summing to <= 1")
        if e.metric not in ("mean_score", "pass_rate"):
            problems.append("evolve.metric must be 'mean_score' or 'pass_rate'")
        if self.eval.trials < 1 or self.eval.concurrency < 1:
            problems.append("eval.trials and eval.concurrency must be >= 1")
        return problems


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


# --------------------------------------------------------------------------- minimal TOML writer


def _toml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        esc = v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
        return f'"{esc}"'
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_scalar(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_key(k)} = {_toml_scalar(x)}" for k, x in v.items() if x is not None) + " }"
    if v is None:
        return '""'
    return _toml_scalar(str(v))


def _toml_key(k: str) -> str:
    import re

    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else _toml_scalar(str(k))


def dump_toml(data: dict[str, Any], _prefix: str = "") -> str:
    """Serialise nested dicts of scalars/lists to TOML (sufficient for our config shape)."""
    lines, tables = [], []
    for k, v in data.items():
        if isinstance(v, dict):
            tables.append((k, v))
        elif v is not None:
            lines.append(f"{_toml_key(k)} = {_toml_scalar(v)}")
    out = "\n".join(lines)
    for k, v in tables:
        if not v:
            continue  # empty tables carry no information; defaults restore them on load
        name = f"{_prefix}{_toml_key(k)}"
        body = dump_toml(v, name + ".")
        # skip emitting a header for tables that only contain sub-tables
        has_scalars = any(not isinstance(x, dict) for x in v.values())
        header = f"[{name}]\n" if has_scalars else ""
        out += ("\n\n" if out else "") + header + body
    return out.strip() + ("\n" if not _prefix else "")


def default_config_dict() -> dict[str, Any]:
    return dataclasses.asdict(Config())
