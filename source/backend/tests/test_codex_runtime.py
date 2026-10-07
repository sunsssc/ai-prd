from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from app.integrations.agent_runtime import AssistantRuntimeError
from app.integrations.agent_runtime.codex_runtime import CodexAgentRuntimeClient
from app.integrations.agent_runtime.models import (
    RuntimeDynamicTool,
    RuntimeDynamicToolResult,
    RuntimeSession,
    RuntimeWorkspaceMount,
    RuntimeWorkspacePlan,
)


class FakeAppServerClient:
    def __init__(
        self,
        *,
        responses: dict[str, dict[str, Any]],
        messages: list[dict[str, Any]],
    ) -> None:
        self.responses = responses
        self.stream_messages = messages
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.closed = False
        self.errors: list[tuple[object, str]] = []
        self.results: list[tuple[object, dict[str, Any]]] = []

    async def initialize(self) -> dict[str, Any]:
        self.calls.append(("initialize", {}))
        return {}

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        return self.responses[method]

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        for message in self.stream_messages:
            yield message

    async def respond_error(self, request_id: object, message: str) -> None:
        self.errors.append((request_id, message))

    async def respond_result(self, request_id: object, result: dict[str, Any]) -> None:
        self.results.append((request_id, result))

    async def close(self) -> None:
        self.closed = True


def _runtime_with_fake(fake: FakeAppServerClient, **kwargs: Any) -> CodexAgentRuntimeClient:
    return CodexAgentRuntimeClient(
        cli_path=sys.executable,
        client_factory=lambda _cli, _env: fake,
        **kwargs,
    )


@pytest.mark.anyio
async def test_codex_runtime_session_working_directory_is_workspace(tmp_path: Path) -> None:
    client = CodexAgentRuntimeClient(model="test-model")

    session = await client.create_or_resume_session(
        runtime_session_id=None,
        working_directory=str(tmp_path),
        system_prompt="system",
    )

    assert session.working_directory == str(tmp_path / "workspace")
    assert session.system_prompt == "system"


