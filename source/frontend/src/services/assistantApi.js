import { notifyAuthExpired } from "./authEvents.js";
import { apiWithCache, invalidateApiCache } from "./cache.js";
import { defaultStreamConnectTimeoutMs, fetchWithTimeout, formatErrorDetail, requestJson } from "./http.js";

export const assistantUploadMaxBytes = 10 * 1024 * 1024;
export const assistantUploadMaxSizeLabel = "10 MB";

function assertAssistantUploadSize(file, label = "文件") {
  if (typeof file?.size === "number" && file.size > assistantUploadMaxBytes) {
    throw new Error(`${label}不能超过 ${assistantUploadMaxSizeLabel}。`);
  }
}

async function request(path, options = {}) {
  return requestJson(path, {
    timeoutMs: 30000,
    timeoutMessage: "请求超时，请确认后端服务已启动。",
    onUnauthorized: notifyAuthExpired,
    ...options
  });
}

export function listAssistantSessions({ query, favoritedOnly, limit, offset } = {}) {
  const search = new URLSearchParams();
  if (query) {
    search.set("query", query);
  }
  if (favoritedOnly) {
    search.set("favorited_only", "true");
  }
  if (typeof limit === "number") {
    search.set("limit", String(limit));
  }
  if (typeof offset === "number") {
    search.set("offset", String(offset));
  }
  const suffix = search.toString();
  return request(`/api/assistant/sessions${suffix ? `?${suffix}` : ""}`, { cache: "no-store" });
}

export function favoriteAssistantSession(sessionId) {
  return request(`/api/assistant/sessions/${sessionId}/favorite`, { method: "PUT" });
}

export function unfavoriteAssistantSession(sessionId) {
  return request(`/api/assistant/sessions/${sessionId}/favorite`, { method: "DELETE" });
}

export function getAssistantSession(sessionId) {
  return request(`/api/assistant/sessions/${sessionId}`, { cache: "no-store" });
}

export function decideAssistantMcpApproval(turnId, approvalId, decision) {
  return request(
    `/api/assistant/turns/${encodeURIComponent(turnId)}/mcp-approvals/${encodeURIComponent(approvalId)}`,
    { method: "POST", body: JSON.stringify({ decision }) }
  );
}

export function revealAssistantTestDataPlanSecret(sessionId, planId) {
  return request(
    `/api/assistant/sessions/${encodeURIComponent(sessionId)}/test-data-plans/${encodeURIComponent(planId)}/reveal-secret`,
    { method: "POST" }
  );
}

export function getImageGenerationQuota() {
  return request("/api/assistant/image-generation/quota");
}

export function searchAssistantUsers(query) {
  const search = new URLSearchParams();
  if (query) {
    search.set("query", query);
  }
  search.set("limit", "20");
  return request(`/api/assistant/users?${search.toString()}`);
}

export function getAssistantSessionShare(sessionId) {
  return request(`/api/assistant/sessions/${sessionId}/share`);
}

export function saveAssistantSessionShare(sessionId, payload) {
  return request(`/api/assistant/sessions/${sessionId}/share`, {
    method: "PUT",
    body: JSON.stringify(payload)
  });
}

export function revokeAssistantSessionShare(sessionId) {
  return request(`/api/assistant/sessions/${sessionId}/share`, {
    method: "DELETE"
  });
}

export function listSharedAssistantSessions() {
  return request("/api/assistant/shared", { cache: "no-store" });
}

export function getSharedAssistantSession(shareToken) {
  return request(`/api/assistant/shared/${encodeURIComponent(shareToken)}`, { cache: "no-store" });
}

export function createSharedAssistantFollowUp(shareToken, message) {
  return request(`/api/assistant/shared/${encodeURIComponent(shareToken)}/follow-ups`, {
    method: "POST",
    body: JSON.stringify({ message })
  });
}

export function rebuildSharedAssistantRuntime(shareToken, requestId) {
  return request(
    `/api/assistant/shared/${encodeURIComponent(shareToken)}/follow-ups/${encodeURIComponent(requestId)}/rebuild-runtime`,
    { method: "POST" }
  );
}

export function listAssistantSessionComments(sessionId, messageId) {
  const search = new URLSearchParams();
  if (messageId) {
    search.set("message_id", messageId);
  }
  const suffix = search.toString();
  return request(`/api/assistant/sessions/${sessionId}/comments${suffix ? `?${suffix}` : ""}`);
}

export function createAssistantSessionComment(sessionId, payload) {
  return request(`/api/assistant/sessions/${sessionId}/comments`, {
    method: "POST",
    body: JSON.stringify(payload)
  });
}

export function updateAssistantSessionComment(commentId, content) {
  return request(`/api/assistant/comments/${commentId}`, {
    method: "PATCH",
    body: JSON.stringify({ content })
  });
}

export function deleteAssistantSessionComment(commentId) {
  return request(`/api/assistant/comments/${commentId}`, {
    method: "DELETE"
  });
}

export function getAssistantLatestTurnTrace(sessionId) {
  return request(`/api/assistant/sessions/${sessionId}/latest-trace`);
}

// Skill 可能在 Skill 管理页同 SPA 内新建/编辑，TTL 短于知识索引。
const ASSISTANT_SKILLS_TTL = 60000;

