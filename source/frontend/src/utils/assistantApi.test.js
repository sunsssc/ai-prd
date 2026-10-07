import assert from "node:assert/strict";
import test, { afterEach } from "node:test";

import {
  rebuildSharedAssistantRuntime,
  createAssistantSession,
  favoriteAssistantSession,
  getAssistantSession,
  getAssistantSkills,
  getSharedAssistantSession,
  listAssistantSessions,
  listSharedAssistantSessions,
  revealAssistantTestDataPlanSecret,
  resumeSharedAssistantTurnStream,
  sendAssistantMessage,
  streamAssistantMessage,
  unfavoriteAssistantSession
} from "../services/assistantApi.js";
import {
  getAuthConfig,
  getGoogleAuthorizationUrl,
  loginWithEmail,
  registerWithEmail,
  requestEmailCode,
  revealEmailCode
} from "../services/authApi.js";
import {
  createOrganizationAccessRequest,
  createRequirementComment,
  getRequirementClickUpEditAccess,
  getRequirementClickUpContent,
  listRequirementClickUpContentHistory,
  restoreRequirementClickUpContentHistory,
  createSkillEntry,
  deleteSkillEntry,
  deleteSkillFolder,
  discardSkillZip,
  listMyTasks,
  listRequirementComments,
  listWorkspaces,
  moveSkillEntry,
  resolveSkillZipGenerationAfterStreamError,
  streamGenerateSkillZipMarkdown,
  updateRequirementClickUpContent,
  updateSkillFile
} from "../services/workspaceApi.js";

function installFetchRecorder() {
  const calls = [];
  globalThis.window = {
    setTimeout,
    clearTimeout
  };
  globalThis.fetch = async (path, options = {}) => {
    calls.push({ path, options });
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    });
  };
  return calls;
}

afterEach(() => {
  delete globalThis.fetch;
  delete globalThis.window;
});

test("assistant session creation sends selected organization key", async () => {
  const calls = installFetchRecorder();

  await createAssistantSession("新会话", "coinex");

  assert.equal(calls[0].path, "/api/assistant/sessions");
  assert.equal(calls[0].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    title: "新会话",
    organization_key: "coinex"
  });
});

test("共享会话 Runtime 重建必须通过显式确认接口触发", async () => {
  const calls = installFetchRecorder();

  await rebuildSharedAssistantRuntime("share/token", "request id");

  assert.equal(
    calls[0].path,
    "/api/assistant/shared/share%2Ftoken/follow-ups/request%20id/rebuild-runtime"
  );
  assert.equal(calls[0].options.method, "POST");
});

test("会话运行状态查询禁用浏览器缓存", async () => {
  const calls = installFetchRecorder();

  await listAssistantSessions({ limit: 50 });
  await getAssistantSession("session-1");
  await listSharedAssistantSessions();
  await getSharedAssistantSession("share/token");

  assert.deepEqual(
    calls.map(({ options }) => options.cache),
    ["no-store", "no-store", "no-store", "no-store"]
  );
});

test("一次性造数凭据通过会话所有者接口领取", async () => {
  const calls = installFetchRecorder();

  await revealAssistantTestDataPlanSecret("session/1", "tdp 1");

  assert.equal(
    calls[0].path,
    "/api/assistant/sessions/session%2F1/test-data-plans/tdp%201/reveal-secret"
  );
  assert.equal(calls[0].options.method, "POST");
});

test("收藏会话接口和收藏列表参数使用明确语义", async () => {
  const calls = installFetchRecorder();

  await listAssistantSessions({ query: "规则", favoritedOnly: true, limit: 20, offset: 0 });
  await favoriteAssistantSession("session-1");
  await unfavoriteAssistantSession("session-1");

  assert.equal(calls[0].path, "/api/assistant/sessions?query=%E8%A7%84%E5%88%99&favorited_only=true&limit=20&offset=0");
  assert.deepEqual(
    calls.slice(1).map(({ path, options }) => [path, options.method]),
    [
      ["/api/assistant/sessions/session-1/favorite", "PUT"],
      ["/api/assistant/sessions/session-1/favorite", "DELETE"]
    ]
  );
});

