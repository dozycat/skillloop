"""共享核心的门面: one object per project exposing everything the entry layers need.

The CLI, the web console, the MCP server and SDK users all go through :class:`SkillLoop`, so they
cannot drift apart. Long operations exist in two forms: synchronous methods (``evolve()``,
``evaluate()``...) and ``submit(kind, **params)`` which runs the same thing as a cancellable job.
:class:`Workspace` manages several isolated projects under one root directory."""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .adapters.base import AgentAdapter, build_adapter
from .core.config import CONFIG_FILE, Config
from .core.dataset import case_from_record, split_cases
from .core.jobs import JobContext, JobManager, NullContext
from .core.models import Case, EvalReport, SkillVersion
from .core.project import Project
from .core.sandbox import safe_join
from .core.skill import Skill
from .eval.runner import Evaluator
from .evolve.compare import Comparison, compare_reports
from .evolve.errorbook import ErrorBook
from .evolve.loop import EvolutionLoop, EvolveResult, load_history
from .llm.base import LLMClient, build_llm

JOB_KINDS = ("evolve", "evaluate", "devset", "bootstrap", "outputs")


class SkillLoop:
    """Facade over one project.

    >>> es = SkillLoop("my_project")
    >>> result = es.evolve()              # blocking
    >>> job_id = es.submit("evolve")      # or as a background job
    """

    def __init__(
        self,
        root: str | Path = ".",
        *,
        adapter: AgentAdapter | None = None,
        llm: LLMClient | None = None,
        echo: Callable[[dict[str, Any]], None] | None = None,
        max_jobs: int = 2,
    ):
        self.project = Project.open(root)
        self.project.load_plugins()
        self._adapter = adapter  # explicit instances win over config (SDK embedding)
        self._llm = llm
        self.jobs = JobManager(self.project.store, self.project.logs_dir, max_workers=max_jobs, echo=echo)
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- components
    def llm(self) -> LLMClient:
        return self._llm or build_llm(self.project.config.llm)

    def adapter(self) -> AgentAdapter:
        return self._adapter or build_adapter(self.project.config.agent)

    def evaluator(self) -> Evaluator:
        return Evaluator.from_project(self.project, llm=self.llm(), adapter=self.adapter())

    def errorbook(self) -> ErrorBook:
        return ErrorBook(self.project.errorbook_path)

    def _ctx(self, ctx: JobContext | None) -> JobContext:
        return ctx or NullContext(self.project.store)

    def splits(self) -> dict[str, list[Case]]:
        e = self.project.config.evolve
        s = split_cases(self.project.load_cases(), e.train_ratio, e.val_ratio, e.split_seed)
        if not s["val"]:
            s["val"] = list(s["train"])
        return s

    def _resolve(self, version: str = "") -> SkillVersion:
        vs = self.project.versions
        if version in ("", "best"):
            v = vs.best()
            if v is None:
                ids = vs.ids()
                if not ids:
                    raise KeyError("the project has no skill versions yet - run bootstrap or evolve first")
                v = vs.get(ids[-1])
            return v
        if version == "latest":
            return vs.get(vs.ids()[-1])
        return vs.get(version)

    # ---------------------------------------------------------------- long operations (sync)
    def evolve(self, max_iterations: int | None = None, ctx: JobContext | None = None) -> EvolveResult:
        """Run the full evolution loop (baseline → iterate → promote → held-out test → outputs)."""
        loop = EvolutionLoop(self.project, llm=self.llm(), adapter=self._adapter, ctx=self._ctx(ctx))
        return loop.run(max_iterations)

    def bootstrap(self, ctx: JobContext | None = None) -> SkillVersion:
        """生成初版 Skill: register the seed skill, or draft one from the requirement document."""
        loop = EvolutionLoop(self.project, llm=self.llm(), adapter=self._adapter, ctx=self._ctx(ctx))
        loop.cases = self.project.load_cases()
        loop.splits = self.splits()
        return loop.ensure_baseline()

    def evaluate(
        self, version: str = "", split: str = "", case_ids: list[str] | None = None, ctx: JobContext | None = None
    ) -> EvalReport:
        """Evaluate a version on a split ("train" | "val" | "test" | "" = every case) or on chosen cases."""
        ctx = self._ctx(ctx)
        if not self.project.versions.ids():
            self.bootstrap(ctx)  # a fresh project: register the seed (or generated) skill as v001 first
        v = self._resolve(version)
        if case_ids:
            wanted = set(case_ids)
            cases, label = [c for c in self.project.load_cases() if c.id in wanted], "adhoc"
            missing = wanted - {c.id for c in cases}
            if missing:
                raise KeyError(f"unknown case id(s): {', '.join(sorted(missing))}")
        elif split:
            cases, label = self.splits().get(split, []), split
        else:
            cases, label = self.project.load_cases(), "all"
        if not cases:
            raise ValueError(f"no cases to evaluate (split={split or 'all'!r})")
        report = self.evaluator().evaluate(
            self.project.versions.load_skill(v.id),
            cases,
            version=v.id,
            split=split if not case_ids else "",
            label=label,
            ctx=ctx,
        )
        self.project.save_eval(report)
        self._after_eval(report)
        if not report.cancelled and not case_ids:
            self.project.versions.record_score(v.id, split or "all", report.summary())
        if not case_ids and split == "train" and not report.cancelled:
            self.errorbook().record_eval(report, cases)
        return report

    def _after_eval(self, report: EvalReport) -> None:
        if any(r.judgment.needs_review for r in report.results):
            from .eval.review import enqueue_reviews

            enqueue_reviews(self.project, report)

    def build_devset(self, merge: bool = False, ctx: JobContext | None = None) -> dict[str, Any]:
        """开发集成管道: 规则抽取 → 候选生成 → 场景演化 → 过滤 → 平衡 → 报告."""
        from .devset.pipeline import DevsetPipeline

        result = DevsetPipeline(self.project, self.llm()).run(self._ctx(ctx), merge=merge)
        return {
            "n_rules": len(result.rules),
            "n_cases": len(result.cases),
            "n_dropped": len(result.dropped),
            "merged": merge,
            "paths": {k: str(v) for k, v in result.paths.items()},
            "report_md": result.report_md,
        }

    def write_outputs(self, ctx: JobContext | None = None) -> dict[str, str]:
        from .report import write_outputs

        return write_outputs(self.project, ctx)

    # ---------------------------------------------------------------- jobs (async)
    def submit(self, kind: str, **params: Any) -> str:
        """Start a background job. Kinds: evolve(max_iterations), evaluate(version, split, case_ids),
        devset(merge), bootstrap(), outputs()."""
        if kind not in JOB_KINDS:
            raise ValueError(f"unknown job kind {kind!r}; expected one of {', '.join(JOB_KINDS)}")
        active = [j for j in self.jobs.list(limit=50) if j["status"] in ("queued", "running", "cancelling")]
        if kind in ("evolve", "bootstrap") and any(j["kind"] in ("evolve", "bootstrap") for j in active):
            raise RuntimeError("an evolve/bootstrap job is already active for this project; cancel it or wait")

        def run(ctx: JobContext) -> Any:
            if kind == "evolve":
                return self.evolve(params.get("max_iterations"), ctx).to_dict()
            if kind == "evaluate":
                return self.evaluate(
                    params.get("version", ""), params.get("split", ""), params.get("case_ids"), ctx
                ).summary()
            if kind == "devset":
                return self.build_devset(bool(params.get("merge")), ctx)
            if kind == "bootstrap":
                return self.bootstrap(ctx).to_dict()
            return self.write_outputs(ctx)

        return self.jobs.submit(kind, run, {k: v for k, v in params.items() if v is not None})

    def run_job(self, kind: str, **params: Any) -> dict[str, Any]:
        """Like ``submit`` but blocks until done and returns the final job record (used by the CLI)."""
        job_id = self.submit(kind, **params)
        try:
            return self.jobs.wait(job_id)  # type: ignore[return-value]
        except KeyboardInterrupt:
            self.jobs.cancel(job_id)
            return self.jobs.wait(job_id)  # type: ignore[return-value]

    def job(self, job_id: str) -> dict[str, Any] | None:
        return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> bool:
        return self.jobs.cancel(job_id)

    def events(self, job_id: str | None = None, after: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        return self.project.store.events(job_id, after, limit)

    # ---------------------------------------------------------------- versions
    def versions(self) -> list[dict[str, Any]]:
        return [v.to_dict() for v in self.project.versions.list()]

    def version(self, version: str = "") -> dict[str, Any]:
        v = self._resolve(version)
        skill = self.project.versions.load_skill(v.id)
        return {
            **v.to_dict(),
            "skill_md": skill.to_markdown(),
            "files": skill.files,
            "lineage": [x.id for x in self.project.versions.lineage(v.id)],
        }

    def diff(self, a: str, b: str) -> str:
        return self.project.versions.diff(self._resolve(a).id, self._resolve(b).id)

    def import_skill(self, source: str | Path | Skill, summary: str = "manual edit", parent: str = "") -> SkillVersion:
        """Register a hand-written/edited skill (a Skill, a ``Path``, or SKILL.md text) as a new version."""
        if isinstance(source, Skill):
            skill = source
        elif isinstance(source, Path):  # only an explicit Path is read from disk; strings are always SKILL.md text,
            skill = Skill.load(source)  # so remote callers (MCP / web) can never make this read a file
        else:
            skill = Skill.from_markdown(str(source))
        problems = skill.validate(self.project.config.evolve.max_skill_chars)
        if problems:
            raise ValueError("invalid skill: " + "; ".join(problems))
        vs = self.project.versions
        if not parent and vs.best_id():
            parent = vs.best_id()
        v = vs.create(skill, parent=parent, origin="manual", summary=summary, status="accepted")
        self.project.store.add_event("", "info", "audit", "version.create", {"version": v.id, "origin": "manual"})
        if not vs.best_id():
            vs.set_best(v.id, "first version")
        return v

    def compare(self, base: str, cand: str, split: str = "val", ctx: JobContext | None = None) -> Comparison:
        """版本比较 on a split, evaluating either side first when it has no stored result."""
        cfg = self.project.config.evolve
        reports = []
        for ref in (base, cand):
            v = self._resolve(ref)
            eval_id = (v.scores.get(split) or {}).get("eval_id")
            try:
                reports.append(self.project.load_eval(eval_id) if eval_id else self.evaluate(v.id, split, ctx=ctx))
            except KeyError:
                reports.append(self.evaluate(v.id, split, ctx=ctx))
        from .eval.review import apply_reviews

        reports = [apply_reviews(self.project, r) for r in reports]
        return compare_reports(reports[0], reports[1], metric=cfg.metric, protect_tags=cfg.protect_tags)

    def promote(self, version: str, reason: str = "manual promotion", by: str = "") -> SkillVersion:
        """Promote a version to best by hand (also how a human approves a ``pending_approval`` candidate)."""
        vs = self.project.versions
        v = self._resolve(version)
        if v.gate:
            v.gate.update({"promoted": True, "needs_approval": False, "approved_by": by or "manual"})
            v.gate.setdefault("reasons", []).append(f"manually promoted: {reason}")
            vs.update(v)
        out = vs.set_best(v.id, reason)
        self.project.store.add_event(
            "", "info", "audit", "version.promote", {"version": v.id, "reason": reason, "by": by or "manual"}
        )
        return out

    def reject(self, version: str, reason: str = "") -> SkillVersion:
        vs = self.project.versions
        v = self._resolve(version)
        if v.id == vs.best_id():
            raise ValueError("cannot reject the current best version; promote another one first")
        v.status = "rejected"
        v.gate.setdefault("reasons", []).append(f"manually rejected: {reason}".rstrip(": "))
        self.project.store.add_event("", "info", "audit", "version.reject", {"version": v.id, "reason": reason})
        return vs.update(v, "rejected")

    def export_best(self, dest: str | Path) -> Path:
        """Copy the best skill bundle to ``dest`` (e.g. an agent's skills directory)."""
        return self.project.versions.export(self._resolve("best").id, dest)

    # ---------------------------------------------------------------- dataset
    def cases(self, split: str = "") -> list[dict[str, Any]]:
        splits = self.splits()
        where = {c.id: name for name, cs in splits.items() for c in cs}
        out = []
        for c in self.project.load_cases():
            if split and where.get(c.id) != split:
                continue
            out.append({**c.to_dict(), "split": where.get(c.id, "")})
        return out

    def add_cases(self, records: list[dict[str, Any]]) -> int:
        """Append cases (loose records accepted, see ``case_from_record``); ids are kept unique."""
        with self._lock:
            cases = self.project.load_cases()
            ids = {c.id for c in cases}
            added = 0
            for i, rec in enumerate(records):
                c = case_from_record(rec, i)
                base, n = c.id, 1
                while c.id in ids:
                    n += 1
                    c.id = f"{base}_{n}"
                ids.add(c.id)
                cases.append(c)
                added += 1
            self.project.save_cases(cases)
            return added

    # ---------------------------------------------------------------- error book / evals / reviews
    def errors(self, status: str = "") -> list[dict[str, Any]]:
        return [e.to_dict() for e in self.errorbook().entries(status)]

    def error_stats(self) -> dict[str, Any]:
        return self.errorbook().stats()

    def annotate_error(self, case_id: str, **fields: str) -> bool:
        return self.errorbook().annotate(case_id, **fields)

    def evals(self) -> list[dict[str, Any]]:
        return self.project.list_evals()

    def eval(self, eval_id: str, with_trajectories: bool = True) -> dict[str, Any]:
        from .eval.review import apply_reviews

        report = apply_reviews(self.project, self.project.load_eval(eval_id))
        data = report.to_dict()
        data["summary"] = report.summary()
        data["by_tag"] = report.by_tag()
        if not with_trajectories:
            for r in data["results"]:
                r["trajectory"] = {k: r["trajectory"][k] for k in ("output", "status", "error", "duration_s")}
        return data

    def reviews(self, status: str = "", eval_id: str = "") -> list[dict[str, Any]]:
        return self.project.store.reviews(eval_id=eval_id, status=status)

    def submit_review(
        self,
        eval_id: str,
        case_id: str,
        trial: int = 0,
        *,
        passed: bool,
        score: float | None = None,
        comment: str = "",
        reviewer: str = "",
    ) -> bool:
        """Record a human verdict; it overrides the automatic judgment wherever that evaluation is used."""
        s = self.project.store
        if not s.reviews(eval_id=eval_id) or not any(
            r["case_id"] == case_id and r["trial"] == trial for r in s.reviews(eval_id=eval_id)
        ):
            s.upsert_review(eval_id, case_id, trial)
        ok = s.submit_review(
            eval_id,
            case_id,
            trial,
            score=(1.0 if passed else 0.0) if score is None else score,
            passed=passed,
            comment=comment,
            reviewer=reviewer,
        )
        s.add_event("", "info", "audit", "review.submit", {"eval_id": eval_id, "case_id": case_id, "passed": passed})
        self.refresh_scores(eval_id)
        return ok

    def refresh_scores(self, eval_id: str) -> bool:
        """Recompute a version's stored score for the evaluation ``eval_id`` after human reviews changed it,
        so version listings, the frontier and stop conditions see the reviewed numbers."""
        from .eval.review import apply_reviews

        try:
            report = apply_reviews(self.project, self.project.load_eval(eval_id))
        except KeyError:
            return False
        vs = self.project.versions
        if not vs.exists(report.skill_version):
            return False
        for split, summary in vs.get(report.skill_version).scores.items():
            if summary.get("eval_id") == eval_id:
                vs.record_score(report.skill_version, split, report.summary())
                return True
        return False

    def export_review_sheet(self, eval_id: str = "", path: str | Path = "") -> Path:
        from .eval.review import export_review_sheet

        if not eval_id:
            evals = self.evals()
            if not evals:
                raise KeyError("no evaluations yet")
            eval_id = evals[0]["eval_id"]
        dest = self.project.path(path) if path else self.project.outputs_dir / f"review_{eval_id}.csv"
        return export_review_sheet(self.project, self.project.load_eval(eval_id), dest)

    def import_review_sheet(self, path: str | Path) -> dict[str, Any]:
        from .eval.review import import_review_sheet

        result = import_review_sheet(self.project, self.project.path(path))
        for eval_id in result.get("eval_ids", []):
            self.refresh_scores(eval_id)
        return result

    # ---------------------------------------------------------------- read-only state
    def info(self) -> dict[str, Any]:
        info = self.project.info()
        info["splits"] = {k: len(v) for k, v in self.splits().items()} if info["n_cases"] else {}
        info["active_jobs"] = [
            j["id"] for j in self.jobs.list(limit=20) if j["status"] in ("queued", "running", "cancelling")
        ]
        info["pending_reviews"] = len(self.project.store.reviews(status="pending"))
        info["pending_approval"] = [v.id for v in self.project.versions.list() if v.status == "pending_approval"]
        info["errorbook"] = {
            k: v for k, v in self.error_stats().items() if k in ("total", "open", "fixed", "regressed")
        }
        return info

    def history(self) -> list[dict[str, Any]]:
        return load_history(self.project)

    def artifacts(self, kind: str = "", version: str = "") -> list[dict[str, Any]]:
        return self.project.store.artifacts(kind=kind, version=version)

    def read_artifact(self, path: str, max_chars: int = 200_000) -> str:
        """Read a project file by relative path (sandboxed to the project)."""
        p = self.project.path(path)
        if p.is_dir() and (p / "SKILL.md").is_file():  # a skill bundle is indexed by its directory
            p = p / "SKILL.md"
        if not p.is_file():
            raise FileNotFoundError(path)
        return p.read_text(encoding="utf-8", errors="replace")[:max_chars]

    def metrics(self) -> dict[str, Any]:
        from .report import metrics_summary

        return metrics_summary(self.project)

    def git_log(self, limit: int = 50) -> list[dict[str, str]]:
        return self.project.versions.git_log(limit)

    def health(self) -> dict[str, Any]:
        """Is the configured agent / LLM reachable? Never raises."""
        out: dict[str, Any] = {}
        for name, build in (("agent", self.adapter), ("llm", self.llm)):
            try:
                ok, msg = build().healthcheck()
            except Exception as e:
                ok, msg = False, f"{type(e).__name__}: {e}"
            out[name] = {"ok": ok, "message": msg}
        out["config_problems"] = self.project.config.validate()
        return out

    # ---------------------------------------------------------------- config / inputs
    def config(self) -> dict[str, Any]:
        return self.project.config.to_dict()

    def update_config(self, overrides: dict[str, Any]) -> dict[str, Any]:
        """Deep-merge ``overrides`` into the config, validate, and persist to skillloop.toml.
        Free-form tables (agent.options, llm.options, llm.roles, eval.judge_options, mcp_profiles) are
        replaced wholesale when present, so keys can be removed."""
        with self._lock:
            new = self.project.config.merged(overrides)
            problems = new.validate()
            if problems:
                raise ValueError("; ".join(problems))
            for rel in (new.project.requirement, new.project.seed_skill, new.project.dataset):
                self.project.path(rel)  # must stay inside the project (raises SandboxError otherwise)
            self.project.save_config(new)
            self.project.store.add_event("", "info", "audit", "config.update", {"keys": sorted(overrides)})
            return new.to_dict()

    def requirement(self) -> str:
        return self.project.load_requirement()

    def set_requirement(self, text: str) -> None:
        self.project.path(self.project.config.project.requirement).write_text(text, encoding="utf-8")
        self.project.store.add_event("", "info", "audit", "requirement.update", {"chars": len(text)})

    def close(self) -> None:
        self.jobs.shutdown(cancel=True)
        self.project.close()


# --------------------------------------------------------------------------- multi-project


_PROJECT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class Workspace:
    """A directory of isolated projects (one sub-directory each). Used by the web console and MCP server.

    When ``root`` itself is a project (contains skillloop.toml) it is exposed as the single project."""

    def __init__(self, root: str | Path = ".", echo: Callable[[dict[str, Any]], None] | None = None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.single = (self.root / CONFIG_FILE).exists()
        self._echo = echo
        self._services: dict[str, SkillLoop] = {}
        self._lock = threading.RLock()

    def _dir(self, name: str) -> Path:
        if self.single:
            if name not in ("", self.root.name):
                raise KeyError(f"unknown project {name!r}")
            return self.root
        if not _PROJECT_NAME.match(name or ""):
            raise KeyError(f"invalid project name {name!r} (letters, digits, . _ - only)")
        return safe_join(self.root, name)

    def names(self) -> list[str]:
        if self.single:
            return [self.root.name]
        return sorted(
            p.name
            for p in self.root.iterdir()
            if p.is_dir() and (p / CONFIG_FILE).exists() and _PROJECT_NAME.match(p.name)
        )

    def list(self) -> list[dict[str, Any]]:
        out = []
        for n in self.names():
            try:
                info = self.get(n).project.info()
                # the workspace key (directory name) is the address; the configured name is only a label
                out.append({**info, "name": n, "display_name": info["name"]})
            except Exception as e:  # a broken project must not hide the others
                out.append({"name": n, "error": f"{type(e).__name__}: {e}"})
        return out

    def get(self, name: str = "") -> SkillLoop:
        """Service for a project. ``name`` may be empty when the workspace holds exactly one project."""
        with self._lock:
            if not name:
                names = self.names()
                if len(names) != 1:
                    raise KeyError(
                        "project name required"
                        + (f"; available: {', '.join(names)}" if names else " (the workspace has no projects yet)")
                    )
                name = names[0]
            if name not in self._services:
                d = self._dir(name)
                if not (d / CONFIG_FILE).exists():
                    raise KeyError(f"unknown project {name!r}")
                self._services[name] = SkillLoop(d, echo=self._echo)
            return self._services[name]

    def create(
        self,
        name: str,
        *,
        requirement: str = "",
        skill_md: str = "",
        cases: list[dict[str, Any]] | None = None,
        config: dict[str, Any] | None = None,
    ) -> SkillLoop:
        if self.single:
            raise ValueError("this workspace is a single project; start the server in a parent directory to add more")
        d = self._dir(name)
        if (d / CONFIG_FILE).exists():
            raise ValueError(f"project {name!r} already exists")
        cfg = Config().merged(config or {})
        cfg.project.name = name
        problems = cfg.validate()
        if problems:
            raise ValueError("; ".join(problems))
        Project.init(
            d,
            name,
            config=cfg,
            requirement=requirement,
            seed_skill=Skill.from_markdown(skill_md) if skill_md.strip() else None,
            cases=[case_from_record(r, i) for i, r in enumerate(cases)] if cases is not None else None,
        )
        return self.get(name)

    def close(self) -> None:
        with self._lock:
            for s in self._services.values():
                s.close()
            self._services.clear()
