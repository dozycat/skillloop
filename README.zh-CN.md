# skillloop — Skill 进化闭环框架

[English](README.md) | **简体中文**

把「需求文档 + 初版 Skill + 测试集」变成「通过评测与评审的最佳 SKILL.md」,并且能接入任意 agent 系统。

```
生成初版 Skill → 运行评测 → 人工/LLM 判分 → 错题库 → 迭代 Skill → 版本比较 ─┐
                    ▲                                                      │
                    └──────────────── 下一轮 ◄─────────────────────────────┘
                                               │ 闸门通过
                                               ▼
                                         提升最佳版本
```

- **零运行时依赖**:纯 Python 标准库(≥ 3.11)。Web 操作台、MCP 服务、SQLite 状态库都不需要额外安装。
- **接入只需实现一个方法**:`AgentAdapter.run(request) -> Trajectory`。内置 Claude Code、Codex、nanobot、任意命令行、HTTP、直接调模型、进程内函数 7 种适配器。
- **三个入口共用一个核心**:CLI、Web 操作台、MCP 服务都走同一个 `SkillLoop` 门面,行为一致。
- **离线可跑**:自带模拟 agent 与模拟 LLM,不要 API key 也能在几秒内看完整个闭环。

## 快速开始

```bash
pip install -e .                      # 在克隆的仓库里;加 '.[anthropic]' 以使用 Anthropic SDK

skillloop init demo --demo             # 离线演示项目(模拟 agent + 模拟 LLM)
skillloop evolve -p demo               # 跑闭环
skillloop versions -p demo             # 版本谱系与各 split 得分
skillloop show -p demo                 # 最佳 SKILL.md 与闸门决定
open demo/outputs/report.html         # 汇总报告
```

演示里模拟 LLM 每隔几次会提出一个「删掉已有规则」的有害补丁,可以看到闸门把它挡下:

```
★ promoted v007: val mean_score 0.750 → 0.875 (1 fixed, 0 regressed)
✗ v006 not promoted: improvement: mean_score 0.750 -> 0.625 (Δ-0.125, need ≥ +0.010); regressions: 2 case(s) regressed (allowed 1)
```

### 用真实模型

[examples/](examples) 里有可直接运行的项目。`examples/expense-policy` 是一个真实例子(差旅报销核算):agent 和框架 LLM 都通过本机 `claude` CLI 调用,不需要 API key。

```bash
skillloop doctor -p examples/expense-policy    # 检查 agent / LLM / 配置 / 输入是否就绪
skillloop evolve -p examples/expense-policy
```

这个例子的初版 Skill 只写了「把费用相加」,不含任何公司政策。一次实测(agent 用 haiku,分析用 sonnet)一轮迭代后,train / val / test 的通过率都从 0% 到 100%(12 / 6 / 6 例),test 全程未参与分析和选择;分析器对每条失败给出了根因(例如「没有按每晚上限比较」「漏发餐补」)。模型输出有随机性,你的结果可能需要更多轮。

### 自己的项目

```bash
skillloop init my-skill --adapter claude_code --llm anthropic --judge llm
# 1. 写 my-skill/requirement.md        需求文档:业务目标、范围、验收标准、约束
# 2. (可选)my-skill/seed_skill/SKILL.md  初版 Skill;没有则由需求文档生成
# 3. my-skill/dataset.jsonl            测试集:每行 {"input": ..., "expected": ..., "tags": [...]}
#    没有测试集?  skillloop devset -p my-skill --merge   从需求文档生成
skillloop evolve -p my-skill
```

### 不只是一段提示的任务

真实任务通常带着不属于 Skill 的资料和约定:

```toml
[agent]
task_file = "task.md"                    # 固定的任务说明与答案格式,随每个用例交给 agent,不参与进化
data_dirs = ["data/treasury_bulletins"]  # 文档库,只读地链接进每个用例的工作目录,不复制

[dataset]                                # 现成的 CSV 基准,列名自定义
input_column = "question"
expected_column = "answer"
tag_columns = ["difficulty"]

[eval]
judge = "answer"                         # "36080 million" == "36,080" == "36.08 billion"
answer_regex = 'FINAL ANSWER:\s*(.+)'    # 只对最终答案那一行判分
trials = 3                               # agent 输出有随机性,三次取多数
max_retries = 1                          # agent 报错或超时后重跑

[evolve]
max_cost_usd = 5.0                       # agent 花费超过该值后不再开始新一轮
```

