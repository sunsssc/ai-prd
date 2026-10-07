import { normalizeKnowledgeSearchText } from "./knowledgeIndex.js";

const DEFAULT_SKILL_LIMIT = 8;

// skill_id 是后端发现根下的相对路径冒号形式（如 prd-writer:SKILL.md），
// 冒号还原为斜杠即为 workspace/.claude/skills 下的相对路径。
export function buildSkillSourceUri(skillId) {
  return `workspace/.claude/skills/${String(skillId || "").replaceAll(":", "/")}`;
}

export function matchAssistantSkills(skills, query = "", { limit = DEFAULT_SKILL_LIMIT } = {}) {
  const list = Array.isArray(skills) ? skills.filter(Boolean) : [];
  const normalizedQuery = normalizeKnowledgeSearchText(query);
  if (!normalizedQuery) {
    return [...list];
  }
  const nameMatches = [];
  const descriptionMatches = [];
  for (const skill of list) {
    const name = normalizeKnowledgeSearchText(skill?.name);
    if (name && name.includes(normalizedQuery)) {
      nameMatches.push(skill);
      continue;
    }
    const description = normalizeKnowledgeSearchText(skill?.description);
    if (description && description.includes(normalizedQuery)) {
      descriptionMatches.push(skill);
    }
  }
  return [...nameMatches, ...descriptionMatches].slice(0, limit);
}

export function buildAtSkillSuggestions(skills, query = "") {
  return matchAssistantSkills(skills, query).map((skill) => ({
    key: `at-skill:${skill.skill_id}`,
    type: "skill",
    label: `@${skill.name}`,
    tone: "olive",
    hint: skill.description || "",
    sourceType: "skill",
    sourceUri: buildSkillSourceUri(skill.skill_id),
    metadata: { skill_id: skill.skill_id, skill_name: skill.name }
  }));
}
