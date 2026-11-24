# skillloop

**English** | [简体中文](README.zh-CN.md)

A closed-loop framework for evolving agent skills. Give it a requirement document, an optional
first draft of a `SKILL.md`, and a set of test cases; it evaluates the skill with your agent, learns
from the failures, proposes better versions, and promotes one only when it beats the current best on
held-out cases.

```
bootstrap skill → evaluate → judge (LLM / human) → error book → propose candidates → compare versions ─┐
                      ▲                                                                                │
                      └───────────────────────── next iteration ◄──────────────────────────────────────┘
                                                        │ gate passes
                                                        ▼
                                                 promote best version
```

- **Works with any agent system.** The integration point is one method,
  `AgentAdapter.run(request) -> Trajectory`. Adapters for Claude Code, Codex, nanobot, any CLI,
  any HTTP endpoint, a direct model call and an in-process function are built in.
- **No runtime dependencies.** Pure standard library on Python ≥ 3.11 — including the web console,
  the MCP server and the SQLite state store.
- **Three entry points, one core.** CLI, web console and MCP server all go through the same facade.
- **Runs offline.** A simulated agent and LLM let you watch the whole loop in seconds without an API key.

## Quick start

```bash
pip install -e .                      # from a clone; add '.[anthropic]' to use the Anthropic SDK

skillloop init demo --demo             # offline demo project (simulated agent + LLM)
skillloop evolve -p demo               # run the loop
skillloop versions -p demo             # lineage and scores per split
skillloop show -p demo                 # the best SKILL.md and why it was promoted
open demo/outputs/report.html         # summary report
```

In the demo the simulated analyst occasionally proposes a harmful edit (deleting a rule that
works), so you can see the gate stop it:

```
★ promoted v007: val mean_score 0.750 → 0.875 (1 fixed, 0 regressed)
✗ v006 not promoted: improvement: mean_score 0.750 -> 0.625 (Δ-0.125, need ≥ +0.010); regressions: 2 case(s) regressed (allowed 1)
```

### Your own project

```bash
skillloop init my-skill --adapter claude_code --llm anthropic --judge llm
# 1. my-skill/requirement.md          goals, scope, acceptance criteria, constraints
# 2. my-skill/seed_skill/SKILL.md     optional first draft; generated from the requirement if absent
# 3. my-skill/dataset.jsonl           one case per line: {"input": ..., "expected": ..., "tags": [...]}
#    no dataset yet?  skillloop devset -p my-skill --merge
skillloop doctor -p my-skill           # is the agent / LLM / config / input ready?
skillloop evolve -p my-skill
```

Runnable projects live in [examples/](examples): a document-QA benchmark sample (OfficeQA) run
through Claude Code, a policy-calculation task driven through the local `claude` CLI, and a project
that ships its own adapter in a single file.

### Tasks that need more than a prompt

Real tasks usually come with material and conventions that are not part of the skill:

```toml
[agent]
task_file = "task.md"                    # fixed framing and answer format, given with every case
data_dirs = ["data/treasury_bulletins"]  # a corpus linked read-only into each case's working directory

[dataset]                                # an existing CSV benchmark with its own column names
input_column = "question"
expected_column = "answer"
tag_columns = ["difficulty"]

[eval]
judge = "answer"                         # "36080 million" == "36,080" == "36.08 billion"
answer_regex = 'FINAL ANSWER:\s*(.+)'    # judge the final-answer line, not the whole reply
trials = 3                               # agents are not deterministic; majority of three runs
max_retries = 1                          # re-run on agent error or timeout

[evolve]
max_cost_usd = 5.0                       # stop starting iterations past this agent spend
```

Finished agent runs are cached, so an interrupted evaluation resumes where it stopped and identical
candidates are never paid for twice.

## Plugging in your agent

Three ways, least to most code:

**1. Configure a command** (any CLI agent)

```toml
[agent]
adapter = "command"
[agent.options]
command = ["my-agent", "run", "--skills", "{skill_dir}", "--prompt-file", "{prompt_file}"]
skill_mode = "dir"          # inline | dir | file
output = "last_line"        # text | json | last_line
```

**2. Expose an HTTP endpoint** — `adapter = "http"`: skillloop POSTs the case and the skill and reads
back `{output, steps?, usage?}`.

**3. Write an adapter** (about 20 lines). Put it next to `skillloop.toml`, list the module under
`[project] plugins`, and it is available by name — no packaging needed:

