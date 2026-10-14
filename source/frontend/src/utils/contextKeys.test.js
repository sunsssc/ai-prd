import assert from "node:assert/strict";
import test from "node:test";

import { buildStableContextKey } from "./contextKeys.js";

test("稳定上下文键不暴露长路径且同一路径结果稳定", () => {
  const sourcePath = "workspace/knowledge/requirements/tasks/梳理中/linked_docs/一份很长的正式文档名称.md";
  const first = buildStableContextKey("knowledge", sourcePath);
  const second = buildStableContextKey("knowledge", sourcePath);

  assert.equal(first, second);
  assert.match(first, /^knowledge-[0-9a-f]{16}$/);
  assert.ok(first.length <= 120);
  assert.equal(first.includes(sourcePath), false);
});

test("不同来源路径生成不同上下文键", () => {
  assert.notEqual(
    buildStableContextKey("knowledge", "workspace/knowledge/requirements/a.md"),
    buildStableContextKey("knowledge", "workspace/knowledge/requirements/b.md")
  );
});