test("共享查看者通过共享权限端点订阅同一条 turn 实时流", async () => {
  const calls = [];
  globalThis.window = { setTimeout, clearTimeout };
  globalThis.fetch = async (path, options = {}) => {
    calls.push({ path, options });
    return new Response("data: [DONE]\n\n", {
      status: 200,
      headers: { "Content-Type": "text/event-stream" }
    });
  };

  await resumeSharedAssistantTurnStream({ shareToken: "share/token", turnId: "turn id" });

  assert.equal(calls[0].path, "/api/assistant/shared/share%2Ftoken/turns/turn%20id/stream");
});

test("Google 登录先获取授权地址再跳转", async () => {
  const calls = [];
  globalThis.window = { setTimeout, clearTimeout };
  globalThis.fetch = async (path, options = {}) => {
    calls.push({ path, options });
    return new Response(JSON.stringify({ authorization_url: "https://accounts.google.com/o/oauth2/v2/auth?state=test" }), {
      status: 200,
      headers: { "Content-Type": "application/json" }
    });
  };

  const authorizationUrl = await getGoogleAuthorizationUrl();

  assert.equal(authorizationUrl, "https://accounts.google.com/o/oauth2/v2/auth?state=test");
  assert.equal(calls[0].path, "/api/auth/google/url");
});

test("邮箱验证码认证调用对应接口", async () => {
  const calls = installFetchRecorder();

  await getAuthConfig();
  await requestEmailCode("guest@gmail.com", "auto");
  await revealEmailCode("guest@gmail.com", "register");
  await registerWithEmail("guest@gmail.com", "Guest", "123456");
  await loginWithEmail("guest@gmail.com", "654321");

  assert.deepEqual(
    calls.map(({ path, options }) => [path, options.body ? JSON.parse(options.body) : null]),
    [
      ["/api/auth/config", null],
      ["/api/auth/email/request-code", { email: "guest@gmail.com", purpose: "auto" }],
      ["/api/auth/email/reveal-code", { email: "guest@gmail.com", purpose: "register" }],
      ["/api/auth/email/register", { email: "guest@gmail.com", name: "Guest", code: "123456" }],
      ["/api/auth/email/login", { email: "guest@gmail.com", code: "654321" }]
    ]
  );
  assert.equal(calls[0].path, "/api/auth/config");
  assert.equal(calls[0].options.method, undefined);
  assert.ok(calls.slice(1).every(({ options }) => options.method === "POST"));
});

test("getAssistantSkills 复用未过期缓存并支持强制刷新", async () => {
  const calls = installFetchRecorder();

  await getAssistantSkills();
  await getAssistantSkills();

  assert.equal(calls.length, 1);
  assert.equal(calls[0].path, "/api/assistant/skills");

  await getAssistantSkills({ force: true });

  assert.equal(calls.length, 2);
});

test("assistant chat sends organization key only for new-session selection", async () => {
  const calls = installFetchRecorder();

  await sendAssistantMessage("你好", null, "acme");

  assert.equal(calls[0].path, "/api/assistant/chat");
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    message: "你好",
    session_id: null,
    organization_key: "acme"
  });
});

test("assistant stream forwards terminal errors before rejecting", async () => {
  globalThis.window = { setTimeout, clearTimeout };
  globalThis.fetch = async () =>
    new Response(
      [
        'data: {"type":"turn_start","session_id":"session-1","turn_id":"turn-1"}',
        "",
        'data: {"type":"error","message":"图片结果超过读取上限"}',
        "",
        "data: [DONE]",
        ""
      ].join("\n"),
      { status: 200, headers: { "Content-Type": "text/event-stream" } }
    );

  const events = [];
  await assert.rejects(
    streamAssistantMessage({
      message: "生成图片",
      sessionId: "session-1",
      onEvent: (event) => events.push(event)
    }),
    (error) => {
      assert.equal(error.message, "图片结果超过读取上限");
      assert.equal(error.isTerminalStreamError, true);
      return true;
    }
  );

  assert.deepEqual(events.map((event) => event.type), ["turn_start", "error"]);
});