def test_codex_runtime_builds_read_only_app_server_params() -> None:
    client = CodexAgentRuntimeClient(model="test-model")

    start = client._thread_start_params(
        cwd="/runtime",
        sandbox=client._effective_sandbox([]),
        system_prompt="system",
    )
    resume = client._thread_resume_params(
        session_id="thread-123",
        cwd="/runtime",
        sandbox=client._effective_sandbox([]),
        system_prompt="system",
    )
    turn = client._turn_start_params(
        thread_id="thread-123",
        cwd="/runtime",
        writable_roots=[],
        message="检查项目",
        metadata={"reasoning_effort": "high"},
    )

    assert start == {
        "model": "test-model",
        "cwd": "/runtime",
        "approvalPolicy": {"granular": {
            "mcp_elicitations": True, "sandbox_approval": False, "rules": False,
            "request_permissions": False, "skill_approval": False,
        }},
        "approvalsReviewer": "user",
        "sandbox": "read-only",
        "developerInstructions": client._developer_instructions("system"),
        "serviceName": "ai_prd",
    }
    assert resume["threadId"] == "thread-123"
    assert resume["developerInstructions"] == client._developer_instructions("system")
    assert resume["approvalPolicy"] == start["approvalPolicy"]
    assert turn["approvalPolicy"] == start["approvalPolicy"]
    assert resume["approvalsReviewer"] == turn["approvalsReviewer"] == "user"
    assert resume["excludeTurns"] is True
    assert "先用一两句概览" in start["developerInstructions"]
    assert "每个主要分区用 `##` 且只回答一个问题" in start["developerInstructions"]
    assert "对象、流程、规则和实现分开说明" in start["developerInstructions"]
    assert "先用易懂语言说明含义，再给示例、公式或技术依据" in start["developerInstructions"]
    assert "普通说明和流程不用代码块" in start["developerInstructions"]
    assert "`###` 只作节内细分" in start["developerInstructions"]
    assert "单次命令输出应控制在 120 行以内" in start["developerInstructions"]
    assert "禁止对整个知识库执行无上限的全文输出" in start["developerInstructions"]
    assert turn["input"] == [{"type": "text", "text": "检查项目", "text_elements": []}]
    assert turn["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
    assert turn["effort"] == "high"


def test_codex_runtime_rejects_missing_local_image() -> None:
    client = CodexAgentRuntimeClient(model="test-model")

    with pytest.raises(AssistantRuntimeError, match="图片不存在或不是绝对路径"):
        client._turn_start_params(
            thread_id="thread-123",
            cwd="/runtime",
            writable_roots=[],
            message="检查图片",
            metadata={"local_image_paths": ["/missing/screen.png"]},
        )


@pytest.mark.anyio
async def test_codex_runtime_registers_and_handles_dynamic_tool() -> None:
    async def handler(arguments):
        return RuntimeDynamicToolResult(content=f"mounted {arguments['reference']}")

    tool = RuntimeDynamicTool(
        name="mount_git_ref",
        description="挂载精确 Git reference",
        input_schema={"type": "object"},
        handler=handler,
    )
    client = CodexAgentRuntimeClient(model="test-model")
    params = client._thread_start_params(
        cwd="/runtime",
        sandbox=client._effective_sandbox([]),
        system_prompt="system",
        dynamic_tools=[tool],
    )
    fake = FakeAppServerClient(responses={}, messages=[])

    await client._handle_dynamic_tool_call(
        client=fake,
        request_id=7,
        params={
            "tool": "mount_git_ref",
            "arguments": {"reference": "main"},
        },
        dynamic_tools=[tool],
    )

    assert params["dynamicTools"] == [
        {
            "type": "function",
            "name": "mount_git_ref",
            "description": "挂载精确 Git reference",
            "inputSchema": {"type": "object"},
        }
    ]
    assert fake.results == [
        (
            7,
            {
                "success": True,
                "contentItems": [{"type": "inputText", "text": "mounted main"}],
            },
        )
    ]


def test_codex_runtime_workspace_write_only_exposes_configured_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "workspace" / "me"
    client = CodexAgentRuntimeClient(
        model="test-model",
        sandbox="workspace-write",
        writable_roots=["workspace/me"],
    )
    session = RuntimeSession(provider="codex", session_id=None, working_directory=str(tmp_path))

    cwd, writable_roots = client._resolve_session_write_policy(session)
    turn = client._turn_start_params(
        thread_id="thread-1",
        cwd=cwd,
        writable_roots=writable_roots,
        message="创建文件",
        metadata={},
    )

    assert writable_roots == [str(allowed)]
    assert turn["sandboxPolicy"] == {
        "type": "workspaceWrite",
        "writableRoots": [str(allowed)],
        "networkAccess": False,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    }


def test_codex_runtime_read_only_ignores_workspace_write_mount_symlinks(tmp_path: Path) -> None:
    shadow_root = tmp_path / "runtime" / "root"
    user_root = tmp_path / "workspace" / "users" / "user-1"
    shadow_root.mkdir(parents=True)
    user_root.mkdir(parents=True)
    (shadow_root / "me").symlink_to(user_root, target_is_directory=True)
    workspace_plan = RuntimeWorkspacePlan(
        user_id="user-1",
        session_id="session-1",
        organization_key="coinex",
        sandbox_cwd="/",
        host_shadow_root=str(shadow_root),
        mounts=[
            RuntimeWorkspaceMount(
                workspace_id="user-workspace",
                workspace_key="user:user-1",
                host_path=str(user_root),
                sandbox_path="/me",
                permission="write",
            )
        ],
    )
    client = CodexAgentRuntimeClient(model="test-model", sandbox="read-only")
    session = RuntimeSession(
        provider="codex",
        session_id=None,
        working_directory=str(shadow_root),
        workspace_plan=workspace_plan,
    )

    cwd, writable_roots = client._resolve_session_write_policy(session)

    assert cwd == str(shadow_root)
    assert writable_roots == []


def test_codex_runtime_workspace_write_uses_workspace_mount_source_path(tmp_path: Path) -> None:
    shadow_root = tmp_path / "runtime" / "root"
    user_root = tmp_path / "workspace" / "users" / "user-1"
    shadow_root.mkdir(parents=True)
    user_root.mkdir(parents=True)
    (user_root / ".agents").mkdir()
    (user_root / ".codex").mkdir()
    (shadow_root / "me").symlink_to(user_root, target_is_directory=True)
    workspace_plan = RuntimeWorkspacePlan(
        user_id="user-1",
        session_id="session-1",
        organization_key="coinex",
        sandbox_cwd="/",
        host_shadow_root=str(shadow_root),
        mounts=[
            RuntimeWorkspaceMount(
                workspace_id="user-workspace",
                workspace_key="user:user-1",
                host_path=str(user_root),
                sandbox_path="/me",
                permission="write",
            )
        ],
    )
    client = CodexAgentRuntimeClient(model="test-model", sandbox="workspace-write")
    session = RuntimeSession(
        provider="codex",
        session_id=None,
        working_directory=str(shadow_root),
        workspace_plan=workspace_plan,
    )

    cwd, writable_roots = client._resolve_session_write_policy(session)

    assert cwd == str(shadow_root)
    assert writable_roots == [str(user_root)]


def test_codex_runtime_auto_uses_only_personal_and_tmp_write_mounts(tmp_path: Path) -> None:
    shadow_root = tmp_path / "runtime" / "root"
    shadow_root.mkdir(parents=True)
    for name in ("me", "tmp", "repos"):
        (shadow_root / name).mkdir()
    workspace_plan = RuntimeWorkspacePlan(
        user_id="user-1",
        session_id="session-1",
        organization_key="coinex",
        sandbox_cwd="/",
        host_shadow_root=str(shadow_root),
        mounts=[
            RuntimeWorkspaceMount(
                workspace_id=f"workspace-{name}",
                workspace_key=f"workspace:{name}",
                host_path=str(shadow_root / name),
                sandbox_path=f"/{name}",
                permission="write",
            )
            for name in ("me", "tmp", "repos")
        ],
    )
    client = CodexAgentRuntimeClient(model="test-model")
    session = RuntimeSession(
        provider="codex",
        session_id=None,
        working_directory=str(shadow_root),
        workspace_plan=workspace_plan,
    )

    cwd, writable_roots = client._resolve_session_write_policy(session)

    assert cwd == str(shadow_root)
    assert writable_roots == [str(shadow_root / "me"), str(shadow_root / "tmp")]
    assert client._effective_sandbox(writable_roots) == "workspace-write"
    assert client._sandbox_policy(writable_roots)["type"] == "workspaceWrite"


def test_codex_runtime_auto_keeps_session_without_write_mounts_read_only(tmp_path: Path) -> None:
    workspace_plan = RuntimeWorkspacePlan(
        user_id="system",
        session_id="background-task",
        organization_key="coinex",
        sandbox_cwd="/",
        host_shadow_root=str(tmp_path),
        mounts=[],
    )
    client = CodexAgentRuntimeClient(model="test-model")
    session = RuntimeSession(
        provider="codex",
        session_id=None,
        working_directory=str(tmp_path),
        workspace_plan=workspace_plan,
    )

    _cwd, writable_roots = client._resolve_session_write_policy(session)

    assert writable_roots == []
    assert client._effective_sandbox(writable_roots) == "read-only"
    assert client._sandbox_policy(writable_roots) == {"type": "readOnly", "networkAccess": False}


def test_codex_runtime_auto_keeps_legacy_background_session_read_only(tmp_path: Path) -> None:
    client = CodexAgentRuntimeClient(
        model="test-model",
        writable_roots=["workspace/me"],
    )
    session = RuntimeSession(provider="codex", session_id=None, working_directory=str(tmp_path))

    cwd, writable_roots = client._resolve_session_write_policy(session)

    assert cwd == str(tmp_path)
    assert writable_roots == []
    assert client._effective_sandbox(writable_roots) == "read-only"


def test_codex_runtime_rejects_writable_root_outside_project(tmp_path: Path) -> None:
    client = CodexAgentRuntimeClient(
        model="test-model",
        sandbox="workspace-write",
        writable_roots=["/tmp/outside"],
    )
    session = RuntimeSession(provider="codex", session_id=None, working_directory=str(tmp_path))

    with pytest.raises(AssistantRuntimeError, match="可写目录必须位于项目根目录内"):
        client._resolve_session_write_policy(session)


def test_codex_runtime_forwards_structured_output_schema() -> None:
    client = CodexAgentRuntimeClient(model="test-model")
    output_schema = {
        "type": "object",
        "properties": {"result": {"type": "string"}},
        "required": ["result"],
    }

    params = client._turn_start_params(
        thread_id="thread-1",
        cwd="/runtime",
        writable_roots=[],
        message="返回 JSON",
        metadata={"output_schema": output_schema},
    )

    assert params["outputSchema"] == output_schema


@pytest.mark.anyio
async def test_codex_runtime_starts_thread_and_streams_app_server_events(tmp_path: Path) -> None:
    image_path = tmp_path / "screen.png"
    image_path.write_bytes(b"image")
    fake = FakeAppServerClient(
        responses={
            "thread/start": {
                "thread": {
                    "id": "thread-new",
                    "cwd": str(tmp_path / "workspace"),
                    "name": "初始标题",
                }
            },
            "turn/start": {"turn": {"id": "turn-1"}},
            "thread/read": {
                "thread": {
                    "id": "thread-new",
                    "cwd": str(tmp_path / "workspace"),
                    "name": "Runtime 归纳标题",
                }
            },
        },
        messages=[
            {"method": "thread/started", "params": {"thread": {"id": "thread-new"}}},
            {"method": "turn/started", "params": {"turn": {"id": "turn-1"}}},
            {
                "method": "item/agentMessage/delta",
                "params": {"itemId": "message-1", "delta": "完成"},
            },
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "message-1",
                        "type": "agentMessage",
                        "text": "完成",
                    }
                },
            },
            {
                "method": "thread/name/updated",
                "params": {"threadId": "thread-new", "threadName": "Runtime 归纳标题"},
            },
            {
                "method": "turn/completed",
                "params": {
                    "turn": {"id": "turn-1", "status": "completed"},
                    "usage": {"inputTokens": 10, "outputTokens": 2},
                },
            },
        ],
    )
    client = _runtime_with_fake(fake, model="test-model")
    session = RuntimeSession(
        provider="codex",
        session_id=None,
        working_directory=str(tmp_path),
        system_prompt="system from session",
    )

    events = [
        event
        async for event in client.send_message_stream(
            session=session,
            message="后端接口是哪个？",
            metadata={
                "reasoning_effort": "medium",
                "local_image_paths": [str(image_path)],
            },
        )
    ]

    assert [call[0] for call in fake.calls] == [
        "initialize",
        "thread/start",
        "turn/start",
        "thread/read",
    ]
    assert fake.calls[1][1]["developerInstructions"] == client._developer_instructions("system from session")
    assert fake.calls[1][1]["sandbox"] == "read-only"
    assert fake.calls[2][0] == "turn/start"
    assert fake.calls[2][1]["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}
    assert fake.calls[2][1]["input"] == [
        {"type": "text", "text": "后端接口是哪个？", "text_elements": []},
        {"type": "localImage", "path": str(image_path), "detail": "high"},
    ]
    assert events[0].type == "session"
    assert events[0].data["session_id"] == "thread-new"
    assert any(
        event.type == "session" and event.data.get("title") == "Runtime 归纳标题"
        for event in events
    )
    assert [event.data["text"] for event in events if event.type == "delta"] == ["完成"]
    assert events[-2].type == "message"
    assert events[-2].data["content"] == "完成"
    assert events[-1].type == "complete"
    assert events[-1].data["result"] == "完成"
    assert fake.closed is True


@pytest.mark.anyio
async def test_codex_runtime_sends_workspace_plan_write_policy_to_app_server(tmp_path: Path) -> None:
    shadow_root = tmp_path / "runtime" / "root"
    (shadow_root / "me").mkdir(parents=True)
    (shadow_root / "tmp").mkdir()
    workspace_plan = RuntimeWorkspacePlan(
        user_id="user-1",
        session_id="session-1",
        organization_key="coinex",
        sandbox_cwd="/",
        host_shadow_root=str(shadow_root),
        mounts=[
            RuntimeWorkspaceMount(
                workspace_id=f"workspace-{name}",
                workspace_key=f"workspace:{name}",
                host_path=str(shadow_root / name),
                sandbox_path=f"/{name}",
                permission="write",
            )
            for name in ("me", "tmp")
        ],
    )
    fake = FakeAppServerClient(
        responses={
            "thread/start": {"thread": {"id": "thread-write", "cwd": str(shadow_root)}},
            "turn/start": {"turn": {"id": "turn-write"}},
            "thread/read": {"thread": {"id": "thread-write", "cwd": str(shadow_root)}},
        },
        messages=[
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-write", "status": "completed"}},
            }
        ],
    )
    client = _runtime_with_fake(fake, model="test-model")
    session = RuntimeSession(
        provider="codex",
        session_id=None,
        working_directory=str(shadow_root),
        workspace_plan=workspace_plan,
    )

    _events = [
        event
        async for event in client.send_message_stream(
            session=session,
            message="写入个人工作区",
            metadata={},
        )
    ]

    assert fake.calls[1][1]["sandbox"] == "workspace-write"
    assert fake.calls[2][1]["sandboxPolicy"] == {
        "type": "workspaceWrite",
        "writableRoots": [str(shadow_root / "me"), str(shadow_root / "tmp")],
        "networkAccess": False,
        "excludeTmpdirEnvVar": False,
        "excludeSlashTmp": False,
    }


