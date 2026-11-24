# MCP 集成

skillloop 和 MCP 有两层关系,互相独立:

- **skillloop 作为 MCP 服务**:让任何支持 MCP 的 agent(Claude Code、Codex、Cursor、nanobot…)驱动进化闭环——建项目、发起进化、轮询进度、取回最佳 SKILL.md。
- **Mock MCP Profiles**:评测时给**被测 agent** 挂一个确定性的假 MCP 服务,让依赖外部工具的 Skill 可以离线、可复现地评测,不碰真实系统。

两者都是纯标准库实现(换行分隔 JSON 的 stdio 传输),不需要安装 `mcp` 包。

## 一、skillloop 作为 MCP 服务

### 启动

```bash
skillloop mcp -w /path/to/workspace
```

`-w` 可以是一个项目目录,也可以是装着多个项目的目录(规则同 [Web 操作台](web-console.md#启动))。服务从 stdin 读、向 stdout 写协议消息,日志只写 stderr。

### 客户端配置

通用的 `mcpServers` JSON(Claude Code 的 `.mcp.json`、`--mcp-config` 文件,以及大多数 MCP 客户端都是这个形状):

```json
{
  "mcpServers": {
    "skillloop": {
      "command": "skillloop",
      "args": ["mcp", "-w", "/path/to/workspace"]
    }
  }
}
```

`skillloop` 不在 `PATH` 上时,把 `command` 写成虚拟环境里的绝对路径(如 `/path/to/.venv/bin/skillloop`)。

Claude Code 命令行注册(`--` 之后是服务命令;`-s` 选择作用域 `local` / `user` / `project`,默认 `local`):

```bash
claude mcp add skillloop -- skillloop mcp -w /path/to/workspace
claude mcp add -s project skillloop -- skillloop mcp -w /path/to/workspace
```

单次无头调用时直接指定配置文件:

```bash
claude -p --mcp-config ./mcp.json --strict-mcp-config --allowedTools "mcp__skillloop" \
  "用 skillloop 的工具告诉我当前最佳版本和它的 val 分数"
```

在 Claude Code 里工具名是 `mcp__skillloop__<tool>`;`--allowedTools "mcp__skillloop"` 放行整个服务。只放行个别工具时要列全——模型可能选择 `list_projects` 或 `get_metrics` 这类你没想到的工具来回答同一个问题。

### 工具

共 29 个。除 `list_projects` 和 `create_project` 外,每个工具都接受可选的 `project` 参数(项目目录名);工作区里只有一个项目时可以省略。下表由服务实际注册的工具生成,**粗体**为必填参数。

**项目与配置**

| 工具 | 用途 | 参数 |
|---|---|---|
| `list_projects` | 列出工作区里的项目及其最佳版本和分数 | — |
| `create_project` | 用三项输入创建项目:需求文档、可选的初版 SKILL.md、可选的用例。`config` 深度合并到默认配置 | **name**, requirement, skill_md, cases, config |
| `project_status` | 项目概览:输入、划分、最佳版本与分数、活跃任务、待评审/待审批、错题库计数、agent/LLM 健康状态 | — |
| `get_config` | 读取项目配置 | — |
| `update_config` | 把 overrides 深度合并进配置并写回 | **overrides** |
| `add_cases` | 向测试集追加用例 | **cases** |

**长任务**(立即返回 `{"job_id": ...}`)

| 工具 | 用途 | 参数 |
|---|---|---|
| `evolve` | 启动进化闭环 | max_iterations |
| `evaluate` | 评测一个版本(默认最佳)在某个 split 或指定用例上的表现 | version, split(`train` / `val` / `test`), case_ids |
| `build_devset` | 从需求文档生成测试集;`merge` 为 true 时追加进项目测试集 | merge |
| `job_status` | 任务的状态、进度和新事件 | **job_id**, after_seq |
| `list_jobs` | 最近的任务 | limit |
| `cancel_job` | 请求取消任务(在下一个安全点停下) | **job_id** |

**版本**

| 工具 | 用途 | 参数 |
|---|---|---|
| `list_versions` | 全部版本:父版本、状态、各 split 得分、变更摘要 | — |
| `get_skill` | 某版本的 SKILL.md 与支撑文件,默认最佳版本;同时返回谱系、得分、闸门决定 | version |
| `diff_versions` | 两个版本之间 Skill 的 unified diff | **base**, **cand** |
| `compare_versions` | 逐用例配对比较:分数差与置信区间、修复/回退用例、按标签分解 | **base**, **cand**, split(默认 `val`) |
| `import_skill` | 把手写或编辑过的 SKILL.md 文本登记为新版本(当前最佳的子版本) | **skill_md**, summary |
| `promote_version` | 手工提升为最佳;也用于批准 `pending_approval` 的候选 | **version**, reason |
| `export_skill` | 把最佳 Skill 包写到项目内的某个目录 | **dest** |

**证据与产物**

| 工具 | 用途 | 参数 |
|---|---|---|
| `list_evals` | 已保存的评测,新的在前 | — |
| `get_eval` | 一次评测的逐用例结果 | **eval_id**, include_trajectories, only_failures |
| `get_errorbook` | 错题库条目与根因统计 | status(`open` / `fixed` / `regressed` / `wontfix`), limit(默认 30) |
| `list_reviews` | 等待(或已有)人工结论的结果 | status(`pending` / `done`,默认 `pending`) |
| `submit_review` | 记录一条人工结论,覆盖自动判分 | **eval_id**, **case_id**, **passed**, trial, score, comment, reviewer |
| `get_metrics` | 指标摘要:最佳版本、各 split 得分、逐轮趋势、谱系、错题库统计、用量合计 | — |
| `get_history` | 每轮迭代记录:父版本、候选、闸门结果 | — |
| `write_outputs` | 重新生成输出包并返回各文件路径 | — |
| `list_artifacts` | 产物索引 | kind, version |
| `read_artifact` | 按相对路径读项目内的文本文件(限制在项目目录内) | **path**, max_chars(默认 40000) |

版本参数处处可以写 `best` 或 `latest`。

### 异步任务模式

`evolve` / `evaluate` / `build_devset` 不会阻塞:

```
evolve {max_iterations: 5}
  → {"job_id": "job_ab12…"}

job_status {job_id, after_seq: 0}
  → {"status": "running", "progress": {"stage": "evaluate:val", "current": 3, "total": 8, "percent": 37.5, …},
     "events": [{"seq": 2, "level": "info", "type": "log", "message": "dataset: 24 cases → …"}, …],
     "last_seq": 41, "error": "", "result": null}

job_status {job_id, after_seq: 41}        ← 把上次的 last_seq 传回来,只拿新事件
  → {"status": "succeeded", "result": {"best_version": "v004", "stop_reason": "…", …}, …}
```

- `status` 取值:`queued` `running` `cancelling` `succeeded` `failed` `cancelled` `interrupted`;后四个是终态。
- `events` 里不含进度事件(进度看 `progress` 字段),每次最多返回最近 60 条;`audit` 事件带 `data`。
- `result` 在成功后给出:`evolve` 是进化结果(最佳版本、基线与最佳的各 split 得分、提升记录、停止原因、输出文件),`evaluate` 是评测摘要,`build_devset` 是规则数/用例数和报告。
- `cancel_job {job_id}` 请求取消;任务在下一个安全点停下,正在运行的 agent 子进程会被立即终止。
- 同一项目同一时间只能有一个进化任务,重复发起会得到工具错误。
- 任务运行在 MCP 服务进程内。客户端断开(服务退出)会取消未完成的任务;已完成的评测已落盘,重新 `evolve` 会接着跑。

### 错误如何返回

工具自身的失败——项目/版本/任务不存在、参数不合法、路径越界、配置校验失败——都以 **`isError: true` 的工具结果**返回,文本里是可读的原因,调用方模型可以据此自行修正:

```json
{"content": [{"type": "text", "text": "an evolve/bootstrap job is already active for this project; cancel it or wait"}], "isError": true}
```

参数不符合工具 schema(缺必填项、类型不对、枚举值不对)同样是 `isError` 结果。只有协议层的问题才是 JSON-RPC error:未知工具 `-32602`、未知方法 `-32601`、请求格式错误 `-32600`、JSON 解析失败 `-32700`。

成功结果以 JSON 文本放在 `content[0].text`;返回值是对象时同时放在 `structuredContent`。

### 协议版本

支持 `2025-06-18`、`2025-03-26`、`2024-11-05`。`initialize` 时客户端请求的版本若在其中则原样采用,否则回落到 `2025-06-18`。实现了 `initialize`、`ping`、`tools/list`、`tools/call`,以及返回空列表的 `resources/list`、`resources/templates/list`、`prompts/list`;声明的能力只有 `tools`。工具调用在工作线程上执行,慢工具不会阻塞 `ping`。

## 二、Mock MCP Profiles

### 用途

如果 Skill 教 agent 使用某些 MCP 工具(查订单、建工单、查知识库),评测就会遇到三个问题:真实系统有副作用、返回值会变导致结果不可比、离线环境连不上。Mock MCP Profile 用一份声明式配置描述「哪个工具、在什么参数下、返回什么」,评测时 skillloop 为被测 agent 启动一个按此应答的假 MCP 服务,并把每次调用记下来。

### Profile 结构

写在 `skillloop.toml` 的 `[mcp_profiles.<name>]` 下,用 `[agent] mcp_profile = "<name>"` 选中:

```toml
[agent]
adapter = "claude_code"
mcp_profile = "crm"              # 评测时挂载下面这个 profile

[[mcp_profiles.crm.tools]]
name = "lookup_order"
description = "按订单号查询订单状态"
input_schema = { type = "object", properties = { order_id = { type = "string" } }, required = ["order_id"] }
default = { status = "not_found", order_id = "{{order_id}}" }

[[mcp_profiles.crm.tools.responses]]
when = { order_id = "88231" }                      # 精确匹配
result = { status = "shipped", carrier = "SF" }

[[mcp_profiles.crm.tools.responses]]
when = { order_id = { regex = "^9" } }             # 正则
error = "order {{order_id}} is locked"

[[mcp_profiles.crm.tools]]
name = "create_ticket"
description = "创建二线工单"
default_error = "ticket backend unavailable"

[[mcp_profiles.crm.tools.responses]]
when = { note = { contains = "投诉" } }             # 子串
result = "T-1001 escalated: {{note}}"
delay_s = 0.1
```

每个工具的字段:

| 字段 | 说明 |
|---|---|
| `name` | 必填。1–128 个字符,字母、数字、`_` `-` `.`;同一 profile 内不能重复 |
| `description` | 给 agent 看的工具说明,默认 `Mock tool <name>` |
| `input_schema` | 参数的 JSON Schema(`type` 必须是 `object`),默认接受任意参数。调用时会按它校验必填项、类型、枚举 |
| `responses` | 应答规则列表,**按顺序匹配,第一条命中的生效** |
| `default` | 没有规则命中时的返回值 |
| `default_error` | 没有规则命中、也没有 `default` 时返回的错误 |

每条应答规则:

| 字段 | 说明 |
|---|---|
| `when` | 参数条件表;缺省或为空表示总是命中。表内所有条件都满足才算命中 |
| `result` | 返回值:字符串、表或列表。与 `error` 至少给一个 |
| `error` | 给了就返回 `isError` 结果,文本为该消息(用来模拟工具失败) |
| `delay_s` | 应答前等待的秒数,非负(用来模拟慢工具) |

`when` 里每个参数条件的三种写法:

| 写法 | 含义 |
|---|---|
| `arg = "值"` | 相等。字符串与数字按文本比较,所以 `"42"` 能匹配 `42`;布尔值、表、列表要求完全相等 |
| `arg = { regex = "模式" }` | 对参数的文本做 `re.search`(非字符串参数先转成 JSON 文本) |
| `arg = { contains = "子串" }` | 参数的文本包含该子串 |

调用里没有带这个参数时,该条件不成立。

**占位符**:`result`、`error`、`default`、`default_error` 里的 `{{参数名}}` 会替换成调用时的参数值(嵌套在表和列表里的字符串也会);参数不存在时原样保留。

**都没命中**:既无规则命中、又没有 `default` / `default_error` 时,返回一个 `isError` 结果,说明该 mock 没有为这组参数配置应答——评测里看到这条消息,通常意味着 profile 需要补一条规则。

profile 不合法(工具列表为空、名字重复、规则既无 `result` 也无 `error`、正则写错、`delay_s` 为负等)时,评测在启动阶段就报错并指出具体位置,不会带着坏配置跑。

### 如何挂到被测 agent 上

评测开始时,skillloop 校验 profile,把它写到 `.skillloop/mcp_profiles/<name>.json`,并生成一份标准的 `mcpServers` 配置随每个用例交给适配器(`AgentRequest.mcp_config`):

```json
{"mcpServers": {"crm": {
  "command": "/path/to/python",
  "args": ["-m", "skillloop.mcp.mock_server",
           "--profile", "<project>/.skillloop/mcp_profiles/crm.json",
           "--log", "<project>/.skillloop/mcp_logs/crm.jsonl",
           "--name", "crm"]}}}
```

各内置适配器的处理:

| 适配器 | 如何使用 `mcp_config` |
|---|---|
| `claude_code` | 写成文件并以 `--mcp-config` 传入;默认同时加 `--strict-mcp-config`,只加载这一份。工具名为 `mcp__<profile>__<tool>`,权限需要放行(`allowed_tools` 或 `permission_mode`) |
| `codex` | 转成 `-c mcp_servers.<name>.command=…` / `args` / `env` 覆盖项 |
| `command` | 写成文件,路径通过占位符 `{mcp_config_file}` 提供,由你的命令模板决定怎么传给 agent |
| `nanobot` | 基于 `command`:默认参数里没有用 `{mcp_config_file}`,需要在 `args` 里自行接上或在 nanobot 配置里声明 |
| `http` | 原样放进请求体的 `mcp_config` 字段,由你的服务决定是否启动 |
| `llm`、`callable`、`mock` | 不使用(`callable` 的函数可以自己读 `request.mcp_config`) |

自己写适配器时,读 `request.mcp_config` 即可;它为 `None` 表示没有配置 profile。

### 调用日志

mock 服务把每次工具调用追加到 `.skillloop/mcp_logs/<profile>.jsonl`,一行一条:

```json
{"ts": 1795517858.63, "tool": "lookup_order", "arguments": {"order_id": "88231"}, "matched_index": 0, "result": {"status": "shipped", "carrier": "SF"}}
{"ts": 1795517858.63, "tool": "lookup_order", "arguments": {"order_id": "901"}, "matched_index": 1, "error": "order 901 is locked"}
{"ts": 1795517858.63, "tool": "lookup_order", "arguments": {"order_id": "5"}, "matched_index": -1, "result": {"status": "not_found", "order_id": "5"}}
```

`matched_index` 是命中的规则下标;`-1` 表示用了 `default` / `default_error`,`-2` 表示什么都没命中。被 `input_schema` 校验拒绝的调用不会进入日志。

日志按 profile 累积,同一项目的所有用例、所有并发评测写同一个文件,不带用例 id——需要按用例区分时,按时间戳与评测记录对照,或把并发设为 1。读取与清空:

```python
from skillloop import Project
from skillloop.mcp.profiles import clear_call_log, list_profiles, read_call_log

project = Project.open("my-skill")
print(list_profiles(project))                 # [{name, tools, problems, active}]
for call in read_call_log(project, "crm", limit=50):
    print(call["tool"], call["arguments"], call.get("result", call.get("error")))
clear_call_log(project, "crm")
```

### 单独运行 mock 服务

mock 服务可以脱离 skillloop 使用,比如手工调试一个 agent,或在自己的测试里当假后端:

```bash
python -m skillloop.mcp.mock_server --profile profile.json [--log calls.jsonl] [--name crm]
```

`profile.json` 就是上面 `[mcp_profiles.<name>]` 那张表的 JSON 形式(`{"tools": [...]}`;`input_schema` 也接受写成 `inputSchema`)。文件读不了或 profile 不合法时,在 stderr 说明原因并以退出码 2 结束。

快速自测:

```console
$ printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"lookup_order","arguments":{"order_id":"88231"}}}' \
    | python -m skillloop.mcp.mock_server --profile profile.json
{"jsonrpc": "2.0", "id": 1, "result": {"content": [{"type": "text", "text": "{\"status\": \"shipped\", \"carrier\": \"SF\"}"}], "isError": false, "structuredContent": {"status": "shipped", "carrier": "SF"}}}
```

接到 Claude Code 里手工试:

```bash
claude mcp add crm -- python -m skillloop.mcp.mock_server --profile /abs/path/profile.json --log /abs/path/calls.jsonl --name crm
```
