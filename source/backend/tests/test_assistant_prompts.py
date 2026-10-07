from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.business.assistant.knowledge_scope import KNOWLEDGE_SCOPES, build_knowledge_scope_instruction
from app.business.assistant.prompts import DEFAULT_ASSISTANT_SYSTEM_PROMPT, build_assistant_system_prompt
from app.integrations.agent_runtime.models import RuntimeWorkspaceMount, RuntimeWorkspacePlan


@dataclass(frozen=True, slots=True)
class PromptMount:
    label: str
    source_type: str


def test_build_assistant_system_prompt_uses_default_base_prompt_when_blank() -> None:
    prompt = build_assistant_system_prompt(
        base_prompt="",
        runtime_working_directory="/repo",
        agent_working_directory=Path("/repo/workspace"),
        session_mounts=[],
    )

    assert DEFAULT_ASSISTANT_SYSTEM_PROMPT in prompt
    assert "项目根目录：/repo" in prompt
    assert "Agent 工作目录：/repo/workspace" in prompt
    assert "请优先使用中文输出" in prompt
    assert "执行工具时使用相对路径" in prompt
    assert "组织知识库位于 `<organization_key>/knowledge`" in prompt
    assert "个人工作区位于 `me`" in prompt
    assert "写入约束：不得创建、修改或删除任何文件" in prompt
    assert "默认写入 `me`" not in prompt
    assert "HTML Artifact 约束" in prompt
    assert "默认使用浅色界面和白色或近白背景" in prompt
    assert "除非用户明确要求深色、夜间模式、黑色背景或大屏暗色风格" in prompt
    assert "不要只写 ClickUp Task ID" in prompt
    assert "链接指向对应的 workspace 内需求文档路径" in prompt


def test_build_assistant_system_prompt_includes_session_mounts() -> None:
    prompt = build_assistant_system_prompt(
        base_prompt="基础提示词",
        runtime_working_directory="/repo",
        agent_working_directory=Path("/repo/workspace"),
        session_mounts=[PromptMount(label="需求范围", source_type="requirement")],
    )

    assert prompt.startswith("基础提示词")
    assert "当前会话范围上下文：" in prompt
    assert "- 需求范围 (requirement)" in prompt


def test_build_assistant_system_prompt_includes_workspace_plan_without_host_paths() -> None:
    workspace_plan = RuntimeWorkspacePlan(
        user_id="user_1",
        session_id="session_1",
        organization_key="coinex",
        sandbox_cwd="/",
        host_shadow_root="/repo/workspace/runtime/sessions/session_1/root",
        mounts=[
            RuntimeWorkspaceMount(
                workspace_id="ws_knowledge",
                workspace_key="org:coinex:knowledge",
                host_path="/repo/workspace/coinex/knowledge",
                sandbox_path="/coinex/knowledge",
                permission="read",
            ),
            RuntimeWorkspaceMount(
                workspace_id="ws_me",
                workspace_key="user:user_1",
                host_path="/repo/workspace/users/user_1",
                sandbox_path="/me",
                permission="write",
            ),
        ],
    )

    prompt = build_assistant_system_prompt(
        base_prompt="",
        runtime_working_directory="/repo",
        agent_working_directory=Path("/repo/workspace"),
        session_mounts=[],
        workspace_plan=workspace_plan,
    )

    assert "当前 active organization：coinex" in prompt
    assert "当前沙箱工作目录：`/`" in prompt
    assert "执行工具时使用相对路径" in prompt
    assert "共享知识库和部门目录只读" in prompt
    assert "`me` 是当前用户个人工作区，可写" in prompt
    assert "`tmp` 是当前会话临时目录，可写" in prompt
    assert "默认写入 `me`" in prompt
    assert "不得创建、修改或删除任何文件" not in prompt
    assert "- `coinex/knowledge`：只读" in prompt
    assert "- `me`：可写" in prompt
    assert "- `/coinex/knowledge`：只读" not in prompt
    assert "/repo/workspace/coinex/knowledge" not in prompt
    assert "/repo/workspace/users/user_1" not in prompt


def test_build_assistant_system_prompt_delegates_git_scope_to_agent_tool() -> None:
    workspace_plan = RuntimeWorkspacePlan(
        user_id="user_1",
        session_id="session_1",
        organization_key="coinex",
        sandbox_cwd="/",
        host_shadow_root="/runtime/session_1/root",
        mounts=[],
    )

    prompt = build_assistant_system_prompt(
        base_prompt="",
        runtime_working_directory="/repo",
        agent_working_directory=Path("/repo/workspace"),
        session_mounts=[],
        workspace_plan=workspace_plan,
        git_dynamic_tool_enabled=True,
    )

    assert "每个普通 Turn 开始时使用 baseline" in prompt
    assert "自主调用 `mount_git_ref`" in prompt
    assert "可以为多个仓库分别调用" in prompt
    assert "不要猜测或传入 commit SHA" in prompt


def test_build_knowledge_scope_instruction_combines_scope_catalog_and_priority() -> None:
    instruction = build_knowledge_scope_instruction(
        working_directory="coinex",
        explicit_scopes=[KNOWLEDGE_SCOPES["requirements"]],
        inferred_scopes=[],
    )

    assert "本轮显式知识范围与知识库目录" in instruction
    assert instruction.count("coinex/knowledge/requirements") == 1
    assert instruction.count("coinex/knowledge/business-docs") == 1
    assert instruction.count("coinex/knowledge/code") == 1
    assert "本轮优先" in instruction
    assert "显式点亮" not in instruction
    assert "知识库目录约定" not in instruction
