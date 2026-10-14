# AI Assistant Git Turn 上下文与 Worktree 设计

## 1. 文档状态

- 状态：已确认，作为后续实现依据。
- 适用范围：AI Assistant 日常代码问答、Task→PR→AI Assistant、指定分支分析、历史 Code Review 复现。
- 当前实现范围：统一 Git worktree 管理、Turn 内按需代码挂载、PR 启动会话、动态工具与页面入口。

## 2. 背景

当前系统存在两条彼此独立的代码读取路径：

1. 日常 AI Assistant 通过组织知识库读取默认分支代码：

   ```text
   /{organization_key}/knowledge/code/{repo}
   ```

2. PR Code Review 在宿主机的以下目录准备 PR worktree 和上下文：

   ```text
   workspace/runtime/code-review-worktrees/{owner}/{repo}/PR_{pr_number}
   workspace/runtime/code-review-contexts/{owner}/{repo}/PR_{pr_number}
   ```

Task 与 PR 的关联已经由 GitHub PR 轮询建立。新的目标是：

- Task 详情中的关联 PR 可以启动 AI Assistant。
- Agent 能读取该 PR 的完整代码、描述、diff、变更文件和关联需求。
- 复用现有 PR worktree 能力，不再实现第二套 Git 下载机制。
- 日常对话继续直接使用 main/master 代码镜像，不为所有会话引入 worktree。
- Agent 可以根据用户问题自主判断是否需要挂载某个受控仓库的 PR 或分支。
- 挂载成功后，当前 Turn 的代码快照与 Runtime 逻辑挂载立即更新。

## 3. 核心结论

代码上下文策略最终绑定的是 **Turn**，不是整个 workspace，也不是 Session。

每个新用户 Turn 一律从组织知识库的 `baseline` 开始。Session 只保存入口关联的
PR/分支提示；Agent 根据当前用户输入决定是否调用受控工具挂载一个或多个仓库的
精确 PR/分支。每次挂载成功后，后端更新该 Turn 的快照和 Runtime 挂载。

```text
新 Turn：baseline
    ↓
Agent 判断是否需要精确版本
    ↓
mount_git_ref(受控仓库, PR/分支)
    ↓
Turn 快照 + RuntimeWorkspacePlan 原子更新
```

系统必须同时区分三层状态：

```text
物理层：revision/{sha}                 不可变
会话层：入口关联的 selector 提示          可调整
轮次层：当前已挂载仓库与 resolved_sha     工具成功后更新
```

## 4. 三种 Turn 策略

### 4.1 baseline

`baseline` 是日常对话的默认策略。

适用场景：

- 普通知识问答。
- 业务逻辑查询。
- 未指定 PR、功能分支或历史版本的代码分析。

行为：

- 直接读取组织知识库中的默认分支镜像：

  ```text
  /{organization_key}/knowledge/code/{repo}
  ```

- 仓库应保持在配置的 `main` 或 `master` 分支。
- 不创建额外 worktree。
- 不增加精确 Git 逻辑挂载。
- 不在每轮开始前访问 GitHub。
- 不保证单个 Turn 固定 SHA。
- 后台同步失败时继续使用当前本地镜像，不阻断日常对话。

现有 `code_sync_loop` 继续负责定时对知识库代码镜像执行 `git pull`。

代码同步前应校验当前分支与仓库配置的默认分支一致。如果被人为切换到其他分支，应记录错误并跳过，不允许后台任务自动切换公共代码镜像的分支。

### 4.2 follow

`follow` 用于 Agent 在当前 Turn 中按需挂载指定 PR 或远程分支的最新版本。

适用场景：

- Task→PR→AI Assistant 的入口提示。
- 用户在当前 Turn 明确引用某个 PR 或功能分支。
- Agent 根据问题推断需要读取的受控仓库 PR 或分支。

行为：

- 仅在 Agent 调用 `mount_git_ref` 时解析 PR `head_sha` 或远程分支 tip。
- 如果 SHA 未变化，复用已有不可变 worktree。
- 如果 SHA 发生变化，创建或复用新 SHA 对应的 worktree。
- 为当前 Turn 原子更新 Runtime 逻辑挂载和持久化快照。
- 同一仓库再次调用工具时可以在当前 Turn 切换到新的 PR/分支；未再次调用时保持已解析 SHA。
- 不同仓库可以在同一 Turn 分别挂载，互不覆盖。
- SHA 更新时明确通知 Agent：之前的结论可能基于旧版本。
- 无法确认远程最新版本时本次工具调用失败，不静默使用旧 SHA。