已完成的 agent 运行会被缓存:评测中断后续跑只补没完成的用例,内容相同的候选不会重复花钱。完整例子见 [examples/officeqa](examples/officeqa)。

## 架构

```
 输入                入口层                      Skill 进化闭环              输出
┌──────────┐   ┌─────────────────────┐   ┌──────────────────────┐   ┌────────────────┐
│ 需求文档  │   │ CLI · Web 操作台 · MCP │   │ bootstrap  生成初版   │   │ 最佳 SKILL.md   │
│ 初版 Skill│──▶└──────────┬──────────┘──▶│ Evaluator  运行评测   │──▶│ Review 标注表   │
│ 测试集    │        SkillLoop 门面         │ Judge      判分       │   │ 错题库          │
└──────────┘   ┌──────────┴──────────┐   │ ErrorBook  错题库     │   │ 版本比较报告    │
               │      共享核心         │   │ Proposer   迭代 Skill │   │ 指标摘要        │
               │ 项目管理 · 任务状态    │   │ compare    版本比较   │   └────────────────┘
               │ Agent 适配器 · 版本管理│   │ gate       提升闸门   │
               │ 开发集管道 · 产物索引  │   └──────────────────────┘
               └─────────────────────┘
        可观测性与安全:结构化日志/审计 · 进度 · 可取消任务 · 本地 Git 历史 · 路径隔离 · Mock MCP Profiles
```

| 图中模块 | 代码位置 |
|---|---|
| 项目管理 | `core/project.py`、`core/config.py`、`service.py: Workspace` |
| 任务状态与进度 | `core/jobs.py`、`core/store.py`(SQLite:jobs / events / artifacts / reviews) |
| Agent 适配器 | `adapters/`(见 [docs/adapters.md](docs/adapters.md)) |
| 版本管理 | `core/versions.py`(快照 + manifest + 独立 git 仓库) |
| 开发集管道 | `devset/pipeline.py`(规则抽取 → 候选生成 → 场景演化 → 过滤 → 平衡 → 报告) |
| Eval 与 Judge | `eval/runner.py`、`eval/judges.py`、`eval/review.py` |
| 产物索引 | `core/store.py: artifacts`,`skillloop` 各阶段通过 `ctx.artifact()` 登记 |
| 进化闭环 | `evolve/loop.py`(编排)、`proposer.py`、`patch.py`、`guard.py`、`compare.py`、`gate.py`、`errorbook.py` |
| 输出 | `report/` |
| Web 操作台 / MCP 服务 | `web/`、`mcp/server.py` |
| Mock MCP Profiles | `mcp/mock_server.py`、`mcp/profiles.py` |

更详细的设计说明见 [docs/architecture.md](docs/architecture.md)。

## 闭环是怎么工作的

1. **数据划分**。测试集按标签分层、按 case id 哈希稳定地分成 train / val / test。分析器只看 train;候选只在 val 上选;test 只在结束时评一次,给出无偏数字。
2. **基线**。有初版 Skill 就登记为 `v001`,没有就由需求文档生成。
3. **每轮迭代**:
   - 在 train 上评测父版本,失败用例进入**错题库**(上下文、判分理由、轨迹摘录、复现命令)。
   - **并行分析**:失败用例分批交给互相独立的错因分析器,各自给出根因诊断和一个针对同一父版本的补丁;成功用例由另一个分析器提炼「必须保留的行为」。
   - **分层合并**:补丁按批合并,多个分析器重复提出的修改视为系统性缺口予以保留,孤立的修改被丢弃。
   - **多候选**:同一批证据产出不同风险档位的候选(`full` 全量 / `additive` 只增不删 / `focused` 聚焦最大根因类别 / `union` 不经 LLM 的并集),一个坏修改不会毁掉整轮。
   - **确定性护栏**:格式校验、体积膨胀上限、与父版本相同、**答案泄漏**(把标准答案抄进 Skill)——不过关的候选不花评测预算。
   - **版本比较**:候选与当前最佳在 val 上逐用例配对比较:修复 / 退化 / 改善 / 变差、按标签分解、符号检验 p 值、bootstrap 置信区间。
   - **闸门**:提升幅度 ≥ `min_delta`、退化用例数 ≤ `max_regressions`、受保护标签零退化、(可选)held-out 不下降、(可选)人工审批。全部通过才提升为最佳;决定和每项检查都写入版本 manifest。
4. **停止**:达到目标分、连续 `patience` 轮无提升、达到最大轮数,或被取消。
5. **产出**:最佳 Skill、Review 标注表、错题库、版本比较报告、指标摘要、HTML 报告,写入 `<project>/outputs/`。

