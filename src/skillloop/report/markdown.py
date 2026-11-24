"""Markdown renderers: 错题库, 版本比较报告, 指标摘要. Pure functions returning strings."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from ..core.models import SkillVersion
from ..evolve.compare import Comparison
from ..evolve.errorbook import ErrorBook
from .metrics import metrics_summary

if TYPE_CHECKING:
    from ..core.project import Project

STATUS_ZH = {
    "open": "未解决",
    "regressed": "回退",
    "fixed": "已修复",
    "wontfix": "不修复",
    "best": "最佳",
    "accepted": "已接受",
    "rejected": "已拒绝",
    "candidate": "候选",
}


def fmt(v: Any, digits: int = 3) -> str:
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float):
        return f"{v:.{digits}f}"
    return "—" if v is None or v == "" else str(v)


def signed(v: Any, digits: int = 3) -> str:
    return f"{v:+.{digits}f}" if isinstance(v, (int, float)) else "—"


def cell(v: Any) -> str:
    """Make a value safe inside a markdown table cell."""
    return fmt(v).replace("|", "\\|").replace("\n", " ")


def table(headers: list[str], rows: list[list[Any]]) -> str:
    if not rows:
        return "_（无数据）_\n"
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(" --- " for _ in headers) + "|"]
    out += ["| " + " | ".join(cell(c) for c in row) + " |" for row in rows]
    return "\n".join(out) + "\n"


def fenced(text: Any, lang: str = "") -> str:
    """Fenced block whose fence is longer than any backtick run inside the content."""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False, indent=2, default=str)
    fence, run = "```", 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        if run >= len(fence):
            fence = "`" * (run + 1)
    return f"{fence}{lang}\n{text.rstrip()}\n{fence}\n"


def _ts(ts: Any) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if isinstance(ts, (int, float)) and ts else "—"


def _vid(v: SkillVersion | str | None, fallback: str = "") -> str:
    return v.id if isinstance(v, SkillVersion) else (v or fallback or "?")


# --------------------------------------------------------------------------- 错题库


def render_errorbook_md(book: ErrorBook) -> str:
    stats = book.stats()
    entries = book.entries()
    out = [
        "# 错题库",
        "",
        f"共 **{stats['total']}** 条：未解决 {stats['open']}，回退 {stats['regressed']}，"
        f"已修复 {stats['fixed']}，不修复 {stats['wontfix']}。",
        "",
    ]
    out += ["## 根因分类（未解决）", "", table(["根因类别", "数量"], [[k, v] for k, v in stats["by_category"].items()])]
    if stats["by_tag"]:
        out += ["## 标签分布（未解决）", "", table(["标签", "数量"], [[k, v] for k, v in stats["by_tag"].items()])]
    if stats["lessons"]:
        out += ["## 沉淀的经验", ""] + [f"- {x}" for x in stats["lessons"]] + [""]
    out += ["## 错题明细", ""]
    if not entries:
        out.append("_（暂无失败用例）_\n")
    order = {"regressed": 0, "open": 1, "wontfix": 2, "fixed": 3}
    for e in sorted(entries, key=lambda e: (order.get(e.status, 9), -e.occurrences, e.case_id)):
        out += [
            f"### {e.case_id} · {STATUS_ZH.get(e.status, e.status)}",
            "",
            f"- 状态：{STATUS_ZH.get(e.status, e.status)}（{e.status}）"
            + (f"，修复于 {e.fixed_in}" if e.fixed_in else ""),
            f"- 失败次数：{e.occurrences}；最近版本：{fmt(e.skill_version)}；迭代：{e.iteration}；得分：{fmt(e.score)}",
            f"- 首次/最近出现：{_ts(e.first_seen)} / {_ts(e.last_seen)}",
            f"- 标签：{', '.join(e.tags) if e.tags else '—'}",
            f"- 根因类别：{fmt(e.category)}",
            f"- 根因：{fmt(e.root_cause)}",
            f"- 经验：{fmt(e.lesson)}",
            f"- 建议措施：{fmt(e.suggested_fix)}",
            f"- 判分理由：{fmt(e.judge_reason)}",
            "",
            "**输入**",
            "",
            fenced(e.input),
            "**期望**",
            "",
            fenced("" if e.expected is None else e.expected),
            "**实际输出**",
            "",
            fenced(e.output),
        ]
        if e.repro:
            out += ["**复现**", "", fenced(e.repro, "sh")]
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- 版本比较


def _case_list(title: str, ids: list[str], deltas: dict[str, dict[str, Any]]) -> list[str]:
    if not ids:
        return []
    lines = [f"### {title}（{len(ids)}）", ""]
    for c in ids:
        d = deltas.get(c) or {}
        detail = f"：{fmt(d.get('base'))} → {fmt(d.get('cand'))}" if d else ""
        lines.append(f"- `{c}`{detail}")
    return lines + [""]


def render_gate_md(gate: dict[str, Any]) -> str:
    if not isinstance(gate, dict) or not gate:
        return ""
    out = ["## 提升闸门", ""]
    if "promoted" in gate:
        out += [f"结论：**{'通过，已提升' if gate.get('promoted') else '未通过'}**", ""]
    checks = [c for c in gate.get("checks") or [] if isinstance(c, dict)]
    if checks:
        out.append(
            table(
                ["检查项", "结果", "说明"],
                [[c.get("name"), "✓" if c.get("passed") else "✗", c.get("detail")] for c in checks],
            )
        )
    reasons = [str(r) for r in gate.get("reasons") or []]
    if reasons:
        out += ["原因：", ""] + [f"- {r}" for r in reasons] + [""]
    return "\n".join(out)


def render_comparison_md(
    comparison: Comparison,
    base_version: SkillVersion | str | None = None,
    cand_version: SkillVersion | str | None = None,
    diff_text: str = "",
) -> str:
    cmp = comparison
    base, cand = _vid(base_version, cmp.base_version), _vid(cand_version, cmp.cand_version)
    verdict = {"better": "更好", "worse": "更差", "same": "持平"}[cmp.verdict]
    out = [f"# 版本比较报告：{base} → {cand}", ""]
    for v in (base_version, cand_version):
        if isinstance(v, SkillVersion) and v.summary:
            out.append(f"- **{v.id}**（{STATUS_ZH.get(v.status, v.status)}）：{v.summary}")
    if out[-1] != "":
        out.append("")
    if not cmp.n_common:
        out += ["两个版本没有可比较的共同用例。", ""]
    else:
        out += [
            f"在 `{fmt(cmp.split)}` 集的 {cmp.n_common} 个共同用例上，`{cmp.metric}` 从 "
            f"**{fmt(cmp.base_score)}** 变为 **{fmt(cmp.cand_score)}**（Δ **{signed(cmp.delta)}**，{verdict}）。",
            "",
            f"- 95% 置信区间（配对 bootstrap）：[{signed(cmp.ci_low)}, {signed(cmp.ci_high)}]",
            f"- 符号检验 p 值：{fmt(cmp.p_value)}",
            f"- 通过率：{fmt(cmp.base_pass_rate)} → {fmt(cmp.cand_pass_rate)}；"
            f"平均分：{fmt(cmp.base_mean_score)} → {fmt(cmp.cand_mean_score)}",
            f"- 修复 {len(cmp.fixed)}，回退 {len(cmp.regressed)}，提升 {len(cmp.improved)}，"
            f"下降 {len(cmp.worsened)}，保持通过 {len(cmp.unchanged_pass)}，保持失败 {len(cmp.unchanged_fail)}",
            "",
        ]
        if cmp.protected_regressions:
            out += ["> ⚠ 受保护用例发生回退：" + ", ".join(f"`{c}`" for c in cmp.protected_regressions), ""]
        out += ["## 用例变化", ""]
        lists = (
            _case_list("修复（失败 → 通过）", cmp.fixed, cmp.case_deltas)
            + _case_list("回退（通过 → 失败）", cmp.regressed, cmp.case_deltas)
            + _case_list("提升（分数上升）", cmp.improved, cmp.case_deltas)
            + _case_list("下降（分数下降）", cmp.worsened, cmp.case_deltas)
            + _case_list("保持失败", cmp.unchanged_fail, cmp.case_deltas)
        )
        out += lists or ["_（所有用例结果一致）_", ""]
        if cmp.by_tag:
            out += [
                "## 按标签",
                "",
                table(
                    ["标签", "用例数", base, cand, "Δ"],
                    [
                        [t, int(d.get("n", 0)), d.get("base"), d.get("cand"), signed(d.get("delta"))]
                        for t, d in cmp.by_tag.items()
                    ],
                ),
            ]
    gate = render_gate_md(cand_version.gate) if isinstance(cand_version, SkillVersion) else ""
    if gate:
        out.append(gate)
    if diff_text.strip():
        out += ["## Skill 差异", "", fenced(diff_text, "diff")]
    return "\n".join(out).rstrip() + "\n"


# --------------------------------------------------------------------------- 指标摘要


def _score_cell(scores: dict[str, Any], split: str, metric: str) -> Any:
    s = scores.get(split)
    return s.get(metric) if isinstance(s, dict) else None


def render_metrics_md(project: Project, summary: dict[str, Any] | None = None) -> str:
    m = summary or metrics_summary(project)
    metric = m["metric"]
    out = [f"# 指标摘要：{m['project']}", "", f"生成时间：{_ts(m['generated_at'])}；主指标：`{metric}`", ""]
    if not m["n_versions"]:
        out += ["尚无 Skill 版本。运行一次进化或评测后再查看。", ""]
    out += [
        "## 概览",
        "",
        table(
            ["项", "值"],
            [
                ["最佳版本", m["best_version"] or "—"],
                ["版本数", m["n_versions"]],
                ["迭代轮数", m["n_iterations"]],
                ["提升 / 拒绝候选", f"{m['promotions']} / {m['rejections']}"],
                ["评测次数 / 运行数", f"{m['totals']['n_evals']} / {m['totals']['n_runs']}"],
                ["评测总耗时 (s)", m["totals"]["duration_s"]],
                ["成本 (USD)", m["totals"]["cost_usd"]],
            ],
        ),
    ]
    if m["best_scores"]:
        out += [
            "## 最佳版本得分",
            "",
            table(
                ["数据集", "通过率", "平均分", "平均分下界", "用例数", "相对首版 Δ"],
                [
                    [
                        s,
                        d.get("pass_rate"),
                        d.get("mean_score"),
                        d.get("score_lcb"),
                        d.get("n_cases"),
                        signed(m["improvement"].get(s)),
                    ]
                    for s, d in m["best_scores"].items()
                ],
            ),
        ]
    if m["trend"]:
        out += [
            "## 迭代趋势",
            "",
            table(
                ["迭代", "最佳版本", "最佳分 (val)", "训练分"],
                [[t["iteration"], t["best_version"], t["best_score"], t["train_score"]] for t in m["trend"]],
            ),
        ]
    if m["versions"]:
        out += [
            "## 版本谱系",
            "",
            table(
                [
                    "版本",
                    "父版本",
                    "来源",
                    "状态",
                    "迭代",
                    f"val {metric}",
                    f"train {metric}",
                    f"test {metric}",
                    "变更摘要",
                ],
                [
                    [
                        v["id"],
                        v["parent"] or "—",
                        v["origin"],
                        STATUS_ZH.get(v["status"], v["status"]),
                        v["iteration"],
                        _score_cell(v["scores"], "val", metric),
                        _score_cell(v["scores"], "train", metric),
                        _score_cell(v["scores"], "test", metric),
                        v["summary"],
                    ]
                    for v in m["versions"]
                ],
            ),
        ]
    eb = m["errorbook"] or {}
    if eb:
        out += [
            "## 错题库",
            "",
            f"共 {eb.get('total', 0)} 条：未解决 {eb.get('open', 0)}，回退 {eb.get('regressed', 0)}，"
            f"已修复 {eb.get('fixed', 0)}。",
            "",
        ]
        if eb.get("by_category"):
            out.append(table(["根因类别", "数量"], [[k, v] for k, v in eb["by_category"].items()]))
    usage = m["totals"]["usage"]
    if usage:
        out += ["## 资源用量", "", table(["项", "合计"], [[k, v] for k, v in sorted(usage.items())])]
    return "\n".join(out).rstrip() + "\n"
