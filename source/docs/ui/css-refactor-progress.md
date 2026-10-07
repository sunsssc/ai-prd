# CSS 精简计划与进度记录

更新时间：2026-05-19

## 背景

当前前端页面数量不多，但手写 CSS 总量接近 6k 行。已确认主要膨胀来源不是简单的死代码，而是多轮 UI 与移动端修补后形成的重复覆盖、断点分散和组件边界混杂。

本项目是新项目，后续 CSS 精简应继续遵守项目规则：不要为了猜测中的旧场景保留兼容层、fallback、未使用的通用类或双轨样式。只有 JSX 中真实使用、设计系统明确需要、或用户明确要求的样式才保留。

## 初始基线

精简前统计：

| 文件 | 行数 |
| --- | ---: |
| `source/frontend/src/styles/global.css` | 9 |
| `source/frontend/src/styles/modules/assistant.css` | 1241 |
| `source/frontend/src/styles/modules/auth.css` | 127 |
| `source/frontend/src/styles/modules/controls.css` | 157 |
| `source/frontend/src/styles/modules/foundation.css` | 108 |
| `source/frontend/src/styles/modules/layout.css` | 539 |
| `source/frontend/src/styles/modules/responsive.css` | 1600 |
| `source/frontend/src/styles/modules/sessions.css` | 920 |
| `source/frontend/src/styles/modules/workspace-collapsed.css` | 25 |
| `source/frontend/src/styles/modules/workspace.css` | 1215 |
| **总计** | **5941** |

主要判断：

- `responsive.css` 单文件 1600 行，且存在多段重复的 `max-width: 840px`、`480px` 覆盖，是首要整理对象。
- `assistant.css`、`workspace.css` 各 1200 行左右，混合了页面布局、markdown、弹窗、表单、工具栏、状态组件等多类职责。
- 直接未使用的 class 不多，大量 class 是动态拼接，如 `badge-${tone}`、`message-card-${role}`、`context-chip-${tone}`、`child-card-${kind}`，不能只靠字符串搜索大删。

## 分轮计划

### 第一轮：整理响应式层

目标：

- 合并散落的相同断点块。
- 移除重复的 `@supports (height: 100dvh)` 兜底。
- 将不属于响应式职责的基础样式移回对应模块。
- 保持 JSX 行为不变。

状态：已完成。

### 第二轮：收敛 controls 通用控件

目标：

- 删除没有实际 JSX 调用的泛化样式。
- 合并按钮变体重复声明。
- 保留真实使用的 `button`、`badge`、`toolbar-icon-button` 等。

状态：已完成。

### 第三轮：模块内部重复合并

目标：

- 合并 `assistant.css` 内 markdown、消息操作按钮、composer 图片和队列按钮的重复声明。
- 合并 `workspace.css` 内 tree、review/doc-update、markdown document 的重复声明。
- 合并 `sessions.css` 中完全相同的会话行声明。
- 删除已经被基础样式覆盖的无效选择器。

状态：已完成。

### 第四轮：验证与记录

目标：

- 运行前端构建。
- 使用本地页面做桌面与移动端冒烟检查。
- 记录精简后的指标与剩余风险。

状态：已完成。

## 当前完成内容

本轮修改文件：

- `source/frontend/src/styles/modules/responsive.css`
- `source/frontend/src/styles/modules/assistant.css`
- `source/frontend/src/styles/modules/workspace.css`
- `source/frontend/src/styles/modules/controls.css`
- `source/frontend/src/styles/modules/layout.css`
- `source/frontend/src/styles/modules/sessions.css`

接续轮修改文件：

- `source/frontend/src/styles/global.css`
- `source/frontend/src/styles/modules/assistant.css`
- `source/frontend/src/styles/modules/markdown.css`
- `source/frontend/src/styles/modules/workspace.css`
- `source/frontend/src/styles/modules/document-detail.css`
- `source/frontend/src/styles/modules/responsive.css`
- `source/frontend/src/styles/modules/workspace-tree.css`
- `source/frontend/src/styles/modules/workspace-collapsed.css`
- `source/frontend/src/styles/modules/skill.css`

已完成的具体动作：

- `responsive.css`
  - 合并部分 `840px` 与 `480px` 断点规则。
  - 删除重复的 `100dvh` 支持检测块。
  - 删除移动端下不再适用的旧 workspace 双行布局覆盖。
  - 将 `child-card-button`、`child-card-folder` 的基础样式移回 `workspace.css`。
  - 将部分多选择器标题规则压缩为 `:where()` / `:is()`。

