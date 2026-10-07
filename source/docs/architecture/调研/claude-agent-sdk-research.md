# Claude Agent SDK 调研与 OpenAI Agents SDK 对比

## 1. 文档信息

- 项目名称：ai-prd
- 文档名称：Claude Agent SDK 调研与 OpenAI Agents SDK 对比
- 文档版本：V1.0
- 文档日期：2026-05-07
- 文档状态：草案

---

## 2. 背景

`source/docs/architecture/openai-agents-sdk-research.md` 已经记录了 OpenAI Agents SDK 升级版的事实与待验证问题。Anthropic 同样提供 Claude Agent SDK（前身为 Claude Code SDK，已正式更名）。本调研用于：

1. 把 Claude Agent SDK 的能力与边界以同样的字段口径写下来。
2. 与 OpenAI Agents SDK 做一次横向对比，给出 ai-prd 选型时的客观参考。

只记录已经核实的事实；尚未验证的能力点单独列出待 PoC，不在结论中作为既定能力陈述。

---

## 3. 信息来源

| 来源 | 性质 | URL |
|---|---|---|
| Claude Agent SDK Overview | 官方文档 | https://code.claude.com/docs/en/agent-sdk/overview |
| Claude Agent SDK Python README | 官方仓库 | https://github.com/anthropics/claude-agent-sdk-python |
| Python SDK transport 源码 | 官方源码 | https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/transport/subprocess_cli.py |
| Claude Agent SDK TypeScript（仓库） | 官方仓库 | https://github.com/anthropics/claude-agent-sdk-typescript |
| Managed Agents 比较表 | 官方文档 | https://code.claude.com/docs/en/agent-sdk/overview（Compare 章节） |
| 项目文档：OpenAI Agents SDK 升级版调研 | 内部 | `source/docs/architecture/openai-agents-sdk-research.md` |
| 项目文档：Claude Code 与 Codex 沙箱对比 | 内部 | `source/docs/architecture/agent-sandboxing-comparison.md` |

---

## 4. Claude Agent SDK 关键事实

### 4.1 整体定位

官方原文："The Agent SDK gives you the same tools, agent loop, and context management that power Claude Code, programmable in Python and TypeScript."

从**能力抽象**角度，Claude Agent SDK 与 Claude Code CLI 共享同一套工具集、agent loop 与上下文管理。Python 与 TypeScript 双版本均已发布。

从**实现拓扑**角度，SDK 是 `claude` 原生二进制的官方 stream-json 协议客户端，详见 §4.11.2。

注意：旧名 "Claude Code SDK" 已重命名为 "Claude Agent SDK"，存在迁移指南。

### 4.2 核心 API

Python：

- `query(prompt, options)`：异步迭代器，返回消息流。
- `ClaudeAgentOptions`：配置（`allowed_tools`、`permission_mode`、`hooks`、`agents`、`mcp_servers`、`cwd`、`resume`、`setting_sources`、`cli_path` 等）。
- `ClaudeSDKClient`：双向交互式会话客户端。
- `@tool` 装饰器 + `create_sdk_mcp_server()`：以 in-process MCP server 形式注入自定义工具。
- 消息类型：`AssistantMessage`、`UserMessage`、`SystemMessage`、`ResultMessage`；内容块：`TextBlock`、`ToolUseBlock`、`ToolResultBlock`。
- `HookMatcher`：声明 hook 触发条件。

TypeScript 版本提供等价 API（`query`、`HookCallback` 等）。

### 4.3 内置工具集

Overview 文档显式列举：

| Tool | 作用 |
|---|---|
| Read / Write / Edit | 文件读写与精确编辑 |
| Bash | 终端命令、脚本、git |
| Monitor | 监听后台脚本输出，每行作为事件 |
| Glob | 按模式查找文件 |
| Grep | 内容正则搜索 |
| WebSearch | 网络搜索 |
| WebFetch | 抓取网页内容 |
| AskUserQuestion | 向用户提多选式澄清问题 |

