"""Self-contained HTML report (inline CSS + inline SVG, no external assets, light & dark)."""

from __future__ import annotations

import html
import time
from typing import TYPE_CHECKING, Any

from ..core.models import SkillVersion
from ..evolve.compare import Comparison
from ..evolve.errorbook import ErrorBook
from .markdown import STATUS_ZH, fmt, signed
from .metrics import comparison_between, metrics_summary, safe_best, safe_versions

if TYPE_CHECKING:
    from ..core.project import Project


def esc(v: Any) -> str:
    return html.escape(fmt(v), quote=True)


# Colours: categorical slots 1 (blue) and 2 (orange) of the validated default palette, with
# per-mode steps; text always wears ink tokens, identity is carried by mark colour + shape + label.
_CSS = """
:root{--plane:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;
--axis:#c3c2b7;--s1:#2a78d6;--s2:#eb6834;--good:#0ca30c;--crit:#d03b3b;--warn:#fab219}
@media (prefers-color-scheme:dark){:root{--plane:#0d0d0d;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;
--grid:#2c2c2a;--axis:#383835;--s1:#3987e5;--s2:#d95926}}
*{box-sizing:border-box}
body{margin:0;background:var(--plane);color:var(--ink);font:15px/1.55 -apple-system,BlinkMacSystemFont,
"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}
main{max-width:1040px;margin:0 auto;padding:24px 16px 56px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:0 0 12px}h3{font-size:14px;margin:16px 0 6px;color:var(--ink2)}
.sub{color:var(--ink2);margin:0 0 20px;font-size:13px}
section{background:var(--surface);border:1px solid var(--grid);border-radius:10px;padding:18px;margin:0 0 16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{border:1px solid var(--grid);border-radius:8px;padding:12px}
.tile b{display:block;font-size:22px;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.tile span{color:var(--ink2);font-size:12px}
.scroll{overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--grid);vertical-align:top}
th{color:var(--ink2);font-weight:600;white-space:nowrap}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.badge{display:inline-block;border:1px solid var(--axis);border-radius:999px;padding:0 8px;font-size:12px;white-space:nowrap}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;background:var(--muted)}
.dot.good{background:var(--good)}.dot.crit{background:var(--crit)}.dot.warn{background:var(--warn)}
.legend{display:flex;gap:18px;flex-wrap:wrap;font-size:13px;color:var(--ink2);margin:0 0 8px}
.legend i{display:inline-block;width:18px;height:0;border-top:2px solid;vertical-align:middle;margin-right:6px}
svg{display:block;width:100%;height:auto}
svg text{fill:var(--muted);font-size:11px}svg .lbl{fill:var(--ink2);font-size:12px}
.hit{fill:transparent}.hit:hover+.pt{stroke-width:4}
pre{background:var(--plane);border:1px solid var(--grid);border-radius:6px;padding:10px;overflow-x:auto;
font-size:12px;margin:6px 0;white-space:pre-wrap;overflow-wrap:anywhere}
details{border-top:1px solid var(--grid);padding:8px 0}summary{cursor:pointer}
.empty{color:var(--muted)}ul{margin:4px 0;padding-left:20px}code{font-size:12px}
"""


def _table(headers: list[tuple[str, bool]], rows: list[list[Any]]) -> str:
    """``headers`` = [(label, is_numeric)]; cells are escaped unless already wrapped in :class:`Raw`."""
    if not rows:
        return '<p class="empty">（无数据）</p>'
    head = "".join(f'<th class="{"n" if n else ""}">{esc(h)}</th>' for h, n in headers)
    body = ""
    for row in rows:
        body += (
            "<tr>"
            + "".join(
                f'<td class="{"n" if headers[i][1] else ""}">{c if isinstance(c, Raw) else esc(c)}</td>'
                for i, c in enumerate(row)
            )
            + "</tr>"
        )
    return f'<div class="scroll"><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


class Raw(str):
    """Marks a string as already-safe HTML."""


