# PR 代码自动 Review 设计文档

## 背景

当前系统已经具备需求文档自动技术评审、代码仓库同步、以及 GitHub PR diff 驱动业务文档更新提案的能力，但还没有面向代码本身的自动 review 流程。研发在 GitHub 上向同事发起 PR review 请求后，仍依赖被请求人主动阅读代码、排查风险和反馈意见。

PR 代码自动 Review 要在“某个可配置项目的 PR 首次被请求 review”时自动运行代码 review skill，生成一份可追溯的代码审查报告，并通过邮件发送到 PR 作者在本次 PR commits 中使用的提交邮箱。第一版优先采用定时拉取 GitHub 数据的方式识别 review 请求，减少 webhook 配置成本；后续可以在不改变任务和产物模型的前提下补充 webhook 触发。

---

## 核心概念

| 概念 | 说明 |
|------|------|
| 可 review 项目 | 显式配置允许自动代码 review 的 GitHub 仓库，未配置仓库不处理 |
| 源 PR | GitHub Pull Request，包含仓库、PR 编号、base/head sha、标题、描述、作者、review 请求事件、变更文件和 diff |
| 首次 review 请求 | 同一仓库同一 PR 第一次出现 `review_requested` 事件；系统只基于这次事件自动 review 一次 |
| 请求人 | 发起 review 请求的 GitHub 用户，即 `review_requested` 事件 actor |
| 被请求人 | 被请求 review 的 GitHub 用户或团队；只要存在任意 review 请求即可触发自动 review |
| PR 作者提交邮箱 | PR 作者在本次 PR commits 中使用的 commit author email；仅用于发送 review 结果通知，不作为任务创建条件 |
| 代码 Review 报告 | AI 根据 PR diff 和代码 review skill 生成的 Markdown 报告，包含问题、风险、建议和验证点 |
| 邮件通知 | 将代码 Review 报告摘要、完整报告正文和 PR 链接发送给 PR 作者提交邮箱 |
| 轮询游标 | 每个仓库的定时拉取状态，用于避免重复扫描已经处理过的事件 |

---

## 目标

1. 针对配置内项目，发现 PR 首次 review 请求后自动生成一次代码 review。
2. 使用专门的代码审查 policy 文件作为 system prompt 的审查规范，而不是复用需求文档评审或业务文档更新规范。
3. 将 review 意见通过邮件发送到 PR 作者在本次 PR commits 中使用的提交邮箱。
4. 所有 AI 产物、任务状态、邮件发送状态都可追溯，避免重复发送。
5. 第一版使用定时拉取监控 GitHub review 请求，不强依赖 webhook。

---

## 非目标

- 不自动在 GitHub PR 下发表评论。
- 不自动 approve、request changes 或阻塞合并。
- 不修改代码。

---

## 触发规则

### 首次请求定义

同一仓库同一 PR 只在第一次发现 `review_requested` 事件时触发自动 review。系统以持久化记录判断是否已经处理：

```text
unique_key = repo_full_name + pr_number
```

如果同一个 PR 后续发生以下情况，不重复触发：

- 删除 reviewer 后重新 request。
- 新增第二批 reviewer。
- PR 代码更新但没有新的产品配置要求。
- 同一 reviewer 被重复 request。


### 触发条件

只有同时满足以下条件才创建自动 review 任务：

| 条件 | 说明 |
|------|------|
| 仓库已配置启用 | `repo_full_name` 在配置文件中，且 `enabled=true` |
| PR 非 draft | Draft PR 默认不触发 |
| 目标分支匹配 | PR base branch 在项目配置的 `base_branches` 内 |
| 尚未处理过该 PR | `code_review_jobs` 中不存在同一仓库同一 PR 的自动任务 |
| 当前存在 review 请求 | PR 当前 `reviewRequests` 非空，并能在 timeline 中匹配到对应的 `review_requested` 事件；不要求被请求 reviewer 有邮箱映射 |
| 变更文件有效 | 至少存在一个符合扩展名和路径规则的代码文件变更 |

---

## 项目配置

新增配置项，由环境变量指定配置文件路径：

```text
CODE_REVIEW_PROJECT_CONFIG_PATH=workspace/config/code-auto-review.json
```

未配置该变量时，代码自动 review 功能不启动。第一版不加入隐式默认仓库，避免误扫代码库。

配置示例：

```json
{
  "enabled": true,
  "poll_interval_seconds": 300,
  "repositories": [
    {
      "repo_full_name": "example-org/example_backend",
      "enabled": true,
      "base_branches": ["main", "master"],
      "notification_recipient_mode": "all",
      "notification_cc_emails": ["observer@corp.test"],
      "review_policy_path": "source/backend/app/business/code_reviews/default_review_policy.md"
    }
  ]
}
```