工具执行由 SDK 自带，无需开发者实现 tool loop。

### 4.4 文件系统配置（与 Claude Code 共享）

SDK 默认会从工作目录的 `.claude/` 与 `~/.claude/` 加载以下资产，可通过 `setting_sources` / `settingSources` 限定：

| 类型 | 位置 |
|---|---|
| Skills | `.claude/skills/*/SKILL.md` |
| Slash commands | `.claude/commands/*.md` |
| Memory | `CLAUDE.md` 或 `.claude/CLAUDE.md` |
| Plugins | 通过 `plugins` 选项以编程方式接入 |

也就是说 ai-prd 现有 `.claude/skills/` 等资产可以**原地被 Agent SDK 复用**，无需重组。

### 4.5 Sessions

Overview 文档原文："Maintain context across multiple exchanges. Claude remembers files read, analysis done, and conversation history. Resume sessions later, or fork them to explore different approaches."

接入方式：

- 首次 query 时通过 `SystemMessage(subtype="init")` 拿到 `session_id`。
- 后续 query 通过 `options.resume=session_id` 恢复上下文。
- Session state 存放方式：本地 JSONL（来源：Managed Agents 对比表）。

### 4.6 Subagents

通过 `agents` 选项声明子 agent（`AgentDefinition` 含 `description`、`prompt`、`tools`），主 agent 通过 `Agent` 工具委派子任务。子 agent 上下文中的消息携带 `parent_tool_use_id`，便于追溯。

### 4.7 Hooks

支持的 hook 点：`PreToolUse`、`PostToolUse`、`Stop`、`SessionStart`、`SessionEnd`、`UserPromptSubmit` 等。回调可校验、记录、阻止或转换 agent 行为。

### 4.8 MCP

`mcp_servers` 配置支持外部 stdio MCP server；同时支持 in-process SDK MCP server（用 `@tool` + `create_sdk_mcp_server()` 实现）。

### 4.9 Permissions

通过 `allowed_tools`（白名单）、`permission_mode`（如 `acceptEdits`）控制工具权限；交互式审批配合 `AskUserQuestion`。

### 4.10 模型与认证

- 默认走 Anthropic API：`ANTHROPIC_API_KEY`。
- 通过环境变量切换到第三方提供商：
  - `CLAUDE_CODE_USE_BEDROCK=1`：AWS Bedrock。
  - `CLAUDE_CODE_USE_VERTEX=1`：Google Vertex AI。
  - `CLAUDE_CODE_USE_FOUNDRY=1`：Azure AI Foundry。
- 支持的模型仍为 Claude 系列（Bedrock/Vertex/Foundry 上的 Claude 模型）。文档未提供接入非 Claude 模型的官方路径。
- Opus 4.7（`claude-opus-4-7`）需要 Agent SDK ≥ v0.2.111。

### 4.11 与 Claude Code 的关系

#### 4.11.1 能力抽象层

SDK 与 CLI 是**同一能力的两种界面**（来源：Overview "Same capabilities, different interface"）。能力清单（Agent Loop、Built-in Tools、Skills、Memory、Sessions）在两种入口下完全一致。

#### 4.11.2 实现拓扑（已核实）

来源：`anthropics/claude-agent-sdk-python` 仓库
`src/claude_agent_sdk/_internal/transport/subprocess_cli.py`。

- 类名：`SubprocessCLITransport(Transport)`。
- 启动方式：`anyio.open_process(cmd, stdin=PIPE, stdout=PIPE, ...)`，`cmd` 形如 `claude --output-format stream-json --input-format stream-json --verbose`。
- 通信协议：双向 stdio **stream-json**（JSONL）。
- 错误类型：`CLINotFoundError` / `CLIConnectionError` / `ProcessError` / `CLIJSONDecodeError`。
- CLI 二进制查找顺序：`cli_path` → bundled CLI → PATH。
- TypeScript SDK 通过平台相关的 optional dependency 捆绑原生二进制（macOS/Linux/Windows 各自预编译版本），二进制本身不依赖 Node.js 运行时。

