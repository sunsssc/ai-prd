export function normalizePersonalWorkspaceFilePath(value = "") {
  let normalized = String(value || "").trim().replace(/\\/g, "/");
  if (!normalized) {
    return "";
  }

  if (normalized.startsWith("sandbox:")) {
    normalized = normalized.slice("sandbox:".length);
  }

  const queryIndex = normalized.search(/[?#]/);
  if (queryIndex >= 0) {
    normalized = normalized.slice(0, queryIndex);
  }
  try {
    normalized = decodeURI(normalized);
  } catch {
    return "";
  }
  normalized = normalized.trim().replace(/^\/+/, "");
  if (!normalized || normalized.includes("..")) {
    return "";
  }

  if (normalized.startsWith("me/")) {
    return normalized;
  }
  if (normalized.startsWith("workspace/users/")) {
    return normalized;
  }
  return "";
}

export function buildPersonalWorkspaceFileUrl(path = "") {
  const normalized = normalizePersonalWorkspaceFilePath(path);
  if (!normalized) {
    return "";
  }
  const search = new URLSearchParams({ path: normalized });
  return `/api/workspace/me/file?${search.toString()}`;
}

export function resolvePersonalWorkspaceLinkHref(href = "") {
  return buildPersonalWorkspaceFileUrl(href) || href;
}
