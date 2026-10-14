import { notifyAuthExpired } from "./authEvents.js";
import { defaultStreamConnectTimeoutMs, fetchWithTimeout, requestJson } from "./http.js";
import { apiWithCache, invalidateApiCache, peekApiCache } from "./cache.js";

async function request(path, options = {}) {
  return requestJson(path, {
    timeoutMs: 30000,
    timeoutMessage: "请求超时，请确认后端服务已启动。",
    onUnauthorized: notifyAuthExpired,
    ...options
  });
}

const KNOWLEDGE_ROOT_TTL = 8000;
const KNOWLEDGE_DIR_TTL = 30000;
const KNOWLEDGE_FILE_TTL = 30000;
const KNOWLEDGE_INDEX_TTL = 300000;

function buildKnowledgeChildrenUrl(knowledgeType, path) {
  const search = new URLSearchParams();
  if (path) search.set("path", path);
  const query = search.toString();
  return `/api/knowledge/${knowledgeType}/children${query ? `?${query}` : ""}`;
}

export function getKnowledgeChildren(knowledgeType, path, options = {}) {
  const url = buildKnowledgeChildrenUrl(knowledgeType, path);
  if (options.force) {
    invalidateApiCache(url);
  }
  return apiWithCache(url, path ? KNOWLEDGE_DIR_TTL : KNOWLEDGE_ROOT_TTL, () => request(url));
}

export function getKnowledgeIndex(options = {}) {
  const url = "/api/knowledge/index";
  if (options.force) {
    invalidateApiCache(url);
  }
  return apiWithCache(url, KNOWLEDGE_INDEX_TTL, () => request(url));
}

export function peekKnowledgeIndexCache() {
  return peekApiCache("/api/knowledge/index");
}

export function peekKnowledgeChildrenCache(knowledgeType) {
  const url = `/api/knowledge/${knowledgeType}/children`;
  return peekApiCache(url);
}

export function listOrganizations() {
  return request("/api/organizations");
}

export function listWorkspaces(organizationKey) {
  const search = new URLSearchParams();
  if (organizationKey) {
    search.set("organization_key", organizationKey);
  }
  const query = search.toString();
  return request(`/api/workspaces${query ? `?${query}` : ""}`);
}

