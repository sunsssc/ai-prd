import test from "node:test";
import assert from "node:assert/strict";
import { splitDocumentComments } from "./documentComments.js";

test("separates structured comments from document content", () => {
  const markdown = [
    "## 核心需求描述",
    "",
    "这里是正文。",
    "",
    "## 评论",
    "",
    "> **张三** · 2026-08-02 10:30:00 UTC",
    ">",
    "> 请补充异常场景。",
    "> 第二行说明。",
    "",
    "> **李四** · 2026-08-02 11:00:00 UTC",
    ">",
    "> 已补充，参考 [说明](https://example.com)。"
  ].join("\n");

  assert.deepEqual(splitDocumentComments(markdown), {
    content: "## 核心需求描述\n\n这里是正文。",
    comments: [
      {
        author: "张三",
        createdAt: "2026-08-02 10:30:00 UTC",
        content: "请补充异常场景。\n第二行说明。"
      },
      {
        author: "李四",
        createdAt: "2026-08-02 11:00:00 UTC",
        content: "已补充，参考 [说明](https://example.com)。"
      }
    ]
  });
});

test("preserves sections after comments", () => {
  const result = splitDocumentComments([
    "正文",
    "",
    "## 评论",
    "",
    "> **AI PRD**",
    ">",
    "> 自动评审完成。",
    "",
    "## 附录",
    "",
    "附录内容"
  ].join("\n"));

  assert.equal(result.content, "正文\n\n## 附录\n\n附录内容");
  assert.equal(result.comments[0].author, "AI PRD");
});

test("ignores comment-like headings inside fenced code", () => {
  const markdown = "```markdown\n## 评论\n> 示例\n```\n\n正文";

  assert.deepEqual(splitDocumentComments(markdown), {
    content: markdown,
    comments: []
  });
});
