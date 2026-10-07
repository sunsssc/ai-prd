# PR 代码自动 Review 重构记录

记录日期：2026-06-17

本文记录最近几轮围绕 PR 代码自动 Review 的实现重构，重点是把原先过大的 `CodeReviewService` 拆成职责清晰的协作者，并落实 `pr-code-auto-review-worktree-context.md` 中的 worktree/context 方案。

## 重构目标

- 缩小 `service.py` 的职责，让它只保留任务编排、执行 review、生成报告、发送邮件等主流程。
- 将 PR 发现、GitHub 轮询、状态持久化、报告文件、邮件渲染、worktree/context 准备分别拆到独立模块。
- 不再把完整 diff、PR 描述和审查规范正文直接塞进 user prompt；审查规范从 policy 文件注入 system prompt，user prompt 只给出审查目标、需求路径、diff 文件、变更文件和完整 PR 代码目录。
- 保持外部调用面稳定，`CodeReviewService.poll_once()`、`run_job()`、`run_email_job()` 等入口继续存在。
- 不引入未验证的兼容路径。worktree/context 准备失败时 job 失败，不回退到旧的 diff-only review。

## 已完成拆分

| 轮次 | 文件 | 主要职责 |
| --- | --- | --- |
| 1 | `source/backend/app/business/code_reviews/review_context.py` | 准备 PR 级 worktree、context 目录、`diff.patch`、`changed-files.txt`；解析 ClickUp task 链接并定位本地需求文档，返回 prompt 所需路径。 |
| 2 | `source/backend/app/business/code_reviews/store.py` | 封装 SQLite 表初始化、review job 状态流转、email job 状态流转、轮询状态记录，以及 row/dataclass 映射。 |
| 3 | `source/backend/app/business/code_reviews/reports.py` | 写入代码 Review Markdown 报告、生成 frontmatter、识别风险级别、按 `review_id` 读取报告详情。 |
| 4 | `source/backend/app/business/code_reviews/email.py` | 生成纯文本邮件、HTML 邮件，以及 Markdown 到邮件 HTML 的样式渲染。 |
| 5 | `source/backend/app/business/code_reviews/polling.py` | 加载项目配置、轮询 GitHub PR、匹配当前 review request、过滤文件、提取 PR 作者提交邮箱、处理 retry 邮箱刷新和邮件 Cc 配置。 |
| 6 | `source/backend/app/business/code_reviews/service.py` | 收敛为服务门面和主流程编排：worker loop、运行 job、调用 runtime、写报告、创建邮件 job、发送邮件。 |

拆分后，`service.py` 已从单文件大实现降到约 300 行；轮询逻辑单独落在 `polling.py`。

## 当前模块边界

### CodeReviewService

`CodeReviewService` 是业务入口和编排层，负责：

- `poll_loop()`、`job_loop()`、`email_job_loop()`。
- `process_pending_jobs()` 和 `run_job()`。
- `process_retryable_email_jobs()` 和 `run_email_job()`。
- 调用 `CodeReviewPoller` 获取仓库配置、GitHub client 和有效文件列表。
- 调用 `CodeReviewContextPreparer` 准备 agent 可读上下文。
- 读取代码审查 policy 文件并注入 system prompt。
- 调用 runtime 生成 review Markdown。
- 调用 `CodeReviewArtifacts` 写入报告。
- 调用 `CodeReviewStore` 更新任务状态和创建邮件任务。

它不再直接负责项目配置解析、PR 轮询、SQLite 表结构、报告 frontmatter、邮件 HTML 样式和 worktree 文件生成。

### CodeReviewPoller

`CodeReviewPoller` 负责“发现需要 review 的 PR”：

- 读取 `CODE_REVIEW_PROJECT_CONFIG_PATH` 指向的 JSON 配置。
- 根据仓库配置选择 GitHub API client 或 `gh_cli` client。
- 扫描 open PR，跳过 draft、目标分支不匹配、已创建 job、没有当前 review request 的 PR。
- 从 timeline 中匹配当前仍有效的 `review_requested` 事件。
- 按扩展名、include/exclude path 过滤变更文件。
- 从 PR 作者 commit 中提取去重后的通知邮箱。
- 为失败 job retry 时刷新作者提交邮箱。
- 根据仓库配置生成邮件 Cc，并排除主收件人。

它不调用 agent runtime，不写 review 文件，也不发送邮件。

### CodeReviewContextPreparer

`CodeReviewContextPreparer` 负责“把 PR 变成 agent 可以自助阅读的本地现场”：