test("assistant stream 将 422 结构化校验错误转换为可读提示", async () => {
  globalThis.window = { setTimeout, clearTimeout };
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        detail: [
          {
            type: "string_too_long",
            loc: ["body", "context_items", 0, "context_key"],
            msg: "String should have at most 120 characters",
            ctx: { max_length: 120 }
          }
        ]
      }),
      { status: 422, headers: { "Content-Type": "application/json" } }
    );

  await assert.rejects(
    streamAssistantMessage({ message: "引用文档" }),
    (error) => {
      assert.equal(error.status, 422);
      assert.equal(error.message, "context_items[0].context_key：不能超过 120 个字符。");
      assert.equal(error.message.includes("[object Object]"), false);
      return true;
    }
  );
});

test("assistant stream does not submit a user-selected Git scope", async () => {
  const calls = [];
  globalThis.window = { setTimeout, clearTimeout };
  globalThis.fetch = async (path, options = {}) => {
    calls.push({ path, options });
    return new Response("data: [DONE]\n\n", {
      status: 200,
      headers: { "Content-Type": "text/event-stream" }
    });
  };

  await streamAssistantMessage({
    message: "分析两个仓库",
    sessionId: "session-1"
  });

  assert.equal(calls[0].path, "/api/assistant/chat/stream");
  assert.equal("git_contexts" in JSON.parse(calls[0].options.body), false);
});

test("workspace APIs preserve organization query and access request payload", async () => {
  const calls = installFetchRecorder();

  await listWorkspaces("coinex");
  await createOrganizationAccessRequest({
    target_organization_key: "acme",
    reason: "需要跨组织查看需求"
  });

  assert.equal(calls[0].path, "/api/workspaces?organization_key=coinex");
  assert.equal(calls[1].path, "/api/organization-access-requests");
  assert.equal(calls[1].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    target_organization_key: "acme",
    reason: "需要跨组织查看需求"
  });
});

test("my tasks API sends server pagination and repeated filters", async () => {
  const calls = installFetchRecorder();

  await listMyTasks({
    scope: "completed",
    assignmentScope: "all",
    page: 3,
    pageSize: 50,
    query: "backend #4321",
    statuses: ["已上线", "测试中"],
    roles: ["后端", "抄送人"]
  });

  const url = new URL(calls[0].path, "https://example.test");
  assert.equal(url.pathname, "/api/my-tasks");
  assert.equal(url.searchParams.get("scope"), "completed");
  assert.equal(url.searchParams.get("assignment_scope"), "all");
  assert.equal(url.searchParams.get("page"), "3");
  assert.equal(url.searchParams.get("page_size"), "50");
  assert.equal(url.searchParams.get("q"), "backend #4321");
  assert.deepEqual(url.searchParams.getAll("status"), ["已上线", "测试中"]);
  assert.deepEqual(url.searchParams.getAll("role"), ["后端", "抄送人"]);
});

test("requirement comments API reads, creates, and replies by source path", async () => {
  const calls = installFetchRecorder();
  const sourcePath = "workspace/knowledge/requirements/tasks/实现中/task.md";

  await listRequirementComments(sourcePath);
  await createRequirementComment(sourcePath, "新增评论");
  await createRequirementComment(sourcePath, "回复内容", "comment-1");

  const readUrl = new URL(calls[0].path, "https://example.test");
  assert.equal(readUrl.pathname, "/api/knowledge/requirements/comments");
  assert.equal(readUrl.searchParams.get("path"), sourcePath);
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    path: sourcePath,
    content: "新增评论",
    parent_comment_id: null
  });
  assert.deepEqual(JSON.parse(calls[2].options.body), {
    path: sourcePath,
    content: "回复内容",
    parent_comment_id: "comment-1"
  });
});

test("ClickUp requirement content API reads and updates markdown by source path", async () => {
  const calls = installFetchRecorder();
  const sourcePath = "workspace/knowledge/requirements/docs/营销/优惠券规则.md";

  await getRequirementClickUpContent(sourcePath);
  await updateRequirementClickUpContent(sourcePath, "# 新需求", "# 原需求");
  await listRequirementClickUpContentHistory(sourcePath);
  await restoreRequirementClickUpContentHistory(sourcePath, "20260802T120000Z-123456abcdef");

  const readUrl = new URL(calls[0].path, "https://example.test");
  assert.equal(readUrl.pathname, "/api/knowledge/requirements/clickup-content");
  assert.equal(readUrl.searchParams.get("path"), sourcePath);
  assert.equal(calls[1].options.method, "PUT");
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    path: sourcePath,
    content: "# 新需求",
    base_content: "# 原需求"
  });
  const historyUrl = new URL(calls[2].path, "https://example.test");
  assert.equal(historyUrl.pathname, "/api/knowledge/requirements/clickup-content/history");
  assert.equal(historyUrl.searchParams.get("path"), sourcePath);
  assert.equal(
    calls[3].path,
    "/api/knowledge/requirements/clickup-content/history/20260802T120000Z-123456abcdef/restore"
  );
  assert.equal(calls[3].options.method, "POST");
  assert.deepEqual(JSON.parse(calls[3].options.body), { path: sourcePath });
});

