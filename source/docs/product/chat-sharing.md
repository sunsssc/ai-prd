# 聊天共享功能设计

## 背景

AI 助手对话（Session）默认仅对创建者可见。共享机制支持将对话开放给全员或指定用户查看，允许查看者发表评论；已登录且有访问权限的查看者还可以在原会话中继续追问。

---

## 核心概念

| 概念 | 说明 |
|------|------|
| 会话（Session） | 现有 `assistant_sessions`，用户与 AI 的完整对话 |
| 共享（Share） | 所有者将会话开放给他人，分 `public`（全员）和 `members`（指定用户）两种模式 |
| 评论（Comment） | 有权查看的用户针对整个会话或某条消息发表的文字反馈 |
| 共享追问（Follow-up） | 共享查看者提交到原会话的后续问题；同一会话全局串行执行 |
| 会话文件附件（File Artifact） | Assistant 在会话中生成、引用并允许用户下载的文件快照 |

---

## 会话文件附件设计

### 问题定义

Assistant 生成的文件默认写入当前用户个人工作区，例如：

```text
me/预测市场业务文档.pdf
```

当前个人文件下载接口语义是“下载当前登录用户自己的 `me` 文件”。当会话被共享给别人后，查看者点击同一条消息里的 `me/...` 路径，会被解析到查看者自己的个人工作区，而不是共享者的个人工作区，因此文件不可见。

不能直接把 `/api/workspace/me/file` 扩展成支持读取分享者 `me`，因为这会把个人工作区权限和共享会话权限混在一起，容易形成越权入口。共享用户应该只能下载“该共享会话明确包含的文件”，不能浏览或猜测读取共享者个人目录。

### 设计结论

会话内可下载文件应提升为“会话文件附件”，而不是继续以个人工作区路径作为授权对象。

核心原则：

1. 生成文件时仍写入会话所有者的个人工作区，例如 `workspace/users/<owner_user_id>/...`。
2. 当文件出现在 Assistant 消息、引用或 runtime 事件中时，后端登记为会话文件附件。
3. 对外展示和下载使用稳定的 `artifact_id`，不把宿主机真实路径暴露给前端。
4. 共享下载权限继承共享会话权限：能看该共享会话，才能下载该会话附件。
5. 共享用户只能下载已登记附件，不能通过路径参数读取共享者个人工作区的其他文件。
6. 共享附件采用快照存储，确保分享后的会话内容可复现。

### 快照策略

采用“共享附件快照”而不是“实时代理读取 owner 的 `me` 文件”。

推荐落盘位置：

```text
workspace/runtime/shared-session-files/
└── <share_id>/
    └── <artifact_id>/
        ├── file
        └── metadata.json
```

快照内容：

- 文件二进制内容。
- 文件名、MIME 类型、大小、sha256。
- 原始展示路径，例如 `me/预测市场业务文档.pdf`。
- 关联的 `session_id`、`message_id` 或 `turn_id`。

选择快照的原因：

- 共享后文件内容稳定，不会因为 owner 后续覆盖个人文件而变化。
- owner 删除个人工作区源文件后，已共享会话仍可复现。
- 撤销共享时，只需要禁用 share 权限；是否物理清理快照可以由后续清理任务处理。
- 权限边界清晰：下载对象是共享会话附件，不是 owner 的个人目录。

### 附件登记时机

第一阶段可以采用后端受控登记，不要求 agent 显式调用“发布附件”工具。

登记来源：

1. Assistant 消息 Markdown 中出现的个人工作区路径：
   - `me/...`
   - `/me/...`
   - `sandbox:/me/...`
2. `answer_citations` 中 `path` 指向个人工作区文件。
3. runtime 事件中工具写入、读取或最终回复引用的个人工作区文件。

登记约束：

- 只登记当前会话 owner 的个人工作区内真实存在的普通文件。
- 不登记目录。
- 不登记包含 `..` 的路径。
- 不登记组织知识库路径；知识库仍按原知识库权限处理。
- 不登记会话临时目录 `tmp/...`，除非后续明确支持“临时文件转附件”。

### 路径与标识

前端可继续展示用户友好的原始路径：

```text
me/预测市场业务文档.pdf
```

但下载地址必须使用附件标识：

```text
/api/assistant/shared/{share_token}/files/{artifact_id}
```

所有者在自己会话内也可以使用同一套附件下载地址：

```text
/api/assistant/sessions/{session_id}/files/{artifact_id}
```

`artifact_id` 是后端生成的不可预测 ID，不从文件名或路径派生。

### 数据模型补充

新增表存入 `assistant.sqlite3`。

#### assistant_file_artifacts（会话附件）