### 4.3 pinned

`pinned` 用于固定已经由服务端验证过的 Git revision。

适用场景：

- 历史 Code Review。
- 问题复现。
- 失败 Turn 重跑。
- 需要稳定证据链的分析。

行为：

- 固定使用记录中的 `resolved_sha`。
- 不访问远程检查更新。
- 用户和 Agent 都不能直接输入任意 SHA。
- SHA 必须来自已授权 PR、分支解析结果或历史 Turn 快照。
- 失败 Turn 重跑必须复用原 Turn 快照，不能重新执行 `follow`。

### 4.4 策略对比

| 策略 | 代码来源 | 每轮远程检查 | Turn 固定 SHA | 远程失败处理 |
| --- | --- | --- | --- | --- |
| `baseline` | 组织知识库默认分支镜像 | 否 | 不保证 | 使用现有镜像 |
| `follow` | PR 或远程分支 worktree | 调用工具时 | 直到同仓库再次挂载 | 工具调用失败 |
| `pinned` | 固定 revision worktree | 否 | 是 | 不受影响 |

## 5. Session 入口提示与 Turn Scope

### 5.1 Session 入口提示

普通 Session 默认不保存精确 Git selector，等价于：

```text
strategy: baseline
```

通过 Task→PR 创建的 Session 保存以下入口关联信息：

```text
repo_full_name: example-org/example_backend
default_strategy: follow
selector_type: pull_request
selector_value: 8784
last_resolved_sha: sha_A
```

从历史 Code Review 创建的 Session 可以默认保存：

```text
default_strategy: pinned
selector_type: pull_request
selector_value: 8784
last_resolved_sha: sha_A
```

字段名沿用 `session_git_context_defaults`，但其运行语义是提示而不是自动挂载。
`last_resolved_sha` 只用于复用、更新比较和提示，不是新 Turn 的初始执行依据。

### 5.2 Turn 动态 Scope

当前用户消息可以提到：

- 使用某个 PR。
- 使用某个分支。
- 使用某个历史 Review 的固定 revision。

客户端不解析或提交结构化 Git Context。Agent 结合用户消息与 Session 入口提示决定是否调用工具。

```text
Turn 开始：baseline
  → Agent 不调用工具：保持 baseline
  → Agent 调用工具：增加或替换对应仓库的精确 scope
```

示例：

```text
普通 Session 默认：baseline

Turn 1：普通业务问题
→ baseline

Turn 2：用户引用 PR #8784，Agent 调用工具
→ follow PR #8784，并更新当前 Turn 快照

Turn 3：未继续引用 PR
→ baseline
```

```text
PR 入口 Session 提示：PR #8784

Turn 1：先 baseline；Agent 判断需要 PR 后挂载 sha_A
Turn 2：先 baseline；Agent 再次判断需要 PR，工具解析并挂载 sha_B
Turn 3：问题与 PR 无关，Agent 不调用工具
→ baseline
```

同一个 Turn 可挂载一个或多个仓库。挂载只影响当前 Turn，不能把一次工具调用自动永久写入
Session。后续 Turn 仍从 baseline 开始。

## 6. 数据模型

建议区分以下三类记录。

### 6.1 session_git_context_defaults

保存特定入口或用户设置产生的 Session 默认值。

| 字段 | 说明 |
| --- | --- |
| `default_id` | 主键 |
| `session_id` | Assistant Session |
| `user_id` | 所属用户 |
| `repo_full_name` | 受控仓库 |
| `default_strategy` | `follow` 或 `pinned`；没有记录即 baseline |
| `selector_type` | `pull_request` 或 `branch` |
| `selector_value` | PR number 或分支名 |
| `last_resolved_sha` | 上次解析到的 SHA |
| `logical_path` | 服务端生成的逻辑路径 |
| `authorization_source` | `task_pr`、`review` 或 `system` |
| `authorization_key` | Task ID、Review ID 等授权来源 |
| `last_checked_at` | 上次检查时间 |
| `created_at` / `updated_at` | 时间戳 |

