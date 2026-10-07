const SKILL_CREATE_DRAFT_VERSION = 1;
const STORAGE_PREFIX = "ai-prd.skill-create-draft";

function getStorageKey(currentUser) {
  const userId = currentUser?.user_id || currentUser?.email || "";
  return userId ? `${STORAGE_PREFIX}:${encodeURIComponent(userId)}` : "";
}

export function readSkillCreateDraft(currentUser, storage = globalThis.sessionStorage) {
  const key = getStorageKey(currentUser);
  if (!key || !storage) return null;

  try {
    const payload = JSON.parse(storage.getItem(key) || "null");
    if (
      payload?.version !== SKILL_CREATE_DRAFT_VERSION ||
      !payload.createForm ||
      typeof payload.createForm !== "object" ||
      (payload.zipPreview !== null && typeof payload.zipPreview !== "object")
    ) {
      return null;
    }
    return payload;
  } catch {
    return null;
  }
}

export function saveSkillCreateDraft(currentUser, draft, storage = globalThis.sessionStorage) {
  const key = getStorageKey(currentUser);
  if (!key || !storage) return;

  try {
    storage.setItem(key, JSON.stringify({ version: SKILL_CREATE_DRAFT_VERSION, ...draft }));
  } catch {
    // 浏览器禁用会话存储或草稿超过配额时，不阻断当前创建流程。
  }
}

export function clearSkillCreateDraft(currentUser, storage = globalThis.sessionStorage) {
  const key = getStorageKey(currentUser);
  if (!key || !storage) return;

  try {
    storage.removeItem(key);
  } catch {
    // 清理失败不影响页面退出创建流程。
  }
}
