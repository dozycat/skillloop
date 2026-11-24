# Security Policy

## Reporting a vulnerability

Please report security issues **privately**, not in a public issue: use the repository host's
private vulnerability reporting ("Report a vulnerability" under the Security tab on GitHub).
Include what is affected, how to reproduce it, and the impact you see. You will get an
acknowledgement, and a fix or a mitigation plan before any public disclosure.

Only the latest release receives security fixes while the project is pre-1.0.

## What skillloop does on your machine

skillloop is a local developer tool. Understanding its trust model is part of using it safely:

- **It runs the agent under test.** Evaluating a skill means executing your configured agent
  (Claude Code, Codex, an arbitrary command, an HTTP endpoint...) on every case, with whatever
  permissions that agent has. skillloop gives each case its own working directory, but it does **not**
  sandbox the agent. For untrusted datasets or skills, run the agent in a container or VM and point
  the `command` adapter at it. The `claude_code` adapter never adds `--dangerously-skip-permissions`
  by default.
- **Configuration is code.** `skillloop.toml` can name a command to run (`command` adapter, `script`
  judge), a Python object to import (`"pkg.module:Class"` specs) and project-local plugin modules
  (`[project] plugins`). Treat a project directory from someone else like a repository with a
  Makefile: read it before running it.
- **The web console and MCP server can start jobs and change configuration**, so anyone who can reach
  them can run commands as you. The web console therefore binds to `127.0.0.1` by default, validates
  the `Host` header, rejects cross-origin state-changing requests, and refuses to bind a non-loopback
  address without `--token`. Do not expose it to a network you do not trust; the MCP server speaks
  stdio only.
- **File access is confined to the project.** Paths coming from configuration, the web API or MCP
  tools are resolved (symlinks included) and rejected if they leave the project directory.
- **Model output is treated as data.** Skill edits proposed by an LLM are applied only as declarative
  patches with validation; text from agent runs is passed to analysts inside delimited blocks with an
  instruction to treat it as evidence, and the web console renders all such text escaped. Prompt
  injection through a dataset or an agent transcript can still degrade the *quality* of a proposed
  skill, which is one reason candidates must pass a held-out gate — review a promoted skill before
  shipping it to production agents.
- **Secrets.** skillloop stores no API keys; providers read them from the environment. Evaluation
  records (`.skillloop/`) contain your dataset, agent transcripts and model outputs in plain text —
  keep that directory out of version control (the generated `.gitignore` does) and out of bug reports.

Reports about any of the guarantees above not holding (sandbox escape through a path, the console
accepting a cross-origin or wrong-host request, unescaped rendering, command execution without
going through configuration) are exactly what this policy is for.