配置原则：

- 仓库必须显式列出，不能通过组织名通配自动开启。
- 邮件收件人来自 PR commits 中 GitHub author login 等于 PR author login 的 commit author email；缺少 PR 作者提交邮箱时仍会创建 review 任务。
- `notification_recipient_mode` 控制正常 review 结果邮件的发送对象：`all` 表示发送给 PR 作者并抄送 Cc，`creator` 表示只发送给 PR 作者，`cc` 表示只发送给 `notification_cc_emails` 中配置的邮箱；未配置时默认为 `all`。
- `notification_cc_emails` 用于配置固定通知列表；在 `all` 模式下作为 Cc，在 `cc` 模式下作为独立 To 收件人。
- 文件路径过滤只用于降低噪音，不作为安全权限边界。



---

## 文件结构

代码 review 报告继续放在 `workspace/knowledge/__reviews__/` 下，与现有 AI 派生产物统一管理。

```text
workspace/knowledge/__reviews__/
└── code-review/
    └── 2026/
        └── 06/
            └── code_review_20260602_153000_ab12cd.md
```

---

## 报告 Frontmatter

```md
---
review_id: code_review_20260602_153000_ab12cd
review_type: code_review
source_type: github_pr
repo_full_name: example-org/example_backend
pr_number: 1234
pr_url: https://github.com/example-org/example_backend/pull/1234
base_ref: main
base_sha: abc123
head_sha: def456
diff_hash: sha256:...
requested_event_id: "123456789"
requested_by_login: requester
requested_reviewer_logins: alice,bob
notification_emails: author@corp.test
review_policy: source/backend/app/business/code_reviews/default_review_policy.md
status: completed
risk_level: medium
created_at: 2026-06-02T15:30:00+08:00
completed_at: 2026-06-02T15:31:20+08:00
---
```

正文保留完整 Markdown 报告，便于后续在 AI 对话中引用。

---

## 数据模型

新增表存入现有 `assistant.sqlite3`。

### code_review_poll_state

记录每个仓库轮询状态。

| 字段 | 类型 | 说明 |
|------|------|------|
| repo_full_name | str | PK |
| last_polled_at | datetime? | 最近一次轮询完成时间 |
| last_event_cursor | str? | 最近处理的 GitHub timeline event 标识 |
| error_message | str? | 最近一次轮询错误 |
| updated_at | datetime | 更新时间 |

第一版可以不依赖严格 cursor，只要每轮扫描最近更新的 open PR 并通过 `code_review_jobs` 去重即可。`last_event_cursor` 保留给后续增量优化。

### code_review_jobs

自动 review 任务表。

| 字段 | 类型 | 说明 |
|------|------|------|
| job_id | str | PK |
| repo_full_name | str | GitHub 仓库 |
| pr_number | int | PR 编号 |
| pr_url | str | PR 地址 |
| base_ref | str | 目标分支 |
| base_sha | str | base sha |
| head_sha | str | head sha |
| diff_hash | str | PR diff hash |
| requested_event_id | str | GitHub review 请求事件 ID |
| requested_by_login | str | 请求人 GitHub login |
| requested_reviewer_logins | str | 被请求个人 reviewer，逗号分隔 |
| requested_team_slugs | str | 被请求团队 slug，逗号分隔 |
| notification_emails | str | 本次需要发送的邮箱，逗号分隔 |
| review_type | str | 固定为 `code_review` |
| skill_name | str? | 历史/审计字段，当前实现不依赖 skill，可为空 |
| review_policy_path | str | 代码审查 policy 文件路径，默认 `source/backend/app/business/code_reviews/default_review_policy.md` |
| status | str | `pending` / `running` / `completed` / `failed` |
| review_id | str? | 成功生成后的报告 ID |
| review_path | str? | 成功生成后的报告路径 |
| error_message | str? | 失败原因 |
| created_at | datetime | 创建时间 |
| started_at | datetime? | 开始时间 |
| completed_at | datetime? | 完成时间 |

唯一约束：

```sql
UNIQUE(repo_full_name, pr_number)
```

如果配置 `rerun_on_new_head=true`，唯一约束改为：

```sql
UNIQUE(repo_full_name, pr_number, head_sha)
```

第一版按默认约束实现。

### code_review_email_jobs

邮件发送任务表，避免 AI review 成功但邮件发送失败时丢失通知。

| 字段 | 类型 | 说明 |
|------|------|------|
| email_job_id | str | PK |
| code_review_job_id | str | 关联 `code_review_jobs.job_id` |
| review_id | str | 报告 ID |
| recipient_email | str | 收件人 |
| subject | str | 邮件标题 |
| status | str | `pending` / `running` / `sent` / `failed` |
| attempts | int | 尝试次数 |
| error_message | str? | 失败原因 |
| created_at | datetime | 创建时间 |
| sent_at | datetime? | 发送成功时间 |

