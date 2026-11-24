# 文档

| 文档 | 内容 | 适合 |
|---|---|---|
| [architecture.md](architecture.md) | 分层、四个契约、数据模型、设计取舍、已知边界 | 想读代码或扩展框架 |
| [configuration.md](configuration.md) | `skillloop.toml` 全部配置项与默认值 | 配置一个项目 |
| [adapters.md](adapters.md) | 内置 Agent 适配器与 LLM 提供方、编写自己的适配器 | 接入一个 agent 系统 |
| [judges.md](judges.md) | 判分器、人工评审流程、校准 | 决定怎么判对错 |
| [devset.md](devset.md) | 从需求文档生成测试集的管道 | 还没有测试集 |
| [cli.md](cli.md) | 命令行参考 | 日常使用 |
| [web-console.md](web-console.md) | Web 操作台与 REST API | 用界面操作或写脚本对接 |
| [mcp.md](mcp.md) | MCP 服务与 Mock MCP Profiles | 让 agent 驱动闭环 / 评测带工具的 agent |

第一次使用请先看仓库根目录的 [README](../README.zh-CN.md)(快速开始)和 [examples/](../examples)。

## 术语

| 术语 | 含义 |
|---|---|
| Skill | 一个目录:`SKILL.md`(frontmatter + 正文)加可选的支撑文件。进化的对象 |
| 被测 agent | 使用 Skill 完成任务的系统,通过适配器接入。闭环中它是固定的,只有 Skill 在变 |
| 框架 LLM | skillloop 自己调用的模型:判分、错因分析、补丁合并、生成初版、生成测试集 |
| Case / 用例 | 一条测试:输入、期望答案或 rubric、标签 |
| train / val / test | 用例的三个分组:分析用 / 选择用 / 最终确认用 |
| Trajectory / 轨迹 | agent 跑一个用例的记录:最终输出、步骤、状态、用量 |
| 错题库 | 失败用例的持久记录:上下文、根因、经验、状态 |
| 补丁 Patch | 对 Skill 的一组声明式编辑 |
| 候选 | 一轮迭代中由补丁生成、待评测的 Skill 版本 |
| 闸门 Gate | 候选成为最佳版本前必须通过的一组显式检查 |
| 最佳版本 | 当前被提升的版本;`outputs/best/` 与 `skillloop show` 给出的就是它 |
