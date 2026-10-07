# Workspace Agent 只读约束设计方案

## 1. 文档信息

- 项目名称：ai-prd
- 文档名称：Workspace Agent 只读约束设计方案
- 文档版本：V1.0
- 文档日期：2026-05-07
- 文档状态：已废弃，由 V2.1 Workspace 设计取代

---

> 历史说明：本文记录 2026-05-07 的第一阶段全局只读方案，不代表当前实现。当前路径与权限契约以 [《用户、组织与 Agent Runtime 沙箱 Workspace 设计方案》](./user-workspace-runtime-design.md)为准：普通 Assistant 按 `RuntimeWorkspacePlan` 仅开放 `/me`、`/tmp`，无写挂载和隔离后台任务保持只读。

---

## 2. 背景

`ai-prd` 已经把应用源码、知识资产和运行时数据拆分为 `source/` 与 `workspace/` 两层，但当前 Agent Runtime 的文件系统权限仍偏宽：Codex 可写 `workspace` 与 `.claude/skills`，Claude Code 以项目根目录运行且默认使用 `bypassPermissions`，Skill 也仍位于项目根目录 `.claude/skills`。这会让产品内置 agent 有机会修改应用源码、公共代码知识库或项目级 Skill，破坏“应用自身代码不可被运行时修改”的边界。第一阶段需要先把 agent 收紧为只读分析能力，同时保留代码检索、Git 只读分析和 Skill 创建体验。

---

## 3. 设计目标

1. Agent Runtime 的工作目录限定在 `workspace` 内。
2. agent 不允许直接修改 `source/`、根目录配置、部署文件或项目根目录 `.claude/skills`。
3. agent 不允许直接修改 `workspace/knowledge/code` 中的公共代码知识库。
4. agent 不允许直接创建、编辑或删除 Skill 文件。
5. Skill 本体迁移到 `workspace` 内，由后端受控管理。
6. Codex 与 Claude Code 均保留只读分析能力，Claude Code 额外保留必要的只读 Bash 命令。
7. 每个 turn 结束后检查公共代码知识库是否被异常修改，并向前端提示兜底处理结果。

---

## 4. 非目标

- 第一阶段不开放 agent 直接写入个人工作目录；个人目录能力等后续工作区模型明确后再设计。
- 第一阶段不开放网络访问能力，例如 `curl`、`wget` 或包管理器。
- 第一阶段不依赖 turn 后回滚作为主要安全边界；回滚只是兜底机制。
- 第一阶段不把所有 runtime 都迁移到容器级隔离；如 Claude sandbox 不能满足要求，再补充 OS/container 级方案。

---

## 5. 核心决策

| 决策 | 方案 | 理由 |
|---|---|---|
| agent cwd | `<project_root>/workspace` | agent 只面对工作区内容，避免默认看到项目源码根目录 |
| Skill 目录 | `workspace/.claude/skills` | 满足“Skill 在 workspace 内”，同时保留 Claude Code 对 `.claude/skills` 的原生发现约定 |
| Codex 权限 | `read-only` sandbox，无 writable roots | Codex 原生支持只读沙箱，第一阶段不需要写根目录 |
| Claude 权限 | 禁 `bypassPermissions`，禁写工具，保留只读 Bash 白名单 | 保留代码理解所需的检索能力，同时去掉直接写盘能力 |
| Skill 创建 | agent 只产出草案，后端受控写盘 | Claude Code 与 Codex 均可用，且不需要给 agent 写权限 |
| 代码库兜底 | turn 前后记录 Git 状态，发现异常改动后提示并回滚 | 处理权限漏网或 runtime 行为异常 |

---

## 6. 目标目录结构

```text
ai-prd/
├── source/
│   ├── backend/
│   ├── frontend/
│   └── docs/
└── workspace/
    ├── .claude/
    │   └── skills/
    ├── knowledge/
    │   ├── requirements/
    │   ├── business-docs/
    │   └── code/
    └── runtime/
```

说明：

- `workspace/.claude/skills` 是第一阶段推荐的 Skill 物理目录。
- UI 可以展示为“Workspace Skills”，不暴露 `.claude` 目录名。
- `workspace/knowledge/code` 是公共代码知识镜像，只允许同步任务更新，不允许 agent 修改。
- `workspace/runtime` 继续承载 DB、上传文件、日志、同步状态等运行时数据，但不作为 agent 可写目录。

---

## 7. Codex Runtime 约束设计

### 7.1 当前问题

当前配置与实现存在以下宽权限：

