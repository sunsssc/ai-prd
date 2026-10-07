import assert from "node:assert/strict";
import test from "node:test";
import {
  clickUpMarkdownToEditorMarkdown,
  editorMarkdownToClickUpMarkdown
} from "./clickupContentAdapter.js";

const mermaidSource = [
  "flowchart TD",
  "  A[需求] --> B{评审通过？}",
  "  B -->|是| C[开发]",
  "  B -->|否| A"
].join("\n");

const plantUmlSource = [
  "@startuml",
  "actor 用户",
  "用户 -> 系统: 提交需求",
  "系统 --> 用户: 返回结果",
  "@enduml"
].join("\n");

test("保存到 ClickUp 时将 Mermaid、PlantUML 和内嵌表格转换为可展示内容", () => {
  const source = [
    "# 复杂需求",
    "",
    "```mermaid",
    mermaidSource,
    "```",
    "",
    "```plantuml",
    plantUmlSource,
    "```",
    "",
    "```csv",
    "字段,类型,说明",
    "id,string,\"唯一标识\"",
    "status,enum,\"状态\"",
    "```",
    "",
    "```typescript",
    "const literalCsv = `a,b`;",
    "```"
  ].join("\n");

  const clickUpContent = editorMarkdownToClickUpMarkdown(source);

  assert.match(clickUpContent, /!\[Mermaid 流程图\]\(https:\/\/mermaid\.ink\/img\//);
  assert.match(clickUpContent, /<!-- doco-clickup-diagram:mermaid:v1 -->/);
  assert.match(clickUpContent, new RegExp(mermaidSource.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));
  assert.match(clickUpContent, /!\[PlantUML UML 图\]\(https:\/\/www\.plantuml\.com\/plantuml\/png\//);
  assert.match(clickUpContent, /<!-- doco-clickup-diagram:plantuml:v1 -->/);
  assert.match(clickUpContent, /<!-- doco-clickup-spreadsheet:v1 -->/);
  assert.match(clickUpContent, /\| 字段 \| 类型 \| 说明 \|/);
  assert.doesNotMatch(clickUpContent, /```csv/);
  assert.match(clickUpContent, /```typescript\nconst literalCsv = `a,b`;\n```/);
});

test("再次编辑 ClickUp 内容时恢复图表源码和 Doco 内嵌表格", () => {
  const editorContent = [
    "```mermaid",
    mermaidSource,
    "```",
    "",
    "```plantuml",
    plantUmlSource,
    "```",
    "",
    "```csv",
    "字段,类型,说明",
    "id,string,\"唯一标识\"",
    "status,enum,\"状态\"",
    "```"
  ].join("\n");
  const clickUpContent = editorMarkdownToClickUpMarkdown(editorContent);
  const restored = clickUpMarkdownToEditorMarkdown(clickUpContent);

  assert.equal(
    restored,
    editorContent.replace(/"唯一标识"|"状态"/g, (value) => value.slice(1, -1))
  );
  assert.doesNotMatch(restored, /mermaid\.ink|plantuml\/png/);
  assert.doesNotMatch(restored, /doco-clickup-(diagram|spreadsheet)/);
});

test("表格单元格中的逗号、引号和换行在 ClickUp 往返后保持不变", () => {
  const source = [
    "```csv",
    "名称,备注",
    "\"多行\",\"第一行\n第二行\"",
    "\"含逗号\",\"他说\"\"确定\"\"\"",
    "```"
  ].join("\n");
  assert.equal(
    clickUpMarkdownToEditorMarkdown(editorMarkdownToClickUpMarkdown(source)),
    [
      "```csv",
      "名称,备注",
      "多行,\"第一行\n第二行\"",
      "含逗号,\"他说\"\"确定\"\"\"",
      "```"
    ].join("\n")
  );
});

test("ClickUp 回读的紧凑表头分隔符会在再次保存前标准化", () => {
  const clickUpContent = [
    "<!-- doco-clickup-spreadsheet:v1 -->",
    "| 字段 | 值 |",
    "| ---| --- |",
    "| status | ready |"
  ].join("\n");
  const restored = clickUpMarkdownToEditorMarkdown(clickUpContent);
  const saved = editorMarkdownToClickUpMarkdown(restored);
  assert.match(saved, /\| --- \| --- \|/);
  assert.doesNotMatch(saved, /\| ---\|/);
});

test("标题后的 Markdown 代码块空行在 Doco 编辑器中保持为代码内容", () => {
  const clickUpContent = [
    "## 推演逻辑",
    "之前：",
    "",
    "```markdown",
    "2. 核心结论详情",
    "",
    "# 输出结构要求",
    "1. 核心结论卡片：",
    "```"
  ].join("\n");

  const editorContent = clickUpMarkdownToEditorMarkdown(clickUpContent);

  assert.match(editorContent, /核心结论详情\n\u200B\n# 输出结构要求/);
  assert.equal(editorMarkdownToClickUpMarkdown(editorContent), clickUpContent);
});