@pytest.mark.anyio
async def test_codex_runtime_keeps_only_final_answer_agent_messages(tmp_path: Path) -> None:
    fake = FakeAppServerClient(
        responses={
            "thread/start": {"thread": {"id": "thread-phases", "cwd": str(tmp_path)}},
            "turn/start": {"turn": {"id": "turn-phases"}},
            "thread/read": {"thread": {"id": "thread-phases", "cwd": str(tmp_path)}},
        },
        messages=[
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "id": "commentary-1",
                        "type": "agentMessage",
                        "phase": None,
                        "text": "",
                    }
                },
            },
            {
                "method": "item/agentMessage/delta",
                "params": {"itemId": "commentary-1", "delta": "我会先检索资料。"},
            },
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "commentary-1",
                        "type": "agentMessage",
                        "phase": "commentary",
                        "text": "我会先检索资料。",
                    }
                },
            },
            {
                "method": "item/started",
                "params": {
                    "item": {
                        "id": "answer-1",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "",
                    }
                },
            },
            {
                "method": "item/agentMessage/delta",
                "params": {"itemId": "answer-1", "delta": "这是最终回答。"},
            },
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "answer-1",
                        "type": "agentMessage",
                        "phase": "final_answer",
                        "text": "这是最终回答。",
                    }
                },
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-phases", "status": "completed"}},
            },
        ],
    )
    client = _runtime_with_fake(fake, model="test-model")
    session = RuntimeSession(provider="codex", session_id=None, working_directory=str(tmp_path))

    events = [
        event
        async for event in client.send_message_stream(
            session=session,
            message="解释功能",
            metadata={},
        )
    ]

    assert [event.data["text"] for event in events if event.type == "delta"] == ["这是最终回答。"]
    assert all("我会先检索资料" not in str(event.data) for event in events)
    assert events[-2].data["content"] == "这是最终回答。"
    assert events[-1].data["result"] == "这是最终回答。"