def _badge(status: str) -> Raw:
    tone = {
        "best": "good",
        "fixed": "good",
        "accepted": "good",
        "rejected": "crit",
        "regressed": "crit",
        "open": "warn",
    }.get(status, "")
    return Raw(f'<span class="badge"><span class="dot {tone}"></span>{esc(STATUS_ZH.get(status, status))}</span>')


# --------------------------------------------------------------------------- chart


def trend_chart(trend: list[dict[str, Any]]) -> str:
    """Inline SVG line chart: best val score and train score per iteration (one shared 0-1 axis)."""
    series = [("最佳分 (val)", "best_score", "var(--s1)", "circle"), ("训练分", "train_score", "var(--s2)", "rect")]
    pts = {
        key: [(i, float(t[key])) for i, t in enumerate(trend) if isinstance(t.get(key), (int, float))]
        for _, key, _, _ in series
    }
    if not any(pts.values()):
        return '<p class="empty">（暂无迭代数据）</p>'
    w, h, left, right, top, bottom = 720, 260, 44, 96, 14, 34
    n = len(trend)
    hi = max(1.0, max(v for p in pts.values() for _, v in p))

    def x(i: int) -> float:
        return left + (w - left - right) * (i / (n - 1) if n > 1 else 0.5)

    def y(v: float) -> float:
        return top + (h - top - bottom) * (1 - max(0.0, v) / hi)

    out = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="每轮迭代的最佳分与训练分">']
    for g in range(5):
        v = hi * g / 4
        out.append(
            f'<line x1="{left}" x2="{w - right}" y1="{y(v):.1f}" y2="{y(v):.1f}" '
            f'stroke="var(--{"axis" if g == 0 else "grid"})" stroke-width="1"/>'
            f'<text x="{left - 8}" y="{y(v) + 4:.1f}" text-anchor="end">{v:.2f}</text>'
        )
    step = max(1, n // 12)
    for i, t in enumerate(trend):
        if i % step == 0 or i == n - 1:
            out.append(f'<text x="{x(i):.1f}" y="{h - 12}" text-anchor="middle">{esc(t.get("iteration", i))}</text>')
    out.append(f'<text x="{(left + w - right) / 2:.1f}" y="{h}" text-anchor="middle">迭代</text>')
    label_ys: list[float] = []
    for name, key, color, shape in series:
        p = pts[key]
        if not p:
            continue
        if len(p) > 1:
            path = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in p)
            out.append(
                f'<polyline points="{path}" fill="none" stroke="{color}" stroke-width="2" '
                'stroke-linejoin="round" stroke-linecap="round"/>'
            )
        for i, v in p:
            cx, cy = x(i), y(v)
            tip = f"{name} · 迭代 {fmt(trend[i].get('iteration', i))}：{v:.3f}"
            if key == "best_score" and trend[i].get("best_version"):
                tip += f"（{fmt(trend[i]['best_version'])}）"
            mark = (
                f'<circle class="pt" cx="{cx:.1f}" cy="{cy:.1f}" r="4" fill="{color}" stroke="var(--surface)" stroke-width="2"/>'
                if shape == "circle"
                else f'<rect class="pt" x="{cx - 4:.1f}" y="{cy - 4:.1f}" width="8" height="8" fill="{color}" stroke="var(--surface)" stroke-width="2"/>'
            )
            out.append(
                f"<g><title>{html.escape(tip)}</title>"
                f'<circle class="hit" cx="{cx:.1f}" cy="{cy:.1f}" r="12"/>{mark}</g>'
            )
        i, v = p[-1]
        ly = y(v) + 4
        while any(abs(ly - o) < 14 for o in label_ys):  # keep the two end labels from colliding
            ly += 14
        label_ys.append(ly)
        out.append(f'<text class="lbl" x="{x(i) + 10:.1f}" y="{ly:.1f}">{esc(name.split(" ")[0])} {v:.2f}</text>')
    out.append("</svg>")
    legend = "".join(f'<span><i style="border-color:{c}"></i>{esc(nm)}</span>' for nm, k, c, _ in series if pts[k])
    return f'<div class="legend">{legend}</div>' + "".join(out)