| 字段 | 类型 | 说明 |
|------|------|------|
| artifact_id | str | PK |
| session_id | str | FK → assistant_sessions |
| message_id | str? | FK → assistant_messages；无法归属到单条消息时为空 |
| turn_id | str? | FK → assistant_turns |
| owner_user_id | str | 会话所有者 |
| display_path | str | 前端展示路径，例如 `me/xxx.pdf` |
| source_path | str | owner 个人工作区内的相对路径，不存宿主机绝对路径 |
| filename | str | 下载文件名 |
| mime_type | str | 下载 MIME 类型 |
| size_bytes | int | 文件大小 |
| sha256 | str | 内容哈希 |
| storage_status | str | `source` / `snapshotted` / `missing` |
| created_at | datetime | |
| updated_at | datetime | |

`source_path` 必须是 owner 个人工作区相对路径，例如 `files/xxx.pdf` 或 `artifacts/xxx.pdf`。不能存 `/Users/...` 这类宿主机绝对路径。

#### shared_session_file_artifacts（共享附件快照）

| 字段 | 类型 | 说明 |
|------|------|------|
| shared_file_id | str | PK |
| share_id | str | FK → session_shares |
| artifact_id | str | FK → assistant_file_artifacts |
| snapshot_path | str | `workspace/runtime/shared-session-files` 下的相对路径 |
| filename | str | 下载文件名 |
| mime_type | str | 下载 MIME 类型 |
| size_bytes | int | 快照文件大小 |
| sha256 | str | 快照内容哈希 |
| created_at | datetime | |

唯一约束：

```text
UNIQUE(share_id, artifact_id)
```

### 权限规则补充

| 操作 | 会话所有者 | 共享用户（public 或 members 成员） | 其他用户 |
|------|:---------:|:--------------------------------:|:-------:|
| 下载会话附件 | ✓ | ✓ | ✗ |
| 下载未登记的 owner 个人文件 | ✗ | ✗ | ✗ |
| 浏览 owner 个人工作区 | ✗ | ✗ | ✗ |

说明：

- owner 下载附件不依赖共享是否存在。
- 共享用户下载附件必须满足 `session_shares.is_active=true`，并通过 `public` 或 `members` 权限校验。
- share 被撤销后，共享下载接口立即不可用。
- 快照文件可保留到清理任务异步删除，不影响权限判断。

### API 补充

#### 会话所有者下载附件

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/assistant/sessions/{session_id}/files/{artifact_id}` | 下载当前用户自己会话中的附件 |

后端校验：

1. 当前用户是 `assistant_sessions.user_id`。
2. `artifact_id` 属于该 `session_id`。
3. 附件源文件或快照存在。

#### 共享用户下载附件

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/assistant/shared/{share_token}/files/{artifact_id}` | 下载共享会话中的附件 |

后端校验：

1. `share_token` 对应 active share。
2. 当前用户对 share 有访问权限。
3. `artifact_id` 属于 share 对应的 `session_id`。
4. 存在 `shared_session_file_artifacts` 快照；如果不存在，可在锁内从源文件创建一次快照。

#### 附件列表

共享会话详情接口应返回每条消息关联的附件元信息：

```json
{
  "message_id": "...",
  "content": "...",
  "file_artifacts": [
    {
      "artifact_id": "...",
      "display_path": "me/预测市场业务文档.pdf",
      "filename": "预测市场业务文档.pdf",
      "mime_type": "application/pdf",
      "size_bytes": 195696,
      "download_url": "/api/assistant/shared/<token>/files/<artifact_id>"
    }
  ]
}
```

普通会话详情接口也返回同结构，但 `download_url` 使用 `/api/assistant/sessions/{session_id}/files/{artifact_id}`。

### 前端渲染规则

消息渲染不应只根据 `me/...` 拼 `/api/workspace/me/file`。应优先查找后端返回的 `file_artifacts`：

1. 如果 inline code、Markdown 链接或 citation path 命中 `display_path`，使用对应 `download_url`。
2. 如果没有命中附件，仍可在 owner 自己会话内回退到 `/api/workspace/me/file`，但共享页面不能使用该回退。
3. 共享页面中未登记的 `me/...` 只能展示为普通文本，不提供下载。

这样可以避免分享页把 `me/...` 错误解析成查看者自己的个人工作区。

### 实施步骤

1. 新增附件表和共享附件快照表。
2. 增加后端路径扫描与附件登记服务，覆盖消息落库后、citations 落库后、共享创建/更新前的补偿扫描。
3. 新增 owner 附件下载接口和 shared 附件下载接口。
4. 会话详情和共享详情响应增加 `file_artifacts`。
5. 前端消息渲染优先使用 `file_artifacts.download_url`。
6. 对已有会话做懒加载补偿：打开会话或创建共享时扫描历史消息中的 `me/...` 路径并登记附件。
7. 增加快照清理任务：share 长期撤销后可清理 `workspace/runtime/shared-session-files/<share_id>`。

