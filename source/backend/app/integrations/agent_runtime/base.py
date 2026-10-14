from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Protocol

from app.integrations.agent_runtime.models import (
    RuntimeDynamicTool,
    RuntimeEvent,
    RuntimeSession,
    RuntimeSkill,
    RuntimeWorkspacePlan,
)


class AgentRuntimeClient(Protocol):
    provider: str
    supports_local_images: bool

    async def create_or_resume_session(
        self,
        *,
        runtime_session_id: str | None,
        working_directory: str | None = None,
        workspace_plan: RuntimeWorkspacePlan | None = None,
        tool_allowlist: tuple[str, ...] | None = None,
        dynamic_tools: list[RuntimeDynamicTool] | None = None,
        dynamic_read_roots: list[str] | None = None,
        system_prompt: str,
    ) -> RuntimeSession:
        ...

    async def send_message_stream(
        self,
        *,
        session: RuntimeSession,
        message: str,
        metadata: dict[str, Any],
    ) -> AsyncIterator[RuntimeEvent]:
        ...

    async def list_available_skills(
        self,
        *,
        working_directory: str,
    ) -> list[RuntimeSkill]:
        ...


def discover_project_skills(skills_root: str) -> list[RuntimeSkill]:
    skills_root = Path(skills_root)
    if not skills_root.exists():
        return []

    skills: list[RuntimeSkill] = []
    for skill_file in sorted(skills_root.rglob("SKILL.md")):
        relative_path = skill_file.relative_to(skills_root).as_posix()
        description = _extract_skill_description(skill_file)
        skills.append(
            RuntimeSkill(
                skill_id=relative_path.replace("/", ":"),
                name=skill_file.parent.name,
                description=description,
                path=str(skill_file),
            )
        )
    return skills


def _extract_skill_description(skill_file: Path) -> str:
    lines = skill_file.read_text(encoding="utf-8").splitlines()
    if lines and lines[0].strip() == "---":
        for line in lines[1:]:
            stripped = line.strip()
            if stripped == "---":
                break
            if stripped.startswith("description:"):
                description = stripped.split(":", 1)[1].strip().strip('"').strip("'")
                if description:
                    return description[:120]
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped == "---" or stripped.startswith("#"):
            continue
        return stripped[:120]
    return f"{skill_file.parent.name} skill"
