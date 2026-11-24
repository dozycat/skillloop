# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html). While the version is 0.x, minor
releases may change configuration keys and on-disk formats; such changes are listed under
**Changed** with migration notes.

## [Unreleased]

### Added

- `[agent] data_dirs`: project directories of reference material (a document corpus, a code base)
  linked read-only into every case's working directory; `claude_code` also allows the real paths.
- `[agent] task_file`: a fixed task prompt given to the agent with every case and shown to analysts as
  context, kept out of the evolving skill.
- `[dataset]` section: column mapping for tabular datasets (`input_column`, `expected_column`,
  `id_column`, `rubric_column`, `tag_columns`).
- `answer` judge: tolerant short-answer matching (thousands separators, magnitude words, percent,
  relative tolerance with optional partial-credit levels, incidental years ignored, text fallback).
- `[eval] answer_regex`: judge only the final-answer line of a reply; replies without one are
  labelled `no_final_answer`.
- `[eval] max_retries`: re-run cases whose agent errored or timed out, in a clean working directory.
- `[evolve] max_cost_usd`: stop starting iterations once agent runs have cost this much.
- Run cache (`[eval] cache`, on by default): a finished agent run is reused when the same adapter
  settings, skill content, case, prompt and trial come up again, so an interrupted evaluation resumes
  at case granularity. Judging is always redone.
- `[evolve] min_net_fixed`: a gate check requiring real fail→pass flips, so a noisy agent cannot be
  promoted on one lucky trial.
- `skillloop reset`: delete a project's run results, keeping inputs and configuration (`--keep-cache`).
- `skillloop doctor` checks the task file, data directories and dataset mapping.
- `examples/officeqa`: the OfficeQA sample (Treasury bulletin questions) driven through Claude Code.

### Fixed

- The growth guardrail rejected every useful edit of a very short seed skill (a three-line skill
  cannot improve without growing several-fold). The ratio is now measured against a 2000-character
  floor. Found by running the OfficeQA example.

- The error book no longer flips cases between `fixed` and `regressed` when a frontier parent other
  than the best version is evaluated; it now follows the best version only.

### Changed

- Writing cases (`add_cases`, `devset --merge`) to a `.csv` / `.tsv` / `.json` dataset is now an
  explicit error instead of silently overwriting it with JSONL; convert the dataset to `.jsonl` first.

## [0.1.0]

First public version.

### Added

- **Evolution loop**: baseline (seed skill or bootstrapped from the requirement document) →
  evaluate → judge → error book → parallel failure analysis and hierarchical patch merging →
  several candidates per iteration → paired version comparison → promotion gate. Train / val / test
  discipline with a single held-out evaluation at the end; cancellable and resumable.
- **Guardrails** on candidates: format and size validation, growth limit, no-op detection and
  answer-leakage detection (`evolve.leak_check`).
- **Agent adapters**: `claude_code`, `codex`, `nanobot`, `command` (any CLI), `http`, `llm`,
  `callable`, and the simulated `mock` agent. Project-local adapters via `[project] plugins`.
- **Judges**: `exact`, `contains`, `regex`, `numeric`, `json_match`, `script`, `llm`, `composite`,
  `human`; human review queue with CSV export/import and judge–human agreement statistics.
- **LLM providers** for the framework's own calls: `anthropic` (optional extra), `openai`
  (any OpenAI-compatible endpoint), `cli`, and the deterministic `mock`.
- **Dev-set pipeline**: rule extraction → candidate generation → scenario evolution → filtering →
  balancing → report.
- **Versioning**: immutable skill snapshots with manifests, lineage, and a private git history.
- **Entry points**: `skillloop` CLI, web console, MCP server (stdio), and the `SkillLoop` Python facade.
- **Mock MCP profiles**: deterministic fake MCP tools attached to the agent under test, with a call log.
- **Outputs**: best skill bundle, review sheet, error book, version comparison report, metrics
  summary, self-contained HTML report.
- **Observability**: structured per-job logs, audit events, staged progress, artifact index.

### Known limitations

- Verified against real systems: `claude_code` and `codex` (basic runs), the `cli` provider, the MCP
  server with a real MCP client. Not yet verified live: `nanobot` (flags checked against v0.4.2
  `--help` only), the `anthropic` provider (tested against a fake SDK), and Codex tool-call / MCP
  event parsing. The `llm` judge and dev-set prompts have only been run against the mock LLM.
- Jobs run inside the process that started them; a job interrupted by process exit is resumed by
  starting it again (finished evaluations are reused).
- No built-in container isolation for the agent under test.
- macOS and Linux only; Windows is untested.
