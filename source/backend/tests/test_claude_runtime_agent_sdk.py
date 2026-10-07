from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.integrations.agent_runtime.claude_runtime import ClaudeCodeAgentRuntimeClient
from app.integrations.agent_runtime.models import (
    AssistantRuntimeError,
    RuntimeDynamicTool,
    RuntimeDynamicToolResult,
    RuntimeSession,
    RuntimeWorkspaceMount,
    RuntimeWorkspacePlan,
)


def test_convert_rate_limit_event_into_usage_and_activity() -> None:
    client = ClaudeCodeAgentRuntimeClient(
        model="claude-sonnet-4-20250514",
        max_turns=3,
        permission_mode="default",
        allowed_tools=["Read"],
        cli_path=None,
    )

    class RateLimitInfo:
        def __init__(self) -> None:
            self.status = "ok"
            self.resets_at = None
            self.rate_limit_type = None
            self.utilization = 0.12
            self.overage_status = None
            self.overage_resets_at = None
            self.overage_disabled_reason = None
            self.raw = {"status": "ok", "utilization": 0.12}

    class RateLimitEvent:
        def __init__(self) -> None:
            self.rate_limit_info = RateLimitInfo()
            self.uuid = "u1"
            self.session_id = "s1"

    events = client._convert_raw_message(RateLimitEvent())

    assert events[0].type == "usage"
    assert events[0].data["rate_limit"]["status"] == "ok"
    assert events[1].type == "activity"
    assert "速率状态" in events[1].data["message"]


def _make_client() -> ClaudeCodeAgentRuntimeClient:
    return ClaudeCodeAgentRuntimeClient(
        model="claude-sonnet-4-20250514",
        max_turns=3,
        permission_mode="default",
        allowed_tools=["Read"],
        cli_path=None,
    )


@pytest.mark.anyio
async def test_claude_runtime_session_working_directory_is_workspace(tmp_path) -> None:
    session = await _make_client().create_or_resume_session(
        runtime_session_id=None,
        working_directory=str(tmp_path),
        system_prompt="system",
    )

    assert session.working_directory == str(tmp_path / "workspace")
    assert session.system_prompt == "system"


@pytest.mark.anyio
async def test_claude_runtime_exposes_dynamic_tool_through_in_process_mcp(tmp_path) -> None:
    captured = {}

    def fake_tool(name, description, input_schema):
        captured["definition"] = (name, description, input_schema)

        def decorate(handler):
            captured["handler"] = handler
            return handler

        return decorate

    def fake_server(**kwargs):
        captured["server"] = kwargs
        return kwargs

    async def handler(arguments):
        return RuntimeDynamicToolResult(content=f"mounted {arguments['reference']}")

    dynamic_tool = RuntimeDynamicTool(
        name="mount_git_ref",
        description="挂载精确 Git reference",
        input_schema={"type": "object"},
        handler=handler,
    )
    session = RuntimeSession(
        provider="claude_code",
        session_id=None,
        working_directory=str(tmp_path / "workspace"),
        system_prompt="system",
        dynamic_tools=[dynamic_tool],
        dynamic_read_roots=[str(tmp_path / "revisions")],
    )

    servers = _make_client()._mcp_servers(
        {"tool": fake_tool, "create_sdk_mcp_server": fake_server},
        session,
    )
    result = await captured["handler"]({"reference": "main"})

    assert servers["ai_prd"]["name"] == "ai_prd"
    assert captured["definition"][0] == "mount_git_ref"
    assert result == {
        "content": [{"type": "text", "text": "mounted main"}],
        "isError": False,
    }
    assert "mcp__ai_prd__mount_git_ref" in _make_client()._allowed_tools(session)


