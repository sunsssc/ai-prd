import assert from "node:assert/strict";
import test from "node:test";
import { canDecideMcpApproval, upsertMcpApproval } from "./mcpApprovals.js";

test("重连重放的 pending 不会覆盖已处理的审批", () => {
  const pending = { approval_id: "a", status: "pending" };
  const approved = { ...pending, status: "approved" };
  let approvals = upsertMcpApproval([], pending);
  approvals = upsertMcpApproval(approvals, approved);
  assert.deepEqual(upsertMcpApproval(approvals, pending), [approved]);
  assert.deepEqual(upsertMcpApproval(approvals, approved), [approved]);
  assert.equal(upsertMcpApproval(approvals, { ...pending, approval_id: "b" }).length, 2);
});

test("已处理、过期及无效时限的审批不能确认", () => {
  const pending = { status: "pending", expires_at: "2026-09-10T05:10:00Z" };
  const now = Date.parse("2026-09-10T05:00:00Z");
  assert.equal(canDecideMcpApproval(pending, now), true);
  assert.equal(canDecideMcpApproval(pending, Date.parse(pending.expires_at)), false);
  for (const status of ["approved", "declined", "expired"]) {
    assert.equal(canDecideMcpApproval({ ...pending, status }, now), false);
  }
  assert.equal(canDecideMcpApproval({ ...pending, expires_at: "invalid" }, now), false);
});
