# 两大主流 agent 沙箱模式的实现机制

# 1\. 文档信息

*   文档版本：V1.1
*   文档日期：2026-05-08
* * *

## 2\. 背景

Claude Code 与 Codex 作为 Agent Runtime，两者都提供"沙箱"能力，但实际边界并不相同：Claude Code 的官方沙箱主要约束 sandboxed Bash 及其子进程，内置文件工具仍由权限系统控制；Codex 则按平台为模型触发的命令执行选择不同的 OS 级 sandbox 后端。项目在设计 workspace 权限、Skill 管理、网络访问和运行时部署方案时，需要把平台依赖、失败模式和可控边界拆开看。

本文档聚焦 **OS 级沙箱机制本身** 与 **SDK 接入下沙箱的适用范围**。SDK 与 CLI 的关系、进程数量与生命周期不是本文档主题，参见 [两大主流 agent SDK 和 CLI 实现的比较](./两大主流%20agent%20SDK%20和%20CLI%20实现的比较.md)。
* * *

## 3\. 结论摘要

| 维度 | Claude Code | Codex |
| ---| ---| --- |
| macOS 后端 | Seatbelt，通过 `sandbox-exec` / sandbox profile | Seatbelt，通过 `/usr/bin/sandbox-exec` |
| Linux 后端 | bubblewrap，依赖 Linux namespaces | seccomp + `PR_SET_NO_NEW_PRIVS`，文件系统策略需要时走 bubblewrap；部分 legacy 策略仍可走 Landlock 路径 |
| WSL | WSL2 使用 bubblewrap；WSL1 不支持 | WSL2 使用 Linux bubblewrap 路径；WSL1 不支持 bubblewrap sandboxing |
| Windows | 原生 Windows 支持仍不是主要稳定路径，官方文档描述为 planned | 源码已有 Windows restricted token 后端，但公开支持口径仍需按版本验证 |
| 主要约束对象 | sandboxed Bash 及其子进程；Read/Edit/WebFetch 另走权限系统 | Codex 执行命令时按策略进入平台 sandbox；文件系统策略与网络策略由 Codex runtime 解析 |
| 文件系统模型 | 默认读较宽，默认写当前工作目录；可用权限和 sandbox filesystem 配置收紧 | `read-only` / `workspace-write` / `danger-full-access`，可解析为更细的 filesystem policy |
| 网络模型 | 通过宿主侧代理控制域名访问；空 allowlist 可实现默认拒绝 | 网络策略独立于文件系统；Linux bubblewrap 可隔离 network namespace 并配合代理 |
| 失败模式 | sandbox 不可用时默认警告并退回非 sandbox 命令，需配置 `sandbox.failIfUnavailable=true` 才硬失败 | Linux 优先系统 `bwrap`，缺失时可用 Codex bundled `bwrap`；user namespace 不可用会告警或拒绝进入对应路径 |

macOS 上二者底层都依赖 Seatbelt；Linux/WSL2 上二者都会涉及 bubblewrap，但 Codex 的 Linux sandbox 还包含 seccomp、`PR_SET_NO_NEW_PRIVS` 和 Landlock 兼容路径。不能把“使用 bubblewrap”简化成“同一种安全模型”。
* * *

## 4\. 核心概念

| 概念 | 说明 |
| ---| --- |
| Seatbelt | macOS 原生 sandbox 机制。调用方生成 sandbox profile，再通过 `/usr/bin/sandbox-exec` 启动目标命令。它不是容器文件系统，而是在现有系统视图上执行内核级访问控制。 |
| bubblewrap | Linux 上的 sandbox 构造工具。它用 user、mount、PID、network 等 namespace 组装新的执行环境，通过 `--ro-bind`、`--bind`、`--tmpfs` 等参数决定进程能看到哪些路径。 |
| seccomp | Linux syscall 过滤机制。它限制进程可调用的系统调用，常与 namespace 或 no-new-privs 一起使用。 |
| `PR_SET_NO_NEW_PRIVS` | Linux 进程属性，用于防止子进程通过 setuid 等方式获得新权限。 |
| Landlock | Linux LSM 能力之一，可对文件系统访问做进程自限制。Codex Linux legacy 策略在语义可等价映射时仍可能使用。 |
| 权限系统 | Agent runtime 自己的 tool-level 决策层，例如是否允许 Read、Edit、Bash、WebFetch。它不是 OS sandbox，但可在工具调用前阻止操作。 |

