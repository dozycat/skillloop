# Web 操作台

一个不需要构建、不依赖任何外部资源的本地控制台:标准库 HTTP 服务 + 单页前端(原生 JS)。它和 CLI、MCP 服务共用 `SkillLoop` 门面,能做的事完全一样,可以离线使用。

## 启动

```bash
skillloop web                                  # 当前目录
skillloop web -w ./workspace --open            # 指定工作区并打开浏览器
skillloop web -w ./my-skill --port 9000        # 换端口
```

| 选项 | 默认 | 说明 |
|---|---|---|
| `-w, --workspace DIR` | `.` | 一个项目,或一个装着多个项目的目录 |
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8765` | 监听端口 |
| `--token` | 空 | 要求所有 `/api` 请求携带此令牌;监听非回环地址时必填 |
| `--open` | 关 | 启动后打开浏览器 |

启动后在 stderr 打印访问地址(设置了令牌时地址里带 `?token=…`)。Ctrl-C 停止服务,同时取消正在运行的任务并关闭项目。

**工作区的两种形态**

- 目录本身含 `skillloop.toml`:**单项目**模式,只暴露这一个项目,不能在控制台里新建项目。
- 否则:**多项目**模式,每个含 `skillloop.toml` 的子目录是一个项目,可以在控制台里新建。

项目在 URL 和 API 里用**目录名**寻址(字母、数字、`.` `_` `-`,最长 64 个字符);配置里的 `project.name` 只是显示名。

任务运行在控制台进程内:关掉控制台,未完成的任务会在下次打开项目时被标记为 `interrupted`,重新发起即可续跑(已完成的评测会复用)。同一项目同一时间只允许一个 `evolve` / `bootstrap` 任务。

## 页面

左侧导航对应以下页面,地址是 `#/p/<项目>/<页面>`。

| 页面 | 能做什么 |
|---|---|
| **项目**(首页) | 项目列表:最佳版本与分数、用例数、版本数。多项目模式下可新建项目:名称、需求文档、可选的初版 SKILL.md、可选的用例(粘贴 JSONL) |
| **概览** | 当前状态(最佳版本、val 分数、版本数、未解决/已修复错题)、分数趋势图(每轮的最佳 val 分数与父版本 train 分数)、待评审/待审批提示;输入与配置一览,含 agent 和 LLM 的健康状态。四个操作:**开始进化**(可填最大轮数)、**运行评测**(选版本和 split)、**生成开发集**(可勾选合并进测试集)、**生成输出** |
| **任务** | 任务历史。任务详情:分阶段进度条、实时结构化日志(可关闭「跟随最新」)、取消按钮、结束后的结果 |
| **版本** | 谱系表(父版本、来源、状态、轮次、各 split 得分、变更摘要);版本比较(基线、候选、split);导入手工编辑的 SKILL.md。版本详情:信息、闸门各项检查与理由、SKILL.md 全文、与任意版本的着色 diff、生成该版本的补丁 JSON,以及 提升 / 拒绝 / 批准 按钮 |
| **评测** | 评测列表。评测详情:汇总、按标签分解、逐用例结果(通过与否、分数、判分理由),可展开查看 agent 轨迹 |
| **错题库** | 状态计数、未解决条目的根因分类;条目可按状态筛选,展开看输入 / 期望 / 实际输出 / 根因 / 经验 / 复现命令,并就地标注(类别、根因、经验、状态) |
| **Review** | 评审队列:输入、期望、输出、自动判分结论;逐条提交 通过/不通过 和备注;导出 CSV 标注表 |
| **产物** | 产物索引(按类型分组)与文件查看器;内嵌的 HTML 进化报告;版本库的本地 Git 历史;重新生成输出 |
| **配置** | 被测 Agent(适配器与 options JSON)、框架 LLM、评测、进化与闸门各项的结构化编辑;需求文档编辑器 |

所有来自 API 的字符串都按文本渲染,不会当作 HTML 执行——Skill 文本、agent 输出和用例输入里出现 HTML 标签是安全的。

## 安全模型

这个控制台能在本机启动 agent,所以默认保守。以下是实际实现的检查,按请求处理顺序:

