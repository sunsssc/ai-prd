# 自动技术评审设计文档

## 背景

当前需求文档从 ClickUp 同步到 `workspace/knowledge/requirements/` 后，研发需要主动进入 AI 助手并手动发起技术评审。自动技术评审要在需求新增或更新后生成一份可复核的技术初评，并以轻量标签提示用户查看。评审不新增一级菜单，不污染原始需求文件；它作为需求文件的附属记录落到文件系统中，并可被引用到 AI 对话继续加工。

---

## 核心概念

| 概念 | 说明 |
|------|------|
| 源需求文件 | ClickUp 同步得到的需求 Markdown，包含 `docs/` 下的文档页和 `tasks/` 下的任务文件 |
| 评审范围 | 一次技术评审实际读取的需求集合；普通 doc 只有自身，task 引用 doc 时包含 task 与被引用 doc |
| 扩展信息文件 | 每个源需求文件对应一个 JSON sidecar，记录评审等派生产物，不改写源需求正文 |
| 评审文件 | AI 生成的评审 Markdown，存放在 `workspace/knowledge/__reviews__/`，不出现在普通知识库目录树 |
| 未读评审标签 | 目录树和需求详情中的 `评` 标签；目录树只表示当前用户有未读评审 |
| 已阅状态 | 用户维度的运行时状态，保存在 `assistant.sqlite3`，不写入评审文件或扩展信息文件 |

---

## 文件结构

### 需求与扩展信息

```text
workspace/knowledge/requirements/
├── docs/
│   └── .../*.md
├── tasks/
│   └── .../*.md
└── __meta__/
    ├── docs/
    │   └── .../*.md.json
    └── tasks/
        └── .../*.md.json
```

扩展信息路径按源需求路径镜像生成：

| 源需求路径 | 扩展信息路径 |
|------|------|
| `workspace/knowledge/requirements/docs/A/B.md` | `workspace/knowledge/requirements/__meta__/docs/A/B.md.json` |
| `workspace/knowledge/requirements/tasks/C.md` | `workspace/knowledge/requirements/__meta__/tasks/C.md.json` |

### 评审记录

```text
workspace/knowledge/__reviews__/
└── tech-review/
    └── 2026/
        └── 05/
            └── review_20260516_103200_ab12cd.md
```

`__reviews__` 和 `__meta__` 属于系统目录。普通知识库目录树默认隐藏名称匹配 `^__.*__$` 的目录；当前实现已经隐藏下划线开头的知识库目录，后续只需把这个约定显式固化。

---

## 文件格式

### 扩展信息 JSON

扩展信息是源需求文件的附属索引，记录该需求有哪些评审文件。它是需求侧到评审侧的引用。

```json
{
  "source_path": "workspace/knowledge/requirements/docs/A/B.md",
  "source_hash": "sha256:...",
  "source_type": "requirement_doc",
  "reviews": [
    {
      "review_id": "review_20260516_103200_ab12cd",
      "review_type": "tech_review",
      "path": "workspace/knowledge/__reviews__/tech-review/2026/05/review_20260516_103200_ab12cd.md",
      "source_hash": "sha256:...",
      "source_path": "workspace/knowledge/requirements/tasks/C.md",
      "source_paths": [
        "workspace/knowledge/requirements/tasks/C.md",
        "workspace/knowledge/requirements/docs/A/B.md"
      ],
      "status": "completed",
      "risk_level": "medium",
      "created_at": "2026-05-16T10:32:00+08:00",
      "completed_at": "2026-05-16T10:33:20+08:00"
    }
  ],
  "latest_reviews": {
    "tech_review": "review_20260516_103200_ab12cd"
  },
  "updated_at": "2026-05-16T10:33:20+08:00"
}
```

`source_hash` 基于评审范围内的源需求正文计算，不包含扩展信息和评审记录。task 引用 doc 时，task 和 doc 的扩展信息可以记录同一个 `review_id`。

### 评审 Markdown

评审文件 frontmatter 强引用源需求文件和扩展信息文件。正文保留可被 AI 助手直接读取的 Markdown 报告。

```md
---
review_id: review_20260516_103200_ab12cd
review_type: tech_review
source_type: requirement_doc
source_path: workspace/knowledge/requirements/docs/A/B.md
source_paths: workspace/knowledge/requirements/docs/A/B.md
source_meta_path: workspace/knowledge/requirements/__meta__/docs/A/B.md.json
source_hash: sha256:...
skill: tech-review-prd
status: completed
risk_level: medium
created_at: 2026-05-16T10:32:00+08:00
completed_at: 2026-05-16T10:33:20+08:00
---

# 技术评审报告

## 来源需求

- 需求文档: `workspace/knowledge/requirements/docs/A/B.md`
- 内容快照 Hash: `sha256:...`
- 生成 Skill: `tech-review-prd`

...
```

暂不把已阅状态写入 frontmatter，因为已阅是用户维度状态，写文件会导致知识库文件频繁变化。