| 位置 | 当前行为 | 问题 |
|---|---|---|
| `settings.ai_codex_sandbox` | 默认 `workspace-write` | Codex 可以写入指定 writable roots |
| `settings.ai_codex_writable_roots` | 默认 `["workspace", ".claude/skills"]` | agent 可以写 workspace 和项目根目录 Skill |
| `CodexAgentRuntimeClient._resolve_write_policy` | 创建 writable roots | read-only 模式下不应主动创建可写目录 |

### 7.2 目标配置

| 配置项 | 第一阶段默认值 |
|---|---|
| `AI_CODEX_SANDBOX` | `read-only` |
| `AI_CODEX_WRITABLE_ROOTS` | 空列表 |
| Codex cwd | `<project_root>/workspace` |
| 网络访问 | `false` |

### 7.3 App Server 调用规则

每次 turn 启动本地 stdio 进程：

```text
codex app-server
```

完成 `initialize` / `initialized` 握手后，新会话依次调用：

```text
thread/start
turn/start
```

恢复会话依次调用：

```text
thread/resume
turn/start
```

Thread 参数保留：

```text
cwd=<project_root>/workspace
approvalPolicy=never
sandbox=read-only
developerInstructions=<system_prompt>
```

`thread/resume` 额外传 `excludeTurns=true`：runtime 只消费 thread 的 id、name、cwd，
不需要回传 turns 历史；含图历史若整体回传，会使单行 JSONL 超过 app-server 子进程
4 MiB 流读取上限（`Separator is not found, and chunk exceed the limit`）。

Turn 使用 `sandboxPolicy={type: "readOnly", networkAccess: false}`。当显式配置
`workspace-write` 时，仅把配置允许的目录传入 `writableRoots`。

说明：`approvalPolicy=never` 保证 runtime 不在产品会话中向用户请求越权操作；只读能力由
`readOnly` sandbox policy 提供。

### 7.4 Codex 可用能力

| 能力 | 是否允许 | 说明 |
|---|---:|---|
| 读取 workspace 文件 | 是 | 用于需求、业务文档、代码知识分析 |
| 搜索 workspace 文件 | 是 | 依赖 Codex 只读工具能力 |
| 写文件 | 否 | 包括 Skill 文件 |
| 修改代码知识库 | 否 | 由 sandbox 和 turn 后审计双重约束 |
| 网络访问 | 否 | 第一阶段不开放 |

---

## 8. Claude Code Runtime 约束设计

### 8.1 当前问题

当前配置与实现存在以下宽权限：

| 位置 | 当前行为 | 问题 |
|---|---|---|
| `settings.ai_permission_mode` | 默认 `bypassPermissions` | 适合隔离容器，不适合直接运行在项目根目录 |
| `settings.ai_allowed_tools` | 包含 `Bash`、`Edit`、`Write` | agent 可执行命令并写文件 |
| `ClaudeAgentOptions.cwd` | 使用 `runtime_working_directory`，当前为项目根目录 | agent 默认可见项目源码和根目录配置 |
| `setting_sources` | `["user", "project"]` | 可能加载项目根目录配置与用户级设置，边界不够集中 |

### 8.2 目标配置

| 配置项 | 第一阶段默认值 |
|---|---|
| Claude cwd | `<project_root>/workspace` |
| `AI_PERMISSION_MODE` | `default` 或 `dontAsk`，不使用 `bypassPermissions` |
| 基础工具 | `Read`、`Grep`、`Glob`、`LS` |
| 可选工具 | `Task` |
| 写工具 | 不允许 `Edit`、`Write`、`MultiEdit` |
| Bash | 仅允许只读命令白名单 |
| 网络访问 | 不允许 |

### 8.3 Bash 白名单

第一阶段允许的 Bash 命令只用于本地只读分析：

| 命令 | 用途 |
|---|---|
| `rg` | 快速全文搜索 |
| `git status` | 查看仓库状态 |
| `git diff` | 查看未提交或指定提交差异 |
| `git show` | 查看提交、对象或文件内容 |
| `git log` | 查看提交历史 |
| `git blame` | 查看代码行历史 |
| `git grep` | 在 Git 管理文件中搜索 |
| `git ls-files` | 列出 Git 管理文件 |
| `git rev-parse` | 解析 HEAD、分支、提交标识 |
| `find` | 只读枚举文件 |
| `sed -n` | 只读抽取片段 |
| `head` | 查看文件头部 |
| `tail` | 查看文件尾部 |
| `wc` | 统计行数、字节数 |

建议权限规则表达：