不得保存由客户端传入的宿主机 worktree 路径。

### 6.2 turn_git_context_requests

保存 Agent 在运行中的 Turn 调用受控工具产生的代码上下文请求。

| 字段 | 说明 |
| --- | --- |
| `request_id` | 主键 |
| `turn_id` | Assistant Turn |
| `repo_full_name` | 受控仓库 |
| `strategy` | 动态挂载为 `follow`；失败重跑为 `pinned` |
| `selector_type` | 可空 |
| `selector_value` | 可空 |
| `authorization_source` | `agent_tool` 或失败重跑的服务端来源 |
| `authorization_key` | 授权依据 |
| `created_at` | 时间戳 |

### 6.3 turn_git_context_snapshots

保存该 Turn 当前真正交给 Runtime 的精确 Git 结果。没有记录即表示 baseline。

| 字段 | 说明 |
| --- | --- |
| `snapshot_id` | 主键 |
| `turn_id` | Assistant Turn |
| `repo_full_name` | 仓库 |
| `strategy` | 最终策略 |
| `selector_type` | 可空 |
| `selector_value` | 可空 |
| `resolved_sha` | baseline 可记录本地 HEAD，follow/pinned 必须存在 |
| `logical_code_path` | Agent 可见代码路径 |
| `logical_context_path` | Agent 可见 PR 上下文路径，可空 |
| `source` | `turn_explicit` 或失败重跑来源 |
| `resolved_at` | 解析时间 |

同一 Turn、同一仓库只有一条当前快照；同仓库再次挂载时原子更新，多仓库分别保留。
Turn 快照是审计、失败重跑、版本提示和后续清理的权威依据。

## 7. 统一 Git Worktree 管理

现有 `CodeReviewContextPreparer` 同时负责 Git checkout、PR 上下文生成和 Review prompt 上下文组织，需要拆分。

新增统一的 `GitWorktreeManager`，由 Code Review 和 AI Assistant 共用。

### 7.1 允许的输入

```text
repository: example-org/example_backend
selector:
  kind: pull_request | branch
  value: 8784 | feature/xxx
```

服务端从现有受控仓库配置解析：

- `repo_full_name`
- `workspace_repo_path`
- `origin`
- `default_branch`
- `organization_key`
- GitHub 访问方式

### 7.2 禁止的输入

客户端和 Agent 都不能提供：

- 任意本地目录。
- 任意远程 URL。
- 任意 remote 名称。
- 任意 refspec。
- 任意 commit SHA。
- 任意宿主机挂载路径。
- 任意 Agent 逻辑挂载目标。

### 7.3 revision 路径

物理 worktree 以 resolved SHA 为唯一 revision：

```text
workspace/runtime/code-review-worktrees/
  {owner}/
    {repo}/
      revisions/
        {full_sha}/
```

同一个 SHA 只保留一份 worktree。PR 和分支只是指向 revision 的逻辑 selector：

```text
PR #8784 ─────┐
              ├── revision/226d733...
feature/x ────┘
```

已有 `PR_{number}` 目录不再作为新的权威路径；新实现复用现有根目录，但不保留可变 PR 目录语义。

### 7.4 ensure 流程

1. 校验仓库在当前组织允许的仓库配置中。
2. 校验 selector：
   - PR number 必须为正整数。
   - 分支名必须通过 `git check-ref-format --branch`。
3. 解析最新目标 SHA：
   - PR：重新读取 GitHub PR metadata 获取 `head_sha`。
   - Branch：只从配置的 `origin` 获取远程 branch tip。
4. 对 `repo_full_name + resolved_sha` 获取跨任务锁。
5. 如果 revision worktree 已存在：
   - 校验它是受控 worktree。
   - 校验 `HEAD == resolved_sha`。
   - 校验工作区干净。
   - 全部符合则直接复用。
6. 如果不存在，使用源仓库创建 detached worktree。
7. 返回服务端生成的 `PreparedGitWorktree`，不向上层暴露可修改宿主机路径。

### 7.5 不可变保证

- revision worktree 创建后不允许切分支。
- 不允许对其执行 pull、merge、reset 或 checkout。
- Agent 只能获得只读访问。
- 同一 SHA 的目录如果出现 HEAD 不匹配或 dirty，应视为受控目录损坏并重建，不能继续使用。

