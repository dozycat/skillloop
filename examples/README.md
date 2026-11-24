# Examples

Each directory is a complete skillloop project: inputs are tracked, results (`.skillloop/`, `outputs/`) are not.

| Example | Agent | Framework LLM | Needs | Shows |
|---|---|---|---|---|
| [custom-adapter](custom-adapter) | `glossary_agent` (in `my_agent.py`) | simulated | nothing | writing your own adapter as a project-local plugin |
| [expense-policy](expense-policy) | a model call via the `claude` CLI | `claude` CLI | a working Claude Code login | a real run: analysts diagnosing real failures, held-out confirmation |

```bash
skillloop doctor -p examples/custom-adapter     # check the setup
skillloop evolve -p examples/custom-adapter     # run the loop
skillloop show   -p examples/custom-adapter     # the evolved SKILL.md
```

There is also a built-in offline demo that needs no files at all: `skillloop init demo --demo`.

## Notes

- **expense-policy** spends model tokens (24 cases × a few evaluations on a small model, plus a
  handful of analyst calls). Its expected answers are computed by `make_dataset.py` from the policy,
  not typed by hand. The requirement document states the full policy, so the analysts can recover it
  in one or two iterations — it demonstrates the mechanics, not a hard optimisation problem.
- **custom-adapter** sets `leak_check = false`: a glossary skill legitimately contains the answers.
  Leave the check on for skills that are meant to teach a procedure.
- To start over, delete the project's `.skillloop/` and `outputs/` directories.
