# 判分与人工评审

判分器(Judge)把「一个用例 + 一次运行轨迹」变成一个 `Judgment`:`score`(0..1)、`passed`、`reason`,以及可选的 `labels`、`needs_review`。闭环里的版本比较、闸门、错题库全部建立在这些判分之上,所以判分器选错,后面的一切都会跟着偏。

- 项目级判分器:`skillloop.toml` 的 `[eval] judge`(默认 `exact`),选项写在 `[eval.judge_options]`,原样传给判分器构造函数。
- `judge` 可以是注册名,也可以是 `"包.模块:类名"`。`skillloop plugins` 列出当前可用的判分器。
- 判分器只会收到 `status == "ok"` 的轨迹。agent 报错、超时的运行由评测器直接记 0 分(`judge = "runner"`,标签 `agent_error` / `agent_timeout`),不经过判分器。

## 怎么选

| 你的答案长什么样 | 用 | 备注 |
|---|---|---|
| 一个确定的短字符串,输出里只有它 | `exact` | 最严格;输出多一个字就不过 |
| 输出是一段话,但必须包含某些关键内容 | `contains` | `expected` 可以是列表 |
| 输出要符合某种格式 | `regex` | |
| 一个数字 | `numeric` | 默认取输出里最后一个数 |
| 问答类的短答案:数字带单位、千分位、百分号,或一个名称 | `answer` | 理解「百万 / billion / 万」等量级词,忽略回复里顺带出现的年份;建议配合 `answer_regex` |
| 结构化 JSON | `json_match` | 期望是实际输出的子集即可 |
| 能用代码检查(跑测试、查文件、调接口) | `script` | 你自己的命令或函数 |
| 只能靠读来判断(开放式回答、按 rubric 打分) | `llm` | 建议配合人工抽检校准 |
| 以上几种要同时满足 | `composite` | |
| 暂时只信人 | `human` | 全部进评审队列 |

能用确定性判分器就不要用 `llm`:确定性判分器免费、可复现、不会被输出里的话术影响。

## 内置判分器

下面每个例子都是完整的 `skillloop.toml` 片段。未列出的选项不存在。

### exact

输出与 `expected` 归一化后(去首尾空白、转小写、连续空白压成一个空格)完全相等则通过。`expected` 是列表时,匹配任意一项即可。没有选项。

```toml
[eval]
judge = "exact"
```

### contains

`expected`(或列表里的每一项)归一化后出现在输出中。

```toml
[eval]
judge = "contains"

[eval.judge_options]
mode = "all"            # all(默认):每一项都要出现 | any:出现任意一项
case_sensitive = false  # 默认 false;为 true 时只压缩空白,不转小写
```

未通过时 `score` 是命中项的比例(3 项中 2 项 → 0.667),但 `passed` 仍为 false。用例没有 `expected` 时判 0 分并标记 `needs_review`。

### regex

输出(去首尾空白后)匹配正则。正则的来源按优先级:`case.meta["pattern"]` → 选项 `pattern` → `case.expected`。

```toml
[eval]
judge = "regex"

[eval.judge_options]
pattern = "^答案[::]\\s*\\d+$"
fullmatch = false   # 默认 false,即 search;true 时要求整段匹配
flags = "is"        # 默认 "is";由 i(忽略大小写)、m(多行)、s(. 匹配换行)组成
```

没有任何正则来源,或正则无法编译时,判 0 分并标记 `needs_review`。

### numeric

从输出里提取数字,与 `expected` 里的第一个数字比较。识别千分位(`1,234.5`)、科学计数法和百分号;一边带 `%` 而另一边不带时,会同时尝试乘/除 100 的写法。

```toml
[eval]
judge = "numeric"

[eval.judge_options]
abs_tol = 0.01   # 绝对容差,默认 1e-6
rel_tol = 0.0    # 相对容差(相对 expected),默认 0;实际容差取两者较大值
pick = "last"    # last(默认):输出里最后一个数 | first:第一个 | any:任意一个数匹配即可
```

`pick = "last"` 配合「最后一行写 `答案: <数字>`」这样的输出约定最稳;`any` 很宽松,输出里列了中间步骤时容易误判为通过。`expected` 不含数字时判 0 分并标记 `needs_review`。

### answer

为问答类数据集准备的宽容匹配:标准答案是 `2,602`、`36080 million`、`6.1596`、`March 1977`、`Zaire` 这样的短答案,`exact` 太严、`numeric`(取最后一个数)又太松时用它。

