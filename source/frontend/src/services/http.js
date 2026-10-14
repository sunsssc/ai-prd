export const defaultRequestTimeoutMs = 30000;
export const defaultStreamConnectTimeoutMs = 60000;

function formatValidationError(item) {
  if (typeof item === "string") {
    return item;
  }
  if (!item || typeof item !== "object") {
    return String(item ?? "");
  }

  let message = item.msg || item.message || "请求参数无效。";
  if (item.type === "string_too_long" && item.ctx?.max_length) {
    message = `不能超过 ${item.ctx.max_length} 个字符。`;
  } else if (item.type === "string_too_short" && item.ctx?.min_length) {
    message = `不能少于 ${item.ctx.min_length} 个字符。`;
  }

  const location = Array.isArray(item.loc)
    ? item.loc
        .filter((part) => !["body", "query", "path"].includes(part))
        .map((part, index, parts) => {
          if (typeof part === "number") {
            return `[${part}]`;
          }
          return index > 0 && typeof parts[index - 1] === "number" ? `.${part}` : part;
        })
        .join("")
    : "";
  return location ? `${location}：${message}` : message;
}

export function formatErrorDetail(detail, fallback = "请求失败，请稍后重试。") {
  if (Array.isArray(detail)) {
    const message = detail.map(formatValidationError).filter(Boolean).join("；");
    return message || fallback;
  }
  if (typeof detail === "string" && detail.trim()) {
    return detail;
  }
  if (detail && typeof detail === "object") {
    return formatValidationError(detail) || fallback;
  }
  return fallback;
}

export async function fetchWithTimeout(path, options = {}) {
  const {
    timeoutMs = defaultRequestTimeoutMs,
    timeoutMessage = "请求超时，请确认后端服务已启动。",
    ...fetchOptions
  } = options;
  const controller = timeoutMs ? new AbortController() : null;
  let timedOut = false;
  const timeoutId = controller
    ? window.setTimeout(() => {
        timedOut = true;
        controller.abort();
      }, timeoutMs)
    : null;

  if (controller && fetchOptions.signal) {
    fetchOptions.signal.addEventListener("abort", () => controller.abort(), { once: true });
  }

  try {
    return await fetch(path, {
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
        ...(fetchOptions.headers || {})
      },
      ...fetchOptions,
      signal: controller?.signal || fetchOptions.signal
    });
  } catch (error) {
    if (error.name === "AbortError" && timedOut) {
      throw new Error(timeoutMessage);
    }
    throw error;
  } finally {
    if (timeoutId) {
      window.clearTimeout(timeoutId);
    }
  }
}

export async function requestJson(path, options = {}) {
  const {
    defaultErrorMessage = "请求失败，请稍后重试。",
    onUnauthorized,
    ...fetchOptions
  } = options;
  const response = await fetchWithTimeout(path, fetchOptions);

  if (response.status === 204) {
    return null;
  }

  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }

  if (!response.ok) {
    const detail = formatErrorDetail(
      payload?.detail || payload?.reason || payload?.message,
      response.statusText || defaultErrorMessage
    );
    const error = new Error(detail);
    error.status = response.status;
    error.payload = payload;
    if (response.status === 401) {
      onUnauthorized?.(detail);
    }
    throw error;
  }

  return payload;
}