* * *

## 5\. Claude Code 沙箱机制

### 5.1 官方定位

Claude Code 的 sandboxing 文档明确描述为“sandboxed bash tool”能力：它用 OS 级机制隔离 Bash 命令的文件系统和网络访问，以减少频繁命令审批。官方同时说明，权限系统与 sandbox 是互补层：权限控制所有工具，sandbox 只对 Bash 及其子进程提供 OS 级约束。

这意味着 Claude Code 的安全边界要分两层理解：

| 层级 | 约束对象 | 典型配置 |
| ---| ---| --- |
| 权限系统 | Bash、Read、Edit、WebFetch、MCP 等所有工具 | `permissions.allow`、`permissions.ask`、`permissions.deny` |
| OS sandbox | Bash 命令及其子进程 | `sandbox.enabled`、`sandbox.filesystem.*`、`sandbox.network.*` |

如果只启用 OS sandbox，但仍允许 `Read(~/.ssh/**)` 或 `Edit(...)` 这类工具权限，就不能认为整个 Agent 已被同一层 OS sandbox 完整包住。

### 5.2 平台实现

| 平台 | 实现 | 依赖 |
| ---| ---| --- |
| macOS | Seatbelt | 系统内置，通常开箱可用 |
| Linux | bubblewrap | 需要安装 `bubblewrap` 和 `socat` |
| WSL2 | bubblewrap | 同 Linux；WSL1 缺少所需 namespace 能力 |
| Windows | 非当前主要稳定路径 | 官方文档写明 native Windows support planned |

Claude Code 文档还提到，如果开启 sandbox 但依赖缺失或平台不支持，默认行为是显示警告并让命令不经 sandbox 运行；若需要把 sandbox 作为硬安全门槛，应设置：

```json
{
  "sandbox": {
    "enabled": true,
    "failIfUnavailable": true
  }
}
```

### 5.3 文件系统策略
Claude Code sandbox 的默认行为是：

| 访问类型 | 默认行为 |
| ---| --- |
| 读 | 读范围较宽，敏感位置依赖 deny 规则收紧 |
| 写 | 当前工作目录及其子目录可写 |
| 额外写路径 | 通过 `sandbox.filesystem.allowWrite` 增加 |
| 禁读/禁写 | 通过 `sandbox.filesystem.denyRead`、`sandbox.filesystem.denyWrite` 和权限规则收紧 |

配置合并时需要注意：`sandbox.filesystem.allowWrite` 等数组会跨 settings scopes 合并，不是简单覆盖。团队级受控场景应优先使用 managed settings，避免用户或项目配置扩大边界。

### 5.4 网络策略

Claude Code 的网络隔离通过宿主侧代理实现：
*   sandbox 内进程不能直接自由联网。
*   HTTP/HTTPS 与其他 TCP 流量通过宿主代理转发。
*   代理按域名 allowlist / denylist 做决策。
*   内置代理不解密 TLS，不做 HTTPS 内容检查。

因此，允许过宽域名会带来数据外传风险。对强安全场景，不能只写 `*.github.com`、`*.npmjs.org` 这类宽规则后就认为安全闭环成立。

### 5.5 重要限制

