# 配置参考

项目配置在项目根目录的 `skillloop.toml`。**每一项都有默认值**:空文件也是合法配置,此时用模拟 agent 和模拟 LLM 离线运行。下文的值即默认值,字段定义在 `src/skillloop/core/config.py`。

改配置的三种方式:直接编辑文件;Web 操作台的「配置」页;MCP 的 `update_config`(以及 SDK 的 `SkillLoop.update_config`)。后两者做深度合并并校验后写回文件;`agent.options`、`llm.options`、`llm.roles`、`eval.judge_options`、`eval.judges`、`mcp_profiles` 这几个自由结构的表是**整体替换**,这样才能删掉其中的键。

`skillloop doctor` 会检查配置是否合法、agent 与 LLM 是否可用。

## `[project]` — 输入

```toml
[project]
name = "skillloop-project"        # 项目名(显示用;`skillloop init` 默认取目录名)
requirement = "requirement.md"   # 需求文档
seed_skill = "seed_skill"        # 初版 Skill:目录(含 SKILL.md)或单个 SKILL.md;不存在则由需求文档生成
dataset = "dataset.jsonl"        # 测试集:.jsonl / .json / .csv / .tsv
skill_name = ""                  # 生成初版 Skill 时用的名字;空则由项目名派生
plugins = []                     # 启动时导入的项目内模块,见下
```

三个路径都相对项目目录,且必须位于项目目录内。

**测试集格式**:每条用例至少有输入字段。字段名有别名:输入可写 `input` / `question` / `prompt` / `query` / `task` / `instruction`;期望答案可写 `expected` / `answer` / `ground_truth` / `gold` / `output` / `label` / `reference`。其余可选字段:`id`、`rubric`(给 LLM / 人工判分的验收标准)、`tags`、`split`(预先指定 `train` / `val` / `test`)、`files`(运行前写入工作目录的文件,相对路径 → 文本)、`weight`、`judge`(单条覆盖判分器)、`meta`。不认识的字段会收进 `meta`。

**`plugins`**:列出的模块在项目打开时被导入,项目目录会加入 `sys.path`。把自己的适配器 / 判分器写在 `skillloop.toml` 旁边的 `.py` 文件里,用 `@ADAPTERS.register` 等装饰器注册,再在这里列出模块名,就能在配置里按名字使用,不需要打包安装。完整例子见 [examples/custom-adapter](../examples/custom-adapter)。导入失败会直接报错并指出是哪个插件。

## `[agent]` — 被测 agent

```toml
[agent]
adapter = "mock"        # 注册名,或 "包.模块:类名"
mcp_profile = ""        # 评测时挂给 agent 的 Mock MCP Profile 名(见 [mcp_profiles])

[agent.options]         # 原样作为关键字参数传给适配器构造函数
```

内置适配器及各自的 `options` 见 [adapters.md](adapters.md)。`skillloop plugins` 列出当前可用的名字。

## `[llm]` — 框架自用的模型

```toml
[llm]
provider = "mock"       # anthropic | openai | cli | mock,或 "包.模块:类名"
model = ""              # 空则用提供方的默认模型

[llm.options]           # 传给提供方构造函数(base_url、api_key_env、timeout 等,见 adapters.md)

[llm.roles.judge]       # 可选:按角色覆盖 provider / model / options
model = "claude-haiku-5-5"
```

角色名:`judge`(LLM 判分)、`analyst_error`(错因分析)、`analyst_success`(成功经验)、`merger`(补丁合并)、`builder`(生成初版 Skill)、`devset_rules` / `devset_cases` / `devset_evolve` / `devset_filter`(开发集管道)。常见做法是分析和合并用强模型、判分用便宜模型。

被测 agent 与框架 LLM 互相独立,可以是不同厂商。

## `[eval]` — 评测与判分

```toml
[eval]
judge = "exact"         # 项目级判分器,见 judges.md
trials = 1              # 每个用例重复运行次数;agent 输出不稳定时调大
concurrency = 4         # 并行运行的用例数
timeout_s = 600.0       # 单个用例的超时(秒);超时记 0 分,状态 timeout
pass_threshold = 0.7    # 分级判分器:分数 ≥ 该值算通过
keep_workdirs = false   # 保留每个用例的工作目录(.skillloop/runs/),排查问题时打开

[eval.judge_options]    # 项目级判分器的选项

[eval.judges.numeric]   # 用例里用 "judge": "numeric" 覆盖时,该判分器的选项
abs_tol = 0.01
```

