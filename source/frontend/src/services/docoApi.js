import { requestJson } from "./http.js";

export function getDocoSettings() {
  return requestJson("/api/assistant/doco/settings");
}

export function updateDocoSettings({ apiToken, defaultKnowledgeBaseId }) {
  return requestJson("/api/assistant/doco/settings", {
    method: "PUT",
    body: JSON.stringify({
      api_token: apiToken || null,
      default_knowledge_base_id: defaultKnowledgeBaseId || null
    })
  });
}

export function deleteDocoSettings() {
  return requestJson("/api/assistant/doco/settings", {
    method: "DELETE"
  });
}