```toml
[eval]
judge = "answer"
answer_regex = 'FINAL ANSWER:\s*(.+)'   # 先把最终答案从回复里取出来,见 configuration.md

[eval.judge_options]
tolerance = 0.0              # 通过所需的相对容差;0 表示归一化后相等
tolerances = [0.01, 0.05]    # 可选:更宽的档位,只用于给部分分
```

规则:

- **期望答案里有数字**:回复中必须有数字与之相等(在 `tolerance` 内)。千分位、货币符号、括号、负号都会归一化;量级词(thousand / million / billion / trillion / mn / bn / 千 / 万 / 亿 等)两边都写了就按换算后的值比较(`36.08 billion` = `36080 million`),只有一边写了也接受裸数字相等(题目已规定单位时,`36080 million` 与 `36,080` 视为一致);一边是百分数一边是小数时按 ×100 比较。
- **年份**:回复里形如 1900–2100 的整数被当作顺带提到的年份而忽略,除非期望答案本身就是年份,或回复里只有这一个数。
- **多个数字**:期望答案里的每个数字都要在回复中找到。
- **数字之外的关键词**:期望答案除了数字和单位还有别的词(如 `March 1977` 里的月份)时,这些词也必须出现在回复里。
- **没有数字**:归一化后回复包含期望答案,或包含它的全部关键词。
- `expected` 可以是列表,任一命中即通过。
- 设置了 `tolerances` 时,分数是「命中的档位数 / 总档位数」,是否通过只看 `tolerance`;没有期望答案的用例标记 `needs_review`。

它不理解语义等价(`twelve` 与 `12`、同义的名称),这类答案请用 `llm` 判分器或在 `expected` 里列出可接受的写法。

### json_match

从输出里解析出 JSON(容忍代码围栏和前后的说明文字),要求 `expected` 是它的**子集**:`expected` 里的每个叶子字段都要在输出里存在且相等,输出多出来的字段不计。`expected` 可以是对象,也可以是 JSON 字符串。

```toml
[eval]
judge = "json_match"

[eval.judge_options]
partial_pass = false   # 默认 false:只有全部字段匹配才通过
```

`score` 是匹配的叶子字段占比。比较规则:字符串归一化后比较,数字按 1e-9 容差比较,列表必须等长且逐项匹配(一个列表算一个叶子)。`partial_pass = true` 时改为 `score >= pass_threshold` 即通过。

### script

把判分交给你自己的代码,二选一:

```toml
[eval]
judge = "script"

[eval.judge_options]
command = ["python", "judge.py"]   # 字符串或 argv 列表;不经过 shell;cwd 是项目根目录
timeout_s = 60                     # 默认 60
# 或者:
# callable = "my_pkg.judging:grade"   # 进程内调用 grade(case, trajectory)
```