`trials > 1` 时,一个用例的分数是各次的平均,多数次通过才算通过(平局算不通过)。

## `[evolve]` — 进化闭环

```toml
[evolve]
# 规模与停止
max_iterations = 8              # 一次 evolve 最多迭代轮数
candidates_per_iteration = 2    # 每轮评测的候选数
patience = 3                    # 连续这么多轮没有提升则停止
target_score = 1.0              # 最佳版本的 val 分数达到该值则停止
frontier_size = 3               # 保留多少个可作父版本的版本;长期无提升时会轮换父版本

# 数据划分
train_ratio = 0.5               # 分析用
val_ratio = 0.3                 # 选择用;剩余部分是 held-out 的 test
split_seed = 7                  # 划分按「种子 + 用例 id」哈希,新增用例不会让已有用例换组

# 分析
analyst_batch_size = 4          # 每个错因分析器处理的失败用例数
merge_batch_size = 6            # 分层合并时每次合并的补丁数
max_failures_per_iteration = 24 # 每轮最多分析的失败用例数(分数最低的优先)
use_success_analysis = true     # 是否分析成功用例以保护有效行为

# 闸门
metric = "mean_score"           # 比较用的指标:mean_score | pass_rate
min_delta = 0.01                # 候选相对当前最佳在 val 上的最小提升
max_regressions = 1             # 允许的退化用例数(最佳通过、候选不通过)
protect_tags = []               # 带这些标签的用例不允许退化
require_human_approval = false  # 闸门通过后是否等待人工 `skillloop promote`
confirm_on_test = false         # 是否额外要求 test 上不下降(会让 test 参与决策,不再是纯 held-out)

# 护栏
max_skill_chars = 24000         # Skill 总字符数上限
max_growth_ratio = 2.5          # 候选相对父版本的体积上限倍数
leak_check = true               # 拒绝把标准答案 / 原题抄进 Skill 的候选
```

说明:

- 测试集很小、分不出 val 时,会退化为在 train 上选择并给出警告(有过拟合风险)。建议至少十几条用例。
- `leak_check` 只应在 Skill 的职责就是承载答案所依赖的参考知识时关闭(术语表、查找表)。对教流程的 Skill,关掉它等于允许背题。
- `confirm_on_test = true` 更保守,代价是结束时报告的 test 数字不再无偏。

## `[devset]` — 开发集管道

```toml
[devset]
target_size = 24          # 每次运行最多保留的新用例数
candidates_per_rule = 3   # 每条规则生成的候选数
evolve_rounds = 1         # 场景演化轮数
max_per_rule = 6          # 平衡:每条规则的用例上限
dedupe_threshold = 0.85   # 近重复判定的相似度阈值

[devset.difficulty_mix]   # 平衡:难度配比
easy = 0.3
medium = 0.4
hard = 0.3
```

各阶段的行为见 [devset.md](devset.md)。

## `[mcp_profiles.<name>]` — Mock MCP Profile

给被测 agent 挂一组确定性的假 MCP 工具,用 `[agent] mcp_profile = "<name>"` 启用。

```toml
[[mcp_profiles.crm.tools]]
name = "lookup_order"
description = "查询订单状态"
default = { status = "not_found" }
input_schema = { type = "object", properties = { id = { type = "string" } }, required = ["id"] }
responses = [{ when = { id = "88231" }, result = { status = "shipped" } }]
```

匹配规则、占位符、调用日志和支持透传 MCP 配置的适配器见 [mcp.md](mcp.md)。

## 一份真实模型的配置

```toml
[project]
name = "support-skill"

[agent]
adapter = "claude_code"
[agent.options]
model = "sonnet"
setting_sources = "project"     # 评测时不加载你的用户级技能与钩子

[llm]
provider = "anthropic"          # 需要 pip install 'skillloop[anthropic]' 与可用的凭据
[llm.roles.judge]
model = "claude-haiku-5-5"

[eval]
judge = "llm"
concurrency = 4
timeout_s = 900

[evolve]
max_iterations = 6
protect_tags = ["core"]
```