# --------------------------------------------------------------------------- sections


def _comparison_html(title: str, cmp: Comparison | None) -> str:
    if cmp is None or not cmp.n_common:
        return f'<h3>{esc(title)}</h3><p class="empty">（没有可比较的共同评测）</p>'
    verdict = {"better": "更好", "worse": "更差", "same": "持平"}[cmp.verdict]
    parts = [
        f"<h3>{esc(title)}</h3>",
        f"<p><code>{esc(cmp.metric)}</code>（{esc(cmp.split)} 集，{cmp.n_common} 个共同用例）："
        f"<b>{esc(cmp.base_score)}</b> → <b>{esc(cmp.cand_score)}</b>，Δ <b>{esc(signed(cmp.delta))}</b>（{verdict}）；"
        f"95% CI [{esc(signed(cmp.ci_low))}, {esc(signed(cmp.ci_high))}]，p = {esc(cmp.p_value)}</p>",
        _table(
            [("修复", True), ("回退", True), ("提升", True), ("下降", True), ("保持通过", True), ("保持失败", True)],
            [
                [
                    len(cmp.fixed),
                    len(cmp.regressed),
                    len(cmp.improved),
                    len(cmp.worsened),
                    len(cmp.unchanged_pass),
                    len(cmp.unchanged_fail),
                ]
            ],
        ),
    ]
    for label, ids in (("修复", cmp.fixed), ("回退", cmp.regressed)):
        if ids:
            parts.append(f"<p>{label}：" + "、".join(f"<code>{esc(c)}</code>" for c in ids) + "</p>")
    if cmp.by_tag:
        parts.append(
            _table(
                [("标签", False), ("用例数", True), ("基线", True), ("候选", True), ("Δ", True)],
                [
                    [t, int(d.get("n", 0)), d.get("base"), d.get("cand"), signed(d.get("delta"))]
                    for t, d in cmp.by_tag.items()
                ],
            )
        )
    return "".join(parts)


def _gate_html(version: SkillVersion) -> str:
    gate = version.gate if isinstance(version.gate, dict) else {}
    checks = [c for c in gate.get("checks") or [] if isinstance(c, dict)]
    if not checks and not gate.get("reasons"):
        return ""
    out = f"<h3>提升闸门（{esc(version.id)}）</h3>"
    if checks:
        out += _table(
            [("检查项", False), ("结果", False), ("说明", False)],
            [[c.get("name"), "✓ 通过" if c.get("passed") else "✗ 未通过", c.get("detail")] for c in checks],
        )
    if gate.get("reasons"):
        out += "<ul>" + "".join(f"<li>{esc(r)}</li>" for r in gate["reasons"]) + "</ul>"
    return out