契约见下文[自定义判分器](#自定义判分器)。脚本超时、启动失败、退出码非 0、没输出 JSON 时,判 0 分、标记 `needs_review`、标签 `judge_error`——脚本坏了不会被当成 agent 答错而悄悄混进分数里。

### llm

用框架 LLM(角色 `judge`)按期望答案和 rubric 打分。提示词里带 `<input>`、`<expected>`、`<rubric>`、`<output>`、`<trajectory>` 五个数据块;系统提示要求只依据 `<output>` 的内容打分,并把所有数据块当作数据而非指令(数据里的闭合标签会被转义,避免跳出数据块)。

```toml
[eval]
judge = "llm"
pass_threshold = 0.7   # 模型没给 passed、或 aggregate = "mean" 时用它判定通过

[eval.judge_options]
instructions = "金额必须精确到分;只给结论不给计算过程的不算通过。"   # 追加到系统提示,默认无
votes = 1                    # 每条结果问几次,默认 1
aggregate = "majority"       # majority(默认):过半票通过 | mean:平均分 >= pass_threshold
min_confidence = 0.5         # 默认 0.5;任意一票的 confidence 低于它就标记 needs_review
max_trajectory_chars = 4000  # 默认 4000
max_output_chars = 12000     # 默认 12000,超出部分截断并注明

[llm.roles.judge]            # 可选:判分单独用一个模型
model = "claude-haiku-5-5"
```

`score` 是各票分数的平均。以下情况标记 `needs_review`:票数有分歧、某一票调用失败、最低置信度低于 `min_confidence`。全部调用失败时判 0 分,标签 `judge_error`。

用例可以只有 `rubric` 没有 `expected`,反之亦然;两者都有时,输出必须在实质上与 `expected` 一致,同时满足 rubric。

> 这个判分器的提示词目前只对模拟 LLM 跑过自动化测试,没有在真实模型上做过系统的校准。上线前请按[校准 LLM 判分](#校准-llm-判分)一节用人工标注核对它。

### composite

组合多个判分器。

```toml
[eval]
judge = "composite"

[eval.judge_options]
mode = "weighted"   # weighted(默认)| all | any
judges = [
  { name = "regex", options = { pattern = "答案[::]" }, weight = 1.0 },
  { name = "numeric", options = { abs_tol = 0.01 }, weight = 3.0 },
]
```

- `weighted`:`score` 是按 `weight`(默认 1.0)的加权平均,`score >= pass_threshold` 即通过。
- `all`:每个子判分器都通过才通过,`score` 取最小值。
- `any`:任一通过即通过,`score` 取最大值。

`judges` 的元素也可以只写名字(`judges = ["exact", "contains"]`)。任一子判分器标记 `needs_review`,整体即标记;`reason` 是各子判分器理由的拼接,明细在 `details.judges`。

### human

每条结果都进人工评审队列。

```toml
[eval]
judge = "human"

[eval.judge_options]
fallback = "contains"        # 可选:用它给一个临时分数
fallback_options = {}        # 传给 fallback 判分器的选项
```

不配 `fallback` 时,人工标注前所有结果都按**不通过(0 分)**计。这意味着在没人标注的情况下跑 `evolve`,闭环会认为所有用例都失败。要么配一个 `fallback` 让闭环先按临时分数运转,要么先标注再迭代。

## 单个用例覆盖判分器

数据集里的用例可以带 `judge` 字段,只对这一条生效:

```json
{"id": "fmt-01", "input": "按模板输出工单号", "expected": "^T-\\d{6}$", "judge": "regex"}
{"id": "calc-01", "input": "应退多少钱?", "expected": 128.5, "judge": "numeric"}
```

`[eval.judge_options]` 只属于项目级判分器,不会传给覆盖用的判分器。覆盖用判分器的选项写在 `[eval.judges.<name>]` 下,没写就用默认选项:

```toml
[eval.judges.numeric]      # 所有 "judge": "numeric" 的用例都用这组选项
abs_tol = 0.01
```

## 分数怎么聚合

`[eval] trials`(默认 1)是每个用例重复运行的次数。agent 不稳定时调大它,用运行成本换判断的可靠性。一次评测(`EvalReport`)里:

- **用例分数** = 该用例各次 trial 的 `score` 平均。
- **用例是否通过** = 通过的 trial **严格过半**。平局算不通过(`trials = 2` 时一过一不过 → 不通过),所以 `trials` 建议取奇数。
- **`mean_score`** = 用例分数按用例 `weight`(默认 1.0)加权平均。
- **`pass_rate`** = 各用例的 trial 通过比例,按 `weight` 加权平均。注意它是比例的平均,不是「通过的用例数 / 用例数」:三次里过两次的用例贡献 0.667。
- 错题库和分析器看到的是每个未通过用例里**分数最低**的那次 trial。

`[eval] pass_threshold`(默认 0.7)只在判分器没有自己决定 `passed` 时起作用:`llm`(模型没返回 `passed`,或 `aggregate = "mean"`)、`composite` 的 `weighted` 模式、`json_match` 的 `partial_pass`、`script` 只返回了分数、导入标注表时只填了 `human_score`。`exact`、`contains`、`regex`、`numeric` 自己决定通过与否,不看阈值。

闸门比较哪个指标由 `[evolve] metric` 决定:`mean_score`(默认)或 `pass_rate`。

## needs_review 从哪来

| 来源 | 情形 |
|---|---|
| `llm` | 多票分歧、某票调用失败、置信度低于 `min_confidence`、没有配置 LLM |
| `human` | 总是 |
| `script` | 脚本超时 / 崩溃 / 输出不可用;或脚本自己返回 `"needs_review": true` |
| `contains` `regex` `numeric` `json_match` | 用例本身有问题:缺 `expected`、正则无效、`expected` 不是数字或不是 JSON |
| `composite` | 任一子判分器标记 |
| 评测器 | 判分器抛了异常(0 分,标签 `judge_error`) |

最后两类值得留意:它们说明的是**用例或判分器**有问题,而不是 agent 答错了。

## 人工评审流程

### 1. 进入队列

`skillloop evolve`、`skillloop eval` 以及 Web / MCP 发起的评测结束后,所有标记了 `needs_review` 的结果自动进入评审队列(SQLite 的 `reviews` 表,状态 `pending`)。`skillloop status` 会显示等待中的数量。

想抽检没被标记的结果(比如审计 LLM 判分的质量),用 Python API:

```python
from skillloop import SkillLoop
from skillloop.eval.review import enqueue_reviews

es = SkillLoop("my_project")
report = es.project.load_eval(es.evals()[0]["eval_id"])   # 最近一次评测
enqueue_reviews(es.project, report, mode="failures", sample_rate=0.2, seed=0)
```

`mode`:`needs_review`(默认,只收被标记的)、`failures`(被标记的 + 所有未通过的)、`all`。`sample_rate` 在此之外再对剩余结果做确定性抽样。

### 2. 标注

**Web 操作台**:Review 页列出待评审结果(输入、期望、输出、自动判分),逐条给通过 / 不通过和备注。

**CSV 标注表**(适合交给不用命令行的同事):

```bash
skillloop review list -p my_project                      # 看队列(--status pending|done)
skillloop review export -p my_project                    # 导出最近一次评测 → outputs/review_<eval_id>.csv
skillloop review export sheets/r1.csv --eval ev_xxx -p my_project   # → my_project/sheets/r1.csv
# 用 Excel / 表格软件填写后:
skillloop review import my_project/sheets/r1.csv -p my_project
```

两个命令的路径都相对于**当前目录**,且文件必须位于项目目录内;`export` 不给路径时写到 `<project>/outputs/review_<eval_id>.csv`。

标注表是 UTF-8(带 BOM,Excel 直接打开中文不乱码),包含该次评测的**全部**结果而不只是队列里的,列为:

`eval_id` `case_id` `trial` `input` `expected` `output` `auto_score` `auto_passed` `auto_reason` `judge` `labels` `human_score` `human_passed` `comment` `reviewer`

评审人只填后四列中的 `human_score` 和/或 `human_passed`(以及 `comment`、`reviewer`):

- `human_passed` 接受 `true/false`、`1/0`、`yes/no`、`pass/fail`、`是/否`、`通过/不通过`、`✓/✗` 等。
- 只填 `human_passed`:分数记为 1 或 0。只填 `human_score`(0..1):按 `pass_threshold` 推出是否通过。
- 两列都空的行被跳过(计入 `unfilled`);填了但读不懂的行被跳过并报告行号和原因,不会中断整个导入。
- 不要改前三列,它们是定位结果的键。

`skillloop outputs`(以及每次 `evolve` 结束)还会把最佳版本最近一次评测的标注表写到 `outputs/review_sheet.csv`。

**MCP**:`list_reviews`、`submit_review`。**Python**:`es.submit_review(eval_id, case_id, trial, passed=..., score=..., comment=..., reviewer=...)`。

### 3. 人工结论如何生效

人工结论不改写评测文件,而是存在 `reviews` 表里,在**读取评测时**覆盖自动判分(`apply_reviews`):对应结果的 `score` / `passed` 换成人工值,`reason` 换成备注,`source` 变为 `"human"`,`needs_review` 清除,原自动判分保留在 `details.auto` 里。

所以补一条标注之后**不需要重跑评测**,下列地方立刻用上新结论:

- `skillloop compare`、Web 的版本比较、MCP `compare_versions`
- 查看评测明细(`es.eval()`、Web 评测页、MCP `get_eval`)
- 进化闭环:之后每一轮读取已有评测做版本比较和闸门判断时

版本 manifest 里缓存的各 split 得分摘要也会随之刷新:提交标注(`submit_review`、Web、MCP)或导入标注表后,对应版本在该 split 上的分数按覆盖后的结果重算,所以 `skillloop versions` / `status` 的数字、闭环挑选父版本的排序、以及「val 分数达到 `target_score` 就停止」的判断都会用上人工结论。

### 校准 LLM 判分

积累了一些人工标注之后,看看判分器和人的一致程度:

```python
from skillloop import SkillLoop
from skillloop.eval.review import agreement

es = SkillLoop("my_project")
stats = agreement(es.project)          # 或 agreement(es.project, eval_id="ev_xxx")
print(stats["n"], stats["pass_agreement"], stats["kappa"], stats["mean_abs_score_diff"])
for d in stats["disagreements"]:
    print(d["case_id"], d["auto_passed"], d["human_passed"], d["auto_reason"], "|", d["comment"])
```

返回 `n`(可比较的标注数)、`pass_agreement`(通过/不通过一致率)、`kappa`(Cohen's kappa,扣除了碰巧一致)、`mean_abs_score_diff`,以及每条分歧的明细。读分歧明细,把反复出现的误判原因写进 `[eval.judge_options] instructions`,再评一轮。

统计只包含**同时有自动判分和人工结论**的记录。通过队列(自动入队或 `enqueue_reviews`)进来的结果都满足;直接对一条不在队列里的结果调 `submit_review` 时没有自动判分可比,不计入。只审「被标记」的结果会让样本偏向疑难案例,估出来的一致率偏低——想要无偏的估计,用 `sample_rate` 随机抽。

## 自定义判分器

### 方式一:script(任何语言)

命令从 stdin 收到一个 JSON 对象,向 stdout 打印一个 JSON 对象:

```
stdin   {"case": {id, input, expected, rubric, tags, meta, ...},
         "output": "<agent 的最终回答>",
         "trajectory": {output, steps, status, usage, artifacts, ...}}

stdout  {"score": 0..1, "passed": true|false, "reason": "...", "labels": ["..."],
         "needs_review": false, "details": {...}}
```

`score` 和 `passed` 至少给一个:只给 `passed` 时分数记 1 或 0;只给 `score` 时按 `pass_threshold` 判定通过。其余字段可选。只打印一个裸的 `true` / `false` 或一个数字也可以。

一个完整的例子——检查回答是否以规定的格式收尾,并且金额正确。`my_project/judge.py`:

```python
import json
import re
import sys

data = json.load(sys.stdin)
expected = float(data["case"]["expected"])
last_line = data["output"].strip().splitlines()[-1] if data["output"].strip() else ""
m = re.fullmatch(r"答案[::]\s*(-?\d+(?:\.\d+)?)", last_line.strip())
if not m:
    print(json.dumps({"score": 0.0, "passed": False, "reason": "最后一行不是「答案: <数字>」", "labels": ["format"]}))
elif abs(float(m.group(1)) - expected) > 0.01:
    print(json.dumps({"score": 0.3, "passed": False, "reason": f"格式正确但金额应为 {expected:g}", "labels": ["wrong_amount"]}))
else:
    print(json.dumps({"score": 1.0, "passed": True, "reason": "格式和金额都正确"}))
```

```toml
[eval]
judge = "script"

[eval.judge_options]
command = ["python", "judge.py"]
```

`labels` 会进入错题库条目的标签,给分析器多一条线索。agent 在工作目录里生成的文本文件在 `trajectory.artifacts`(相对路径 → 内容)里,需要检查产出文件时从那里读。

`callable = "pkg.module:func"` 是同一件事的进程内版本:`func(case, trajectory)` 收到的是 `Case` 和 `Trajectory` 对象,返回上面那样的 dict、一个 bool、一个数字,或直接返回 `Judgment`。模块必须能被运行 skillloop 的 Python 导入。

### 方式二:Judge 子类

需要选项、需要调框架 LLM,或想在多个项目间复用时,写一个子类:

```python
from skillloop import JUDGES, Case, Judge, Judgment, Trajectory


@JUDGES.register("max_length")
class MaxLengthJudge(Judge):
    """回答必须包含期望内容,且不超过 max_chars 个字符。"""

    name = "max_length"

    def judge(self, case: Case, trajectory: Trajectory) -> Judgment:
        limit = int(self.options.get("max_chars", 200))
        out = trajectory.output.strip()
        if str(case.expected) not in out:
            return self.verdict(0.0, "缺少期望内容", passed=False, labels=["wrong_answer"])
        if len(out) > limit:
            return self.verdict(0.5, f"内容正确但有 {len(out)} 字,上限 {limit}", passed=False, labels=["too_long"])
        return self.verdict(1.0, "正确且简洁", passed=True)
```

- `self.options` 是 `[eval.judge_options]` 的内容;`self.ctx` 提供 `llm`(框架 LLM,调用时请传 `role="judge"`)、`pass_threshold`、`project_root`。
- `self.verdict(score, reason, **kw)` 会把分数夹到 0..1;不传 `passed` 时按 `pass_threshold` 推出。
- 判分器可能被多个线程同时调用,不要在实例上存每次调用的状态。
- 抛异常不会中断评测:该结果记 0 分、`needs_review`、标签 `judge_error`。

最省事的做法是把判分器写在项目目录里的一个 `.py` 文件中,并在 `[project] plugins` 里列出模块名(见 [configuration.md](configuration.md))。除此之外注册方式与适配器相同:在你的代码里 `@JUDGES.register("name")`(需保证该模块在评测前被导入);不注册,配置里直接写 `judge = "my_pkg.judging:MaxLengthJudge"`;或通过 entry point 组 `skillloop.judges` 发布。用 CLI 时第二种最省事,因为它不要求你另外安排导入。
