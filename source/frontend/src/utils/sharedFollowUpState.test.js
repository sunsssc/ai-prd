import assert from "node:assert/strict";
import test from "node:test";

import {
  createSharedLiveTurnState,
  getSharedTurnPresentation,
  isSharedFollowUpInProgress,
  reduceSharedLiveTurnState
} from "./sharedFollowUpState.js";

test("追问者看到自己的处理状态而不是第三人称姓名", () => {
  const user = { user_id: "user-1", name: "Jordan Lee", email: "jordan@example.com" };

  assert.deepEqual(getSharedTurnPresentation(user, user), {
    title: "正在处理你的追问",
    description: "本轮完成后，你会在这里看到最新回复。",
    isOwnFollowUp: true
  });
});

test("其他查看者看到追问人姓名和输入锁定原因", () => {
  const requestedBy = { user_id: "user-1", name: "Jordan Lee", email: "jordan@example.com" };
  const viewer = { user_id: "user-2", name: "Viewer", email: "viewer@example.com" };

  assert.deepEqual(getSharedTurnPresentation(requestedBy, viewer), {
    title: "Jordan Lee 正在追问",
    description: "本轮完成前，其他查看者暂时不能继续追问。",
    isOwnFollowUp: false
  });
  assert.equal(isSharedFollowUpInProgress(requestedBy, { has_active_turn: true }), true);
  assert.equal(isSharedFollowUpInProgress(null, { has_active_turn: true }), false);
});

test("共享实时流为每个查看者累积相同的逐字输出和工具过程", () => {
  const initial = createSharedLiveTurnState("turn-1");
  const afterTool = reduceSharedLiveTurnState(initial, { type: "tool_use", tool_name: "Read" });
  const afterSkill = reduceSharedLiveTurnState(afterTool, { type: "skill_use", skill_name: "ui-design" });
  const afterFirstDelta = reduceSharedLiveTurnState(afterSkill, { type: "delta", delta: "第一段" });
  const afterSecondDelta = reduceSharedLiveTurnState(afterFirstDelta, { type: "delta", delta: "第二段" });

  assert.equal(afterSecondDelta.turnId, "turn-1");
  assert.equal(afterSecondDelta.assistantText, "第一段第二段");
  assert.deepEqual(afterSecondDelta.activities, ["正在调用工具：Read", "正在使用 Skill：ui-design"]);
  assert.deepEqual(afterSecondDelta.skills, [{ type: "skill_use", skill_name: "ui-design" }]);
});
