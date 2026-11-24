# Agent 适配器与 LLM 提供方

skillloop 通过 **适配器(AgentAdapter)** 接入被评测的 agent 系统,通过 **LLM 提供方(LLMClient)** 调用框架自身使用的模型(判分、错因分析、补丁合并、生成初版 Skill 等)。两者互相独立:被测 agent 可以是 Claude Code,而判分模型可以是任意 OpenAI 兼容端点。

- 被测 agent:`skillloop.toml` 的 `[agent]` 段,`adapter` 为注册名或 `"包.模块:类名"`,`[agent.options]` 原样传给适配器构造函数。
- 框架 LLM:`[llm]` 段,`provider` 同理;`[llm.roles.<role>]` 可为单个角色(如 `judge`)换模型。

适配器契约只有一条:`run(AgentRequest) -> Trajectory`,在 `request.workdir` 内用 `request.skill` 执行一个 case。所有内置适配器都线程安全、不抛异常(失败体现在 `Trajectory.status` = `error | timeout | cancelled`)、支持超时与取消(子进程按进程组杀掉),原始输出最多保留约 200KB。

## 内置适配器一览

| adapter | 接入对象 | Skill 投递方式 | 轨迹来源 |
|---|---|---|---|
| `mock` | 离线模拟 agent(演示/测试) | 读取 skill 文本 | 模拟 |
| `claude_code` | Claude Code 无头模式 `claude -p` | 原生:`<workdir>/.claude/skills/<name>/` | `stream-json` 事件 |
| `codex` | Codex CLI `codex exec` | `AGENTS.md` + `.codex/skills/<name>/` | `--json` JSONL 事件 |
| `nanobot` | HKUDS nanobot | `<workspace>/skills/<name>/SKILL.md` | stdout |
| `command` | **任意命令行 agent**(模板) | inline / dir / file 三选一 | text / json / last_line |
| `http` | 你自己的 agent HTTP 服务 | 请求体里带 skill | 响应 JSON |
| `llm` | 直接调模型(skill 作 system prompt) | system prompt | 单轮回复 |
| `callable` | 进程内 Python 函数 | 函数自行处理 | 函数返回值 |

所有适配器都能使用 `[agent] task_file`(固定任务说明)和 `[agent] data_dirs`(只读参考目录):前者由评测器拼进交给适配器的输入,后者由评测器链接进用例工作目录,适配器不需要做任何事。agent 自带访问控制时还需要放行真实路径——`claude_code` 已通过 `--add-dir` 处理;其余适配器未对「工作目录内的符号链接指向目录外」做过实测,如果你的 agent 拒绝读取,把真实路径(`request.data_dirs`)通过它自己的参数放行。

### claude_code

```toml
[agent]
adapter = "claude_code"

[agent.options]
binary = "claude"                # 直接调用二进制,不经过 shell alias
model = "sonnet"                 # 可选:别名或完整模型 id
permission_mode = "acceptEdits"  # acceptEdits | auto | bypassPermissions | manual | dontAsk | plan
allowed_tools = ["Read", "Write", "Bash(python *)"]   # 可选,对应 --allowed-tools
# disallowed_tools = [...]  tools = ["Read","Bash"]  effort = "low"  max_budget_usd = 0.5
# max_turns = 20            append_system_prompt = "..."  setting_sources = "project"
strict_mcp_config = true         # 默认只加载 skillloop 传入的 MCP(--strict-mcp-config)
extra_args = []                  # 追加任意 CLI 参数
```

实际命令:`claude -p --output-format stream-json --verbose --no-session-persistence --permission-mode <mode> ...`,任务从 stdin 传入。`-p` 模式下需要人工确认的操作会被自动拒绝,所以默认 `acceptEdits`(只自动放行工作目录内的文件编辑);适配器**不会**默认加 `--dangerously-skip-permissions`,需要更宽权限时显式设 `permission_mode = "bypassPermissions"` 或用 `allowed_tools` 精确放行。轨迹包含思考、工具调用/结果、最终回答,`usage` 含 token 数、`cost_usd`、`turns`。配置了 `[agent] mcp_profile` 时,Mock MCP 通过 `--mcp-config` 传入。

注意:默认会加载用户级设置(用户自己的全局 skills/hooks 也会生效)。想要更干净的环境可设 `setting_sources = "project"`。

### codex