## 8. PR Context 生成

PR Context 生成器调用 `GitWorktreeManager`，自身只负责生成 PR 资料。

建议路径：

```text
workspace/runtime/code-review-contexts/
  {owner}/
    {repo}/
      PR_{pr_number}/
        {head_sha}/
          pull-request.md
          diff.patch
          changed-files.txt
          requirements/
```

内容：

| 文件 | 说明 |
| --- | --- |
| `pull-request.md` | PR 标题、URL、作者、状态、base/head、描述 |
| `diff.patch` | `base_sha...head_sha` 完整 diff |
| `changed-files.txt` | name-status 变更列表 |
| `requirements/` | 从 PR 描述解析出的本地需求文档副本与索引 |

Code Review 和 AI Assistant 共用同一份 revision worktree 与 PR Context。

## 9. Turn 创建与运行中解析

新的执行顺序：

1. 校验 Session 权限，并确认没有运行中的 Turn。
2. 创建没有精确 Git 快照的 baseline Turn。
3. 生成仅包含组织知识库、用户空间等基础挂载的 `RuntimeWorkspacePlan`。
4. 将 Session 的 Task/PR 关联作为提示写入 Runtime message。
5. 创建或恢复 Runtime Session，并注册 `mount_git_ref`。
6. Agent 判断问题是否需要精确 PR/分支。
7. 每次工具调用由后端校验受控仓库和 selector，并解析最新 SHA。
8. 完整准备 revision worktree 与可选 PR Context。
9. 原子物化新的逻辑挂载；失败则保持调用前的 Runtime plan。
10. 在事务中追加 Turn 请求并更新该仓库的 Turn 快照。
11. 通知 Agent 与页面当前 Turn scope 已更新，然后继续执行。

失败 Turn 重跑是唯一例外：它在 Runtime 启动前按原 Turn 快照准备 `pinned` 挂载，
以保证重跑证据一致。

## 10. Runtime 逻辑挂载

对于当前 Turn 的精确代码上下文，Agent 使用稳定逻辑路径：

```text
/repos/example_backend/code
/repos/example_backend/context
```

同一个 Session 的不同 Turn 可以使用相同逻辑路径，但目标 revision 可以不同：

```text
Turn A:
/repos/example_backend/code → revision/sha_A

Turn B:
/repos/example_backend/code → revision/sha_B
```

逻辑路径只由服务端生成。

### 10.1 挂载优先级提示

组织知识库中的 baseline 代码仍然可见：

```text
/coinex/knowledge/code/example_backend
```

当本 Turn 存在精确 Git Context 时，system/runtime prompt 必须明确：

- `/repos/example_backend/code` 是本轮主要代码依据。
- `/coinex/knowledge/code/example_backend` 是默认分支镜像，不能代替本轮 PR/分支代码。
- Agent 应优先读取精确挂载路径。

### 10.2 原子切换

不能使用“删除旧链接，再创建新链接”的方式。

正确流程：

1. 先准备新 revision 和 context。
2. 在逻辑挂载同级创建临时链接。
3. 使用原子替换更新目标。
4. 成功后让新的 Runtime plan 生效。
5. 任一步失败都不能留下空路径或半更新状态。

单个 Turn 只允许通过 `mount_git_ref` 切换逻辑挂载。切换成功后，当前 Turn 快照同步更新；
Agent、客户端和普通命令不能绕过该工具修改挂载。

## 11. 版本更新通知

当工具解析到同一 Session、同一 selector 从 `sha_A` 更新为 `sha_B` 时：

1. 持久化 Session 时间线事件。
2. 在 Runtime message 中注入：

   ```text
   代码上下文已更新：
   - example-org/example_backend PR #8784
   - 原版本：sha_A
   - 当前版本：sha_B

   此前会话结论可能基于旧版本。本轮必须重新读取相关代码和 diff 后再回答。
   ```

3. 不能伪造为用户消息。
4. 不能只在前端展示而不通知 Agent。

旧 SHA 的来源应取 Session 入口提示或最近一次有效 Turn 快照，而不是依赖可变目录状态。

## 12. 失败与重跑

### 12.1 baseline

- 不做每轮远程检查。
- 后台同步失败不阻断 Turn。
- 使用当前本地知识库镜像。

