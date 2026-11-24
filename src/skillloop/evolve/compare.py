"""版本比较: paired, per-case comparison of two evaluations of different skill versions on the same cases."""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from typing import Any

from ..core.models import EvalReport, Serializable

EPS = 1e-9


@dataclass
class Comparison(Serializable):
    base_version: str = ""
    cand_version: str = ""
    split: str = ""
    metric: str = "mean_score"
    n_common: int = 0
    base_score: float = 0.0  # value of `metric` on the common cases
    cand_score: float = 0.0
    delta: float = 0.0
    base_pass_rate: float = 0.0
    cand_pass_rate: float = 0.0
    base_mean_score: float = 0.0
    cand_mean_score: float = 0.0
    fixed: list[str] = field(default_factory=list)  # fail -> pass
    regressed: list[str] = field(default_factory=list)  # pass -> fail
    improved: list[str] = field(default_factory=list)  # same pass state, score up
    worsened: list[str] = field(default_factory=list)  # same pass state, score down
    unchanged_pass: list[str] = field(default_factory=list)
    unchanged_fail: list[str] = field(default_factory=list)
    protected_regressions: list[str] = field(default_factory=list)  # regressed cases carrying a protected tag
    p_value: float = 1.0  # two-sided sign test on per-case metric differences
    ci_low: float = 0.0  # 95% paired-bootstrap CI of the mean per-case difference
    ci_high: float = 0.0
    by_tag: dict[str, dict[str, float]] = field(default_factory=dict)
    case_deltas: dict[str, dict[str, Any]] = field(
        default_factory=dict
    )  # case -> {base, cand, base_passed, cand_passed}

    @property
    def verdict(self) -> str:
        if self.delta > EPS:
            return "better"
        return "worse" if self.delta < -EPS else "same"


def sign_test(wins: int, losses: int) -> float:
    """Two-sided exact binomial sign test (ties dropped)."""
    n = wins + losses
    if n == 0:
        return 1.0
    k = min(wins, losses)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2**n
    return min(1.0, 2 * tail)


def bootstrap_ci(diffs: list[float], n_boot: int = 2000, seed: int = 0) -> tuple[float, float]:
    if not diffs:
        return 0.0, 0.0
    if len(diffs) == 1:
        return diffs[0], diffs[0]
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(rng.choices(diffs, k=n)) / n for _ in range(n_boot))
    return means[int(0.025 * n_boot)], means[min(n_boot - 1, int(0.975 * n_boot))]


def compare_reports(
    base: EvalReport,
    cand: EvalReport,
    *,
    metric: str = "mean_score",
    protect_tags: list[str] | tuple[str, ...] = (),
    seed: int = 0,
) -> Comparison:
    """Compare on the cases both reports contain (weights/tags are taken from ``cand``, then ``base``)."""
    b_scores, c_scores = base.case_scores(), cand.case_scores()
    b_rates, c_rates = base.case_pass_rates(), cand.case_pass_rates()
    b_pass, c_pass = base.case_passed(), cand.case_passed()
    common = [c for c in cand.case_ids() if c in b_scores]
    weights = {**base.weights, **cand.weights}
    tags = {**base.tags, **cand.tags}
    cmp = Comparison(
        base_version=base.skill_version,
        cand_version=cand.skill_version,
        split=cand.split or base.split,
        metric=metric,
        n_common=len(common),
    )
    if not common:
        return cmp

    def wmean(values: dict[str, float], ids: list[str]) -> float:
        total = sum(weights.get(c, 1.0) for c in ids)
        return sum(values[c] * weights.get(c, 1.0) for c in ids) / total if total > 0 else 0.0

    cmp.base_mean_score, cmp.cand_mean_score = wmean(b_scores, common), wmean(c_scores, common)
    cmp.base_pass_rate, cmp.cand_pass_rate = wmean(b_rates, common), wmean(c_rates, common)
    b_metric, c_metric = (b_rates, c_rates) if metric == "pass_rate" else (b_scores, c_scores)
    cmp.base_score, cmp.cand_score = wmean(b_metric, common), wmean(c_metric, common)
    cmp.delta = cmp.cand_score - cmp.base_score

    protect = set(protect_tags)
    diffs = []
    for c in common:
        d = c_metric[c] - b_metric[c]
        diffs.append(d)
        cmp.case_deltas[c] = {
            "base": round(b_scores[c], 4),
            "cand": round(c_scores[c], 4),
            "base_passed": b_pass[c],
            "cand_passed": c_pass[c],
        }
        if c_pass[c] and not b_pass[c]:
            cmp.fixed.append(c)
        elif b_pass[c] and not c_pass[c]:
            cmp.regressed.append(c)
            if protect & set(tags.get(c, [])):
                cmp.protected_regressions.append(c)
        elif c_scores[c] - b_scores[c] > EPS:
            cmp.improved.append(c)
        elif b_scores[c] - c_scores[c] > EPS:
            cmp.worsened.append(c)
        elif c_pass[c]:
            cmp.unchanged_pass.append(c)
        else:
            cmp.unchanged_fail.append(c)
    cmp.p_value = sign_test(sum(1 for d in diffs if d > EPS), sum(1 for d in diffs if d < -EPS))
    cmp.ci_low, cmp.ci_high = bootstrap_ci(diffs, seed=seed)

    groups: dict[str, list[str]] = {}
    for c in common:
        for t in tags.get(c, []) or ["(untagged)"]:
            groups.setdefault(t, []).append(c)
    for t, ids in sorted(groups.items()):
        b, k = wmean(b_metric, ids), wmean(c_metric, ids)
        cmp.by_tag[t] = {"n": len(ids), "base": round(b, 4), "cand": round(k, 4), "delta": round(k - b, 4)}
    return cmp
