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

## `[dataset]` — 表格数据集的列映射

数据集的列名不是默认别名时(常见于现成的 CSV 基准),在这里指明哪一列是什么。留空的项回退到别名规则。

```toml
[dataset]
input_column = ""       # 任务输入所在的列
expected_column = ""    # 期望答案所在的列
id_column = ""          # 稳定的用例 id 所在的列
rubric_column = ""      # 单条验收标准所在的列
tag_columns = []        # 这些列的值成为标签;第一个标签用于 train/val/test 的分层
```

配置了但数据里没有的列会直接报错并指出是哪一列。没被映射的列保留在 `meta` 里,不会交给 agent 或分析器。`.csv` / `.tsv` / `.json` 数据集是只读的:要用 `add_cases` 或 `devset --merge` 追加用例,先把它转成 `.jsonl`。

## `[agent]` — 被测 agent

```toml
[agent]
adapter = "mock"        # 注册名,或 "包.模块:类名"
mcp_profile = ""        # 评测时挂给 agent 的 Mock MCP Profile 名(见 [mcp_profiles])
task_file = ""          # 固定的任务说明文件,随每个用例一起交给 agent;不参与进化
data_dirs = []          # 参考资料目录(文档库、代码库),只读地出现在每个用例的工作目录里

[agent.options]         # 原样作为关键字参数传给适配器构造函数
```

**`task_file`**:写「不管 Skill 怎么变都成立」的东西——角色、资料在哪、答案必须用什么格式。它和用例输入拼在一起交给 agent(文件里写 `{input}` 可以指定用例输入插入的位置,否则追加在末尾);用例本身、错题库和分析器看到的仍是原始输入,分析器另外会被告知这段固定说明,避免把它重复教进 Skill。把答案格式放在这里而不是 Skill 里,是为了让判分所依赖的格式不会被进化改掉。

**`data_dirs`**:每个目录以自己的名字链接进用例工作目录(不复制,多大的语料都一样),并在提示里告知 agent。目录必须在项目内,名字不能重复。`claude_code` 适配器会同时把真实路径加入允许访问的目录;自己写的适配器可以从 `request.data_dirs` 读到这些路径。

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
max_retries = 0         # agent 报错或超时后重跑的次数(在干净的工作目录里;不算作额外的 trial)
answer_regex = ""       # 从 agent 回复里定位最终答案的正则,见下
cache = true            # 复用已完成的 agent 运行,见下
concurrency = 4         # 并行运行的用例数
timeout_s = 600.0       # 单个用例的超时(秒);超时记 0 分,状态 timeout
pass_threshold = 0.7    # 分级判分器:分数 ≥ 该值算通过
keep_workdirs = false   # 保留每个用例的工作目录(.skillloop/runs/),排查问题时打开

[eval.judge_options]    # 项目级判分器的选项

[eval.judges.numeric]   # 用例里用 "judge": "numeric" 覆盖时,该判分器的选项
abs_tol = 0.01
```

`trials > 1` 时,一个用例的分数是各次的平均,多数次通过才算通过(平局算不通过)。

**`cache`**:一次 agent 运行成功后,它的轨迹按「适配器及其设置 + Skill 内容 + 用例 + 实际提示 + 第几次 trial」缓存在 `.skillloop/cache/runs/`。之后遇到完全相同的组合就直接复用,不再调用 agent——评测被取消或进程中断后重新发起,只会补跑没完成的用例;内容相同的 Skill 也不会被重复评测。判分每次都重做,所以换判分器或补人工标注不受缓存影响。复用的运行费用记为 0,并在用量里带 `cached` 计数。失败和超时的运行不缓存。

缓存不感知 `data_dirs` 里文件内容的变化,也不感知 agent 背后模型的更新:这两种情况下删除 `.skillloop/cache/` 或设 `cache = false`。进程内函数适配器(`FunctionAdapter`)没有可识别的设置,不参与缓存。

**`answer_regex`**:agent 的回复往往是一段推理加一个答案,直接对整段判分容易误判(比如把页码当成答案)。设置后取**最后一处**匹配(有捕获组取第 1 组)作为判分对象,完整回复保留在轨迹的 `full_output` 里。回复里找不到匹配时仍按整段判分,并给结果加上 `no_final_answer` 标签,分析器能据此发现「没按格式作答」。它应当与 `task_file` 里的格式要求配对使用:

```toml
[eval]
answer_regex = 'FINAL ANSWER:\s*(.+)'
```

## `[evolve]` — 进化闭环

```toml
[evolve]
# 规模与停止
max_iterations = 8              # 一次 evolve 最多迭代轮数
candidates_per_iteration = 2    # 每轮评测的候选数
patience = 3                    # 连续这么多轮没有提升则停止
target_score = 1.0              # 最佳版本的 val 分数达到该值则停止
frontier_size = 3               # 保留多少个可作父版本的版本;长期无提升时会轮换父版本
max_cost_usd = 0.0              # 本次 evolve 的 agent 运行费用达到该值后不再开始新一轮(0 = 不限)

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
min_net_fixed = 0               # 要求「修复数 − 退化数」不少于该值;见下
protect_tags = []               # 带这些标签的用例不允许退化
require_human_approval = false  # 闸门通过后是否等待人工 `skillloop promote`
confirm_on_test = false         # 是否额外要求 test 上不下降(会让 test 参与决策,不再是纯 held-out)

# 护栏
max_skill_chars = 24000         # Skill 总字符数上限
max_growth_ratio = 2.5          # 候选相对父版本的体积上限倍数(父版本不足 2000 字符时按 2000 计)
leak_check = true               # 拒绝把标准答案 / 原题抄进 Skill 的候选
```

说明:

- 测试集很小、分不出 val 时,会退化为在 train 上选择并给出警告(有过拟合风险)。建议至少十几条用例。
- `leak_check` 只应在 Skill 的职责就是承载答案所依赖的参考知识时关闭(术语表、查找表)。对教流程的 Skill,关掉它等于允许背题。
- `max_cost_usd` 只统计适配器上报了 `cost_usd` 的 agent 运行(目前是 `claude_code`),不含框架 LLM 的开销;它在每轮开始前检查,所以实际花费会超出一轮以内的量。
- `min_net_fixed = 1` 是给「agent 有随机性、用例又少」准备的:多跑几次 trial 后,分数可能只因为某一次运气好而上涨,没有任何用例真正从不通过变为通过。设为 1 就要求至少有一个净翻转,否则视为噪声不予提升。用分级判分器(如 `llm` 打 0–1 分)时保持 0,因为那里分数上涨而不翻转是真实的进步。
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
