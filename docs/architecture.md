# 架构与设计说明

本文解释 skillloop 的分层、关键契约,以及几个设计取舍背后的原因。读代码前先读这一篇。

## 分层

```
入口层        cli.py            web/server.py + static/        mcp/server.py
                 └──────────────────┬───────────────────────────┘
门面                          service.py  (SkillLoop / Workspace)
                 ┌──────────────────┼───────────────────────────┐
闭环         evolve/loop.py    eval/runner.py     devset/pipeline.py    report/
             proposer patch    judges review
             guard compare gate errorbook
                 └──────────────────┼───────────────────────────┘
共享核心      core/  models skill config project versions store jobs sandbox registry dataset
插件          adapters/  (被测 agent)        llm/  (框架自用模型)        eval/judges.py
```

依赖只向下。入口层不直接碰核心模块,全部经过 `SkillLoop` 门面——这是三个入口行为一致的保证,也意味着新增一个入口(比如 gRPC)只需要再包一层。

## 四个契约

整个框架围绕四个小接口,全部在 `skillloop` 包顶层导出。

### 1. `AgentAdapter` — 接入一个 agent 系统

```python
class AgentAdapter:
    def run(self, request: AgentRequest) -> Trajectory: ...
    def healthcheck(self) -> tuple[bool, str]: ...
```

`AgentRequest` 带着:一个 `Case`、一个 `Skill`、一个已建好的隔离工作目录、超时、取消事件、可选的 MCP 配置。适配器负责三件事:把 skill 交给 agent(原生技能目录 / system prompt / AGENTS.md,基类有现成帮助函数)、跑完一个用例、把结果装进 `Trajectory`。

约定:**agent 侧的失败不抛异常**,而是返回 `status = "error" | "timeout" | "cancelled"`。这样一个用例的崩溃不会中断整次评测,而且超时和报错本身就是评测信号(记 0 分,进错题库,根因类别 `agent_error`)。

### 2. `Judge` — 判分

```python
class Judge:
    def judge(self, case: Case, trajectory: Trajectory) -> Judgment: ...   # score 0..1, passed, reason
```

`Judgment.needs_review = True` 会把结果送进人工评审队列。人工结论存在 `reviews` 表里,由 `apply_reviews()` 在读取评测时覆盖自动判分——所以补一条人工标注后,不用重跑评测,版本比较和闸门就会用上它。

### 3. `LLMClient` — 框架自己用的模型

```python
class LLMClient:
    def complete(self, prompt, *, system="", max_tokens=16000, json_schema=None, role="") -> LLMResponse: ...
```

每次调用都带 `role`(`judge` / `analyst_error` / `analyst_success` / `merger` / `builder` / `devset_*`)。`role` 有两个用处:配置里可以按角色路由到不同模型(便宜模型判分、强模型分析);模拟 LLM 靠它给出确定性回答。提示词里的数据统一放在 `<tag>…</tag>` 块中。

被测 agent 和框架 LLM 是两套独立配置:被测的可以是 Codex,判分的可以是 Claude,反之亦然。

### 4. `JobContext` — 可观测、可取消

所有长操作接收一个 `ctx`:`ctx.progress(stage, current, total)`、`ctx.log()`、`ctx.audit()`、`ctx.artifact()`、`ctx.check_cancelled()`。通过任务系统运行时它写 SQLite 和 JSONL;直接当库用时传 `None`,得到一个什么都不持久化的 `NullContext`。闭环代码不需要知道自己是被 CLI、Web 还是 MCP 调起的。

## 数据模型

| 类型 | 含义 |
|---|---|
| `Case` | 一个用例:`input`、`expected`(可空)、`rubric`、`tags`、`files`(运行前写入工作目录)、`meta` |
| `Skill` | 一个技能包:`SKILL.md`(frontmatter + 正文)加若干支撑文件。进化的对象 |
| `Trajectory` | 一次运行:最终 `output`、`steps`(思考/工具调用/结果)、`status`、`usage` |
| `EvalReport` | 一个版本在一组用例上的全部结果;多次 trial 先按用例取平均再聚合 |
| `SkillVersion` | 版本 manifest:父版本、来源、状态、各 split 得分、产生它的补丁、闸门决定 |
| `ErrorEntry` | 错题库条目:每个用例一条,带出现次数、根因、经验、状态 |
| `Patch` | 一组声明式编辑(append / replace / delete / write_file / set_description) |
| `Comparison` | 两个版本在同一批用例上的配对比较 |

