# AI 助手性能优化任务清单

## 1. 文档信息

- 项目名称：ai-prd
- 文档名称：AI 助手性能优化任务清单
- 文档版本：V1.0
- 文档日期：2026-04-28
- 文档状态：草案

---

## 2. 背景

当前前端接口偶发出现明显延迟波动：部分请求几十毫秒完成，部分请求需要数秒甚至十几秒。现象在后端刚重启后通常较轻，运行一段时间后更容易出现。

已知高风险链路包括：

- AI 助手 SSE 对话过程中持续产生 runtime event。
- 会话详情接口一次性读取 messages、turns、citations、runtime events、context requests、mounts 等数据。
- 知识库文件树涉及大量本地文件扫描和缓存刷新。
- ClickUp / 代码同步任务会定期写入知识库文件和 sync-state。

本任务清单用于沉淀性能诊断结论，并按优先级拆解后续优化工作。

---

## 3. 现状判断

### 3.1 已确认事实

- 部署配置中 uvicorn 当前为单 worker。
- `SQLiteAssistantStore` 和 `SQLiteAuthStore` 都使用进程内 `Lock` 串行化写操作。
- AI 对话流中每收到一个 runtime event 都会调用 `append_runtime_event` 写入 SQLite。
- AI 对话等待 runtime 输出时，每 10 秒 idle heartbeat 也会写入 runtime event。
- 会话详情接口会加载较多附属数据，且 latest trace 会读取 runtime events。
- 知识库缓存默认 TTL 为 300 秒，首次无缓存时会同步构建文件树。
- ClickUp 同步和代码同步运行在后台线程中，不直接阻塞 event loop，但会带来文件系统和网络负载。

### 3.2 需要澄清的判断

- Assistant Store 和 Auth Store 的 Python 锁不是同一个锁，Assistant 写锁不会直接阻塞 Auth 写锁。
- ClickUp 同步不写 assistant/auth SQLite，因此不会竞争 Assistant Store 的 `_lock`。
- 知识库已有缓存过期或 sync-state 变化时，当前实现会后台重建并返回旧缓存；不是每次同步后都阻塞下一个请求。
- 直接增加 uvicorn workers 会让每个 worker 都启动后台同步任务，并导致进程内 SSE 状态不共享，不能作为第一步优化。

---

## 4. 优化目标

1. 降低 AI 对话流对 SQLite 的高频写压力。
2. 降低会话详情接口的默认读取范围和序列化成本。
3. 让知识库缓存刷新更可控，避免请求路径承担重扫描。
4. 建立可定位原因的性能日志，而不仅是记录 URL 总耗时。
5. 在不引入多进程状态一致性问题的前提下逐步优化吞吐。

---

## 5. 非目标

- 不在本阶段把 SQLite 替换为 PostgreSQL。
- 不在本阶段直接启用多 uvicorn workers。
- 不在本阶段引入 Celery、APScheduler 等独立调度系统。
- 不在本阶段改变前端 SSE 协议。
- 不为了未来可能场景增加兼容层或双写方案。

---

## 6. 优先级任务

### P0-1. 降低 runtime event 写库频率

问题：

- 当前每个 runtime event 都立即写库。
- Agent 运行期间可能产生大量 activity、delta、usage、tool 相关事件。
- 同步 SQLite 写发生在 async 执行路径中，会短暂阻塞 event loop。

建议方案：

1. 增加 `append_runtime_events` 批量写方法。
2. 对 runtime event 做分级持久化：
   - 必须持久化：`message`、`usage`、`error`、tool activity、重要 lifecycle event。
   - 可降频：idle heartbeat。
   - 默认不持久化或合并：高频 delta。
3. SSE 推送仍保持实时，数据库只保存恢复和审计需要的关键事件。

验收标准：

- 单轮 Agent 对话的 runtime event 写库次数明显下降。
- SSE 体验不退化。
- 已完成 turn 的 trace 仍能展示关键执行过程。
- 慢请求日志中不再频繁出现由 runtime event 写入引发的抖动。

### P0-2. 收窄会话详情接口默认读取范围

问题：

- `GET /api/assistant/sessions/{session_id}` 当前承担会话详情、活跃 turn、latest trace、引用、上下文请求等多个职责。
- 会话越长，默认响应越重。
- latest trace 只有失败或运行中状态才真正必要。

建议方案：

1. 会话详情默认返回 session、messages、mounts、active_turn。
2. latest trace 改为按需接口读取，或仅当 latest turn 未完成/失败时读取。
3. context requests、citations 优先批量查询，避免按 turn 循环查询。
4. 前端仅在用户展开 trace 或需要恢复运行中状态时请求 trace。

验收标准：

- 会话详情接口在普通历史会话上稳定低于 500ms。
- 长会话不会因为 runtime events 积累而默认变慢。
- trace 页面或失败详情仍可完整读取执行事件。

### P0-3. 保留并完善慢请求原因日志

问题：

- 只记录 URL 总耗时不足以定位原因。
- 需要区分查询、序列化、缓存、文件扫描、后台同步等阶段。

建议方案：

1. 全局 middleware 记录超过阈值的 method、path、status、elapsed_ms。
2. 会话列表记录 query_ms、serialize_ms、session_count。
3. 会话详情记录 messages、turns、citations、trace、context、mounts、serialize 各阶段耗时。
4. 知识库目录记录 list_ms、serialize_ms、node_count、file_count。
5. 同步任务记录单次同步耗时、变更数量、失败数量。