- `controls.css`
  - 删除未发现实际 JSX 调用的 `.chip`、`.chip-dark`、`.chip-signal`、`.chip-sand`、`.chip-olive`。
  - 合并 `.button-primary` 与 `.button-send` 的重复色彩和 hover 规则。

- `assistant.css`
  - 删除未必要的 `.message-card-assistant` 覆盖。
  - 删除未使用的 `.chip-row` 组合。
  - 合并 markdown 标题、段落、列表选择器。
  - 合并消息反馈按钮成功/失败状态重复样式。
  - 合并 composer 图片 chip 与队列删除按钮的重复样式。

- `workspace.css`
  - 接收 `child-card-button`、`child-card-folder` 基础样式。
  - 合并 tree 节点公共 grid 声明。
  - 合并 review/doc-update 空状态按钮声明。
  - 合并 document markdown 的多选择器 margin 规则。

- `layout.css`
  - 删除已无实际效果的 `.chip` hover/transition 组合。
  - 删除被 `assistant.css` 覆盖且无必要保留的 `.message-avatar` 基础组合。

- `sessions.css`
  - 合并 `recent-session-row` 与 `history-session-row` 的重复声明。

接续轮完成的具体动作：

- 新增 `markdown.css`，承接 `MarkdownRenderer` 的通用样式，包括标题、列表、引用、代码块、表格、图片、图表、文档元信息和 streaming 光标。
- 从 `assistant.css` 移出 markdown 渲染器样式，使 assistant 模块回到聊天页面布局、消息和 composer 相关职责。
- 在 `global.css` 中引入 `markdown.css`，并保持其位于 `workspace.css` 之前，确保文档 markdown 的 workspace 专属覆盖仍按原顺序生效。
- 新增 `document-detail.css`，承接文档详情面板、预览工具栏、引用菜单、编辑区、文档 markdown variant 和子卡片的基础样式。
- 从 `workspace.css` 移出文档详情相关样式，使 workspace 模块更聚焦树、review/doc-update 和页面框架。
- 将 workspace 文档详情相关移动端覆盖从 `responsive.css` 移入 `document-detail.css`，减少全局响应式补丁体积。
- 新增 `workspace-tree.css`，承接 workspace shell、tree pane、tree search、tree node、未读标记、节点操作按钮和移动端目录抽屉样式。
- 删除独立的 `workspace-collapsed.css`，将折叠状态规则并入 `workspace-tree.css`。
- 将 tree/sidebar 相关移动端覆盖和 reduced-motion 覆盖从 `responsive.css` 移入 `workspace-tree.css`。
- 新增 `skill.css`，承接 Skill 创建入口、草稿表单、脚本编辑弹窗、删除确认弹窗、Agent 输出面板和创建错误状态样式。
- 从 `workspace.css` 移出 `skill-*` 相关样式，使 workspace 模块继续收敛到 review/doc-update 等剩余职责。
- 将 Skill 创建相关移动端覆盖和 `skill-create-spin` 动画从 `responsive.css` 移入 `skill.css`。

## 当前结果

精简后统计：

| 文件 | 行数 |
| --- | ---: |
| `source/frontend/src/styles/global.css` | 12 |
| `source/frontend/src/styles/modules/assistant.css` | 896 |
| `source/frontend/src/styles/modules/auth.css` | 127 |
| `source/frontend/src/styles/modules/controls.css` | 130 |
| `source/frontend/src/styles/modules/document-detail.css` | 451 |
| `source/frontend/src/styles/modules/foundation.css` | 108 |
| `source/frontend/src/styles/modules/layout.css` | 535 |
| `source/frontend/src/styles/modules/markdown.css` | 291 |
| `source/frontend/src/styles/modules/responsive.css` | 1072 |
| `source/frontend/src/styles/modules/sessions.css` | 915 |
| `source/frontend/src/styles/modules/skill.css` | 395 |
| `source/frontend/src/styles/modules/workspace-tree.css` | 546 |
| `source/frontend/src/styles/modules/workspace.css` | 327 |
| **总计** | **5805** |

变化：

