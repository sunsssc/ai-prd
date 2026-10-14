# OpenAI Agents SDK 升级版调研

## 1. 文档信息

- 项目名称：ai-prd
- 文档名称：OpenAI Agents SDK 升级版调研
- 文档版本：V1.0
- 文档日期：2026-05-07
- 文档状态：草案

---

## 2. 背景

`ai-prd` 当前后端 AI 能力的运行时定位是 Claude Code CLI，并已经产出 Claude Code 与 Codex CLI 的沙箱对比、Assistant 迁移到 Agent Runtime 等设计文档。在调研中发现 OpenAI 在 2026 年公布了 Agents SDK 的重大升级，新增 Sandbox Agents 抽象、原生 Skills/Sessions/Snapshot 能力，并提供本地与多家第三方沙箱 client。本调研用于核对该 SDK 的实际能力，与既有 Claude Code / Codex 路线做客观对比，作为后续是否调整 Runtime 选型的事实底稿。

本调研只记录已经核实的事实；尚未验证的能力点单独列出待 PoC，不在结论中作为既定能力陈述。

---

## 3. 信息来源

| 来源 | 性质 | URL |
|---|---|---|
| OpenAI 官方公告（中文） | 升级公告 | https://openai.com/zh-Hans-CN/index/the-next-evolution-of-the-agents-sdk/ |
| Agents SDK Models 章节 | 官方文档 | https://openai.github.io/openai-agents-python/models/ |
| Agents SDK Tools 章节 | 官方文档 | https://openai.github.io/openai-agents-python/tools/ |
| Sandbox Agents 章节 | 官方文档 | https://openai.github.io/openai-agents-python/sandbox_agents/ |
| Sandbox Clients 章节 | 官方文档 | https://openai.github.io/openai-agents-python/sandbox/clients/ |
| 项目文档：Claude Code 与 Codex 沙箱对比 | 内部 | `source/docs/architecture/agent-sandboxing-comparison.md` |
| 项目文档：Assistant 迁移到 Agent Runtime 设计 | 内部 | `source/docs/architecture/assistant-agent-runtime-migration-design.md` |

下方所有"事实"段落均能在以上来源中找到对应表述；推论性内容会显式标注"评估"或"待验证"。

---

## 4. 升级要点（事实）

### 4.1 整体定位

官方公告将本次升级总结为三件事：

1. 提供"模型原生运行框架"，集成 MCP、技能披露、自定义指令、shell 工具、apply patch 工具等。
2. 原生支持沙箱执行：开发者可接入自有沙箱或多家第三方平台。
3. 引入 Manifest 抽象，统一描述工作空间、文件挂载与外部数据接入。

### 4.2 模型抽象

官方 Models 文档说明：

- 提供 `ModelProvider` 接口，将运行时模型名映射到具体实现；同时支持把 `Model` 接口实现直接传给 `Agent.model`。
- 三种粒度的接入方式：
  - `set_default_openai_client`：把"OpenAI 兼容端点"作为全局默认。
  - `Runner.run` 级别的 `ModelProvider`：单次运行级别替换。
  - `Agent.model`：单个 agent 指定提供商或模型实例。
- 提供两类第三方适配器：**LiteLLM** 与 **Any-LLM**。
- 官方示例覆盖 `custom_example_global` / `custom_example_provider` / `custom_example_agent`，以及 LiteLLM、Any-LLM 的 `*_auto` 与 `*_provider` 两种用法。
- 文档明确表述："许多 LLM 提供商仍不支持 Responses API"，建议在这种情况下使用 OpenAI Chat Completions API/模型作为替代。
- 文档同时声明：第三方适配器属于"尽力而为的 beta 集成，功能支持因上游提供商而异"。

### 4.3 Sandbox Agents

官方 Sandbox Agents 文档要点：

