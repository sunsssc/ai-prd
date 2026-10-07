# 知识库索引缓存重构计划

## 1. 文档信息

- 项目名称：ai-prd
- 文档名称：知识库索引缓存重构计划
- 文档版本：V1.0
- 文档日期：2026-05-17
- 文档状态：草案

---

## 2. 背景

知识库目录搜索和 AI 助手输入框 `@` 文件引用当前已经共用后端 `GET /api/knowledge/index` 全量索引。后端索引来源清晰，但前端存在多处消费和缓存逻辑：应用启动会预热索引，知识库页面维护页面级索引状态，AI 助手又额外维护模块级快照。这个结构容易造成缓存过期口径不一致，也让搜索匹配、scope 元数据和索引解析逻辑在多个页面中漂移。

本计划用于把索引缓存职责收敛到公共模块，在不改变现有后端接口的前提下，先统一前端缓存、解析和搜索行为。

---

## 3. 当前实现

### 3.1 后端索引

| 项目 | 当前实现 |
| --- | --- |
| 公共接口 | `GET /api/knowledge/index` |
| 后端入口 | `source/backend/app/api/workspace.py:get_knowledge_index` |
| 构建服务 | `WorkspaceBrowserService.list_knowledge_index()` |
| 索引范围 | `requirements`、`business`、`code` |
| 返回字段 | `prefixes`、`directories`、`files` |
| 后端缓存 | `WorkspaceBrowserService._knowledge_cache[knowledge_type]` |
| 失效依据 | 缓存 TTL、`workspace/runtime/sync-state/{scope}.json` 签名变化 |

后端是一套索引 API，内部按知识库 scope 分片缓存。目录接口和索引接口都复用同一份文件树缓存。

### 3.2 前端公共 API

| 项目 | 当前实现 |
| --- | --- |
| 调用入口 | `source/frontend/src/services/workspaceApi.js:getKnowledgeIndex()` |
| 缓存工具 | `apiWithCache(url, ttl, fn)` |
| 缓存键 | `/api/knowledge/index` |
| TTL | 300000ms |
| 失败处理 | 请求失败时删除缓存，下次重新请求 |

`apiWithCache` 已经能复用已完成结果，也能合并并发请求。

### 3.3 应用启动预热

| 项目 | 当前实现 |
| --- | --- |
| 位置 | `source/frontend/src/app/App.jsx` |
| 触发 | 用户登录后 idle task |
| 行为 | 调用 `getKnowledgeIndex()`，只预热公共缓存 |

预热只依赖公共 API 缓存，不维护独立业务状态。

### 3.4 知识库页面使用

| 用途 | 当前实现 |
| --- | --- |
| 页面状态 | `knowledgeIndex`、`knowledgeIndexLoading`、`knowledgeIndexError` |
| 加载时机 | 树加载后 idle 预取；用户输入搜索词时立即加载 |
| 搜索对象 | 当前知识库 scope 内的目录和文件 |
| 匹配字段 | name + path |
| 匹配规则 | `toLocaleLowerCase("zh-CN")` 后 includes |
| 结果形态 | 构造搜索结果树，展开命中路径 |
| 额外用途 | 用索引补全目录 `child_count` |

知识库页面需要目录和文件两类索引数据，并且需要把扁平索引还原为局部树结构。

### 3.5 AI 助手 `@` 使用

| 用途 | 当前实现 |
| --- | --- |
| 页面状态 | `indexPayload`、`atSuggestions` |
| 额外缓存 | 模块级 `knowledgeIndexPromise`、`knowledgeIndexSnapshot` |
| 加载时机 | 助手页面 mount 后加载一次 |
| 搜索对象 | 全部 scope 的文件，不含目录 |
| 匹配字段 | relative path |
| 匹配规则 | 原始字符串 includes |
| 结果数量 | 最多 8 个 |
| 选中结果 | 转成 `referencedSources`，发送时进入 `context_requests` |

AI 助手的 `knowledgeIndexSnapshot` 不受公共 API TTL 控制，页面生命周期内不会主动过期。

---

## 4. 问题

| 问题 | 影响 |
| --- | --- |
| AI 助手有永不过期模块级快照 | 知识库同步后，`@` 文件候选可能继续展示旧索引 |
| 索引解析散落在页面组件 | `prefixes/directories/files` 结构变化会同时影响多个页面 |
| scope 元数据重复维护 | label、tone、rootPath、route prefix 容易不一致 |
| 搜索匹配规则不一致 | 知识库搜索和 `@` 候选对大小写、中文 locale 的表现不同 |
| 加载状态各自实现 | 错误文案、重试策略、缓存命中加载态难以统一 |