### 非目标

- 不开放共享者个人工作区目录浏览。
- 不支持共享用户修改附件。
- 不把 `tmp/...` 自动作为长期附件；临时文件需要先转入 `me/...` 或显式登记。
- 不把组织知识库文件复制成共享附件；知识库仍走知识库权限和引用机制。

### 风险与约束

- 如果历史消息只写了文件名，没有写 `me/...` 路径，后端无法可靠反推出源文件，不能自动登记。
- 如果创建共享时源文件已经被 owner 删除，附件状态应标记为 `missing`，前端展示“文件已不存在”，不要伪造下载。
- 附件快照会增加磁盘占用，需要后续清理策略。
- MIME 类型以内容检测优先，扩展名兜底，避免 PDF 被当成 `application/octet-stream` 影响浏览器处理。

## 数据模型

共享配置、评论和追问队列均存入现有 `assistant.sqlite3`。

### session_shares（共享配置）

| 字段 | 类型 | 说明 |
|------|------|------|
| share_id | str | PK |
| session_id | str | FK → assistant_sessions |
| owner_id | str | FK → users |
| share_type | str | `public` / `members` |
| share_token | str | 唯一访问令牌（生成共享链接用） |
| is_active | bool | false 表示已撤销 |
| created_at | datetime | |

### session_share_members（指定成员，仅 members 模式有效）

| 字段 | 类型 | 说明 |
|------|------|------|
| member_id | str | PK |
| share_id | str | FK → session_shares |
| user_id | str | FK → users |
| added_at | datetime | |

### session_comments（评论）

| 字段 | 类型 | 说明 |
|------|------|------|
| comment_id | str | PK |
| session_id | str | FK → assistant_sessions |
| message_id | str? | FK → assistant_messages；null 表示针对整个会话 |
| user_id | str | FK → users |
| content | str | |
| created_at | datetime | |
| updated_at | datetime | |

> 暂不支持嵌套回复。

### shared_session_follow_ups（共享追问队列）

| 字段 | 类型 | 说明 |
|------|------|------|
| request_id | str | PK |
| share_id | str | FK → session_shares |
| session_id | str | FK → assistant_sessions |
| requested_by_user_id | str | 追问者 |
| content | str | 追问内容 |
| status | str | `queued` / `processing` / `completed` / `failed` / `cancelled` |
| turn_id | str? | 开始执行后关联的 assistant_turn |
| error_message | str? | 失败或取消原因 |
| created_at / started_at / completed_at | datetime | 排队与执行时间 |

约束：同一 `session_id` 最多只有一条 `processing` 记录；领取队首请求时还必须确认该会话不存在 `running` turn。这样即使两个客户端同时提交，也只能有一个请求开始执行，后到请求保持 `queued`。

---

## 权限规则

public 共享会话允许通过链接匿名查看；继续追问、发表评论和 members 共享访问均要求登录。

| 操作 | 会话所有者 | 共享用户（public 或 members 成员） | 其他用户 |
|------|:---------:|:--------------------------------:|:-------:|
| 查看会话与消息 | ✓ | ✓ | ✗ |
| 在原会话中继续追问 | ✓ | ✓（需登录） | ✗ |
| 发表/编辑/删除自己的评论 | ✓ | ✓ | ✗ |
| 管理共享配置 | ✓ | ✗ | ✗ |

---

## API 设计

### 共享管理（需登录 + 是会话所有者）

| 方法 | 路径 | 说明 |
|------|------|------|
| PUT | `/api/sessions/{session_id}/share` | 创建或更新共享配置（幂等） |
| GET | `/api/sessions/{session_id}/share` | 查询当前共享状态 |
| DELETE | `/api/sessions/{session_id}/share` | 撤销共享（is_active=false） |

PUT 请求体：
```json
{
  "share_type": "public",
  "member_user_ids": ["uid1"]
}
```

响应体（共享详情）：
```json
{
  "share_id": "...",
  "share_type": "public",
  "share_token": "...",
  "share_url": "https://.../shared/xxx",
  "members": [{ "user_id": "...", "name": "...", "avatar_url": "..." }],
  "created_at": "..."
}
```

### 共享内容访问（需登录）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/shared/{share_token}` | 通过 token 获取会话及消息列表 |
| GET | `/api/shared` | 获取当前用户可见的共享会话列表（含 public 和自己在 members 中的） |

后端校验：`is_active=true` 且（`public` 或当前用户在 members 中）。