- 设计目标原文："Sandbox Agents…give the model a persistent workspace where it can search large document sets, edit files, run commands, generate artifacts, and pick work back up from saved sandbox state."
- 复用标准 `Agent` / `Runner` 流程，通过新增 `Manifest`、capabilities、`SandboxRunConfig` 实现 sandbox 模式。
- Capabilities 包含：Filesystem、Shell、Skills、Memory、Compaction。
- 与普通 hosted shell 的取舍原文："If shell access is only one occasional tool, start with hosted shell in the tools guide. Reach for sandbox agents when workspace isolation, sandbox client choice, or sandbox-session resume behavior are part of the design."
- 提供 Session、SessionState、Snapshot 等抽象，用于会话恢复。

### 4.4 SandboxClient 实现

官方 Sandbox Clients 文档列出的内置 client：

- 本地：
  - `UnixLocalSandboxClient`：标注 "no extra install"、"simple local filesystem development"。
  - `DockerSandboxClient`：基于 Docker 后端。
- 第三方托管：`BlaxelSandboxClient`、`CloudflareSandboxClient`、`DaytonaSandboxClient`、`E2BSandboxClient`、`ModalSandboxClient`、`RunloopSandboxClient`、`VercelSandboxClient`。

文档建议路径："For most users, start with one of these two sandbox clients"，指向本地的两个 client。

### 4.5 工具与本地执行

官方 Tools 文档要点：

- 工具分为多类，其中"Local/runtime execution tools" 明确包含 `ComputerTool` 和 `ApplyPatchTool`，原文："always run in your environment"。
- 自定义本地执行需要实现对应接口：
  - `ComputerTool`：实现 `Computer` 或 `AsyncComputer` 接口。
  - `ApplyPatchTool`：实现 `ApplyPatchEditor` 接口。
  - `ShellTool`：通过 `ShellTool(executor=run_shell)` 注入自定义 shell 执行函数。
- 文档原文："Local runtime tools require you to supply implementations"。
- 文档不包含对 macOS `sandbox-exec`、Linux `bwrap` 等 OS 级 sandbox 后端的直接集成或预置封装。

### 4.6 语言与发布状态

- Python 版本已上线新运行框架与 sandbox 功能；TypeScript 支持已列入发布计划但尚未发布（来源：官方公告）。
- 计费沿用标准 OpenAI API：按 token 使用量与工具调用次数结算（来源：官方公告）。

---

## 5. 与 ai-prd 既有路线的关系

### 5.1 与 Runtime 迁移设计的对齐点

`assistant-agent-runtime-migration-design.md` 列出的目标能力包括：自动搜索代码和文件、自动调用工具并多轮推理、Skill 自动加载、Runtime 级会话恢复、Runtime 级执行上下文管理。

按文档事实，Sandbox Agents 的 Capabilities（Filesystem / Shell / Skills / Memory / Compaction）与 Session/Snapshot 抽象与上述目标在能力清单层面对齐。是否能在语义和工程接入上完整满足，待 PoC 验证。

### 5.2 与沙箱调研文档的关系

`agent-sandboxing-comparison.md` 比较的是 Claude Code 与 Codex 两个 CLI Runtime 的 OS 级沙箱实现。本 SDK 的 SandboxClient 抽象是一层位于 Runtime 之上的接入点，允许把"如何隔离"作为可替换组件。两者并不冲突：如需在 Agents SDK 路径下使用本机 OS 级 sandbox，只能通过实现自定义 `SandboxClient`、`ShellTool.executor` 等方式包装 `sandbox-exec` 或 `bwrap`，**SDK 本身没有现成的 OS-level sandbox client**。该路径的可行性与隔离强度待 PoC 验证。

### 5.3 与"后端基于 Claude Code 运行"记忆的关系

记忆条目陈述当前后端运行时是 Claude Code CLI。Agents SDK 不绑定模型厂商，可以通过 LiteLLM/Any-LLM 接 Claude；但接入后能否完整支持 Claude 的工具调用、流式、prompt caching 等行为，仅依赖文档无法确认（属于上游提供商功能差异范畴），需要 PoC。