---

## 5. 重构目标

1. 索引数据仍然只通过 `GET /api/knowledge/index` 获取。
2. 删除 AI 助手模块级永不过期索引快照。
3. 前端统一索引加载、解析、过滤和路径转换逻辑。
4. 知识库搜索和 AI `@` 使用同一套搜索归一化规则。
5. 不在本阶段新增后端搜索接口，不改变现有接口响应结构。

---

## 6. 非目标

- 不改造后端 `WorkspaceBrowserService` 的缓存实现。
- 不引入新的前端状态管理库。
- 不把路径搜索升级为正文全文搜索。
- 不为旧索引结构增加兼容分支。
- 不新增 `/api/knowledge/search`，除非后续确认前端过滤已经成为性能瓶颈。

---

## 7. 推荐方案

### 7.1 新增前端索引工具模块

新增 `source/frontend/src/utils/knowledgeIndex.js`。

| 函数 | 说明 |
| --- | --- |
| `normalizeKnowledgeSearchText(value)` | 统一搜索归一化 |
| `getKnowledgeIndexScope(indexPayload, rootPath)` | 根据 root path 获取 scopeIndex 和 prefix |
| `buildKnowledgeFullPath(indexPayload, scopeIndex, relativePath)` | 把 scopeIndex + relativePath 转成完整知识库路径 |
| `listKnowledgeFiles(indexPayload, scopeId?)` | 返回标准化文件记录 |
| `listKnowledgeDirectories(indexPayload, scopeId?)` | 返回标准化目录记录 |
| `matchKnowledgeFiles(indexPayload, query, options)` | 给 AI `@` 使用的文件候选过滤 |
| `buildDirectoryChildCountMap(treeRoot, indexPayload)` | 给知识库树补全 child_count |
| `buildKnowledgeSearchTree(treeRoot, indexPayload, query)` | 给知识库页面构造搜索结果树 |

标准化文件记录：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `scopeIndex` | number | 后端索引中的 scope 下标 |
| `scopeId` | string | `requirements`、`business`、`code` |
| `relativePath` | string | scope 内相对路径 |
| `fullPath` | string | `workspace/knowledge/...` 完整路径 |
| `name` | string | 文件名 |

### 7.2 新增索引加载 hook

新增 `source/frontend/src/hooks/useKnowledgeIndex.js`。

| 返回值 | 说明 |
| --- | --- |
| `indexPayload` | 当前索引数据 |
| `loading` | 是否正在加载 |
| `error` | 加载错误 |
| `ensureIndex()` | 确保索引已加载 |
| `reloadIndex()` | 清理公共缓存后重新加载 |

hook 内部只调用 `getKnowledgeIndex()`，不再增加新的长生命周期快照。

### 7.3 统一 scope 配置

新增 `source/frontend/src/data/knowledgeScopes.js`。

| 字段 | 说明 |
| --- | --- |
| `id` | scope 标识 |
| `label` | UI 展示标签 |
| `tone` | 前端色彩语义 |
| `rootPath` | 知识库根路径 |
| `routeVisible` | 是否在侧边知识库导航展示 |

AI 助手、知识库页面、应用路由引用这一份配置。

---

## 8. 分步实施计划

### Step 1. 抽取 scope 前端配置

改动：

- 新增 `source/frontend/src/data/knowledgeScopes.js`。
- 从 `AiAssistantPage.jsx` 移出 `knowledgeScopes`、`knowledgeScopeMap`、`defaultKnowledgeScopeIds`。
- 从 `KnowledgePage.jsx` 移出 `knowledgeTone`、`knowledgeRoutePrefixes`。
- 从 `App.jsx` 移出 `CITATION_SCOPE_PREFIXES` 和 `visibleKnowledgeTypes`。

验收：

- AI 助手知识范围标签不变。
- 知识库侧边导航不变。
- 引用路径跳转不变。

### Step 2. 抽取索引纯函数

改动：

- 新增 `source/frontend/src/utils/knowledgeIndex.js`。
- 先迁移无副作用函数：路径拼接、scope 查找、搜索文本归一化、文件候选过滤。
- `AiAssistantPage.jsx` 改用 `matchKnowledgeFiles()` 生成 `@` 候选。

