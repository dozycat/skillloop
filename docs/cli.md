# CLI 参考

```
skillloop [--version] <command> [选项]
```

CLI 是 `SkillLoop` 门面的薄封装,和 Web 操作台、MCP 服务走同一套逻辑。除 `init` `plugins` `web` `mcp` 外,所有命令都作用于一个项目,并接受两个公共选项:

| 选项 | 说明 |
|---|---|
| `-p, --project DIR` | 项目目录,默认当前目录。目录里没有 `skillloop.toml` 时报错退出 |
| `--json` | 机器可读输出(见 [--json 输出](#--json-输出)) |

## 命令一览

### 准备

| 命令 | 作用 | 选项 |
|---|---|---|
| `init [dir]` | 创建项目目录:`skillloop.toml`、需求文档模板、空测试集、`.gitignore` | `--name NAME` 项目名(默认取目录名);`--adapter NAME` 被测 agent(默认 `mock`);`--llm NAME` 框架 LLM(默认 `mock`);`--judge NAME` 判分器(默认 `exact`);`--demo` 生成离线演示项目;`--force` 目录里已有配置时覆盖 |
| `doctor` | 逐项检查 agent 适配器、框架 LLM、配置、需求文档、测试集(至少 4 例)。任一项不过退出码为 1 | — |
| `plugins` | 列出已注册的适配器、判分器、LLM 提供方 | — |
| `status` | 项目概览:输入是否齐全、划分大小、最佳版本及各 split 得分、错题库计数、待评审/待审批 | — |

```bash
skillloop init my-skill --adapter claude_code --llm cli --judge llm
skillloop init demo --demo
skillloop doctor -p my-skill
```

### 运行

这五个命令以任务形式执行:带进度条(stderr,仅 TTY)和日志行,状态落盘,可在 `jobs` / `logs` 里回看。

| 命令 | 作用 | 选项 |
|---|---|---|
| `evolve` | 跑进化闭环:基线 → 迭代 → 提升 → held-out 测试 → 输出 | `-n, --iterations N` 本次最多迭代轮数(默认取配置 `evolve.max_iterations`);`-q, --quiet` 不打印进度和日志 |
| `eval` | 评测一个版本。项目还没有版本时会先登记初版 Skill | `--version V` `v003` / `best`(默认)/ `latest`;`--split {train,val,test}` 不给则评全部用例;`--case ID` 只评指定用例,可重复;`-q` |
| `devset` | 从需求文档生成测试集(规则抽取 → 候选生成 → 场景演化 → 过滤 → 平衡 → 报告) | `--merge` 把结果追加进项目测试集;`-q` |
| `bootstrap` | 只做第一步:登记初版 Skill,没有则由需求文档生成 | — |
| `outputs` | 重新生成输出包(`<project>/outputs/`) | — |
| `reset` | 删除项目的运行结果(版本、评测、错题库、日志、输出),保留输入和配置 | `-y, --yes` 不询问直接删;`--keep-cache` 保留已缓存的 agent 运行,重跑时不再花钱 |

```bash
skillloop evolve -p my-skill -n 5
skillloop eval -p my-skill --version v003 --split test
skillloop eval -p my-skill --case ticket-01 --case ticket-07
skillloop devset -p my-skill --merge
```

`eval` 的结果处理:按 split 评测会把得分记到版本上(不给 `--split` 时记为 `all`);`--split train` 的结果还会并入错题库;`--case` 是临时评测,只保存评测记录,不改版本得分。

### 查看

| 命令 | 作用 | 选项 |
|---|---|---|
| `versions` | 版本谱系表:父版本、来源、状态、train / val / test 得分、变更摘要;`★` 标记最佳 | — |
| `show [version]` | 打印某版本的 SKILL.md,前面带状态、谱系、摘要和闸门各项检查 | 省略 version 为最佳版本 |
| `errors` | 错题库:状态计数、根因分类、条目表 | `--status {open,fixed,regressed,wontfix}`;`--limit N`(默认 40) |
| `jobs` | 最近的任务 | `--limit N`(默认 20) |
| `logs [job]` | 某个任务的结构化日志,省略 job 为最近一个 | `--limit N`(默认 200);`--audit` 只看改变状态的操作;`--all` 包含进度事件 |

```bash
skillloop show v004 -p my-skill
skillloop errors -p my-skill --status open
skillloop logs -p my-skill --audit
```

### 版本操作

| 命令 | 作用 | 选项 |
|---|---|---|
| `diff <base> <cand>` | 两个版本 Skill 的 unified diff | 版本可写 `best` / `latest` |
| `compare <base> <cand>` | 在同一 split 上逐用例配对比较:Δ、置信区间、p 值、修复/回退列表、按标签分解。缺评测结果的一方会先评测 | `--split {train,val,test}`(默认 `val`) |
| `promote <version>` | 手工提升为最佳版本;也是批准 `pending_approval` 候选的方式 | `--reason TEXT` |
| `reject <version>` | 标记为已拒绝(不能拒绝当前最佳) | `--reason TEXT` |
| `import <path>` | 把手工编辑的 Skill(目录或 SKILL.md)登记为新版本,父版本为当前最佳 | `--summary TEXT`(默认 `manual edit`) |
| `export <dest>` | 把某版本的 Skill 包复制到任意目录,例如 agent 的技能目录 | `--version V`(默认最佳) |

```bash
skillloop compare v001 best -p my-skill --split test
skillloop import ./edited/SKILL.md -p my-skill --summary "补充退款时效"
skillloop export ~/.claude/skills/my-skill -p my-skill
```

### 人工评审

```
skillloop review {list,export,import} [file] [--eval ID] [--status {pending,done}]
```

| 动作 | 作用 |
|---|---|
| `list` | 评审队列,默认只列 `pending`;`--status ""` 列全部 |
| `export [file]` | 导出 Review 标注表(CSV)。`--eval` 指定评测,默认最近一次;不给 file 时写到 `outputs/review_<eval_id>.csv` |
| `import <file>` | 读回填好的表(填了 `human_passed` 或 `human_score` 的行),打印导入数和被跳过的行及原因 |

人工结论会覆盖自动判分,在所有用到该评测的地方生效,不需要重跑评测。

### 服务

| 命令 | 作用 | 选项 |
|---|---|---|
| `web` | 启动 Web 操作台,见 [web-console.md](web-console.md) | `-w, --workspace DIR`(默认 `.`);`--host`(默认 `127.0.0.1`);`--port`(默认 `8765`);`--token`;`--open` |
| `mcp` | 启动 MCP 服务(stdio),见 [mcp.md](mcp.md) | `-w, --workspace DIR`(默认 `.`) |

`--workspace` 可以是一个项目目录,也可以是装着多个项目子目录的目录。

## --json 输出

支持 `--json` 的命令:`evolve` `eval` `devset` `bootstrap` `outputs` `status` `doctor` `versions` `show` `compare` `errors` `jobs` `review list`。其余命令(`diff` `logs` `promote` `reject` `import` `export` `review export/import`)接受该选项但仍输出文本。

带 `--json` 时,运行类命令(`evolve` `eval` `devset` `bootstrap` `outputs` `compare`)的日志行改写到 stderr,stdout 只有一个 JSON 值,可以直接接管道;加 `-q` 则连日志也不打印。

```bash
skillloop evolve -p demo -q --json | jq .best_version
skillloop versions -p demo --json | jq -r '.[] | select(.status=="best") | .id'
```

## 退出码

| 退出码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 出错:不是项目目录、版本/评测不存在、配置非法、任务失败或被取消、`doctor` 有检查项不过。错误信息在 stderr,以 `error:` 或 `<kind> <status>:` 开头 |
| 2 | 命令行用法错误(未知命令、缺参数) |
| 130 | 在非任务命令里按了 Ctrl-C |

## Ctrl-C 与中断

在 `evolve` / `eval` / `devset` 运行时按 Ctrl-C,CLI 会请求取消任务并等它停下:正在跑的 agent 子进程按进程组被终止,任务状态记为 `cancelled`,命令以退出码 1 结束(`evolve cancelled: cancelled by user`)。被取消的评测不会计入版本得分。

已完成的工作都已落盘,再次执行 `skillloop evolve` 会接着跑:已有的评测结果直接复用,迭代编号顺延。如果进程被强杀(没来得及标记状态),该任务会在下次打开项目时被标为 `interrupted`,同样重新执行即可。

任务状态取值:`queued` `running` `cancelling` `succeeded` `failed` `cancelled` `interrupted`。

## 一次完整的会话

下面是在空目录里真实执行演示项目的输出(只删减了行,数字未改)。演示用的是模拟 agent 和模拟 LLM,不需要任何 API key。

```console
$ skillloop init demo --demo
Demo project created in /tmp/x/demo (simulated agent + LLM, runs offline).
Next:  skillloop evolve -p demo

$ skillloop doctor -p demo
✓ agent adapter  mock: ok
✓ framework LLM  mock: ok
✓ config         valid
✓ requirement    present
✓ dataset        24 cases

$ skillloop evolve -p demo -n 2
18:56:55   dataset: 24 cases → train 8 / val 8 / test 8
18:56:55   baseline v001: val mean_score=0.000 (pass rate 0.0%)
18:56:55   iteration 1: parent v001 (best v001, val mean_score=0.000)
18:56:55   candidate 1/1 v002: [full] add rules for: coupon; add rules for: partial-refund
18:56:55   ★ promoted v002: val mean_score 0.000 → 0.125 (1 fixed, 0 regressed)
18:56:55   iteration 2: parent v002 (best v002, val mean_score=0.125)
18:56:55   candidate 1/2 v003: [full] add rules for: escalation; add rules for: tone
18:56:56   candidate 2/2 v004: [additive] add rules for: escalation; add rules for: tone
18:56:56   ★ promoted v004: val mean_score 0.125 → 0.500 (3 fixed, 0 regressed)
18:56:56   ✗ v003 not promoted: a better candidate won
18:56:56   done: best v004 (reached max_iterations (2)); val mean_score 0.000 → 0.500
18:56:56   job succeeded

Best version: v004  (baseline v001, 2 iteration(s), 2 promotion(s))
Stopped: reached max_iterations (2)

split  cases  baseline mean_score  best mean_score  Δ
-----  -----  -------------------  ---------------  ------
train  8      0.125                0.625            +0.500
val    8      0.000                0.500            +0.500
test   8      0.125                0.500            +0.375

Outputs:
  best_skill         outputs/best
  review_sheet       outputs/review_sheet.csv
  errorbook_md       outputs/errorbook.md
  errorbook_jsonl    outputs/errorbook.jsonl
  version_compare    outputs/version_compare.md
  metrics_json       outputs/metrics_summary.json
  metrics_md         outputs/metrics_summary.md
  report_html        outputs/report.html

$ skillloop versions -p demo
version  parent  origin  status    train  val    test   summary
-------  ------  ------  --------  -----  -----  -----  -----------------------------------------------------------
  v001   -       seed    accepted  0.125  0.000  0.125  seed skill provided by the user
  v002   v001    evolve  accepted  0.375  0.125  -      [full] add rules for: coupon; add rules for: partial-refund
  v003   v002    evolve  accepted  -      0.375  -      [full] add rules for: escalation; add rules for: tone
★ v004   v002    evolve  best      0.625  0.500  0.500  [additive] add rules for: escalation; add rules for: tone

$ skillloop compare v001 best -p demo
# 版本比较报告：v001 → v004

在 `val` 集的 8 个共同用例上，`mean_score` 从 **0.000** 变为 **0.500**（Δ **+0.500**，更好）。

- 95% 置信区间（配对 bootstrap）：[+0.125, +0.875]
- 符号检验 p 值：0.125
- 通过率：0.000 → 0.500；平均分：0.000 → 0.500
- 修复 4，回退 0，提升 0，下降 0，保持通过 0，保持失败 4
...

$ skillloop logs -p demo --audit
18:56:55 [info   ] audit version.create {"version": "v003", "parent": "v002", "strategy": "full"}
18:56:56 [info   ] audit version.create {"version": "v004", "parent": "v002", "strategy": "additive"}
18:56:56 [info   ] audit version.promote {"version": "v004", "previous": "v002", "delta": 0.375}

$ skillloop evolve -p demo        # 接着跑:前两轮的评测直接复用
```
