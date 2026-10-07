# 用户、组织与 Agent Runtime 沙箱 Workspace 设计方案

## 1. 文档信息

- 项目名称：ai-prd
- 文档名称：用户、组织与 Agent Runtime 沙箱 Workspace 设计方案
- 文档版本：V2.2
- 文档日期：2026-08-17
- 文档状态：已实现

---

## 2. 本次修订结论

上一版实现把宿主机目录、runtime 工作目录、沙箱目录和提示词目录混在了一起，形成了一个半成品抽象：

```text
宿主机存储根：<project>/workspace
runtime session cwd：<project>/workspace/runtime/sessions/<session_id>/workspace
提示词声称：/workspace
历史快捷路径：/workspace/knowledge
实际组织知识库：<project>/workspace/coinex/knowledge
```

这会让 agent 误以为 `/workspace/knowledge` 是沙箱内真实路径，但 Claude Code 实际只在宿主机 session cwd 中运行；同时 session cwd 中的 `knowledge/*` 又是指向组织知识库的符号链接。沙箱解析符号链接后会看到真实宿主机目标路径，如果目标不在允许范围内，就会出现 `Path does not exist`、`Operation not permitted` 或 `allowed working directories` 相关错误。

V2.0 确认以下核心原则，V2.1 补充 Codex 按会话 workspace plan 动态选择 sandbox 的实现契约，V2.2 补充 Codex 可写挂载必须使用真实物理路径的约束：

1. 宿主机可以继续使用 `<project>/workspace` 作为后端存储根。
2. 沙箱内部不再暴露 `workspace` 这个宿主机存储概念。
3. 沙箱内部路径以 `/` 为 agent 可见根。
4. 组织知识库直接挂到 `/<organization_key>/knowledge`。
5. 用户个人空间固定挂到 `/me`。
6. 会话临时目录固定挂到 `/tmp`。
7. 提示词、前端展示、trace、citation 和 runtime 事件只能暴露沙箱路径，不能暴露宿主机路径。

最终目标路径示例：

```text
/coinex/knowledge      # 只读，来自 <project>/workspace/coinex/knowledge
/acme/knowledge      # 只读，来自 <project>/workspace/acme/knowledge
/me                    # 可写，来自 <project>/workspace/users/<user_id>
/tmp                   # 可写，来自 <project>/workspace/runtime/containers/<session_id>/tmp
```

`workspace` 和 `orgs` 都不是 agent 需要理解的业务目录名，不应出现在沙箱内路径中。

---

## 3. 当前问题复盘

### 3.1 最近会话暴露的问题

最近两个 Assistant 会话首轮没有正常完成，数据库中的 `assistant_turns.status` 被标记为 `failed`，但 `assistant_messages` 里已经写入了部分回复。用户侧看到的是“回答到一半停了”，追问后又能继续。

实际原因不是 Claude 速率限制，也不是知识库未同步，而是 runtime 文件系统视图不一致：

- `86exjtbq5` 等任务文件真实存在于 `workspace/coinex/knowledge/requirements`。
- runtime session 里通过符号链接暴露 `knowledge/requirements`。
- 普通 `find` 默认不跟随符号链接，会误判“没有文件”。
- Claude Code 沙箱只允许读 session cwd 的 `.`，符号链接目标解析到 `workspace/coinex/knowledge` 后可能不在允许范围内。
- 提示词提示 `/workspace/knowledge`，但 Claude SDK 实际 cwd 是宿主机 session workspace，不存在真实 `/workspace` 根。

### 3.2 后端状态问题

当前后端在 runtime 抛异常时，会把已经收集到的回复文本落成 assistant message，同时把 turn 标为 `failed`。这导致前端有一段可见回复，但系统状态是失败。

这层行为本身不是路径问题的根因，但会放大体验问题：runtime 已经产出 `complete/result` 后，如果后续 iterator 抛出工具命令失败，不能简单把整轮反转成失败。后续实现需要区分：

- 没有最终结果的 runtime 失败。
- 已经产生最终结果，但存在工具错误或尾部清理异常。
- runtime 明确返回失败。

### 3.3 不能继续依赖符号链接兜底

符号链接可以作为本地开发模拟手段，但不能成为权限和路径语义的核心方案。原因：