验收：

- 输入 `@文件名` 的候选结果与当前一致，大小写匹配规则统一为归一化后 includes。
- 裸 `@` 的 scope 候选不变。
- 文件引用写入 `referencedSources` 的结构不变。

### Step 3. 删除 AI 助手模块级索引快照

改动：

- 删除 `knowledgeIndexPromise`。
- 删除 `knowledgeIndexSnapshot`。
- 助手页面 mount 时直接通过公共加载逻辑获取索引。
- 保留 `indexPayload` 页面状态，用于渲染候选和加载中提示。

验收：

- 同一时间并发打开页面仍只发起一次 `/api/knowledge/index` 请求。
- 公共 `apiWithCache` TTL 到期后，AI `@` 能拿到新索引。
- 请求失败后再次输入 `@` 可以重新加载。

### Step 4. 引入 `useKnowledgeIndex`

改动：

- 新增 `source/frontend/src/hooks/useKnowledgeIndex.js`。
- `App.jsx` 预热改用 hook 之外的 `getKnowledgeIndex()` 保持简单。
- `KnowledgePage.jsx` 和 `AiAssistantPage.jsx` 使用同一个 hook。
- 统一 loading/error 处理口径。

验收：

- 知识库搜索首次输入时仍有加载状态。
- AI `@` 面板仍能显示“索引加载中”。
- 已有 `apiWithCache` 缓存命中时不出现多余加载闪烁。

### Step 5. 迁移知识库搜索树构造

改动：

- 把 `buildDirectoryChildCountMap()` 和 `buildKnowledgeSearchTree()` 移入 `utils/knowledgeIndex.js`。
- `KnowledgePage.jsx` 只保留页面状态、事件处理和渲染逻辑。

验收：

- 搜索目录和文件结果不变。
- 搜索结果点击后仍能加载真实目录并定位节点。
- 目录 `child_count` 展示不退化。

### Step 6. 增加测试覆盖

改动：

- 为 `utils/knowledgeIndex.js` 增加前端单元测试。
- 覆盖 scope 查找、完整路径拼接、文件候选过滤、目录 child_count、搜索树构造。
- 保留后端现有 `/api/knowledge/index` 测试。

验收：

- `pnpm` 测试通过。
- `@` 候选和知识库搜索核心逻辑可脱离页面组件验证。

---

## 9. 风险和处理

| 风险 | 处理 |
| --- | --- |
| 搜索匹配规则统一后结果顺序轻微变化 | 保持原始索引顺序，只有大小写归一化变化 |
| 删除 AI 快照后用户感知到加载 | 依赖 App idle 预热和 `apiWithCache`，正常路径仍命中缓存 |
| 工具函数迁移引入路径拼接错误 | 用标准化记录和单元测试覆盖 scope prefix 场景 |
| scope 配置迁移遗漏引用点 | 用 `rg "knowledgeScopes\\|knowledgeTone\\|knowledgeRoutePrefixes\\|CITATION_SCOPE_PREFIXES"` 检查 |

---

## 10. 验证清单

### 10.1 静态检查

- 搜索确认没有残留的 AI 模块级索引快照。
- 搜索确认 scope 配置只从公共模块导入。
- 搜索确认 `prefixes/directories/files` 解析逻辑集中在 `utils/knowledgeIndex.js`。

### 10.2 前端行为

- 登录后应用仍会 idle 预热索引。
- 进入知识库页面后，目录树正常加载。
- 搜索目录名可以命中目录。
- 搜索文件名可以命中文件。
- 点击搜索结果可以展开真实路径并展示内容。
- AI 助手输入裸 `@` 展示三个知识范围。
- AI 助手输入 `@优惠券` 展示文件候选。
- 选择文件后发送消息，用户消息仍展示引用对象。

### 10.3 后端行为

- `GET /api/knowledge/index` 响应结构不变。
- `GET /api/knowledge/{knowledge_type}/children` 行为不变。
- 同步状态变化后，后端索引缓存仍按原有机制刷新。

---

## 11. 推荐落地顺序

1. 先做 Step 1 和 Step 2，把配置和纯函数抽出来。
2. 再做 Step 3，删除 AI 助手永不过期快照。
3. 然后做 Step 4 和 Step 5，把页面状态和索引算法彻底分离。
4. 最后补 Step 6 测试，锁住索引结构和搜索行为。

这个顺序能让每一步都有明确回归面，且不需要修改后端接口。
