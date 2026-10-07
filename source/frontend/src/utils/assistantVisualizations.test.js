import assert from "node:assert/strict";
import test from "node:test";

import {
  buildVisualizationRequestContent,
  parseVisualizationRequest,
  prepareVisualizationMarkdown,
  sanitizeSkillPromptLeak,
  sanitizeVisualizationMarkdown,
  sessionVisualizationDisplayText,
  turnVisualizationDisplayText
} from "./assistantVisualizations.js";

test("builds and parses session visualization requests", () => {
  const content = buildVisualizationRequestContent({ scope: "session" }, "请可视化");

  assert.equal(sessionVisualizationDisplayText, "可视化以上会话");
  assert.equal(turnVisualizationDisplayText, "可视化本轮问答");
  assert.deepEqual(parseVisualizationRequest(content), { scope: "session" });
});

test("builds and parses turn visualization requests", () => {
  const content = buildVisualizationRequestContent(
    { scope: "turn", targetTurnId: "turn-123" },
    "请可视化这个 turn"
  );

  assert.deepEqual(parseVisualizationRequest(content), {
    scope: "turn",
    targetTurnId: "turn-123",
    targetMessageId: null
  });
});

test("supports message id as turn visualization target", () => {
  const content = buildVisualizationRequestContent(
    { scope: "turn", targetMessageId: "message-123" },
    "请可视化这个回答"
  );

  assert.deepEqual(parseVisualizationRequest(content), {
    scope: "turn",
    targetTurnId: null,
    targetMessageId: "message-123"
  });
});

test("ignores ordinary messages", () => {
  assert.equal(parseVisualizationRequest("普通提问"), null);
});

test("keeps ordinary visualization markdown unchanged", () => {
  const markdown = "```mermaid\nflowchart TD\nA-->B\n```\n\n说明：正常输出。";

  assert.equal(sanitizeVisualizationMarkdown(markdown), markdown);
});

test("wraps raw svg visualization output as an artifact code block", () => {
  const markdown = [
    '<svg width="100%" viewBox="0 0 120 80" role="img">',
    "<rect width=\"120\" height=\"80\" />",
    "</svg>",
    "",
    "说明：保留业务说明。"
  ].join("\n");

  assert.equal(
    prepareVisualizationMarkdown(markdown),
    [
      "```svg",
      '<svg width="100%" viewBox="0 0 120 80" role="img">',
      "<rect width=\"120\" height=\"80\" />",
      "</svg>",
      "```"
    ].join("\n")
  );
});

test("keeps fenced svg visualization output unchanged", () => {
  const markdown = "```svg\n<svg width=\"100%\" viewBox=\"0 0 120 80\" role=\"img\"></svg>\n```";

  assert.equal(prepareVisualizationMarkdown(markdown), markdown);
});

test("keeps fenced html visualization output unchanged", () => {
  const markdown = [
    "```html",
    "<!doctype html>",
    "<html lang=\"zh-CN\">",
    "<body><main>跟单交易逻辑</main></body>",
    "</html>",
    "```"
  ].join("\n");

  assert.equal(prepareVisualizationMarkdown(markdown), markdown);
});

test("drops markdown prose after fenced visualization output", () => {
  const markdown = [
    "```html",
    "<!doctype html>",
    "<html lang=\"zh-CN\">",
    "<body><main>跟单交易逻辑</main></body>",
    "</html>",
    "```",
    "",
    "- **关卡 1 白名单是最高优先门槛**：用户默认状态为 `NO_PERMISSION`。"
  ].join("\n");

  assert.equal(
    prepareVisualizationMarkdown(markdown),
    [
      "```html",
      "<!doctype html>",
      "<html lang=\"zh-CN\">",
      "<body><main>跟单交易逻辑</main></body>",
      "</html>",
      "```"
    ].join("\n")
  );
});

test("drops non-visualization markdown for visualization output", () => {
  assert.equal(prepareVisualizationMarkdown("说明：这次只生成了文字。"), "");
});

test("strips leaked visualizer skill prompt before visualization code", () => {
  const leaked = [
    "---",
    "name: visualizer",
    "description: Generate high-quality inline SVG diagrams.",
    "---",
    "",
    "# Visualizer Skill",
    "",
    "## Core Design System",
    "Hard prohibitions: no gradients.",
    "",
    "```svg",
    "<svg width=\"100%\" viewBox=\"0 0 680 120\" role=\"img\"></svg>",
    "```",
    "",
    "说明：保留业务说明。"
  ].join("\n");

  assert.equal(
    sanitizeVisualizationMarkdown(leaked),
    "```svg\n<svg width=\"100%\" viewBox=\"0 0 680 120\" role=\"img\"></svg>\n```\n\n说明：保留业务说明。"
  );
  assert.equal(
    prepareVisualizationMarkdown(leaked),
    "```svg\n<svg width=\"100%\" viewBox=\"0 0 680 120\" role=\"img\"></svg>\n```"
  );
});

test("hides leaked visualizer skill prompt when no visualization code is available", () => {
  const leaked = ["# Visualizer Skill", "", "## Core Design System", "Hard prohibitions: no gradients."].join("\n");

  assert.equal(sanitizeVisualizationMarkdown(leaked), "");
});

test("strips leaked skill prompt from ordinary assistant markdown", () => {
  const leaked = [
    "先给结论：状态会从草稿进入审核。",
    "",
    "Base directory for this skill: /workspace/.claude/skills/visualizer",
    "",
    "# Visualizer Skill",
    "Produces SVG diagrams and HTML interactive widgets."
  ].join("\n");

  assert.equal(sanitizeSkillPromptLeak(leaked), "先给结论：状态会从草稿进入审核。");
});
