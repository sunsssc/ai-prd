from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from typing import Any

from app.integrations.agent_runtime.codex_app_server_client import CodexAppServerClient
from app.integrations.agent_runtime.models import RuntimeEvent


def mcp_approval_policy() -> dict[str, object]:
    return {"granular": {
        "mcp_elicitations": True,
        "sandbox_approval": False,
        "rules": False,
        "request_permissions": False,
        "skill_approval": False,
    }}


async def messages_with_mcp_approvals(
    client: CodexAppServerClient,
    *,
    thread_id: str,
    turn_id: str,
    register: Callable | None,
    expire: Callable | None,
) -> AsyncIterator[dict[str, Any] | RuntimeEvent]:
    """等待用户决定期间继续读取协议通知，处理并行审批、取消及进程断开。"""
    messages = client.messages().__aiter__()
    next_message = asyncio.create_task(messages.__anext__())
    pending: dict[object, tuple[dict[str, object], asyncio.Future]] = {}
    active_calls: dict[str, dict[str, Any]] = {}
    try:
        while True:
            done, _ = await asyncio.wait(
                [next_message, *(future for _, future in pending.values())],
                return_when=asyncio.FIRST_COMPLETED,
            )
            # 先处理协议通知，避免已被 Runtime 取消的请求因同时到达的批准而继续执行。
            if next_message in done:
                try:
                    message = next_message.result()
                except StopAsyncIteration:
                    return
                next_message = asyncio.create_task(messages.__anext__())
                method = message.get("method")
                params = message.get("params") or {}
                item = params.get("item") or {}
                if method == "item/started" and item.get("type") == "mcpToolCall":
                    active_calls[str(item["id"])] = item
                if method == "item/completed":
                    active_calls.pop(str(item.get("id")), None)
                if method == "serverRequest/resolved" and params.get("threadId") == thread_id:
                    entry = pending.pop(params.get("requestId"), None)
                    if entry is not None:
                        record, future = entry
                        expired = expire(str(record["approval_id"])) if expire else None
                        future.cancel()
                        if expired:
                            yield RuntimeEvent("mcp_approval", expired)
                if method == "turn/completed":
                    yield message
                    return
                if method == "mcpServer/elicitation/request" and message.get("id") is not None:
                    meta = params.get("_meta") or {}
                    schema = params.get("requestedSchema") or {}
                    supported = (
                        isinstance(meta, dict)
                        and meta.get("codex_approval_kind") == "mcp_tool_call"
                        and params.get("mode") == "form"
                        and isinstance(schema, dict)
                        and schema.get("type") == "object"
                        and not schema.get("properties")
                        and not schema.get("required")
                        and params.get("threadId") == thread_id
                        and params.get("turnId") == turn_id
                    )
                    if not supported or not callable(register) or not callable(expire):
                        await client.respond_result(message["id"], {"action": "decline", "content": None})
                        continue
                    matches = [call for call in active_calls.values()
                               if call.get("server") == params.get("serverName")
                               and call.get("arguments") == meta.get("tool_params")]
                    details = {
                        "server_name": params.get("serverName"),
                        "tool_name": matches[0].get("tool") if len(matches) == 1 else None,
                        "message": str(params.get("message") or ""),
                        "description": str(meta.get("tool_description") or ""),
                        "arguments": meta.get("tool_params"),
                    }
                    record, future = register(details)
                    pending[message["id"]] = (record, future)
                    yield RuntimeEvent("mcp_approval", record)
                else:
                    yield message
            for request_id, (record, future) in list(pending.items()):
                if not future.done():
                    continue
                pending.pop(request_id)
                if future.cancelled():
                    resolved = expire(str(record["approval_id"])) if expire else None
                    resolved = resolved or {**record, "status": "expired"}
                else:
                    resolved = future.result()
                action = {"approved": "accept", "declined": "decline"}.get(str(resolved["status"]), "cancel")
                await client.respond_result(request_id, {"action": action, "content": {} if action == "accept" else None})
                yield RuntimeEvent("mcp_approval", resolved)
    finally:
        next_message.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await next_message
        for record, future in pending.values():
            if expire:
                expire(str(record["approval_id"]))
            future.cancel()
        await messages.aclose()