@pytest.mark.anyio
async def test_claude_runtime_options_are_read_only_and_workspace_scoped(tmp_path, monkeypatch) -> None:
    captured = {}

    class FakeOptions:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    async def fake_query(*, prompt, options):
        del prompt, options
        if False:
            yield None

    client = ClaudeCodeAgentRuntimeClient(
        model="claude-sonnet-4-20250514",
        max_turns=3,
        effort="medium",
        permission_mode="default",
        allowed_tools=["Read", "Grep", "Glob", "LS", "Task", "Bash(rg *)"],
        cli_path=None,
        max_output_tokens=64000,
    )
    monkeypatch.setattr(client, "_load_sdk", lambda: {"query": fake_query, "ClaudeAgentOptions": FakeOptions})
    runtime_session = RuntimeSession(
        provider="claude_code",
        session_id=None,
        working_directory=str(tmp_path / "workspace"),
        system_prompt="system from session",
    )

    events = [
        event
        async for event in client.send_message_stream(
            session=runtime_session,
            message="分析需求",
            metadata={},
        )
    ]

    assert events[0].type == "session"
    assert captured["system_prompt"] == "system from session"
    assert str(captured["cwd"]) == str(tmp_path / "workspace")
    assert captured["permission_mode"] == "default"
    assert captured["effort"] == "medium"
    assert captured["setting_sources"] == ["project"]
    assert captured["disallowed_tools"] == ["Edit", "Write"]
    assert captured["sandbox"]["enabled"] is True
    assert captured["sandbox"]["failIfUnavailable"] is True
    assert captured["sandbox"]["autoAllowBashIfSandboxed"] is False
    assert captured["sandbox"]["allowUnsandboxedCommands"] is False
    assert captured["sandbox"]["filesystem"] == {
        "denyWrite": ["."],
        "denyRead": ["~/"],
        "allowRead": ["."],
    }
    assert captured["sandbox"]["network"] == {
        "allowAllUnixSockets": False,
        "allowLocalBinding": False,
    }
    assert "Edit" not in captured["allowed_tools"]
    assert "Write" not in captured["allowed_tools"]
    assert "Bash" not in captured["allowed_tools"]
    assert captured["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "64000"


@pytest.mark.anyio
async def test_claude_runtime_options_allow_workspace_plan_write_mounts(tmp_path, monkeypatch) -> None:
    captured = {}

    class FakeOptions:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    async def fake_query(*, prompt, options):
        del prompt, options
        if False:
            yield None

    runtime_venv = tmp_path / "workspace/runtime/python/.venv"
    runtime_venv_bin = runtime_venv / "bin"
    runtime_venv_bin.mkdir(parents=True)
    (runtime_venv_bin / "python").write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    client = ClaudeCodeAgentRuntimeClient(
        model="claude-sonnet-4-20250514",
        max_turns=3,
        permission_mode="default",
        allowed_tools=["Read"],
        cli_path=None,
        runtime_python_venv_path=str(runtime_venv),
    )
    monkeypatch.setattr(client, "_load_sdk", lambda: {"query": fake_query, "ClaudeAgentOptions": FakeOptions})

    shadow_root = tmp_path / "workspace/runtime/sessions/session_1/root"
    user_root = tmp_path / "workspace/users/user_1"
    tmp_root = tmp_path / "workspace/runtime/containers/session_1/tmp"
    knowledge_root = tmp_path / "workspace/coinex/knowledge"
    workspace_plan = RuntimeWorkspacePlan(
        user_id="user_1",
        session_id="session_1",
        organization_key="coinex",
        sandbox_cwd="/",
        host_shadow_root=str(shadow_root),
        mounts=[
            RuntimeWorkspaceMount(
                workspace_id="ws_knowledge",
                workspace_key="org:coinex:knowledge",
                host_path=str(knowledge_root),
                sandbox_path="/coinex/knowledge",
                permission="read",
            ),
            RuntimeWorkspaceMount(
                workspace_id="ws_user",
                workspace_key="user:user_1",
                host_path=str(user_root),
                sandbox_path="/me",
                permission="write",
            ),
            RuntimeWorkspaceMount(
                workspace_id="runtime_tmp_session_1",
                workspace_key="runtime:tmp:session_1",
                host_path=str(tmp_root),
                sandbox_path="/tmp",
                permission="write",
            ),
        ],
    )
    runtime_session = RuntimeSession(
        provider="claude_code",
        session_id=None,
        working_directory=str(shadow_root),
        system_prompt="system from session",
        workspace_plan=workspace_plan,
    )

    events = [
        event
        async for event in client.send_message_stream(
            session=runtime_session,
            message="生成 PDF",
            metadata={},
        )
    ]

    assert events[0].type == "session"
    assert str(captured["cwd"]) == str(shadow_root)
    assert captured["disallowed_tools"] == []
    assert "Bash" in captured["allowed_tools"]
    assert "Edit" in captured["allowed_tools"]
    assert "Write" in captured["allowed_tools"]
    assert captured["sandbox"]["autoAllowBashIfSandboxed"] is True
    assert "CLAUDE_CODE_TMPDIR" not in captured["env"]
    assert "TMPDIR" not in captured["env"]
    assert "TEMP" not in captured["env"]
    assert "TMP" not in captured["env"]
    assert captured["env"]["XDG_CACHE_HOME"] == str(tmp_root.resolve() / ".cache")
    assert captured["env"]["TEXMFVAR"] == str(tmp_root.resolve() / ".cache/texmf-var")
    assert captured["env"]["TEXMFCONFIG"] == str(tmp_root.resolve() / ".cache/texmf-config")
    assert captured["env"]["PATH"] == f"{runtime_venv_bin.resolve()}:/usr/bin:/bin"
    assert captured["env"]["VIRTUAL_ENV"] == str(runtime_venv.resolve())
    assert captured["env"]["PYTHONNOUSERSITE"] == "1"
    assert (tmp_root / ".cache/texmf-var").is_dir()
    assert (tmp_root / ".cache/texmf-config").is_dir()
    assert captured["sandbox"]["filesystem"]["denyWrite"] == ["."]
    assert captured["sandbox"]["filesystem"]["denyRead"] == ["~/"]

    allow_read = set(captured["sandbox"]["filesystem"]["allowRead"])
    assert str(shadow_root) in allow_read
    assert str(user_root.resolve()) in allow_read
    assert str(shadow_root / "me") not in allow_read
    assert str(shadow_root / "tmp") not in allow_read
    assert str(shadow_root / "coinex/knowledge") not in allow_read
    assert str(knowledge_root.resolve()) in allow_read
    assert str(runtime_venv.resolve()) in allow_read

    allow_write = set(captured["sandbox"]["filesystem"]["allowWrite"])
    assert str(user_root.resolve()) in allow_write
    assert str(shadow_root / "me") not in allow_write
    assert str(tmp_root.resolve()) in allow_write
    assert str(shadow_root / "tmp") not in allow_write
    assert str(knowledge_root.resolve()) not in allow_write

    permission_allow = set(json.loads(captured["settings"])["permissions"]["allow"])
    assert f"Read({Path(shadow_root).resolve().as_posix()}/**)" in permission_allow
    assert f"Write({Path(user_root).resolve().as_posix()}/**)" in permission_allow
    assert f"Edit({Path(tmp_root).resolve().as_posix()}/**)" in permission_allow
    assert "Write(/me/**)" in permission_allow
    assert "Write(me/**)" in permission_allow
    assert "Edit(/tmp/**)" in permission_allow
    assert "Edit(tmp/**)" in permission_allow
    assert "Read(/coinex/knowledge/**)" in permission_allow
    assert "Read(coinex/knowledge/**)" in permission_allow
    assert f"Read({runtime_venv.resolve().as_posix()}/**)" in permission_allow
    assert "Write(/coinex/knowledge/**)" not in permission_allow
    assert f"Write({Path(knowledge_root).resolve().as_posix()}/**)" not in permission_allow
    assert "Write(//me/**)" not in permission_allow
    assert "Edit(//tmp/**)" not in permission_allow
    assert "Read(//coinex/knowledge/**)" not in permission_allow
    assert "Write(//coinex/knowledge/**)" not in permission_allow
    assert f"Read({Path(shadow_root).resolve().as_posix().replace('/', '//', 1)}/**)" not in permission_allow
    assert f"Write({Path(user_root).resolve().as_posix().replace('/', '//', 1)}/**)" not in permission_allow
    assert f"Edit({Path(tmp_root).resolve().as_posix().replace('/', '//', 1)}/**)" not in permission_allow
    assert f"Read({runtime_venv.resolve().as_posix().replace('/', '//', 1)}/**)" not in permission_allow
    assert f"Write({Path(knowledge_root).resolve().as_posix().replace('/', '//', 1)}/**)" not in permission_allow


@pytest.mark.anyio
async def test_claude_runtime_disables_bash_for_file_read_only_workspace_plan(tmp_path, monkeypatch) -> None:
    captured = {}

    class FakeOptions:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

    async def fake_query(*, prompt, options):
        del prompt, options
        if False:
            yield None

    client = ClaudeCodeAgentRuntimeClient(
        model="claude-sonnet-4-20250514",
        max_turns=3,
        permission_mode="default",
        allowed_tools=["Read", "Glob", "Grep", "Bash(find *)", "Bash(rg *)"],
        cli_path=None,
    )
    monkeypatch.setattr(client, "_load_sdk", lambda: {"query": fake_query, "ClaudeAgentOptions": FakeOptions})
    package_root = tmp_path / "skill-import" / "workspace"
    workspace_plan = RuntimeWorkspacePlan(
        user_id="user_1",
        session_id="skill-import-token",
        organization_key="skill-import",
        sandbox_cwd="/",
        host_shadow_root=str(package_root),
        mounts=[],
    )
    runtime_session = await client.create_or_resume_session(
        runtime_session_id=None,
        workspace_plan=workspace_plan,
        tool_allowlist=("Read", "Glob", "Grep"),
        system_prompt="system",
    )

    events = [
        event
        async for event in client.send_message_stream(
            session=runtime_session,
            message="递归读取导入包",
            metadata={},
        )
    ]

    assert events[0].type == "session"
    assert str(captured["cwd"]) == str(package_root)
    assert captured["sandbox"]["autoAllowBashIfSandboxed"] is False
    assert captured["sandbox"]["filesystem"] == {
        "denyWrite": ["."],
        "denyRead": ["~/"],
        "allowRead": [str(package_root.resolve())],
    }
    assert "Bash" not in captured["allowed_tools"]
    assert not any(tool.startswith("Bash(") for tool in captured["allowed_tools"])
    assert captured["disallowed_tools"] == ["Bash", "Edit", "Write"]


@pytest.mark.anyio
async def test_claude_runtime_fails_when_auto_approval_is_required(tmp_path, monkeypatch) -> None:
    class FakeOptions:
        def __init__(self, **kwargs) -> None:
            del kwargs

    class ToolUseBlock:
        id = "tool-1"
        name = "Bash"
        input = {"command": "pandoc me/doc.md -o me/doc.pdf"}

    class AssistantMessage:
        session_id = "s1"
        content = [ToolUseBlock()]

    class ToolResultBlock:
        tool_use_id = "tool-1"
        content = "This command requires approval"
        is_error = True

    class UserMessage:
        session_id = "s1"
        content = [ToolResultBlock()]

    async def fake_query(*, prompt, options):
        del prompt, options
        yield AssistantMessage()
        yield UserMessage()

    client = _make_client()
    monkeypatch.setattr(client, "_load_sdk", lambda: {"query": fake_query, "ClaudeAgentOptions": FakeOptions})
    runtime_session = RuntimeSession(
        provider="claude_code",
        session_id=None,
        working_directory=str(tmp_path / "workspace"),
        system_prompt="system",
    )

    stream = client.send_message_stream(session=runtime_session, message="生成 PDF", metadata={}).__aiter__()

    session_event = await stream.__anext__()
    assert session_event.type == "session"
    tool_use_event = await stream.__anext__()
    assert tool_use_event.type == "tool_use"
    with pytest.raises(AssistantRuntimeError, match="Bash 自动审批未通过"):
        await stream.__anext__()


def test_stream_event_text_delta_emits_delta_event() -> None:
    client = _make_client()

    class StreamEvent:
        def __init__(self) -> None:
            self.session_id = "s1"
            self.event = {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}}

    events = client._convert_raw_message(StreamEvent())

    assert len(events) == 1
    assert events[0].type == "delta"
    assert events[0].data["text"] == "Hello"


