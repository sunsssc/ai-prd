from app.integrations.agent_runtime.base import discover_project_skills


def test_discover_project_skills_reads_frontmatter_description(tmp_path) -> None:
    skill_dir = tmp_path / "skills/test-data-builder"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: test-data-builder
description: 通过对话安全规划并执行测试造数
---

# 测试造数
""",
        encoding="utf-8",
    )

    skills = discover_project_skills(str(tmp_path / "skills"))

    assert len(skills) == 1
    assert skills[0].name == "test-data-builder"
    assert skills[0].description == "通过对话安全规划并执行测试造数"
    assert skills[0].skill_id == "test-data-builder:SKILL.md"