- CSS 总行数：`5941 -> 5805`，减少 `136` 行。
- `responsive.css`：`1600 -> 1072`。
- `workspace.css`：`1215 -> 327`。
- `responsive.css` 内 `@media` 数量：`15 -> 8`。
- 构建产物 CSS：约 `89.64 kB -> 88.12 kB`。
- gzip 后 CSS：约 `16.32 kB -> 16.22 kB`。

说明：`workspace.css` 行数增加是因为 `child-card` 基础样式从 `responsive.css` 迁回了正确模块，整体行数仍下降。
接续轮是职责边界重整，CSS 总行数保持 `5777`，但 `assistant.css` 从 `1188` 行降至 `896` 行，通用 markdown 样式集中到独立模块，便于后续继续拆分响应式 markdown 覆盖。
第二接续轮继续做职责边界重整，CSS 总行数因新增模块边界略回升到 `5793`，但 `responsive.css` 降至 `1286` 行，`workspace.css` 降至 `1018` 行。
第三接续轮继续拆 tree/sidebar 边界，CSS 总行数为 `5797`，但 `responsive.css` 降至 `1125` 行，`workspace.css` 降至 `662` 行。
第四接续轮继续拆 Skill 创建边界，CSS 总行数为 `5805`，但 `responsive.css` 降至 `1072` 行，`workspace.css` 降至 `327` 行。

## 验证记录

已执行：

```bash
source .venv/bin/activate && pnpm --dir source/frontend build
```

结果：

- 构建通过。
- Vite 仍提示部分 JS chunk 超过 500 kB，该提示与本次 CSS 精简无直接关系。
- 接续轮构建继续通过，产物 CSS 为 `87.84 kB`，gzip 后为 `16.23 kB`。
- 第二接续轮构建继续通过，产物 CSS 为 `87.94 kB`，gzip 后为 `16.27 kB`。
- 第三接续轮构建继续通过，产物 CSS 为 `88.04 kB`，gzip 后为 `16.27 kB`。
- 第四接续轮构建继续通过，产物 CSS 为 `88.12 kB`，gzip 后为 `16.22 kB`。

本地页面冒烟：

- 访问 `http://localhost:4173` 成功。
- 登录页正常渲染，页面标题为 `ai-prd Web Prototype`。
- 移动端视口 `390x844` 检查：无横向溢出，`documentElement.scrollWidth === 390`。
- 接续轮未启动/重启本地服务；`curl -I http://localhost:4173` 返回 `200 OK`，但当前 GET 响应体为空，因此新版视觉冒烟以构建验证为准。

备注：浏览器插件自身访问 Statsig 时出现外部网络 403/Cloudflare 日志，但本地应用页面加载与 CSS 检查正常，不影响本次验证结论。

## 剩余问题与后续建议

下一轮不建议继续做纯机械删行，收益会变小。后续应进入“组件边界重整”，但要分小批进行。

建议下一阶段计划：

1. `assistant.css` 拆分或重排职责
   - 已分出 markdown 通用模块。
   - 后续继续分出 chat shell、message、composer、history/share 相关区块。

2. `workspace.css` 拆分职责
   - 已分出 document detail。
   - 已分出 tree/sidebar。
   - 已分出 skill create。
   - 后续继续分出 review/doc-update、dialog。

3. `responsive.css` 继续组件化
   - 不再新增大块全局断点补丁。
   - 已迁出 workspace 文档详情相关移动端规则。
   - 已迁出 tree/sidebar 相关移动端规则。
   - 已迁出 skill create 相关移动端规则。
   - 后续优先迁出 assistant/history、review/doc-update 相关移动端规则。

4. 清理内联样式
   - `source/frontend/src/admin/AdminApp.jsx` 内仍有若干按钮内联 `style`，后续可用小尺寸按钮类替代。

5. 评估动态 tone 类
   - `badge-${tone}`、`context-chip-${tone}`、`nav-icon-${tone}` 当前是真实动态使用，不应删除。
   - 如果后续 tone 固定在 `signal/sand/olive/neutral`，可以考虑抽成 CSS 变量驱动，减少重复色彩声明。

## 后续新会话接续提示

可以直接对新会话说明：

> 继续 `source/docs/ui/css-refactor-progress.md` 里的 CSS 精简计划，从“剩余问题与后续建议”开始。请先读取该文档和当前 git diff，再按小步提交式继续整理，保持构建通过，不要启动/重启本地服务。