也就是说，官方 Agent SDK **不是把 CLI 改写为库**，而是**Claude Code 原生二进制的官方 stream-json 协议客户端**。每次 `query()` / `ClaudeSDKClient` 都会拉起一个 `claude` 子进程并保持 stdio 长连接，通过结构化 JSONL 通信（不是裸跑 `claude --print` 再 parse stdout）。

#### 4.11.2.1 native binary 与"Claude Code CLI"的关系（澄清）

容易产生歧义的一个点："Claude Code CLI" 是不是在 native binary 之外再包了一层 Node 程序？答案是**否**。

- npm 包 `@anthropic-ai/claude-code` 通过平台相关 optional dependency 安装预编译的可执行文件。安装后 PATH 上的 `claude` 命令就是这个二进制本身，无 Node 包装层。
- "Claude Code CLI" 这个产品名指代的就是**这个二进制 + 配套的 `.claude/` 文件约定**，不是单独的另一个程序。
- 同一个二进制根据启动参数走不同对外接口模式：
  - 无 flag / 交互启动 → TUI（终端 UI）。
  - `--output-format stream-json --input-format stream-json --verbose` → 协议模式（SDK 用）。
  - 协议模式下 TUI 完全不渲染，输出 100% 是 JSONL。
- SDK 内捆绑的 "bundled CLI binary" 与独立安装的 `claude` 是**同一个二进制**，只是分发位置和版本绑定方式不同。`ClaudeAgentOptions(cli_path=...)` 用于把默认捆绑版替换为独立安装版。

这意味着 ai-prd 后端集成时，无论选 SDK 还是直接 spawn `claude`，启动的都是同一个核心进程；选 SDK 的价值在于免去自己实现 stream-json 协议解析、进程生命周期管理、错误分类等管道代码。

#### 4.11.3 进程边界与 sandbox 继承

- `agent-sandboxing-comparison.md` 中描述的 Claude Code OS 级 sandbox（macOS Seatbelt、Linux bubblewrap）作为 `claude` 二进制的内置行为，自动适用于 SDK 启动的子进程。SDK 自身没有独立的 sandbox 抽象层。
- 重要：**宿主进程（如 FastAPI）与 `claude` 子进程是两个进程**。Claude Code 的 sandbox 约束作用在 `claude` 子进程及其再 fork 出来的 sandboxed Bash 上，不约束宿主。这意味着 ai-prd 后端在使用 SDK 时，权限边界至少要分成"宿主权限"与"`claude` 子进程权限"两层。

#### 4.11.4 In-process 扩展点

虽然主 agent loop 在子进程，但 SDK 提供两种"宿主进程内"的扩展：

- `@tool` + `create_sdk_mcp_server()`：自定义工具以 in-process MCP server 形式运行在调用方 Python/TS 进程中，不再额外起子进程。
- `hooks`：`PreToolUse` / `PostToolUse` 等回调函数运行在宿主进程，由 SDK 与子进程之间的协议触发。

这两类扩展点不改变"主 agent loop 在子进程"的事实，但为审计、权限检查、自定义工具执行提供了在宿主侧落地的接口。

### 4.12 与 Managed Agents 的边界

官方对比表给出三类边界差异：

|  | Agent SDK | Managed Agents |
|---|---|---|
| 运行在哪 | 你自己的进程/基础设施 | Anthropic 托管基础设施 |
| 接口 | Python / TypeScript 库 | REST API |
| 工作对象 | 你机器上的真实文件系统与服务 | 每会话一个托管 sandbox |
| Session state | 本机 JSONL | Anthropic 托管事件日志 |
| 自定义工具 | in-process Python/TS 函数 | Claude 触发，你的服务执行并回写 |

Managed Agents 是另一条路径，本调研不展开。

