from __future__ import annotations

from collections.abc import Iterable

from app.business.assistant.models import AssistantContextInput
from app.integrations.agent_runtime.models import RuntimeEvent


def map_runtime_event_to_sse(event: RuntimeEvent) -> list[dict[str, object]]:
    if event.type == "mcp_approval":
        return [{"type": "mcp_approval", "approval": event.data}]
    if event.type == "delta":
        return [{"type": "delta", "delta": str(event.data.get("text", ""))}]
    if event.type == "tool_use":
        return [
            {
                "type": "tool_use",
                "tool_name": event.data.get("tool_name"),
            }
        ]
    if event.type == "tool_result":
        return [
            {
                "type": "activity",
                "message": build_tool_result_activity_message(event),
                "activity_kind": "tool_progress",
            }
        ]
    if event.type == "skill_use":
        return [
            {
                "type": "skill_use",
                "skill_id": event.data.get("skill_id"),
                "skill_name": event.data.get("skill_name"),
                "path": event.data.get("path"),
            }
        ]
    if event.type == "activity":
        return [
            {
                "type": "activity",
                "message": str(event.data.get("message", "正在处理本轮请求")),
                "activity_kind": str(event.data.get("kind") or "status"),
            }
        ]
    if event.type == "workspace_guard":
        return [
            {
                "type": "workspace_guard",
                "message": str(event.data.get("message", "已检查代码知识库改动。")),
                "level": str(event.data.get("level") or "warning"),
                "details": event.data,
            }
        ]
    if event.type in {"search", "read"}:
        return [
            {
                "type": "activity",
                "message": build_activity_message(event),
                "activity_kind": "tool_progress",
            }
        ]
    if event.type == "usage":
        return [{"type": "usage", "usage": event.data}]
    if event.type == "error":
        return [{"type": "error", "message": str(event.data.get("message", "Runtime 执行失败。"))}]
    return []


def build_activity_message(event: RuntimeEvent) -> str:
    if event.type == "search":
        query = str(event.data.get("query") or event.data.get("target") or "项目内容")
        return f"正在搜索：{query}"
    if event.type == "read":
        path = str(event.data.get("path") or "目标文件")
        return f"正在读取：{path}"
    return "正在处理本轮请求"


def build_tool_result_activity_message(event: RuntimeEvent) -> str:
    tool_name = str(event.data.get("tool_name") or "").strip()
    is_error = bool(event.data.get("is_error", False))

    if tool_name.lower() == "skill":
        skill_name = str(event.data.get("skill_name") or "").strip()
        return f"Skill 执行失败：{skill_name or 'Skill'}" if is_error else f"Skill 执行完成：{skill_name or 'Skill'}"

    if is_error:
        message = f"工具执行失败：{tool_name or '工具'}"
        detail = _safe_error_detail(event.data.get("content"))
        return f"{message}（{detail}）" if detail else message
    return f"工具执行完成：{tool_name or '工具'}"


def _safe_error_detail(value: object) -> str | None:
    if value is None:
        return None
    detail = str(value).strip()
    if not detail:
        return None
    detail = detail.replace("<tool_use_error>", "").replace("</tool_use_error>", "")
    detail = " ".join(detail.split())
    if len(detail) > 180:
        detail = detail[:177].rstrip() + "..."
    return detail


def extract_context_usage_from_event(event: RuntimeEvent) -> list[AssistantContextInput]:
    usage_items: list[AssistantContextInput] = []

    if event.type == "read":
        path = str(event.data.get("path") or "").strip()
        if path:
            usage_items.append(
                AssistantContextInput(
                    context_key=f"read:{path}",
                    label=path,
                    content=_normalize_optional_text(event.data.get("excerpt")),
                    source_type="file",
                    source_uri=path,
                    metadata={key: value for key, value in event.data.items() if key != "excerpt"},
                )
            )
            if "/.claude/skills/" in path or path.endswith("/SKILL.md"):
                usage_items.append(
                    AssistantContextInput(
                        context_key=f"skill:{path}",
                        label=str(event.data.get("skill_name") or _skill_name_from_path(path)),
                        content=_normalize_optional_text(event.data.get("excerpt")),
                        source_type="skill",
                        source_uri=path,
                        metadata={key: value for key, value in event.data.items() if key != "excerpt"},
                    )
                )

    if event.type == "skill_use":
        path = _normalize_optional_text(event.data.get("path"))
        usage_items.append(
            AssistantContextInput(
                context_key=f"skill:{event.data.get('skill_id') or path or 'unknown'}",
                label=str(event.data.get("skill_name") or "Skill"),
                source_type="skill",
                source_uri=path,
                metadata=dict(event.data),
            )
        )

    return usage_items


def dedupe_context_inputs(items: Iterable[AssistantContextInput]) -> list[AssistantContextInput]:
    deduped: list[AssistantContextInput] = []
    seen: set[tuple[str, str | None, str]] = set()

    for item in items:
        key = (item.source_type, item.source_uri, item.label)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)

    return deduped


def _normalize_optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _skill_name_from_path(path: str) -> str:
    parts = [part for part in path.split("/") if part]
    if len(parts) >= 2 and parts[-1] == "SKILL.md":
        return parts[-2]
    return parts[-1] if parts else "skill"
