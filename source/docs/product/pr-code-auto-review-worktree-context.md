# PR 代码自动 Review Worktree 上下文改造方案

## 背景

当前 PR 代码自动 Review 主要把 PR 元信息、PR 描述和 GitHub 返回的 diff patch 直接塞进 prompt。这个方案有几个明显问题：

- reviewer 只能看到有限 diff 片段，缺少完整分支代码现场。
- PR 描述里的 ClickUp 需求单只能作为链接文本出现，agent 不一定会主动定位本地需求。
- prompt 过长且上下文组织不自然，不利于 agent 自主查看相邻代码、调用 `rg`、读取 skill。

新的目标是让 review agent 进入一个独立、可复现的 PR 代码目录，并通过简洁的上下文文件知道：

- 需求文档在哪里。
- PR 分支完整代码在哪里。
- diff 文件在哪里。
- code review skill 在哪里。
- 需要它自行观察 diff、相邻代码和需求后输出审查报告。

## 核心决策

### Agent 运行根目录

真实 agent runtime 的工作目录应视为 `workspace`。

虽然业务代码调用 runtime 时可能传入项目根目录，但 `claude_code` / `codex` runtime 都会把实际 agent cwd 归一化到项目内的 `workspace` 目录。因此所有交给 agent 的路径必须相对 `workspace`：

```text
knowledge/requirements/...
runtime/code-review-worktrees/...
runtime/code-review-contexts/...
source/backend/app/business/code_reviews/default_review_policy.md
```

不要在 prompt 中使用 `workspace/knowledge/...` 这类相对项目根的路径。

### Worktree 唯一性

worktree 以 PR 为单位，而不是以分支为单位。

目录结构：

```text
runtime/code-review-worktrees/
  {owner}/
    {repo}/
      PR_{pr_number}/
```

示例：

```text
runtime/code-review-worktrees/
  example-org/
    example_backend/
      PR_8784/
```

原因：

- 代码 review 的业务对象是 PR。
- 同一分支可能关联多个 PR，用分支作为唯一键会混淆审查现场。
- PR 号稳定、易读，也方便排查失败任务和历史产物。

### Head 变化处理

`PR_{pr_number}` 目录应随 PR `head_sha` 变化自动重建。

规则：

1. 如果 worktree 目录不存在，创建到当前 `head_sha`。
2. 如果目录存在且 `HEAD == head_sha`，直接复用。
3. 如果目录存在但 `HEAD != head_sha`，自动移除旧 worktree 并用新 `head_sha` 重建。
4. 自动移除只允许发生在受控目录 `runtime/code-review-worktrees/{owner}/{repo}/PR_{pr_number}` 内。
5. 移除前必须校验目标路径位于 worktree 根目录下，避免误删任意目录。
6. 优先使用 `git worktree remove --force <path>`，不要直接对任意路径做递归删除。
7. 如果移除或重建失败，job 标记 failed，不回退到旧的 diff-only review。

## 目录设计

### Worktree 目录

```text
runtime/code-review-worktrees/{owner}/{repo}/PR_{pr_number}/
```

该目录是完整 PR head 代码目录，agent 可以在里面读取文件、运行只读搜索命令。

### Context 目录

```text
runtime/code-review-contexts/{owner}/{repo}/PR_{pr_number}/
  diff.patch
  changed-files.txt
```

文件职责：

| 文件 | 说明 |
| --- | --- |
| `diff.patch` | 服务端生成的完整 PR diff，agent 自行读取 |
| `changed-files.txt` | 变更文件列表，便于快速定位 |

这些路径在 prompt 中均使用相对 `workspace` 的形式，例如：

```text
runtime/code-review-contexts/example-org/example_backend/PR_8784/diff.patch
```

## Git 数据来源

### 配置项

继续使用现有仓库配置中的 `workspace_repo_path` 作为本地源仓库路径。

示例：