```python
from skillloop import ADAPTERS, AgentAdapter, AgentRequest, Trajectory, Step

@ADAPTERS.register("my_agent")
class MyAgent(AgentAdapter):
    def run(self, request: AgentRequest) -> Trajectory:
        skill_dir = self.install_skill(request.skill, request.workdir / "skills")
        answer, trace = my_agent_sdk.run(request.case.input, skills=skill_dir, cwd=request.workdir,
                                         timeout=request.timeout_s)
        return Trajectory(output=answer, steps=[Step("tool_call", t.text, name=t.tool) for t in trace])
```

Or embed skillloop as a library:

```python
from skillloop import SkillLoop
from skillloop.adapters.callable import FunctionAdapter

es = SkillLoop("my_project", adapter=FunctionAdapter(lambda req: my_agent(req.case.input, req.skill)))
result = es.evolve()
print(result.best_version, es.version()["skill_md"])
```

Judges (`skillloop.judges`) and the framework's own LLM providers (`skillloop.llm`) extend the same way.

## How the loop works

1. **Split.** Cases are split into train / val / test, stratified by tag and stable under additions.
   Analysts only ever see train; candidates are selected on val; test is evaluated once at the end.
2. **Baseline.** The seed skill becomes `v001`, or one is drafted from the requirement document.
3. **Each iteration**
   - evaluate the parent on train; failures go into the **error book** with context and a repro command;
   - **analyse in parallel**: failures are batched to independent analysts, each returning root-cause
     diagnoses and a patch against the same parent;
   - **merge hierarchically**: edits that recur across analysts are kept as evidence of a systematic
     gap, one-off edits are dropped;
   - build **several candidates** with different risk profiles (`full`, `additive`, `focused`, `union`);
   - **guardrails**: format, size growth, no-op, and answer leakage (gold answers copied into the skill);
   - **compare** each candidate with the current best on val, per case: fixed / regressed, per-tag
     breakdown, sign test, bootstrap confidence interval;
   - **gate**: minimum improvement, maximum regressions, protected tags, optional held-out
     confirmation, optional human approval. Every check and its numbers are recorded with the version.
4. **Stop** on target score, patience, max iterations, or cancellation.
5. **Outputs** in `<project>/outputs/`: best skill, review sheet, error book, comparison report,
   metrics summary, HTML report.

State lives on disk, so a run can be cancelled and resumed; finished evaluations are reused.

## Entry points

| | |
|---|---|
| **CLI** | `skillloop --help` — `init` `doctor` `evolve` `eval` `devset` `status` `versions` `show` `diff` `compare` `promote` `errors` `review` `logs` `outputs` … |
| **Web console** | `skillloop web -w ./workspace --open` — projects, config, jobs with live progress and cancel, versions and diffs, evaluations and trajectories, error book, review queue |
| **MCP server** | `skillloop mcp -w ./workspace` (stdio) — about 30 tools so any MCP-capable agent can drive the loop |
| **Python** | `from skillloop import SkillLoop` |

## Documentation

The reference documentation is currently written in Chinese.

| | |
|---|---|
| [Architecture](docs/architecture.md) | layers, the four contracts, design decisions |
| [Configuration](docs/configuration.md) | every key of `skillloop.toml` |
| [Adapters and LLM providers](docs/adapters.md) | built-in adapters, writing your own |
| [Judges and human review](docs/judges.md) | scoring, review queue, calibration |
| [Dev-set pipeline](docs/devset.md) | generating cases from a requirement document |
| [CLI](docs/cli.md) · [Web console](docs/web-console.md) · [MCP](docs/mcp.md) | entry points, REST API, mock MCP profiles |

## Status

Alpha (0.1). The loop, the offline world and the entry points are covered by an offline test suite.
Integrations differ in how far they have been exercised against the real system —
[CHANGELOG.md](CHANGELOG.md) lists what has and has not been verified live. macOS and Linux;
Windows is untested.

Evaluating a skill runs your agent on your machine. Read [SECURITY.md](SECURITY.md) before pointing
skillloop at datasets or projects you do not trust.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). `make install && make check` gets you a working checkout.

## Acknowledgements

The design draws on three pieces of work; the implementation is independent.

- [EvoSkill](https://github.com/sentient-agi/EvoSkill) — learning from failed trajectories, the
  proposer / generator / evaluator split, a frontier of programs, git-backed versions. This project is not affiliated with it.
- [Trace2Skill: Distill Trajectory-Local Lessons into Transferable Agent Skills](https://arxiv.org/abs/2603.25158)
  — parallel analysis, hierarchical patch consolidation, deterministic patch guardrails.
- [Trace2Skill: Verifier-Guided Skill Evolution for Long-Context EDA Agents](https://arxiv.org/abs/2605.21810)
  — pass-rate-dominated selection, regression checks, a lesson bank, auditable artifacts per stage.

## License

[Apache-2.0](LICENSE)