| 限制 | 影响 |
| ---| --- |
| sandbox 只覆盖 Bash | Read/Edit/Write/WebFetch/MCP 仍要靠权限系统约束 |
| sandbox 不可用默认可退回非 sandbox | 受控部署必须开启 `failIfUnavailable` |
| `dangerouslyDisableSandbox` 逃逸口默认可用 | 企业策略应考虑 `allowUnsandboxedCommands=false` |
| Unix socket 放行风险高 | 放行 Docker socket 等同于给宿主执行入口 |
| Linux nested sandbox 弱化模式会降级安全性 | `enableWeakerNestedSandbox` 只适合外层已有隔离的环境 |

* * *

## 6\. Codex 沙箱机制

### 6.1 官方定位

Codex CLI 的公开说明把 sandbox 与 approval 分成两层：approval 决定是否需要用户确认，sandbox 决定命令在 OS 层能访问什么。OpenAI Help Center 对早期 CLI 的描述是：Full Auto 会在网络禁用、限定当前目录范围的 sandbox 中自主读写和执行命令。当前 Codex 源码进一步把 sandbox 抽象为平台后端和文件系统/网络策略。

常见 sandbox mode：

| 模式 | 语义 |
| ---| --- |
| `read-only` | 允许读，禁止写；适合分析、审查 |
| `workspace-write` | 允许在工作区或显式 writable roots 写；受保护元数据仍只读 |
| `danger-full-access` | 不做沙箱限制；只应在外层容器/VM 已提供隔离时使用 |

### 6.2 平台实现

Codex 源码中的 sandbox manager 会按平台选择后端：

| 平台 | 后端 | 说明 |
| ---| ---| --- |
| macOS | `MacosSeatbelt` | 使用 `/usr/bin/sandbox-exec`，由 Codex 生成 Seatbelt profile |
| Linux | `LinuxSeccomp` | Linux helper 组合 seccomp、`PR_SET_NO_NEW_PRIVS` 和文件系统 sandbox |
| Windows | `WindowsRestrictedToken` | 源码已有 restricted token 后端；实际产品支持口径需按发布版本确认 |

### 6.3 macOS：Seatbelt

Codex 的 `codex-core` 明确要求 macOS 环境存在 `/usr/bin/sandbox-exec`。在 `workspace-write` 策略下，Seatbelt profile 允许 configured writable roots 下写入，同时保持 `.git`、解析后的 `gitdir:` 目标和 `.codex` 只读。网络和文件读写根由 `SandboxPolicy` 解析后交给 Seatbelt 执行。

Codex 源码还固定只使用 `/usr/bin/sandbox-exec`，避免 PATH 中被注入同名可执行文件。

### 6.4 Linux：seccomp + bubblewrap / Landlock

Codex Linux 不能只理解成“等于 bubblewrap”。源码中的实际分层是：

| 层 | 作用 |
| ---| --- |
| seccomp + `PR_SET_NO_NEW_PRIVS` | 限制 syscall 和提权路径 |
| bubblewrap | 在 exec 前构造文件系统视图和 namespace |
| Landlock legacy path | 对仍可等价映射到 legacy `SandboxPolicy` 的策略保留 |

Codex 的 bubblewrap 文件系统策略与 macOS Seatbelt 语义对齐：
*   文件系统默认只读。
*   显式 writable roots 再通过 `--bind` 叠加为可写。
*   `.git`、`.agents`、`.codex` 等敏感元数据即使位于 writable root 内也继续只读。
*   restricted-read 策略可从 `--tmpfs /` 开始，只挂载被允许读取的路径。
*   需要网络隔离时使用 `--unshare-net`。

Linux 依赖处理也有细节：
*   优先使用 PATH 中第一个不位于当前工作目录内的系统 `bwrap`。
*   如果系统 `bwrap` 缺失，可回退到 Codex 随包携带的 `codex-resources/bwrap`。
*   如果 user namespace 不可用，Codex 会通过正常通知路径告警；WSL1 会拒绝进入 bubblewrap sandboxing 路径。

### 6.5 网络策略

Codex 的网络策略独立于文件系统 sandbox。Linux bubblewrap 路径在需要限制网络时会 unshare network namespace；更细粒度的代理式网络控制由 runtime 策略和 helper 负责。对项目设计而言，应把“是否允许联网”和“可写哪些目录”拆成两个独立配置项，不要因为 `workspace-write` 就默认允许网络。
* * *