```json
{
  "repo_full_name": "example-org/example_backend",
  "github_access": "gh_cli",
  "workspace_repo_path": "workspace/coinex/knowledge/code/example_backend",
  "base_branches": ["release", "main", "master"],
  "review_policy_path": "source/backend/app/business/code_reviews/default_review_policy.md"
}
```

### 创建 worktree

准备 worktree 时应在 `workspace_repo_path` 对应仓库中执行：

```bash
git fetch origin +refs/pull/<pr_number>/head:refs/remotes/<origin>/pull/<pr_number>/head
git fetch origin +refs/heads/<base_ref>:refs/remotes/<origin>/<base_ref>
git worktree add --detach <worktree_path> <head_sha>
```

Review 准备阶段会在仓库锁内同步 PR head 和 base 分支；如果 base commit 检查仍未通过，会再次同步 base 分支后重试。若 `head_sha` 不可达，应失败并记录清晰错误。第一版不做兼容 fallback，不从 GitHub zip 包或 API patch 拼代码目录。

### Fork PR

第一版可以不支持 fork PR。若本地源仓库 fetch 后无法解析 `head_sha`，job 失败，错误说明为：

```text
无法在本地仓库解析 PR head_sha，可能是 fork PR 或本地仓库未 fetch 到该提交。
```

## Diff 文件生成

服务端生成 diff 文件，而不是把 diff 直接塞进 prompt。

推荐命令：

```bash
git -C <worktree_path> diff <base_sha>...<head_sha>
```

如果 `base_sha` 在 worktree 中不可达，可在源仓库 fetch 后重试。仍失败则 job failed。

`changed-files.txt` 可由 GitHub PR files API 或本地命令生成。推荐本地命令：

```bash
git -C <worktree_path> diff --name-status <base_sha>...<head_sha>
```

如果未来需要保留 GitHub API 的文件状态，可以同时写入 API 结果，但第一版不需要。

## 需求文档解析

从 PR 描述中解析 ClickUp Task 链接，支持：

```text
https://app.clickup.com/t/{task_id}
https://acme.clickup.com/t/{task_id}
https://acme.clickup.com/t/{workspace_id}/{task_id}
```

解析到 task id 后，在本地需求目录中查找：

```text
knowledge/requirements/tasks/**/*__{task_id}.md
```

过滤规则：

- 路径中任何一段以 `_` 开头的目录跳过，例如 `_deleted`。
- 命中多个文件时全部写入 prompt，但按文件更新时间倒序排列，让较新的状态目录优先出现。

写给 agent 的路径必须是相对 `workspace`：

```text
knowledge/requirements/tasks/实现中/【后端】“仅提现”用户支持撤单、赎回等操作__86exqz8gc.md
```

## Prompt 设计

system prompt 内联 `review_policy_path` 指向的代码审查规范。user prompt 不再包含完整 diff，也不包含大量 PR 调度元信息；但会直接包含审查目标、需求、diff、changed-files 和 worktree 路径，避免再通过单独的导航文件跳转。

示例：

```md
请为以下 GitHub PR 执行代码审查，只输出中文 Markdown review 报告，不修改任何文件。

## 审查目标

- 仓库: `example-org/example_backend`
- PR: `#8784`
- PR URL: https://github.com/example-org/example_backend/pull/8784
- Base: `release` `d0c1a459880fb9f73ee5e91dbac7570f33d9de1d`
- Head: `226d7334639a65af29bca500a0949bb1d3a8835f`

## 必读资料

请按顺序读取并使用以下资料：

1. 需求文档:
   - `knowledge/requirements/tasks/实现中/【后端】“仅提现”用户支持撤单、赎回等操作__86exqz8gc.md`
2. Changed files: `runtime/code-review-contexts/example-org/example_backend/PR_8784/changed-files.txt`
3. Diff: `runtime/code-review-contexts/example-org/example_backend/PR_8784/diff.patch`
4. PR 分支完整代码目录: `runtime/code-review-worktrees/example-org/example_backend/PR_8784`

## 审查要求

