"""The ``answer`` judge: tolerant matching of short factual answers.

Meant for question-answering datasets whose gold answers are short and mostly numeric
("2,602", "36080 million", "6.1596", "March 1977", "Zaire"), where exact string comparison is too
strict and "the last number in the reply" is too loose. It understands thousands separators,
magnitude words (million / billion / 万 / 亿 ...), percent signs and relative tolerance, ignores
incidental years in the reply, and falls back to text containment for non-numeric answers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..core.models import Case, Judgment, Trajectory
from ..core.registry import JUDGES
from .base import Judge

SCALES = {
    "thousand": 1e3, "thousands": 1e3, "million": 1e6, "millions": 1e6, "mn": 1e6, "mm": 1e6,
    "billion": 1e9, "billions": 1e9, "bn": 1e9, "trillion": 1e12, "trillions": 1e12, "tn": 1e12,
    "千": 1e3, "万": 1e4, "百万": 1e6, "亿": 1e8, "万亿": 1e12,
}  # fmt: skip
_SCALE_WORDS = sorted(SCALES, key=len, reverse=True)
_NUMBER = re.compile(
    r"(?<![\w.])(?P<neg>\(?[-−–]?)\s?[$€£¥]?\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?|\.\d+)"
    r"\s?(?P<pct>%|percent\b|个?百分点)?\s?(?P<scale>" + "|".join(re.escape(w) for w in _SCALE_WORDS) + r")?(?![a-z])",
    re.I,
)
# words that carry no information about *which* quantity the answer is
_FILLER = {
    "a", "an", "the", "of", "in", "and", "or", "to", "is", "was", "were", "about", "approximately", "roughly",
    "around", "nearly", "dollar", "dollars", "usd", "us", "nominal", "percent", "per", "cent", "total", "answer",
    "final", "value", "amount", "equals", "equal", "which", "that", "it",
}  # fmt: skip


@dataclass
class Num:
    value: float  # as written, magnitude word not applied
    scale: float  # 1.0 when no magnitude word followed the number
    percent: bool
    text: str

    @property
    def scaled(self) -> float:
        return self.value * self.scale

    @property
    def year_like(self) -> bool:
        return self.scale == 1.0 and not self.percent and self.value == int(self.value) and 1900 <= self.value <= 2100


def extract_numbers(text: Any) -> list[Num]:
    if isinstance(text, bool):
        return []
    if isinstance(text, (int, float)):
        return [Num(float(text), 1.0, False, str(text))]
    out = []
    for m in _NUMBER.finditer(str(text)):
        try:
            value = float(m.group("num").replace(",", ""))
        except ValueError:
            continue
        neg = m.group("neg") or ""
        if any(c in neg for c in "-−–"):
            value = -value
        scale = SCALES.get((m.group("scale") or "").lower(), 1.0)
        out.append(Num(value, scale, bool(m.group("pct")), m.group(0).strip()))
    return out


def _close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol * abs(b) + 1e-9 * max(1.0, abs(b))


def numbers_match(expected: Num, got: Num, tol: float) -> bool:
    """Equal within ``tol`` under any consistent reading of magnitude words and percent signs."""
    pairs = [(got.scaled, expected.scaled)]
    if (expected.scale == 1.0) != (got.scale == 1.0):
        # only one side spelled the magnitude out ("36080 million" vs "36080" when the question fixed
        # the unit): accept the bare figures agreeing as well
        pairs.append((got.value, expected.value))
    if expected.percent != got.percent:  # 0.12 vs 12%
        pairs += [(g * 100, e) if expected.percent else (g, e * 100) for g, e in list(pairs)]
    return any(_close(g, e, tol) for g, e in pairs)


def significant_words(text: str) -> set[str]:
    """Lower-cased words that identify the quantity (months, names...), minus numbers, units and filler."""
    stripped = _NUMBER.sub(" ", str(text))
    words = set(re.findall(r"[a-z]{2,}|[㐀-鿿]+", stripped.lower()))
    return {w for w in words if w not in _FILLER and w not in SCALES}


def _norm(text: Any) -> str:
    return " ".join(re.sub(r"[^\w%.\-]+", " ", str(text).lower()).split())


def match_answer(expected: Any, predicted: str, tol: float) -> tuple[bool, str]:
    """(matched, explanation) for one acceptable expected answer."""
    exp_nums = extract_numbers(expected)
    if not exp_nums:
        want = _norm(expected)
        if not want:
            return False, "expected answer is empty"
        if want in _norm(predicted):
            return True, f"reply contains {str(expected)!r}"
        words = significant_words(str(expected))
        if words and words <= set(re.findall(r"[a-z]{2,}|[㐀-鿿]+", predicted.lower())):
            return True, f"reply contains every key word of {str(expected)!r}"
        return False, f"expected {str(expected)!r} not found in reply"

    got = extract_numbers(predicted)
    if not got:
        return False, "no number in reply"
    if not any(n.year_like for n in exp_nums) and any(not n.year_like for n in got):
        got = [n for n in got if not n.year_like]  # incidental years ("in FY 1955 ...") are not the answer
    missing = []
    for e in exp_nums:
        if not any(numbers_match(e, g, tol) for g in got):
            missing.append(e)
    if missing:
        e = missing[0]
        nearest = min(got, key=lambda g: min(abs(g.scaled - e.scaled), abs(g.value - e.value)))
        off = abs(nearest.value - e.value) / abs(e.value) if e.value else float("inf")
        detail = f"closest {nearest.text!r} is {off:.2%} off" if off < 10 else f"got {nearest.text!r}"
        return False, f"expected {e.text!r}, {detail} (tolerance {tol:.2%})"
    need = significant_words(str(expected)) if isinstance(expected, str) else set()
    if need:
        have = set(re.findall(r"[a-z]{2,}|[㐀-鿿]+", predicted.lower()))
        lost = need - have
        if lost:
            return False, f"number matches but reply lacks {sorted(lost)} from expected {expected!r}"
    return True, f"matches {', '.join(n.text for n in exp_nums)}" + (f" within {tol:.2%}" if tol else "")


@JUDGES.register("answer")
class AnswerJudge(Judge):
    """Tolerant short-answer matching.

    Options:
      ``tolerance``   relative tolerance for a pass (default 0.0 = equal after normalisation)
      ``tolerances``  extra, looser levels used only for partial credit, e.g. [0.01, 0.05]:
                      the score is the fraction of all levels matched, ``passed`` is decided by ``tolerance``.
    ``case.expected`` may be a list of acceptable answers (any one suffices)."""

    name = "answer"

    def judge(self, case: Case, trajectory: Trajectory) -> Judgment:
        tol = float(self.options.get("tolerance", 0.0))
        levels = sorted({tol, *(float(t) for t in self.options.get("tolerances", []) if float(t) >= tol)})
        expected = case.expected if isinstance(case.expected, list) else [case.expected]
        expected = [e for e in expected if e not in (None, "")]
        if not expected:
            return self.verdict(0.0, "case has no expected answer", passed=False, needs_review=True)
        best: tuple[int, str] = (-1, "")
        for exp in expected:
            results = [match_answer(exp, trajectory.output, level) for level in levels]
            hits = sum(ok for ok, _ in results)
            if hits > best[0]:
                # explain with the strictest level: that is the one deciding pass/fail
                best = (hits, results[0][1])
            if results[0][0]:
                break
        hits, reason = best
        passed = hits == len(levels)
        if not passed and hits:
            reason += f"; within looser tolerance ({hits}/{len(levels)} levels)"
        return self.verdict(hits / len(levels), reason, passed=passed)