### 评论（需登录 + 有访问权限）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/sessions/{session_id}/comments` | 获取评论列表（支持 `?message_id=` 过滤） |
| POST | `/api/sessions/{session_id}/comments` | 发表评论 |
| PATCH | `/api/comments/{comment_id}` | 编辑评论（仅作者） |
| DELETE | `/api/comments/{comment_id}` | 删除评论（仅作者） |

### 共享追问（需登录 + 有访问权限）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/assistant/shared/{share_token}/follow-ups` | 接收追问并返回 `202`；请求先入服务端队列 |
| GET | `/api/assistant/shared/{share_token}` | 同时返回 `active_turn`、`pending_follow_ups` 与 `failed_follow_ups`，供所有查看者同步状态 |
| GET | `/api/assistant/shared/{share_token}/turns/{turn_id}/stream` | 按共享访问权限订阅 turn 的 SSE；公开分享支持匿名查看，成员分享要求成员身份 |
| GET | `/api/assistant/sessions/{session_id}` | 所有者会话详情通过 `active_turn_requested_by` 标识当前 turn 是否来自共享追问及其发起人 |
| POST | `/api/assistant/shared/{share_token}/follow-ups/{request_id}/rebuild-runtime` | 用户确认后重建失效的 Agent Runtime，并重跑原追问 |

执行规则：

1. 已有 `running` turn 时不启动新 turn；追问显示为“排队中”。
2. 当前 turn 完成或停止后，服务端按入队时间领取下一条追问。
3. 共享撤销时取消尚未执行的排队追问；已经运行的 turn 按普通 turn 生命周期结束。
4. 共享追问延续原会话的 Runtime 和上下文；共享本身因此代表允许有权成员在该上下文中提问，但不开放对 owner 个人目录的直接浏览接口。
5. 如果历史 Runtime 无法恢复，失败追问保持可见，不自动新建 Runtime；只有原追问者或会话所有者确认后，才清除旧 Runtime 引用并重跑原 turn。
6. 重建会话时使用已持久化的历史消息恢复上下文，不重复写入原追问；旧 Runtime 中未持久化的临时状态无法恢复，并须在确认框中明确告知用户。
7. turn 实时事件采用多订阅广播；分享者、追问者、被分享者和匿名公开查看者在各自权限范围内订阅同一份回复，不按身份拆分实时输出能力。共享 SSE 同时下发回答文本和脱敏后的 `activity/tool_use/skill_use`；工具过程只保留工具名、Skill 名与通用状态，不暴露工具参数、内部路径或原始 Runtime 载荷。

---

## 前端交互

### 共享入口
- 会话侧边栏/详情页增加"共享"按钮，仅所有者可见
- 点击弹出面板：切换 public/members 模式；members 模式下搜索并选择用户；显示当前共享链接，支持一键复制
- 保存 members 模式后，后端向本次新增的指定成员发送 Slack 私信，包含共享者和会话链接；重复保存未变更的成员不重复通知，Slack 发送失败不影响共享生效

### 共享列表页
- 导航增加"共享"入口，列出所有对当前用户可见的共享会话
- 每条显示：会话标题、共享者、共享时间、评论数
- public 会话对全员可见；members 类型只对被加入的成员显示

### 共享内容页
- 入口：从共享列表点击，或直接访问 `/#/shared/{share_token}`
- 共享内容页和分享者的原会话页都展示完整消息，并为每个用户问题显示实际提问者姓名；已登录的分享者与被分享者都可继续提问，匿名查看者需先登录
- 每 1.5 秒以 `no-store` 请求刷新会话状态和已完成回复，使多个查看者看到一致的 turn/队列状态
- active turn 存在时同时订阅共享 SSE，在最终消息落库前逐字渲染同一份回复；断线后从已持久化事件恢复并继续订阅
- 所有查看者都能看到同一轮的工具调用、Skill 使用和生成状态；共享页面沿用回答消息卡展示脱敏后的过程摘要
- 共享追问执行时，发起人看到“正在处理你的追问”，其他查看者看到“{发起人} 正在追问”；共享页及所有者的原会话页都锁定输入区
- 并发冲突中后到的请求展示为“排队中”
- 问答输入区仅以一行小号辅助文字说明分享双方都可参与；单轮串行与自动排队规则放在“发送追问”按钮的悬停提示中，不单独占用常驻说明行
- Runtime 无法恢复时展示失败卡片并弹出可取消的重建确认框；未经确认不得自动重建或自动重试
- 会话所有者与共享查看者从服务端同步 `has_active_turn`，运行标记不依赖发起请求的浏览器本地 stream
- turn 完成或停止且队列清空后重新开放输入区
- 页面右侧或底部显示评论区

### 评论区
- 消息级评论：每条消息旁有评论图标，点击展开该消息的评论
- 会话级评论：页面底部固定评论区
- 已登录用户可输入提交；自己的评论显示编辑/删除按钮