---

## 数据模型

新增表存入现有 `assistant.sqlite3`。

### requirement_review_reads（评审已阅状态）

| 字段 | 类型 | 说明 |
|------|------|------|
| read_id | str | PK |
| review_id | str | 评审文件 frontmatter 中的 `review_id` |
| review_path | str | 评审 Markdown 路径 |
| source_path | str | 源需求文件路径 |
| user_id | str | FK → users |
| read_at | datetime | 标记已阅时间 |

唯一约束：`UNIQUE(review_id, user_id)`。

### requirement_review_jobs（自动评审任务）

| 字段 | 类型 | 说明 |
|------|------|------|
| job_id | str | PK |
| source_path | str | 源需求文件路径 |
| source_hash | str | 触发评审时的需求内容 hash |
| review_type | str | 当前固定为 `tech_review` |
| skill_name | str | 当前固定为 `tech-review-prd` |
| status | str | `pending` / `running` / `completed` / `failed` |
| review_id | str? | 成功生成后写入 |
| review_path | str? | 成功生成后写入 |
| error_message | str? | 失败原因 |
| created_at | datetime | |
| started_at | datetime? | |
| completed_at | datetime? | |

评审正文和评审元信息以 Markdown 文件和需求扩展信息为事实来源；任务表只负责调度与失败重试。

---

## 生成流程

### 触发时机

ClickUp 同步完成后，现有后台任务已经能拿到 `changed_files`。自动技术评审只处理这些新增或更新的 `.md` 文件：

1. 过滤非需求正文文件、图片、附件、`__meta__` 目录。
2. 解析评审范围：task 中的本地 doc 链接会并入同一次评审；doc 变更时若被 task 引用，则以引用它的 task 作为评审主入口。
3. 计算评审范围 hash。
4. 读取对应扩展信息文件。
5. 若最新 `tech_review` 的 `source_hash` 等于当前 hash 且状态为 `completed`，不重复生成。
6. 否则创建 `requirement_review_jobs` 记录。

### 执行评审

后台 worker 消费 `pending` 任务：

1. 读取评审范围内的全部源需求 Markdown。
2. 读取 `workspace/.claude/skills/tech-review-prd/SKILL.md`。
3. 组装 prompt，要求 AI 按该 skill 输出技术评审报告。
4. 将结果写入 `workspace/knowledge/__reviews__/tech-review/YYYY/MM/`。
5. 更新评审范围内每个源需求对应的 `__meta__/*.md.json`；多个源需求指向同一个评审文件和 `review_id`。
6. 将任务标记为 `completed`，失败则写入 `failed` 和 `error_message`。
7. 如果主需求来源是 ClickUp Task，且 `CLICKUP_REVIEW_PUBLISH_ENABLED=true` 并已配置 `CLICKUP_API_TOKEN`、`CLICKUP_WORKSPACE_ID`、`CLICKUP_REVIEW_DOC_FOLDER_ID`，创建独立的 ClickUp 评审发布任务。该任务异步消费，先在共享 ClickUp Folder 下创建评审 Doc 和 Markdown Page，再在对应 Task 评论区添加评审文档引用；发布失败只更新发布任务状态并记录日志，不影响本地评审记录和未读提示。该开关默认关闭。

旧评审不覆盖。源需求更新后生成新评审，旧评审继续保留；详情页可显示历史记录。

自动评审由 `REQUIREMENT_REVIEW_AUTO_ENABLED` 控制，默认开启。设为 `false` 时，ClickUp 同步只更新本地需求文件和同步状态，不再自动创建 `requirement_review_jobs`；手动触发的 `/api/requirements/reviews/jobs` 和 `/api/requirements/reviews/rebuild` 不受影响。

---

## 权限规则

须已登录。

| 操作 | 普通用户 | 管理员 |
|------|:------:|:------:|
| 查看需求目录树中的评审标签 | ✓ | ✓ |
| 查看自己可访问需求的评审记录 | ✓ | ✓ |
| 标记自己的评审已阅 | ✓ | ✓ |
| 引用评审到 AI 对话 | ✓ | ✓ |
| 手动重新生成评审 | ✓ | ✓ |

当前系统没有按部门拆分知识库权限，评审访问继承源需求文件的可见性。

---

## API 设计