def render_html_report(project: Project) -> str:
    m = metrics_summary(project)
    metric = m["metric"]
    versions = safe_versions(project)
    by_id = {v.id: v for v in versions}
    best = safe_best(project)
    try:
        book = ErrorBook(project.errorbook_path)
        open_entries = book.open_entries()
    except (OSError, ValueError):
        open_entries = []
    eb = m["errorbook"] or {}

    def score(scores: dict[str, Any], split: str) -> Any:
        return (scores.get(split) or {}).get(metric)

    best_val = score(m["best_scores"], "val")
    tiles = [
        ("最佳版本", m["best_version"] or "—"),
        (f"val {metric}", best_val),
        ("相对首版 Δ", signed(m["improvement"].get("val")) if "val" in m["improvement"] else "—"),
        ("版本数", m["n_versions"]),
        ("提升 / 拒绝", f"{m['promotions']} / {m['rejections']}"),
        ("未解决错题", (eb.get("open", 0) or 0) + (eb.get("regressed", 0) or 0)),
        ("评测运行数", m["totals"]["n_runs"]),
        ("成本 (USD)", m["totals"]["cost_usd"]),
    ]
    body = [
        f"<h1>Skill 进化报告：{esc(m['project'])}</h1>",
        f'<p class="sub">生成于 {esc(time.strftime("%Y-%m-%d %H:%M", time.localtime(m["generated_at"])))}'
        f" · 主指标 <code>{esc(metric)}</code></p>",
        '<section><h2>指标摘要</h2><div class="tiles">'
        + "".join(f'<div class="tile"><b>{esc(v)}</b><span>{esc(k)}</span></div>' for k, v in tiles)
        + "</div></section>",
    ]

    body.append("<section><h2>迭代趋势</h2>" + trend_chart(m["trend"]))
    if m["trend"]:
        body.append(
            "<details><summary>数据表</summary>"
            + _table(
                [("迭代", True), ("最佳版本", False), ("最佳分 (val)", True), ("训练分", True)],
                [[t["iteration"], t["best_version"], t["best_score"], t["train_score"]] for t in m["trend"]],
            )
            + "</details>"
        )
    body.append("</section>")

    body.append(
        "<section><h2>版本谱系</h2>"
        + _table(
            [
                ("版本", False),
                ("父版本", False),
                ("来源", False),
                ("状态", False),
                ("迭代", True),
                (f"val {metric}", True),
                ("val 通过率", True),
                (f"train {metric}", True),
                (f"test {metric}", True),
                ("变更摘要", False),
            ],
            [
                [
                    v["id"],
                    v["parent"] or "—",
                    v["origin"],
                    _badge(v["status"]),
                    v["iteration"],
                    score(v["scores"], "val"),
                    (v["scores"].get("val") or {}).get("pass_rate"),
                    score(v["scores"], "train"),
                    score(v["scores"], "test"),
                    v["summary"],
                ]
                for v in m["versions"]
            ],
        )
        + "</section>"
    )

    cmp_html = ""
    if best is not None:
        parent = by_id.get(best.parent)
        first = versions[0] if versions else None
        if parent is not None:
            cmp_html += _comparison_html(
                f"{parent.id} → {best.id}（最佳 vs 父版本）", comparison_between(project, parent, best)
            )
        if first is not None and first.id != best.id and (parent is None or first.id != parent.id):
            cmp_html += _comparison_html(
                f"{first.id} → {best.id}（最佳 vs 首版）", comparison_between(project, first, best)
            )
        cmp_html += _gate_html(best)
    body.append("<section><h2>版本比较</h2>" + (cmp_html or '<p class="empty">（尚无可比较的版本）</p>') + "</section>")

    eb_html = (
        f"<p>共 {esc(eb.get('total', 0))} 条：未解决 {esc(eb.get('open', 0))}，回退 {esc(eb.get('regressed', 0))}，"
        f"已修复 {esc(eb.get('fixed', 0))}，不修复 {esc(eb.get('wontfix', 0))}。</p>"
    )
    if eb.get("by_category"):
        eb_html += "<h3>根因分类（未解决）</h3>" + _table(
            [("根因类别", False), ("数量", True)], [[k, v] for k, v in eb["by_category"].items()]
        )
    if open_entries:
        eb_html += "<h3>主要未解决错题</h3>"
        for e in open_entries[:10]:
            eb_html += (
                f"<details><summary><code>{esc(e.case_id)}</code> {_badge(e.status)} "
                f"失败 {e.occurrences} 次 · {esc(e.category or '未分析')}</summary>"
                f"<p>根因：{esc(e.root_cause)}<br>经验：{esc(e.lesson)}<br>建议措施：{esc(e.suggested_fix)}<br>"
                f"判分理由：{esc(e.judge_reason)}</p>"
                f"<b>输入</b><pre>{esc(e.input)}</pre><b>期望</b><pre>{esc(e.expected)}</pre>"
                f"<b>实际输出</b><pre>{esc(e.output)}</pre>"
                + (f"<b>复现</b><pre>{esc(e.repro)}</pre>" if e.repro else "")
                + "</details>"
            )
    body.append("<section><h2>错题库</h2>" + eb_html + "</section>")

    return (
        '<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Skill 进化报告 · {esc(m['project'])}</title><style>{_CSS}</style></head>"
        f"<body><main>{''.join(body)}</main></body></html>\n"
    )