@pytest.mark.anyio
async def test_codex_runtime_resumes_existing_thread(tmp_path: Path) -> None:
    fake = FakeAppServerClient(
        responses={
            "thread/resume": {"thread": {"id": "thread-old", "cwd": str(tmp_path)}},
            "turn/start": {"turn": {"id": "turn-2"}},
            "thread/read": {
                "thread": {
                    "id": "thread-old",
                    "cwd": str(tmp_path),
                    "name": "恢复后的标题",
                }
            },
        },
        messages=[
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "message-2",
                        "type": "agentMessage",
                        "text": "继续完成",
                    }
                },
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-2", "status": "completed"}},
            },
        ],
    )
    client = _runtime_with_fake(fake, model="test-model")
    session = RuntimeSession(
        provider="codex",
        session_id="thread-old",
        working_directory=str(tmp_path),
        system_prompt="system",
    )

    events = [
        event
        async for event in client.send_message_stream(
            session=session,
            message="继续",
            metadata={},
        )
    ]

    assert [call[0] for call in fake.calls] == [
        "initialize",
        "thread/resume",
        "turn/start",
        "thread/read",
    ]
    assert fake.calls[1][1]["threadId"] == "thread-old"
    assert fake.calls[1][1]["excludeTurns"] is True
    assert any(
        event.type == "session" and event.data.get("title") == "恢复后的标题"
        for event in events
    )
    assert events[-1].data["result"] == "继续完成"


