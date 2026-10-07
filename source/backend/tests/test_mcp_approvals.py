from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api.assistant import router
from app.business.assistant.mcp_approvals import McpApprovalConflict, McpApprovals
from app.business.assistant.service import AssistantService, sanitize_shared_turn_stream_event
from app.business.assistant.store import SQLiteAssistantStore
from app.core.dependencies import get_assistant_service, get_current_app_user
from app.integrations.agent_runtime.mcp_approvals import messages_with_mcp_approvals
from app.integrations.agent_runtime.models import RuntimeEvent
from app.integrations.agent_runtime.mock_runtime import MockAgentRuntimeClient


@pytest.fixture
def context(tmp_path):
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    session = store.create_session("owner", "部署确认")
    message = store.append_message(session.session_id, "owner", "user", "部署到测试环境")
    turn = store.create_turn(session.session_id, "owner", message.message_id)
    service = AssistantService(store=store, runtime_client=SimpleNamespace(provider="codex"),
                               system_prompt="", runtime_working_directory=str(tmp_path))
    return service, session, turn


DETAILS = {"server_name": "coinex-tests-ops", "tool_name": "ops_prepare_deployment",
           "message": "Allow deployment precheck?", "description": "部署预检查",
           "arguments": {"service_ids": ["admin-test11"], "branch": "cgw-dev"}}


class QueueClient:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.results = []

    async def messages(self):
        while True:
            item = await self.queue.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def respond_result(self, request_id, result):
        self.results.append((request_id, result))


def request(request_id=0, **overrides):
    return {"id": request_id, "method": "mcpServer/elicitation/request", "params": {
        "threadId": "thread", "turnId": "turn", "serverName": "coinex-tests-ops",
        "mode": "form", "message": "Allow deployment precheck?",
        "requestedSchema": {"type": "object", "properties": {}},
        "_meta": {"codex_approval_kind": "mcp_tool_call", "tool_params": DETAILS["arguments"],
                  "tool_description": "部署预检查"}, **overrides}}


def stream_for(client, context):
    service, session, turn = context
    return messages_with_mcp_approvals(client, thread_id="thread", turn_id="turn",
        register=lambda details: service.mcp_approvals.request(session.session_id, turn.turn_id, details),
        expire=service.mcp_approvals.expire)


@pytest.mark.anyio
@pytest.mark.parametrize(("decision", "action"), [("approved", "accept"), ("declined", "decline")])
async def test_protocol_waits_for_exact_user_decision(context, decision, action):
    service, session, turn = context
    client = QueueClient()
    stream = stream_for(client, context)
    await client.queue.put(request())
    pending = await anext(stream)
    assert isinstance(pending, RuntimeEvent) and pending.data["status"] == "pending"
    assert client.results == []
    # 刷新页面能读取同一个请求，不会生成新的批准或执行。
    assert service.mcp_approvals.list_for_session(session.session_id)[0] == pending.data
    approval_id = pending.data["approval_id"]
    service.decide_mcp_approval(SimpleNamespace(user_id="owner"), turn.turn_id, approval_id, decision)
    resolved = await anext(stream)
    assert resolved.data["status"] == decision
    assert client.results == [(0, {"action": action, "content": {} if action == "accept" else None})]
    service.decide_mcp_approval(SimpleNamespace(user_id="owner"), turn.turn_id, approval_id, decision)
    assert len(client.results) == 1
    with pytest.raises(McpApprovalConflict):
        service.decide_mcp_approval(SimpleNamespace(user_id="owner"), turn.turn_id, approval_id,
                                   "declined" if decision == "approved" else "approved")
    await stream.aclose()


@pytest.mark.anyio
async def test_parallel_approvals_are_independent(context):
    service, _, turn = context
    client = QueueClient()
    stream = stream_for(client, context)
    await client.queue.put(request(1))
    first = await anext(stream)
    await client.queue.put(request(2))
    second = await anext(stream)
    service.decide_mcp_approval(SimpleNamespace(user_id="owner"), turn.turn_id,
                               second.data["approval_id"], "approved")
    assert (await anext(stream)).data["approval_id"] == second.data["approval_id"]
    assert client.results == [(2, {"action": "accept", "content": {}})]
    assert first.data["approval_id"] in service.mcp_approvals.pending
    await stream.aclose()
    assert not service.mcp_approvals.pending


