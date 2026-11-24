"""输出层: 最佳 SKILL.md, Review 标注表, 错题库, 版本比较报告, 指标摘要, HTML 报告."""

from .html import render_html_report, trend_chart
from .markdown import render_comparison_md, render_errorbook_md, render_gate_md, render_metrics_md
from .metrics import comparison_between, latest_eval, load_history, metrics_summary
from .outputs import write_outputs

__all__ = [
    "comparison_between",
    "latest_eval",
    "load_history",
    "metrics_summary",
    "render_comparison_md",
    "render_errorbook_md",
    "render_gate_md",
    "render_html_report",
    "render_metrics_md",
    "trend_chart",
    "write_outputs",
]