1. **回环绑定**。默认监听 `127.0.0.1`。`--host` 不是 `127.0.0.1` / `localhost` / `::1` 且没有 `--token` 时,**拒绝启动**并说明原因。
2. **Host 校验**(防 DNS 重绑定)。对所有请求(含静态文件)检查 `Host` 头的主机名,只接受 `127.0.0.1`、`localhost`、`::1` 以及绑定的地址,其余返回 403。绑定通配地址(`0.0.0.0` / `::`)时不做此项检查,此时令牌是唯一防线。
3. **跨源拒绝**。对所有 `/api` 请求:带 `Origin` 头且其主机与 `Host` 不一致(或为 `null`)时返回 403;`Sec-Fetch-Site: cross-site` 同样返回 403。
4. **令牌**。设置了 `--token` 时,每个 `/api` 请求都必须携带:`Authorization: Bearer <token>`,或查询参数 `?token=<token>`(供 `EventSource` 这类不能设请求头的场景使用)。常数时间比较,不符返回 401。静态文件不需要令牌。
5. **请求体**。上限 5 MB(超出返回 413);有请求体时 `Content-Type` 必须是 `application/json`(否则 415),且必须是 JSON 对象。
6. **路径隔离**。读文件接口只能读项目目录内的文件,解析符号链接后校验,越界返回 403。静态文件只从包内的 static 目录按文件名提供。
7. **响应头**。所有响应带 `X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`、`Cache-Control: no-store`;页面带限制到同源的 CSP;内嵌的 HTML 报告以 `Content-Security-Policy: sandbox` 提供(不执行脚本)。

500 错误只向客户端返回固定文案,堆栈写到服务端 stderr。

没有做的事:没有用户体系(只有一个共享令牌),没有 TLS。需要远程访问时,请放在带认证的反向代理或 SSH 隧道后面,不要直接暴露到公网。

## REST API

所有接口在 `/api` 下,请求和响应都是 JSON。项目级接口的前缀是 `/api/projects/<name>`,下表中省略。

### 工作区

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/api/meta` | `{job_kinds, single, token_required}` |
| GET | `/api/projects` | 项目列表 `{projects, single}` |
| POST | `/api/projects` | 新建项目。body:`name`(必填)、`requirement`、`skill_md`、`cases`(列表)、`config`(覆盖默认配置)。返回 201 |

### 项目、输入与配置

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/` 或 `/info` | 项目概览 |
| GET | `/health` | agent / LLM 是否可用、配置问题 |
| GET | `/config` | 当前配置 |
| PATCH | `/config` | 深度合并 body 到配置并写回 `skillloop.toml`。`agent.options`、`llm.options`、`llm.roles`、`eval.judge_options`、`mcp_profiles` 出现时整体替换 |
| GET / PUT | `/requirement` | 读 / 写需求文档,body 为 `{"text": "..."}` |
| GET | `/cases?split=` | 用例列表(带所属 split) |
| POST | `/cases` | 追加用例,body 为 `{"cases": [{...}]}` |

### 任务

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/jobs?limit=` | 任务列表 |
| POST | `/jobs` | 发起任务,返回 201 `{job_id, job}`。body:`{"kind": ..., "params": {...}}` |
| GET | `/jobs/<id>` | 任务记录:`status`、`progress`、`result`、`error` |
| POST | `/jobs/<id>/cancel` | 请求取消 |
| GET | `/jobs/<id>/events?after=<seq>` | 事件流(SSE) |
| GET | `/jobs/<id>/poll?after=<seq>&limit=` | 事件轮询 |

`kind` 与可用参数:

| kind | params |
|---|---|
| `evolve` | `max_iterations`(0–1000) |
| `evaluate` | `version`、`split`(`train` / `val` / `test`)、`case_ids`(列表) |
| `devset` | `merge`(布尔) |
| `bootstrap` | — |
| `outputs` | — |

### 版本

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/versions` | `{versions, best}` |
| GET | `/versions/<v>` | manifest + `skill_md` + `files` + `lineage`。`<v>` 可为 `best` / `latest` |
| POST | `/versions/import` | 把 `skill_md` 文本登记为新版本(可选 `summary`、`parent`)。返回 201 |
| POST | `/versions/<v>/promote` | 提升为最佳(可选 `reason`、`by`) |
| POST | `/versions/<v>/reject` | 拒绝(可选 `reason`) |
| GET | `/diff?a=&b=` | unified diff |
| POST | `/compare` | 配对比较,body:`base`、`cand`、`split`(默认 `val`)。用 POST 是因为缺结果时会触发评测 |