## 7\. Seatbelt 与 bubblewrap 的本质区别

| 维度 | Seatbelt | bubblewrap |
| ---| ---| --- |
| 平台 | macOS | Linux / WSL2 |
| 安全机制 | macOS kernel sandbox profile | Linux namespaces + bind mount，可叠加 seccomp |
| 文件系统视图 | 仍是宿主真实路径视图，由 profile allow/deny | 新 mount namespace，可从空 root 组装 |
| 典型表达 | `(allow file-read* ...)`、`(deny file-write* ...)` | `--tmpfs /`、`--ro-bind`、`--bind` |
| 网络隔离 | profile 规则与代理通道 | network namespace、代理、seccomp 组合 |
| 进程视图 | 不以 PID namespace 为核心 | 可使用 `--unshare-pid` |
| 依赖 | macOS 系统自带 | 需要 Linux kernel namespace 能力和 `bwrap` |
| 失败风险 | profile 规则错误导致误阻断或误放行 | mount 顺序、bind 范围、namespace 能力导致路径不可见或误暴露 |

Seatbelt 更像“在当前系统视图上做强制访问控制”；bubblewrap 更像“给进程搭一个新的 Linux 执行环境”。两者都能服务 agent sandbox，但策略表达、可观测性和故障排查方式不同。
* * *

## 8\. SDK 入口下的沙箱继承关系

第 5、6 章描述 CLI 进程内部的 OS 级沙箱实现。三方应用后端实际可能不直接 spawn `claude`/`codex` 二进制，而是通过 SDK 编程接入。本节聚焦"哪些代码在/不在 OS sandbox 内"。SDK 与 CLI 的关系、进程模型、生命周期管理见 [两大主流 agent SDK 和 CLI 实现的比较](./两大主流%20agent%20SDK%20和%20CLI%20实现的比较.md) 。

需要先明确：OpenAI 同时提供两条相关 SDK 产品线，它们与 CLI 沙箱的关系完全不同——

| SDK 产品 | 定位 | 与 CLI 沙箱的关系 |
| ---| ---| --- |
| Claude Agent SDK | `claude` 二进制的 stream-json 协议客户端 | §8.1 完整继承 |
| Codex SDK | `codex` 二进制的 JSON-RPC 协议客户端 | §8.1 完整继承 |
| OpenAI Agents SDK（升级版通用 agent 框架） | 独立产品，与 Codex CLI 无代码继承 | §8.2 不继承，需自实现 |

### 8.1 Claude Agent SDK 与 Codex SDK：CLI binary 客户端路径

两者本质相同：SDK 都是 native CLI binary 的协议客户端（Anthropic 走 stream-json，Codex 走 JSON-RPC 2.0），由 SDK spawn binary 子进程。OS 级 sandbox 由 binary 内部执行，与直接跑 CLI 等价。

| 维度 | 结论 |
| ---| --- |
| 第 5 / 6 章描述的所有 sandbox 行为 | 在 SDK 路径下完整继承，binary 内部执行 |
| 平台后端 | 与 CLI 一致 |
| 配置入口 | Claude：`.claude/`、`~/.claude/`；Codex：`~/.codex/` |
| 失败模式 | 与 CLI 一致；受控部署仍需 `sandbox.failIfUnavailable=true`（Claude）或对应 Codex 配置 |

但 SDK 接入引入一个 CLI 单进程模式下不存在的新边界——**宿主进程与 binary 子进程的进程边界**：

| 进程层 | 沙箱适用性 |
| ---| --- |
| 宿主进程（如 FastAPI） | 不在 sandbox 之内，宿主权限就是运行时进程权限 |
| binary 子进程<br>（`claude` / `codex app-server`） | 第 5 / 6 章 sandbox 行为生效 |
| sandbox 内 fork 的子孙进程 | 受 sandbox 直接约束 |
| SDK 宿主侧扩展点<br>（`@tool` / Hook 回调 / 自定义 transport） | 运行在宿主进程，不在 binary 子进程内，因此不在 OS sandbox 内 |

