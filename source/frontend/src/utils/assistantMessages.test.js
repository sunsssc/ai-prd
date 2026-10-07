import assert from "node:assert/strict";
import test from "node:test";

import {
  attachTimelineNotices,
  buildComposerDraftFromUserMessage,
  buildStreamFailedTurnTrace,
  formatGitScopeSummary,
  formatSharedMemberNames,
  groupTestDataPlansByTurn,
  shouldAppendStreamMessage,
  toMessageCard,
  toTimelineNoticeCard
} from "./assistantMessages.js";

test("shared member names show at most two people", () => {
  assert.equal(formatSharedMemberNames([{ name: "张三" }]), "张三");
  assert.equal(formatSharedMemberNames([{ name: "张三" }, { name: "李四" }]), "张三、李四");
  assert.equal(
    formatSharedMemberNames([{ name: "张三" }, { name: "李四" }, { email: "wangwu@corp.test" }]),
    "张三、李四…"
  );
});

test("user messages can be restored to the composer with their text and images", () => {
  const contextItem = {
    context_key: "uploaded-image:image-1",
    label: "图片：screen.png",
    source_type: "image",
    source_uri: "workspace/runtime/uploads/image-1.png",
    metadata: {
      image_id: "image-1",
      original_filename: "screen.png",
      mime_type: "image/png",
      size_bytes: 128
    }
  };
  const card = toMessageCard(
    {
      message_id: "user-image-1",
      role: "user",
      content: "请继续处理这张图",
      context_requests: [contextItem]
    },
    { imageUrlForId: (imageId) => `/api/assistant/uploads/images/${imageId}` }
  );

  const draft = buildComposerDraftFromUserMessage(card);

  assert.equal(draft.content, "请继续处理这张图");
  assert.equal(draft.images.length, 1);
  assert.equal(draft.images[0].id, "image-1");
  assert.equal(draft.images[0].fileName, "screen.png");
  assert.equal(draft.images[0].objectUrl, "/api/assistant/uploads/images/image-1");
  assert.deepEqual(draft.images[0].contextItem, contextItem);
});

test("assistant messages cannot be copied into the question composer", () => {
  assert.equal(buildComposerDraftFromUserMessage({ role: "assistant", markdown: "回答" }), null);
});

test("git scope summaries stay compact and expose the actual turn revision", () => {
  assert.equal(formatGitScopeSummary([]), "baseline");
  assert.equal(
    formatGitScopeSummary([
      {
        repo_full_name: "example-org/example_backend",
        strategy: "follow",
        selector_type: "pull_request",
        selector_value: "8784",
        resolved_sha: "226d7331234567890"
      }
    ]),
    "example_backend · PR #8784 · follow · 226d733"
  );
});

test("turn scope appears on both the question and answer cards", () => {
  const gitScopes = [
    {
      repo_full_name: "example-org/example_backend",
      strategy: "baseline"
    }
  ];
  const userCard = toMessageCard({
    message_id: "user-1",
    role: "user",
    content: "分析后端",
    git_scopes: gitScopes
  });
  const assistantCard = toMessageCard({
    message_id: "assistant-1",
    role: "assistant",
    content: "分析完成",
    git_scopes: gitScopes
  });

  assert.equal(userCard.scopeLabel, "example_backend · baseline");
  assert.equal(assistantCard.scopeLabel, "example_backend · baseline");
});

test("ordinary turns display baseline without choosing a repository", () => {
  const userCard = toMessageCard({
    message_id: "user-baseline",
    role: "user",
    content: "分析业务逻辑",
    git_scopes: []
  });

  assert.equal(userCard.scopeLabel, "baseline");
});

test("shared follow-up questions show the requester in the message status", () => {
  const card = toMessageCard({
    message_id: "shared-user-1",
    role: "user",
    content: "继续说明并发规则",
    author: {
      user_id: "member-1",
      name: "小林",
      email: "lin@corp.test"
    }
  });

  assert.equal(card.status, "小林 · 追问");
});

test("stream messages only append to the chat route that owns the stream", () => {
  assert.equal(
    shouldAppendStreamMessage({
      view: "chat",
      routeSessionId: "session-a",
      streamSessionId: "session-a"
    }),
    true
  );
  assert.equal(
    shouldAppendStreamMessage({
      view: "chat",
      routeSessionId: "session-b",
      streamSessionId: "session-a"
    }),
    false
  );
  assert.equal(
    shouldAppendStreamMessage({
      view: "history",
      routeSessionId: "session-a",
      streamSessionId: "session-a"
    }),
    false
  );
});