```text
Bash(rg *)
Bash(git status *)
Bash(git diff *)
Bash(git show *)
Bash(git log *)
Bash(git blame *)
Bash(git grep *)
Bash(git ls-files *)
Bash(git rev-parse *)
Bash(find *)
Bash(sed -n *)
Bash(head *)
Bash(tail *)
Bash(wc *)
```

### 8.4 禁止项

第一阶段明确禁止：

```text
Edit
Write
MultiEdit
Bash(rm *)
Bash(mv *)
Bash(cp *)
Bash(mkdir *)
Bash(touch *)
Bash(chmod *)
Bash(chown *)
Bash(tee *)
Bash(git add *)
Bash(git commit *)
Bash(git reset *)
Bash(git restore *)
Bash(git checkout *)
Bash(git clean *)
Bash(git pull *)
Bash(git fetch *)
Bash(git push *)
Bash(git merge *)
Bash(git rebase *)
Bash(git stash *)
Bash(git apply *)
Bash(curl *)
Bash(wget *)
Bash(ssh *)
Bash(scp *)
Bash(npm *)
Bash(pnpm *)
Bash(yarn *)
Bash(pip *)
Bash(python *)
Bash(node *)
Bash(sh *)
Bash(bash *)
Bash(zsh *)
```

说明：`curl` 不写磁盘，但它打开网络边界，可能访问内网服务、发送本地内容、引入不可审计外部上下文，因此第一阶段不开放。

### 8.5 Sandbox 要求

Bash 白名单只能表达工具使用意图，不能作为最终安全边界。Claude Code 第一阶段需要启用 sandbox，并设置为 sandbox 不可用时失败。

目标约束：

| 项 | 要求 |
|---|---|
| sandbox | 启用 |
| `failIfUnavailable` | `true` |
| `allowUnsandboxedCommands` | `false` |
| 文件系统 | `workspace` 只读 |
| 网络 | 禁止 |

实现前需要验证 Python SDK 的 `ClaudeAgentOptions` 是否能直接传递 sandbox 配置；如果不能，需要通过 Claude Code settings 文件、环境变量或 CLI 参数注入。

### 8.6 Claude 验证命令

第一阶段实现后需要用真实 runtime 验证：

| 验证动作 | 预期 |
|---|---|
| `rg "keyword" workspace/knowledge` | 成功 |
| `git diff` | 成功 |
| `git show HEAD` | 成功 |
| `touch /tmp/ai-prd-test` | 失败或不允许 |
| `touch workspace/knowledge/code/test` | 失败或不允许 |
| `echo test > workspace/knowledge/code/test` | 失败或不允许 |
| `rm workspace/knowledge/code/test` | 失败或不允许 |
| `curl https://example.com` | 失败或不允许 |

---

## 9. Skill 管理设计

### 9.1 Skill 根目录

`settings.skills_root` 改为：

```text
<project_root>/workspace/.claude/skills
```

所有 Skill 浏览、读取、创建、编辑、删除接口都继续依赖 `settings.skills_root`，不再依赖项目根目录 `.claude/skills`。

### 9.2 Skill 发现

当前 `discover_project_skills(working_directory)` 写死扫描：

```text
<working_directory>/.claude/skills
```

第一阶段改为显式传入 `skills_root`：

```python
discover_project_skills(skills_root: str) -> list[RuntimeSkill]
```

Runtime client 的 `list_available_skills()` 从配置或初始化参数读取 `skills_root`，不要从 `working_directory` 推导。

### 9.3 Skill 创建

第一阶段统一采用后端受控写盘：

1. 用户在 Skill 页输入创建意向。
2. 后端调用 Claude Code 或 Codex。
3. agent 只输出结构化 Skill 草案。
4. 后端解析草案。
5. 后端调用 `WorkspaceBrowserService.create_skill(...)` 写入 `settings.skills_root`。
6. 前端刷新 Skill 树。

草案结构：

```json
{
  "folder_name": "requirement-review",
  "name": "需求评审",
  "description": "当用户需要审查需求完整性、业务一致性和风险点时使用。",
  "skill_markdown": "---\nname: 需求评审\n...\n---\n\n# 需求评审\n...",
  "scripts": []
}
```

agent prompt 需要明确：

```text
目标路径是 workspace/.claude/skills/<folder_name>/。
你不得直接创建、修改或删除文件。
你只需要输出 Skill 草案，由后端受控写入。
```

### 9.4 后端写盘校验

