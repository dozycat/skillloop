# OfficeQA sample

Ten questions about U.S. federal finances (1939–2003) that an agent has to answer by finding the right
table in nine large Treasury Bulletin text files (0.2–1.2 MB each). The sample comes from the
[EvoSkill](https://github.com/sentient-agi/EvoSkill) repository's `examples/officeqa` and is downloaded
on demand, not stored here.

It is the reference example for tasks that need more than a prompt: a shared document corpus, an
existing CSV with its own column names, a fixed answer format, numeric answers that must be compared
tolerantly, and an agent that is not deterministic.

## Run it

Needs a working Claude Code login (`claude` on your PATH). A full run costs well under a dollar with
the configured small model.

```bash
python examples/officeqa/fetch_data.py         # 10 questions + 9 documents into examples/officeqa/data/
skillloop doctor -p examples/officeqa          # agent, LLM, config, dataset, task file, data dirs
skillloop eval   -p examples/officeqa --case UID0045    # one question, about 15 seconds
skillloop evolve -p examples/officeqa
skillloop show   -p examples/officeqa          # the evolved SKILL.md and the gate's reasons
skillloop reset  -p examples/officeqa --keep-cache      # start over without paying for finished runs again
```

## What the configuration shows

| In `skillloop.toml` | Why it is there |
|---|---|
| `[dataset] input_column / expected_column / id_column / tag_columns` | the CSV's columns are `question`, `answer`, `uid`, `difficulty`; the difficulty also stratifies the split |
| `[agent] data_dirs` | the bulletins are linked into each case's working directory instead of being copied 10 × 3 times |
| `[agent] task_file` | the role, "search, do not read end to end", and the `FINAL ANSWER:` line are true for every skill version, so they are not part of the skill and cannot be evolved away |
| `[eval] answer_regex` + `judge = "answer"` | only the final-answer line is judged, and `36080 million`, `36,080` and `36.08 billion` count as the same answer |
| `[eval] trials = 3` | the agent gives different answers to the same question on different runs; a case passes on a majority |
| `[eval] max_retries` | a timed-out or crashed agent run is retried once |
| `[evolve] min_net_fixed = 1` | with two validation questions, a score can rise by one lucky trial; promotion requires a question to actually flip from fail to pass |
| `[evolve] max_cost_usd` | a ceiling on agent spend |
| `[agent.options] setting_sources = "project"` | your personal Claude Code skills and hooks stay out of the evaluation |

## What a run looks like

Three runs were made while building this example (agent: Claude Haiku 5.5 through Claude Code;
analysts: Claude Sonnet through the `claude` CLI; split 4 train / 2 val / 4 test, 3 trials per question).
They are reported as they came out, including the parts that did not go well.

| Run | What happened | Agent cost |
|---|---|---|
| 1 | Baseline already strong: test 4/4, train 2/4, val 1/2 (single trial). Both candidates were **rejected by the growth guardrail** - a three-line seed skill cannot gain a real procedure without growing 4x. That was a framework bug (the ratio is now measured against a 2000-character floor). The same question passed in one evaluation and failed in another: single trials are too noisy here. | $0.08 |
| 2 | With 3 trials: one candidate promoted on val 0.500 → 0.667, although **no question changed outcome** - one extra lucky trial out of six. On the held-out test it scored 0.917 against the baseline's 1.000. A noise promotion. This led to the `min_net_fixed` gate check. | $0.57 |
| 3 | With `min_net_fixed = 1` and the cache kept from run 2: the 30 baseline runs were **reused at no cost**. The candidate lifted train from 0.667 to 0.833 but left val unchanged, so the gate kept the baseline: `0 fixed - 0 regressed = 0 (need ≥ 1)`. | $0.16 |

The honest conclusion for this sample: **the small model's baseline is close to the ceiling on these ten
questions, and the remaining differences are within run-to-run noise**, so no skill version was shown to
be better - and the framework ends by saying so rather than promoting a lucky candidate. The analysts'
diagnoses were nevertheless specific (for example: the same line item appears in a summary table and a
detail table with slightly different monthly values, and the agent summed the wrong one).

To see larger effects you need more headroom or more data: a larger slice of the benchmark, harder
questions only, or a weaker starting point. With two validation questions no method can separate a real
improvement of a few points from noise; the full OfficeQA benchmark is the place to measure that, and
this example's configuration carries over unchanged apart from the dataset path.

## Data and attribution

`fetch_data.py` downloads the sample at a pinned commit of the EvoSkill repository (Apache-2.0). The
bulletins are U.S. Treasury publications; the questions come from the OfficeQA benchmark. Nothing under
`data/` is committed to this repository.