### 评测、错题库、评审

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/evals` | 评测摘要列表,新的在前 |
| GET | `/evals/<id>?trajectories=0` | 评测详情;`trajectories=0` 时不带完整轨迹 |
| GET | `/errors?status=` | 错题库条目 |
| GET | `/errors/stats` | 错题库统计 |
| POST | `/errors/<case_id>/annotate` | 标注:`category`、`root_cause`、`lesson`、`suggested_fix`、`status`(`open` / `fixed` / `regressed` / `wontfix`) |
| GET | `/reviews?status=&eval_id=&detail=0` | 评审队列;默认附带输入、期望、输出 |
| POST | `/reviews` | 提交人工结论:`eval_id`、`case_id`、`passed`(布尔,必填)、`trial`、`score`(0–1)、`comment`、`reviewer` |
| GET | `/review-sheet?eval_id=` | 下载 CSV 标注表(默认最近一次评测) |

### 产物与报告

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/history` | 每轮迭代记录 |
| GET | `/metrics` | 指标摘要 |
| GET | `/artifacts?kind=&version=` | 产物索引 |
| GET | `/file?path=` | 读项目内的文本文件(相对路径);Skill 目录返回其 SKILL.md |
| GET | `/report` | `outputs/report.html`(未生成时 404) |
| GET | `/gitlog?limit=` | 版本库的 Git 历史 |
| POST | `/outputs` | 同步生成输出包,返回各文件路径 |

### 错误格式

```json
{"error": {"type": "not_found", "message": "unknown project 'ghost'"}}
```

| 状态码 | type | 触发 |
|---|---|---|
| 400 | `bad_request` | 参数或 JSON 不合法、配置校验失败 |
| 401 | `unauthorized` | 令牌缺失或错误 |
| 403 | `forbidden` | Host / Origin 校验失败、路径越界 |
| 404 | `not_found` | 项目、版本、评测、任务、文件、路由不存在 |
| 409 | `conflict` | 状态冲突,如该项目已有进化任务在运行 |
| 413 | `too_large` | 请求体超过 5 MB |
| 415 | `unsupported_media_type` | 请求体不是 `application/json` |
| 500 | `internal` | 其他错误(详情只在服务端日志) |

## 任务事件流

每个任务事件有递增的 `seq`,形如:

```json
{"seq": 7, "job_id": "job_…", "ts": 1795517839.26, "level": "info", "type": "log",
 "message": "baseline v001: val mean_score=0.000 (pass rate 0.0%)", "data": {}}
```

`type` 取值:`job`(started / succeeded / failed / cancelled / cancel requested)、`log`、`progress`(`data` 含 `stage`、`current`、`total`、`percent`)、`audit`(改变状态的操作,`data` 为操作详情)。

**SSE**:`GET /jobs/<id>/events?after=<seq>`。每个事件以 `id: <seq>`、`event: <type>`、`data: <JSON>` 发送;任务结束且事件发完后,发一个 `event: end`(`data` 为最终任务记录)并关闭连接。断线后带上 `after=<最后收到的 seq>` 重连即可续传,也认标准的 `Last-Event-ID` 请求头,所以浏览器的 `EventSource` 会自动续传。空闲时每 10 秒发一行 `: ping` 注释保活。

**轮询**:`GET /jobs/<id>/poll?after=<seq>&limit=500`,返回 `{job, events, last, final}`。把 `last` 作为下次的 `after`,直到 `final` 为 true。

## 用 curl 走一遍

下面的输出来自一个真实运行的服务(长字段有截断)。