### 需求详情评审

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/requirements/reviews` | 按 `source_path` 查询某需求的评审记录 |
| GET | `/api/requirements/reviews/{review_id}` | 读取评审详情和 Markdown 内容 |
| POST | `/api/requirements/reviews/{review_id}/read` | 标记当前用户已阅 |
| POST | `/api/requirements/reviews/rebuild` | 对某需求手动重新生成技术评审 |

GET `/api/requirements/reviews?source_path=...` 响应示例：

```json
{
  "source_path": "workspace/knowledge/requirements/docs/A/B.md",
  "reviews": [
    {
      "review_id": "review_20260516_103200_ab12cd",
      "review_type": "tech_review",
      "path": "workspace/knowledge/__reviews__/tech-review/2026/05/review_20260516_103200_ab12cd.md",
      "status": "completed",
      "risk_level": "medium",
      "source_hash": "sha256:...",
      "is_read": false,
      "created_at": "2026-05-16T10:32:00+08:00"
    }
  ]
}
```

POST `/api/requirements/reviews/rebuild` 请求体：

```json
{
  "source_path": "workspace/knowledge/requirements/docs/A/B.md",
  "review_type": "tech_review"
}
```

### 知识库目录树

现有 `/api/knowledge/{knowledge_type}/children` 的节点响应增加评审标签字段。

```json
{
  "name": "B.md",
  "path": "workspace/knowledge/requirements/docs/A/B.md",
  "review_badge": {
    "type": "tech_review",
    "unread_count": 1,
    "latest_review_id": "review_20260516_103200_ab12cd"
  }
}
```

目录节点的 `review_badge` 由子孙文件聚合得到。只要当前用户在该目录下有任一未读已完成评审，目录显示 `评`。

### 对话上下文引用

评审引用复用现有 `AssistantContextItem` 结构，新增约定 `source_type=review_file`。

```json
{
  "context_key": "review:review_20260516_103200_ab12cd",
  "label": "技术评审：B",
  "source_type": "review_file",
  "source_uri": "workspace/knowledge/__reviews__/tech-review/2026/05/review_20260516_103200_ab12cd.md",
  "metadata": {
    "review_id": "review_20260516_103200_ab12cd",
    "review_type": "tech_review",
    "source_path": "workspace/knowledge/requirements/docs/A/B.md",
    "risk_level": "medium"
  }
}
```

只引用评审时，AI 读取评审文件；选择“引用需求 + 最新评审”时，同时挂载源需求文件和评审文件两个上下文对象。

---

## 前端交互

### 需求目录树

- 有未读技术评审的需求文件，在文件名旁显示 `评` 标签。
- 有未读评审的子孙文件时，父目录旁显示 `评` 标签。
- 目录树中的 `评` 只做状态提示，不作为独立点击入口；点击文件仍进入需求详情。
- 标记已阅后，当前用户目录树中的 `评` 消失。

不新增一级菜单。评审跟随需求对象出现。

### 需求详情

- 标题区显示评审入口。
- 有未读评审时显示醒目的 `评` 标签。
- 已阅后仍保留入口，文案可显示为“评审记录”或弱化的 `评`。
- 点击入口打开评审面板。

评审面板展示：

| 区域 | 内容 |
|------|------|
| 元信息 | 评审类型、状态、风险等级、生成时间、基于的需求 hash |
| 摘要 | 主要问题和风险结论 |
| 正文 | 完整 Markdown 技术评审报告 |
| 操作 | 标记已阅、引用到对话、引用需求 + 评审、重新评审 |

### 引用到对话

评审面板提供两个入口：

- `引用评审到对话`：只挂载评审文件。
- `引用需求 + 评审到对话`：同时挂载源需求文件和最新评审文件。

进入 AI 助手后，上下文 chip 显示为“技术评审：xxx”，消息来源区也按评审记录展示，不暴露为普通知识库文件。

---

## 目录树标签规则

文件节点显示 `评` 的条件：

| 条件 | 说明 |
|------|------|
| 存在最新 `tech_review` | 来源于源需求扩展信息 |
| 评审状态为 `completed` | 失败或运行中不显示 `评` |
| 当前用户未读 | 查询 `requirement_review_reads` |
| 评审 `source_hash` 等于当前源需求 hash | 避免旧评审误提示 |

目录节点显示 `评` 的条件：任一后代文件节点满足上述条件。

评审失败第一版不在目录树显示错误标签。失败状态只在需求详情评审入口中展示，避免目录树符号过多。

---

## 实现边界

- 不新增“评审”一级菜单。
- 不把评审链接写入 ClickUp 同步得到的源需求 Markdown。
- 不把已阅状态写入评审文件或需求扩展信息。
- 不自动写回 ClickUp、不自动通知 Slack、不自动改需求状态。
- 第一版只自动生成 `tech-review-prd` 类型评审。
- 评审是 AI 初评记录，不代表人工审批结论。

---

## 落地步骤

1. 新增路径与文件工具：生成源需求 hash、扩展信息路径、评审文件路径。
2. 在 ClickUp 同步完成后，根据 `changed_files` 创建自动评审任务。
3. 实现评审 worker，读取 `tech-review-prd` skill 并写入评审 Markdown。
4. 更新 `requirements/__meta__/**/*.md.json`，形成需求到评审的引用。
5. 新增已阅表和评审 API。
6. 扩展知识库树节点响应，返回当前用户的 `review_badge`。
7. 前端目录树和需求详情显示 `评` 标签与评审面板。
8. 将评审文件作为 `review_file` 上下文引用到 AI 对话。