- worktree 路径为 `runtime/code-review-worktrees/{owner}/{repo}/PR_{pr_number}`。
- context 路径为 `runtime/code-review-contexts/{owner}/{repo}/PR_{pr_number}`。
- 路径写给 agent 时统一使用相对 `workspace` 的形式。
- 如果已有 worktree 的 `HEAD` 与 job 的 `head_sha` 一致则复用。
- 如果 `HEAD` 不一致，则校验路径在受控 worktree 根目录下，再使用 `git worktree remove --force` 移除并重建。
- 生成完整 `diff.patch` 和 `changed-files.txt`，不把 diff 直接塞进 prompt。
- 从 PR 描述中的 ClickUp 链接查找 `knowledge/requirements/tasks/**/*__{task_id}.md`，跳过 `_` 开头目录，按更新时间倒序返回给 prompt。

### CodeReviewArtifacts

`CodeReviewArtifacts` 负责 review 报告产物：

- 将 runtime 输出包装成完整 Markdown 报告。
- 写入 `workspace/knowledge/__reviews__/code-review/{YYYY}/{MM}/code_review_*.md`。
- 写入包含 repo、PR、base/head、review request、通知邮箱、skill、风险等级等字段的 frontmatter。
- 读取指定 job 或 `review_id` 对应的报告详情。

### CodeReviewStore

`CodeReviewStore` 只负责数据库：

- `code_review_poll_state`。
- `code_review_jobs`。
- `code_review_email_jobs`。
- pending/running/completed/failed/sent 状态流转。
- stale running job 回收。
- failed job/email job retry。

### email.py

邮件模块保持纯渲染职责：

- `build_email_body()` 生成纯文本正文。
- `build_email_html_body()` 生成 HTML 正文。
- `render_markdown_for_email()` 将 review Markdown 转成适合邮件阅读的 HTML。

## 行为收敛

- system prompt 包含代码审查 policy；user prompt 直接包含审查目标、需求路径、`changed-files.txt`、`diff.patch` 和 PR worktree 路径，要求 agent 自行读取这些资料和相邻代码。
- Agent 可见路径统一相对 `workspace`，不使用 `workspace/...` 前缀。
- job 运行前重新拉取 PR 快照，并校验 `base_sha`、`head_sha` 必须与创建 job 时一致。
- PR 更新导致 `head_sha` 变化时，旧 job 失败，不继续用旧上下文生成报告。
- worktree/context 准备失败时，job 失败并记录明确错误，不回退到旧 diff-only 逻辑。
- 第一版不额外兼容 fork PR。如果本地源仓库无法解析 `head_sha`，job 失败并提示可能是 fork PR 或本地仓库未 fetch 到该提交。
- 邮件发送失败会进入 email job retry；代码 review job retry 时会刷新 PR 作者提交邮箱。

## 验证记录

最近一轮拆分后已执行：

```bash
source .venv/bin/activate && python -m py_compile source/backend/app/business/code_reviews/service.py source/backend/app/business/code_reviews/polling.py source/backend/app/business/code_reviews/store.py source/backend/app/business/code_reviews/reports.py source/backend/app/business/code_reviews/review_context.py source/backend/app/business/code_reviews/email.py
```

```bash
source .venv/bin/activate && pytest source/backend/tests/test_code_reviews.py
```

结果：`17 passed`。

测试覆盖的关键路径包括：

- 未配置项目时不轮询。
- 首次 review request 创建 job 且不重复创建。
- PR 作者 commit 邮箱提取与去重。
- worktree/diff/changed-files 生成。
- user prompt 不再包含完整 diff、policy 正文和 PR 描述。
- ClickUp task 链接解析和本地需求文件定位。
- worktree stale HEAD 自动重建。
- 缺少 `workspace_repo_path` 时 job 失败且保留诊断信息。
- 邮件 HTML 样式、Cc 去重和失败重试。

## 后续可继续拆分

下一步如果继续缩小 `service.py`，优先考虑：

- 抽出 `review_runner.py` 或 `executor.py`：承接 prompt 构建、runtime session 创建、stream 结果聚合。
- 抽出 worker 调度：把 `poll_loop()`、`job_loop()`、`email_job_loop()` 放到更薄的后台任务模块。
- 为 `CodeReviewPoller` 单独补充更聚焦的单元测试，尤其是路径过滤、base branch 过滤和 review request 匹配。

在继续拆分前，建议保持当前外部入口不变，让 API 层和后台任务仍只依赖 `CodeReviewService`。
