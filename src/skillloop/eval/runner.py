"""运行评测: run every case (x trials) through the agent adapter with a given skill, then judge.

The Evaluator is the only component that talks to adapters and judges, so the evolution loop,
the CLI, the web console and the MCP server all evaluate in exactly the same way."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import re
import shutil
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..adapters.base import AgentAdapter, AgentRequest, build_adapter
from ..core.jobs import JobContext, NullContext
from ..core.models import Case, CaseResult, EvalReport, Judgment, Trajectory, new_id
from ..core.sandbox import safe_join, safe_name
from ..core.skill import Skill
from ..llm.base import LLMClient
from .base import Judge, JudgeContext, build_judge

if TYPE_CHECKING:
    from ..core.project import Project


class Evaluator:
    def __init__(
        self,
        adapter: AgentAdapter,
        judge: Judge,
        *,
        work_root: str | Path,
        trials: int = 1,
        concurrency: int = 4,
        timeout_s: float = 600.0,
        mcp_config: dict[str, Any] | None = None,
        keep_workdirs: bool = False,
        env: dict[str, str] | None = None,
        case_judge_options: dict[str, dict[str, Any]] | None = None,
        task_prompt: str = "",
        data_dirs: list[Path] | None = None,
        answer_regex: str = "",
        max_retries: int = 0,
        cache_dir: str | Path | None = None,
    ):
        self.adapter = adapter
        self.judge = judge
        self.work_root = Path(work_root)
        self.trials = max(1, trials)
        self.concurrency = max(1, concurrency)
        self.timeout_s = timeout_s
        self.mcp_config = mcp_config
        self.keep_workdirs = keep_workdirs
        self.env = env or {}
        self.case_judge_options = case_judge_options or {}
        self.task_prompt = task_prompt.strip()
        self.data_dirs = [Path(d) for d in (data_dirs or [])]
        self.answer_regex = re.compile(answer_regex, re.M | re.I) if answer_regex else None
        self.max_retries = max(0, max_retries)
        self.cache_dir = Path(cache_dir) if cache_dir and adapter.cacheable else None
        self._adapter_key = adapter.cache_key() if self.cache_dir else ""
        self._case_judges: dict[str, Judge] = {}
        self._lock = threading.Lock()

    @classmethod
    def from_project(
        cls,
        project: Project,
        *,
        llm: LLMClient | None = None,
        adapter: AgentAdapter | None = None,
        judge: Judge | None = None,
    ) -> Evaluator:
        cfg = project.config
        if judge is None:
            ctx = JudgeContext(llm=llm, pass_threshold=cfg.eval.pass_threshold, project_root=project.root)
            judge = build_judge(cfg.eval.judge, cfg.eval.judge_options, ctx)
        mcp_config = None
        if cfg.agent.mcp_profile:
            from ..mcp.profiles import build_mcp_config

            mcp_config = build_mcp_config(project, cfg.agent.mcp_profile)
        return cls(
            adapter or build_adapter(cfg.agent),
            judge,
            work_root=project.state.mkdir("runs"),
            trials=cfg.eval.trials,
            concurrency=cfg.eval.concurrency,
            timeout_s=cfg.eval.timeout_s,
            mcp_config=mcp_config,
            keep_workdirs=cfg.eval.keep_workdirs,
            case_judge_options=cfg.eval.judges,
            task_prompt=project.load_task(),
            data_dirs=project.data_dirs(),
            answer_regex=cfg.eval.answer_regex,
            max_retries=cfg.eval.max_retries,
            cache_dir=project.state.mkdir("cache", "runs") if cfg.eval.cache else None,
        )

    # ---------------------------------------------------------------- internals
    def _judge_for(self, case: Case) -> Judge:
        if not case.judge or case.judge == self.judge.name:
            return self.judge
        with self._lock:
            if case.judge not in self._case_judges:
                self._case_judges[case.judge] = build_judge(
                    case.judge, self.case_judge_options.get(case.judge, {}), self.judge.ctx
                )
            return self._case_judges[case.judge]

    def compose_input(self, case: Case) -> str:
        """What the agent is actually asked: the fixed task framing, a note about the reference
        directories, then the case input. The stored Case (and everything analysts see) keeps the bare input."""
        parts = []
        task = self.task_prompt
        if "{input}" in task:
            task = task.replace("{input}", case.input)
        if task:
            parts.append(task)
        if self.data_dirs:
            listing = ", ".join(f"./{d.name}/" for d in self.data_dirs)
            parts.append(f"Reference material is available read-only in your working directory: {listing}")
        if "{input}" not in self.task_prompt:
            parts.append(case.input)
        return "\n\n".join(parts)

    def _prepare(self, workdir: Path, case: Case) -> None:
        shutil.rmtree(workdir, ignore_errors=True)  # a retry starts from a clean directory
        workdir.mkdir(parents=True, exist_ok=True)
        for d in self.data_dirs:  # shared corpus: linked, never copied
            (workdir / d.name).symlink_to(d, target_is_directory=True)
        for rel, content in case.files.items():
            target = safe_join(workdir, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def _extract_answer(self, traj: Trajectory) -> bool:
        """Narrow ``traj.output`` to the final answer located by ``answer_regex``. False when it is absent."""
        assert self.answer_regex is not None
        matches = list(self.answer_regex.finditer(traj.output))
        if not matches:
            return False
        m = matches[-1]
        answer = (m.group(1) if m.groups() and m.group(1) is not None else m.group(0)).strip()
        traj.full_output, traj.output = traj.output, answer
        return True

    def _cache_path(self, skill: Skill, asked: Case, trial: int) -> Path | None:
        if self.cache_dir is None:
            return None
        ident = json.dumps(
            [
                self._adapter_key,
                skill.content_hash(),
                asked.id,
                asked.input,
                sorted(asked.files.items()),
                trial,
                sorted(d.name for d in self.data_dirs),
                self.mcp_config,
            ],
            sort_keys=True,
            ensure_ascii=False,
            default=repr,
        )
        digest = hashlib.sha256(ident.encode()).hexdigest()
        return self.cache_dir / digest[:2] / f"{digest[2:40]}.json"

    @staticmethod
    def _cache_load(path: Path | None) -> Trajectory | None:
        if path is None or not path.exists():
            return None
        try:
            traj = Trajectory.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError):
            return None
        # a reused run costs nothing now; keep the token counts for reference
        traj.usage = {**traj.usage, "cost_usd": 0.0, "cached": 1}
        return traj

    @staticmethod
    def _cache_store(path: Path | None, traj: Trajectory) -> None:
        if path is None or traj.status != "ok":
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(traj.to_dict(), ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass  # the cache is an optimisation; never fail a run over it

    def _run_one(
        self, skill: Skill, case: Case, trial: int, version: str, run_dir: Path, cancel: threading.Event
    ) -> CaseResult:
        workdir = run_dir / f"{safe_name(case.id)}_t{trial}"
        asked = dataclasses.replace(case, input=self.compose_input(case))
        t0 = time.time()
        attempts = 0
        cache_path = self._cache_path(skill, asked, trial)
        cached = self._cache_load(cache_path)
        while cached is None:
            attempts += 1
            self._prepare(workdir, case)
            try:
                traj = self.adapter.run(
                    AgentRequest(
                        case=asked,
                        skill=skill,
                        workdir=workdir,
                        timeout_s=self.timeout_s,
                        cancel=cancel,
                        env=dict(self.env),
                        mcp_config=self.mcp_config,
                        data_dirs=list(self.data_dirs),
                    )
                )
            except Exception as e:  # adapters should not raise, but a broken one must not kill the run
                traj = Trajectory(status="error", error=f"adapter raised {type(e).__name__}: {e}")
            if traj.status not in ("error", "timeout") or attempts > self.max_retries or cancel.is_set():
                break
        if cached is not None:
            traj = cached
        else:
            self._cache_store(cache_path, traj)
            traj.attempts = attempts
        traj.case_id = case.id
        if not traj.duration_s or attempts > 1:
            traj.duration_s = time.time() - t0
        if traj.status == "ok":
            found_answer = self._extract_answer(traj) if self.answer_regex else True
            try:
                judgment = self._judge_for(case).judge(case, traj)
                if not found_answer:  # tell analysts the reply ignored the required answer format
                    judgment.labels.append("no_final_answer")
                    judgment.reason = (judgment.reason + " (reply has no final-answer line)").strip()
            except Exception as e:
                judgment = Judgment(
                    score=0.0,
                    passed=False,
                    judge=self.judge.name,
                    needs_review=True,
                    reason=f"judge raised {type(e).__name__}: {e}",
                    labels=["judge_error"],
                )
        else:
            judgment = Judgment(
                score=0.0,
                passed=False,
                judge="runner",
                labels=[f"agent_{traj.status}"],
                reason=f"agent {traj.status}: {traj.error}".strip(),
            )
        if not self.keep_workdirs:
            shutil.rmtree(workdir, ignore_errors=True)
        return CaseResult(case_id=case.id, trial=trial, skill_version=version, trajectory=traj, judgment=judgment)

    # ---------------------------------------------------------------- API
    def evaluate(
        self,
        skill: Skill,
        cases: list[Case],
        *,
        version: str = "",
        split: str = "",
        label: str = "",
        ctx: JobContext | None = None,
        stage: str = "evaluate",
    ) -> EvalReport:
        """Evaluate ``skill`` on ``cases``. Cancellation (via ``ctx``) returns a partial report
        with ``cancelled=True``; cancelled trials are not included."""
        ctx = ctx or NullContext()
        report = EvalReport(
            skill_version=version,
            split=split,
            label=label,
            weights={c.id: c.weight for c in cases},
            tags={c.id: list(c.tags) for c in cases},
        )
        run_dir = self.work_root / safe_name(f"{report.id}_{new_id()}")
        todo = [(c, t) for c in cases for t in range(self.trials)]
        total, done = len(todo), 0
        ctx.progress(stage, 0, total, f"{label or split or 'eval'} {version}: 0/{total}".strip())
        cancel = ctx.cancel_event
        with ThreadPoolExecutor(max_workers=self.concurrency, thread_name_prefix="skillloop-eval") as pool:
            pending = set()
            it = iter(todo)
            exhausted = False
            while pending or not exhausted:
                while not exhausted and len(pending) < self.concurrency and not cancel.is_set():
                    try:
                        case, trial = next(it)
                    except StopIteration:
                        exhausted = True
                        break
                    pending.add(pool.submit(self._run_one, skill, case, trial, version, run_dir, cancel))
                if cancel.is_set():
                    exhausted = True
                if not pending:
                    break
                finished, pending = wait(pending, timeout=0.5, return_when=FIRST_COMPLETED)
                for fut in finished:
                    res = fut.result()
                    if res.trajectory.status == "cancelled":
                        continue
                    report.results.append(res)
                    done += 1
                    ctx.progress(
                        stage,
                        done,
                        total,
                        f"{label or split or 'eval'} {version}: {done}/{total} "
                        f"({res.case_id} {'✓' if res.judgment.passed else '✗'})".strip(),
                        case_id=res.case_id,
                        passed=res.judgment.passed,
                        score=res.judgment.score,
                    )
        order = {c.id: i for i, c in enumerate(cases)}
        report.results.sort(key=lambda r: (order.get(r.case_id, 0), r.trial))
        report.finished_at = time.time()
        report.cancelled = cancel.is_set()
        if not self.keep_workdirs:
            shutil.rmtree(run_dir, ignore_errors=True)
        return report