### 12.2 follow

- 无法读取 PR metadata、无法获取远程分支 tip、无法准备 worktree 或无法生成 context 时，
  本次工具调用直接失败。
- 不允许静默使用 `last_resolved_sha`。
- Agent 必须说明精确 scope 获取失败，不能伪装成已经挂载。

### 12.3 pinned

- 不依赖远程检查。
- 本地 revision 不存在或损坏时，可以从已验证的受控源仓库恢复同一 SHA。
- 无法恢复时直接失败。

### 12.4 失败 Turn 重跑

重跑必须直接读取原 `turn_git_context_snapshots`：

- 不重新套用 Session 默认值。
- 不重新解析 follow。
- 不自动升级到新 SHA。

否则重跑结果不再与原失败 Turn 可比。

## 13. Task→PR→AI Assistant

### 13.1 后端接口

建议新增：

```http
POST /api/my-tasks/{task_id}/pull-requests/assistant-session
```

请求：

```json
{
  "repo_full_name": "example-org/example_backend",
  "pr_number": 8784
}
```

处理：

1. 校验当前用户与 Task 的负责人、提出人、测试人员等关系。
2. 校验该 PR 确实关联当前 Task。
3. 校验仓库属于当前组织的受控仓库。
4. 获取最新 PR metadata。
5. 通过 `GitWorktreeManager` 准备 revision。
6. 生成 PR Context。
7. 创建 AI Assistant Session。
8. 为 Session 写入 `follow PR` 入口提示。
9. 返回 Session ID 和当前 resolved SHA。

准备失败时不创建空 Session。

### 13.2 页面行为

Task 详情中，关联 PR 提供“使用 AI 分析”按钮：

1. 点击后进入准备状态。
2. 调用创建接口。
3. 成功后跳转：

   ```text
   /assistant?sessionId={session_id}
   ```

4. 新 Session 显示该 PR 作为入口关联；每个 Turn 仍从 baseline 开始。
5. 页面不需要自行拼接宿主机路径或 Git metadata。

页面工作必须在后端与 Runtime 链路完成后再接入，避免与其他 Task 页面修改互相覆盖。

## 14. Agent 动态挂载工具

当前实现提供：

```text
mount_git_ref(repository, reference_type, reference)
```

- `repository` 只能解析为当前组织受控仓库配置中的 `owner/repo` 或唯一短名称。
- `reference_type` 只能是 `pull_request` 或 `branch`。
- `reference` 只能是 PR number 或合法远程分支名。
- 物理路径、remote、refspec、SHA 和逻辑挂载目标全部由后端生成。

### 14.1 权限规则

后端必须校验：

- 当前用户仍拥有相应组织权限。
- 仓库仍在受控仓库配置中。
- PR 或分支 selector 格式合法，并由配置中的 GitHub/remote 解析。

Agent 可以传入受控仓库名、PR number 或分支名；不能传入 remote、refspec、SHA、
本地路径或挂载目标。

### 14.2 Turn 边界

工具成功后直接修改当前 Turn 的精确快照：

1. 解析并准备不可变 revision。
2. 原子更新 Runtime 逻辑挂载。
3. 追加工具请求审计记录。
4. upsert 当前 Turn 对应仓库快照。
5. 向 Agent 返回逻辑路径和 resolved SHA。
6. 向页面发送 `git_scope` 事件。

没有工具调用时保持 baseline。失败 Turn 重跑期间不提供该工具，避免改变原快照。

## 15. 并发与安全

### 15.1 并发

- Session 现有“一次只能运行一个 Turn”的规则继续生效。
- worktree 准备需要按 `repo_full_name + resolved_sha` 加跨任务锁。
- 并发请求同一 SHA 时只允许一个创建者，其他请求等待并复用结果。
- context 生成需要按 `repo_full_name + pr_number + head_sha` 加锁。

### 15.2 路径安全

- owner、repo 必须通过严格字符校验。
- SHA 必须来自服务端 Git 解析结果并满足完整 commit 格式。
- 所有物理路径必须在受控 worktree/context 根目录下。
- 删除或重建前必须执行 `relative_to(root)` 校验。
- Agent 只看到逻辑路径，不看到宿主机绝对路径。

### 15.3 Git 安全

