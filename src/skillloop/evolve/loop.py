"""Skill 进化闭环 - the orchestrator.

    生成初版 Skill → 运行评测 → 人工/LLM 判分 → 错题库 → 迭代 Skill → 版本比较 ─┐
                        ▲                                                      │
                        └──────────────── (next iteration) ◄───────────────────┘
                                                   │ gate passes
                                                   ▼
                                             提升最佳版本

Data discipline: the *train* split is the only thing analysts ever see; candidates are selected on
the *val* split; the *test* split is touched once at the end to report an unbiased number
(or per-candidate when ``evolve.confirm_on_test`` is on). All state is on disk, so a run can be
cancelled and resumed: finished evaluations are reused, never re-run."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..adapters.base import AgentAdapter
from ..core.dataset import split_cases
from ..core.jobs import JobCancelled, JobContext, NullContext
from ..core.models import Case, EvalReport, Serializable, SkillVersion, now
from ..core.project import Project
from ..eval.runner import Evaluator
from ..llm.base import LLMClient, build_llm
from .bootstrap import generate_initial_skill
from .compare import Comparison, compare_reports
from .errorbook import ErrorBook
from .gate import GateDecision, decide
from .guard import check_candidate, find_leaks, scrub_case_ids
from .proposer import Proposer

HISTORY_FILE = "history.json"


@dataclass
class EvolveResult(Serializable):
    best_version: str = ""
    baseline_version: str = ""
    iterations: int = 0
    stop_reason: str = ""
    baseline_scores: dict[str, Any] = field(default_factory=dict)  # split -> summary
    best_scores: dict[str, Any] = field(default_factory=dict)
    promotions: list[str] = field(default_factory=list)
    pending_approval: list[str] = field(default_factory=list)
    outputs: dict[str, str] = field(default_factory=dict)
    splits: dict[str, int] = field(default_factory=dict)


def load_history(project: Project) -> list[dict[str, Any]]:
    p = project.state.path(HISTORY_FILE)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


class EvolutionLoop:
    def __init__(
        self,
        project: Project,
        *,
        llm: LLMClient | None = None,
        adapter: AgentAdapter | None = None,
        evaluator: Evaluator | None = None,
        ctx: JobContext | None = None,
    ):
        self.project = project
        self.cfg = project.config.evolve
        self.ctx = ctx or NullContext(project.store)
        self.llm = llm or build_llm(project.config.llm)
        self.evaluator = evaluator or Evaluator.from_project(project, llm=self.llm, adapter=adapter)
        self.versions = project.versions
        self.errorbook = ErrorBook(project.errorbook_path)
        self.proposer = Proposer(
            self.llm, self.cfg, project.load_requirement(), max_workers=max(1, project.config.eval.concurrency)
        )
        self.history = load_history(project)
        self.cases: list[Case] = []
        self.splits: dict[str, list[Case]] = {}

    # ---------------------------------------------------------------- helpers
    def _save_history(self) -> None:
        self.project.state.write(HISTORY_FILE, json.dumps(self.history, ensure_ascii=False, indent=1, default=str))

    def _score(self, summary: dict[str, Any] | None) -> float:
        return float((summary or {}).get(self.cfg.metric, 0.0))

    def evaluate(self, version: SkillVersion, split: str, *, force: bool = False) -> EvalReport:
        """Evaluate ``version`` on a split, reusing a stored complete evaluation when there is one."""
        cases = self.splits.get(split, [])
        prior = version.scores.get(split) or {}
        if not force and prior.get("eval_id") and not prior.get("cancelled"):
            try:
                report = self.project.load_eval(prior["eval_id"])
                if set(report.case_ids()) == {c.id for c in cases}:
                    report = self._with_reviews(report)
                    summary = report.summary()
                    if any(summary[k] != prior.get(k) for k in ("mean_score", "pass_rate")):
                        version.scores = self.versions.record_score(version.id, split, summary).scores
                    return report
            except KeyError:
                pass
        skill = self.versions.load_skill(version.id)
        report = self.evaluator.evaluate(
            skill, cases, version=version.id, split=split, label=split, ctx=self.ctx, stage=f"evaluate:{split}"
        )
        if report.cancelled:
            raise JobCancelled(self.ctx.job_id)
        self.project.save_eval(report)
        self._enqueue_reviews(report)
        report = self._with_reviews(report)
        fresh = self.versions.record_score(version.id, split, report.summary())
        version.scores = fresh.scores
        return report

    def _enqueue_reviews(self, report: EvalReport) -> None:
        if not any(r.judgment.needs_review for r in report.results):
            return
        try:
            from ..eval.review import enqueue_reviews

            n = enqueue_reviews(self.project, report)
            if n:
                self.ctx.log(f"{n} result(s) queued for human review", eval_id=report.id)
        except ImportError:
            pass

    def _with_reviews(self, report: EvalReport) -> EvalReport:
        """Human review verdicts, when present, override automatic judgments."""
        try:
            from ..eval.review import apply_reviews

            return apply_reviews(self.project, report)
        except ImportError:
            return report

    def _frontier(self) -> list[SkillVersion]:
        """Top-N viable versions by val score (best first); parents are drawn from here."""
        pool = [v for v in self.versions.list() if v.status in ("best", "accepted") and "val" in v.scores]
        pool.sort(key=lambda v: (-self._score(v.scores.get("val")), -int(v.id[1:])))
        best = self.versions.best_id()
        pool.sort(key=lambda v: v.id != best)  # the promoted best always leads
        return pool[: max(1, self.cfg.frontier_size)]

    def _attempt_history(self) -> list[dict[str, Any]]:
        """Compact record of recent attempts, shown to analysts so they do not repeat rejected ideas."""
        out = []
        for rec in self.history[-4:]:
            for c in rec.get("candidates", []):
                out.append(
                    {
                        "iteration": rec.get("iteration"),
                        "change": c.get("summary", "")[:300],
                        "outcome": "promoted" if c.get("promoted") else "rejected",
                        "val_delta": c.get("delta"),
                        "why": "; ".join(c.get("reasons", []))[:300],
                    }
                )
        return out

    # ---------------------------------------------------------------- stage 1: baseline
    def ensure_baseline(self) -> SkillVersion:
        """生成初版 Skill (seed or bootstrap), store it as the first version and make it the best."""
        best = self.versions.best()
        if best is not None:
            return best
        existing = self.versions.list()
        if existing:
            version = existing[-1]
        else:
            seed = self.project.load_seed_skill()
            if seed is not None:
                origin, skill, summary = "seed", seed, "seed skill provided by the user"
            else:
                self.ctx.progress("bootstrap", 0, 1, "generating the initial skill from the requirement document")
                name = self.project.config.project.skill_name or self.project.name
                skill = generate_initial_skill(
                    self.llm,
                    self.project.load_requirement(),
                    name,
                    self.splits.get("train", [])[:6],
                    self.cfg.max_skill_chars,
                )
                origin, summary = "bootstrap", "generated from the requirement document"
            problems = skill.validate(self.cfg.max_skill_chars)
            if problems:
                raise ValueError("initial skill is invalid: " + "; ".join(problems))
            version = self.versions.create(skill, origin=origin, summary=summary, status="accepted")
            self.ctx.audit("version.create", version=version.id, origin=origin)
            self.ctx.artifact(
                "skill",
                self.versions.skill_dir(version.id) / "SKILL.md",
                version=version.id,
                title=f"{version.id} ({origin})",
            )
        self.versions.set_best(version.id, "baseline")
        self.ctx.audit("version.promote", version=version.id, reason="baseline")
        return self.versions.get(version.id)

    # ---------------------------------------------------------------- one iteration
    def iterate(self, iteration: int, stale: int) -> dict[str, Any]:
        ctx, cfg = self.ctx, self.cfg
        best = self.versions.best()
        assert best is not None
        frontier = self._frontier() or [best]
        parent = frontier[stale % len(frontier)]
        parent_skill = self.versions.load_skill(parent.id)
        record: dict[str, Any] = {
            "iteration": iteration,
            "parent": parent.id,
            "candidates": [],
            "ts": now(),
            "best_version": best.id,
            "best_score": self._score(best.scores.get("val")),
        }
        ctx.log(
            f"iteration {iteration}: parent {parent.id} (best {best.id}, val {cfg.metric}={record['best_score']:.3f})",
            iteration=iteration,
        )

        # 运行评测 + 判分 on train, 错题库
        train_report = self.evaluate(parent, "train")
        record["train_score"] = self._score(train_report.summary())
        record["errorbook"] = self.errorbook.record_eval(train_report, self.splits["train"], iteration)
        if not train_report.failures():
            record["note"] = "parent passes every train case"
            return record

        # 迭代 Skill
        lessons = self.errorbook.stats()["lessons"]
        proposal = self.proposer.propose(
            parent_skill, train_report, self.splits["train"], lessons=lessons, history=self._attempt_history(), ctx=ctx
        )
        for d in proposal.diagnoses:
            self.errorbook.annotate(
                str(d.get("case_id", "")),
                category=str(d.get("category", "")),
                root_cause=str(d.get("root_cause", "")),
                lesson=str(d.get("lesson", "")),
            )
        record["notes"] = proposal.notes
        record["n_patches"] = len(proposal.patches)
        ctx.check_cancelled()
        if not proposal.candidates:
            record["note"] = "; ".join(proposal.notes) or "no candidate produced"
            return record

        # 版本比较 + 闸门, each candidate against the current best on val
        best_val = self.evaluate(best, "val")
        decisions: list[tuple[SkillVersion, GateDecision, Comparison]] = []
        for i, cand in enumerate(proposal.candidates, 1):
            ctx.check_cancelled()
            known = self.splits["train"] + self.splits["val"]
            skill = scrub_case_ids(cand.skill, self.cases)
            problems = check_candidate(skill, parent_skill, known, cfg)
            raw_leaks = (
                find_leaks(cand.skill, parent_skill, known) if cfg.leak_check else []
            )  # checked before scrubbing can mask a leak
            if raw_leaks and not any("leakage" in p for p in problems):
                problems.append(
                    "answer leakage: gold answers/inputs of cases copied into the skill: " + ", ".join(raw_leaks[:8])
                )
            dup = self.versions.find_by_hash(skill.content_hash())
            if dup is not None:
                record["candidates"].append(
                    {
                        "version": dup.id,
                        "summary": cand.summary,
                        "promoted": False,
                        "reasons": [f"identical to existing version {dup.id}"],
                        "duplicate": True,
                    }
                )
                continue
            version = self.versions.create(
                skill,
                parent=parent.id,
                origin="evolve",
                summary=cand.summary,
                iteration=iteration,
                patches=[{**cand.patch.to_dict(), "strategy": cand.strategy, "rejected_edits": cand.rejected_edits}],
            )
            ctx.audit("version.create", version=version.id, parent=parent.id, strategy=cand.strategy)
            ctx.artifact(
                "skill",
                self.versions.skill_dir(version.id) / "SKILL.md",
                version=version.id,
                title=f"{version.id} ({cand.strategy})",
            )
            if problems:
                cmp = Comparison(base_version=best.id, cand_version=version.id, split="val", metric=cfg.metric)
                decision = decide(cmp, cfg, guard_problems=problems)
                ctx.log(f"candidate {version.id} blocked by guardrails: {'; '.join(problems)}", level="warning")
            else:
                ctx.log(f"candidate {i}/{len(proposal.candidates)} {version.id}: {cand.summary[:160]}")
                cand_val = self.evaluate(version, "val")
                cmp = compare_reports(best_val, cand_val, metric=cfg.metric, protect_tags=cfg.protect_tags)
                test_cmp = None
                if cfg.confirm_on_test and self.splits["test"] and cmp.delta >= cfg.min_delta:
                    test_cmp = compare_reports(
                        self.evaluate(best, "test"),
                        self.evaluate(version, "test"),
                        metric=cfg.metric,
                        protect_tags=cfg.protect_tags,
                    )
                decision = decide(cmp, cfg, test_cmp=test_cmp)
            version = self.versions.get(version.id)
            version.gate = decision.to_dict()
            version.status = (
                "pending_approval"
                if decision.needs_approval
                else "accepted"
                if decision.passed or self._viable(cmp, problems)
                else "rejected"
            )
            self.versions.update(version, f"gate: {'pass' if decision.passed else 'fail'}")
            decisions.append((version, decision, cmp))
            record["candidates"].append(
                {
                    "version": version.id,
                    "strategy": cand.strategy,
                    "summary": cand.summary,
                    "val_score": cmp.cand_score,
                    "delta": cmp.delta,
                    "fixed": cmp.fixed,
                    "regressed": cmp.regressed,
                    "promoted": False,
                    "needs_approval": decision.needs_approval,
                    "reasons": decision.reasons,
                }
            )

        # 提升最佳版本: the best gate-passing candidate
        winners = [(v, d, c) for v, d, c in decisions if d.promoted]
        if winners:
            v, d, c = max(winners, key=lambda t: (t[2].cand_score, -len(t[2].regressed), -int(t[0].id[1:])))
            self.versions.set_best(v.id, d.reasons[-1] if d.reasons else "")
            ctx.audit("version.promote", version=v.id, previous=best.id, delta=c.delta)
            ctx.log(
                f"★ promoted {v.id}: val {cfg.metric} {c.base_score:.3f} → {c.cand_score:.3f} "
                f"({len(c.fixed)} fixed, {len(c.regressed)} regressed)"
            )
            for rec in record["candidates"]:
                rec["promoted"] = rec["version"] == v.id
            record["promoted"] = v.id
            record["best_version"], record["best_score"] = v.id, c.cand_score
        promoted_id = record.get("promoted")
        for v, d, _cmp in decisions:
            if v.id != promoted_id:
                why = "; ".join(r for r in d.reasons if not r.startswith("promoted:")) or "a better candidate won"
                ctx.log(f"✗ {v.id} not promoted: {why}")
        return record

    def _viable(self, cmp: Comparison, problems: list[str]) -> bool:
        """Not promotable, yet good enough to stay in the frontier as a possible parent."""
        return (
            not problems
            and cmp.n_common > 0
            and cmp.delta >= 0
            and len(cmp.regressed) <= self.cfg.max_regressions
            and not cmp.protected_regressions
        )

    # ---------------------------------------------------------------- the loop
    def run(self, max_iterations: int | None = None, *, write_outputs: bool = True) -> EvolveResult:
        ctx, cfg = self.ctx, self.cfg
        problems = self.project.config.validate()
        if problems:
            raise ValueError("invalid config: " + "; ".join(problems))
        self.cases = self.project.load_cases()
        if not self.cases:
            raise ValueError("the dataset is empty - add cases (or run the devset pipeline) before evolving")
        self.splits = split_cases(self.cases, cfg.train_ratio, cfg.val_ratio, cfg.split_seed)
        if not self.splits["val"]:
            ctx.log(
                "dataset too small for a separate val split; selecting on train (overfitting risk)", level="warning"
            )
            self.splits["val"] = list(self.splits["train"])
        result = EvolveResult(splits={k: len(v) for k, v in self.splits.items()})
        ctx.log(
            f"dataset: {len(self.cases)} cases → train {len(self.splits['train'])} / "
            f"val {len(self.splits['val'])} / test {len(self.splits['test'])}"
        )

        baseline = self.ensure_baseline()
        first = self.versions.lineage(baseline.id)[0]
        result.baseline_version = first.id
        base_val = self.evaluate(baseline, "val")
        ctx.log(
            f"baseline {baseline.id}: val {cfg.metric}={self._score(base_val.summary()):.3f} "
            f"(pass rate {base_val.pass_rate:.1%})"
        )

        n_iter = cfg.max_iterations if max_iterations is None else max_iterations
        start = (self.history[-1]["iteration"] + 1) if self.history else 1
        stale, stop = 0, ""
        for iteration in range(start, start + n_iter):
            ctx.check_cancelled()
            best = self.versions.best()
            if self._score(best.scores.get("val")) >= cfg.target_score - 1e-9:  # type: ignore[union-attr]
                stop = f"target score {cfg.target_score} reached on val"
                break
            ctx.progress("iteration", iteration - start, n_iter, f"iteration {iteration}", iteration=iteration)
            record = self.iterate(iteration, stale)
            self.history.append(record)
            self._save_history()
            result.iterations += 1
            if record.get("promoted"):
                result.promotions.append(record["promoted"])
                stale = 0
            else:
                stale += 1
            result.pending_approval += [c["version"] for c in record["candidates"] if c.get("needs_approval")]
            if record.get("note") == "parent passes every train case" and len(self._frontier()) <= 1:
                stop = "nothing left to learn: the best version passes every train case"
                break
            if stale >= cfg.patience:
                stop = f"no promotion in {cfg.patience} consecutive iterations"
                break
        else:
            stop = f"reached max_iterations ({n_iter})"
        best = self.versions.best()
        assert best is not None
        if not stop and self._score(best.scores.get("val")) >= cfg.target_score - 1e-9:
            stop = f"target score {cfg.target_score} reached on val"
        result.stop_reason = stop

        # held-out confirmation: baseline vs best on test, evaluated once
        if self.splits["test"]:
            ctx.progress("test", 0, 1, "held-out test evaluation")
            self.evaluate(first, "test")
            if best.id != first.id:
                self.evaluate(best, "test")
        final_train = self.evaluate(best, "train")
        self.errorbook.record_eval(
            final_train, self.splits["train"], self.history[-1]["iteration"] if self.history else 0
        )
        result.best_version = best.id
        result.best_scores = self.versions.get(best.id).scores
        result.baseline_scores = self.versions.get(first.id).scores
        ctx.log(
            f"done: best {best.id} ({stop}); val {cfg.metric} "
            f"{self._score(result.baseline_scores.get('val')):.3f} → {self._score(result.best_scores.get('val')):.3f}"
        )
        if write_outputs:
            try:
                from ..report import write_outputs as _write

                result.outputs = _write(self.project, ctx)
            except ImportError:
                pass
        return result