- 按 system prompt 中的代码审查规范和输出格式生成报告。
- 结合需求文档判断代码是否完整覆盖需求。
- 重点审查代码相对需求的正确性、遗漏、越界改动、风险和测试缺口。
- 必要时进入 PR 分支代码目录读取相邻代码、调用关系和测试。
- 主要发现中的每条 finding 必须使用三级标题作为问题标题，格式为 `### P1 — 问题标题`、`### P2 — 问题标题` 等；不要把问题标题写成普通列表项。
```

PR 元信息只保留审查必要字段。作者、reviewer、邮件收件人、review request event 等调度和通知字段不写入 prompt。

## Worker 流程

代码 review worker 调整为：

1. 从 `code_review_jobs` 读取 pending job。
2. 重新拉取 PR metadata，确认 PR 状态、base/head 是否仍符合 job。
3. 解析仓库 owner/repo 和 PR number。
4. 准备 worktree：
   - fetch 本地源仓库。
   - 若 worktree 不存在，创建到 `head_sha`。
   - 若存在且 HEAD 不同，自动移除后重建。
   - 若失败，job failed。
5. 生成 context 目录。
6. 写入 `diff.patch`。
7. 写入 `changed-files.txt`。
8. 从 PR 描述解析需求任务，生成完整 prompt 所需路径。
9. 调用 agent，让 agent 自行读取 skill、需求、diff、changed-files 和代码。
10. 写入 review Markdown。
11. 标记 job completed。
12. 创建邮件任务。

## 失败策略

不允许 fallback 到旧的 diff-only 方案。

以下情况直接 job failed：

- `workspace_repo_path` 未配置或不是 Git 仓库。
- fetch 失败。
- `head_sha` 不可达。
- `base_sha` 不可达，导致 diff 文件无法生成。
- worktree 自动移除或重建失败。
- context 目录、diff 或 changed-files 写入失败。
- skill 文件不存在。

错误信息应包含：

- repo_full_name
- pr_number
- base_sha
- head_sha
- workspace_repo_path
- worktree_path
- 失败命令或失败原因摘要

## 清理策略

第一版不自动清理 worktree 和 context 文件。

后续可以单独增加清理能力，例如：

- 按 PR 关闭时间清理。
- 保留最近 N 天。
- 提供手动清理接口。

不要在 review 成功后立即删除 worktree，因为失败排查和复审可能需要保留现场。

## 测试建议

### 单元测试

1. worktree 路径生成：
   - `example-org/example_backend#8784` -> `runtime/code-review-worktrees/example-org/example_backend/PR_8784`
2. context 路径生成：
   - `runtime/code-review-contexts/example-org/example_backend/PR_8784/diff.patch`
3. ClickUp task 链接解析：
   - `/t/86abc`
   - `/t/9000000001/86abc`
4. 需求本地路径过滤：
   - 命中 `实现中`
   - 跳过 `_deleted`
5. worktree 复用：
   - 已存在且 HEAD 相同，复用
6. worktree 自动重建：
   - 已存在但 HEAD 不同，调用 remove 后重建
7. user prompt 不包含完整 diff，但包含需求、changed-files、diff 和 worktree 路径。

### 集成测试

使用临时 Git 仓库：

1. 创建 base commit 和 head commit。
2. 构造 fake PR snapshot。
3. 运行 review job。
4. 断言：
   - worktree 目录存在且 HEAD 为 head_sha。
   - `diff.patch` 存在且包含变更。
   - `changed-files.txt` 存在。
   - runtime 收到的 message 中路径均相对 `workspace`。
   - runtime 收到的 message 不包含完整 diff、policy 正文和 PR 描述正文。

## 兼容与迁移

这是新项目，不做旧路径兼容。

旧的 chunk review prompt 和 diff 分片策略可以删除或停用；不再保留“diff-only fallback”。如果保留部分函数仅用于其他功能，必须明确调用边界，避免代码 review worker 继续走旧链路。