### 4.13 商业条款

- 受 Anthropic Commercial Terms of Service 约束。
- 未经审批，不允许第三方提供 claude.ai 登录或限额，必须使用文档中的 API key 认证方式。
- 品牌使用："Claude Agent" / "Powered by Claude" 允许；"Claude Code" / "Claude Code Agent" 不允许第三方使用。

---

## 5. 与 OpenAI Agents SDK 的横向对比

字段口径与 `openai-agents-sdk-research.md` 一致。

### 5.1 总览

| 维度 | Claude Agent SDK | OpenAI Agents SDK |
|---|---|---|
| 设计基线 | 把 Claude Code 库化 | 重新设计的"模型原生运行框架" + Sandbox Agents |
| 语言 | Python、TypeScript 同步发布 | Python 已发布；TypeScript 仍在路线图 |
| 模型绑定 | 仅 Claude 系列（Anthropic / Bedrock / Vertex / Foundry） | 通过 `ModelProvider` + LiteLLM/Any-LLM 接入第三方（含 Claude）；第三方为 beta 集成 |
| 默认运行位置 | 调用方进程（库形态） | 调用方进程（库形态） |
| Session 持久化 | 本地 JSONL，`options.resume=session_id` 恢复 | SDK 提供 SandboxSession / SessionState / Snapshot 抽象 |

### 5.2 工具与执行

| 维度 | Claude Agent SDK | OpenAI Agents SDK |
|---|---|---|
| 内置工具 | Read / Write / Edit / Bash / Monitor / Glob / Grep / WebSearch / WebFetch / AskUserQuestion 等成体系 | hosted shell、apply patch、Computer、文件系统等以 capability/tool 形式提供 |
| 工具循环 | SDK 自动驱动 | SDK 自动驱动 |
| 自定义工具 | `@tool` + in-process MCP server，或外部 stdio MCP | `ShellTool(executor=...)`、`Computer`/`AsyncComputer`、`ApplyPatchEditor`、MCP 等接口 |
| 子 agent | `agents` + `AgentDefinition`，通过 `Agent` 工具委派 | Handoffs / Agent orchestration（属于 SDK 一等抽象） |

### 5.3 Skill / 资产

| 维度 | Claude Agent SDK | OpenAI Agents SDK |
|---|---|---|
| Skill 机制 | `.claude/skills/*/SKILL.md` 自动发现，与 Claude Code CLI 完全共享 | Sandbox Agents 的 Skills capability，需要按 SDK 的 Manifest/约定接入 |
| 现有 ai-prd `.claude/skills/` 资产 | 可原地复用 | 需要重新组织/适配 |
| Slash commands / Plugins / Memory | `.claude/commands/`、`CLAUDE.md`、`plugins` 选项原生支持 | 没有同名概念，等价能力需用 capability + Manifest 表达 |

### 5.4 沙箱

| 维度 | Claude Agent SDK | OpenAI Agents SDK |
|---|---|---|
| SDK 层抽象 | 无独立 sandbox 抽象 | `SandboxClient` 抽象 + 内置多个 client |
| 默认 sandbox 后端 | 继承 Claude Code 的 OS 级 sandbox：macOS Seatbelt、Linux bubblewrap（详见 `agent-sandboxing-comparison.md`） | 内置 `UnixLocalSandboxClient`、`DockerSandboxClient`，以及 7 家第三方托管 client |
| OS 级 sandbox 接入 | 已经是 SDK 默认行为，零额外工作 | 需要自实现 `SandboxClient` 或通过 `ShellTool(executor=...)` 包装 `sandbox-exec` / `bwrap` |
| 远程托管 sandbox | 通过 Managed Agents（另一条产品线） | SDK 内一等支持，可直接接入 E2B / Modal / Daytona / Cloudflare / Vercel 等 |

### 5.5 模型可替换性