@pytest.mark.anyio
@pytest.mark.parametrize("ending", [None, "resolved", "completed"])
async def test_disconnect_or_cancel_expires_without_accepting(context, ending):
    service, session, _ = context
    client = QueueClient()
    stream = stream_for(client, context)
    await client.queue.put(request())
    await anext(stream)
    notification = None if ending is None else {
        "method": "serverRequest/resolved" if ending == "resolved" else "turn/completed",
        "params": {"threadId": "thread", "requestId": 0},
    }
    await client.queue.put(notification)
    if ending is None:
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    else:
        await anext(stream)
        await stream.aclose()
    assert client.results == []
    assert service.mcp_approvals.list_for_session(session.session_id)[0]["status"] == "expired"
    assert not service.mcp_approvals.pending


@pytest.mark.anyio
async def test_timeout_sends_cancel_and_rejects_late_decision(context):
    service, _, turn = context
    service.mcp_approvals.timeout_seconds = 0.02
    client = QueueClient()
    stream = stream_for(client, context)
    await client.queue.put(request())
    pending = await anext(stream)
    assert (await asyncio.wait_for(anext(stream), 1)).data["status"] == "expired"
    assert client.results == [(0, {"action": "cancel", "content": None})]
    with pytest.raises(McpApprovalConflict):
        service.decide_mcp_approval(SimpleNamespace(user_id="owner"), turn.turn_id,
                                   pending.data["approval_id"], "approved")
    await stream.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("overrides", [
    {"threadId": "other-thread"}, {"turnId": "other-turn"},
    {"mode": "url"}, {"_meta": {}},
    {"requestedSchema": {"type": "object", "properties": {"password": {"type": "string"}}}},
])
async def test_unsupported_elicitations_do_not_create_approval(context, overrides):
    service, _, _ = context
    client = QueueClient()
    stream = stream_for(client, context)
    await client.queue.put(request(**overrides))
    await client.queue.put(None)
    assert [item async for item in stream] == []
    assert client.results == [(0, {"action": "decline", "content": None})]
    assert not service.mcp_approvals.pending


@pytest.mark.anyio
async def test_restart_does_not_reactivate_persisted_approval(context):
    service, session, turn = context
    record, future = service.mcp_approvals.request(session.session_id, turn.turn_id, DETAILS)
    restarted = McpApprovals(service.store)
    assert restarted.list_for_session(session.session_id)[0]["status"] == "expired"
    with pytest.raises(McpApprovalConflict):
        restarted.decide(session.session_id, turn.turn_id, record["approval_id"], "approved", "owner")
    assert not future.done()
    service.mcp_approvals.expire_turn(turn.turn_id)


@pytest.mark.anyio
async def test_api_requires_owner_and_exact_turn_and_no_argument_edits(context):
    service, session, turn = context
    record, future = service.mcp_approvals.request(session.session_id, turn.turn_id, DETAILS)
    api = FastAPI()
    api.include_router(router, prefix="/api")
    api.dependency_overrides[get_assistant_service] = lambda: service
    api.dependency_overrides[get_current_app_user] = lambda: SimpleNamespace(user_id="other")
    url = f"/api/assistant/turns/{turn.turn_id}/mcp-approvals/{record['approval_id']}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
        assert (await client.post(url, json={"decision": "approved"})).status_code == 404
        assert not future.done()
        api.dependency_overrides[get_current_app_user] = lambda: SimpleNamespace(user_id="owner")
        assert (await client.post(url, json={"decision": "approved", "arguments": {}})).status_code == 422
        assert (await client.post(url, json={"decision": "approve_all"})).status_code == 422
        assert not future.done()
        other_session = service.store.create_session("owner", "另一个会话")
        other_message = service.store.append_message(other_session.session_id, "owner", "user", "查询")
        other_turn = service.store.create_turn(other_session.session_id, "owner", other_message.message_id)
        assert (await client.post(url.replace(turn.turn_id, other_turn.turn_id), json={"decision": "approved"})).status_code == 404
        response = await client.post(url, json={"decision": "approved"})
        assert response.status_code == 200
        assert (await future)["resolved_by"] == "owner"
        assert (await client.post(url, json={"decision": "approved"})).status_code == 200
        assert (await client.post(url, json={"decision": "declined"})).status_code == 409
    assert sanitize_shared_turn_stream_event({"type": "mcp_approval", "approval": record}) is None