所有状态落盘,**可取消、可续跑**:已完成的评测会被复用,不会重跑。

## 接入你的 agent 系统

三种方式,按侵入程度从低到高:

**1. 配置一条命令**(任何 CLI agent)

```toml
[agent]
adapter = "command"
[agent.options]
command = ["my-agent", "run", "--skills", "{skill_dir}", "--prompt-file", "{prompt_file}"]
skill_mode = "dir"          # inline | dir | file
output = "last_line"        # text | json | last_line
```

**2. 暴露一个 HTTP 端点**:`adapter = "http"`,skillloop 把 `{input, skill, files}` POST 过去,读回 `{output, steps?, usage?}`。

**3. 写一个适配器**(约 20 行):

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

把这个文件放在 `skillloop.toml` 旁边,在 `[project] plugins = ["my_agent"]` 里列出模块名,就能按名字使用,不需要打包([examples/custom-adapter](examples/custom-adapter) 是完整例子)。也可以不注册,直接在配置里写 `adapter = "my_pkg.my_module:MyAgent"`,或通过 entry point 组 `skillloop.adapters` 发布。判分器(`skillloop.judges`)和框架 LLM(`skillloop.llm`)同理。各内置适配器的配置项见 [docs/adapters.md](docs/adapters.md)。

**作为库嵌入**:

```python
from skillloop import SkillLoop
from skillloop.adapters.callable import FunctionAdapter

es = SkillLoop("my_project", adapter=FunctionAdapter(lambda req: my_agent(req.case.input, req.skill)))
result = es.evolve()
print(result.best_version, es.version()["skill_md"])
```

## 判分

`[eval] judge = ...`,也可以在单个 case 上用 `"judge"` 字段覆盖。

| judge | 用途 |
|---|---|
| `exact` / `contains` / `regex` | 文本精确、包含、正则 |
| `numeric` | 数值比较(容差、百分号、千分位) |
| `json_match` | JSON 子集匹配,按字段给部分分 |
| `script` | 你的命令或函数:stdin 收 `{case, output, trajectory}`,返回 `{score, reason}` |
| `llm` | 按 rubric 的 LLM 判分,可多票;低置信度或票数分歧会标记 `needs_review` |
| `composite` | 加权 / all / any 组合多个 judge |
| `human` | 全部进入人工评审队列 |

**人工评审**:标记为 `needs_review` 的结果进入队列,可在 Web 操作台逐条标注,或 `skillloop review export` 导出 CSV、填好后 `skillloop review import`。人工结论会覆盖自动判分,并在所有用到该评测的地方生效;`eval/review.py: agreement()` 给出 judge 与人工的一致率和 Cohen's kappa,用来校准 LLM 判分。

## 入口

**CLI** — `skillloop --help`。常用:`init` `doctor` `evolve` `eval` `devset` `status` `versions` `show` `diff` `compare` `promote` `reject` `import` `export` `errors` `review` `jobs` `logs` `outputs` `plugins`。所有读命令支持 `--json`。

**Web 操作台**

```bash
skillloop web -w ./workspace --open        # workspace 是一个项目,或一个装着多个项目的目录
```

项目管理、配置、发起任务、实时进度与日志、取消、版本谱系与 diff、评测明细与轨迹、错题库、Review 标注、产物浏览。默认只监听 127.0.0.1;监听其他地址必须带 `--token`。

**MCP 服务**(stdio)——让任何支持 MCP 的 agent 驱动闭环:

```json
{ "mcpServers": { "skillloop": { "command": "skillloop", "args": ["mcp", "-w", "/path/to/workspace"] } } }
```

提供 `create_project` `evolve` `evaluate` `build_devset` `job_status` `cancel_job` `list_versions` `get_skill` `diff_versions` `compare_versions` `get_errorbook` `submit_review` `get_metrics` 等约 30 个工具。长任务立即返回 `job_id`,由调用方轮询。

## 配置

`skillloop.toml`,每一项都有默认值。最常调的:

