import assert from "node:assert/strict";
import test from "node:test";
import { clearSkillCreateDraft, readSkillCreateDraft, saveSkillCreateDraft } from "./skillCreateDraft.js";

function createMemoryStorage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key)
  };
}

test("按用户保存并恢复 Skill 导入草稿", () => {
  const storage = createMemoryStorage();
  const currentUser = { user_id: "user-1", email: "one@example.com" };
  const draft = {
    createForm: { mode: "zip", intent: "", name: "", description: "", content: "", files: [] },
    zipPreview: {
      token: "preview-token",
      archive_name: "demo.zip",
      folderName: "demo",
      skillMdMode: "generated",
      skillMdContent: "# Demo",
      generation_status: "completed",
      generation_message: "SKILL.md 已生成，可编辑后确认导入。",
      generated_skill_md_content: "# Demo",
      entries: [{ path: "SKILL.md", kind: "file", size: 6 }]
    },
    zipGenerating: false,
    zipGenerationStatus: "SKILL.md 已生成"
  };

  saveSkillCreateDraft(currentUser, draft, storage);

  assert.deepEqual(readSkillCreateDraft(currentUser, storage), { version: 1, ...draft });
  assert.equal(readSkillCreateDraft({ user_id: "user-2" }, storage), null);
});

test("清除 Skill 导入草稿", () => {
  const storage = createMemoryStorage();
  const currentUser = { user_id: "user-1" };
  saveSkillCreateDraft(currentUser, { createForm: { mode: "zip" }, zipPreview: null }, storage);
  clearSkillCreateDraft(currentUser, storage);
  assert.equal(readSkillCreateDraft(currentUser, storage), null);
});