export function createOrganizationAccessRequest(payload) {
  return request("/api/organization-access-requests", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function listOrganizationAccessRequests() {
  return request("/api/organization-access-requests");
}

export function getKnowledgeFile(knowledgeType, path, options = {}) {
  const search = new URLSearchParams({ path });
  const url = `/api/knowledge/${knowledgeType}/file?${search.toString()}`;
  if (options.force) {
    invalidateApiCache(url);
  }
  return apiWithCache(url, KNOWLEDGE_FILE_TTL, () => request(url));
}

export function refreshRequirementSource(path) {
  return request("/api/knowledge/requirements/source-refresh", {
    method: "POST",
    timeoutMs: 120000,
    timeoutMessage: "拉取最新版本超时，请稍后重试。",
    body: JSON.stringify({ path })
  });
}

export function getRequirementClickUpContent(path) {
  const search = new URLSearchParams({ path });
  return request(`/api/knowledge/requirements/clickup-content?${search.toString()}`);
}

export function getRequirementClickUpEditAccess(path) {
  const search = new URLSearchParams({ path });
  return request(`/api/knowledge/requirements/clickup-edit-access?${search.toString()}`);
}

export function updateRequirementClickUpContent(path, content, baseContent) {
  return request("/api/knowledge/requirements/clickup-content", {
    method: "PUT",
    timeoutMs: 120000,
    timeoutMessage: "保存到 ClickUp 超时，请重新打开正文确认是否已保存。",
    body: JSON.stringify({ path, content, base_content: baseContent })
  });
}

export function listRequirementClickUpContentHistory(path) {
  const search = new URLSearchParams({ path });
  return request(`/api/knowledge/requirements/clickup-content/history?${search.toString()}`);
}

export function restoreRequirementClickUpContentHistory(path, versionId) {
  return request(`/api/knowledge/requirements/clickup-content/history/${encodeURIComponent(versionId)}/restore`, {
    method: "POST",
    timeoutMs: 120000,
    timeoutMessage: "恢复历史版本超时，请重新打开正文确认是否已保存。",
    body: JSON.stringify({ path })
  });
}

export function listRequirementComments(path) {
  const search = new URLSearchParams({ path });
  return request(`/api/knowledge/requirements/comments?${search.toString()}`);
}

export function createRequirementComment(path, content, parentCommentId = null) {
  return request("/api/knowledge/requirements/comments", {
    method: "POST",
    body: JSON.stringify({
      path,
      content,
      parent_comment_id: parentCommentId
    })
  });
}

export function buildKnowledgeAssetUrl(knowledgeType, path) {
  const search = new URLSearchParams({ path });
  return `/api/knowledge/${knowledgeType}/asset?${search.toString()}`;
}

export function listRequirementReviews(sourcePath) {
  const search = new URLSearchParams({ source_path: sourcePath });
  return request(`/api/requirements/reviews?${search.toString()}`);
}

export function listRequirementPullRequests(sourcePath) {
  const search = new URLSearchParams({ source_path: sourcePath });
  return request(`/api/requirements/pull-requests?${search.toString()}`);
}

export function listMyTasks({
  scope = "active",
  assignmentScope = "mine",
  page = 1,
  pageSize = 50,
  query = "",
  statuses = null,
  roles = null
} = {}) {
  const search = new URLSearchParams({
    scope,
    assignment_scope: assignmentScope,
    page: String(page),
    page_size: String(pageSize)
  });
  if (query) search.set("q", query);
  for (const status of statuses || []) search.append("status", status);
  for (const role of roles || []) search.append("role", role);
  return request(`/api/my-tasks?${search.toString()}`);
}

export function updateMyTaskEnvironment(taskId, environment) {
  return request(`/api/my-tasks/${encodeURIComponent(taskId)}/environment`, {
    method: "PUT",
    body: JSON.stringify({ environment: environment || null })
  });
}

export function createMyTaskAssistantSession(taskId, repoFullName, prNumber) {
  return request(
    `/api/my-tasks/${encodeURIComponent(taskId)}/pull-requests/assistant-session`,
    {
      method: "POST",
      body: JSON.stringify({
        repo_full_name: repoFullName,
        pr_number: prNumber
      })
    }
  );
}

export function createManualCodeReview(repoFullName, prNumber) {
  return request("/api/code-reviews/jobs", {
    method: "POST",
    body: JSON.stringify({
      repo_full_name: repoFullName,
      pr_number: prNumber
    })
  });
}

export function listCodeReviewJobs(repoFullName, prNumber) {
  const search = new URLSearchParams({
    repo_full_name: repoFullName,
    pr_number: String(prNumber)
  });
  return request(`/api/code-reviews/jobs?${search.toString()}`);
}

export function getCodeReviewJob(jobId) {
  return request(`/api/code-reviews/jobs/${encodeURIComponent(jobId)}`);
}

export function getCodeReview(reviewId) {
  return request(`/api/code-reviews/${encodeURIComponent(reviewId)}`);
}

export function getRequirementReview(reviewId) {
  return request(`/api/requirements/reviews/${encodeURIComponent(reviewId)}`);
}

export function markRequirementReviewRead(reviewId) {
  return request(`/api/requirements/reviews/${encodeURIComponent(reviewId)}/read`, {
    method: "POST"
  });
}

export function createRequirementReviewJob(sourcePath) {
  return request("/api/requirements/reviews/jobs", {
    method: "POST",
    body: JSON.stringify({
      source_path: sourcePath,
      review_type: "tech_review"
    })
  });
}

export function getRequirementReviewJob(jobId) {
  return request(`/api/requirements/reviews/jobs/${encodeURIComponent(jobId)}`);
}

export function rebuildRequirementReview(sourcePath) {
  return request("/api/requirements/reviews/rebuild", {
    method: "POST",
    timeoutMs: 120000,
    timeoutMessage: "重新评审超时，请稍后查看评审记录。",
    body: JSON.stringify({
      source_path: sourcePath,
      review_type: "tech_review"
    })
  });
}

export function listBusinessDocUpdates(sourcePath) {
  const search = new URLSearchParams({ source_path: sourcePath });
  return request(`/api/business-docs/updates?${search.toString()}`);
}

export function getBusinessDocUpdate(updateId) {
  return request(`/api/business-docs/updates/${encodeURIComponent(updateId)}`);
}

export function applyBusinessDocUpdate(updateId, reason = "") {
  return request(`/api/business-docs/updates/${encodeURIComponent(updateId)}/apply`, {
    method: "POST",
    body: JSON.stringify({ reason })
  });
}

export function ignoreBusinessDocUpdate(updateId, reason = "") {
  return request(`/api/business-docs/updates/${encodeURIComponent(updateId)}/ignore`, {
    method: "POST",
    body: JSON.stringify({ reason })
  });
}

export function getSkillTree() {
  return request("/api/skills/tree");
}

export function getSkillFile(path) {
  const search = new URLSearchParams({ path });
  return request(`/api/skills/file?${search.toString()}`);
}

export function deleteSkillFolder(path, options = {}) {
  const search = new URLSearchParams({ path });
  if (options.adminOverrideConfirmed) search.set("admin_override_confirmed", "true");
  return request(`/api/skills/folder?${search.toString()}`, {
    method: "DELETE"
  });
}

export function updateSkillFile(path, content, options = {}) {
  return request("/api/skills/file", {
    method: "PUT",
    body: JSON.stringify({ path, content, ...options })
  });
}

export function createSkill(payload) {
  return request("/api/skills", {
    method: "POST",
    timeoutMs: 120000,
    timeoutMessage: "Skill 生成超时，请稍后查看后端执行状态或缩短意向描述后重试。",
    body: JSON.stringify(payload)
  });
}

export function previewSkillZip(file) {
  const search = new URLSearchParams({ filename: file.name });
  return request(`/api/skills/import/preview?${search.toString()}`, {
    method: "POST",
    timeoutMs: 120000,
    timeoutMessage: "ZIP 解析超时，请检查压缩包大小后重试。",
    headers: { "Content-Type": "application/zip" },
    body: file
  });
}

export function getLatestSkillZipPreview() {
  return request("/api/skills/import/latest");
}

export function getSkillZipPreview(token) {
  const search = new URLSearchParams({ token });
  return request(`/api/skills/import/preview?${search.toString()}`);
}

export function discardSkillZip(token) {
  const search = new URLSearchParams({ token });
  return request(`/api/skills/import/draft?${search.toString()}`, {
    method: "DELETE"
  });
}

export function resolveSkillZipGenerationAfterStreamError(persistedPreview, streamErrorMessage) {
  if (persistedPreview?.generation_status === "completed" && persistedPreview.generated_skill_md_content?.trim()) {
    return { status: "completed", error: "" };
  }
  if (persistedPreview?.generation_status === "running") {
    return { status: "running", error: "" };
  }
  return {
    status: "failed",
    error: persistedPreview?.generation_error || streamErrorMessage
  };
}

export function commitSkillZip(payload) {
  return request("/api/skills/import/commit", {
    method: "POST",
    timeoutMs: 120000,
    timeoutMessage: "ZIP 导入超时，请稍后重试。",
    body: JSON.stringify(payload)
  });
}

export async function streamGenerateSkillZipMarkdown({ payload, onEvent, onOpen, signal }) {
  const response = await fetchWithTimeout("/api/skills/import/generate", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    signal,
    timeoutMs: defaultStreamConnectTimeoutMs,
    timeoutMessage: "连接 SKILL.md 生成 agent 超时，请确认后端服务已启动。",
    body: JSON.stringify(payload)
  });

  return consumeEventStream(response, onEvent, onOpen);
}

export function createSkillEntry(payload) {
  return request("/api/skills/entry", {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function moveSkillEntry(path, destinationPath, options = {}) {
  return request("/api/skills/entry", {
    method: "PUT",
    body: JSON.stringify({
      path,
      destination_path: destinationPath,
      admin_override_confirmed: Boolean(options.adminOverrideConfirmed)
    })
  });
}

export function deleteSkillEntry(path, options = {}) {
  const search = new URLSearchParams({ path });
  if (options.adminOverrideConfirmed) search.set("admin_override_confirmed", "true");
  return request(`/api/skills/entry?${search.toString()}`, {
    method: "DELETE"
  });
}

async function consumeEventStream(response, onEvent, onOpen) {
  if (!response.ok) {
    let detail = "请求失败，请稍后重试。";
    try {
      const payload = await response.json();
      detail = payload.detail || payload.message || detail;
    } catch {
      detail = response.statusText || detail;
    }
    const error = new Error(detail);
    error.status = response.status;
    if (response.status === 401) {
      notifyAuthExpired(detail);
    }
    throw error;
  }

  if (!response.body) {
    throw new Error("浏览器不支持流式响应。");
  }

  onOpen?.(response);
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });

    const frames = buffer.split("\n\n");
    buffer = frames.pop() || "";

    for (const frame of frames) {
      const lines = frame.split(/\r?\n/);
      for (const line of lines) {
        if (!line.startsWith("data: ")) {
          continue;
        }
        const payload = line.slice(6);
        if (payload === "[DONE]") {
          return;
        }
        const event = JSON.parse(payload);
        if (event.type === "error") {
          throw new Error(event.message || "Skill 创建失败。");
        }
        onEvent?.(event);
      }
    }

    if (done) {
      break;
    }
  }
}

export async function streamCreateSkill({ payload, onEvent, onOpen, signal }) {
  const response = await fetchWithTimeout("/api/skills/stream", {
    method: "POST",
    headers: {
      "Content-Type": "application/json"
    },
    signal,
    timeoutMs: defaultStreamConnectTimeoutMs,
    timeoutMessage: "连接 Skill 创建 agent 超时，请确认后端服务已启动。",
    body: JSON.stringify(payload)
  });

  return consumeEventStream(response, onEvent, onOpen);
}