工程含义：

*   不能把"binary 已开 sandbox"等同于"整个后端服务已被 sandbox 包住"。binary 内置工具仍走权限系统约束；自定义工具与 Hook 跑在宿主，需在宿主侧另做权限控制。
*   进程粒度差异：
    *   Anthropic 每会话一个 binary 子进程，sandbox 按子进程粒度生效；
    *   Codex 一个 app-server 承载多 Thread，sandbox 按 server 进程粒度生效。这是相同的"宿主 vs 子进程"边界，但**子进程内部的会话隔离强度不同**。

### 8.2 OpenAI Agents SDK：独立 SandboxClient 体系

（来源：官方文档 Sandbox Agents、Sandbox Clients、Tools 章节）：

*   提供 `SandboxClient` 抽象，内置 `UnixLocalSandboxClient`、`DockerSandboxClient` 与 7 家第三方托管 client（E2B、Modal、Daytona 等）。
*   内置 client **不包含** macOS Seatbelt、Linux bubblewrap 等 OS 级 sandbox 后端预置封装。文档原文："Local runtime tools require you to supply implementations"。
*   接入 OS 级隔离需自行实现：自定义 `SandboxClient`，或通过 `ShellTool(executor=run_shell)` 在 executor 内包装 `sandbox-exec` / `bwrap` 命令前缀。

| 维度 | 结论 |
| ---| --- |
| 与 Codex CLI 沙箱的关系 | 不存在自动继承。Codex 的 Linux helper（seccomp + bwrap + Landlock）不会被 Agents SDK 自动复用 |
| 默认隔离强度 | `UnixLocalSandboxClient` 文档定位为 "simple local filesystem development"，不是 OS 级强隔离 |
| 接入 OS sandbox 的工程量 | 中等：需自实现 client 或包装 executor，并自行验证隔离行为与失败模式 |
| 远程托管沙箱 | SDK 一等支持（E2B / Modal 等），这是其他四种入口方案都不直接提供的能力 |

### 8.3 选型对沙箱设计的影响

| 入口方案 | 沙箱设计要点 |
| ---| --- |
| 直接 spawn Claude Code CLI | 第 5 章全部适用 |
| 直接 spawn Codex CLI | 第 6 章全部适用 |
| 通过 Claude Agent SDK | 第 5 章全部适用；按 §8.1 处理"宿主 vs 子进程"边界 |
| 通过 Codex SDK | 第 6 章全部适用；按 §8.1 处理"宿主 vs 子进程"边界 |
| 通过 OpenAI Agents SDK | 第 6 章不自动适用；按 §8.2 自实现 `SandboxClient` 或 `ShellTool.executor` 包装 |

* * *

## 9\. 对三方应用的设计建议

### 9.1 Runtime 接入原则

| 原则 | 说明 |
| ---| --- |
| 不依赖 agent 自律 | 只用 prompt 要求 agent 不写文件不够，必须配置 tool 权限和 OS sandbox |
| 不把 Claude Code sandbox 当全工具沙箱 | Claude 的 Bash sandbox 与 Read/Edit 权限要同时配置 |
| 不把 Codex Linux 简化为 bwrap | 需要同时考虑 seccomp、Landlock 兼容路径和 bwrap availability |
| 不默认允许网络 | 网络应作为显式能力单独开启，且按域名或代理策略收敛 |
| 不使用 fallback 放宽安全边界 | sandbox 不可用时应失败或拒绝执行，而不是静默降级 |

### 9.2 推荐默认策略