@pytest.mark.anyio
async def test_codex_runtime_does_not_expose_command_output_deltas(tmp_path: Path) -> None:
    fake = FakeAppServerClient(
        responses={
            "thread/start": {"thread": {"id": "thread-output", "cwd": str(tmp_path)}},
            "turn/start": {"turn": {"id": "turn-output"}},
            "thread/read": {"thread": {"id": "thread-output", "cwd": str(tmp_path)}},
        },
        messages=[
            {
                "method": "item/commandExecution/outputDelta",
                "params": {
                    "itemId": "command-1",
                    "delta": "不应进入前端或持久化的超长检索正文",
                },
            },
            {
                "method": "item/agentMessage/delta",
                "params": {"itemId": "message-1", "delta": "最终回答"},
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-output", "status": "completed"}},
            },
        ],
    )
    client = _runtime_with_fake(fake, model="test-model")
    session = RuntimeSession(provider="codex", session_id=None, working_directory=str(tmp_path))

    events = [
        event
        async for event in client.send_message_stream(
            session=session,
            message="检查项目",
            metadata={},
        )
    ]

    assert all("超长检索正文" not in str(event.data) for event in events)
    assert events[-1].data["result"] == "最终回答"


def test_codex_runtime_converts_command_tool_events() -> None:
    client = CodexAgentRuntimeClient(model="test-model")

    tool_use_events = client._convert_item(
        {
            "id": "call-1",
            "type": "commandExecution",
            "command": "rg 链上交易 coinex/knowledge",
            "cwd": "/workspace",
        },
        "started",
        "thread-1",
    )
    tool_result_events = client._convert_item(
        {
            "id": "call-1",
            "type": "commandExecution",
            "aggregatedOutput": "done",
            "exitCode": 0,
            "status": "completed",
        },
        "completed",
        "thread-1",
    )

    assert tool_use_events[0].type == "tool_use"
    assert tool_use_events[1].type == "search"
    assert tool_result_events[0].type == "tool_result"
    assert tool_result_events[0].data["tool_name"] == "exec_command"
    assert tool_result_events[0].data["is_error"] is False