1. 沙箱通常按解析后的真实路径判断权限。
2. 搜索工具对符号链接遍历行为不一致。
3. prompt 中的路径和工具实际看到的路径可能不同。
4. citation 和前端展示容易泄漏宿主机路径。

---

## 4. 设计目标

1. 以 `RuntimeWorkspacePlan` 作为唯一事实来源，统一后端、前端、定时任务、agent runtime 和提示词的路径语义。
2. 支持一个会话挂载一个或多个用户有权访问的组织知识库。
3. 组织知识库默认只读，用户个人空间和会话临时目录默认可写。
4. agent 只看到沙箱路径，不看到宿主机路径。
5. 普通 Assistant 的 Claude Code / Codex / Mock runtime 都使用同一份 workspace plan；不挂载用户工作区的隔离后台任务可以不传 plan，但必须保持只读。
6. 不再保留 `/workspace/knowledge`、`/knowledge` 这类“当前组织快捷路径”。
7. 不再让 runtime client 自行根据用户或组织推断目录；授权解析必须在应用层完成。
8. 后端定时任务仍写宿主机真实目录，但对外输出必须映射成沙箱路径。

---

## 5. 非目标

- 不为了兼容旧的 `/workspace/knowledge` 提示词继续保留旧路径别名。
- 不允许用户请求直接拼接宿主机路径。
- 不允许 agent 修改组织知识库。
- 不在普通 Assistant 会话里隐式挂载所有组织；必须基于用户授权和本轮需求生成 plan。
- 不要求第一阶段必须完成真正容器化；可以先用宿主机 sandbox + path adapter 模拟同一契约。

---

## 6. 核心路径契约

### 6.1 宿主机目录

宿主机目录保持项目现有组织方式：

```text
<project>/
└── workspace/
    ├── coinex/
    │   ├── knowledge/
    │   │   ├── requirements/
    │   │   ├── business-docs/
    │   │   └── code/
    │   └── departments/
    ├── acme/
    │   ├── knowledge/
    │   └── departments/
    ├── users/
    │   └── <user_id>/
    └── runtime/
        ├── db/
        ├── sync-state/
        ├── sessions/
        └── containers/
            └── <session_id>/
                └── tmp/
```

说明：

- `workspace/<org_key>/knowledge` 是组织知识库，由同步任务、导入任务和后台受控流程更新。
- `workspace/users/<user_id>` 是用户全局个人工作区，不按组织强制拆分。
- `workspace/runtime` 是应用运行时数据，除会话级 `tmp` 外不挂给 agent。

### 6.2 沙箱内部目录

agent 看到的目录固定为：

```text
/
├── coinex/
│   └── knowledge/       # ro
├── acme/
│   └── knowledge/       # ro
├── me/                  # rw
└── tmp/                 # rw
```

如果某个用户只被授权访问 `coinex`，则沙箱里只出现 `/coinex/knowledge`、`/me` 和 `/tmp`。如果用户同时被授权访问 `coinex` 与 `acme`，且本轮 plan 确认需要两者，则可以同时出现 `/coinex/knowledge` 与 `/acme/knowledge`。

保留名：

```text
me
tmp
```

组织 key 不允许使用保留名，也不允许包含 `/`、`.`、`..` 或会导致路径歧义的字符。

### 6.3 路径映射示例

| 宿主机路径 | 沙箱路径 | 权限 |
|---|---|---|
| `<project>/workspace/coinex/knowledge` | `/coinex/knowledge` | read |
| `<project>/workspace/acme/knowledge` | `/acme/knowledge` | read |
| `<project>/workspace/users/<user_id>` | `/me` | write |
| `<project>/workspace/runtime/containers/<session_id>/tmp` | `/tmp` | write |

---

## 7. RuntimeWorkspacePlan

`RuntimeWorkspacePlan` 是后端为普通 Assistant 会话生成并传给 runtime 的唯一路径与权限事实来源。不挂载用户或组织目录的隔离后台任务可以不传 plan，此时 runtime 不得自行推断可写目录。

