from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any


class AssistantRuntimeError(RuntimeError):
    """Assistant Runtime 执行异常。"""


@dataclass(slots=True)
class RuntimeWorkspaceMount:
    workspace_id: str
    workspace_key: str
    host_path: str
    sandbox_path: str
    permission: str

    def to_dict(self) -> dict[str, str]:
        return {
            "workspace_id": self.workspace_id,
            "workspace_key": self.workspace_key,
            "sandbox_path": self.sandbox_path,
            "permission": self.permission,
        }


@dataclass(slots=True)
class RuntimeWorkspacePlan:
    user_id: str
    session_id: str
    organization_key: str
    sandbox_cwd: str
    host_shadow_root: str
    mounts: list[RuntimeWorkspaceMount]

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "session_id": self.session_id,
            "organization_key": self.organization_key,
            "sandbox_cwd": self.sandbox_cwd,
            "mounts": [mount.to_dict() for mount in self.mounts],
        }


@dataclass(slots=True)
class RuntimeDynamicToolResult:
    content: str
    is_error: bool = False


@dataclass(slots=True)
class RuntimeDynamicTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], Awaitable[RuntimeDynamicToolResult]]


@dataclass(slots=True)
class RuntimeSession:
    provider: str
    working_directory: str
    session_id: str | None = None
    system_prompt: str = ""
    workspace_plan: RuntimeWorkspacePlan | None = None
    tool_allowlist: tuple[str, ...] | None = None
    dynamic_tools: list[RuntimeDynamicTool] = field(default_factory=list)
    dynamic_read_roots: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RuntimeSkill:
    skill_id: str
    name: str
    description: str
    path: str


@dataclass(slots=True)
class RuntimeEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)