| 校验项 | 规则 |
|---|---|
| 根目录 | 只能写入 `settings.skills_root` |
| Skill 文件夹 | 只能创建 `settings.skills_root/<folder_name>` |
| 文件名 | 主文件固定为 `SKILL.md` |
| 脚本路径 | 只能位于当前 Skill 文件夹内 |
| 路径穿越 | 禁止绝对路径、`../`、反斜杠逃逸 |
| 覆盖 | 已存在 Skill 文件夹时报错 |
| 非 Skill 文件夹删除 | 禁止 |

### 9.5 Skill 所有权与修改权限

Skill 内容仍保存在 `workspace/.claude/skills/<skill_key>/`，但权限依据独立保存在认证数据库的 `skill_ownership` 表中：

| 字段 | 说明 |
|---|---|
| `skill_key` | Skill 顶层目录名，作为稳定资源标识 |
| `creator_user_id` | 创建或导入 Skill 的登录用户 ID |
| `created_at` | 所有权记录创建时间 |

`SKILL.md` frontmatter 中的 `creator` 只用于文档展示和编辑日志，不参与授权，避免用户通过修改文件内容转移所有权。

权限规则：

1. 所有已登录用户都可以浏览和引用 Skill。
2. 创建人可以编辑、增删自己 Skill 内的文件，并删除整个 Skill。
3. 普通用户不能修改或删除他人创建的 Skill。
4. 管理员可以修改或删除所有 Skill，但操作非本人 Skill 时，前端必须提示，后端也必须收到显式的 `admin_override_confirmed` 确认。
5. 没有所有权记录的存量目录视为“历史 Skill / 所有者未知”，只允许管理员在显式确认后修改或删除。
6. 普通创建、意向创建、流式创建和 ZIP 导入成功后，都必须写入同一份所有权记录。

Skill 树接口向前端返回创建人、所有权状态、当前用户是否可编辑，以及是否需要管理员确认。所有写接口仍在后端重复鉴权，不能只依赖前端隐藏按钮。

---

## 10. Turn 后代码库审计与兜底

### 10.1 审计范围

只审计：

```text
workspace/knowledge/code/*
```

其中每个包含 `.git` 的一级子目录视为一个代码知识库。

### 10.2 turn 前快照

每个 turn 开始前记录：

| 字段 | 说明 |
|---|---|
| `repo_path` | 代码库路径 |
| `head` | `git rev-parse HEAD` |
| `status` | `git status --porcelain` |
| `captured_at` | 快照时间 |

### 10.3 turn 后检查

每个 turn 完成、失败或异常中断后检查：

| 检查项 | 处理 |
|---|---|
| turn 前 clean，turn 后 tracked dirty | 自动回滚并提示 |
| turn 前 clean，turn 后 HEAD 变化 | 记录高风险事件并提示 |
| turn 前 clean，turn 后 untracked 文件 | 第一阶段先提示；是否隔离到 quarantine 放到第二阶段 |
| turn 前已 dirty | 不自动回滚，避免误删人工或同步任务改动 |

### 10.4 回滚策略

第一阶段只对 turn 前 clean 的 repo 自动处理 tracked dirty：

```text
git reset --hard <turn_start_head>
```

不自动执行：

```text
git clean
```

原因：`git clean` 会删除 untracked 文件，第一阶段无法可靠区分用户文件、同步任务产物和 agent 异常产物。

### 10.5 前端提示事件

新增 SSE 事件：

```json
{
  "type": "workspace_guard",
  "level": "warning",
  "message": "检测到代码知识库被异常修改，已自动回滚 3 个 tracked 文件。",
  "details": {
    "repo": "workspace/knowledge/code/example-repo",
    "rolled_back_files": ["src/a.py", "src/b.py"],
    "untracked_files": []
  }
}
```

该事件同时持久化到 runtime events，便于 turn trace 审计。

---

## 11. 配置变更

| 配置项 | 当前默认值 | 第一阶段默认值 |
|---|---|---|
| `skills_root` | `<project_root>/.claude/skills` | `<project_root>/workspace/.claude/skills` |
| `ai_working_directory` | `<project_root>` | 保持 `<project_root>`，runtime client 内部将 cwd 解析到 `workspace` |
| `ai_permission_mode` | `bypassPermissions` | `default` 或 `dontAsk` |
| `ai_allowed_tools` | `Bash, Edit, Glob, Grep, LS, Read, Task, Write` | `Read, Grep, Glob, LS, Task, Bash(只读白名单)` |
| `ai_codex_sandbox` | `workspace-write` | `read-only` |
| `ai_codex_writable_roots` | `workspace,.claude/skills` | 空列表 |

