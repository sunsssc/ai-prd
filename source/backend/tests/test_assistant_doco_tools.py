from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.business.assistant.store import SQLiteAssistantStore
from app.business.assistant.service import AssistantService
from app.business.assistant.doco_tools import AssistantDocoTools
from app.business.assistant.models import DocoCredentialRecord
from app.api.assistant import delete_doco_settings, get_doco_settings, update_doco_settings
from app.integrations.agent_runtime import (
    RuntimeEvent,
    RuntimeSession,
    RuntimeWorkspaceMount,
    RuntimeWorkspacePlan,
)
from app.integrations.doco import DocoError
from app.schemas.assistant import AssistantDocoSettingsRequest
from app.services.auth_models import UserRecord


class FakeDocoClient:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, object]] = []
        self.save_calls: list[dict[str, object]] = []
        self.next_document_id = 0

    async def list_knowledge_bases(self) -> object:
        return {"data": [{"id": 1, "name": "产品知识库"}, {"id": 2, "name": "研发知识库"}]}

    async def create_document(
        self,
        *,
        title: str,
        knowledge_base_id: int,
        content_markdown: str,
        idempotency_key: str,
    ) -> object:
        self.create_calls.append(
            {
                "title": title,
                "knowledge_base_id": knowledge_base_id,
                "content_markdown": content_markdown,
                "idempotency_key": idempotency_key,
            }
        )
        self.next_document_id += 1
        return {"data": {"id": f"doc_test_{self.next_document_id}"}}

    async def save_document(self, *, document_id: str, content_markdown: str) -> object:
        self.save_calls.append(
            {"document_id": document_id, "content_markdown": content_markdown}
        )
        return {"data": {"document_id": document_id, "version": "sha256:new"}}

    async def fail_next_save(self) -> None:
        self.save_error = DocoError("Doco 请求失败：HTTP 409 文档已被其他调用方修改", status=409)


class CapturingRuntime:
    provider = "mock"

    async def create_or_resume_session(self, **kwargs) -> RuntimeSession:
        self.kwargs = kwargs
        return RuntimeSession(
            provider=self.provider,
            session_id="runtime-1",
            working_directory=kwargs["working_directory"],
            system_prompt=kwargs["system_prompt"],
            dynamic_tools=kwargs.get("dynamic_tools", []),
        )

    async def send_message_stream(self, *, session, message, metadata):
        del session, message, metadata
        yield RuntimeEvent("message", {"content": "已连接 Doco 工具"})
        yield RuntimeEvent("complete", {"result": "已连接 Doco 工具"})


def _user(*, role: str = "admin", user_id: str = "user-1") -> UserRecord:
    now = datetime.now(timezone.utc)
    return UserRecord(
        user_id=user_id,
        email=f"{user_id}@corp.test",
        name="Admin",
        avatar_url=None,
        status="active",
        role=role,
        default_role="member",
        agent_access="active",
        auth_provider="email",
        hosted_domain="corp.test",
        email_verified=True,
        department_name=None,
        access_group=None,
        created_at=now,
        first_login_at=now,
        last_login_at=now,
    )


def _tool_service(
    store: SQLiteAssistantStore,
    client: FakeDocoClient,
    *,
    default_knowledge_base_id: int | None = 1,
) -> AssistantDocoTools:
    now = datetime.now(timezone.utc)
    store.upsert_doco_credentials(
        DocoCredentialRecord(
            user_id="user-1",
            api_token="doco-token-user-1",
            default_knowledge_base_id=default_knowledge_base_id,
            created_at=now,
            updated_at=now,
        )
    )
    return AssistantDocoTools(
        store=store,
        client_factory=lambda api_token: client,
        allowed_organizations=["coinex"],
    )


def _tool(tools, name: str):
    return next(item for item in tools if item.name == name)


def _make_workspace(tmp_path, *, session_id: str = "session-1") -> RuntimeWorkspacePlan:
    root = tmp_path / "runtime" / session_id / "root"
    root.mkdir(parents=True)
    me_dir = tmp_path / "me"
    me_dir.mkdir()
    (root / "me").symlink_to(me_dir, target_is_directory=True)
    plan = RuntimeWorkspacePlan(
        user_id="user-1",
        session_id=session_id,
        organization_key="coinex",
        sandbox_cwd="/",
        host_shadow_root=str(root),
        mounts=[
            RuntimeWorkspaceMount(
                workspace_id="me",
                workspace_key="me",
                host_path=str(me_dir),
                sandbox_path="/me",
                permission="write",
            )
        ],
    )
    return plan


def _write_doc(workspace: RuntimeWorkspacePlan, *, rel: str, content: str) -> None:
    host = workspace.mounts[0].host_path
    path = Path(host) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