| 场景 | Claude Code | Codex |
| ---| ---| --- |
| 只读代码/文档分析 | 允许 Read/Grep/Glob/LS；Bash 只开只读命令白名单；启用 sandbox 且 `failIfUnavailable=true` | `read-only` sandbox；无 writable roots；网络关闭 |
| 修改 workspace 草稿 | 明确允许 Edit/Write 到 workspace 草稿目录；Bash sandbox 只允许该目录写 | `workspace-write`，writable roots 只包含草稿目录 |
| 创建/更新 Skill | agent 只生成草案，后端受控写入 | agent 只生成草案，后端受控写入 |
| 安装依赖/联网调研 | 单独审批，并限制域名 | 单独审批，并显式开启网络 |
| CI / 容器隔离环境 | 可考虑外层容器作为主边界，但仍应保留 tool deny | 可考虑 `danger-full-access`，前提是外层容器/VM 是真实安全边界 |

## 10\. 已核对资料

### 10.1 CLI 层 OS 沙箱

| 资料 | 用途 |
| ---| --- |
| [Claude Code Sandboxing](https://code.claude.com/docs/en/sandboxing) | Claude Code sandbox 的对象、平台后端、默认行为、限制和配置项 |
| [Claude Code Settings](https://code.claude.com/docs/en/configuration) | settings scopes、sandbox 配置、`failIfUnavailable`、`allowUnsandboxedCommands` 等配置说明 |
| [Anthropic sandbox-runtime](https://github.com/anthropic-experimental/sandbox-runtime) | Claude sandbox runtime 的开源实现说明，验证 macOS 使用 `sandbox-exec`、Linux 使用 bubblewrap 与代理模型 |
| [OpenAI Codex CLI Help Center](https://help.openai.com/en/articles/11096431-openai-codex-cli-getting-started) | Codex CLI approval mode 与 Full Auto sandbox 基本行为 |
| [OpenAI Codex core README](https://github.com/openai/codex/blob/main/codex-rs/core/README.md) | Codex 各平台依赖、macOS `/usr/bin/sandbox-exec`、Linux bubblewrap/Landlock/WSL 行为 |
| [Codex seatbelt.rs](https://github.com/openai/codex/blob/main/codex-rs/sandboxing/src/seatbelt.rs) | Codex macOS Seatbelt profile 生成和 `/usr/bin/sandbox-exec` 使用方式 |
| [Codex bwrap.rs](https://github.com/openai/codex/blob/main/codex-rs/linux-sandbox/src/bwrap.rs) | Codex Linux bubblewrap 文件系统挂载、network namespace、seccomp/no-new-privs 组合 |
| [Codex sandbox manager](https://github.com/openai/codex/blob/main/codex-rs/sandboxing/src/manager.rs) | Codex 平台 sandbox backend 选择逻辑 |
| [containers/bubblewrap README](https://github.com/containers/bubblewrap/blob/main/README.md) | bubblewrap 自身的 namespace、mount、PID、network、seccomp 能力说明 |

### 10.2 SDK 入口下的沙箱

| 资料 | 用途 |
| ---| --- |
| [Claude Agent SDK Overview](https://code.claude.com/docs/en/agent-sdk/overview) | Claude Agent SDK 定位、能力、与 CLI 关系 |
| [claude-agent-sdk-python: subprocess\_cli.py](https://github.com/anthropics/claude-agent-sdk-python/blob/main/src/claude_agent_sdk/_internal/transport/subprocess_cli.py) | 验证 SDK 通过 SubprocessCLITransport 启动 `claude` 子进程并使用 stream-json 协议 |
| [Codex SDK 官方文档](https://developers.openai.com/codex/sdk) | Codex SDK 通过 JSON-RPC 控制 `codex app-server` |
| [OpenAI Agents SDK: Sandbox Clients](https://openai.github.io/openai-agents-python/sandbox/clients/) | 内置 SandboxClient 实现清单（UnixLocal / Docker / 7 家第三方） |
| [OpenAI Agents SDK: Tools](https://openai.github.io/openai-agents-python/tools/) | ShellTool / ComputerTool / ApplyPatchTool 接入接口 |
|  |  |
|  |  |