def test_stream_event_non_text_delta_emits_nothing() -> None:
    client = _make_client()

    class StreamEvent:
        def __init__(self) -> None:
            self.session_id = "s1"
            self.event = {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": "{"}}

    events = client._convert_raw_message(StreamEvent())

    assert events == []


def test_stream_event_other_type_emits_activity() -> None:
    client = _make_client()

    class StreamEvent:
        def __init__(self) -> None:
            self.session_id = "s1"
            self.event = {"type": "message_start"}

    events = client._convert_raw_message(StreamEvent())

    assert len(events) == 1
    assert events[0].type == "activity"


def test_user_message_text_block_does_not_emit_delta() -> None:
    client = _make_client()

    class TextBlock:
        text = "Base directory for this skill: /workspace/.claude/skills/visualizer\n\n# Visualizer Skill"

    class UserMessage:
        session_id = "s1"
        content = [TextBlock()]

    events = client._convert_raw_message(UserMessage())

    assert events == []


def test_user_message_tool_result_emits_tool_result_only() -> None:
    client = _make_client()

    class ToolResultBlock:
        tool_use_id = "tool-1"
        content = "Base directory for this skill: /workspace/.claude/skills/visualizer"
        is_error = False

    class UserMessage:
        session_id = "s1"
        content = [ToolResultBlock()]

    events = client._convert_raw_message(UserMessage())

    assert len(events) == 1
    assert events[0].type == "tool_result"
    assert events[0].data["content"] == "Base directory for this skill: /workspace/.claude/skills/visualizer"


def test_tool_result_uses_previous_tool_use_name() -> None:
    client = _make_client()

    class ToolUseBlock:
        id = "tool-1"
        name = "Grep"
        input = {"pattern": "链上交易", "path": "coinex/knowledge/requirements"}

    class AssistantMessage:
        session_id = "s1"
        content = [ToolUseBlock()]

    class ToolResultBlock:
        tool_use_id = "tool-1"
        content = "Path does not exist"
        is_error = True

    class UserMessage:
        session_id = "s1"
        content = [ToolResultBlock()]

    client._convert_raw_message(AssistantMessage())
    events = client._convert_raw_message(UserMessage())

    assert events[0].type == "tool_result"
    assert events[0].data["tool_name"] == "Grep"
    assert events[0].data["is_error"] is True


def test_assistant_message_text_block_still_emits_delta() -> None:
    client = _make_client()

    class TextBlock:
        text = "这是 assistant 正文。"

    class AssistantMessage:
        session_id = "s1"
        content = [TextBlock()]

    events = client._convert_raw_message(AssistantMessage())

    assert len(events) == 1
    assert events[0].type == "delta"
    assert events[0].data["text"] == "这是 assistant 正文。"


def test_assistant_message_thinking_block_emits_redacted_activity() -> None:
    client = _make_client()

    class ThinkingBlock:
        thinking = "内部规划内容"

    class AssistantMessage:
        session_id = "s1"
        content = [ThinkingBlock()]

    events = client._convert_raw_message(AssistantMessage())

    assert len(events) == 1
    assert events[0].type == "activity"
    assert events[0].data["kind"] == "thinking"
    assert events[0].data["thinking_chars"] == len("内部规划内容")
    assert "内部规划内容" not in events[0].data["message"]


def test_assistant_message_skill_tool_use_emits_skill_progress() -> None:
    client = _make_client()

    class ToolUseBlock:
        id = "tool-1"
        name = "Skill"
        input = {"skill": "visualizer", "args": "生成流程图"}

    class AssistantMessage:
        session_id = "s1"
        content = [ToolUseBlock()]

    events = client._convert_raw_message(AssistantMessage())

    assert [event.type for event in events] == ["tool_use", "skill_use"]
    assert events[1].data["skill_id"] == "visualizer"
    assert events[1].data["skill_name"] == "visualizer"