```toml
[agent]
adapter = "codex"

[agent.options]
model = "gpt-5-codex"            # 可选
sandbox = "workspace-write"      # read-only | workspace-write | danger-full-access
ephemeral = true                 # 不落盘会话
[agent.options.config]           # 透传为 -c key=value
model_reasoning_effort = "low"
```

实际命令:`codex exec --json --skip-git-repo-check --color never -C <workdir> -o <last_message> -s <sandbox> ... -`。Skill 渲染进 `AGENTS.md`(Codex 自动读取),完整目录另存 `.codex/skills/<name>/` 供引用支持文件。MCP 配置转换为 `-c mcp_servers.<name>.command=... / args / env`。

### nanobot

```toml
[agent]
adapter = "nanobot"

[agent.options]
binary = "nanobot"
args = ["agent", "--workspace", "{workdir}", "--no-markdown", "-m", "{prompt}"]   # 二进制之后的完整参数
skills_dir = "skills"            # 相对 workspace
config_path = ""                 # 非空时追加 --config <path>(config_flag 可改名)
```

每个 case 使用独立 workspace(即 case 的 workdir),不污染 `~/.nanobot/workspace`。不同版本 nanobot 的参数名可能不同,所以 `binary / args / skills_dir / config_path / config_flag / extra_args` 全部可覆盖;`healthcheck` 会在找不到二进制时明确提示。

默认参数已对照 nanobot v0.4.2 的 `nanobot agent --help` 核对(`--workspace` `--no-markdown` `-m` `--config` 均存在);尚未用配置好模型的 nanobot 做过真实运行,首次接入请先 `skillloop doctor` 再用 `skillloop eval --case <id>` 跑单个用例确认输出解析。

### command(通用模板,接入任何 CLI agent)

```toml
[agent]
adapter = "command"

[agent.options]
command = ["my-agent", "run", "--cwd", "{workdir}", "--prompt-file", "{prompt_file}"]
skill_mode = "dir"               # inline: 拼进 prompt | dir: 装到 skills_dir/<name>/ | file: 写成单个文件
skills_dir = ".agent/skills"     # skill_mode = "dir"
skill_file = "AGENTS.md"         # skill_mode = "file"
output = "json"                  # text: 全部 stdout | last_line: 最后一行 | json: 解析 JSON
answer_path = "result.answer"    # output = "json" 时答案的点路径(支持数组下标)
steps_path = "trace"             # 可选:步骤数组 [{kind,name,content}]
usage_path = "usage"             # 可选
answer_regex = "ANSWER:\\s*(.+)" # 可选:从答案里再抽取
# prompt_template = "{skill}\n\n任务: {input}"   env = { KEY = "v" }   ok_exit_codes = [0]
```

占位符:`{prompt}` `{prompt_file}` `{skill_dir}` `{skill_file}` `{skill_name}` `{workdir}` `{mcp_config_file}` `{case_id}`。命令里既没有 `{prompt}` 也没有 `{prompt_file}` 时,prompt 走 stdin。命令以 argv 列表执行,不经过 shell。agent 在 workdir 新建的文本文件会收集进 `Trajectory.artifacts`。

### http

```toml
[agent]
adapter = "http"

[agent.options]
url = "http://localhost:8000/run"
headers = { Authorization = "Bearer ${MY_AGENT_TOKEN}" }   # ${VAR} 从环境变量展开
output_path = "output"           # 响应里答案的点路径;steps_path / usage_path / artifacts_path / error_path 同理
health_url = "http://localhost:8000/health"
```

请求体:`{case_id, input, files, meta, tags, skill: {name, description, markdown, files, rendered}, timeout_s, mcp_config}`;响应:`{output, steps?, usage?, artifacts?, error?}`(也接受纯文本响应)。取消时适配器立即返回 `cancelled`,但无法中断服务端已在执行的请求。

### llm

```toml
[agent]
adapter = "llm"

[agent.options]
provider = "anthropic"           # 任意已注册的 LLM 提供方
model = "claude-opus-5-5"
# system_template = "...{skill}..."   max_tokens = 16000
[agent.options.llm_options]      # 透传给提供方构造函数
effort = "low"
```

### callable

```toml
[agent]
adapter = "callable"

[agent.options]
target = "my_pkg.my_agent:run_case"   # (AgentRequest) -> Trajectory | str | dict
```