@pytest.mark.anyio
async def test_stop_turn_expires_pending_approval(context):
    service, session, turn = context
    record, future = service.mcp_approvals.request(session.session_id, turn.turn_id, DETAILS)
    await service.stop_turn(SimpleNamespace(user_id="owner"), turn.turn_id)
    assert (await future)["status"] == "expired"
    with pytest.raises(McpApprovalConflict):
        service.decide_mcp_approval(SimpleNamespace(user_id="owner"), turn.turn_id,
                                   record["approval_id"], "approved")


@pytest.mark.anyio
async def test_chat_stream_reconnect_keeps_approval_and_continues_same_turn(context):
    service, _, _ = context
    executed = []

    class ApprovalRuntime(MockAgentRuntimeClient):
        async def send_message_stream(self, *, session, message, metadata):
            record, future = metadata["mcp_approval_register"](DETAILS)
            yield RuntimeEvent("mcp_approval", record)
            resolved = await future
            if resolved["status"] == "approved":
                executed.append(record["approval_id"])
            yield RuntimeEvent("mcp_approval", resolved)
            yield RuntimeEvent("message", {"content": "操作已处理"})
            yield RuntimeEvent("complete", {"result": "操作已处理"})

    service.runtime_client = ApprovalRuntime()
    owner = SimpleNamespace(user_id="owner")
    stream = service.stream_chat(owner, None, "处理测试操作")
    async for event in stream:
        if event["type"] == "mcp_approval":
            pending = event["approval"]
            break
    await stream.aclose()
    assert executed == []
    resumed = service.resume_turn_stream(owner, pending["turn_id"])
    async for event in resumed:
        if event["type"] == "mcp_approval":
            assert event["approval"] == pending
            break
    assert executed == []
    service.decide_mcp_approval(owner, pending["turn_id"], pending["approval_id"], "approved")
    remaining = [event async for event in resumed]
    assert any(event["type"] == "complete" for event in remaining)
    assert executed == [pending["approval_id"]]
    assert service.mcp_approvals.list_for_session(pending["session_id"])[0]["status"] == "approved"


@pytest.mark.anyio
async def test_audit_failure_does_not_release_approval(context, monkeypatch):
    service, session, turn = context
    record, future = service.mcp_approvals.request(session.session_id, turn.turn_id, DETAILS)
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise RuntimeError("模拟持久化失败")
        patch.setattr(service.store, "append_runtime_event", fail)
        with pytest.raises(RuntimeError, match="持久化失败"):
            service.decide_mcp_approval(SimpleNamespace(user_id="owner"), turn.turn_id,
                                       record["approval_id"], "approved")
        assert not future.done()
    service.mcp_approvals.expire_turn(turn.turn_id)


@pytest.mark.anyio
async def test_protocol_process_error_cleans_pending_approval(context):
    service, session, _ = context
    client = QueueClient()
    stream = stream_for(client, context)
    await client.queue.put(request())
    await anext(stream)
    await client.queue.put(RuntimeError("进程退出"))
    with pytest.raises(RuntimeError, match="进程退出"):
        await anext(stream)
    assert client.results == []
    assert not service.mcp_approvals.pending
    assert service.mcp_approvals.list_for_session(session.session_id)[0]["status"] == "expired"
