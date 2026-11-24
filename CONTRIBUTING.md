# Contributing to skillloop

Thanks for helping. This page is the short path from a clone to a merged change.

## Setup

```bash
git clone <your fork>
cd skillloop
make install          # creates .venv and installs the package with dev tools
make check            # ruff + the full test suite; this is exactly what CI runs
```

Python 3.11 or newer. The core has **no runtime dependencies** and we would like to keep it that way:
a new dependency needs a strong reason and belongs in an optional extra.

The whole test suite is offline — simulated agent and LLM, fake CLIs, loopback HTTP servers — and
takes about half a minute. Please keep it that way: a test must not need a network, an API key, or
an installed agent CLI.

## Where things live

```
src/skillloop/
  core/        data models, project, config, versions, jobs, store, sandbox, registries
  adapters/    agent systems under test (one file per system)
  llm/         the framework's own model providers
  eval/        evaluator, judges, human review
  evolve/      the loop: proposer, patch, guard, compare, gate, error book
  devset/      dataset generation pipeline
  report/      output bundle and HTML report
  mcp/  web/   entry layers          service.py   the facade they all share        cli.py
tests/         one file per area; conftest.py has the offline demo project fixture
docs/          user documentation (Chinese)        examples/   runnable projects
```

Dependencies point downwards only, and the entry layers go through `service.py` rather than into
`core/`. [docs/architecture.md](docs/architecture.md) explains the contracts and the reasoning.

## Common contributions

**A new agent adapter.** Subclass `AgentAdapter` in `src/skillloop/adapters/<name>.py`, register it
with `@ADAPTERS.register("<name>")`, add the module to the `ADAPTERS` built-in list in
`core/registry.py`, test it against a fake CLI or local server in `tests/test_adapters.py`, and
document its options in `docs/adapters.md`. In the PR, say what you verified against the real
system and what only against fakes — `docs/adapters.md` records that distinction per adapter.
[examples/custom-adapter](examples/custom-adapter) is a complete adapter in one file.

**A new judge or LLM provider.** Same shape: `eval/judges.py` with `@JUDGES.register`, or
`llm/<name>.py` with `@LLMS.register`.

**Changing the loop** (`evolve/`). The simulated world (`adapters/mock.py` + `llm/mock.py`) exists
so that promotion, rejection, regression, resume and cancel can each be exercised by a real run in
milliseconds. Add a test in `tests/test_loop.py` that reaches your new branch that way rather than
by stubbing internals.

## Conventions

- `make format` before committing (ruff, line length 120). `pre-commit install` does it for you.
- Public functions and classes get a docstring that says what they are for; comments explain *why*.
- Adapters never raise for agent-side failures; they return a `Trajectory` with a non-`ok` status.
- Anything that writes on behalf of a project goes through the project sandbox (`core/sandbox.py`).
- Prompts live next to the code that uses them and carry their data in `<tag>` blocks (`llm.base.tagged`).
- User-facing behaviour or config changes come with a docs update and a line in `CHANGELOG.md`.

## Pull requests

Small and focused beats large. Describe what changed and how you verified it; link the issue if
there is one. CI must be green. By contributing you agree that your contribution is licensed under
the [Apache License 2.0](LICENSE).

## Reporting bugs and security issues

Bugs: open an issue with the smallest project that reproduces it (the offline demo if possible).
Security problems: please do **not** open a public issue — see [SECURITY.md](SECURITY.md).