def test_codex_runtime_converts_dynamic_tool_content_items() -> None:
    client = CodexAgentRuntimeClient(model="test-model")

    success = client._convert_item(
        {
            "id": "dynamic-1",
            "type": "dynamicToolCall",
            "tool": "list_test_data_capabilities",
            "status": "completed",
            "success": True,
            "contentItems": [
                {"type": "inputText", "text": '{"tasks":["update_kyc_status"]}'},
            ],
        },
        "completed",
        "thread-1",
    )[0]
    failure = client._convert_item(
        {
            "id": "dynamic-2",
            "type": "dynamicToolCall",
            "tool": "prepare_test_data",
            "status": "failed",
            "success": False,
            "contentItems": [
                {"type": "inputText", "text": '{"error":"测试造数平台连接失败：ReadTimeout"}'},
            ],
        },
        "completed",
        "thread-1",
    )[0]

    assert success.type == "tool_result"
    assert success.data["content"] == '{"tasks":["update_kyc_status"]}'
    assert success.data["is_error"] is False
    assert failure.data["content"] == '{"error":"测试造数平台连接失败：ReadTimeout"}'
    assert failure.data["is_error"] is True


def test_codex_runtime_keeps_legacy_dynamic_tool_result_fallback() -> None:
    client = CodexAgentRuntimeClient(model="test-model")

    event = client._convert_item(
        {
            "id": "dynamic-legacy",
            "type": "dynamicToolCall",
            "tool": "legacy_tool",
            "status": "completed",
            "result": {"text": "legacy result"},
        },
        "completed",
        "thread-1",
    )[0]

    assert event.data["content"] == "legacy result"
    assert event.data["is_error"] is False


def test_codex_runtime_rejects_unknown_sandbox() -> None:
    with pytest.raises(AssistantRuntimeError, match="不支持的 Codex sandbox"):
        CodexAgentRuntimeClient(sandbox="unknown")


@pytest.mark.anyio
async def test_codex_runtime_surfaces_turn_failure(tmp_path: Path) -> None:
    fake = FakeAppServerClient(
        responses={
            "thread/start": {"thread": {"id": "thread-fail", "cwd": str(tmp_path)}},
            "turn/start": {"turn": {"id": "turn-fail"}},
        },
        messages=[
            {
                "method": "turn/completed",
                "params": {
                    "turn": {
                        "id": "turn-fail",
                        "status": "failed",
                        "error": {"message": "模型调用失败"},
                    }
                },
            }
        ],
    )
    client = _runtime_with_fake(fake, model="test-model")
    session = RuntimeSession(provider="codex", session_id=None, working_directory=str(tmp_path))

    with pytest.raises(AssistantRuntimeError, match="模型调用失败"):
        async for _event in client.send_message_stream(
            session=session,
            message="执行",
            metadata={},
        ):
            pass

    assert fake.closed is True