全部是可 JSON 往返的 dataclass,`from_dict` 忽略未知字段,所以旧数据在新版本下仍可读。

## 几个设计取舍

**为什么补丁是声明式编辑,而不是让模型重写整个 SKILL.md。** 重写会悄悄丢掉已经有效的内容,而且无法归因。声明式编辑可以逐条确定性地校验(`old` 文本必须恰好出现一次、路径不能越界、内容不能重复),不合格的编辑被单独拒绝并记录,合格的照常应用;每个版本的 manifest 里留有产生它的补丁,任何一行都能追溯到是哪批失败用例引出的。

**为什么并行分析再合并,而不是逐条失败顺序修改。** 顺序修改的结果依赖用例顺序,后面的修改还会建立在前面可能错误的修改之上。并行时每个分析器都面对同一个父版本,彼此独立;多个分析器不约而同提出的修改是「系统性缺口」的证据,只有一个提出的多半是噪声。这是 Trace2Skill 的核心观察。

**为什么每轮出多个候选。** LLM 提出的删除和改写是退化的主要来源。同一批证据同时产出「全量」和「只增不删」两个候选,闸门自然会在两者之间选;演示项目里可以直接看到这一点。

**为什么分 train / val / test 三份。** 分析器看过的用例不能再用来评判它提出的修改,否则选出来的是「最会背题的版本」。val 用于选择,会被反复使用因而也有乐观偏差,所以结束时再在从未参与任何决策的 test 上各评一次基线和最佳版本。划分按 case id 哈希,新增用例不会让已有用例换组。

**答案泄漏检查。** 即使有划分,模型仍可能把 train 用例的标准答案或原题抄进 Skill。`guard.find_leaks` 检查候选中新出现的、与用例答案/题面逐字相同的长片段,命中即拒绝,不进入评测。提示词里也明确要求了这一点,但提示词是劝告,护栏才是保证。

**为什么版本库是独立的 git 仓库。** 需要历史、diff 和 tag,但不能往用户自己的仓库里提交东西。`.skillloop/versions/` 自成一个仓库;磁盘上的快照目录是事实来源,git 只是尽力而为的历史记录,git 不可用时功能不受影响。

**为什么闸门是一组显式检查。** 「分数更高就换」会被噪声和局部退化骗过。每项检查(提升幅度、退化数、受保护标签、held-out、人工审批)独立记录通过与否和具体数字,所以每次提升或拒绝都能用一句话解释,也能事后审计。

## 模拟世界

`adapters/mock.py` 和 `llm/mock.py` 一起构成一个确定性的小世界,用于演示和测试:

- 用例在 `meta.requires` 里列出它需要的规则标记;模拟 agent 当且仅当 Skill 文本里出现全部 `[标记]` 时答对,否则在轨迹里写 `MISSING_GUIDANCE: <标记> :: <提示>`。
- 模拟分析器读这些行,提出补上规则的补丁(每次最多 `max_fixes_per_call` 条,所以需要多轮);每 `regress_every` 次顺带删掉一条已有规则,制造一个应当被闸门拦下的有害候选。

这让闭环的每个分支——提升、拒绝、退化检测、续跑、取消——都能在毫秒级的测试里被真实地走到,而不是用桩函数绕过去。

## 扩展点一览

| 想做的事 | 做法 |
|---|---|
| 接入新的 agent 系统 | 实现 `AgentAdapter`,`@ADAPTERS.register("name")`,或配置里写 `"pkg.mod:Class"` |
| 自定义判分 | 实现 `Judge` 并注册;或用 `script` judge 指向一个命令/函数 |
| 换框架模型 | `[llm] provider`;或实现 `LLMClient` 并注册 |
| 自定义候选策略 | 继承 `Proposer`,重写 `propose`,传给 `EvolutionLoop` |
| 自定义闸门 | `evolve/gate.py: decide` 是纯函数,输入 `Comparison` 输出 `GateDecision` |
| 新入口 | 包一层 `SkillLoop` / `Workspace` |

## 已知边界

- 进程内任务:任务跑在启动它的进程里(CLI 进程或 Web 服务进程)。进程退出时,未完成的任务在下次启动时被标记为 `interrupted`;重新发起即可续跑,已完成的评测会复用。
- 没有内置容器隔离:路径隔离约束的是框架自己的读写,被测 agent 的权限由它自己的沙箱/权限模式决定。
- 每个项目进化一个 Skill。多 Skill 协同进化不在当前范围内。