```python
@dataclass(slots=True)
class RuntimeWorkspaceMount:
    workspace_id: str
    workspace_key: str
    host_path: str
    sandbox_path: str
    permission: Literal["read", "write"]


@dataclass(slots=True)
class RuntimeWorkspacePlan:
    user_id: str
    session_id: str
    sandbox_cwd: str
    host_shadow_root: str
    mounts: list[RuntimeWorkspaceMount]
```

示例：

```json
{
  "user_id": "da5356c4-ceb9-49f8-86d1-5d4c43034598",
  "session_id": "f80e2bc0-773e-4ac2-b0eb-2ee4f1e7aa6c",
  "sandbox_cwd": "/",
  "host_shadow_root": "<project>/workspace/runtime/sessions/f80e2bc0-773e-4ac2-b0eb-2ee4f1e7aa6c/root",
  "mounts": [
    {
      "workspace_key": "org:coinex:knowledge",
      "host_path": "<project>/workspace/coinex/knowledge",
      "sandbox_path": "/coinex/knowledge",
      "permission": "read"
    },
    {
      "workspace_key": "user:da5356c4-ceb9-49f8-86d1-5d4c43034598",
      "host_path": "<project>/workspace/users/da5356c4-ceb9-49f8-86d1-5d4c43034598",
      "sandbox_path": "/me",
      "permission": "write"
    },
    {
      "workspace_key": "runtime:f80e2bc0-773e-4ac2-b0eb-2ee4f1e7aa6c:tmp",
      "host_path": "<project>/workspace/runtime/containers/f80e2bc0-773e-4ac2-b0eb-2ee4f1e7aa6c/tmp",
      "sandbox_path": "/tmp",
      "permission": "write"
    }
  ]
}
```

`host_shadow_root` 是本地开发或非容器 runtime 的实现细节。它可以用于构造一个宿主机上的模拟根目录，但不得出现在提示词、前端展示和最终回答中。

---

## 8. Claude Code 沙箱能力边界

Claude Code 沙箱可以配置文件系统允许范围，例如 `allowRead`。但需要明确：

1. `allowRead` / `allowWrite` 是宿主机权限边界，不是业务路径映射。
2. Claude Code 不会自动把多个宿主机路径变成沙箱内的 `/coinex/knowledge`、`/me`。
3. 如果使用符号链接模拟挂载，沙箱可能按解析后的真实路径做权限判断。
4. 所以 runtime adapter 必须同时处理：
   - 宿主机权限 allow list。
   - 沙箱路径到宿主机路径的映射。
   - runtime 事件、工具路径和 citation 的反向映射。

第一阶段如果暂不启用真正容器，Claude Code adapter 至少要做到：

```text
allowRead:
  - <project>/workspace/coinex/knowledge
  - <project>/workspace/acme/knowledge
  - <project>/workspace/users/<user_id>
  - <project>/workspace/runtime/containers/<session_id>/tmp

allowWrite:
  - <project>/workspace/users/<user_id>
  - <project>/workspace/runtime/containers/<session_id>/tmp
```

同时，提示词中只能描述：

```text
/coinex/knowledge
/acme/knowledge
/me
/tmp
```

不得提示 agent 使用宿主机路径，也不得提示使用旧的 `/workspace` 路径。

---

## 9. 各层职责

### 9.1 后端应用层

后端应用层负责：

- 根据用户 membership、组织授权和会话请求生成 `RuntimeWorkspacePlan`。
- 校验所有 `host_path` 必须位于项目允许的 `workspace` 根下。
- 初始化 `/me` 和 `/tmp` 对应宿主机目录。
- 持久化本轮使用的 workspace plan 摘要。
- 将 runtime 事件中的宿主机路径映射回沙箱路径。
- 对前端、trace、citation 输出沙箱路径。

后端应用层不允许：

- 把用户输入直接拼成宿主机路径。
- 让 runtime client 自行推断组织目录。
- 对外暴露 `host_path`。

### 9.2 定时任务与同步任务

定时任务仍写宿主机真实目录：

```text
workspace/coinex/knowledge/requirements
workspace/acme/knowledge/requirements
```

但同步状态、索引和对外 API 必须记录可映射的组织 key，并能输出沙箱路径：

```text
/coinex/knowledge/requirements/tasks/...
/acme/knowledge/requirements/tasks/...
```

不能再出现“物理路径在 `workspace/coinex`，展示路径仍写 `workspace/knowledge`”的混合状态。

