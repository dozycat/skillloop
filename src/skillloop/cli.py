"""Command line entry point: ``skillloop <command>``. Thin wrapper over :class:`skillloop.service.SkillLoop`."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from .core.config import CONFIG_FILE, Config
from .core.models import error_message
from .core.project import Project
from .core.registry import ADAPTERS, JUDGES, LLMS
from .service import SkillLoop

REQUIREMENT_STUB = """# 需求文档

## 业务目标
(这个 Skill 要让 agent 做成什么事)

## 范围
(包含 / 不包含)

## 验收标准
- (可检查的标准,每条一行)

## 约束
- (必须遵守的限制)
"""


# --------------------------------------------------------------------------- terminal output


class Console:
    """Renders job events: log lines on stdout, a single-line progress bar on stderr when it is a TTY."""

    def __init__(self, quiet: bool = False, log_stream: Any = None):
        self.quiet = quiet
        self.log_stream = log_stream  # None -> stdout; --json sends logs to stderr so stdout stays parseable
        self.tty = sys.stderr.isatty()
        self._bar = False

    def _clear(self) -> None:
        if self._bar:
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()
            self._bar = False

    def __call__(self, rec: dict[str, Any]) -> None:
        if self.quiet:
            return
        if rec["type"] == "progress":
            if not self.tty:
                return
            d = rec["data"]
            total, cur = d.get("total") or 0, d.get("current") or 0
            width = 24
            fill = int(width * cur / total) if total else 0
            bar = "█" * fill + "░" * (width - fill)
            pct = f"{100 * cur / total:5.1f}%" if total else "      "
            sys.stderr.write(f"\r\033[K{bar} {pct} {d.get('stage', ''):<16} {rec['message'][:70]}")
            sys.stderr.flush()
            self._bar = True
        elif rec["type"] in ("log", "job"):
            self._clear()
            mark = {"warning": "! ", "error": "✗ "}.get(rec["level"], "  ")
            text = rec["message"]
            if rec["type"] == "job":
                if text == "started":
                    return
                text = f"job {text}" + (f": {rec['data'].get('error')}" if rec["data"].get("error") else "")
            print(f"{time.strftime('%H:%M:%S')} {mark}{text}", flush=True, file=self.log_stream or sys.stdout)

    def done(self) -> None:
        self._clear()


def _table(headers: list[str], rows: list[list[Any]]) -> str:
    rows = [[("" if c is None else str(c)) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    return "\n".join(
        [line, "  ".join("-" * w for w in widths)] + ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in rows]
    )


def _fmt(x: Any) -> str:
    return "-" if x is None else f"{x:.3f}" if isinstance(x, float) else str(x)


def _score(v: dict[str, Any], split: str) -> str:
    s = v["scores"].get(split)
    return "-" if not s else f"{s['mean_score']:.3f}"


def _out(args: argparse.Namespace, data: Any, text: str) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str) if args.json else text)


def _service(args: argparse.Namespace, console: Console | None = None) -> SkillLoop:
    root = Path(args.project)
    if not (root / CONFIG_FILE).exists():
        sys.exit(
            f"error: {root.resolve()} is not an skillloop project (no {CONFIG_FILE}). "
            f"Run `skillloop init {args.project}` first, or pass -p <dir>."
        )
    return SkillLoop(root, echo=console)


def _run_job(args: argparse.Namespace, kind: str, **params: Any) -> tuple[SkillLoop, dict[str, Any]]:
    console = Console(getattr(args, "quiet", False), sys.stderr if args.json else None)
    es = _service(args, console)
    job = es.run_job(kind, **params)
    console.done()
    if job["status"] != "succeeded":
        es.close()
        sys.exit(f"{kind} {job['status']}: {job['error']}")
    return es, job


# --------------------------------------------------------------------------- commands


def cmd_init(args: argparse.Namespace) -> None:
    root = Path(args.dir)
    if (root / CONFIG_FILE).exists() and not args.force:
        sys.exit(f"error: {root} already contains {CONFIG_FILE} (use --force to overwrite the config)")
    if args.demo:
        from .demo import REQUIREMENT, SEED_SKILL, demo_cases, demo_config

        cfg = demo_config()
        if args.name:
            cfg.project.name = args.name
        p = Project.init(
            root, config=cfg, requirement=REQUIREMENT, seed_skill=SEED_SKILL, cases=demo_cases(), overwrite=args.force
        )
        print(
            f"Demo project created in {p.root} (simulated agent + LLM, runs offline).\n"
            f"Next:  skillloop evolve -p {args.dir}"
        )
        return
    cfg = Config()
    cfg.agent.adapter = args.adapter
    cfg.llm.provider = args.llm
    if args.judge:
        cfg.eval.judge = args.judge
    p = Project.init(root, args.name, config=cfg, requirement=REQUIREMENT_STUB, overwrite=args.force)
    if not p.path(cfg.project.dataset).exists():
        p.path(cfg.project.dataset).write_text("", encoding="utf-8")
    print(
        f"Project created in {p.root}\n"
        f"  1. write the requirement:   {cfg.project.requirement}\n"
        f"  2. (optional) seed skill:    {cfg.project.seed_skill}/SKILL.md\n"
        f"  3. add cases to:             {cfg.project.dataset}   (or run `skillloop devset --merge`)\n"
        f"  4. check the setup:          skillloop doctor -p {args.dir}\n"
        f"  5. run the loop:             skillloop evolve -p {args.dir}"
    )


def cmd_evolve(args: argparse.Namespace) -> None:
    es, job = _run_job(args, "evolve", max_iterations=args.iterations)
    r = job["result"]
    metric = es.project.config.evolve.metric
    if args.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        rows = []
        for split in ("train", "val", "test"):
            b, k = r["baseline_scores"].get(split), r["best_scores"].get(split)
            if b or k:
                rows.append(
                    [
                        split,
                        (b or k)["n_cases"],
                        _fmt((b or {}).get(metric)),
                        _fmt((k or {}).get(metric)),
                        f"{k[metric] - b[metric]:+.3f}" if b and k else "-",
                    ]
                )
        print(
            f"\nBest version: {r['best_version']}  (baseline {r['baseline_version']}, "
            f"{r['iterations']} iteration(s), {len(r['promotions'])} promotion(s))\nStopped: {r['stop_reason']}\n"
        )
        print(_table(["split", "cases", f"baseline {metric}", f"best {metric}", "Δ"], rows))
        if r["pending_approval"]:
            print(f"\nAwaiting approval: {', '.join(r['pending_approval'])}  →  skillloop promote <version>")
        outs = {k: v for k, v in r.get("outputs", {}).items() if k != "skipped"}
        if outs:
            print("\nOutputs:\n" + "\n".join(f"  {k:<18} {v}" for k, v in outs.items()))
    es.close()


def cmd_eval(args: argparse.Namespace) -> None:
    es, job = _run_job(args, "evaluate", version=args.version, split=args.split, case_ids=args.case or None)
    s = job["result"]
    _out(
        args,
        s,
        f"\n{s['skill_version']} on {s['label']}: pass rate {s['pass_rate']:.1%}, "
        f"mean score {s['mean_score']:.3f} over {s['n_cases']} cases  (eval {s['eval_id']})",
    )
    es.close()


def cmd_devset(args: argparse.Namespace) -> None:
    es, job = _run_job(args, "devset", merge=args.merge)
    r = job["result"]
    _out(
        args,
        r,
        f"\n{r['n_rules']} rules → {r['n_cases']} cases kept ({r['n_dropped']} dropped)"
        + (", merged into the dataset" if r["merged"] else "")
        + f"\nReport: {r['paths'].get('report', '')}",
    )
    es.close()


def cmd_bootstrap(args: argparse.Namespace) -> None:
    es, job = _run_job(args, "bootstrap")
    v = job["result"]
    _out(args, v, f"Initial skill registered as {v['id']} ({v['origin']})")
    es.close()


def cmd_outputs(args: argparse.Namespace) -> None:
    es, job = _run_job(args, "outputs")
    _out(args, job["result"], "\n".join(f"{k:<18} {v}" for k, v in job["result"].items()))
    es.close()


def cmd_status(args: argparse.Namespace) -> None:
    es = _service(args)
    info = es.info()
    lines = [
        f"Project   {info['name']}  ({info['root']})",
        f"Inputs    requirement: {'yes' if info['has_requirement'] else 'MISSING'}   "
        f"seed skill: {'yes' if info['has_seed_skill'] else 'none (will bootstrap)'}   cases: {info['n_cases']} "
        + (
            f"(train {info['splits']['train']} / val {info['splits']['val']} / test {info['splits']['test']})"
            if info["splits"]
            else ""
        ),
        f"Setup     agent: {info['agent']}   llm: {info['llm']}   judge: {info['judge']}",
        f"Versions  {info['n_versions']}   best: {info['best_version'] or '-'}",
    ]
    for split, s in info["best_scores"].items():
        lines.append(
            f"            {split:<5} pass {s['pass_rate']:.1%}  mean {s['mean_score']:.3f}  ({s['n_cases']} cases)"
        )
    eb = info["errorbook"]
    lines.append(f"Errors    open {eb['open']}  regressed {eb['regressed']}  fixed {eb['fixed']}")
    if info["pending_reviews"]:
        lines.append(f"Reviews   {info['pending_reviews']} waiting  →  skillloop review export")
    if info["pending_approval"]:
        lines.append(f"Approval  {', '.join(info['pending_approval'])} waiting  →  skillloop promote <version>")
    _out(args, info, "\n".join(lines))
    es.close()


def cmd_doctor(args: argparse.Namespace) -> None:
    es = _service(args)
    h = es.health()
    info = es.info()
    checks = [
        ("agent adapter", h["agent"]["ok"], f"{info['agent']}: {h['agent']['message']}"),
        ("framework LLM", h["llm"]["ok"], f"{info['llm']}: {h['llm']['message']}"),
        ("config", not h["config_problems"], "; ".join(h["config_problems"]) or "valid"),
        (
            "requirement",
            info["has_requirement"],
            "present" if info["has_requirement"] else "requirement document is empty",
        ),
        (
            "dataset",
            info["n_cases"] >= 4,
            f"{info['n_cases']} cases" + ("" if info["n_cases"] >= 4 else " (need a few to split train/val)"),
        ),
    ]
    cfg = es.project.config.agent
    if cfg.task_file or cfg.data_dirs or h["input_problems"]:
        described = ", ".join(filter(None, [cfg.task_file, *cfg.data_dirs])) or "ok"
        checks.append(("task inputs", not h["input_problems"], "; ".join(h["input_problems"]) or described))
    _out(args, h, "\n".join(f"{'✓' if ok else '✗'} {name:<14} {msg}" for name, ok, msg in checks))
    es.close()
    if not all(ok for _, ok, _ in checks):
        sys.exit(1)


def cmd_versions(args: argparse.Namespace) -> None:
    es = _service(args)
    vs = es.versions()
    rows = [
        [
            ("★ " if v["status"] == "best" else "  ") + v["id"],
            v["parent"] or "-",
            v["origin"],
            v["status"],
            _score(v, "train"),
            _score(v, "val"),
            _score(v, "test"),
            v["summary"][:60],
        ]
        for v in vs
    ]
    _out(args, vs, _table(["version", "parent", "origin", "status", "train", "val", "test", "summary"], rows))
    es.close()


def cmd_show(args: argparse.Namespace) -> None:
    es = _service(args)
    v = es.version(args.version)
    if args.json:
        print(json.dumps(v, ensure_ascii=False, indent=2))
    else:
        print(f"# {v['id']}  status={v['status']}  lineage={' → '.join(v['lineage'])}\n# {v['summary']}\n")
        for c in v["gate"].get("checks", []):
            print(f"#   {'✓' if c['passed'] else '✗'} {c['name']}: {c['detail']}")
        print(v["skill_md"])
    es.close()


def cmd_diff(args: argparse.Namespace) -> None:
    es = _service(args)
    print(es.diff(args.base, args.cand) or "(no differences)")
    es.close()


def cmd_compare(args: argparse.Namespace) -> None:
    console = Console(log_stream=sys.stderr if args.json else None)
    es = _service(args, console)
    cmp = es.compare(args.base, args.cand, args.split)
    console.done()
    if args.json:
        print(json.dumps(cmp.to_dict(), ensure_ascii=False, indent=2))
    else:
        from .report import render_comparison_md

        print(render_comparison_md(cmp, cmp.base_version, cmp.cand_version))
    es.close()


def cmd_promote(args: argparse.Namespace) -> None:
    es = _service(args)
    v = es.promote(args.version, args.reason, by="cli")
    print(f"{v.id} is now the best version")
    es.close()


def cmd_reject(args: argparse.Namespace) -> None:
    es = _service(args)
    print(f"{es.reject(args.version, args.reason).id} rejected")
    es.close()


def cmd_import(args: argparse.Namespace) -> None:
    es = _service(args)
    v = es.import_skill(Path(args.path).resolve(), args.summary)
    print(
        f"imported as {v.id} (parent {v.parent or '-'}); evaluate it with `skillloop eval --version {v.id} --split val`"
    )
    es.close()


def cmd_export(args: argparse.Namespace) -> None:
    es = _service(args)
    v = es._resolve(args.version)
    dest = es.project.versions.export(v.id, Path(args.dest).resolve())
    print(f"{v.id} exported to {dest}")
    es.close()


def cmd_errors(args: argparse.Namespace) -> None:
    es = _service(args)
    entries, stats = es.errors(args.status), es.error_stats()
    rows = [
        [e["case_id"], e["status"], e["occurrences"], e["category"] or "-", (e["root_cause"] or e["judge_reason"])[:70]]
        for e in entries[: args.limit]
    ]
    text = (
        f"open {stats['open']}  regressed {stats['regressed']}  fixed {stats['fixed']}  total {stats['total']}\n"
        + (f"root causes: {stats['by_category']}\n" if stats["by_category"] else "")
        + "\n"
        + _table(["case", "status", "fails", "category", "root cause"], rows)
    )
    _out(args, {"stats": stats, "entries": entries}, text)
    es.close()


def cmd_review(args: argparse.Namespace) -> None:
    es = _service(args)
    if args.action == "export":
        print(
            f"review sheet written to {es.export_review_sheet(args.eval, Path(args.file).resolve() if args.file else '')}\n"
            f"fill human_passed / human_score / comment, then: skillloop review import <file>"
        )
    elif args.action == "import":
        if not args.file:
            sys.exit("error: review import needs a file")
        r = es.import_review_sheet(Path(args.file).resolve())
        print(f"imported {r['imported']} review(s); {len(r['skipped'])} skipped")
        for s in r["skipped"]:
            print(f"  row {s['row']}: {s['reason']}")
    else:
        rows = [
            [r["eval_id"], r["case_id"], r["trial"], r["status"], _fmt(r["auto_score"]), r["auto_reason"][:60]]
            for r in es.reviews(args.status)
        ]
        _out(args, es.reviews(args.status), _table(["eval", "case", "trial", "status", "auto", "reason"], rows))
    es.close()


def cmd_jobs(args: argparse.Namespace) -> None:
    es = _service(args)
    jobs = es.jobs.list(args.limit)
    rows = [
        [
            j["id"],
            j["kind"],
            j["status"],
            time.strftime("%m-%d %H:%M", time.localtime(j["created_at"])),
            j["error"][:60],
        ]
        for j in jobs
    ]
    _out(args, jobs, _table(["job", "kind", "status", "created", "error"], rows))
    es.close()


def cmd_logs(args: argparse.Namespace) -> None:
    es = _service(args)
    job_id = args.job or next((j["id"] for j in es.jobs.list(1)), None)
    events = es.project.store.tail_events(args.limit, job_id=job_id, type="audit" if args.audit else "")
    for e in events:
        if e["type"] == "progress" and not args.all:
            continue
        extra = f" {json.dumps(e['data'], ensure_ascii=False)}" if e["type"] == "audit" else ""
        print(
            f"{time.strftime('%H:%M:%S', time.localtime(e['ts']))} [{e['level']:<7}] {e['type']:<5} {e['message']}{extra}"
        )
    es.close()


def cmd_reset(args: argparse.Namespace) -> None:
    """Delete everything a project's runs produced, keeping its inputs and configuration."""
    import shutil

    root = Path(args.project).resolve()
    if not (root / CONFIG_FILE).exists():
        sys.exit(f"error: {root} is not a skillloop project (no {CONFIG_FILE})")
    state, outputs = root / ".skillloop", root / "outputs"
    cache = state / "cache"
    targets = [p for p in (state, outputs) if p.exists()]
    if not targets:
        print("nothing to reset")
        return
    what = "versions, evaluations, error book, logs" + ("" if args.keep_cache else ", run cache") + ", outputs"
    if not args.yes:
        if not sys.stdin.isatty():
            sys.exit("error: reset deletes run results; pass --yes to confirm in a non-interactive shell")
        answer = input(f"Delete {what} of {root.name}? Inputs and config are kept. [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            print("aborted")
            return
    kept = None
    if args.keep_cache and cache.exists():
        kept = root / ".skillloop-cache-keep"
        shutil.rmtree(kept, ignore_errors=True)
        cache.rename(kept)
    for p in targets:
        shutil.rmtree(p)
    if kept is not None:
        state.mkdir()
        kept.rename(cache)
    print(f"reset {root.name}: removed {what}")


def cmd_plugins(args: argparse.Namespace) -> None:
    print("agent adapters: " + ", ".join(ADAPTERS.names()))
    print("judges:         " + ", ".join(JUDGES.names()))
    print("llm providers:  " + ", ".join(LLMS.names()))


def cmd_web(args: argparse.Namespace) -> None:
    from .web.server import serve

    serve(args.workspace, host=args.host, port=args.port, token=args.token, open_browser=args.open)


def cmd_mcp(args: argparse.Namespace) -> None:
    from .mcp.server import serve

    serve(args.workspace)


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-p", "--project", default=".", help="project directory (default: current directory)")
    common.add_argument("--json", action="store_true", help="machine-readable output")
    parser = argparse.ArgumentParser(
        prog="skillloop",
        description="Skill 进化闭环: evaluate → judge → error book → "
        "iterate → compare → promote. Works with any agent system via adapters.",
    )
    parser.add_argument("--version", action="version", version=f"skillloop {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    def add(name: str, fn, help: str, project: bool = True) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help, description=help, parents=[common] if project else [])
        p.set_defaults(fn=fn)
        return p

    p = add("init", cmd_init, "create a project directory", project=False)
    p.add_argument("dir", nargs="?", default=".")
    p.add_argument("--name", default="")
    p.add_argument("--demo", action="store_true", help="offline demo project (simulated agent and LLM)")
    p.add_argument("--adapter", default="mock", help="agent adapter (see `skillloop plugins`)")
    p.add_argument("--llm", default="mock", help="LLM provider for judging/analysis")
    p.add_argument("--judge", default="", help="judge name (default: exact)")
    p.add_argument("--force", action="store_true")

    p = add("evolve", cmd_evolve, "run the evolution loop")
    p.add_argument("-n", "--iterations", type=int, default=None, help="max iterations (default: config)")
    p.add_argument("-q", "--quiet", action="store_true")

    p = add("eval", cmd_eval, "evaluate a skill version")
    p.add_argument("--version", default="", help='"v003", "best" (default) or "latest"')
    p.add_argument("--split", default="", choices=["", "train", "val", "test"])
    p.add_argument("--case", action="append", help="evaluate only this case id (repeatable)")
    p.add_argument("-q", "--quiet", action="store_true")

    p = add("devset", cmd_devset, "build an evaluation set from the requirement document")
    p.add_argument("--merge", action="store_true", help="append the result to the project dataset")
    p.add_argument("-q", "--quiet", action="store_true")

    add("bootstrap", cmd_bootstrap, "register the seed skill or generate the first version")
    add("outputs", cmd_outputs, "write the output bundle (best skill, reports, error book)")
    add("status", cmd_status, "project overview")
    add("doctor", cmd_doctor, "check that the agent, LLM, config and inputs are ready")
    add("versions", cmd_versions, "list skill versions")

    p = add("show", cmd_show, "print a version's SKILL.md and gate decision")
    p.add_argument("version", nargs="?", default="")
    p = add("diff", cmd_diff, "diff two versions")
    p.add_argument("base")
    p.add_argument("cand")
    p = add("compare", cmd_compare, "paired comparison of two versions on a split")
    p.add_argument("base")
    p.add_argument("cand")
    p.add_argument("--split", default="val", choices=["train", "val", "test"])
    p = add("promote", cmd_promote, "promote a version to best (also approves a pending candidate)")
    p.add_argument("version")
    p.add_argument("--reason", default="manual promotion")
    p = add("reject", cmd_reject, "mark a version rejected")
    p.add_argument("version")
    p.add_argument("--reason", default="")
    p = add("import", cmd_import, "register a hand-edited skill (dir or SKILL.md) as a new version")
    p.add_argument("path")
    p.add_argument("--summary", default="manual edit")
    p = add("export", cmd_export, "copy a version's skill bundle to a directory")
    p.add_argument("dest")
    p.add_argument("--version", default="")

    p = add("errors", cmd_errors, "show the error book")
    p.add_argument("--status", default="", choices=["", "open", "fixed", "regressed", "wontfix"])
    p.add_argument("--limit", type=int, default=40)
    p = add("review", cmd_review, "human review queue: list | export | import")
    p.add_argument("action", choices=["list", "export", "import"])
    p.add_argument("file", nargs="?", default="")
    p.add_argument("--eval", default="", help="evaluation id (default: most recent)")
    p.add_argument("--status", default="pending", choices=["", "pending", "done"])
    p = add("jobs", cmd_jobs, "recent jobs")
    p.add_argument("--limit", type=int, default=20)
    p = add("logs", cmd_logs, "structured log of a job (default: the most recent)")
    p.add_argument("job", nargs="?", default="")
    p.add_argument("--limit", type=int, default=200)
    p.add_argument("--audit", action="store_true", help="only state-changing operations")
    p.add_argument("--all", action="store_true", help="include progress events")

    p = add("reset", cmd_reset, "delete a project's run results (versions, evals, outputs); inputs are kept")
    p.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--keep-cache", action="store_true", help="keep cached agent runs so a re-run is cheap")

    add("plugins", cmd_plugins, "list registered adapters, judges and LLM providers", project=False)

    p = add("web", cmd_web, "start the web console", project=False)
    p.add_argument("-w", "--workspace", default=".", help="a project, or a directory of projects")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--token", default="", help="require this bearer token (mandatory for non-loopback hosts)")
    p.add_argument("--open", action="store_true", help="open the browser")
    p = add("mcp", cmd_mcp, "start the MCP server (stdio)", project=False)
    p.add_argument("-w", "--workspace", default=".", help="a project, or a directory of projects")
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
    except KeyboardInterrupt:
        sys.exit(130)
    except BrokenPipeError:
        sys.exit(0)
    except (KeyError, ValueError, FileNotFoundError, RuntimeError) as e:
        sys.exit(f"error: {error_message(e)}")


if __name__ == "__main__":
    main()