- 只允许配置中的源仓库和 remote。
- 分支名必须通过 Git ref 校验。
- 不允许任意 refspec、URL 或 local path。
- revision worktree 只读。
- 不自动初始化未知 submodule 或访问额外 remote。

## 16. Worktree 清理

第一阶段不自动清理旧 revision。

未来只有满足以下全部条件时才进入清理候选：

- 没有 Session 默认值引用。
- 没有 pinned 上下文引用。
- 没有运行中的 Turn 使用。
- 没有 Code Review 任务或历史复现记录要求保留。
- 超过配置的保留期限。

清理依据是数据库引用关系和最后使用时间，不根据目录名或当前 PR 状态猜测。

## 17. 建议模块边界

```text
business/git_context/
  repository_catalog.py     # 受控仓库与组织授权
  worktree_manager.py       # selector 解析、revision worktree
  models.py                 # Git selector、revision、prepared worktree

business/assistant/
  git_context.py            # Session 入口提示、Turn 请求、Turn 快照解析

business/code_reviews/
  review_context.py         # PR context 生成，调用 worktree manager
```

现有 Code Review 与新的 AI Assistant 必须调用同一个 `GitWorktreeManager`。

不要复制 `_ensure_worktree` 或 Git 命令执行逻辑到 Assistant 模块。

## 18. 实施顺序

### 第一阶段：底层与后端

1. 抽取受控仓库配置和 `GitWorktreeManager`。
2. 将 Code Review worktree 准备迁移到 revision 模型。
3. 生成 revision 级 PR Context。
4. 增加 Session 入口提示、Turn 请求、Turn 快照存储。
5. 每个新 Turn 从 baseline 开始，并向 Runtime 注册受控动态工具。
6. 扩展 `RuntimeWorkspacePlan` 支持后端托管的只读精确代码挂载。
7. 实现工具成功后的 Turn 快照与版本更新通知。
8. 实现 Task→PR 创建 Assistant Session 接口。

### 第二阶段：页面

1. 在 Task 详情的关联 PR 中增加入口。
2. 接入准备状态、错误状态和跳转。
3. 验证不会覆盖其他 Session 的 Task 页面修改。

### 第三阶段：后续增强

1. 评估历史 Codex Runtime Session 的工具能力迁移。
2. 增加 worktree 引用计数和安全清理。

## 19. 验收标准

### baseline

- 普通会话不创建 worktree。
- Agent 可读取组织知识库中的 main/master 代码。
- 后台代码同步失败不阻断普通 Turn。
- 每个新用户 Turn 的初始 scope 都是 baseline。

### follow

- Task→PR 创建的 Session 保存 PR 入口提示，但不在 Turn 开始时自动挂载。
- Agent 可根据用户输入或入口提示自主调用工具。
- 工具成功后，当前 Turn 可以读取 PR 完整代码、描述、diff 和关联需求。
- PR 未更新时复用同一 revision worktree。
- PR 从 sha_A 更新到 sha_B 后，下次工具调用使用 sha_B。
- 同一 Turn 同一仓库再次挂载时，快照和逻辑路径同步切换。
- 同一 Turn 可以同时挂载多个受控仓库。
- Agent 收到明确的版本更新通知。
- 远程检查失败时不使用旧 SHA 继续回答。

### pinned

- 历史 Review 和失败重跑使用原 SHA。
- pinned Turn 不访问远程获取最新版本。
- 客户端不能提交任意 SHA。

### 覆盖规则

- 普通 Session 的精确挂载只影响当前 Turn。
- PR 入口 Session 的新 Turn 仍从 baseline 开始。
- Agent 不调用工具时保持 baseline。
- 同仓库再次挂载替换该仓库，其他仓库 scope 保持不变。

### 安全与并发

- Agent 只能请求受控仓库的合法 PR/分支，不能挂载任意目录、remote 或 SHA。
- 同一 revision 的并发准备只生成一份 worktree。
- Runtime 只获得只读逻辑挂载。
- 挂载切换失败时不留下空目录或半更新状态。

## 20. 非目标

当前不处理：

- Agent 选择配置之外的仓库。
- Agent 直接执行 Git checkout、pull、reset。
- 可写 PR worktree。
- 任意远程仓库克隆。
- 自动清理所有历史 worktree。
- 为 baseline 日常对话增加每轮远程检查。