SDK 用法:`FunctionAdapter(lambda req: my_agent(req.case.input, req.skill.render_for_prompt()))`。进程内函数无法被强杀,长任务请自行轮询 `request.cancel`。

## LLM 提供方(`[llm]`)

```toml
[llm]
provider = "anthropic"           # 官方 anthropic SDK:pip install 'skillloop[anthropic]'
model = "claude-opus-5-5"        # 默认值
[llm.options]
effort = "high"                  # 可选:low | medium | high | xhigh | max
```

凭据由 SDK 自行解析(`ANTHROPIC_API_KEY` / `ANTHROPIC_AUTH_TOKEN` / `ant auth login`),skillloop 不读取也不保存。请求使用流式 + 自适应思考;传入 JSON schema 时走结构化输出,被拒绝则退回普通提示;默认开启服务端拒答回退(`fallbacks = "default"`,设为 `""` 可关闭)。

```toml
[llm]
provider = "openai"              # 任意 OpenAI 兼容 /chat/completions 端点(标准库实现,无依赖)
model = "qwen-max"
[llm.options]
base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
api_key_env = "DASHSCOPE_API_KEY"
structured = "none"              # none | json_object | json_schema(端点支持时再开)
```

```toml
[llm]
provider = "cli"                 # 复用已登录的 CLI,无需 API key;默认 claude -p(禁用工具,纯补全)
model = "sonnet"
# [llm.options] command = ["llm", "-m", "{model}", "-s", "{system}"]   # 自定义:{prompt} {system} {model}
```

`provider = "mock"` 为离线确定性实现,配合 `adapter = "mock"` 可零成本跑通整个闭环。

## 写一个自己的适配器

不想打包时,把适配器写在 `skillloop.toml` 旁边的 `.py` 文件里,在 `[project] plugins = ["my_adapter"]` 里列出模块名即可按注册名使用;[examples/custom-adapter](../examples/custom-adapter) 是一个可直接运行的完整例子。

```python
# my_pkg/my_adapter.py
import time
from skillloop.adapters.base import AgentAdapter, AgentRequest
from skillloop.core.models import Step, Trajectory
from skillloop.core.registry import ADAPTERS


@ADAPTERS.register("my_agent")            # 注册名;也可不注册,直接在配置里写 "my_pkg.my_adapter:MyAdapter"
class MyAdapter(AgentAdapter):
    name = "my_agent"

    def __init__(self, endpoint: str = "", **options):   # [agent.options] 的键就是这里的参数
        super().__init__(**options)
        self.endpoint = endpoint

    def run(self, request: AgentRequest) -> Trajectory:
        t0 = time.time()
        try:
            # 1) 把 skill 交给 agent:原生目录 / 拼进 prompt,二选一
            self.install_skill(request.skill, request.workdir / "skills")
            # prompt = self.compose_prompt(request.skill, request.case)
            # 2) 执行,并尊重 request.timeout_s 与 request.cancel
            result = my_sdk.run(request.case.input, cwd=request.workdir,
                                timeout=request.timeout_s, should_stop=request.cancel.is_set)
            # 3) 返回轨迹:最终答案放 output,过程放 steps
            return Trajectory(
                output=result.answer,
                steps=[Step("tool_call", c.args, name=c.tool) for c in result.calls],
                usage={"input_tokens": result.tokens_in, "output_tokens": result.tokens_out},
                artifacts=self.collect_artifacts(request.workdir),
                duration_s=time.time() - t0,
            )
        except TimeoutError as e:           # 不要向外抛异常,用 status 表达失败
            return Trajectory(status="timeout", error=str(e))
        except Exception as e:
            return Trajectory(status="error", error=f"{type(e).__name__}: {e}")

    def healthcheck(self) -> tuple[bool, str]:
        return (True, "ok") if self.endpoint else (False, "endpoint 未配置")
```

```toml
[agent]
adapter = "my_pkg.my_adapter:MyAdapter"   # 或注册名 "my_agent"(需保证模块已被 import,或声明 entry point 组 skillloop.adapters)
[agent.options]
endpoint = "http://localhost:9000"
```

要点:`run` 会被多个线程并发调用(每个 case 一次),不要在实例上保存单次运行的状态;包装命令行 agent 时可直接继承 `CommandAdapter`,只重写 `build_command` / `parse`(`claude_code`、`codex`、`nanobot` 都是这么做的),子进程用 `skillloop.adapters._proc.run_process` 即可获得超时、取消和输出截断。
