import test from "node:test";
import assert from "node:assert/strict";

import {
  buildTaskRequirementReference,
  prStatusMeta,
  shortRepoName
} from "./requirementPrLinks.js";
import { buildStableContextKey } from "./contextKeys.js";

test("prStatusMeta maps known states to badge tone", () => {
  assert.deepEqual(prStatusMeta("open"), { label: "Open", tone: "olive" });
  assert.deepEqual(prStatusMeta("draft"), { label: "Draft", tone: "neutral" });
  assert.deepEqual(prStatusMeta("merged"), { label: "Merged", tone: "sand" });
  assert.deepEqual(prStatusMeta("closed"), { label: "Closed", tone: "signal" });
});

test("prStatusMeta falls back for unknown state", () => {
  assert.deepEqual(prStatusMeta("weird"), { label: "weird", tone: "neutral" });
  assert.deepEqual(prStatusMeta(""), { label: "未知", tone: "neutral" });
});

test("shortRepoName keeps the repository segment", () => {
  assert.equal(shortRepoName("example-org/example_backend"), "example_backend");
  assert.equal(shortRepoName("coinex_web"), "coinex_web");
  assert.equal(shortRepoName(""), "");
});

test("task requirement reference uses a stable short key and keeps the exact source path", () => {
  const sourcePath = "/coinex/knowledge/requirements/tasks/梳理中/task__86eye2fjk.md";
  assert.deepEqual(
    buildTaskRequirementReference({
      title: "翻译控件过滤下架语言",
      source_path: sourcePath
    }),
    {
      key: buildStableContextKey("knowledge", sourcePath),
      tone: "signal",
      label: "需求：翻译控件过滤下架语言",
      scopeId: "requirements",
      sourceUri: sourcePath
    }
  );
});