### 9.3 Agent Runtime

普通 Assistant 的 runtime client 接收 `RuntimeWorkspacePlan`：

```text
create_or_resume_session(workspace_plan=..., system_prompt=...)
```

runtime 不得通过 `working_directory` 推导用户、组织或知识库路径。代码评审、需求评审、业务文档提案和 Skill 导入分析等不挂载用户可写目录的隔离任务，可以只提供 `working_directory` 或空 mounts plan；这类任务的有效 sandbox 必须为只读。

Codex 默认配置为 `AI_CODEX_SANDBOX=auto`，每个会话按有效写挂载生成 thread 和 turn 的权限：

| 会话条件 | Thread sandbox | Turn sandbox policy | 可写目录 |
|---|---|---|---|
| plan 包含 `/me` 或 `/tmp` 的 `write` 挂载 | `workspace-write` | `workspaceWrite` | 仅 plan 中实际存在的 `/me`、`/tmp` |
| plan 没有写挂载 | `read-only` | `readOnly` | 无 |
| 未提供 plan 的隔离后台任务 | `read-only` | `readOnly` | 无 |
| 显式配置 `AI_CODEX_SANDBOX=read-only` | `read-only` | `readOnly` | 无，即使 plan 声明写挂载也不开放 |

`auto` 不接受 plan 中其他路径的 `write` 声明；`/repos`、`/attachments` 和 `/<org_key>/knowledge` 始终不得因此获得写权限。网络访问在 `readOnly` 与 `workspaceWrite` 下均保持关闭。`thread/start` / `thread/resume` 的 sandbox 与 `turn/start.sandboxPolicy` 必须由同一份有效写目录计算，不能出现 thread 只读、turn 可写或相反的矛盾。

`AI_CODEX_SANDBOX` 的取值约束如下：

| 配置值 | 用途 |
|---|---|
| `auto` | 默认且推荐；普通 Assistant 按 plan 动态授权，无有效写挂载时自动只读 |
| `read-only` | 强制所有 Codex 会话只读，用于需要收紧权限的环境 |
| `workspace-write` | 显式启用 scoped write；仍只接受解析后的有效 writable roots，不得作为扩大生产权限的默认方案 |
| `danger-full-access` | 关闭文件系统 sandbox，仅允许受控诊断，生产环境禁止使用 |

### 可写挂载的物理路径约束

Agent 可见路径始终使用 runtime workspace 中的逻辑路径，例如 `me`、`tmp`；
提示词、引用和前端展示不得暴露宿主机路径。

但传给 Codex `workspaceWrite.writableRoots` 的路径必须使用
`RuntimeWorkspaceMount.host_path` 的真实解析路径，不能使用
`host_shadow_root / sandbox_path` 的符号链接别名。

原因：Codex 会识别 `.agents`、`.codex` 等内部目录并施加只读限制。
若这些目录位于可写符号链接下，Bubblewrap 会拒绝构建沙箱。

### 9.4 提示词

提示词只描述沙箱路径：

```text
你运行在 ai-prd 的 Agent Runtime 沙箱中。
当前可见根目录为 `/`。
可读取的组织知识库：
- `/coinex/knowledge`：只读
- `/acme/knowledge`：只读
当前用户个人工作区：
- `/me`：可写
当前会话临时目录：
- `/tmp`：可写
回答中引用文件时使用上述沙箱路径，不要输出宿主机路径。
```

提示词不得出现：

```text
<project>/workspace/...
/workspace/knowledge
workspace/knowledge
host_agent_cwd
```

### 9.5 前端

前端展示 runtime workspace 时使用沙箱路径和组织名：

```text
CoinEx 知识库: /coinex/knowledge
Acme 知识库: /acme/knowledge
我的空间: /me
临时目录: /tmp
```

前端不展示宿主机绝对路径。会话详情、运行轨迹、引用文件和错误提示都使用同一套沙箱路径。

---

## 10. 数据模型调整

### 10.1 organizations

组织仍然是顶层业务边界：