```toml
[agent]
adapter = "claude_code"              # 被测 agent
mcp_profile = "crm"                  # 可选:评测时挂载的 Mock MCP

[llm]
provider = "anthropic"               # 框架自用模型:anthropic | openai | cli | mock
model = "claude-opus-5-5"
[llm.roles.judge]                    # 可按角色换模型:judge / analyst_error / analyst_success / merger / builder / devset_*
model = "claude-haiku-5-5"

[eval]
judge = "llm"
trials = 1                           # 每个用例重复次数(agent 不稳定时调大)
concurrency = 4
timeout_s = 600

[evolve]
max_iterations = 8
candidates_per_iteration = 2
patience = 3
metric = "mean_score"                # 或 pass_rate
min_delta = 0.01                     # 闸门:最小提升
max_regressions = 1                  # 闸门:允许的退化用例数
protect_tags = ["core"]              # 闸门:这些标签的用例不允许退化
require_human_approval = false       # 闸门通过后是否等人工 promote
confirm_on_test = false              # 是否要求 held-out 不下降

[[mcp_profiles.crm.tools]]              # Mock MCP Profile:让被测 agent 调到确定性的假工具
name = "lookup_order"
description = "查询订单"
default = { status = "not_found" }
responses = [{ when = { id = "88231" }, result = { status = "shipped" } }]
```

完整字段见 [docs/configuration.md](docs/configuration.md)。

## 可观测性与安全

- **日志与审计**:每个任务的结构化事件写入 SQLite 和 `.skillloop/logs/<job>.jsonl`;所有改变状态的操作(创建版本、提升、改配置、提交评审)记为 `audit` 事件。`skillloop logs --audit`。
- **进度与取消**:任务分阶段上报进度;取消会立刻终止子进程 agent(按进程组),评测返回部分结果且不计入版本得分。
- **本地 Git 历史**:`.skillloop/versions/` 是独立的 git 仓库,每个版本一个提交和 tag,`best/` 目录的提交历史就是提升历史。不会碰你自己的仓库。
- **路径隔离**:项目的所有读写都限制在项目目录内(解析符号链接后校验);每个用例在独立的临时工作目录里运行。
- **Mock MCP Profiles**:评测时给 agent 挂确定性的假 MCP 工具,调用记录落盘,可复现、不触碰真实系统。

评测会在你的机器上运行被测 agent。`claude_code` 适配器默认 `acceptEdits` 权限、不加 `--dangerously-skip-permissions`;需要更强隔离时把 agent 放进容器,通过 `command` 适配器调用。

## 项目目录

```
my-skill/
  skillloop.toml  requirement.md  dataset.jsonl  seed_skill/SKILL.md      # 输入
  outputs/       best/ review_sheet.csv errorbook.md version_compare.md metrics_summary.{json,md} report.html
  .skillloop/     state.db  versions/(git)  evals/  errorbook/  devset/  logs/  runs/  history.json
```

## 文档

| | |
|---|---|
| [架构与设计](docs/architecture.md) | 分层、四个契约、设计取舍 |
| [配置参考](docs/configuration.md) | `skillloop.toml` 全部配置项 |
| [适配器与 LLM 提供方](docs/adapters.md) | 内置适配器、编写自己的适配器 |
| [判分与人工评审](docs/judges.md) | 判分器、评审队列、校准 |
| [开发集管道](docs/devset.md) | 从需求文档生成测试集 |
| [CLI](docs/cli.md) · [Web 操作台](docs/web-console.md) · [MCP](docs/mcp.md) | 三个入口、REST API、Mock MCP Profiles |

## 状态

Alpha(0.1)。闭环、模拟世界和三个入口由离线测试覆盖;各集成对真实系统的验证程度不同,[CHANGELOG.md](CHANGELOG.md) 列出了哪些已实测、哪些还没有。支持 macOS 与 Linux,Windows 未测试。

评测会在你的机器上运行被测 agent。在不可信的数据集或项目上使用前请先读 [SECURITY.md](SECURITY.md)。

## 参与贡献

见 [CONTRIBUTING.md](CONTRIBUTING.md)。`make install && make check` 即可得到一个可用的开发环境。

## 参考

设计上借鉴了三份工作,实现是独立的:

- [EvoSkill](https://github.com/sentient-agi/EvoSkill):从失败轨迹出发、proposer / generator / evaluator 分工、frontier、用 git 管理版本。本项目与它没有隶属关系。
- [Trace2Skill: Distill Trajectory-Local Lessons into Transferable Agent Skills](https://arxiv.org/abs/2603.25158):并行分析、分层合并补丁、确定性的补丁应用护栏。
- [Trace2Skill: Verifier-Guided Skill Evolution for Long-Context EDA Agents](https://arxiv.org/abs/2605.21810):通过率主导的选择、语义退化检查、经验库、每阶段产物可审计。

## License

[Apache-2.0](LICENSE)