| 维度 | Claude Agent SDK | OpenAI Agents SDK |
|---|---|---|
| 切换到非默认厂商模型 | 不支持非 Claude；可在 Bedrock / Vertex / Foundry 上跑 Claude | 支持，但第三方为 beta 集成，能力差异由上游决定 |
| 与 ai-prd 既有"Claude Code 为后端"路线的契合 | 自然延续 | 需要把 Claude 经 LiteLLM/Any-LLM 接入 + 重组 Skill 资产 |

### 5.6 工程化成本

| 维度 | Claude Agent SDK | OpenAI Agents SDK |
|---|---|---|
| 一次性接入成本 | 低：直接复用 `.claude/skills/`、`CLAUDE.md`、Claude Code 已有 sandbox 行为 | 中：需要设计 Manifest、SandboxClient、Skills 适配，PoC 验证 Claude on LiteLLM/Any-LLM 行为 |
| 长期可替换性 | 较低（绑定 Claude 与 Anthropic 商业条款） | 较高（模型/沙箱/平台都是可替换组件） |
| 商业条款约束 | 第三方禁止 claude.ai 登录限额；品牌使用受限 | 标准 OpenAI API 计费 |

---

## 6. 评估（基于事实推论）

以下为评估观点，不是事实陈述。

1. **就 ai-prd 当前状态而言，Claude Agent SDK 是迁移成本最低的路径**：它直接复用现有 `.claude/skills/`、CLAUDE.md、Claude Code OS sandbox 行为，与 `assistant-agent-runtime-migration-design.md` 列出的目标能力（Skill 自动加载、Runtime 级会话恢复、Runtime 级执行上下文管理）天然吻合。
2. **OpenAI Agents SDK 的优势在长期可替换性**：模型、沙箱、托管平台都是可换组件；如果 ai-prd 的产品定位需要"模型/平台中立"或"远程托管 sandbox"，它的抽象更适合。
3. **沙箱视角的差异已被澄清**：Claude Agent SDK 不需要自己接 OS sandbox，直接继承 Claude Code 行为；OpenAI Agents SDK 内置 client 偏向 Docker / 第三方 SaaS，OS 级 sandbox 需要自实现 client 或包装 executor。
4. **两者并不互斥**：Claude Agent SDK 用于复用 Claude Code 资产并快速落地；如未来产品演进出"多模型"或"远程并行 sandbox"诉求，再通过独立 PoC 评估是否引入 Agents SDK。

---

## 7. 待验证问题

1. **Claude Agent SDK 的 sandbox 行为是否与 Claude Code CLI 完全等价**：包括失败模式（`sandbox.failIfUnavailable` 是否生效）、`sandboxed Bash` 子进程边界、Read/Edit 走权限系统的行为。
2. **`ClaudeSDKClient`（双向流式）与 ai-prd 后端 SSE 流式输出的对接方式**：消息粒度、错误恢复、长任务断连重连。
3. **Hook 机制能否覆盖 ai-prd 应用层审计需求**：`PreToolUse` / `PostToolUse` 与现有审计/权限链路如何分层。
4. **Bedrock / Vertex / Foundry 路径的实际功能完整度**：Skill、子 agent、Session 恢复在三家平台上是否一致。
5. **Session JSONL 与应用层 SQLite 持久化的事实源关系**：哪一层是真相，恢复路径如何对齐。

---

## 8. 后续动作建议

1. 把"后端基于 Claude Code 运行"的项目记忆扩充为"基于 Claude Code 系（CLI + Agent SDK）运行"，并在 `assistant-agent-runtime-migration-design.md` 中加入 Agent SDK 作为优选迁移目标的事实依据。
2. 在不动主干的前提下做一个最小 PoC：Python 项目内用 `query()` 跑一次最小会话循环，验证 §7 的 1、2、3 点。
3. OpenAI Agents SDK 的 PoC 排在 Claude Agent SDK PoC 之后；其触发条件是产品出现"模型/沙箱多供应商"明确诉求。
