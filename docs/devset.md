# 开发集管道

进化闭环需要一个测试集。手里没有、或者只有寥寥几条的时候,开发集管道可以从需求文档生成一份初稿:

```
规则抽取 → 候选生成 → 场景演化 → 过滤 → 平衡 → 报告
```

它产出的是**初稿**。生成的期望答案出自模型,没有人核对过;在让闭环依据它做决定之前,先读[用之前先审](#用之前先审)。

> 四个阶段的提示词目前只对模拟 LLM 跑过自动化测试,没有在真实模型上系统验证过生成质量。第一次用真实模型跑时,请把报告和用例从头看一遍。

## 什么时候用

- **冷启动**:有需求文档,没有测试集。
- **补覆盖**:已有测试集,但某些验收标准一条用例都没有。管道会对照已有数据集去重,只补新的。
- **加难度**:已有用例都太直白,想要边界、对抗、换说法的变体。

不适合的情形:你已经有真实的线上样本或历史工单。真实数据永远比生成的好,直接整理成 `dataset.jsonl` 即可。

## 运行

```bash
skillloop devset -p my_project            # 只生成,写到 .skillloop/devset/,不动数据集
skillloop devset -p my_project --merge    # 生成并追加到项目数据集([project] dataset)
```

需求文档(`[project] requirement`)为空,或者一条规则都抽不出来时,命令报错退出,不会产生半成品。

其他入口做的是同一件事:

| 入口 | 用法 |
|---|---|
| Python | `SkillLoop("my_project").build_devset(merge=False)`,返回 `{n_rules, n_cases, n_dropped, merged, paths, report_md}` |
| 后台任务 | `es.submit("devset", merge=True)`,返回 job id,可查进度、可取消 |
| Web 操作台 | 概览页「生成开发集」按钮,旁边的勾选框对应 `--merge` |
| MCP | 工具 `build_devset`(参数 `merge`),返回 `job_id`,用 `job_status` 轮询 |

建议先不带 `--merge` 跑一次,看过报告和用例再决定合并。

## 六个阶段

每个阶段是 `skillloop.devset` 里的一个普通函数,可以单独调用和测试。进度上报的阶段名在括号里。

### 1. 规则抽取(`devset.extract_rules`)

LLM 角色 `devset_rules`。把需求文档拆成一条条**可独立验证**的规则——业务规则、约束、验收标准、格式要求、文档点名的边界情况;复合句会被拆开,背景和动机会被略过。每条规则有 `id`(短横线小写)、`text`、`category`。

会丢弃:文本为空的规则;与已有规则几乎相同的规则(词集相似度 ≥ 0.9)。重复的 `id` 会自动加后缀。

这一步决定了后面的一切。规则抽漏了,那条验收标准就不会有用例;规则抽得太碎,用例会集中在琐碎的点上。需求文档里把验收标准写成一行一条的清单,抽取效果最好。

### 2. 候选生成(`devset.generate_candidates`)

LLM 角色 `devset_cases`,**每条规则调用一次**,要求生成 `candidates_per_rule` 条用例:真实用户可能发来的任务,正确回答需要用到这条规则,题面不能把规则或答案说出来。每条用例要有一个可检查的短 `expected`,或一段 `rubric`(验收标准),或两者都有。

模型多给的用例会被截断到 `candidates_per_rule` 条。模型没标难度时,按 easy / medium / hard 轮流分配。

### 3. 场景演化(`devset.evolve_scenarios`)

LLM 角色 `devset_evolve`,**每条规则每轮调用一次**。对每条已有用例生成一个变体,由模型挑选最能补充覆盖的一种:

| kind | 含义 |
|---|---|
| `harder` | 更多步骤、更多数据,或多个条件叠加 |
| `edge` | 边界值、空的 / 缺失的 / 古怪的输入、少见但合理的情形 |
| `adversarial` | 用户施压、打岔或误导,想让 agent 违反规则 |
| `paraphrase` | 同一情形,换一种措辞、语气或语体 |

变体有自己的 `expected` / `rubric`。`evolve_rounds` 大于 1 时,第 n 轮在第 n-1 轮的变体上继续演化;设为 0 则跳过本阶段。输出是「原候选 + 所有变体」。

会丢弃:没有指明来源用例(`parent`)、或指向不存在的用例的变体。

### 4. 过滤(`devset.filter_cases`)

先做确定性检查,按顺序,命中即丢弃:

| 检查 | 丢弃原因(报告里的写法) |
|---|---|
| 题面为空或不足 8 个字符 | `schema: input is empty or too short` |
| 既没有 `expected` 也没有 `rubric` | `schema: neither expected answer nor rubric` |
| 期望答案原样出现在题面里 | `answer leak: expected answer appears in the input` |
| 与已保留的用例、或**项目现有数据集**里的用例近似重复 | `near-duplicate of <id>` |

近似重复的判定:题面切成词集(小写英文单词 + 中文相邻两字组合),Jaccard 相似度 ≥ `dedupe_threshold`。很短的答案(是/否、单个数字、一个词)出现在题面里不算泄漏。

然后是 LLM 审核,角色 `devset_filter`,每批 12 条:题意含糊或无法作答、期望答案或 rubric 有错或不能从需求文档推出、并没有真正考到它声称的规则、题面泄漏答案——判为无效的用例被丢弃,原因记为 `llm audit: <模型给的理由>`。

### 5. 平衡(`devset.balance_cases`)

不调用 LLM,完全确定性(只依赖用例 id)。

1. 每条规则最多保留 `max_per_rule` 条,保留时按难度轮流取,尽量三种难度都有。
2. 在各规则间轮流挑选,直到凑满 `target_size`;每次从当前规则里挑「所属难度离配额缺口最大」的那条,让整体难度分布向 `difficulty_mix` 靠拢。

会丢弃:超出每规则上限的(`over max_per_rule (N) for <rule>`)、超出总量的(`over target_size (N)`)。

规则数多于 `target_size` 时,轮流挑选意味着排在后面的规则可能一条都轮不到——报告的「未覆盖规则」会列出来。这时调大 `target_size`。

### 6. 报告(`devset.report`)

生成 `report.md`:管道漏斗(每个阶段后剩多少条)、规则覆盖表(每条规则在各难度下的用例数)、难度分布、来源分布、丢弃原因统计、未覆盖规则清单。

## 配置

```toml
[devset]
target_size = 24          # 最终用例数上限
candidates_per_rule = 3   # 候选生成:每条规则生成几条
evolve_rounds = 1         # 场景演化轮数;0 = 不演化
max_per_rule = 6          # 平衡:每条规则最多保留几条
dedupe_threshold = 0.85   # 过滤:题面相似度达到多少算重复(0..1,越小去重越狠)

[devset.difficulty_mix]   # 平衡:目标难度比例(自动归一化)
easy = 0.3
medium = 0.4
hard = 0.3

[llm.roles.devset_cases]  # 可选:按角色换模型;角色名 devset_rules / devset_cases / devset_evolve / devset_filter
model = "claude-opus-5-5"
```

以上都是默认值。粗略的调用量:1 次规则抽取 + 规则数 × (1 + `evolve_rounds`) 次生成 + 约(候选总数 ÷ 12)次审核。

## 产出

写到 `<project>/.skillloop/devset/`,每次运行覆盖上一次的结果,并登记进产物索引:

| 文件 | 内容 | 产物类型 |
|---|---|---|
| `rules.json` | 抽取出的规则 `[{id, text, category}]` | `devset_rules` |
| `candidates.jsonl` | 演化之后、过滤之前的全部用例 | `devset_candidates` |
| `devset.jsonl` | 最终用例(过滤、平衡之后) | `devset` |
| `report.md` | 上面说的报告 | `devset_report` |

带 `--merge` 时,`devset.jsonl` 的用例被追加到项目数据集末尾,并记一条 `devset.merge` 审计事件。与数据集里已有 id 冲突的用例会自动改名(加 `_2`、`_3`…)。

### 一条生成的用例

```json
{"id": "dev_refund-window_2_edge", "input": "我正好是第 30 天申请退款,还能自动退吗?", "expected": "可以自动退款,30 天当天仍在期限内", "rubric": "明确说明第 30 天仍在期限内;不要求转人工", "tags": ["refund-window", "medium"], "meta": {"rule": "refund-window", "difficulty": "medium", "origin": "evolved:edge", "parent": "dev_refund-window_2"}}
```

- `id`:候选是 `dev_<规则 id>_<序号>`,变体是 `<来源用例 id>_<kind>`。
- `tags`:固定为 `[规则 id, 难度]`。数据划分按第一个标签分层,所以每条规则的用例会被分散到 train / val / test。
- `meta.origin`:`generated` 或 `evolved:<kind>`;变体另有 `meta.parent`。
- `expected` 和 `rubric` 至少有一个;为空的字段不写入文件。
- 没有 `split` 字段:合并后由闭环按 id 哈希稳定地划分。

(示例中的文字是示意;字段名和结构与实际输出一致。)

## 用之前先审

管道里的 LLM 审核能拦下一部分坏用例,但出题和审题的是同一类模型,它们会在同一个地方犯错。一份没人看过的生成测试集,最常见的问题是:

- **期望答案是错的。** 这最伤:闭环会把 Skill 朝着错误答案「优化」,而且分数还会上涨。
- **期望答案只是众多正确答案中的一种。** 配 `exact` 或 `contains` 判分时,答对了也判不过。
- **用例其实没考到它标称的规则**,覆盖表看着漂亮,实际是空的。
- **规则抽取阶段漏了或曲解了某条验收标准**,后面再怎么生成也补不回来。

建议的做法:

**1. 先看 `rules.json` 和 `report.md`。** 规则是不是需求文档的忠实拆分?有没有未覆盖的规则?丢弃原因里有没有异常多的 `llm audit`?规则不对就改需求文档的写法重跑,不要在错误的规则上继续。

**2. 逐条读 `devset.jsonl`。** 二三十条用例,认真读一遍用不了多久。它是 JSONL,直接改:改掉错的 `expected`,把过于死板的 `expected` 换成 `rubric`,删掉没意义的用例。改完后手工追加到数据集,或者先 `--merge` 再去改 `dataset.jsonl`——数据集文件本身就是事实来源,随时可以手工编辑。

**3. 选一个和答案形态相称的判分器。** 生成的 `expected` 是自由文本,几乎不可能和 agent 的输出逐字相同。`exact` 基本不适用;答案是关键短语时用 `contains`,开放式回答用 `llm`(见 [judges.md](judges.md))。

**4. 用基线评测反查用例。** 合并后对初版 Skill 跑一次评测,把失败的结果送去人工看一遍:

```bash
skillloop eval --split train -p my_project
skillloop review export -p my_project      # 标注表里 input / expected / output 并排
```

并排读 `expected` 和 `output`。如果 agent 的回答其实是对的,问题在用例:回到 `dataset.jsonl` 修正 `expected`(修正后重新评测),而不是只在标注表里把这一条标成通过——人工标注修正的是「这一次的判分」,用例本身的错误答案还留在数据集里,下一个版本还会被它误判。

**5. 把生成的用例和你确信的用例区分开。** 给手写的、核对过的用例打一个标签(比如 `core`),并设 `[evolve] protect_tags = ["core"]`:这些用例不允许退化。生成的用例用来发现问题,核对过的用例用来把关。

## 已知边界

- 某条规则的生成调用失败(模型返回了无法解析的内容)时,管道不会报错,而是该规则没有用例——它会出现在报告的「未覆盖规则」里。每次运行后都看一眼那一节。
- 管道总是带 LLM 审核运行。只有直接调用 `DevsetPipeline(project, llm).run(llm_check=False)` 才能跳过。
- 重复运行 `--merge` 会让数据集持续变大。已经合并过的题面会在过滤阶段被去重,但候选池通常比 `target_size` 大,平衡阶段会改从剩下的候选里再挑满 `target_size` 条——离线演示项目连跑两次,数据集是 24 → 48 → 72 条。想要的是「补到够」而不是「再来一批」时,合并一次即可;之后用不带 `--merge` 的运行查看还能补什么。
- 生成的用例不带 `files`(运行前写入工作目录的文件)。需要附带文件的任务请手写用例。