唯一约束：

```sql
UNIQUE(code_review_job_id, recipient_email)
```

---

## GitHub 拉取流程

### 定时轮询

在 FastAPI lifespan 中注册后台任务：

```text
code_review_poll_loop
```

流程：

1. 读取 `CODE_REVIEW_PROJECT_CONFIG_PATH`。
2. 若全局或仓库配置未开启，跳过。
3. 对每个配置仓库拉取 open PR 列表。
4. 过滤 draft PR、目标分支不匹配的 PR。
5. 跳过当前 `reviewRequests` 为空的 PR。
6. 对候选 PR 拉取 timeline events。
7. 找到与当前 pending reviewer/team 匹配的 `review_requested` 事件。
8. 如果 `code_review_jobs` 已存在同一仓库同一 PR，跳过。
9. 拉取 PR changed files 和 diff。
10. 根据文件扩展名和路径规则过滤。
11. 根据 PR 作者在本次 PR commits 中使用的 commit author email 解析通知邮箱；解析不到时保留空收件人。
12. 创建 `code_review_jobs`。
13. 由 worker 消费 pending job。

### GitHub Client 能力

现有 `GitHubPullRequestClient` 已支持读取 PR metadata 和 files，可扩展为更通用的 PR client，新增：

| 方法 | 说明 |
|------|------|
| `list_open_pull_requests(repo_full_name)` | 拉取 open PR |
| `fetch_pull_request(repo_full_name, pr_number)` | 读取 PR metadata |
| `fetch_pull_request_files(repo_full_name, pr_number)` | 读取 changed files 和 patch |
| `fetch_pull_request_timeline(repo_full_name, pr_number)` | 读取 timeline events，识别 `review_requested` |

第一版直接使用 GitHub REST API 和当前 `GITHUB_API_TOKEN` / `GITHUB_API_BASE_URL` 配置，不新增多平台抽象。

---

## Review Worker

后台 worker：

```text
code_review_job_loop
```

执行步骤：

1. 从 `code_review_jobs` 读取 pending 任务。
2. 标记任务为 running。
3. 重新拉取 PR，确认 PR 仍 open、非 draft、head_sha 与任务一致。
4. 拉取 changed files 和 diff。
5. 按项目配置过滤文件。
6. 读取 `review_policy_path` 指向的 policy 文件，并注入 system prompt。
7. 准备 worktree、`changed-files.txt` 和 `diff.patch`。
8. 组装 user prompt，包含：
   - 仓库、PR URL、base/head sha。
   - 需求文档路径、`changed-files.txt`、`diff.patch` 和 PR worktree 路径。
9. 调用 `AgentRuntimeClient` 生成 review 报告。
10. 写入 `workspace/knowledge/__reviews__/code-review/YYYY/MM/*.md`。
11. 标记任务 completed。
13. 为每个收件人创建 `code_review_email_jobs`。
14. 邮件 worker 发送通知。

如果 PR head 已变更，第一版将当前任务标记 failed，错误原因写明“PR 已更新，旧 head_sha 任务跳过”。不自动创建新任务，除非配置 `rerun_on_new_head=true`。

---

## 邮件通知

现有 `EmailSender` 只定义了 `send_verification_code`。代码 review 邮件需要新增通用发送能力：

```python
class EmailSender(Protocol):
    def send_verification_code(self, *, email: str, code: str, purpose: str) -> None: ...
    def send_message(
        self,
        *,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        cc: tuple[str, ...] = (),
    ) -> None: ...
```

`DebugEmailSender` 打印邮件内容，`SMTPEmailSender` 通过现有 SMTP 配置发送普通文本邮件。

邮件标题：

```text
[AI Code Review] example-org/example_backend PR #1234: <PR 标题>
```

邮件正文：

```text
你好，

你的 PR 已邀请审核人，系统已自动完成一次 AI 代码审查。

仓库：example-org/example_backend
PR：#1234 <标题>
链接：https://github.com/example-org/example_backend/pull/1234
PR 提交人：author
风险等级：medium

以下是 review 意见：

<Markdown 报告正文>

请以实际代码和项目上下文为准，AI 结论只作为辅助审查意见。
```

发送规则：