```console
$ skillloop web -w ./ws --port 8793 &
skillloop console: http://127.0.0.1:8793/  (workspace: /tmp/ws)
$ B=http://127.0.0.1:8793/api

$ cat > body.json <<'EOF'
{"name": "tickets",
 "requirement": "# 工单处理\n- 先查询订单再回答。",
 "skill_md": "---\nname: tickets\ndescription: 处理售后工单时使用。\n---\n\n# 工单处理\n\n## Rules\n\n- 礼貌、简洁地回答用户。\n",
 "cases": [
  {"id": "c1", "input": "订单到哪了", "expected": "A1", "meta": {"requires": ["lookup"], "hints": {"lookup": "先查询订单再回答。"}}},
  {"id": "c2", "input": "帮我查下物流", "expected": "A2", "meta": {"requires": ["lookup"], "hints": {"lookup": "先查询订单再回答。"}}},
  {"id": "c3", "input": "包裹丢了", "expected": "A3", "meta": {"requires": ["lookup"], "hints": {"lookup": "先查询订单再回答。"}}},
  {"id": "c4", "input": "什么时候发货", "expected": "A4", "meta": {"requires": ["lookup"], "hints": {"lookup": "先查询订单再回答。"}}}
 ]}
EOF
$ curl -s -X POST $B/projects -H 'Content-Type: application/json' -d @body.json
{"name": "tickets", "root": "/tmp/ws/tickets", "has_requirement": true, "has_seed_skill": true, "n_cases": 4, "n_versions": 0, ...}

$ curl -s -X POST $B/projects/tickets/jobs -H 'Content-Type: application/json' \
       -d '{"kind":"evolve","params":{"max_iterations":3}}'
{"job_id": "job_59959e0e6e5d", "job": {"id": "job_59959e0e6e5d", "kind": "evolve", "status": "running", ...}}

$ curl -sN "$B/projects/tickets/jobs/job_59959e0e6e5d/events?after=0"
retry: 2000

id: 1
event: job
data: {"seq": 1, "job_id": "job_59959e0e6e5d", ..., "type": "job", "message": "started", "data": {"kind": "evolve", "params": {"max_iterations": 3}}}

id: 2
event: log
data: {"seq": 2, ..., "type": "log", "message": "dataset: 4 cases → train 2 / val 1 / test 1", "data": {}}

id: 3
event: audit
data: {"seq": 3, ..., "type": "audit", "message": "version.create", "data": {"version": "v001", "origin": "seed"}}
...
event: end
data: {"id": "job_59959e0e6e5d", "kind": "evolve", "status": "succeeded", ...}

$ curl -s $B/projects/tickets/versions | jq -c '[.best, (.versions[] | [.id, .status, .scores.val.mean_score])]'
["v002",["v001","accepted",0],["v002","best",1]]

$ curl -s -X POST $B/projects/tickets/jobs -H 'Content-Type: application/json' -d '{"kind":"nope"}'
{"error": {"type": "bad_request", "message": "unknown job kind 'nope'; expected one of evolve, evaluate, devset, bootstrap, outputs"}}

$ curl -s "$B/projects/tickets/file?path=../../etc/passwd"
{"error": {"type": "forbidden", "message": "path '../../etc/passwd' escapes sandbox '/tmp/ws/tickets'"}}

$ curl -s -H 'Host: evil.example' $B/projects
{"error": {"type": "forbidden", "message": "Host header not allowed"}}
```

这个例子里的用例带 `meta.requires` / `meta.hints`,是给默认的模拟 agent 用的(见 [architecture.md](architecture.md) 的「模拟世界」);接真实 agent 时不需要这两个字段。

带令牌时:

```console
$ skillloop web -w ./ws --host 0.0.0.0 --port 8794
error: refusing to listen on non-loopback address '0.0.0.0' without a token: this console can run agents on this machine. Pass token=... (CLI: --token) or bind 127.0.0.1.

$ skillloop web -w ./ws --port 8795 --token s3cret &
$ curl -s http://127.0.0.1:8795/api/projects
{"error": {"type": "unauthorized", "message": "missing or invalid token"}}
$ curl -s -H 'Authorization: Bearer s3cret' http://127.0.0.1:8795/api/meta
{"job_kinds": ["evolve", "evaluate", "devset", "bootstrap", "outputs"], "single": false, "token_required": true}
```
