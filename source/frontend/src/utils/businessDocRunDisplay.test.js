import assert from "node:assert/strict";
import test from "node:test";

import {
  formatBusinessDocConfidence,
  getBusinessDocItemDisplay,
  getBusinessDocReviewLabels,
  getHighConfidenceManualUpdates,
} from "./businessDocRunDisplay.js";

test("无需更新显示为成功结论而不是内部枚举", () => {
  const display = getBusinessDocItemDisplay({
    status: "completed",
    result_type: "no_update_needed",
    apply_status: "not_applicable",
  });

  assert.equal(display.tone, "success");
  assert.equal(display.title, "检查完成 · 无需更新");
  assert.match(display.description, /没有生成更新提案/);
});

test("失败、人工处理和自动应用有明确结论", () => {
  assert.equal(getBusinessDocItemDisplay({ status: "failed" }).title, "检查失败");
  assert.equal(
    getBusinessDocItemDisplay({ status: "completed", apply_status: "manual_required" }).title,
    "检查完成 · 等待人工处理",
  );
  assert.equal(
    getBusinessDocItemDisplay({ status: "completed", apply_status: "applied" }).title,
    "检查完成 · 已应用更新",
  );
  assert.equal(
    getBusinessDocItemDisplay({ status: "completed", apply_status: "ignored" }).title,
    "检查完成 · 已忽略",
  );
});

test("审核与置信度显示为中文说明", () => {
  assert.deepEqual(
    getBusinessDocReviewLabels({ review_status: "approve", generator_review: "agree" }),
    ["独立审核通过", "生成 Agent 已采纳审核意见"],
  );
  assert.deepEqual(getBusinessDocReviewLabels({ review_status: "not_required" }), []);
  assert.equal(formatBusinessDocConfidence(0.99), "判断置信度 99%");
});

test("批量应用只选择置信度严格大于 90% 的待人工处理提案", () => {
  const items = [
    { update_id: "high", apply_status: "manual_required", confidence: 0.91 },
    { update_id: "boundary", apply_status: "manual_required", confidence: 0.9 },
    { update_id: "applied", apply_status: "applied", confidence: 0.99 },
    { update_id: "missing-confidence", apply_status: "manual_required", confidence: null },
    { update_id: null, apply_status: "manual_required", confidence: 0.98 },
  ];

  assert.deepEqual(getHighConfidenceManualUpdates(items).map((item) => item.update_id), ["high"]);
});