验收标准：

- 任意慢请求都能通过日志判断至少一个主要方向：DB、序列化、文件树、同步任务、外围依赖。
- 日志不会输出用户消息正文、文件正文、token、cookie 等敏感内容。

---

## 7. P1 任务

### P1-1. 知识库缓存主动刷新

问题：

- 缓存 TTL 和 sync-state 变化会触发刷新。
- 首次无缓存时请求会承担构建成本。

建议方案：

1. 后端启动后主动后台预热 requirements、business、code 三类缓存。
2. ClickUp / 代码同步完成后，主动调度对应 scope 的后台缓存重建。
3. 提高默认 TTL，例如从 300 秒调整为 1800 秒。
4. 保持请求路径优先返回旧缓存，不等待重建。

验收标准：

- 知识库目录接口在缓存已存在时稳定返回。
- 同步完成后用户看到的数据最终更新，但请求不承担完整扫描成本。

### P1-2. 认证短 TTL 缓存

问题：

- 每个需要鉴权的接口都会读取 session 和 user。
- 高频前端请求下会产生大量小查询。

建议方案：

1. 对 `get_current_user(token)` 结果增加 30 秒进程内 TTL 缓存。
2. logout、session 过期、用户变更时清理对应缓存。
3. 缓存值只保存必要用户字段，不保存原始 token 明文到日志。

验收标准：

- 高频页面加载时 auth SQLite 查询量下降。
- logout 后旧 session 不会继续通过鉴权。

### P1-3. 会话列表查询优化

问题：

- 会话列表通过聚合和子查询获取 message_count、last_message_preview。
- 数据量增长后列表接口可能变慢。

建议方案：

1. 在 `assistant_sessions` 中维护 last_message_preview 和 message_count 冗余字段。
2. `start_turn`、完成 turn、删除 session 时同步更新这些字段。
3. 列表接口避免扫描 messages 表。

验收标准：

- 会话列表接口耗时不随历史消息总量线性增长。
- message_count 和 last_message_preview 与真实消息保持一致。

---

## 8. P2 任务

### P2-1. Direct LLM Client 连接复用

问题：

- `AnthropicAssistantLLMClient` 每次请求都会创建新的 `httpx.AsyncClient`。
- 如果未来使用 direct LLM client，会增加 TCP/TLS 建连成本。

建议方案：

1. 将 `httpx.AsyncClient` 作为长期实例管理。
2. 在应用 shutdown 时关闭 client。
3. 仅在确认 direct LLM client 是真实路径时实施。

验收标准：

- direct LLM 调用复用连接池。
- 应用关闭时无未关闭 client 警告。

### P2-2. WAL checkpoint 策略

问题：

- SQLite WAL 文件长期增长可能影响读性能和磁盘占用。

建议方案：

1. 增加轻量定期 checkpoint。
2. 避免在高峰请求期间执行 `TRUNCATE`。
3. 结合日志观察 WAL 文件增长趋势后再定阈值。

验收标准：

- WAL 文件大小稳定。
- checkpoint 不引入新的请求抖动。

---

## 9. 暂缓事项

### 多 uvicorn workers

暂缓原因：

- 多 worker 会重复启动 ClickUp 和代码同步任务。
- `AssistantService._turn_streams` 是进程内状态，多 worker 下 resume SSE 可能打到不同进程。
- 需要先解决后台任务单实例化和 SSE 状态共享或粘性路由。

前置条件：

1. 后台同步任务只允许一个实例运行。
2. SSE resume 能跨 worker 找到运行中 turn，或前端请求能稳定路由到同一 worker。
3. SQLite 写入策略已经降频，避免多进程放大写竞争。

---

## 10. 推荐实施顺序

1. 实施 P0-1：runtime event 降频与批量写。
2. 实施 P0-2：会话详情拆分，trace 按需加载。
3. 观察慢请求日志，确认主要瓶颈是否下降。
4. 实施 P1-1：知识库缓存预热和同步后主动刷新。
5. 根据日志数据决定是否实施 P1-2、P1-3。
6. 暂不启用多 worker，除非完成对应前置条件。

---

## 11. 观测指标

需要持续观察以下日志和指标：

- `慢请求`：确认 URL、状态码、总耗时。
- `慢会话详情`：确认具体慢在 messages、trace、citations、context_requests 还是 serialize。
- `慢知识库目录`：确认慢在 list 还是 serialize。
- `知识库缓存重建完成`：确认节点数、文件数、重建耗时。
- `代码同步完成` / `ClickUp 同步完成`：确认同步耗时是否与慢请求时间重叠。
- SQLite 文件和 WAL 文件大小。
- 单轮 Agent 对话持久化 runtime event 数量。

---

## 12. 风险与回滚

### runtime event 降频风险

风险：

- trace 展示信息减少。
- resume 已完成 turn 时可回放事件减少。

控制方式：

- 不删除 message、usage、error、tool activity 等关键事件。
- 前端 SSE 实时推送不受影响。
- 保留配置开关控制是否持久化 delta。

### 会话详情拆分风险

风险：

- 前端部分视图依赖 latest trace。

控制方式：

- 先保留接口字段兼容一版，前端切换后再移除默认加载。
- 对运行中和失败 turn 保持 trace 可见。

### 缓存主动刷新风险

风险：

- 刷新任务与同步任务同时运行，增加文件系统压力。

控制方式：

- 同一 scope 同一时间只允许一个缓存重建任务。
- 请求路径始终优先返回旧缓存。

