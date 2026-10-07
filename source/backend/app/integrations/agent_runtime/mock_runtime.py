from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

from app.integrations.agent_runtime.base import discover_project_skills
from app.integrations.agent_runtime.models import (
    RuntimeDynamicTool,
    RuntimeEvent,
    RuntimeSession,
    RuntimeSkill,
    RuntimeWorkspacePlan,
)


class MockAgentRuntimeClient:
    provider = "mock"
    supports_local_images = True

    def __init__(self, *, skills_root: str | None = None) -> None:
        self.skills_root = skills_root

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
        if workspace_plan is not None:
            agent_cwd = Path(workspace_plan.host_shadow_root).resolve()
        elif working_directory is not None:
            working_path = Path(working_directory).resolve()
            agent_cwd = working_path if working_path.name == "workspace" else working_path / "workspace"
        else:
            raise ValueError("working_directory 或 workspace_plan 至少需要提供一个。")
        agent_cwd.mkdir(parents=True, exist_ok=True)
        return RuntimeSession(
            provider=self.provider,
            working_directory=str(agent_cwd),
            session_id=runtime_session_id or str(uuid4()),
            system_prompt=system_prompt,
            workspace_plan=workspace_plan,
            tool_allowlist=tool_allowlist,
            dynamic_tools=list(dynamic_tools or []),
            dynamic_read_roots=list(dynamic_read_roots or []),
        )

    async def send_message_stream(
        self,
        *,
        session: RuntimeSession,
        message: str,
        metadata: dict[str, object],
    ) -> AsyncIterator[RuntimeEvent]:
        session_id = session.session_id or str(uuid4())
        skills = await self.list_available_skills(working_directory=session.working_directory)
        yield RuntimeEvent(
            "session",
            {
                "provider": self.provider,
                "session_id": session_id,
                "working_directory": session.working_directory,
                "organization_key": session.workspace_plan.organization_key if session.workspace_plan else None,
            },
        )

        if skills:
            first_skill = skills[0]
            yield RuntimeEvent(
                "skill_use",
                {
                    "skill_id": first_skill.skill_id,
                    "skill_name": first_skill.name,
                    "path": first_skill.path,
                },
            )

        yield RuntimeEvent(
            "search",
            {
                "query": _build_search_query(message),
                "target": "组织知识库与用户个人空间",
            },
        )

        inspected_paths = _candidate_paths(session)
        for inspected_path in inspected_paths:
            yield RuntimeEvent(
                "tool_use",
                {
                    "tool_name": "Read",
                    "tool_input": {"file_path": inspected_path},
                },
            )
            yield RuntimeEvent(
                "read",
                {
                    "path": inspected_path,
                    "excerpt": f"mock runtime 已读取 {Path(inspected_path).name}",
                },
            )
            yield RuntimeEvent(
                "tool_result",
                {
                    "tool_name": "Read",
                    "content": f"已读取 {inspected_path}",
                    "is_error": False,
                },
            )

        reply = _build_mock_reply(
            message=message,
            session_id=session_id,
            inspected_paths=inspected_paths,
            skill=skills[0] if skills else None,
            metadata=metadata,
        )

        for chunk in _chunk_text(reply, 32):
            yield RuntimeEvent("delta", {"text": chunk})

        yield RuntimeEvent(
            "message",
            {
                "content": reply,
                "session_id": session_id,
            },
        )
        yield RuntimeEvent(
            "usage",
            {
                "input_tokens": max(len(message) * 2, 1),
                "output_tokens": max(len(reply), 1),
            },
        )
        yield RuntimeEvent(
            "complete",
            {
                "session_id": session_id,
                "result": reply,
            },
        )

    async def list_available_skills(
        self,
        *,
        working_directory: str,
    ) -> list[RuntimeSkill]:
        fallback_root = Path(working_directory)
        if fallback_root.name == "workspace":
            fallback_root = fallback_root / ".claude/skills"
        else:
            fallback_root = fallback_root / "workspace/.claude/skills"
        return discover_project_skills(self.skills_root or str(fallback_root))


def _candidate_paths(session: RuntimeSession) -> list[str]:
    if session.workspace_plan is not None:
        knowledge_mounts = [
            mount.sandbox_path
            for mount in session.workspace_plan.mounts
            if mount.workspace_key.startswith("org:") and mount.workspace_key.endswith(":knowledge")
            and Path(mount.host_path).exists()
        ]
        return sorted(knowledge_mounts)

    candidates = [
        Path(session.working_directory) / "knowledge/requirements",
        Path(session.working_directory) / "knowledge/business-docs",
    ]
    return [str(path) for path in candidates if path.exists()]


def _build_search_query(message: str) -> str:
    normalized = " ".join(message.strip().split())
    return normalized[:48] or "当前问题"


def _build_mock_reply(
    *,
    message: str,
    session_id: str,
    inspected_paths: list[str],
    skill: RuntimeSkill | None,
    metadata: dict[str, object],
) -> str:
    lines = [
        "已进入 mock agent runtime 执行链路。",
        f"当前 Runtime 会话 ID：{session_id}",
        f"收到的问题：{message.strip()}",
    ]

    if metadata.get("session_mount_count"):
        lines.append(f"会话级上下文挂载数：{metadata['session_mount_count']}")
    if metadata.get("requested_context_count"):
        lines.append(f"本轮指定上下文数：{metadata['requested_context_count']}")
    organization_key = metadata.get("organization_key")
    if organization_key:
        lines.append(f"当前组织：{organization_key}")
    if skill is not None:
        lines.append(f"已发现并模拟启用 Skill：{skill.name}")
    if inspected_paths:
        lines.append("本轮模拟读取的文件：")
        lines.extend(f"- {path}" for path in inspected_paths)

    lines.append("当前环境尚未安装真实 Claude Code SDK/CLI，因此这是可落库、可追踪、可恢复的 mock runtime 回复。")
    return "\n\n".join(lines)


def _chunk_text(text: str, size: int) -> list[str]:
    return [text[index : index + size] for index in range(0, len(text), size)]
