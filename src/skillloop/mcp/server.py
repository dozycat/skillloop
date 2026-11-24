"""MCP 服务: expose the evolution loop to any MCP-capable agent (Claude Code, Codex, Cursor, nanobot...).

    skillloop mcp --workspace /path/to/workspace        # stdio transport

Tools mirror the service facade. Long operations (`evolve`, `evaluate`, `build_devset`) return a
job id immediately; the calling agent polls `job_status` and may `cancel_job`. Every tool takes an
optional ``project`` (omit it when the workspace holds a single project)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import __version__
from ..core.models import error_message
from ..service import Workspace
from .protocol import ToolError, ToolServer

INSTRUCTIONS = """skillloop evolves agent skills (SKILL.md) in a closed loop: evaluate on a dataset, judge, record
failures in an error book, propose patched candidates, compare versions, and promote the best one
through a quality gate.

Typical flow: list_projects → (create_project) → project_status → evolve → poll job_status until
status is succeeded/failed/cancelled → get_metrics / list_versions / get_skill (the best SKILL.md).
Long tools return {"job_id": ...} immediately; poll job_status with `after_seq` to stream new events."""

P = {"type": "string", "description": "Project name; optional when the workspace has exactly one project."}


def _schema(props: dict[str, Any] | None = None, required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": {"project": P, **(props or {})}, "required": required or []}


def _trim_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "seq": e["seq"],
            "level": e["level"],
            "type": e["type"],
            "message": e["message"],
            **({"data": e["data"]} if e["type"] in ("progress", "audit") else {}),
        }
        for e in events
    ]


def build_server(workspace: Workspace) -> ToolServer:
    server = ToolServer("skillloop", __version__, INSTRUCTIONS)

    def svc(project: str = ""):
        try:
            return workspace.get(project)
        except KeyError as e:
            raise ToolError(error_message(e)) from e

    def guarded(fn):
        def wrapper(**kw: Any) -> Any:
            try:
                return fn(**kw)
            except ToolError:
                raise
            except (KeyError, FileNotFoundError) as e:
                raise ToolError(f"not found: {error_message(e)}") from e
            except (ValueError, RuntimeError) as e:
                raise ToolError(str(e)) from e

        return wrapper

    def tool(name: str, description: str, schema: dict[str, Any]):
        def deco(fn):
            server.add_tool(name, description, schema, guarded(fn))
            return fn

        return deco

    # ------------------------------------------------------------ projects
    @tool(
        "list_projects",
        "List projects in the workspace with their best version and scores.",
        {"type": "object", "properties": {}},
    )
    def list_projects() -> dict[str, Any]:
        return {"workspace": str(workspace.root), "projects": workspace.list()}

    @tool(
        "create_project",
        "Create a project from its three inputs: a requirement document, an optional seed SKILL.md, and "
        "optional evaluation cases. `config` deep-merges into the defaults (sections: agent, llm, eval, evolve, devset).",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Letters, digits, . _ - (max 64 chars)."},
                "requirement": {
                    "type": "string",
                    "description": "需求文档: goals, scope, acceptance criteria, constraints.",
                },
                "skill_md": {
                    "type": "string",
                    "description": "初版 SKILL.md content (frontmatter + body). Omit to bootstrap one.",
                },
                "cases": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "Cases: {input, expected?, rubric?, tags?, id?, meta?}.",
                },
                "config": {"type": "object"},
            },
            "required": ["name"],
        },
    )
    def create_project(
        name: str, requirement: str = "", skill_md: str = "", cases: list | None = None, config: dict | None = None
    ) -> dict[str, Any]:
        return workspace.create(name, requirement=requirement, skill_md=skill_md, cases=cases, config=config).info()

    @tool(
        "project_status",
        "Project overview: inputs present, dataset splits, best version and scores, active jobs, "
        "pending reviews/approvals, error-book counts, and agent/LLM health.",
        _schema(),
    )
    def project_status(project: str = "") -> dict[str, Any]:
        s = svc(project)
        return {**s.info(), "health": s.health()}

    @tool("get_config", "Read the project's configuration (skillloop.toml).", _schema())
    def get_config(project: str = "") -> dict[str, Any]:
        return svc(project).config()

    @tool(
        "update_config",
        "Deep-merge overrides into the project config, e.g. "
        '{"agent": {"adapter": "claude_code"}, "evolve": {"max_iterations": 5}}.',
        _schema({"overrides": {"type": "object"}}, ["overrides"]),
    )
    def update_config(overrides: dict, project: str = "") -> dict[str, Any]:
        return svc(project).update_config(overrides)

    @tool(
        "add_cases",
        "Append evaluation cases to the dataset.",
        _schema({"cases": {"type": "array", "items": {"type": "object"}}}, ["cases"]),
    )
    def add_cases(cases: list, project: str = "") -> dict[str, Any]:
        s = svc(project)
        return {"added": s.add_cases(cases), "n_cases": len(s.project.load_cases())}

    # ------------------------------------------------------------ long operations
    @tool(
        "evolve",
        "Start the skill evolution loop as a background job. Returns {job_id}.",
        _schema({"max_iterations": {"type": "integer", "minimum": 0}}),
    )
    def evolve(project: str = "", max_iterations: int | None = None) -> dict[str, Any]:
        return {"job_id": svc(project).submit("evolve", max_iterations=max_iterations)}

    @tool(
        "evaluate",
        "Evaluate a skill version (default: best) on a split or on specific cases, as a background job.",
        _schema(
            {
                "version": {"type": "string", "description": '"v003", "best" or "latest".'},
                "split": {"type": "string", "enum": ["", "train", "val", "test"]},
                "case_ids": {"type": "array", "items": {"type": "string"}},
            }
        ),
    )
    def evaluate(project: str = "", version: str = "", split: str = "", case_ids: list | None = None) -> dict[str, Any]:
        return {"job_id": svc(project).submit("evaluate", version=version, split=split, case_ids=case_ids)}

    @tool(
        "build_devset",
        "Generate an evaluation set from the requirement document (rule extraction → candidates → "
        "scenario evolution → filtering → balancing → report). `merge` appends the result to the dataset.",
        _schema({"merge": {"type": "boolean"}}),
    )
    def build_devset(project: str = "", merge: bool = False) -> dict[str, Any]:
        return {"job_id": svc(project).submit("devset", merge=merge)}

    @tool(
        "job_status",
        "Status, progress and new events of a job. Pass the last seen `seq` as after_seq to get only newer events.",
        _schema({"job_id": {"type": "string"}, "after_seq": {"type": "integer"}}, ["job_id"]),
    )
    def job_status(job_id: str, project: str = "", after_seq: int = 0) -> dict[str, Any]:
        s = svc(project)
        job = s.job(job_id)
        if job is None:
            raise ToolError(f"unknown job {job_id!r}")
        page = s.events(job_id, after_seq, 200)
        last_seq = page[-1]["seq"] if page else after_seq  # advance past progress events we do not return
        events = [e for e in _trim_events(page) if e["type"] != "progress"][-60:]
        return {
            "job_id": job_id,
            "kind": job["kind"],
            "status": job["status"],
            "progress": job["progress"],
            "error": job["error"],
            "result": job["result"],
            "events": events,
            "last_seq": last_seq,
        }

    @tool("list_jobs", "Recent jobs of the project.", _schema({"limit": {"type": "integer"}}))
    def list_jobs(project: str = "", limit: int = 20) -> list[dict[str, Any]]:
        return [
            {k: j[k] for k in ("id", "kind", "status", "progress", "error", "created_at")}
            for j in svc(project).jobs.list(limit)
        ]

    @tool(
        "cancel_job",
        "Request cancellation of a running job (it stops at the next safe point).",
        _schema({"job_id": {"type": "string"}}, ["job_id"]),
    )
    def cancel_job(job_id: str, project: str = "") -> dict[str, Any]:
        return {"cancel_requested": svc(project).cancel(job_id)}

    # ------------------------------------------------------------ versions
    @tool("list_versions", "All skill versions with parent, status, scores per split and change summary.", _schema())
    def list_versions(project: str = "") -> list[dict[str, Any]]:
        return [
            {k: v[k] for k in ("id", "parent", "origin", "status", "summary", "iteration")}
            | {
                "scores": {
                    sp: {m: s.get(m) for m in ("mean_score", "pass_rate", "n_cases")} for sp, s in v["scores"].items()
                }
            }
            for v in svc(project).versions()
        ]

    @tool(
        "get_skill",
        "The SKILL.md (and supporting files) of a version; default is the best version.",
        _schema({"version": {"type": "string"}}),
    )
    def get_skill(project: str = "", version: str = "") -> dict[str, Any]:
        v = svc(project).version(version)
        return {k: v[k] for k in ("id", "status", "summary", "lineage", "scores", "gate", "skill_md", "files")}

    @tool(
        "diff_versions",
        "Unified diff of the skill between two versions.",
        _schema({"base": {"type": "string"}, "cand": {"type": "string"}}, ["base", "cand"]),
    )
    def diff_versions(base: str, cand: str, project: str = "") -> str:
        return svc(project).diff(base, cand) or "(no differences)"

    @tool(
        "compare_versions",
        "Paired per-case comparison of two versions on a split: score delta with CI, "
        "fixed / regressed cases, per-tag breakdown.",
        _schema(
            {
                "base": {"type": "string"},
                "cand": {"type": "string"},
                "split": {"type": "string", "enum": ["train", "val", "test"]},
            },
            ["base", "cand"],
        ),
    )
    def compare_versions(base: str, cand: str, project: str = "", split: str = "val") -> dict[str, Any]:
        d = svc(project).compare(base, cand, split).to_dict()
        d.pop("case_deltas", None)
        return d

    @tool(
        "import_skill",
        "Register a hand-written or edited SKILL.md as a new version (child of the current best).",
        _schema({"skill_md": {"type": "string"}, "summary": {"type": "string"}}, ["skill_md"]),
    )
    def import_skill(skill_md: str, project: str = "", summary: str = "manual edit") -> dict[str, Any]:
        return svc(project).import_skill(skill_md, summary).to_dict()

    @tool(
        "promote_version",
        "Promote a version to best by hand (also approves a pending_approval candidate).",
        _schema({"version": {"type": "string"}, "reason": {"type": "string"}}, ["version"]),
    )
    def promote_version(version: str, project: str = "", reason: str = "manual promotion") -> dict[str, Any]:
        return svc(project).promote(version, reason, by="mcp").to_dict()

    @tool(
        "export_skill",
        "Write the best skill bundle to a directory inside the project (e.g. to hand it to an agent).",
        _schema({"dest": {"type": "string", "description": "Project-relative directory."}}, ["dest"]),
    )
    def export_skill(dest: str, project: str = "") -> dict[str, Any]:
        s = svc(project)
        return {"path": str(s.export_best(s.project.path(dest)))}

    # ------------------------------------------------------------ evidence
    @tool("list_evals", "Stored evaluations, newest first.", _schema())
    def list_evals(project: str = "") -> list[dict[str, Any]]:
        return svc(project).evals()

    @tool(
        "get_eval",
        "Per-case results of one evaluation. Set include_trajectories for full agent traces.",
        _schema(
            {
                "eval_id": {"type": "string"},
                "include_trajectories": {"type": "boolean"},
                "only_failures": {"type": "boolean"},
            },
            ["eval_id"],
        ),
    )
    def get_eval(
        eval_id: str, project: str = "", include_trajectories: bool = False, only_failures: bool = False
    ) -> dict[str, Any]:
        d = svc(project).eval(eval_id, with_trajectories=include_trajectories)
        if only_failures:
            d["results"] = [r for r in d["results"] if not r["judgment"]["passed"]]
        return {k: d[k] for k in ("summary", "by_tag", "results")}

    @tool(
        "get_errorbook",
        "错题库: failed cases with context, root cause, lesson and status, plus root-cause statistics.",
        _schema(
            {
                "status": {"type": "string", "enum": ["", "open", "fixed", "regressed", "wontfix"]},
                "limit": {"type": "integer"},
            }
        ),
    )
    def get_errorbook(project: str = "", status: str = "", limit: int = 30) -> dict[str, Any]:
        s = svc(project)
        return {"stats": s.error_stats(), "entries": s.errors(status)[:limit]}

    @tool(
        "list_reviews",
        "Results waiting for (or having) a human verdict.",
        _schema({"status": {"type": "string", "enum": ["", "pending", "done"]}}),
    )
    def list_reviews(project: str = "", status: str = "pending") -> list[dict[str, Any]]:
        return svc(project).reviews(status)

    @tool(
        "submit_review",
        "Record a human verdict for one result; it overrides the automatic judgment.",
        _schema(
            {
                "eval_id": {"type": "string"},
                "case_id": {"type": "string"},
                "trial": {"type": "integer"},
                "passed": {"type": "boolean"},
                "score": {"type": "number"},
                "comment": {"type": "string"},
                "reviewer": {"type": "string"},
            },
            ["eval_id", "case_id", "passed"],
        ),
    )
    def submit_review(
        eval_id: str,
        case_id: str,
        passed: bool,
        project: str = "",
        trial: int = 0,
        score: float | None = None,
        comment: str = "",
        reviewer: str = "",
    ) -> dict[str, Any]:
        return {
            "ok": svc(project).submit_review(
                eval_id, case_id, trial, passed=passed, score=score, comment=comment, reviewer=reviewer
            )
        }

    @tool(
        "get_metrics",
        "指标摘要: best version, per-split scores, per-iteration trend, lineage, error-book stats, usage totals.",
        _schema(),
    )
    def get_metrics(project: str = "") -> dict[str, Any]:
        return svc(project).metrics()

    @tool("get_history", "Per-iteration record of the evolution loop: parent, candidates, gate outcomes.", _schema())
    def get_history(project: str = "") -> list[dict[str, Any]]:
        return svc(project).history()

    @tool(
        "write_outputs",
        "Regenerate the output bundle (best skill, review sheet, error book, comparison report, "
        "metrics summary, HTML report) under <project>/outputs and return the paths.",
        _schema(),
    )
    def write_outputs(project: str = "") -> dict[str, str]:
        return svc(project).write_outputs()

    @tool(
        "list_artifacts",
        "产物索引: indexed files (skills, evals, reports, datasets).",
        _schema({"kind": {"type": "string"}, "version": {"type": "string"}}),
    )
    def list_artifacts(project: str = "", kind: str = "", version: str = "") -> list[dict[str, Any]]:
        return [
            {k: a[k] for k in ("kind", "path", "version", "title", "created_at")}
            for a in svc(project).artifacts(kind, version)
        ]

    @tool(
        "read_artifact",
        "Read a text file of the project by relative path (sandboxed to the project directory).",
        _schema({"path": {"type": "string"}, "max_chars": {"type": "integer"}}, ["path"]),
    )
    def read_artifact(path: str, project: str = "", max_chars: int = 40000) -> str:
        return svc(project).read_artifact(path, max_chars)

    return server


def serve(workspace_root: str | Path = ".") -> None:
    workspace = Workspace(workspace_root)
    try:
        build_server(workspace).serve_stdio()
    finally:
        workspace.close()