```sql
CREATE TABLE orgs (
    org_id TEXT PRIMARY KEY,
    org_key TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

`org_key` 同时参与沙箱路径命名，因此必须通过更严格校验。

### 10.2 workspaces

`workspaces.default_mount_path` 应改名或语义调整为 `default_sandbox_path`：

```sql
CREATE TABLE workspaces (
    workspace_id TEXT PRIMARY KEY,
    org_id TEXT,
    workspace_key TEXT NOT NULL UNIQUE,
    workspace_type TEXT NOT NULL,
    display_name TEXT NOT NULL,
    host_path TEXT NOT NULL,
    default_sandbox_path TEXT NOT NULL,
    default_permission TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

示例：

| workspace_key | host_path | default_sandbox_path | permission |
|---|---|---|---|
| `org:coinex:knowledge` | `workspace/coinex/knowledge` | `/coinex/knowledge` | read |
| `org:acme:knowledge` | `workspace/acme/knowledge` | `/acme/knowledge` | read |
| `user:<user_id>` | `workspace/users/<user_id>` | `/me` | write |

### 10.3 assistant_sessions

会话需要记录本轮 plan 摘要：

```sql
ALTER TABLE assistant_sessions ADD COLUMN runtime_workspace_profile_json TEXT;
```

如果产品仍需要默认组织选择，可以保留 `active_organization_key`，但它不再表示“本轮只允许挂载一个组织”。它只表示默认业务组织或会话主组织。实际可见组织以 `runtime_workspace_profile_json.mounts` 为准。

---

## 11. 重构方案

### 11.1 阶段一：文档与路径契约冻结

1. 以本文档为准冻结沙箱路径契约。
2. 标记旧路径废弃：
   - `/workspace/knowledge`
   - `workspace/knowledge`
   - `/knowledge`
3. 梳理所有后端、前端、prompt、测试中出现的旧路径。

验收：

- 文档明确宿主机路径和沙箱路径的区别。
- 新代码不得新增旧路径引用。

### 11.2 阶段二：RuntimeWorkspacePlan 改造

1. 将 `mount_path` 语义改为 `sandbox_path`。
2. `WorkspaceAccessService` 输出 `/<org_key>/knowledge`、`/me`、`/tmp`。
3. `runtime_workspace_profile_json` 不再暴露 `host_path` 给前端。
4. 增加组织 key 保留名和字符校验。

验收：

- 用户有 `coinex` 授权时，plan 包含 `/coinex/knowledge`。
- 用户有 `acme` 授权时，plan 包含 `/acme/knowledge`。
- `/me` 指向当前用户个人目录。
- `/tmp` 指向当前会话临时目录。

### 11.3 阶段三：Claude Code / Codex adapter 改造（已实现）

1. 普通 Assistant runtime 以 `workspace_plan` 作为路径和权限事实来源。
2. 不挂载用户工作区的隔离后台任务保持 `readOnly`，不得从全局配置继承宽泛写权限。
3. Claude Code sandbox allow list 从 plan 生成。
4. Codex 在 `auto` 模式下仅从 plan 中提取 `/me`、`/tmp` 的 `write` 挂载；没有有效写挂载时使用 `readOnly`。
5. Codex thread sandbox 与 turn sandbox policy 使用同一份有效写目录生成。
6. 本地开发模式如需 shadow root，必须由 adapter 维护路径映射，不向 prompt 泄漏。
7. runtime 事件、工具读取路径和错误消息统一映射为沙箱路径。

验收：

- Claude Code 能读取 `/coinex/knowledge` 对应内容。
- Claude Code 不能写组织知识库。
- Claude Code 能写 `/me` 和 `/tmp`。
- Codex 普通 Assistant 只能写 `/me` 和 `/tmp`，不能写其他挂载。
- Codex 无写挂载会话和隔离后台任务保持 `readOnly`。
- 错误消息不出现宿主机路径。

### 11.4 阶段四：提示词与知识定位改造

1. 系统提示改为沙箱根 `/`。
2. ClickUp 定位提示改成：

```text
在 `/<org_key>/knowledge/requirements/tasks/` 搜索 `Task ID:`。
在 `/<org_key>/knowledge/requirements/docs/` 搜索 `Page ID:`。
```

3. 多组织可见时，提示词列出所有可见组织知识库。
4. 移除“当前组织 knowledge 位于 `knowledge/`”这类相对快捷表达。

验收：

- 新会话首轮不会再尝试访问 `/workspace/knowledge`。
- 回答引用路径为 `/coinex/knowledge/...` 或 `/me/...`。

### 11.5 阶段五：前端、API 与定时任务统一

1. workspace API 返回沙箱路径字段。
2. 前端会话详情、引用、trace、文件树统一展示沙箱路径。
3. 同步任务记录组织 key，并能从宿主机路径映射到沙箱路径。
4. `requirements.json` 等 sync-state 不再只存无法区分组织的旧展示路径。

验收：

- 用户在前端看到的路径和 agent 回答中的路径一致。
- 同一个文件不会一处显示 `workspace/coinex/...`，另一处显示 `workspace/knowledge/...`。

### 11.6 阶段六：失败状态处理

1. runtime 已返回最终 `complete/result` 后，后续非关键异常不应把 turn 标为 `failed`。
2. 工具错误应作为 runtime event 留痕，但最终状态取决于是否有可用最终回答。
3. 对真正无结果的 runtime 异常继续标记 failed。

验收：

- 有最终回答的 turn 状态为 `completed`，并保留工具错误事件。
- 无最终回答的 turn 状态为 `failed`。
- 前端不再出现“有回答但 latest_turn failed”的矛盾状态。

---

## 12. 安全边界

### 12.1 路径校验

所有 host path 必须满足：

1. 已在 workspace 数据模型中登记。
2. resolve 后位于 `<project>/workspace` 允许根下。
3. 组织知识库只能位于 `<project>/workspace/<org_key>/knowledge`。
4. 用户个人目录只能位于 `<project>/workspace/users/<user_id>`。
5. 临时目录只能位于 `<project>/workspace/runtime/containers/<session_id>/tmp`。
6. 不允许符号链接逃逸到允许根外。

### 12.2 权限边界

| 沙箱路径 | 权限 | 说明 |
|---|---|---|
| `/<org_key>/knowledge` | read | 组织知识库 |
| `/me` | write | 当前用户个人工作区 |
| `/tmp` | write | 当前会话临时目录 |

`workspace/runtime/db`、`workspace/runtime/sync-state`、`workspace/runtime/sessions` 不挂给 agent。

---

## 13. 验收标准

| 场景 | 预期 |
|---|---|
| CoinEx 用户提问 | agent 使用 `/coinex/knowledge` |
| Acme 用户提问 | agent 使用 `/acme/knowledge` |
| 用户同时可访问 CoinEx 与 Acme | plan 可同时挂载 `/coinex/knowledge` 与 `/acme/knowledge` |
| 用户个人产物 | 写入 `/me`，宿主机落在 `workspace/users/<user_id>` |
| 会话临时文件 | 写入 `/tmp`，宿主机落在当前 session tmp |
| 组织知识库 | 可读不可写 |
| Codex 普通 Assistant | 仅 plan 中的 `/me`、`/tmp` 写挂载触发 `workspaceWrite` |
| Codex 无写挂载或无 plan 任务 | thread 与 turn 均为 `readOnly` |
| Codex 显式全局只读 | `AI_CODEX_SANDBOX=read-only` 覆盖 plan 的写挂载 |
| 提示词 | 不出现宿主机路径，不出现 `/workspace` |
| 前端路径 | 与 agent 回答路径一致 |
| citation | 使用沙箱路径 |
| runtime trace | 默认使用沙箱路径，调试模式才允许管理员查看宿主机路径 |
| 首轮会话 | 不因 `/workspace/knowledge` 或符号链接遍历失败而中断 |
| 有最终回答但工具报错 | turn 不应被简单标记为 failed |

---

## 14. 废弃项

以下路径或语义废弃，后续不得新增依赖：

```text
/workspace
/workspace/knowledge
/workspace/me
/workspace/tmp
workspace/knowledge
knowledge/requirements 作为跨组织快捷路径
```

以下实现方式废弃：

1. 让 prompt 声称存在 `/workspace`，但 runtime 实际运行在宿主机 session cwd。
2. 只靠 session workspace 内的符号链接表达挂载。
3. runtime client 根据 `working_directory` 自行推断 workspace 结构。
4. 前端、trace、citation 暴露宿主机路径。
5. 通过旧路径兼容层掩盖新旧路径不一致。