---

## 6. 评估（基于事实推论）

| 维度 | 评估 |
|---|---|
| 抽象层级 | Agents SDK 的 Sandbox Agents 与 Claude Code、Codex CLI 在能力清单上对等：Skill、Session、Snapshot、Filesystem、Shell 都是一等公民。 |
| 模型可替换性 | 强于 Claude Code（绑定 Claude）与 Codex（绑定 OpenAI）。Agents SDK 通过 `ModelProvider` / 适配器开放给第三方模型，但适配器为 beta 集成。 |
| 沙箱接入 | 内置仅 `UnixLocalSandboxClient`、`DockerSandboxClient` 与 7 家第三方托管 client；OS 级 sandbox 需要自实现 client 或通过 `ShellTool(executor=...)` 包装命令前缀。 |
| 工程一次性投入 | 高于"直接用 Claude Code CLI"：需要重新组织 Skill 资产、设计自定义 SandboxClient、对齐 ai-prd 应用层与 SDK Session/Manifest 概念。 |
| 长期维护 | 取决于上游 SDK 演进节奏与第三方模型适配器维护质量。 |

结论性判断（**评估**，非事实）：在 ai-prd 当前阶段，Agents SDK 是有竞争力的候选 Runtime，但不应在没有 PoC 的情况下取代 Claude Code 路线。是否切换需先回答下面 §7 的问题。

---

## 7. 待验证问题（PoC 范围）

以下问题的答案直接决定是否切换 Runtime，目前只能通过实跑得到：

1. **Claude on Agents SDK 的实际能力**：通过 LiteLLM 或 Any-LLM 接 Claude 后，工具调用、流式输出、prompt caching、长上下文等是否完整可用，是否存在 SDK 控制流（如自动多轮）与 Claude 行为的语义不对齐。
2. **`UnixLocalSandboxClient` 的隔离强度**：其底层实现是否仅为子进程；是否允许通过自定义 `ShellTool.executor` 或继承 `SandboxClient` 注入 `sandbox-exec` / `bwrap` 包裹；包装后 Filesystem capability 与文件挂载语义是否仍然成立。
3. **Skill 资产对齐**：Agents SDK Skills capability 的目录约定、元数据格式与 ai-prd 现有 `.claude/skills/` 资产能否平滑映射，还是需要重组。
4. **Session/Snapshot 与应用层会话的关系**：SDK 的 SandboxSession 与 ai-prd 应用层（SQLite 中的 session/message 持久化）如何分层，谁是事实源。
5. **TypeScript 时间表**：当前只有 Python 版本，若产品后续需要 Node 侧调用，需评估发布时间表与替代路径。

---

## 8. 已修正的早期误判（备忘）

调研过程中，基于 OpenAI 公告中文摘要曾形成两条结论，后被官方文档证伪，记录在此避免重复犯错：

- 误判一："Agents SDK 专为 OpenAI 模型量身打造，不支持非 OpenAI 模型。"
  - 实际：通过 `ModelProvider` 抽象 + LiteLLM/Any-LLM 适配器支持第三方模型，包括 Claude。来源：Models 章节。
- 误判二："要用 Agents SDK 就必须接入第三方 SaaS 沙箱平台。"
  - 实际：官方提供 `UnixLocalSandboxClient`、`DockerSandboxClient` 两个本地实现，文档明确建议大多数用户从这两个开始。来源：Sandbox Clients 章节。

---

## 9. 后续动作建议

1. 出一份独立的 Claude Agent SDK 调研，对齐本调研的字段口径，便于横向对比（已在跟进）。
2. 在两份调研都完成后，再决定是否启动 PoC，PoC 范围以 §7 的 5 个问题为限。
3. 任何架构决定在 PoC 完成前不进入主干。