test("ClickUp requirement edit access API reads the source path", async () => {
  const calls = installFetchRecorder();
  const sourcePath = "workspace/knowledge/requirements/tasks/实现中/task.md";

  await getRequirementClickUpEditAccess(sourcePath);

  const accessUrl = new URL(calls[0].path, "https://example.test");
  assert.equal(accessUrl.pathname, "/api/knowledge/requirements/clickup-edit-access");
  assert.equal(accessUrl.searchParams.get("path"), sourcePath);
});

test("discarding a skill ZIP draft calls the owner-scoped delete endpoint", async () => {
  const calls = installFetchRecorder();

  await discardSkillZip("preview-token");

  assert.equal(calls[0].path, "/api/skills/import/draft?token=preview-token");
  assert.equal(calls[0].options.method, "DELETE");
});

test("skill admin override confirmation is sent on every mutation shape", async () => {
  const calls = installFetchRecorder();

  await updateSkillFile("workspace/.claude/skills/shared/SKILL.md", "# Shared", {
    admin_override_confirmed: true
  });
  await createSkillEntry({
    parent_path: "workspace/.claude/skills/shared",
    name: "rules.md",
    kind: "file",
    content: "",
    admin_override_confirmed: true
  });
  await moveSkillEntry(
    "workspace/.claude/skills/shared/rules.md",
    "workspace/.claude/skills/shared/policy.md",
    { adminOverrideConfirmed: true }
  );
  await deleteSkillEntry("workspace/.claude/skills/shared/rules.md", { adminOverrideConfirmed: true });
  await deleteSkillFolder("workspace/.claude/skills/shared", { adminOverrideConfirmed: true });

  assert.equal(JSON.parse(calls[0].options.body).admin_override_confirmed, true);
  assert.equal(JSON.parse(calls[1].options.body).admin_override_confirmed, true);
  assert.equal(JSON.parse(calls[2].options.body).admin_override_confirmed, true);
  assert.match(calls[3].path, /admin_override_confirmed=true/);
  assert.match(calls[4].path, /admin_override_confirmed=true/);
});

test("missing SKILL.md generation consumes progress events from a stream", async () => {
  globalThis.window = { setTimeout, clearTimeout };
  globalThis.fetch = async (path, options = {}) => {
    assert.equal(path, "/api/skills/import/generate");
    assert.equal(options.method, "POST");
    assert.deepEqual(JSON.parse(options.body), {
      token: "preview-token",
      folder_name: "automation-kit"
    });
    return new Response(
      [
        'data: {"type":"activity","message":"正在读取 references/rules.md"}',
        "",
        'data: {"type":"complete","session_id":"session-1","content":"---\\nname: automation-kit\\ndescription: test\\n---"}',
        "",
        "data: [DONE]",
        ""
      ].join("\n"),
      { status: 200, headers: { "Content-Type": "text/event-stream" } }
    );
  };

  const events = [];
  await streamGenerateSkillZipMarkdown({
    payload: { token: "preview-token", folder_name: "automation-kit" },
    onEvent: (event) => events.push(event)
  });

  assert.deepEqual(events.map((event) => event.type), ["activity", "complete"]);
  assert.equal(events[1].session_id, "session-1");
});

test("persisted completion wins over a stale skill generation stream error", () => {
  const result = resolveSkillZipGenerationAfterStreamError(
    {
      generation_status: "completed",
      generated_skill_md_content: "---\nname: test\ndescription: test\n---"
    },
    "Bash 自动审批未通过，已直接终止本轮执行。"
  );

  assert.deepEqual(result, { status: "completed", error: "" });
});
