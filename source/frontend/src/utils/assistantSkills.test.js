import assert from "node:assert/strict";
import test from "node:test";
import { buildAtSkillSuggestions, buildSkillSourceUri, matchAssistantSkills } from "./assistantSkills.js";

const skills = [
  { skill_id: "prd-writer:SKILL.md", name: "prd-writer", description: "按模板撰写 PRD 文档。" },
  { skill_id: "ui-design:SKILL.md", name: "ui-design", description: "生成页面视觉设计稿。" },
  { skill_id: "nested:test-data-builder:SKILL.md", name: "test-data-builder", description: "构造测试数据。" }
];

test("matchAssistantSkills 空 query 返回全部 skill", () => {
  assert.deepEqual(matchAssistantSkills(skills, ""), skills);
  assert.deepEqual(matchAssistantSkills(skills, "  "), skills);
});

test("matchAssistantSkills 对非法输入保持安全", () => {
  assert.deepEqual(matchAssistantSkills(null, "prd"), []);
  assert.deepEqual(matchAssistantSkills([null, skills[0]], "prd"), [skills[0]]);
});

test("matchAssistantSkills name 命中优先于 description 命中", () => {
  const mixed = [
    { skill_id: "a:SKILL.md", name: "alpha", description: "包含 prd 关键词的描述。" },
    { skill_id: "b:SKILL.md", name: "prd-writer", description: "撰写文档。" }
  ];
  const matched = matchAssistantSkills(mixed, "prd");
  assert.deepEqual(
    matched.map((skill) => skill.name),
    ["prd-writer", "alpha"]
  );
});

test("matchAssistantSkills 大小写不敏感且受 limit 限制", () => {
  const many = Array.from({ length: 12 }, (_, index) => ({
    skill_id: `skill-${index}:SKILL.md`,
    name: `skill-${index}`,
    description: ""
  }));
  assert.equal(matchAssistantSkills(many, "SKILL").length, 8);
  assert.equal(matchAssistantSkills(many, "skill", { limit: 3 }).length, 3);
});

test("buildSkillSourceUri 将冒号形式 skill_id 还原为 SKILL.md 相对路径", () => {
  assert.equal(buildSkillSourceUri("prd-writer:SKILL.md"), "workspace/.claude/skills/prd-writer/SKILL.md");
  assert.equal(
    buildSkillSourceUri("nested:test-data-builder:SKILL.md"),
    "workspace/.claude/skills/nested/test-data-builder/SKILL.md"
  );
});

test("buildAtSkillSuggestions 生成 @ 候选结构", () => {
  const suggestions = buildAtSkillSuggestions(skills, "prd");
  assert.equal(suggestions.length, 1);
  assert.deepEqual(suggestions[0], {
    key: "at-skill:prd-writer:SKILL.md",
    type: "skill",
    label: "@prd-writer",
    tone: "olive",
    hint: "按模板撰写 PRD 文档。",
    sourceType: "skill",
    sourceUri: "workspace/.claude/skills/prd-writer/SKILL.md",
    metadata: { skill_id: "prd-writer:SKILL.md", skill_name: "prd-writer" }
  });
});

test("buildAtSkillSuggestions 空 query 输出全部候选", () => {
  const suggestions = buildAtSkillSuggestions(skills, "");
  assert.deepEqual(
    suggestions.map((suggestion) => suggestion.label),
    ["@prd-writer", "@ui-design", "@test-data-builder"]
  );
});
