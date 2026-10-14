# 知识库文件树、@索引与同步摘要设计

## 目标

- 输入框 `@` 文件候选要求本地即时过滤，避免每次击键请求后端。
- 知识库目录页顶部不再展示全路径，改为展示最近一次同步摘要。
- 目录内容、@ 文件索引共用后端文件树缓存；同步摘要、接口响应和前端缓存保持独立。

## 共用缓存：FileTreeCache

`WorkspaceBrowserService` 内维护 `FileTreeCache`，按知识库 scope 缓存业务可见文件树：

```
磁盘扫描
  -> FileTreeCache
      - requirements
      - business
      - code
  -> list_knowledge_children()
  -> list_knowledge_index()
```

缓存职责：

- 扫描知识库根目录，生成完整文件树。
- 统一过滤业务不可见内容：隐藏目录、系统目录、图片、附件、二进制文件、纯媒体目录。
- 使用 `time.monotonic()` 管理 TTL，建议 5 分钟。
- 缓存命中时目录接口和索引接口都不触发磁盘 I/O。
- 后台定时刷新重建，避免文件扫描阻断 Web 请求线程。

过滤口径：

- 跳过 `.git`、`.venv`、`node_modules`、`__pycache__` 等隐藏或系统目录。
- 跳过 `images`、`assets`、`attachments` 等纯媒体目录。
- 跳过 `.png`、`.jpg`、`.gif`、`.pdf`、`.zip`、`.pack`、`.idx` 等非业务文本文件。

## 缓存失效

同步任务完成后写入 `workspace/runtime/sync-state/{scope}.json`。

`FileTreeCache` 命中前轻量检查对应状态文件版本：

- 状态文件不存在：只按 TTL 判断。
- 状态文件 mtime 或 `synced_at` 变化：对应 scope 标记为待刷新。
- 状态文件未变化：继续使用缓存，直到 TTL 过期。

刷新策略：

- 后台任务定时检查过期或待刷新 scope，并异步重建缓存。
- 请求命中有效旧缓存时直接返回，不等待重建完成。
- 首次启动或无可用缓存时，请求可以返回空结果/加载态，后台完成后下次请求拿到新缓存。
- 同一 scope 同一时间只允许一个重建任务运行。

同步任务只负责写文件和同步状态，不直接调用缓存刷新逻辑，避免同步任务耦合 UI 缓存实现。

## 同步状态

同步摘要独立存储，不放进 `FileTreeCache`：

- `workspace/runtime/sync-state/requirements.json`
- `workspace/runtime/sync-state/code.json`

结构：

```json
{
  "synced_at": "2026-04-25T03:26:38Z",
  "added": 3,
  "updated": 12,
  "changed_files": [
    "Zendesk手动拉取/用户操作手册.md",
    "Zendesk手动拉取/API说明.md"
  ],
  "errors": 0
}
```

字段口径：

- `synced_at`：同步任务完成时间，存 UTC ISO，前端按本地时区展示为 `YYYY-MM-DD HH:mm:ss`。
- `added` / `updated`：本轮新增和更新文件数。
- `changed_files`：本轮新增或更新的相对路径。
- `errors`：本轮失败数量，仅记录，不影响基础摘要展示。

写入要求：使用临时文件加原子替换，避免前端读取半写入内容。

## 同步任务采集

ClickUp 同步：

- 成功写入新 Markdown：计入 `added` 和 `changed_files`。
- 成功覆盖已有 Markdown：计入 `updated` 和 `changed_files`。
- 跳过文件不计入 `changed_files`。
- 任务结束后写入 `requirements.json`。

代码同步：

1. pull 前执行 `git rev-parse HEAD`，记录 `old_head`。
2. 执行 `git pull`。
3. pull 成功后再次执行 `git rev-parse HEAD`，记录 `new_head`。
4. 若 `old_head != new_head`，执行 `git diff --name-only old_head..new_head` 获取变更文件。
5. 变更文件按 `仓库名/文件路径` 写入 `code.json`。

不解析 `git pull` 输出，因为它是人类可读日志，受 Git 版本、语言环境、pull 策略、rename 输出格式影响；`git diff --name-only` 更适合脚本消费。

## 后端接口

### 当前目录内容

将浅层目录接口语义调整为“children”，避免把浅层读取误称为完整 tree：

- `GET /api/knowledge/{knowledge_type}/children?path=...`

响应增加同步摘要：

```json
{
  "root": {},
  "default_file_path": null,
  "sync_summary": {
    "synced_at": "2026-04-25T03:26:38Z",
    "changed_count": 15,
    "sample_files": ["xxxxx.md", "yyyy.md", "zzzz.md"],
    "errors": 0
  }
}
```

实现上从 `FileTreeCache` 裁剪当前目录一层 children；从 `sync-state` 读取 `sync_summary`。

### @ 文件索引

- `GET /api/knowledge/index`

响应：

```json
{
  "prefixes": [
    "workspace/knowledge/requirements/docs/",
    "workspace/knowledge/business-docs/",
    "workspace/knowledge/code/"
  ],
  "files": [
    [0, "Admin/业务视频管理/需求.md"],
    [1, "01_trading_domain/01_spot.md"],
    [2, "api/trading/spot.py"]
  ]
}
```

- `files[i][0]`：scope 序号，对应 `prefixes` 下标。
- `files[i][1]`：去掉前缀后的相对路径，含文件名。
- 前端还原完整路径：`prefixes[s] + relpath`。
- 接口不返回冗余 `name` 字段，前端用 `relpath.split('/').pop()` 取文件名。

## 前端实现

### 目录页

目录页顶部副标题优先展示 `sync_summary`，不再展示 `node.path`。

展示规则：

- 无同步记录：`尚未完成同步`
- 有变更：`2026-04-25 11:26:38 更新 15 个文件，包括 xxxxx.md、yyyy.md、zzzz.md ...`
- 无变更：`2026-04-25 11:26:38 已同步，暂无文件更新`

目录请求保留前端 TTL 缓存：

- 根目录 children：5-10 秒。
- 子目录 children：30-60 秒。
- 页面停留期间不轮询；用户切换知识库、切换目录或重新进入页面时，过期后重新请求。

### @ 候选

用 Web Worker 后台加载索引，不阻塞主线程：

```
用户首次打开页面
  -> Worker 请求 GET /api/knowledge/index
  -> 主线程保存模块级单例索引

用户首次输入 @
  -> 立即展示 3 个 scope 选项

后续 @ 输入
  -> index 未就绪：只展示 scope 选项
  -> index 已就绪：本地 filter，按 scope 分组展示文件候选
```

过滤逻辑：`files.filter(([s, p]) => p.includes(query))`。索引在当前页面会话内复用，不与目录页请求缓存互相清理。

## 改动范围

| 位置 | 改动内容 |
|---|---|
| `WorkspaceBrowserService` | 新增 `FileTreeCache`；目录 children 和 @ index 从缓存派生 |
| `workspace.py` | 新增/调整目录 children 接口；新增 `GET /api/knowledge/index` |
| 同步任务 | 写入 `workspace/runtime/sync-state/{scope}.json` |
| `workspaceApi.js` | children 请求 TTL 缓存；新增 `getKnowledgeIndex()` |
| `AiAssistantPage.jsx` | `@` 触发 scope 与文件候选 |
| 目录页组件 | 顶部副标题展示同步摘要 |
