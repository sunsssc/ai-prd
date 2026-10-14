from __future__ import annotations

from app.business.assistant.runtime_mapper import map_runtime_event_to_sse
from app.integrations.agent_runtime.models import RuntimeEvent


def test_activity_event_maps_to_sse_activity_with_kind() -> None:
    event = RuntimeEvent(
        "activity",
        {
            "message": "已读取上下文，正在整理回答。",
            "kind": "idle_heartbeat",
        },
    )

    mapped = map_runtime_event_to_sse(event)

    assert mapped == [
        {
            "type": "activity",
            "message": "已读取上下文，正在整理回答。",
            "activity_kind": "idle_heartbeat",
        }
    ]


def test_read_event_maps_to_tool_progress_activity() -> None:
    event = RuntimeEvent(
        "read",
        {
            "path": "/tmp/example.md",
        },
    )

    mapped = map_runtime_event_to_sse(event)

    assert mapped == [
        {
            "type": "activity",
            "message": "正在读取：/tmp/example.md",
            "activity_kind": "tool_progress",
        }
    ]


def test_tool_use_maps_without_tool_input_content() -> None:
    event = RuntimeEvent(
        "tool_use",
        {
            "tool_name": "Skill",
            "tool_input": {"skill": "visualizer", "args": "这里可能包含很长的上下文内容。"},
        },
    )

    mapped = map_runtime_event_to_sse(event)

    assert mapped == [
        {
            "type": "tool_use",
            "tool_name": "Skill",
        }
    ]
    assert "tool_input" not in mapped[0]
    assert "上下文内容" not in str(mapped)


def test_tool_result_maps_to_safe_activity_without_content() -> None:
    event = RuntimeEvent(
        "tool_result",
        {
            "tool_name": "Read",
            "content": "这里是文件正文，不应该通过 SSE 输出。",
            "is_error": False,
        },
    )

    mapped = map_runtime_event_to_sse(event)

    assert mapped == [
        {
            "type": "activity",
            "message": "工具执行完成：Read",
            "activity_kind": "tool_progress",
        }
    ]
    assert "文件正文" not in str(mapped)


def test_error_tool_result_maps_to_activity_with_short_detail() -> None:
    event = RuntimeEvent(
        "tool_result",
        {
            "tool_name": "Grep",
            "content": "<tool_use_error>Path does not exist: /coinex/knowledge/requirements.</tool_use_error>",
            "is_error": True,
        },
    )

    mapped = map_runtime_event_to_sse(event)

    assert mapped == [
        {
            "type": "activity",
            "message": "工具执行失败：Grep（Path does not exist: /coinex/knowledge/requirements.）",
            "activity_kind": "tool_progress",
        }
    ]


def test_skill_tool_result_maps_to_named_safe_activity() -> None:
    event = RuntimeEvent(
        "tool_result",
        {
            "tool_name": "Skill",
            "skill_name": "visualizer",
            "content": "Launching skill: visualizer",
            "is_error": False,
        },
    )

    mapped = map_runtime_event_to_sse(event)

    assert mapped == [
        {
            "type": "activity",
            "message": "Skill 执行完成：visualizer",
            "activity_kind": "tool_progress",
        }
    ]


def test_workspace_guard_event_maps_to_dedicated_sse_event() -> None:
    event = RuntimeEvent(
        "workspace_guard",
        {
            "level": "warning",
            "message": "检测到代码知识库被异常修改，已自动回滚 1 个 tracked 文件。",
            "repos": [{"path": "workspace/knowledge/code/repo-a", "rolled_back_files": ["a.py"]}],
        },
    )

    mapped = map_runtime_event_to_sse(event)

    assert mapped == [
        {
            "type": "workspace_guard",
            "message": "检测到代码知识库被异常修改，已自动回滚 1 个 tracked 文件。",
            "level": "warning",
            "details": event.data,
        }
    ]