说明：`ai_working_directory` 暂时保留为项目根目录，避免影响 DB、上传目录、workspace service 等应用侧路径解析；agent runtime 的实际 cwd 由 runtime client 收敛到 `workspace`。

---

## 12. 实施步骤

### 12.1 阶段一：配置与路径收敛

1. 创建 `workspace/.claude/skills`。
2. 将现有 `.claude/skills` 内容迁移到 `workspace/.claude/skills`。
3. 修改 `settings.skills_root` 默认值。
4. 修改前端和后端文案，将目标路径从 `.claude/skills` 改为 `workspace/.claude/skills`。

### 12.2 阶段二：Codex 只读化

1. 修改 `ai_codex_sandbox` 默认值为 `read-only`。
2. 修改 `ai_codex_writable_roots` 默认值为空列表。
3. 调整 Codex command 构建逻辑，writable roots 为空时不传可写根配置。
4. 更新 `test_codex_runtime.py`。

### 12.3 阶段三：Claude 只读化

1. 修改 `ai_permission_mode` 默认值，禁用 `bypassPermissions`。
2. 移除 `Edit`、`Write`。
3. 添加只读 Bash 白名单。
4. 验证 Claude sandbox 配置注入方式。
5. 增加真实 runtime 权限验证测试或手工验证脚本。

### 12.4 阶段四：Skill 后端受控写盘

1. 修改 Skill 创建 prompt，要求 agent 只输出草案。
2. 删除或弱化“agent 直接落盘成功”的判断路径。
3. 保留并强化 `AI_PRD_SKILL_DRAFT` 解析。
4. 确认 `WorkspaceBrowserService.create_skill(...)` 只写 `settings.skills_root`。

### 12.5 阶段五：代码库审计兜底

1. 在 turn 开始前记录 `workspace/knowledge/code` Git 快照。
2. 在 turn 结束后执行检查。
3. 对 turn 前 clean、turn 后 tracked dirty 的 repo 执行回滚。
4. 新增 `workspace_guard` SSE 事件和 runtime event 持久化。

---

## 13. 验收标准

### 13.1 Codex

| 场景 | 预期 |
|---|---|
| 新会话启动 | cwd 为 `<project_root>/workspace` |
| 新会话启动 | sandbox 为 `read-only` |
| 命令参数 | 不包含 writable roots |
| 请求创建 Skill | agent 输出草案，后端写入 `workspace/.claude/skills` |
| 尝试写文件 | 失败 |

### 13.2 Claude Code

| 场景 | 预期 |
|---|---|
| 新会话启动 | cwd 为 `<project_root>/workspace` |
| 权限模式 | 不使用 `bypassPermissions` |
| 工具列表 | 不包含 `Edit`、`Write`、`MultiEdit` |
| `rg` / `git diff` / `git show` | 可用 |
| `touch` / `rm` / `curl` | 不可用 |
| 创建 Skill | agent 输出草案，后端写入 `workspace/.claude/skills` |

### 13.3 产品行为

| 场景 | 预期 |
|---|---|
| 需求评审 | 能读取需求文档和业务文档 |
| 代码影响分析 | 能搜索和阅读代码知识库 |
| Skill 浏览 | 从 `workspace/.claude/skills` 读取 |
| Skill 创建 | 成功后刷新 Skill 树 |
| 公共代码库异常改动 | 前端提示并记录审计事件 |

---

## 14. 风险与处理

| 风险 | 处理 |
|---|---|
| Claude sandbox 配置无法通过 SDK 传递 | 调研 settings 文件或 CLI 参数注入；无法可靠启用时，第一阶段禁用 Bash，仅保留 Read/Grep/Glob/LS |
| Claude 无法原生发现 `workspace/.claude/skills` | 将 cwd 固定为 `workspace`；如果仍不可用，则由应用层读取 Skill 并注入 system prompt |
| Bash pattern 约束不足 | 依赖 sandbox 作为最终边界；权限规则只作为工具层 allowlist |
| turn 后回滚误伤已有改动 | 只处理 turn 前 clean 的 repo；turn 前 dirty 不自动回滚 |
| `git clean` 删除未知文件风险高 | 第一阶段不自动清理 untracked 文件，只提示 |

---

## 15. 参考资料

- Claude Code Permissions: https://code.claude.com/docs/en/permissions
- Claude Code Sandboxing: https://code.claude.com/docs/en/sandboxing
- Claude Code Settings: https://code.claude.com/docs/en/settings