test("stream messages do not append to a different empty draft route", () => {
  assert.equal(
    shouldAppendStreamMessage({
      view: "chat",
      routeSessionId: null,
      streamSessionId: "session-a"
    }),
    false
  );
});

test("stream failures create an immediately renderable failed turn trace", () => {
  const trace = buildStreamFailedTurnTrace({
    sessionId: "session-a",
    turnId: "turn-a",
    errorMessage: "图片结果超过读取上限",
    gitScopes: [{ repo_full_name: "example-org/example_backend", strategy: "pinned" }],
    completedAt: "2026-07-28T08:04:18Z"
  });

  assert.equal(trace.turn.session_id, "session-a");
  assert.equal(trace.turn.turn_id, "turn-a");
  assert.equal(trace.turn.status, "failed");
  assert.equal(trace.turn.error_message, "图片结果超过读取上限");
  assert.equal(trace.turn.completed_at, "2026-07-28T08:04:18Z");
  assert.deepEqual(trace.runtime_events, []);
  assert.equal(trace.git_scopes[0].strategy, "pinned");
});

test("assistant message cards preserve file artifacts from API responses", () => {
  const card = toMessageCard({
    message_id: "message-1",
    session_id: "session-1",
    role: "assistant",
    content: "文件已生成：`me/files/report.pdf`",
    created_at: "2026-06-29T00:00:00Z",
    file_artifacts: [
      {
        artifact_id: "artifact-1",
        display_path: "me/files/report.pdf",
        filename: "report.pdf",
        mime_type: "application/pdf",
        size_bytes: 32,
        storage_status: "source",
        download_url: "/api/assistant/sessions/session-1/files/artifact-1"
      }
    ]
  });

  assert.equal(card.fileArtifacts.length, 1);
  assert.equal(card.fileArtifacts[0].download_url, "/api/assistant/sessions/session-1/files/artifact-1");
});

test("image quota notices remain attached after the assistant message for the same turn", () => {
  const messageCards = [
    toMessageCard({
      message_id: "user-1",
      turn_id: "turn-1",
      role: "user",
      content: "再生成两张",
      created_at: "2026-07-28T11:03:23Z"
    }),
    toMessageCard({
      message_id: "assistant-1",
      turn_id: "turn-1",
      role: "assistant",
      content: "已生成两张新风格。",
      created_at: "2026-07-28T11:06:14Z"
    })
  ];
  const notices = [
    {
      event_id: "notice-1",
      turn_id: "turn-1",
      kind: "image_quota",
      message: "生图额度提示：本轮已保留 1 张；另 1 张未加入会话。",
      created_at: "2026-07-28T11:06:14Z",
      resets_at: "2026-07-29T00:00:00Z"
    }
  ];

  const timeline = attachTimelineNotices(messageCards, notices, { formatTime: (value) => value });

  assert.deepEqual(timeline.map((item) => item.role), ["user", "assistant", "system"]);
  assert.equal(timeline[2].messageId, "timeline-notice-notice-1");
  assert.match(timeline[2].detail, /本轮已保留 1 张/);
  assert.match(timeline[2].detail, /重置/);
});

test("timeline notice cards use a system role and do not masquerade as assistant replies", () => {
  const card = toTimelineNoticeCard({
    event_id: "notice-2",
    turn_id: "turn-2",
    message: "本轮未新增图片。",
    created_at: "2026-07-28T12:00:00Z"
  });

  assert.equal(card.role, "system");
  assert.equal(card.status, "系统提示");
  assert.equal(card.title, "生图额度");
});

test("git context updates render as system version notices", () => {
  const card = toTimelineNoticeCard({
    event_id: "git-notice",
    turn_id: "turn-git",
    kind: "git_context_updated",
    message: "代码上下文已更新：sha_a → sha_b",
    created_at: "2026-07-30T00:00:00Z"
  });

  assert.equal(card.role, "system");
  assert.equal(card.title, "代码版本更新");
  assert.match(card.detail, /sha_a → sha_b/);
});

test("test data plans attach once after the assistant answer for the prepared turn", () => {
  const plan = { plan_id: "tdp_1", prepared_turn_id: "turn-1", status: "prepared" };
  const grouped = groupTestDataPlansByTurn(
    [plan, { plan_id: "tdp_orphan", prepared_turn_id: "turn-missing", status: "failed" }],
    [
      { role: "user", turnId: "turn-1" },
      { role: "assistant", turnId: "turn-1" }
    ]
  );

  assert.deepEqual(grouped.byTurn.get("turn-1"), [plan]);
  assert.deepEqual(grouped.orphaned.map((item) => item.plan_id), ["tdp_orphan"]);
});