@pytest.mark.anyio
async def test_codex_runtime_rejects_unsupported_server_request(tmp_path: Path) -> None:
    fake = FakeAppServerClient(
        responses={
            "thread/start": {"thread": {"id": "thread-request", "cwd": str(tmp_path)}},
            "turn/start": {"turn": {"id": "turn-request"}},
        },
        messages=[
            {
                "id": 99,
                "method": "item/tool/requestUserInput",
                "params": {"threadId": "thread-request", "turnId": "turn-request"},
            }
        ],
    )
    client = _runtime_with_fake(fake, model="test-model")
    session = RuntimeSession(provider="codex", session_id=None, working_directory=str(tmp_path))

    with pytest.raises(AssistantRuntimeError, match="不支持的交互"):
        async for _event in client.send_message_stream(
            session=session,
            message="执行",
            metadata={},
        ):
            pass

    assert fake.errors == [
        (99, "ai-prd 当前不支持 app-server 交互请求：item/tool/requestUserInput")
    ]
    assert fake.closed is True


@pytest.mark.anyio
async def test_codex_runtime_emits_image_generation_events(tmp_path: Path) -> None:
    fake = FakeAppServerClient(
        responses={
            "thread/start": {"thread": {"id": "thread-image", "cwd": str(tmp_path)}},
            "turn/start": {"turn": {"id": "turn-image"}},
            "thread/read": {"thread": {"id": "thread-image", "cwd": str(tmp_path)}},
        },
        messages=[
            {"method": "turn/started", "params": {"turn": {"id": "turn-image"}}},
            {
                "method": "item/started",
                "params": {
                    "item": {"id": "img-1", "type": "imageGeneration", "status": "inProgress"}
                },
            },
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "img-1",
                        "type": "imageGeneration",
                        "status": "completed",
                        "revisedPrompt": "白底蓝色猫头 Logo",
                        "result": "cG5n",
                        "savedPath": str(tmp_path / "generated_images" / "img-1.png"),
                    }
                },
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-image", "status": "completed"}},
            },
        ],
    )
    client = _runtime_with_fake(fake, model="test-model")
    session = RuntimeSession(
        provider="codex",
        session_id=None,
        working_directory=str(tmp_path),
        system_prompt="system",
    )

    events = [
        event
        async for event in client.send_message_stream(
            session=session,
            message="生成一张蓝色猫头 Logo",
            metadata={},
        )
    ]

    activities = [event for event in events if event.type == "activity"]
    assert any("正在生成图像" in str(event.data.get("message") or "") for event in activities)
    image_events = [event for event in events if event.type == "image"]
    assert len(image_events) == 1
    assert image_events[0].data["data"] == "cG5n"
    assert image_events[0].data["source_path"] == str(tmp_path / "generated_images" / "img-1.png")
    assert image_events[0].data["revised_prompt"] == "白底蓝色猫头 Logo"
    assert [event for event in events if event.type == "delta"] == []
    assert events[-1].type == "complete"
    assert events[-1].data["result"] == ""


@pytest.mark.anyio
async def test_codex_runtime_reports_image_generation_without_payload(tmp_path: Path) -> None:
    fake = FakeAppServerClient(
        responses={
            "thread/start": {"thread": {"id": "thread-image", "cwd": str(tmp_path)}},
            "turn/start": {"turn": {"id": "turn-image"}},
            "thread/read": {"thread": {"id": "thread-image", "cwd": str(tmp_path)}},
        },
        messages=[
            {
                "method": "item/completed",
                "params": {
                    "item": {
                        "id": "img-2",
                        "type": "imageGeneration",
                        "status": "failed",
                        "revisedPrompt": None,
                        "result": "",
                    }
                },
            },
            {
                "method": "turn/completed",
                "params": {"turn": {"id": "turn-image", "status": "completed"}},
            },
        ],
    )
    client = _runtime_with_fake(fake, model="test-model")
    session = RuntimeSession(
        provider="codex",
        session_id=None,
        working_directory=str(tmp_path),
        system_prompt="system",
    )

    events = [
        event
        async for event in client.send_message_stream(
            session=session,
            message="生成一张图",
            metadata={},
        )
    ]

    assert [event for event in events if event.type == "image"] == []
    activities = [str(event.data.get("message") or "") for event in events if event.type == "activity"]
    assert any("图像生成未返回可用结果" in message for message in activities)