@pytest.mark.anyio
async def test_save_new_document_creates_and_maps(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    client = FakeDocoClient()
    service = _tool_service(store, client)
    session = store.create_session("user-1", "Doco 保存", active_organization_key="coinex")
    _, turn = store.start_turn(session.session_id, "user-1", "把文档保存到 Doco")
    workspace = _make_workspace(tmp_path)
    tools = service.build_tools(
        user=_user(),
        session_id=session.session_id,
        turn_id=turn.turn_id,
        organization_key="coinex",
        user_message="把文档保存到 Doco",
        workspace_plan=workspace,
    )
    _write_doc(workspace, rel="docs/需求.md", content="# 需求文档\n\n正文内容")

    result = await _tool(tools, "save_doco_document").handler(
        {"path": "me/docs/需求.md"}
    )
    assert not result.is_error, result.content
    payload = json.loads(result.content)
    assert payload["action"] == "created"
    assert payload["document_id"] == "doc_test_1"
    assert payload["title"] == "需求"
    assert payload["knowledge_base_id"] == 1
    assert payload["document_uri"] == "doco://doc/doc_test_1"
    assert len(client.create_calls) == 1
    assert client.create_calls[0]["title"] == "需求"
    assert client.create_calls[0]["knowledge_base_id"] == 1
    assert client.create_calls[0]["content_markdown"] == "# 需求文档\n\n正文内容"
    assert client.create_calls[0]["idempotency_key"].startswith("doco_save_")

    mapping = store.get_doco_document_mapping(user_id="user-1", local_path="me/docs/需求.md")
    assert mapping is not None
    assert mapping.document_id == "doc_test_1"


@pytest.mark.anyio
async def test_save_again_updates_same_document(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    client = FakeDocoClient()
    service = _tool_service(store, client)
    session = store.create_session("user-1", "Doco 保存", active_organization_key="coinex")
    _, turn = store.start_turn(session.session_id, "user-1", "保存")
    workspace = _make_workspace(tmp_path)
    _write_doc(workspace, rel="docs/需求.md", content="# 需求文档\n\nv1")
    tools = service.build_tools(
        user=_user(),
        session_id=session.session_id,
        turn_id=turn.turn_id,
        organization_key="coinex",
        user_message="保存",
        workspace_plan=workspace,
    )
    save = _tool(tools, "save_doco_document")

    first = await save.handler({"path": "me/docs/需求.md"})
    assert json.loads(first.content)["action"] == "created"
    _write_doc(workspace, rel="docs/需求.md", content="# 需求文档\n\nv2")

    second = await save.handler({"path": "me/docs/需求.md"})
    assert not second.is_error, second.content
    payload = json.loads(second.content)
    assert payload["action"] == "updated"
    assert payload["document_id"] == "doc_test_1"
    assert len(client.create_calls) == 1
    assert len(client.save_calls) == 1
    assert client.save_calls[0]["document_id"] == "doc_test_1"
    assert client.save_calls[0]["content_markdown"] == "# 需求文档\n\nv2"

    mapping = store.get_doco_document_mapping(user_id="user-1", local_path="me/docs/需求.md")
    assert mapping is not None
    assert mapping.document_id == "doc_test_1"


@pytest.mark.anyio
async def test_save_rejects_path_escape(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    client = FakeDocoClient()
    service = _tool_service(store, client)
    session = store.create_session("user-1", "Doco 保存", active_organization_key="coinex")
    _, turn = store.start_turn(session.session_id, "user-1", "保存")
    tools = service.build_tools(
        user=_user(),
        session_id=session.session_id,
        turn_id=turn.turn_id,
        organization_key="coinex",
        user_message="保存",
        workspace_plan=_make_workspace(tmp_path),
    )
    result = await _tool(tools, "save_doco_document").handler(
        {"path": "me/../outside.md"}
    )
    assert result.is_error
    assert "超出工作区挂载范围" in json.loads(result.content)["error"]
    assert client.create_calls == []


@pytest.mark.anyio
async def test_save_requires_knowledge_base(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    client = FakeDocoClient()
    service = _tool_service(store, client, default_knowledge_base_id=None)
    session = store.create_session("user-1", "Doco 保存", active_organization_key="coinex")
    _, turn = store.start_turn(session.session_id, "user-1", "保存")
    workspace = _make_workspace(tmp_path)
    _write_doc(workspace, rel="docs/需求.md", content="# 需求文档")
    tools = service.build_tools(
        user=_user(),
        session_id=session.session_id,
        turn_id=turn.turn_id,
        organization_key="coinex",
        user_message="保存",
        workspace_plan=workspace,
    )
    result = await _tool(tools, "save_doco_document").handler(
        {"path": "me/docs/需求.md"}
    )
    assert result.is_error
    assert "默认知识库" in json.loads(result.content)["error"]
    assert client.create_calls == []


@pytest.mark.anyio
async def test_list_knowledge_bases_and_get_mapping(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    client = FakeDocoClient()
    service = _tool_service(store, client)
    session = store.create_session("user-1", "Doco 保存", active_organization_key="coinex")
    _, turn = store.start_turn(session.session_id, "user-1", "查知识库")
    workspace = _make_workspace(tmp_path)
    tools = service.build_tools(
        user=_user(),
        session_id=session.session_id,
        turn_id=turn.turn_id,
        organization_key="coinex",
        user_message="查知识库",
        workspace_plan=workspace,
    )

    list_result = await _tool(tools, "list_doco_knowledge_bases").handler({})
    assert not list_result.is_error, list_result.content
    payload = json.loads(list_result.content)
    assert payload["knowledge_bases"] == [
        {"id": 1, "name": "产品知识库"},
        {"id": 2, "name": "研发知识库"},
    ]
    assert payload["default_knowledge_base_id"] == 1

    missing = await _tool(tools, "get_doco_document_mapping").handler(
        {"path": "me/docs/需求.md"}
    )
    assert json.loads(missing.content) == {"mapped": False, "local_path": "me/docs/需求.md"}

    _write_doc(workspace, rel="docs/需求.md", content="# 需求文档")
    await _tool(tools, "save_doco_document").handler({"path": "me/docs/需求.md"})
    found = await _tool(tools, "get_doco_document_mapping").handler(
        {"path": "me/docs/需求.md"}
    )
    assert not found.is_error, found.content
    payload = json.loads(found.content)
    assert payload["mapped"] is True
    assert payload["document_id"] == "doc_test_1"
    assert payload["title"] == "需求"


@pytest.mark.anyio
async def test_save_with_explicit_knowledge_base_and_title(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    client = FakeDocoClient()
    service = _tool_service(store, client, default_knowledge_base_id=None)
    session = store.create_session("user-1", "Doco 保存", active_organization_key="coinex")
    _, turn = store.start_turn(session.session_id, "user-1", "保存")
    workspace = _make_workspace(tmp_path)
    _write_doc(workspace, rel="docs/需求.md", content="# 需求文档")
    tools = service.build_tools(
        user=_user(),
        session_id=session.session_id,
        turn_id=turn.turn_id,
        organization_key="coinex",
        user_message="保存",
        workspace_plan=workspace,
    )
    result = await _tool(tools, "save_doco_document").handler(
        {"path": "me/docs/需求.md", "title": "2026 需求汇总", "knowledge_base_id": 2}
    )
    assert not result.is_error, result.content
    payload = json.loads(result.content)
    assert payload["action"] == "created"
    assert payload["title"] == "2026 需求汇总"
    assert payload["knowledge_base_id"] == 2
    assert client.create_calls[0]["knowledge_base_id"] == 2


def test_doco_client_uses_each_users_own_token(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    client = FakeDocoClient()
    now = datetime.now(timezone.utc)
    for user_id, token in (("user-1", "token-one"), ("user-2", "token-two")):
        store.upsert_doco_credentials(
            DocoCredentialRecord(
                user_id=user_id,
                api_token=token,
                default_knowledge_base_id=1,
                created_at=now,
                updated_at=now,
            )
        )
    seen_tokens: list[str] = []
    service = AssistantDocoTools(
        store=store,
        client_factory=lambda api_token: seen_tokens.append(api_token) or client,
        allowed_organizations=["coinex"],
    )

    tools = service.build_tools(
        user=_user(user_id="user-2"),
        session_id="session-1",
        turn_id="turn-1",
        organization_key="coinex",
        user_message="查询知识库",
    )

    assert tools
    assert seen_tokens == ["token-two"]


def test_doco_settings_are_user_scoped_and_do_not_return_token(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))

    empty = get_doco_settings(current_user=_user(), assistant_store=store)
    assert empty.configured is False
    assert empty.default_knowledge_base_id is None

    updated = update_doco_settings(
        body=AssistantDocoSettingsRequest(api_token="token-one", default_knowledge_base_id=7),
        current_user=_user(),
        assistant_store=store,
    )
    assert updated.configured is True
    assert updated.default_knowledge_base_id == 7
    assert not hasattr(updated, "api_token")
    assert store.get_doco_credentials(user_id="user-1").api_token == "token-one"
    assert get_doco_settings(current_user=_user(user_id="user-2"), assistant_store=store).configured is False

    kept_token = update_doco_settings(
        body=AssistantDocoSettingsRequest(default_knowledge_base_id=8),
        current_user=_user(),
        assistant_store=store,
    )
    assert kept_token.default_knowledge_base_id == 8
    assert store.get_doco_credentials(user_id="user-1").api_token == "token-one"

    deleted = delete_doco_settings(current_user=_user(), assistant_store=store)
    assert deleted.configured is False
    assert store.get_doco_credentials(user_id="user-1") is None


@pytest.mark.anyio
async def test_assistant_injects_doco_tools(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    client = FakeDocoClient()
    doco_tools = _tool_service(store, client)
    runtime = CapturingRuntime()
    assistant = AssistantService(
        store=store,
        runtime_client=runtime,
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
        doco_tools=doco_tools,
    )
    session = assistant.create_session(_user(), organization_key="coinex")
    await assistant.chat(_user(), session.session_id, "帮我把文档保存到 Doco")

    tool_names = {item.name for item in runtime.kwargs["dynamic_tools"]}
    assert {
        "list_doco_knowledge_bases",
        "save_doco_document",
        "get_doco_document_mapping",
    } <= tool_names
    assert "save_doco_document" in runtime.kwargs["system_prompt"]