- 默认 `notification_recipient_mode=all`，发送给 PR 作者在本次 PR commits 中使用的 commit author email，并把 `notification_cc_emails` 写入 Cc header。
- `notification_recipient_mode=creator` 时，只发送给 PR 作者 commit author email，不抄送 `notification_cc_emails`。
- `notification_recipient_mode=cc` 时，只发送给 `notification_cc_emails` 中的邮箱；这些邮箱作为独立 To 收件人，不写入 Cc header。
- 同一 `code_review_job_id + recipient_email` 只发送一次。
- PR 作者提交邮箱缺失时仍保留本地 review 报告，不创建邮件任务。
- 邮件发送失败不回滚 review 报告；失败状态保存在 `code_review_email_jobs`，由邮件 worker 重试。
- 默认最多重试 3 次，间隔由 worker loop 控制。

---

## API

第一版后台自动运行即可，但保留查询和手动重试 API，便于排查。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/code-reviews/jobs` | 查询代码 review 任务列表，可按 repo/pr/status 过滤 |
| GET | `/api/code-reviews/jobs/{job_id}` | 查询任务详情 |
| GET | `/api/code-reviews/{review_id}` | 读取代码 review 报告 |
| POST | `/api/code-reviews/jobs/{job_id}/retry` | 重试 failed 任务 |
| POST | `/api/code-reviews/email-jobs/{email_job_id}/retry` | 重试 failed 邮件 |
| POST | `/api/code-reviews/poll` | 管理员手动触发一次轮询 |

第一版不需要前端新增主入口；如果后续需要可在管理页增加任务状态面板。

---

## 与现有能力的关系

| 维度 | 需求自动技术评审 | PR 自动业务文档更新 | PR 代码自动 Review |
|------|------|------|------|
| 触发源 | ClickUp 需求同步 | GitHub PR Webhook / 手动 | GitHub 定时拉取 review 请求 |
| 源对象 | 需求 Markdown | PR metadata + diff | PR metadata + diff + review 请求事件 |
| 产物目录 | `__reviews__/tech-review/` | `__reviews__/business-doc-update/` | `__reviews__/code-review/` |
| 任务表 | `requirement_review_jobs` | `business_doc_update_jobs` | `code_review_jobs` |
| 执行规范 | `tech-review-prd` Skill | `business-doc-updater` Skill | 代码审查 policy 文件 |
| 主要动作 | 生成需求技术初评 | 生成业务文档更新提案 | 生成代码审查意见并发邮件 |
| 是否改源文件 | 否 | 需用户确认后改业务文档 | 否 |
| 是否评论 PR | 否 | 否 | 第一版否 |

---

## 安全与权限

- 只有配置内仓库会被扫描。
- GitHub Token 只需要读取 PR、文件 diff 和 timeline 的权限。
- 邮件收件人只来自 GitHub PR commits 中 PR 作者使用的 commit author email，不从 PR 正文或 AI 输出中提取。
- AI prompt 不包含 SMTP 密钥、GitHub Token 或系统环境变量。
- 报告文件只写入 `workspace/knowledge/__reviews__/code-review/`。
- 自动 review 不执行代码、不运行测试、不修改工作区。

---

## 失败处理

| 场景 | 处理 |
|------|------|
| GitHub API 失败 | 记录 poll state 错误，下轮重试 |
| PR 已关闭 | 跳过，不创建任务 |
| PR 是 draft | 跳过 |
| 当前没有 pending review request | 跳过，不使用历史 timeline 事件触发 |
| PR head 已变化 | 当前 job 标记 failed，默认不自动新建 |
| 没有有效代码变更 | 跳过 |
| 请求人没有邮箱映射 | 创建 review 任务并保留本地报告；不创建邮件任务 |
| skill 缺失 | job 标记 failed |
| AI 生成失败 | job 标记 failed，可手动 retry |
| 邮件发送失败 | email job 标记 failed，可自动或手动 retry |

---


## 落地步骤

1. 找到当前 workspace 下对应的 review skill。
2. 新增 `CODE_REVIEW_PROJECT_CONFIG_PATH` 配置读取和校验。
3. 扩展 GitHub PR client，支持 open PR 列表和 timeline events。
4. 新增 `code_review_poll_state`、`code_review_jobs`、`code_review_email_jobs` 表。
5. 实现 `CodeReviewService`，包含轮询、任务创建、review worker 和邮件任务创建。
6. 扩展 `EmailSender`，增加通用 `send_message` 能力。
7. 在 FastAPI lifespan 注册 `code_review_poll_loop`、`code_review_job_loop`、`code_review_email_job_loop`。
8. 新增查询和重试 API。
9. 增加后端测试：
   - 未配置仓库不触发。
   - 首次 review 请求创建任务。
   - 同一 PR 重复请求不重复创建任务。
   - PR 作者提交邮箱缺失时仍创建 review，但不创建邮件任务。
   - review 成功后创建邮件任务。
   - 邮件失败后可重试。
10. 可选增加管理端任务状态面板。