export function getAssistantSkills(options = {}) {
  const url = "/api/assistant/skills";
  if (options.force) {
    invalidateApiCache(url);
  }
  return apiWithCache(url, ASSISTANT_SKILLS_TTL, () => request(url));
}

export function createAssistantSession(title, organizationKey = null) {
  return request("/api/assistant/sessions", {
    method: "POST",
    body: JSON.stringify({ title, organization_key: organizationKey || null })
  });
}

export function renameAssistantSession(sessionId, title) {
  return request(`/api/assistant/sessions/${sessionId}`, {
    method: "PATCH",
    body: JSON.stringify({ title })
  });
}

export function deleteAssistantSession(sessionId) {
  return request(`/api/assistant/sessions/${sessionId}`, {
    method: "DELETE"
  });
}

export function setAssistantMessageFeedback(messageId, feedback) {
  return request(`/api/assistant/messages/${messageId}/feedback`, {
    method: "PUT",
    body: JSON.stringify({ feedback })
  });
}

export function replaceAssistantSessionMounts(sessionId, mounts) {
  return request(`/api/assistant/sessions/${sessionId}/mounts`, {
    method: "POST",
    body: JSON.stringify({ mounts })
  });
}

export function sendAssistantMessage(message, sessionId, organizationKey = null) {
  return request("/api/assistant/chat", {
    method: "POST",
    body: JSON.stringify({
      message,
      session_id: sessionId || null,
      organization_key: organizationKey || null
    })
  });
}

export async function uploadAssistantImage(file) {
  assertAssistantUploadSize(file, "图片");
  const search = new URLSearchParams();
  search.set("filename", file.name || "image");
  const response = await fetchWithTimeout(`/api/assistant/uploads/images?${search.toString()}`, {
    method: "POST",
    headers: {
      "Content-Type": file.type || "application/octet-stream"
    },
    body: file,
    timeoutMs: 30000,
    timeoutMessage: "图片上传超时，请稍后重试。"
  });

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const detail = formatErrorDetail(payload?.detail || payload?.message, response.statusText || "图片上传失败。");
    const error = new Error(detail);
    error.status = response.status;
    if (response.status === 401) {
      notifyAuthExpired(detail);
    }
    throw error;
  }

  return payload;
}

export async function uploadAssistantFile(file) {
  assertAssistantUploadSize(file, "文件");
  const search = new URLSearchParams();
  search.set("filename", file.name || "file");
  const response = await fetchWithTimeout(`/api/assistant/uploads/files?${search.toString()}`, {
    method: "POST",
    headers: {
      "Content-Type": file.type || "application/octet-stream"
    },
    body: file,
    timeoutMs: 30000,
    timeoutMessage: "文件上传超时，请稍后重试。"
  });

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const detail = formatErrorDetail(payload?.detail || payload?.message, response.statusText || "文件上传失败。");
    const error = new Error(detail);
    error.status = response.status;
    if (response.status === 401) {
      notifyAuthExpired(detail);
    }
    throw error;
  }

  return payload;
}

async function consumeEventStream(response, onEvent, onOpen) {
  if (!response.ok) {
    let detail = "请求失败，请稍后重试。";
    try {
      const payload = await response.json();
      detail = formatErrorDetail(payload?.detail || payload?.message, detail);
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
          onEvent?.(event);
          const error = new Error(event.message || "流式对话失败。");
          error.isTerminalStreamError = true;
          throw error;
        }
        onEvent?.(event);
      }
    }

    if (done) {
      break;
    }
  }
}

async function openEventStream(path, { method = "GET", body, signal, onEvent, onOpen }) {
  const hasBody = body !== undefined;
  const response = await fetchWithTimeout(path, {
    method,
    headers: hasBody
      ? {
          "Content-Type": "application/json"
        }
      : undefined,
    signal,
    timeoutMs: defaultStreamConnectTimeoutMs,
    timeoutMessage: "连接 AI 助手超时，请确认后端服务已启动。",
    body: hasBody ? JSON.stringify(body) : undefined
  });

  return consumeEventStream(response, onEvent, onOpen);
}

export async function streamAssistantMessage({
  message,
  sessionId,
  organizationKey = null,
  contextItems = [],
  onEvent,
  onOpen,
  signal
}) {
  return openEventStream("/api/assistant/chat/stream", {
    method: "POST",
    signal,
    onEvent,
    onOpen,
    body: {
      message,
      session_id: sessionId || null,
      organization_key: organizationKey || null,
      context_items: contextItems
    }
  });
}

export async function resumeAssistantTurnStream({ turnId, onEvent, onOpen, signal }) {
  return openEventStream(`/api/assistant/turns/${turnId}/stream`, {
    signal,
    onEvent,
    onOpen
  });
}

export async function resumeSharedAssistantTurnStream({ shareToken, turnId, onEvent, onOpen, signal }) {
  return openEventStream(
    `/api/assistant/shared/${encodeURIComponent(shareToken)}/turns/${encodeURIComponent(turnId)}/stream`,
    {
      signal,
      onEvent,
      onOpen
    }
  );
}

export function stopAssistantTurn(turnId) {
  return request(`/api/assistant/turns/${turnId}/stop`, {
    method: "POST"
  });
}

export async function rerunAssistantTurnStream({ turnId, onEvent, onOpen, signal }) {
  return openEventStream(`/api/assistant/turns/${turnId}/rerun/stream`, {
    method: "POST",
    signal,
    onEvent,
    onOpen
  });
}
