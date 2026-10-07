export const authExpiredEventName = "ai-prd:auth-expired";

export function notifyAuthExpired(message = "登录状态已过期，请重新登录。") {
  window.dispatchEvent(
    new CustomEvent(authExpiredEventName, {
      detail: { message }
    })
  );
}
