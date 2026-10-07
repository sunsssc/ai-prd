import { buildStableContextKey } from "./contextKeys.js";

const PR_STATUS_META = {
  draft: { label: "Draft", tone: "neutral" },
  open: { label: "Open", tone: "olive" },
  merged: { label: "Merged", tone: "sand" },
  closed: { label: "Closed", tone: "signal" }
};

export function prStatusMeta(state) {
  return PR_STATUS_META[state] || { label: state || "未知", tone: "neutral" };
}

export function shortRepoName(repoFullName) {
  const parts = String(repoFullName || "").split("/").filter(Boolean);
  return parts[parts.length - 1] || repoFullName || "";
}

export function buildTaskRequirementReference(task) {
  return {
    key: buildStableContextKey("knowledge", task.source_path),
    tone: "signal",
    label: `需求：${task.title}`,
    scopeId: "requirements",
    sourceUri: task.source_path
  };
}
