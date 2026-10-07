import { requestJson } from "./http.js";

async function request(path, options = {}) {
  return requestJson(path, {
    timeoutMs: 5000,
    timeoutMessage: "请求超时，请确认后端服务已启动。",
    ...options
  });
}

export async function getCurrentUser() {
  try {
    const payload = await request("/api/auth/me", {
      timeoutMs: 5000,
      timeoutMessage: "登录状态验证超时，请直接重新登录。"
    });
    return payload?.user || null;
  } catch (error) {
    if (error.status === 401) {
      return null;
    }
    throw error;
  }
}

export async function getGoogleAuthorizationUrl() {
  const payload = await request("/api/auth/google/url");
  if (!payload?.authorization_url) {
    throw new Error("Google 登录地址获取失败，请稍后重试。");
  }
  return payload.authorization_url;
}

export function getAuthConfig() {
  return request("/api/auth/config");
}

export function requestEmailCode(email, purpose) {
  return request("/api/auth/email/request-code", {
    method: "POST",
    body: JSON.stringify({ email, purpose })
  });
}

export function revealEmailCode(email, purpose) {
  return request("/api/auth/email/reveal-code", {
    method: "POST",
    body: JSON.stringify({ email, purpose })
  });
}

export function registerWithEmail(email, name, code) {
  return request("/api/auth/email/register", {
    method: "POST",
    body: JSON.stringify({ email, name, code })
  });
}

export function loginWithEmail(email, code) {
  return request("/api/auth/email/login", {
    method: "POST",
    body: JSON.stringify({ email, code })
  });
}

export function logout() {
  return request("/api/auth/logout", {
    method: "POST"
  });
}

export function requestAgentAccess(reason) {
  return request("/api/agent-access/request", {
    method: "POST",
    body: JSON.stringify({ reason })
  });
}

export function getAgentAccessStatus() {
  return request("/api/agent-access/me");
}
