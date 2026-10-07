from __future__ import annotations

import asyncio
import json
import queue
import shutil
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest

from conftest import reset_dependency_overrides
from app.business.assistant.models import (
    AssistantContextInput,
    AssistantRuntimeEventRecord,
    AssistantSessionRecord,
    AssistantTurnRecord,
    TestDataPlanRecord as DataPlanRecord,
)
from app.business.assistant.store import SQLiteAssistantStore
from app.core.config import settings
from app.core.dependencies import (
    get_access_policy_service,
    get_assistant_runtime_client,
    get_assistant_service,
    get_assistant_store,
    get_assistant_test_data_tools,
    get_auth_service,
    get_auth_store,
    get_email_sender,
    get_google_oauth_service,
    get_lightweight_llm_client,
    get_notification_service,
    get_oauth_state_store,
    get_workspace_access_service,
    get_workspace_access_store,
    get_workspace_browser_service,
)
from app.integrations.agent_runtime.models import AssistantRuntimeError, RuntimeEvent, RuntimeSession
from app.integrations.agent_runtime.mock_runtime import MockAgentRuntimeClient
from app.main import app


class _CapturingWorkspaceRuntime(MockAgentRuntimeClient):
    def __init__(self) -> None:
        super().__init__(skills_root=settings.skills_root)
        self.workspace_plans = []
        self.messages = []
        self.metadata = []

    async def create_or_resume_session(self, **kwargs):
        session = await super().create_or_resume_session(**kwargs)
        self.workspace_plans.append(session.workspace_plan)
        return session

    async def send_message_stream(self, *, session, message, metadata):
        self.messages.append(message)
        self.metadata.append(metadata)
        async for event in super().send_message_stream(
            session=session,
            message=message,
            metadata=metadata,
        ):
            yield event


class _CapturingNotificationService:
    def __init__(self) -> None:
        self.shared_sessions: list[dict[str, str]] = []

    def notify_agent_access_requested(self, user) -> None:
        del user

    def notify_agent_access_approved(self, user) -> None:
        del user

    def notify_session_shared(self, *, owner, recipient, share_url: str) -> None:
        self.shared_sessions.append(
            {
                "owner_email": owner.email,
                "recipient_email": recipient.email,
                "share_url": share_url,
            }
        )


class _BlockingSharedFollowUpRuntime(MockAgentRuntimeClient):
    def __init__(self) -> None:
        super().__init__(skills_root=settings.skills_root)
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.call_count = 0
        self.active_count = 0
        self.max_active_count = 0

    async def send_message_stream(self, *, session, message, metadata):
        self.call_count += 1
        call_number = self.call_count
        self.active_count += 1
        self.max_active_count = max(self.max_active_count, self.active_count)
        try:
            if call_number == 1:
                self.first_started.set()
                await self.release_first.wait()
            async for event in super().send_message_stream(
                session=session,
                message=message,
                metadata=metadata,
            ):
                yield event
        finally:
            self.active_count -= 1


class _MissingRolloutThenRebuiltRuntime(MockAgentRuntimeClient):
    def __init__(self, legacy_session_id: str) -> None:
        super().__init__(skills_root=settings.skills_root)
        self.legacy_session_id = legacy_session_id
        self.missing_rollout_started = asyncio.Event()
        self.release_missing_rollout = asyncio.Event()
        self.rebuild_started = asyncio.Event()
        self.release_rebuild = asyncio.Event()
        self.rehydrated_messages: list[str] = []

    async def send_message_stream(self, *, session, message, metadata):
        if session.session_id == self.legacy_session_id:
            self.missing_rollout_started.set()
            await self.release_missing_rollout.wait()
            raise AssistantRuntimeError(
                f"no rollout found for thread id {self.legacy_session_id}"
            )
        self.rehydrated_messages.append(message)
        self.rebuild_started.set()
        await self.release_rebuild.wait()
        async for event in super().send_message_stream(
            session=session,
            message=message,
            metadata=metadata,
        ):
            yield event


@pytest.fixture(autouse=True)
def reset_state(tmp_path: Path) -> None:
    get_google_oauth_service.cache_clear()
    get_access_policy_service.cache_clear()
    get_auth_service.cache_clear()
    get_auth_store.cache_clear()
    get_oauth_state_store.cache_clear()
    get_email_sender.cache_clear()
    get_notification_service.cache_clear()
    get_workspace_access_service.cache_clear()
    get_workspace_access_store.cache_clear()
    get_assistant_store.cache_clear()
    get_assistant_test_data_tools.cache_clear()
    get_assistant_runtime_client.cache_clear()
    get_lightweight_llm_client.cache_clear()
    get_assistant_service.cache_clear()
    get_workspace_browser_service.cache_clear()
    reset_dependency_overrides()

    original_auth_db_path = settings.auth_db_path
    original_assistant_db_path = settings.assistant_db_path
    original_image_upload_root = settings.assistant_image_upload_root
    original_email_domain = settings.email_login_allowed_domain
    original_delivery_mode = settings.email_delivery_mode
    original_debug_response = settings.email_verification_debug_response
    original_ai_provider = settings.ai_provider
    original_ai_cli_path = settings.ai_cli_path
    original_ai_light_api_key = settings.ai_light_api_key
    original_test_help_token = settings.test_help_token
    original_working_directory = settings.ai_working_directory
    original_skills_root = settings.skills_root

    settings.auth_db_path = str(tmp_path / "auth.sqlite3")
    settings.assistant_db_path = str(tmp_path / "assistant.sqlite3")
    settings.assistant_image_upload_root = str(
        Path(settings.ai_working_directory) / "workspace/runtime/uploads/test-assistant-images" / tmp_path.name
    )
    settings.email_login_allowed_domain = "corp.test"
    settings.email_delivery_mode = "debug"
    settings.email_verification_debug_response = True
    settings.ai_provider = "mock"
    settings.ai_cli_path = None
    settings.ai_light_api_key = ""
    settings.test_help_token = ""
    settings.ai_working_directory = str(Path(__file__).resolve().parents[3])
    settings.skills_root = str(Path(settings.ai_working_directory) / ".claude/skills")

    yield

    settings.auth_db_path = original_auth_db_path
    settings.assistant_db_path = original_assistant_db_path
    shutil.rmtree(settings.assistant_image_upload_root, ignore_errors=True)
    settings.assistant_image_upload_root = original_image_upload_root
    settings.email_login_allowed_domain = original_email_domain
    settings.email_delivery_mode = original_delivery_mode
    settings.email_verification_debug_response = original_debug_response
    settings.ai_provider = original_ai_provider
    settings.ai_cli_path = original_ai_cli_path
    settings.ai_light_api_key = original_ai_light_api_key
    settings.test_help_token = original_test_help_token
    settings.ai_working_directory = original_working_directory
    settings.skills_root = original_skills_root
    reset_dependency_overrides()
    get_assistant_service.cache_clear()
    get_assistant_test_data_tools.cache_clear()
    get_lightweight_llm_client.cache_clear()
    get_workspace_access_service.cache_clear()
    get_workspace_access_store.cache_clear()


async def _register_user(client: httpx.AsyncClient, email: str = "assistant@corp.test") -> str:
    send_code_response = await client.post(
        "/api/auth/email/request-code",
        json={"email": email, "purpose": "register"},
    )
    code = send_code_response.json()["debug_code"]
    register_response = await client.post(
        "/api/auth/email/register",
        json={"email": email, "name": "Assistant User", "code": code},
    )
    payload = register_response.json()
    user_id = payload["user"]["user_id"]
    get_auth_store().update_user_status(user_id, "active")
    get_auth_store().update_user_agent_access(user_id, "active")
    get_workspace_access_store().upsert_user_membership(
        user_id=user_id,
        organization_key="coinex",
        source="test",
        is_default=True,
    )
    return payload["session"]["token"]


async def _register_user_with_profile(client: httpx.AsyncClient, email: str) -> tuple[str, dict[str, object]]:
    token = await _register_user(client, email=email)
    client.cookies.set(settings.session_cookie_name, token)
    me_response = await client.get("/api/auth/me")
    assert me_response.status_code == 200
    return token, me_response.json()["user"]


@pytest.mark.anyio
async def test_assistant_chat_creates_session_and_persists_history(client: httpx.AsyncClient) -> None:
    token = await _register_user(client)
    client.cookies.set(settings.session_cookie_name, token)

    response = await client.post(
        "/api/assistant/chat",
        json={"message": "请帮我总结优惠券规则"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_created"] is True
    assert payload["user_message"]["content"] == "请帮我总结优惠券规则"
    assert "mock agent runtime" in payload["assistant_message"]["content"]
    assert payload["turn_id"]
    assert payload["session"]["runtime_provider"] == "mock"
    assert payload["citations"]

    session_id = payload["session"]["session_id"]
    detail_response = await client.get(f"/api/assistant/sessions/{session_id}")

    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["session"]["message_count"] == 2
    assert [message["role"] for message in detail_payload["messages"]] == ["user", "assistant"]
    assert detail_payload["messages"][0]["author"] is None


@pytest.mark.anyio
async def test_admin_can_view_all_chat_sessions(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="ops-admin@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    chat_response = await client.post(
        "/api/assistant/chat",
        json={"message": "这是一条需要运维查看的聊天记录"},
    )
    assert chat_response.status_code == 200
    chat_payload = chat_response.json()
    session_id = chat_payload["session"]["session_id"]
    assistant_message_id = chat_payload["assistant_message"]["message_id"]

    user = get_auth_store().get_user_by_email("ops-admin@corp.test")
    assert user is not None
    image_bytes = b"\x89PNG\r\n\x1a\nadmin-generated-image"
    image_path = (
        Path(settings.ai_working_directory)
        / "workspace/users"
        / user.user_id
        / "generated-images/admin-preview.png"
    )
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(image_bytes)
    updated_message = get_assistant_store().update_message_content(
        assistant_message_id,
        user.user_id,
        "已生成图片：\n\n![管理端预览](me/generated-images/admin-preview.png)",
    )
    assert updated_message is not None

    get_auth_store().update_user_role(user.user_id, "admin")
    admin_session = get_auth_service()._create_admin_session(user.user_id)

    client.cookies.clear()
    unauthenticated_response = await client.get("/api/admin/assistant/sessions")
    assert unauthenticated_response.status_code == 401

    client.cookies.set(settings.admin_session_cookie_name, admin_session.token)
    list_response = await client.get("/api/admin/assistant/sessions")
    assert list_response.status_code == 200
    sessions = list_response.json()
    assert sessions[0]["session_id"] == session_id
    assert sessions[0]["owner"]["email"] == "ops-admin@corp.test"
    assert sessions[0]["has_active_turn"] is False
    assert sessions[0]["message_count"] == 2

    user_search_response = await client.get(
        "/api/admin/assistant/sessions",
        params={"user_id": user.user_id},
    )
    assert user_search_response.status_code == 200
    assert user_search_response.json()[0]["session_id"] == session_id

    no_user_match_response = await client.get(
        "/api/admin/assistant/sessions",
        params={"user_id": "missing-user"},
    )
    assert no_user_match_response.status_code == 200
    assert no_user_match_response.json() == []

    detail_response = await client.get(f"/api/admin/assistant/sessions/{session_id}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["session"]["session_id"] == session_id
    assert [message["role"] for message in detail["messages"]] == ["user", "assistant"]
    assert detail["messages"][0]["content"] == "这是一条需要运维查看的聊天记录"
    assistant_message = detail["messages"][1]
    assert assistant_message["file_artifacts"][0]["display_path"] == "me/generated-images/admin-preview.png"
    assert assistant_message["file_artifacts"][0]["mime_type"] == "image/png"
    artifact_url = assistant_message["file_artifacts"][0]["download_url"]
    assert artifact_url.startswith(f"/api/admin/assistant/sessions/{session_id}/files/")

    image_response = await client.get(artifact_url)
    assert image_response.status_code == 200
    assert image_response.headers["content-type"].startswith("image/png")
    assert image_response.headers["content-disposition"].startswith("inline;")
    assert image_response.content == image_bytes

    running_session = get_assistant_store().create_session(user.user_id, "正在运行的会话")
    running_message = get_assistant_store().append_message(
        running_session.session_id,
        user.user_id,
        "user",
        "请继续处理",
    )
    get_assistant_store().create_turn(
        running_session.session_id,
        user.user_id,
        running_message.message_id,
    )

    running_list_response = await client.get("/api/admin/assistant/sessions")
    assert running_list_response.status_code == 200
    sessions_by_id = {item["session_id"]: item for item in running_list_response.json()}
    assert sessions_by_id[running_session.session_id]["has_active_turn"] is True
    assert sessions_by_id[session_id]["has_active_turn"] is False

    running_detail_response = await client.get(
        f"/api/admin/assistant/sessions/{running_session.session_id}"
    )
    assert running_detail_response.status_code == 200
    assert running_detail_response.json()["session"]["has_active_turn"] is True


@pytest.mark.anyio
async def test_runtime_session_title_replaces_default_session_title(tmp_path: Path) -> None:
    from app.business.assistant.service import AssistantService

    class NamingRuntimeClient:
        provider = "tracking"

        async def create_or_resume_session(self, *, runtime_session_id, working_directory, system_prompt):
            del runtime_session_id, system_prompt
            return RuntimeSession(provider=self.provider, session_id=None, working_directory=working_directory)

        async def send_message_stream(self, *, session, message, metadata):
            del message, metadata
            yield RuntimeEvent(
                "session",
                {
                    "provider": self.provider,
                    "session_id": "runtime-session-1",
                    "title": "优惠券规则梳理",
                },
            )
            yield RuntimeEvent("message", {"content": "已完成。", "session_id": "runtime-session-1"})
            yield RuntimeEvent("complete", {"session_id": "runtime-session-1"})

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=NamingRuntimeClient(),
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
    )
    user = SimpleNamespace(user_id="user-1")

    result = await service.chat(user, None, "请帮我总结优惠券规则")

    assert result.session.title == "优惠券规则梳理"
    assert result.session.runtime_session_id == "runtime-session-1"
    assert store.get_session(result.session.session_id, user.user_id).title == "优惠券规则梳理"


@pytest.mark.anyio
async def test_runtime_session_title_does_not_replace_custom_session_title(tmp_path: Path) -> None:
    from app.business.assistant.service import AssistantService

    class NamingRuntimeClient:
        provider = "tracking"

        async def create_or_resume_session(self, *, runtime_session_id, working_directory, system_prompt):
            del runtime_session_id, system_prompt
            return RuntimeSession(provider=self.provider, session_id=None, working_directory=working_directory)

        async def send_message_stream(self, *, session, message, metadata):
            del message, metadata
            yield RuntimeEvent(
                "session",
                {
                    "provider": self.provider,
                    "session_id": "runtime-session-1",
                    "title": "Runtime 生成标题",
                },
            )
            yield RuntimeEvent("message", {"content": "已完成。", "session_id": "runtime-session-1"})
            yield RuntimeEvent("complete", {"session_id": "runtime-session-1"})

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=NamingRuntimeClient(),
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
    )
    user = SimpleNamespace(user_id="user-1")
    session = service.create_session(user, "手动标题")

    result = await service.chat(user, session.session_id, "请继续分析")

    assert result.session.title == "手动标题"
    assert result.session.runtime_session_id == "runtime-session-1"


@pytest.mark.anyio
async def test_lightweight_model_replaces_new_session_title_and_emits_session_event(tmp_path: Path) -> None:
    from app.business.assistant.service import AssistantService

    class TitleLLMClient:
        async def generate_reply(self, system_prompt, messages):
            assert "会话标题生成器" in system_prompt
            assert messages[0].content == "请帮我梳理优惠券的适用规则和失效条件"
            await asyncio.sleep(0.01)
            return "“优惠券适用与失效规则”"

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=MockAgentRuntimeClient(skills_root=str(tmp_path / "skills")),
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
        lightweight_llm_client=TitleLLMClient(),
    )
    user = SimpleNamespace(user_id="user-1")

    events = [
        event
        async for event in service.stream_chat(
            user,
            None,
            "请帮我梳理优惠券的适用规则和失效条件",
        )
    ]

    session_events = [event for event in events if event["type"] == "session"]
    assert session_events[0]["title"] == "请帮我梳理优惠券的适用规则和失效条件"
    assert session_events[-1]["title"] == "优惠券适用与失效规则"
    session_id = session_events[0]["session_id"]
    assert store.get_session(session_id, user.user_id).title == "优惠券适用与失效规则"
    assert events[-1]["type"] == "complete"


@pytest.mark.anyio
async def test_lightweight_title_failure_keeps_fallback_title_without_failing_turn(tmp_path: Path) -> None:
    from app.business.assistant.service import AssistantService

    class FailingTitleLLMClient:
        async def generate_reply(self, system_prompt, messages):
            del system_prompt, messages
            raise RuntimeError("模型不可用")

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=MockAgentRuntimeClient(skills_root=str(tmp_path / "skills")),
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
        lightweight_llm_client=FailingTitleLLMClient(),
    )
    user = SimpleNamespace(user_id="user-1")

    result = await service.chat(user, None, "梳理活动报名规则")

    assert result.session.title == "梳理活动报名规则"
    assert result.turn.status == "completed"


@pytest.mark.anyio
async def test_lightweight_model_only_names_new_sessions(tmp_path: Path) -> None:
    from app.business.assistant.service import AssistantService

    class CountingTitleLLMClient:
        def __init__(self) -> None:
            self.call_count = 0

        async def generate_reply(self, system_prompt, messages):
            del system_prompt, messages
            self.call_count += 1
            return "模型标题"

    title_client = CountingTitleLLMClient()
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=MockAgentRuntimeClient(skills_root=str(tmp_path / "skills")),
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
        lightweight_llm_client=title_client,
    )
    user = SimpleNamespace(user_id="user-1")
    session = service.create_session(user, "手动标题")

    result = await service.chat(user, session.session_id, "请继续分析")

    assert result.session.title == "手动标题"
    assert title_client.call_count == 0


@pytest.mark.anyio
async def test_lightweight_title_replaces_runtime_generated_title_for_new_session(tmp_path: Path) -> None:
    from app.business.assistant.service import AssistantService

    class RuntimeNamingClient:
        provider = "tracking"

        async def create_or_resume_session(self, *, runtime_session_id, working_directory, system_prompt):
            del runtime_session_id, system_prompt
            return RuntimeSession(provider=self.provider, session_id=None, working_directory=working_directory)

        async def send_message_stream(self, *, session, message, metadata):
            del session, message, metadata
            yield RuntimeEvent(
                "session",
                {
                    "provider": self.provider,
                    "session_id": "runtime-session-1",
                    "title": "Runtime 生成标题",
                },
            )
            yield RuntimeEvent("message", {"content": "已完成。", "session_id": "runtime-session-1"})
            yield RuntimeEvent("complete", {"session_id": "runtime-session-1"})

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    class SlowerTitleLLMClient:
        async def generate_reply(self, system_prompt, messages):
            del system_prompt, messages
            await asyncio.sleep(0.01)
            return "轻量模型生成标题"

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=RuntimeNamingClient(),
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
        lightweight_llm_client=SlowerTitleLLMClient(),
    )
    user = SimpleNamespace(user_id="user-1")

    result = await service.chat(user, None, "请梳理营销规则")

    assert result.session.title == "轻量模型生成标题"


@pytest.mark.anyio
async def test_runtime_generated_image_is_persisted_into_personal_workspace(tmp_path: Path) -> None:
    import base64

    from app.business.assistant.service import AssistantService

    png_bytes = b"\x89PNG\r\n\x1a\nfake-png-body"

    class ImageRuntimeClient:
        provider = "tracking"

        async def create_or_resume_session(self, *, runtime_session_id, working_directory, system_prompt):
            del runtime_session_id, system_prompt
            return RuntimeSession(provider=self.provider, session_id=None, working_directory=working_directory)

        async def send_message_stream(self, *, session, message, metadata):
            del message, metadata
            yield RuntimeEvent("session", {"provider": self.provider, "session_id": "runtime-image"})
            yield RuntimeEvent(
                "image",
                {
                    "data": base64.b64encode(png_bytes).decode(),
                    "source_path": "",
                    "revised_prompt": "白底蓝色猫头 Logo",
                },
            )
            yield RuntimeEvent("complete", {"session_id": "runtime-image"})

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=ImageRuntimeClient(),
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
    )
    user = SimpleNamespace(user_id="user-1")

    result = await service.chat(user, None, "画一张蓝色猫头 Logo")

    assert result.turn.status == "completed"
    assert result.turn.error_message is None
    content = result.assistant_message.content
    assert "![白底蓝色猫头 Logo](me/generated-images/" in content
    assert content.rstrip().endswith(".png)")

    image_files = list((tmp_path / "workspace/users/user-1/generated-images").glob("*.png"))
    assert [path.read_bytes() for path in image_files] == [png_bytes]

    artifacts = store.list_file_artifacts_by_session_id(result.session.session_id)
    assert any(artifact.display_path.startswith("me/generated-images/") for artifact in artifacts)


@pytest.mark.anyio
async def test_runtime_generated_image_prefers_saved_source_path(tmp_path: Path) -> None:
    from app.business.assistant.service import AssistantService

    jpeg_bytes = b"\xff\xd8\xff\xe0fake-jpeg-body"
    source_file = tmp_path / "codex-generated" / "image.jpeg"
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_bytes(jpeg_bytes)

    class ImageRuntimeClient:
        provider = "tracking"

        async def create_or_resume_session(self, *, runtime_session_id, working_directory, system_prompt):
            del runtime_session_id, system_prompt
            return RuntimeSession(provider=self.provider, session_id=None, working_directory=working_directory)

        async def send_message_stream(self, *, session, message, metadata):
            del message, metadata
            yield RuntimeEvent("session", {"provider": self.provider, "session_id": "runtime-image"})
            yield RuntimeEvent(
                "image",
                {
                    "data": "!!!not-base64!!!",
                    "source_path": str(source_file),
                    "revised_prompt": "",
                },
            )
            yield RuntimeEvent("delta", {"text": "已为你生成图片。"})
            yield RuntimeEvent("complete", {"session_id": "runtime-image", "result": "已为你生成图片。"})

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=ImageRuntimeClient(),
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
    )
    user = SimpleNamespace(user_id="user-2")

    result = await service.chat(user, None, "画一张产品图")

    assert result.turn.status == "completed"
    content = result.assistant_message.content
    assert content.startswith("已为你生成图片。")
    assert "![AI 生成图像](me/generated-images/" in content
    assert content.rstrip().endswith(".jpg)")

    image_files = list((tmp_path / "workspace/users/user-2/generated-images").glob("*.jpg"))
    assert [path.read_bytes() for path in image_files] == [jpeg_bytes]


@pytest.mark.anyio
async def test_stream_chat_image_only_turn_completes_instead_of_orphaning(tmp_path: Path) -> None:
    import base64

    from app.business.assistant.service import AssistantService

    png_bytes = b"\x89PNG\r\n\x1a\nimage-only-turn"

    class ImageOnlyRuntimeClient:
        provider = "tracking"

        async def create_or_resume_session(self, *, runtime_session_id, working_directory, system_prompt):
            del runtime_session_id, system_prompt
            return RuntimeSession(provider=self.provider, session_id=None, working_directory=working_directory)

        async def send_message_stream(self, *, session, message, metadata):
            del message, metadata
            yield RuntimeEvent("session", {"provider": self.provider, "session_id": "runtime-image-only"})
            yield RuntimeEvent(
                "image",
                {
                    "data": base64.b64encode(png_bytes).decode(),
                    "source_path": "",
                    "revised_prompt": "产品海报",
                },
            )
            yield RuntimeEvent("complete", {"session_id": "runtime-image-only", "result": ""})

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=ImageOnlyRuntimeClient(),
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
    )
    user = SimpleNamespace(user_id="user-3")

    events = [event async for event in service.stream_chat(user, None, "帮我生成一张产品海报")]

    types = [event["type"] for event in events]
    assert "error" not in types
    assert "complete" in types
    delta_text = "".join(
        str(event.get("delta") or "") for event in events if event["type"] == "delta"
    )
    assert "![产品海报](me/generated-images/" in delta_text
    message_event = next(event for event in events if event["type"] == "message")
    assert "![产品海报](me/generated-images/" in str(message_event["message"]["content"])

    turn_id = next(event["turn_id"] for event in events if event["type"] == "turn_start")
    turns = [turn for turn in store.list_turns(message_event["message"]["session_id"], user.user_id)]
    assert [turn.status for turn in turns if turn.turn_id == turn_id] == ["completed"]


@pytest.mark.anyio
async def test_successful_image_generation_records_one_usage_per_image(tmp_path: Path) -> None:
    import base64
    from datetime import datetime, timezone

    from app.business.assistant.service import AssistantService

    png_bytes = b"\x89PNG\r\n\x1a\nquota-count-body"

    class TwoImageRuntimeClient:
        provider = "tracking"

        async def create_or_resume_session(self, *, runtime_session_id, working_directory, system_prompt):
            del runtime_session_id, system_prompt
            return RuntimeSession(provider=self.provider, session_id=None, working_directory=working_directory)

        async def send_message_stream(self, *, session, message, metadata):
            del session, message, metadata
            yield RuntimeEvent("session", {"provider": self.provider, "session_id": "runtime-two-img"})
            for item_id in ("img-a", "img-b"):
                yield RuntimeEvent(
                    "image",
                    {
                        "data": base64.b64encode(png_bytes).decode(),
                        "source_path": "",
                        "revised_prompt": f"图 {item_id}",
                        "item_id": item_id,
                    },
                )
            yield RuntimeEvent("complete", {"session_id": "runtime-two-img", "result": "两张图好了。"})

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=TwoImageRuntimeClient(),
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
        image_generation_daily_limit=10,
        image_generation_weekly_limit=20,
    )
    user = SimpleNamespace(user_id="user-quota-count")

    result = await service.chat(user, None, "画两张图")

    assert result.turn.status == "completed"
    content = result.assistant_message.content
    assert content.count("](me/generated-images/") == 2
    images = list((tmp_path / "workspace/users/user-quota-count/generated-images").glob("*.png"))
    assert len(images) == 2
    assert (
        store.count_image_generation_usage(
            user_id=user.user_id,
            since=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
        == 2
    )


@pytest.mark.anyio
async def test_image_generation_blocked_when_daily_quota_exhausted(tmp_path: Path) -> None:
    import base64
    from datetime import datetime, timezone

    from app.business.assistant.service import AssistantService

    png_bytes = b"\x89PNG\r\n\x1a\nquota-block-body"

    class ImageRuntimeClient:
        provider = "tracking"

        def __init__(self) -> None:
            self._seq = 0

        async def create_or_resume_session(self, *, runtime_session_id, working_directory, system_prompt):
            del runtime_session_id, system_prompt
            return RuntimeSession(provider=self.provider, session_id=None, working_directory=working_directory)

        async def send_message_stream(self, *, session, message, metadata):
            del session, message, metadata
            self._seq += 1
            yield RuntimeEvent("session", {"provider": self.provider, "session_id": "runtime-quota"})
            yield RuntimeEvent(
                "image",
                {
                    "data": base64.b64encode(png_bytes).decode(),
                    "source_path": "",
                    "revised_prompt": "额度测试图",
                    "item_id": f"img-{self._seq}",
                },
            )
            yield RuntimeEvent("complete", {"session_id": "runtime-quota", "result": ""})

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=ImageRuntimeClient(),
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
        image_generation_daily_limit=1,
        image_generation_weekly_limit=0,
    )
    user = SimpleNamespace(user_id="user-quota-block")
    image_dir = tmp_path / "workspace/users/user-quota-block/generated-images"

    first = await service.chat(user, None, "画一张图")
    assert first.turn.status == "completed"
    assert "![额度测试图](me/generated-images/" in first.assistant_message.content
    assert len(list(image_dir.glob("*.png"))) == 1
    first_notices = [
        event
        for event in store.list_runtime_events(first.turn.turn_id)
        if event.event_type == "activity" and event.payload.get("kind") == "image_quota"
    ]
    assert len(first_notices) == 1
    assert first_notices[0].payload["accepted"] == 1
    assert first_notices[0].payload["blocked"] == 0
    assert "本轮无法继续生成更多图片" in first_notices[0].payload["message"]

    second = await service.chat(user, None, "再画一张图")
    assert second.turn.status == "completed"
    assert "今日生图额度已用完" in second.assistant_message.content
    assert "![额度测试图](me/generated-images/" not in second.assistant_message.content
    # 被拦截的图不落盘、不计数
    assert len(list(image_dir.glob("*.png"))) == 1
    assert (
        store.count_image_generation_usage(
            user_id=user.user_id,
            since=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
        == 1
    )


@pytest.mark.anyio
async def test_partial_image_generation_quota_persists_turn_notice(tmp_path: Path) -> None:
    import base64

    from app.business.assistant.service import AssistantService

    png_bytes = b"\x89PNG\r\n\x1a\npartial-quota-body"

    class TwoImageRuntimeClient:
        provider = "tracking"

        def __init__(self) -> None:
            self.message = ""

        async def create_or_resume_session(self, *, runtime_session_id, working_directory, system_prompt):
            del runtime_session_id, system_prompt
            return RuntimeSession(provider=self.provider, session_id=None, working_directory=working_directory)

        async def send_message_stream(self, *, session, message, metadata):
            del session, metadata
            self.message = message
            yield RuntimeEvent("session", {"provider": self.provider, "session_id": "runtime-partial-quota"})
            for item_id in ("img-first", "img-second"):
                yield RuntimeEvent(
                    "image",
                    {
                        "data": base64.b64encode(png_bytes).decode(),
                        "source_path": "",
                        "revised_prompt": item_id,
                        "item_id": item_id,
                    },
                )
            yield RuntimeEvent(
                "complete",
                {
                    "session_id": "runtime-partial-quota",
                    "result": "已生成两张新风格。",
                },
            )

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    store.record_image_generation_usage(user_id="user-partial-quota", turn_id=None, item_id="existing")
    runtime = TwoImageRuntimeClient()
    service = AssistantService(
        store=store,
        runtime_client=runtime,
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
        image_generation_daily_limit=2,
        image_generation_weekly_limit=0,
    )
    user = SimpleNamespace(user_id="user-partial-quota")

    result = await service.chat(user, None, "再生成两张")

    assert "当前最多还能接收 1 张" in runtime.message
    assert result.assistant_message.content.startswith("已生成两张新风格。")
    assert result.assistant_message.content.count("](me/generated-images/") == 1
    notices = [
        event
        for event in store.list_runtime_events(result.turn.turn_id)
        if event.event_type == "activity" and event.payload.get("kind") == "image_quota"
    ]
    assert len(notices) == 1
    assert notices[0].payload["accepted"] == 1
    assert notices[0].payload["blocked"] == 1
    assert notices[0].payload["used"] == 2
    assert notices[0].payload["limit"] == 2
    assert "本轮已保留 1 张" in notices[0].payload["message"]


def test_image_generation_quota_instruction_is_scoped_to_image_requests(tmp_path: Path) -> None:
    from app.business.assistant.service import AssistantService

    service = AssistantService(
        store=SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3")),
        runtime_client=MockAgentRuntimeClient(),
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
        image_generation_daily_limit=5,
        image_generation_weekly_limit=10,
    )

    assert service._image_generation_quota_instruction("user-1", message="请分析结算逻辑") is None
    assert service._image_generation_quota_instruction("user-1", message="请生成一份报告") is None
    assert service._image_generation_quota_instruction("user-1", message="请生成流程图") is None
    instruction = service._image_generation_quota_instruction("user-1", message="请生成两张图片")
    assert instruction is not None
    assert "当前最多还能接收 5 张" in instruction


@pytest.mark.anyio
async def test_failed_image_generation_is_not_counted(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    from app.business.assistant.service import AssistantService

    class BrokenImageRuntimeClient:
        provider = "tracking"

        async def create_or_resume_session(self, *, runtime_session_id, working_directory, system_prompt):
            del runtime_session_id, system_prompt
            return RuntimeSession(provider=self.provider, session_id=None, working_directory=working_directory)

        async def send_message_stream(self, *, session, message, metadata):
            del session, message, metadata
            yield RuntimeEvent("session", {"provider": self.provider, "session_id": "runtime-bad-img"})
            yield RuntimeEvent(
                "image",
                {"data": "", "source_path": "", "revised_prompt": "坏图", "item_id": "img-bad"},
            )
            yield RuntimeEvent("complete", {"session_id": "runtime-bad-img", "result": "没生成成功。"})

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=BrokenImageRuntimeClient(),
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
        image_generation_daily_limit=10,
    )
    user = SimpleNamespace(user_id="user-quota-fail")

    result = await service.chat(user, None, "画张图")

    assert result.turn.status == "completed"
    assert (
        store.count_image_generation_usage(
            user_id=user.user_id,
            since=datetime(2000, 1, 1, tzinfo=timezone.utc),
        )
        == 0
    )


@pytest.mark.anyio
async def test_record_image_generation_usage_dedups_by_item_id(tmp_path: Path) -> None:
    from datetime import datetime, timezone

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    since = datetime(2000, 1, 1, tzinfo=timezone.utc)

    assert store.record_image_generation_usage(user_id="u1", turn_id="t1", item_id="item-x") is True
    assert store.record_image_generation_usage(user_id="u1", turn_id="t1", item_id="item-x") is False
    assert store.count_image_generation_usage(user_id="u1", since=since) == 1

    # 无 item_id 时无法去重，每次都计入
    assert store.record_image_generation_usage(user_id="u1", turn_id="t2", item_id="") is True
    assert store.record_image_generation_usage(user_id="u1", turn_id="t2", item_id="") is True
    assert store.count_image_generation_usage(user_id="u1", since=since) == 3


@pytest.mark.anyio
async def test_image_generation_usage_summary_by_user_aggregates_per_user(tmp_path: Path) -> None:
    from datetime import datetime, timedelta, timezone

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    epoch = datetime(2000, 1, 1, tzinfo=timezone.utc)
    future = datetime.now(timezone.utc) + timedelta(days=1)

    store.record_image_generation_usage(user_id="u1", turn_id="t1", item_id="a")
    store.record_image_generation_usage(user_id="u1", turn_id="t1", item_id="b")
    store.record_image_generation_usage(user_id="u2", turn_id="t2", item_id="c")

    # 窗口起点在过去：daily/weekly/total 全部计入
    wide = store.image_generation_usage_summary_by_user(day_start=epoch, week_start=epoch)
    assert wide["u1"] == {"total": 2, "daily": 2, "weekly": 2}
    assert wide["u2"] == {"total": 1, "daily": 1, "weekly": 1}

    # 日窗口起点在未来：daily 归零，weekly/total 不受影响（验证条件聚合）
    narrow = store.image_generation_usage_summary_by_user(day_start=future, week_start=epoch)
    assert narrow["u1"] == {"total": 2, "daily": 0, "weekly": 2}
    assert narrow["u2"] == {"total": 1, "daily": 0, "weekly": 1}


@pytest.mark.anyio
async def test_image_generation_quota_endpoint_returns_limits_and_usage(client: httpx.AsyncClient) -> None:
    from app.core.config import settings

    token = await _register_user(client, email="quota-endpoint@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    response = await client.get("/api/assistant/image-generation/quota")

    assert response.status_code == 200
    payload = response.json()
    assert payload["daily"]["used"] == 0
    assert payload["weekly"]["used"] == 0
    assert payload["daily"]["limit"] == settings.image_generation_daily_limit
    assert payload["weekly"]["limit"] == settings.image_generation_weekly_limit
    assert payload["daily"]["resets_at"] is not None


@pytest.mark.anyio
async def test_image_quota_notice_is_returned_in_owner_and_shared_session_timeline(
    client: httpx.AsyncClient,
) -> None:
    token = await _register_user(client, email="quota-timeline@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    chat_response = await client.post("/api/assistant/chat", json={"message": "生成两张图片"})
    assert chat_response.status_code == 200
    chat_payload = chat_response.json()
    session_id = chat_payload["session"]["session_id"]
    turn_id = chat_payload["turn_id"]
    get_assistant_store().append_runtime_event(
        session_id,
        turn_id,
        "activity",
        {
            "message": "生图额度提示：本轮已保留 1 张；另 1 张因达到今日额度上限未加入会话。今日已用 5/5 张。",
            "kind": "image_quota",
            "accepted": 1,
            "blocked": 1,
            "window": "daily",
            "used": 5,
            "limit": 5,
            "resets_at": "2026-07-29T00:00:00+00:00",
        },
    )

    detail_response = await client.get(f"/api/assistant/sessions/{session_id}")
    assert detail_response.status_code == 200
    notices = detail_response.json()["timeline_notices"]
    assert len(notices) == 1
    assert notices[0]["turn_id"] == turn_id
    assert notices[0]["accepted"] == 1
    assert notices[0]["blocked"] == 1

    share_response = await client.put(
        f"/api/assistant/sessions/{session_id}/share",
        json={"share_type": "public", "member_user_ids": []},
        headers={"Origin": "http://localhost:4173"},
    )
    assert share_response.status_code == 200
    share_token = share_response.json()["share_token"]
    client.cookies.clear()

    shared_response = await client.get(f"/api/assistant/shared/{share_token}")
    assert shared_response.status_code == 200
    shared_payload = shared_response.json()
    assert shared_payload["timeline_notices"][0]["turn_id"] == turn_id
    assert shared_payload["messages"][1]["turn_id"] == turn_id




@pytest.mark.anyio
async def test_assistant_image_upload_can_be_used_as_turn_context(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="image@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    owner_headers = {"Authorization": f"Bearer {token}"}

    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
        b"\x90wS\xde"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    upload_response = await client.post(
        "/api/assistant/uploads/images",
        params={"filename": "首页截图-优惠券.png"},
        headers={"Content-Type": "image/png"},
        content=png_bytes,
    )

    assert upload_response.status_code == 201
    upload_payload = upload_response.json()
    assert upload_payload["file_name"] == "首页截图-优惠券.png"
    assert upload_payload["label"] == "图片：首页截图-优惠券.png"
    assert upload_payload["mime_type"] == "image/png"
    assert upload_payload["context_item"]["source_type"] == "image"
    assert upload_payload["context_item"]["metadata"]["original_filename"] == "首页截图-优惠券.png"
    assert upload_payload["context_item"]["source_uri"].startswith("workspace/runtime/")
    assert (Path(settings.ai_working_directory) / upload_payload["source_uri"]).exists()

    runtime = _CapturingWorkspaceRuntime()
    get_assistant_service().runtime_client = runtime

    chat_response = await client.post(
        "/api/assistant/chat",
        json={
            "message": "请分析这张图片",
            "context_items": [upload_payload["context_item"]],
        },
    )

    assert chat_response.status_code == 200
    assert len(runtime.workspace_plans) == 1
    workspace_plan = runtime.workspace_plans[0]
    assert workspace_plan is not None
    attachment_mount = next(
        mount for mount in workspace_plan.mounts if mount.workspace_key == f"attachment:{upload_payload['image_id']}"
    )
    assert attachment_mount.permission == "read"
    assert attachment_mount.sandbox_path == f"/attachments/{upload_payload['image_id']}.png"
    shadow_attachment = Path(workspace_plan.host_shadow_root) / attachment_mount.sandbox_path.removeprefix("/")
    assert shadow_attachment.is_file()
    assert shadow_attachment.read_bytes() == png_bytes
    assert runtime.metadata[0]["local_image_paths"] == [str(shadow_attachment)]
    assert f"`{attachment_mount.sandbox_path}`" in runtime.messages[0]
    assert upload_payload["source_uri"] not in runtime.messages[0]
    session_id = chat_response.json()["session"]["session_id"]
    detail_response = await client.get(f"/api/assistant/sessions/{session_id}")
    user_message = detail_response.json()["messages"][0]
    assert user_message["context_requests"][0]["source_type"] == "image"
    assert user_message["context_requests"][0]["source_uri"] == upload_payload["source_uri"]

    image_response = await client.get(f"/api/assistant/uploads/images/{upload_payload['image_id']}")
    assert image_response.status_code == 200
    assert image_response.headers["content-type"].startswith("image/png")
    assert image_response.headers["content-disposition"].startswith("inline;")
    assert image_response.content == png_bytes

    member_token, member = await _register_user_with_profile(client, "image-share-member@corp.test")
    share_response = await client.put(
        f"/api/assistant/sessions/{session_id}/share",
        json={"share_type": "members", "member_user_ids": [member["user_id"]]},
        headers=owner_headers,
    )
    assert share_response.status_code == 200
    member_image_response = await client.get(
        f"/api/assistant/uploads/images/{upload_payload['image_id']}",
        headers={"Authorization": f"Bearer {member_token}"},
    )
    assert member_image_response.status_code == 200
    assert member_image_response.headers["content-disposition"].startswith("inline;")
    assert member_image_response.content == png_bytes

    admin = get_auth_store().create_email_user(
        "image-admin@corp.test",
        "Image Admin",
        initial_status="active",
        initial_role="admin",
    )
    admin_session = get_auth_service()._create_admin_session(admin.user_id)
    client.cookies.set(settings.admin_session_cookie_name, admin_session.token)

    admin_detail_response = await client.get(f"/api/admin/assistant/sessions/{session_id}")
    assert admin_detail_response.status_code == 200
    admin_user_message = admin_detail_response.json()["messages"][0]
    assert admin_user_message["context_requests"][0]["metadata"]["image_id"] == upload_payload["image_id"]

    admin_image_response = await client.get(f"/api/admin/assistant/uploads/images/{upload_payload['image_id']}")
    assert admin_image_response.status_code == 200
    assert admin_image_response.headers["content-type"].startswith("image/png")
    assert admin_image_response.headers["content-disposition"].startswith("inline;")
    assert admin_image_response.content == png_bytes


@pytest.mark.anyio
async def test_assistant_rejects_image_when_runtime_has_no_native_vision(
    client: httpx.AsyncClient,
) -> None:
    token = await _register_user(client, email="image-no-vision@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    upload_response = await client.post(
        "/api/assistant/uploads/images",
        params={"filename": "接口截图.png"},
        headers={"Content-Type": "image/png"},
        content=(
            b"\x89PNG\r\n\x1a\n"
            b"\x00\x00\x00\rIHDR"
            b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
            b"\x90wS\xde"
            b"\x00\x00\x00\x00IEND\xaeB`\x82"
        ),
    )
    assert upload_response.status_code == 201

    runtime = MockAgentRuntimeClient()
    runtime.supports_local_images = False
    get_assistant_service().runtime_client = runtime
    chat_response = await client.post(
        "/api/assistant/chat",
        json={
            "message": "后端接口是哪个？",
            "context_items": [upload_response.json()["context_item"]],
        },
    )

    assert chat_response.status_code == 502
    assert "不支持原生图片输入" in chat_response.json()["detail"]
    assert "拒绝仅传图片路径" in chat_response.json()["detail"]


@pytest.mark.anyio
async def test_assistant_text_upload_can_be_used_as_turn_context(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="upload-file@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    file_bytes = "# 需求说明\n\n请支持 TXT 和 MD 上传。\n".encode("utf-8")
    upload_response = await client.post(
        "/api/assistant/uploads/files",
        params={"filename": "需求说明.md"},
        headers={"Content-Type": "text/markdown"},
        content=file_bytes,
    )

    assert upload_response.status_code == 201
    upload_payload = upload_response.json()
    assert upload_payload["file_name"] == "需求说明.md"
    assert upload_payload["label"] == "文件：需求说明.md"
    assert upload_payload["mime_type"] == "text/markdown"
    assert upload_payload["source_type"] == "file"
    assert upload_payload["context_item"]["source_type"] == "file"
    assert upload_payload["context_item"]["metadata"]["original_filename"] == "需求说明.md"
    assert upload_payload["context_item"]["source_uri"].startswith("workspace/runtime/")
    assert (Path(settings.ai_working_directory) / upload_payload["source_uri"]).read_bytes() == file_bytes

    runtime = _CapturingWorkspaceRuntime()
    get_assistant_service().runtime_client = runtime

    chat_response = await client.post(
        "/api/assistant/chat",
        json={
            "message": "请总结这个文件",
            "context_items": [upload_payload["context_item"]],
        },
    )

    assert chat_response.status_code == 200
    workspace_plan = runtime.workspace_plans[0]
    assert workspace_plan is not None
    attachment_mount = next(
        mount for mount in workspace_plan.mounts if mount.workspace_key == f"attachment:{upload_payload['file_id']}"
    )
    shadow_attachment = Path(workspace_plan.host_shadow_root) / attachment_mount.sandbox_path.removeprefix("/")
    assert attachment_mount.sandbox_path == f"/attachments/{upload_payload['file_id']}.md"
    assert shadow_attachment.read_bytes() == file_bytes
    assert f"`{attachment_mount.sandbox_path}`" in runtime.messages[0]
    assert upload_payload["source_uri"] not in runtime.messages[0]
    session_id = chat_response.json()["session"]["session_id"]
    detail_response = await client.get(f"/api/assistant/sessions/{session_id}")
    user_message = detail_response.json()["messages"][0]
    assert user_message["context_requests"][0]["source_type"] == "file"
    assert user_message["context_requests"][0]["source_uri"] == upload_payload["source_uri"]


@pytest.mark.anyio
async def test_assistant_markdown_upload_accepts_x_markdown_content_type(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="upload-x-markdown@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    file_bytes = "# 新手任务\n\nAB Test 推送机制。\n".encode("utf-8")
    upload_response = await client.post(
        "/api/assistant/uploads/files",
        params={"filename": "新手任务-AB Test推送机制-20260716174807.md"},
        headers={"Content-Type": "text/x-markdown"},
        content=file_bytes,
    )

    assert upload_response.status_code == 201
    upload_payload = upload_response.json()
    assert upload_payload["file_name"] == "新手任务-AB Test推送机制-20260716174807.md"
    assert upload_payload["mime_type"] == "text/markdown"
    assert (Path(settings.ai_working_directory) / upload_payload["source_uri"]).read_bytes() == file_bytes


@pytest.mark.anyio
async def test_assistant_message_feedback_persists_and_can_be_cleared(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="feedback@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    chat_response = await client.post(
        "/api/assistant/chat",
        json={"message": "请生成一段可评价的回复"},
    )
    chat_payload = chat_response.json()
    session_id = chat_payload["session"]["session_id"]
    assistant_message_id = chat_payload["assistant_message"]["message_id"]
    user_message_id = chat_payload["user_message"]["message_id"]

    feedback_response = await client.put(
        f"/api/assistant/messages/{assistant_message_id}/feedback",
        json={"feedback": "like"},
    )

    assert feedback_response.status_code == 200
    assert feedback_response.json()["feedback"] == "like"

    detail_response = await client.get(f"/api/assistant/sessions/{session_id}")
    assert detail_response.status_code == 200
    assistant_message = next(
        message for message in detail_response.json()["messages"] if message["message_id"] == assistant_message_id
    )
    assert assistant_message["feedback"] == "like"

    with sqlite3.connect(settings.assistant_db_path) as connection:
        feedback_count = connection.execute(
            "SELECT COUNT(*) FROM assistant_message_feedback WHERE message_id = ? AND feedback = 'like'",
            (assistant_message_id,),
        ).fetchone()[0]
    assert feedback_count == 1

    user_message_feedback_response = await client.put(
        f"/api/assistant/messages/{user_message_id}/feedback",
        json={"feedback": "dislike"},
    )
    assert user_message_feedback_response.status_code == 404

    clear_response = await client.put(
        f"/api/assistant/messages/{assistant_message_id}/feedback",
        json={"feedback": None},
    )
    assert clear_response.status_code == 200
    assert clear_response.json()["feedback"] is None

    with sqlite3.connect(settings.assistant_db_path) as connection:
        feedback_count = connection.execute(
            "SELECT COUNT(*) FROM assistant_message_feedback WHERE message_id = ?",
            (assistant_message_id,),
        ).fetchone()[0]
    assert feedback_count == 0


@pytest.mark.anyio
async def test_assistant_chat_can_continue_existing_session(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="followup@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    first_response = await client.post(
        "/api/assistant/chat",
        json={"message": "第一轮问题"},
    )
    session_id = first_response.json()["session"]["session_id"]

    second_response = await client.post(
        "/api/assistant/chat",
        json={"session_id": session_id, "message": "第二轮追问"},
    )

    assert second_response.status_code == 200
    second_payload = second_response.json()
    assert second_payload["session_created"] is False
    assert second_payload["session"]["message_count"] == 4

    list_response = await client.get("/api/assistant/sessions")
    assert list_response.status_code == 200
    sessions = list_response.json()
    assert len(sessions) == 1
    assert sessions[0]["session_id"] == session_id
    assert sessions[0]["message_count"] == 4


@pytest.mark.anyio
async def test_turn_visualization_uses_isolated_runtime_session(tmp_path: Path) -> None:
    from app.business.assistant.service import AssistantService

    class TrackingRuntimeClient:
        provider = "tracking"

        def __init__(self) -> None:
            self.resume_ids: list[str | None] = []
            self.created_ids: list[str] = []

        async def create_or_resume_session(
            self,
            *,
            runtime_session_id,
            working_directory,
            system_prompt,
        ) -> RuntimeSession:
            del system_prompt
            self.resume_ids.append(runtime_session_id)
            session_id = runtime_session_id or f"runtime-session-{len(self.created_ids) + 1}"
            if runtime_session_id is None:
                self.created_ids.append(session_id)
            return RuntimeSession(provider=self.provider, session_id=session_id, working_directory=working_directory)

        async def send_message_stream(
            self,
            *,
            session,
            message,
            metadata,
        ):
            del metadata
            yield RuntimeEvent(
                "session",
                {
                    "provider": self.provider,
                    "session_id": session.session_id,
                    "working_directory": session.working_directory,
                },
            )
            yield RuntimeEvent(
                "message",
                {
                    "content": f"回复来自 {session.session_id}: {message[:12]}",
                    "session_id": session.session_id,
                },
            )

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    runtime_client = TrackingRuntimeClient()
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=runtime_client,
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
    )
    user = SimpleNamespace(user_id="user-1")

    first = await service.chat(user, None, "第一轮普通问题")
    session_id = first.session.session_id
    assert store.get_session(session_id, user.user_id).runtime_session_id == "runtime-session-1"

    request = quote(json.dumps({"scope": "turn", "targetTurnId": first.turn.turn_id}))
    turn_visualization_message = f"<!-- ai-prd-visualization-request:{request} -->\n请可视化这个 turn"
    await service.chat(user, session_id, turn_visualization_message)

    session_after_visualization = store.get_session(session_id, user.user_id)
    assert session_after_visualization.runtime_session_id == "runtime-session-1"

    await service.chat(user, session_id, "第三轮普通追问")

    assert runtime_client.resume_ids == [None, None, "runtime-session-1"]
    assert runtime_client.created_ids == ["runtime-session-1", "runtime-session-2"]


@pytest.mark.anyio
async def test_runtime_tail_error_after_complete_keeps_turn_completed(tmp_path: Path) -> None:
    from app.business.assistant.service import AssistantService

    class TailErrorRuntimeClient:
        provider = "tail-error"

        async def create_or_resume_session(
            self,
            *,
            runtime_session_id,
            working_directory,
            system_prompt,
        ) -> RuntimeSession:
            del system_prompt
            return RuntimeSession(
                provider=self.provider,
                session_id=runtime_session_id or "runtime-session-1",
                working_directory=working_directory,
            )

        async def send_message_stream(
            self,
            *,
            session,
            message,
            metadata,
        ):
            del message, metadata
            yield RuntimeEvent(
                "message",
                {
                    "content": "已经完成的回答",
                    "session_id": session.session_id,
                },
            )
            yield RuntimeEvent(
                "complete",
                {
                    "result": "已经完成的回答",
                    "session_id": session.session_id,
                },
            )
            raise RuntimeError("tail cleanup failed")

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=TailErrorRuntimeClient(),
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
    )
    user = SimpleNamespace(user_id="user-1")

    result = await service.chat(user, None, "会产生尾部异常的问题")
    events = store.list_runtime_events(result.turn.turn_id)

    assert result.turn.status == "completed"
    assert result.assistant_message.content == "已经完成的回答"
    assert any(event.event_type == "error" for event in events)


@pytest.mark.anyio
async def test_manual_retry_after_visualization_output_limit_failure_is_isolated(tmp_path: Path) -> None:
    from app.business.assistant.service import AssistantService

    class TrackingRuntimeClient:
        provider = "tracking"

        def __init__(self) -> None:
            self.resume_ids: list[str | None] = []
            self.created_ids: list[str] = []
            self.seen_messages: list[str] = []

        async def create_or_resume_session(
            self,
            *,
            runtime_session_id,
            working_directory,
            system_prompt,
        ) -> RuntimeSession:
            del system_prompt
            self.resume_ids.append(runtime_session_id)
            session_id = runtime_session_id or f"runtime-session-{len(self.created_ids) + 1}"
            if runtime_session_id is None:
                self.created_ids.append(session_id)
            return RuntimeSession(provider=self.provider, session_id=session_id, working_directory=working_directory)

        async def send_message_stream(
            self,
            *,
            session,
            message,
            metadata,
        ):
            del metadata
            self.seen_messages.append(message)
            yield RuntimeEvent(
                "session",
                {
                    "provider": self.provider,
                    "session_id": session.session_id,
                    "working_directory": session.working_directory,
                },
            )
            yield RuntimeEvent(
                "message",
                {
                    "content": f"回复来自 {session.session_id}",
                    "session_id": session.session_id,
                },
            )

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    runtime_client = TrackingRuntimeClient()
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=runtime_client,
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
    )
    user = SimpleNamespace(user_id="user-1")

    first = await service.chat(user, None, "第一轮普通问题")
    session_id = first.session.session_id
    assert store.get_session(session_id, user.user_id).runtime_session_id == "runtime-session-1"

    request = quote(json.dumps({"scope": "turn", "targetTurnId": first.turn.turn_id}))
    turn_visualization_message = f"<!-- ai-prd-visualization-request:{request} -->\n请可视化这个 turn"
    _failed_user_message, failed_turn = store.start_turn(
        session_id,
        user.user_id,
        turn_visualization_message,
    )
    store.fail_turn(failed_turn.turn_id, "response exceeded the 32000 output token maximum")

    await service.chat(user, session_id, "刚才失败了，再试一次")

    session_after_retry = store.get_session(session_id, user.user_id)
    assert session_after_retry.runtime_session_id == "runtime-session-1"
    assert runtime_client.resume_ids == [None, None]
    assert runtime_client.created_ids == ["runtime-session-1", "runtime-session-2"]
    assert "上一次可视化请求因为模型输出 token 上限失败" in runtime_client.seen_messages[-1]
    assert turn_visualization_message in runtime_client.seen_messages[-1]


@pytest.mark.anyio
async def test_assistant_session_can_be_renamed_and_deleted(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="manage@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    chat_response = await client.post(
        "/api/assistant/chat",
        json={"message": "需要一个会话"},
    )
    session_id = chat_response.json()["session"]["session_id"]

    rename_response = await client.patch(
        f"/api/assistant/sessions/{session_id}",
        json={"title": "营销规则澄清"},
    )
    assert rename_response.status_code == 200
    assert rename_response.json()["title"] == "营销规则澄清"

    delete_response = await client.delete(f"/api/assistant/sessions/{session_id}")
    assert delete_response.status_code == 204

    detail_response = await client.get(f"/api/assistant/sessions/{session_id}")
    assert detail_response.status_code == 404


@pytest.mark.anyio
async def test_assistant_session_share_members_and_comments(client: httpx.AsyncClient) -> None:
    owner_token, _owner = await _register_user_with_profile(client, "share-owner@corp.test")
    member_token, member = await _register_user_with_profile(client, "share-member@corp.test")
    outsider_token, _outsider = await _register_user_with_profile(client, "share-outsider@corp.test")

    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    member_headers = {"Authorization": f"Bearer {member_token}"}
    outsider_headers = {"Authorization": f"Bearer {outsider_token}"}

    chat_response = await client.post(
        "/api/assistant/chat",
        json={"message": "这是一条可共享会话"},
        headers=owner_headers,
    )
    assert chat_response.status_code == 200
    session_id = chat_response.json()["session"]["session_id"]
    user_message_id = chat_response.json()["user_message"]["message_id"]

    share_response = await client.put(
        f"/api/assistant/sessions/{session_id}/share",
        json={"share_type": "members", "member_user_ids": [member["user_id"]]},
        headers={**owner_headers, "Origin": "http://localhost:4173"},
    )
    assert share_response.status_code == 200
    share_payload = share_response.json()
    assert share_payload["share_type"] == "members"
    assert share_payload["members"][0]["user_id"] == member["user_id"]
    assert share_payload["share_url"].startswith("http://localhost:4173/#/shared/")
    share_token = share_payload["share_token"]

    forbidden_response = await client.get(f"/api/assistant/shared/{share_token}", headers=outsider_headers)
    assert forbidden_response.status_code == 404

    shared_response = await client.get(f"/api/assistant/shared/{share_token}", headers=member_headers)
    assert shared_response.status_code == 200
    shared_payload = shared_response.json()
    assert shared_payload["share"]["session"]["session_id"] == session_id
    assert [message["role"] for message in shared_payload["messages"]] == ["user", "assistant"]

    comment_response = await client.post(
        f"/api/assistant/sessions/{session_id}/comments",
        json={"message_id": user_message_id, "content": "这里需要补一条边界说明"},
        headers=member_headers,
    )
    assert comment_response.status_code == 201
    comment_payload = comment_response.json()
    assert comment_payload["message_id"] == user_message_id
    assert comment_payload["can_edit"] is True

    comments_response = await client.get(
        f"/api/assistant/sessions/{session_id}/comments",
        params={"message_id": user_message_id},
        headers=member_headers,
    )
    assert comments_response.status_code == 200
    assert [comment["content"] for comment in comments_response.json()] == ["这里需要补一条边界说明"]

    update_forbidden_response = await client.patch(
        f"/api/assistant/comments/{comment_payload['comment_id']}",
        json={"content": "越权修改"},
        headers=outsider_headers,
    )
    assert update_forbidden_response.status_code == 404

    update_response = await client.patch(
        f"/api/assistant/comments/{comment_payload['comment_id']}",
        json={"content": "这里需要补活动结束边界"},
        headers=member_headers,
    )
    assert update_response.status_code == 200
    assert update_response.json()["content"] == "这里需要补活动结束边界"

    revoke_response = await client.delete(f"/api/assistant/sessions/{session_id}/share", headers=owner_headers)
    assert revoke_response.status_code == 204

    revoked_response = await client.get(f"/api/assistant/shared/{share_token}", headers=member_headers)
    assert revoked_response.status_code == 404


@pytest.mark.anyio
async def test_shared_session_follow_ups_are_serialized_and_visible_to_all_viewers(
    client: httpx.AsyncClient,
) -> None:
    owner_token, owner = await _register_user_with_profile(client, "follow-up-owner@corp.test")
    first_token, first_member = await _register_user_with_profile(client, "follow-up-first@corp.test")
    second_token, second_member = await _register_user_with_profile(client, "follow-up-second@corp.test")
    outsider_token, _outsider = await _register_user_with_profile(client, "follow-up-outsider@corp.test")

    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    first_headers = {"Authorization": f"Bearer {first_token}"}
    second_headers = {"Authorization": f"Bearer {second_token}"}
    outsider_headers = {"Authorization": f"Bearer {outsider_token}"}

    chat_response = await client.post(
        "/api/assistant/chat",
        json={"message": "共享会话原始问题"},
        headers=owner_headers,
    )
    session_id = chat_response.json()["session"]["session_id"]
    share_response = await client.put(
        f"/api/assistant/sessions/{session_id}/share",
        json={
            "share_type": "members",
            "member_user_ids": [first_member["user_id"], second_member["user_id"]],
        },
        headers=owner_headers,
    )
    share_token = share_response.json()["share_token"]

    client.cookies.clear()
    anonymous_response = await client.post(
        f"/api/assistant/shared/{share_token}/follow-ups",
        json={"message": "匿名追问"},
    )
    assert anonymous_response.status_code == 401
    outsider_response = await client.post(
        f"/api/assistant/shared/{share_token}/follow-ups",
        json={"message": "越权追问"},
        headers=outsider_headers,
    )
    assert outsider_response.status_code == 404

    runtime = _BlockingSharedFollowUpRuntime()
    get_assistant_service().runtime_client = runtime

    first_response, second_response = await asyncio.gather(
        client.post(
            f"/api/assistant/shared/{share_token}/follow-ups",
            json={"message": "第一个成员的追问"},
            headers=first_headers,
        ),
        client.post(
            f"/api/assistant/shared/{share_token}/follow-ups",
            json={"message": "第二个成员同时发送的追问"},
            headers=second_headers,
        ),
    )
    assert first_response.status_code == 202
    assert second_response.status_code == 202
    await asyncio.wait_for(runtime.first_started.wait(), timeout=2)

    state_response = await client.get(
        f"/api/assistant/shared/{share_token}",
        headers=second_headers,
    )
    assert state_response.status_code == 200
    state = state_response.json()
    assert state["active_turn"] is not None
    assert [item["status"] for item in state["pending_follow_ups"]] == ["running", "queued"]
    running_follow_up, queued_follow_up = state["pending_follow_ups"]
    assert {
        running_follow_up["requested_by"]["user_id"],
        queued_follow_up["requested_by"]["user_id"],
    } == {first_member["user_id"], second_member["user_id"]}
    assert queued_follow_up["content"] in {
        "第一个成员的追问",
        "第二个成员同时发送的追问",
    }

    owner_state_response = await client.get(
        f"/api/assistant/sessions/{session_id}",
        headers=owner_headers,
    )
    assert owner_state_response.status_code == 200
    owner_state = owner_state_response.json()
    assert owner_state["session"]["has_active_turn"] is True
    assert owner_state["active_turn_requested_by"]["user_id"] == running_follow_up["requested_by"]["user_id"]

    runtime.release_first.set()
    for _ in range(40):
        completed_response = await client.get(
            f"/api/assistant/shared/{share_token}",
            headers=first_headers,
        )
        completed = completed_response.json()
        if len(completed["messages"]) == 6 and not completed["pending_follow_ups"]:
            break
        await asyncio.sleep(0.05)

    assert len(completed["messages"]) == 6
    user_messages = [message for message in completed["messages"] if message["role"] == "user"]
    assert [message["author"]["user_id"] for message in user_messages] == [
        owner["user_id"],
        running_follow_up["requested_by"]["user_id"],
        queued_follow_up["requested_by"]["user_id"],
    ]
    owner_completed_response = await client.get(
        f"/api/assistant/sessions/{session_id}",
        headers=owner_headers,
    )
    assert owner_completed_response.status_code == 200
    owner_user_messages = [
        message for message in owner_completed_response.json()["messages"] if message["role"] == "user"
    ]
    assert [message["author"]["user_id"] for message in owner_user_messages] == [
        owner["user_id"],
        running_follow_up["requested_by"]["user_id"],
        queued_follow_up["requested_by"]["user_id"],
    ]
    follow_up_messages = [
        message for message in user_messages if message["content"] != "共享会话原始问题"
    ]
    assert [message["content"] for message in follow_up_messages] == [
        running_follow_up["content"],
        queued_follow_up["content"],
    ]
    assert [message["author"]["user_id"] for message in follow_up_messages] == [
        running_follow_up["requested_by"]["user_id"],
        queued_follow_up["requested_by"]["user_id"],
    ]
    assert runtime.max_active_count == 1


@pytest.mark.anyio
async def test_shared_follow_up_requires_confirmation_before_rebuilding_missing_runtime(
    client: httpx.AsyncClient,
) -> None:
    app.dependency_overrides[get_notification_service] = lambda: _CapturingNotificationService()
    owner_token, owner = await _register_user_with_profile(client, "rebuild-owner@corp.test")
    requester_token, requester = await _register_user_with_profile(client, "rebuild-requester@corp.test")
    viewer_token, viewer = await _register_user_with_profile(client, "rebuild-viewer@corp.test")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    requester_headers = {"Authorization": f"Bearer {requester_token}"}
    viewer_headers = {"Authorization": f"Bearer {viewer_token}"}

    original_message = "共享会话原始问题"
    follow_up_message = "请结合前面的结论继续分析"
    chat_response = await client.post(
        "/api/assistant/chat",
        json={"message": original_message},
        headers=owner_headers,
    )
    assert chat_response.status_code == 200
    session_id = chat_response.json()["session"]["session_id"]

    store = get_assistant_store()
    session = store.get_session(session_id, owner["user_id"])
    assert session is not None
    legacy_session_id = "8a015441-89e1-4f81-9ab6-bb6b311ecd1a"
    store.update_session_runtime(
        session_id,
        owner["user_id"],
        runtime_provider="codex",
        runtime_session_id=legacy_session_id,
        runtime_working_directory=session.runtime_working_directory,
        active_organization_key=session.active_organization_key,
        runtime_workspace_profile=session.runtime_workspace_profile,
        status="active",
    )

    share_response = await client.put(
        f"/api/assistant/sessions/{session_id}/share",
        json={
            "share_type": "members",
            "member_user_ids": [requester["user_id"], viewer["user_id"]],
        },
        headers=owner_headers,
    )
    share_token = share_response.json()["share_token"]
    runtime = _MissingRolloutThenRebuiltRuntime(legacy_session_id)
    get_assistant_service().runtime_client = runtime

    follow_up_response = await client.post(
        f"/api/assistant/shared/{share_token}/follow-ups",
        json={"message": follow_up_message},
        headers=requester_headers,
    )
    assert follow_up_response.status_code == 202
    request_id = follow_up_response.json()["request_id"]
    await asyncio.wait_for(runtime.missing_rollout_started.wait(), timeout=2)
    queued_message = "排在重建追问后面的另一个问题"
    queued_response = await client.post(
        f"/api/assistant/shared/{share_token}/follow-ups",
        json={"message": queued_message},
        headers=viewer_headers,
    )
    assert queued_response.status_code == 202
    runtime.release_missing_rollout.set()

    failed_state = None
    for _ in range(40):
        state_response = await client.get(
            f"/api/assistant/shared/{share_token}",
            headers=requester_headers,
        )
        candidate = state_response.json()
        if candidate["failed_follow_ups"]:
            failed_state = candidate
            break
        await asyncio.sleep(0.05)

    assert failed_state is not None
    assert failed_state["active_turn"] is None
    assert [item["status"] for item in failed_state["pending_follow_ups"]] == ["queued"]
    assert failed_state["pending_follow_ups"][0]["content"] == queued_message
    failed_follow_up = failed_state["failed_follow_ups"][0]
    assert failed_follow_up["request_id"] == request_id
    assert failed_follow_up["requires_runtime_rebuild"] is True, failed_follow_up
    assert failed_follow_up["can_rebuild_runtime"] is True
    assert "no rollout found for thread id" in failed_follow_up["error_message"]
    assert runtime.rebuild_started.is_set() is False
    assert store.get_session(session_id, owner["user_id"]).runtime_session_id == legacy_session_id

    viewer_state_response = await client.get(
        f"/api/assistant/shared/{share_token}",
        headers=viewer_headers,
    )
    assert viewer_state_response.json()["failed_follow_ups"][0]["can_rebuild_runtime"] is False
    forbidden_rebuild = await client.post(
        f"/api/assistant/shared/{share_token}/follow-ups/{request_id}/rebuild-runtime",
        headers=viewer_headers,
    )
    assert forbidden_rebuild.status_code == 403

    rebuild_response = await client.post(
        f"/api/assistant/shared/{share_token}/follow-ups/{request_id}/rebuild-runtime",
        headers=requester_headers,
    )
    assert rebuild_response.status_code == 202
    assert rebuild_response.json()["status"] == "running"
    await asyncio.wait_for(runtime.rebuild_started.wait(), timeout=2)

    owner_sessions_response = await client.get("/api/assistant/sessions", headers=owner_headers)
    owner_session = next(item for item in owner_sessions_response.json() if item["session_id"] == session_id)
    assert owner_session["has_active_turn"] is True
    shared_list_response = await client.get("/api/assistant/shared", headers=requester_headers)
    shared_session = next(item for item in shared_list_response.json() if item["share_token"] == share_token)
    assert shared_session["session"]["has_active_turn"] is True

    runtime.release_rebuild.set()
    completed_state = None
    for _ in range(40):
        completed_response = await client.get(
            f"/api/assistant/shared/{share_token}",
            headers=requester_headers,
        )
        candidate = completed_response.json()
        if not candidate["active_turn"] and not candidate["pending_follow_ups"]:
            completed_state = candidate
            break
        await asyncio.sleep(0.05)

    assert completed_state is not None
    assert completed_state["failed_follow_ups"] == []
    assert sum(message["content"] == follow_up_message for message in completed_state["messages"]) == 1
    assert sum(message["content"] == queued_message for message in completed_state["messages"]) == 1
    rebuilt_session = store.get_session(session_id, owner["user_id"])
    assert rebuilt_session is not None
    assert rebuilt_session.runtime_session_id not in {None, legacy_session_id}
    assert len(runtime.rehydrated_messages) == 2
    assert "旧 Agent Runtime 已无法恢复" in runtime.rehydrated_messages[0]
    assert original_message in runtime.rehydrated_messages[0]
    assert follow_up_message in runtime.rehydrated_messages[0]


@pytest.mark.anyio
async def test_assistant_members_share_notifies_only_new_members(client: httpx.AsyncClient) -> None:
    owner_token, owner = await _register_user_with_profile(client, "share-notification-owner@corp.test")
    _first_member_token, first_member = await _register_user_with_profile(
        client,
        "share-notification-first@corp.test",
    )
    _second_member_token, second_member = await _register_user_with_profile(
        client,
        "share-notification-second@corp.test",
    )
    notifications = _CapturingNotificationService()
    app.dependency_overrides[get_notification_service] = lambda: notifications

    chat_response = await client.post(
        "/api/assistant/chat",
        json={"message": "需要通知指定成员的会话"},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert chat_response.status_code == 200
    session_id = chat_response.json()["session"]["session_id"]

    share_response = await client.put(
        f"/api/assistant/sessions/{session_id}/share",
        json={"share_type": "members", "member_user_ids": [first_member["user_id"]]},
        headers={"Authorization": f"Bearer {owner_token}", "Origin": "http://localhost:4173"},
    )
    assert share_response.status_code == 200
    share_url = share_response.json()["share_url"]
    assert notifications.shared_sessions == [
        {
            "owner_email": owner["email"],
            "recipient_email": first_member["email"],
            "share_url": share_url,
        }
    ]

    repeat_response = await client.put(
        f"/api/assistant/sessions/{session_id}/share",
        json={"share_type": "members", "member_user_ids": [first_member["user_id"]]},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert repeat_response.status_code == 200
    assert len(notifications.shared_sessions) == 1

    add_member_response = await client.put(
        f"/api/assistant/sessions/{session_id}/share",
        json={
            "share_type": "members",
            "member_user_ids": [first_member["user_id"], second_member["user_id"]],
        },
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert add_member_response.status_code == 200
    assert notifications.shared_sessions[-1]["recipient_email"] == second_member["email"]
    assert len(notifications.shared_sessions) == 2


@pytest.mark.anyio
async def test_shared_session_file_artifacts_use_registered_snapshot_downloads(client: httpx.AsyncClient) -> None:
    owner_token, owner = await _register_user_with_profile(client, "artifact-owner@corp.test")
    member_token, member = await _register_user_with_profile(client, "artifact-member@corp.test")
    outsider_token, _outsider = await _register_user_with_profile(client, "artifact-outsider@corp.test")

    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    member_headers = {"Authorization": f"Bearer {member_token}"}
    outsider_headers = {"Authorization": f"Bearer {outsider_token}"}

    chat_response = await client.post(
        "/api/assistant/chat",
        json={"message": "请生成一份 PDF"},
        headers=owner_headers,
    )
    assert chat_response.status_code == 200
    chat_payload = chat_response.json()
    session_id = chat_payload["session"]["session_id"]
    assistant_message_id = chat_payload["assistant_message"]["message_id"]

    pdf_bytes = b"%PDF-1.4\n% artifact snapshot\n"
    report_path = Path(settings.ai_working_directory) / "workspace/users" / owner["user_id"] / "files/report.pdf"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_bytes(pdf_bytes)
    updated_message = get_assistant_store().update_message_content(
        assistant_message_id,
        owner["user_id"],
        "文件已生成：`me/files/report.pdf`",
    )
    assert updated_message is not None

    share_response = await client.put(
        f"/api/assistant/sessions/{session_id}/share",
        json={"share_type": "members", "member_user_ids": [member["user_id"]]},
        headers=owner_headers,
    )
    assert share_response.status_code == 200
    share_token = share_response.json()["share_token"]

    owner_detail_response = await client.get(f"/api/assistant/sessions/{session_id}", headers=owner_headers)
    assert owner_detail_response.status_code == 200
    owner_assistant_message = owner_detail_response.json()["messages"][1]
    owner_artifact = owner_assistant_message["file_artifacts"][0]
    assert owner_artifact["display_path"] == "me/files/report.pdf"
    assert owner_artifact["mime_type"] == "application/pdf"
    assert owner_artifact["download_url"].startswith(f"/api/assistant/sessions/{session_id}/files/")

    owner_download_response = await client.get(owner_artifact["download_url"], headers=owner_headers)
    assert owner_download_response.status_code == 200
    assert owner_download_response.headers["content-type"].startswith("application/pdf")
    assert owner_download_response.content == pdf_bytes

    shared_detail_response = await client.get(f"/api/assistant/shared/{share_token}", headers=member_headers)
    assert shared_detail_response.status_code == 200
    shared_assistant_message = shared_detail_response.json()["messages"][1]
    shared_artifact = shared_assistant_message["file_artifacts"][0]
    assert shared_artifact["artifact_id"] == owner_artifact["artifact_id"]
    assert shared_artifact["download_url"].startswith(f"/api/assistant/shared/{share_token}/files/")

    shared_download_response = await client.get(shared_artifact["download_url"], headers=member_headers)
    assert shared_download_response.status_code == 200
    assert shared_download_response.headers["content-type"].startswith("application/pdf")
    assert shared_download_response.content == pdf_bytes

    outsider_download_response = await client.get(shared_artifact["download_url"], headers=outsider_headers)
    assert outsider_download_response.status_code == 404

    revoke_response = await client.delete(f"/api/assistant/sessions/{session_id}/share", headers=owner_headers)
    assert revoke_response.status_code == 204
    revoked_download_response = await client.get(shared_artifact["download_url"], headers=member_headers)
    assert revoked_download_response.status_code == 404


@pytest.mark.anyio
async def test_public_shared_sessions_are_listed_for_logged_in_users(client: httpx.AsyncClient) -> None:
    owner_token, _owner = await _register_user_with_profile(client, "public-owner@corp.test")
    viewer_token, _viewer = await _register_user_with_profile(client, "public-viewer@corp.test")

    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    viewer_headers = {"Authorization": f"Bearer {viewer_token}"}

    chat_response = await client.post(
        "/api/assistant/chat",
        json={"message": "公开共享会话"},
        headers=owner_headers,
    )
    session_id = chat_response.json()["session"]["session_id"]
    share_response = await client.put(
        f"/api/assistant/sessions/{session_id}/share",
        json={"share_type": "public", "member_user_ids": []},
        headers=owner_headers,
    )
    assert share_response.status_code == 200
    share_token = share_response.json()["share_token"]

    list_response = await client.get("/api/assistant/shared", headers=viewer_headers)
    assert list_response.status_code == 200
    shared_items = list_response.json()
    assert any(item["share_token"] == share_token for item in shared_items)

    session_comment_response = await client.post(
        f"/api/assistant/sessions/{session_id}/comments",
        json={"content": "整场会话很清楚"},
        headers=viewer_headers,
    )
    assert session_comment_response.status_code == 201

    list_response = await client.get("/api/assistant/shared", headers=viewer_headers)
    assert list_response.status_code == 200
    target_item = next(item for item in list_response.json() if item["share_token"] == share_token)
    assert target_item["comment_count"] == 1


@pytest.mark.anyio
async def test_assistant_members_share_excludes_owner(client: httpx.AsyncClient) -> None:
    owner_token, owner = await _register_user_with_profile(client, "share-self-owner@corp.test")
    member_token, member = await _register_user_with_profile(client, "share-self-member@corp.test")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    member_headers = {"Authorization": f"Bearer {member_token}"}

    chat_response = await client.post(
        "/api/assistant/chat",
        json={"message": "指定成员不应包含自己"},
        headers=owner_headers,
    )
    session_id = chat_response.json()["session"]["session_id"]

    share_response = await client.put(
        f"/api/assistant/sessions/{session_id}/share",
        json={"share_type": "members", "member_user_ids": [owner["user_id"], member["user_id"]]},
        headers=owner_headers,
    )

    assert share_response.status_code == 200
    member_ids = [item["user_id"] for item in share_response.json()["members"]]
    assert member_ids == [member["user_id"]]

    share_token = share_response.json()["share_token"]
    owner_list_response = await client.get("/api/assistant/shared", headers=owner_headers)
    assert owner_list_response.status_code == 200
    owner_share = next(item for item in owner_list_response.json() if item["share_token"] == share_token)
    assert [item["user_id"] for item in owner_share["members"]] == [member["user_id"]]

    member_list_response = await client.get("/api/assistant/shared", headers=member_headers)
    assert member_list_response.status_code == 200
    member_share = next(item for item in member_list_response.json() if item["share_token"] == share_token)
    assert member_share["members"] == []

    shared_response = await client.get(f"/api/assistant/shared/{share_token}", headers=member_headers)
    assert shared_response.status_code == 200


@pytest.mark.anyio
async def test_public_shared_session_is_accessible_anonymously(client: httpx.AsyncClient) -> None:
    owner_email = "anon-share-owner@corp.test"
    owner_token, _owner = await _register_user_with_profile(client, owner_email)
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    chat_response = await client.post(
        "/api/assistant/chat",
        json={"message": "这是一条公开共享会话"},
        headers=owner_headers,
    )
    assert chat_response.status_code == 200
    session_id = chat_response.json()["session"]["session_id"]

    share_response = await client.put(
        f"/api/assistant/sessions/{session_id}/share",
        json={"share_type": "public", "member_user_ids": []},
        headers={**owner_headers, "Origin": "http://localhost:4173"},
    )
    assert share_response.status_code == 200
    share_token = share_response.json()["share_token"]

    client.cookies.clear()

    anon_response = await client.get(f"/api/assistant/shared/{share_token}")
    assert anon_response.status_code == 200
    anon_payload = anon_response.json()
    assert anon_payload["share"]["session"]["session_id"] == session_id
    assert anon_payload["share"]["owner"]["email"] == owner_email
    assert [message["role"] for message in anon_payload["messages"]] == ["user", "assistant"]

    anon_comments_response = await client.get(f"/api/assistant/sessions/{session_id}/comments")
    assert anon_comments_response.status_code == 200

    anon_comment_post = await client.post(
        f"/api/assistant/sessions/{session_id}/comments",
        json={"content": "匿名评论不应被接受"},
    )
    assert anon_comment_post.status_code == 401

    _member_token, member = await _register_user_with_profile(client, "anon-share-member@corp.test")
    downgrade_response = await client.put(
        f"/api/assistant/sessions/{session_id}/share",
        json={"share_type": "members", "member_user_ids": [member["user_id"]]},
        headers={**owner_headers, "Origin": "http://localhost:4173"},
    )
    assert downgrade_response.status_code == 200

    client.cookies.clear()
    anon_after_downgrade = await client.get(f"/api/assistant/shared/{share_token}")
    assert anon_after_downgrade.status_code == 404

    revoke_response = await client.delete(
        f"/api/assistant/sessions/{session_id}/share",
        headers=owner_headers,
    )
    assert revoke_response.status_code == 204

    revoked_response = await client.get(f"/api/assistant/shared/{share_token}")
    assert revoked_response.status_code == 404


@pytest.mark.anyio
async def test_public_share_visibility_controls_viewer_access(client: httpx.AsyncClient) -> None:
    owner_token, _owner = await _register_user_with_profile(client, "visibility-owner@corp.test")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    agent_token, _agent_user = await _register_user_with_profile(client, "visibility-agent@corp.test")
    agent_headers = {"Authorization": f"Bearer {agent_token}"}

    registered_token, registered_user = await _register_user_with_profile(
        client, "visibility-registered@corp.test"
    )
    get_auth_store().update_user_agent_access(registered_user["user_id"], "none")
    registered_headers = {"Authorization": f"Bearer {registered_token}"}

    async def _create_share(visibility: str) -> str:
        chat_response = await client.post(
            "/api/assistant/chat",
            json={"message": f"可见范围 {visibility}"},
            headers=owner_headers,
        )
        assert chat_response.status_code == 200
        session_id = chat_response.json()["session"]["session_id"]
        share_response = await client.put(
            f"/api/assistant/sessions/{session_id}/share",
            json={"share_type": "public", "visibility": visibility, "member_user_ids": []},
            headers=owner_headers,
        )
        assert share_response.status_code == 200
        payload = share_response.json()
        assert payload["visibility"] == visibility
        return payload["share_token"]

    agent_share = await _create_share("agent")
    registered_share = await _create_share("registered")
    anyone_share = await _create_share("anyone")

    # Agent 用户三种范围都能看
    for share_token in (agent_share, registered_share, anyone_share):
        response = await client.get(f"/api/assistant/shared/{share_token}", headers=agent_headers)
        assert response.status_code == 200

    # 无 Agent 权限的注册用户：只能看 registered / anyone
    assert (
        await client.get(f"/api/assistant/shared/{agent_share}", headers=registered_headers)
    ).status_code == 404
    assert (
        await client.get(f"/api/assistant/shared/{registered_share}", headers=registered_headers)
    ).status_code == 200
    assert (
        await client.get(f"/api/assistant/shared/{anyone_share}", headers=registered_headers)
    ).status_code == 200

    # 未登录用户：只能看 anyone
    client.cookies.clear()
    assert (await client.get(f"/api/assistant/shared/{agent_share}")).status_code == 404
    assert (await client.get(f"/api/assistant/shared/{registered_share}")).status_code == 404
    assert (await client.get(f"/api/assistant/shared/{anyone_share}")).status_code == 200


@pytest.mark.anyio
async def test_share_visibility_rejects_invalid_value(client: httpx.AsyncClient) -> None:
    owner_token, _owner = await _register_user_with_profile(client, "visibility-invalid@corp.test")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}

    chat_response = await client.post(
        "/api/assistant/chat",
        json={"message": "校验可见范围"},
        headers=owner_headers,
    )
    session_id = chat_response.json()["session"]["session_id"]

    response = await client.put(
        f"/api/assistant/sessions/{session_id}/share",
        json={"share_type": "public", "visibility": "everyone", "member_user_ids": []},
        headers=owner_headers,
    )
    assert response.status_code == 422


@pytest.mark.anyio
async def test_assistant_sessions_support_query_limit_and_offset(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="search@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    await client.post("/api/assistant/chat", json={"message": "优惠券审批流程需要补哪些规则"})
    await client.post("/api/assistant/chat", json={"message": "库存同步失败后的补偿策略"})
    await client.post("/api/assistant/chat", json={"message": "退款链路怎么补齐状态机"})

    search_response = await client.get("/api/assistant/sessions", params={"query": "库存", "limit": 10, "offset": 0})
    assert search_response.status_code == 200
    search_payload = search_response.json()
    assert len(search_payload) == 1
    assert "库存" in search_payload[0]["title"]

    paged_response = await client.get("/api/assistant/sessions", params={"limit": 1, "offset": 1})
    assert paged_response.status_code == 200
    paged_payload = paged_response.json()
    assert len(paged_payload) == 1
    assert paged_payload[0]["title"] in {
        "优惠券审批流程需要补哪些规则",
        "库存同步失败后的补偿策略",
        "退款链路怎么补齐状态机",
    }


@pytest.mark.anyio
async def test_session_favorites_are_user_specific_and_support_shared_sessions(client: httpx.AsyncClient) -> None:
    owner_token, _owner = await _register_user_with_profile(client, "favorite-owner@corp.test")
    viewer_token, _viewer = await _register_user_with_profile(client, "favorite-viewer@corp.test")
    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    viewer_headers = {"Authorization": f"Bearer {viewer_token}"}

    first_chat = await client.post(
        "/api/assistant/chat",
        json={"message": "需要收藏的活动规则"},
        headers=owner_headers,
    )
    first_session_id = first_chat.json()["session"]["session_id"]
    await client.post(
        "/api/assistant/chat",
        json={"message": "不收藏的退款规则"},
        headers=owner_headers,
    )

    favorite_response = await client.put(
        f"/api/assistant/sessions/{first_session_id}/favorite",
        headers=owner_headers,
    )
    assert favorite_response.status_code == 200
    assert favorite_response.json() == {"session_id": first_session_id, "is_favorited": True}

    favorite_list_response = await client.get(
        "/api/assistant/sessions",
        params={"favorited_only": True},
        headers=owner_headers,
    )
    assert favorite_list_response.status_code == 200
    assert [item["session_id"] for item in favorite_list_response.json()] == [first_session_id]
    assert favorite_list_response.json()[0]["is_favorited"] is True

    forbidden_response = await client.put(
        f"/api/assistant/sessions/{first_session_id}/favorite",
        headers=viewer_headers,
    )
    assert forbidden_response.status_code == 404

    share_response = await client.put(
        f"/api/assistant/sessions/{first_session_id}/share",
        json={"share_type": "public", "member_user_ids": []},
        headers=owner_headers,
    )
    assert share_response.status_code == 200

    viewer_favorite_response = await client.put(
        f"/api/assistant/sessions/{first_session_id}/favorite",
        headers=viewer_headers,
    )
    assert viewer_favorite_response.status_code == 200

    shared_list_response = await client.get("/api/assistant/shared", headers=viewer_headers)
    shared_item = next(
        item for item in shared_list_response.json() if item["session"]["session_id"] == first_session_id
    )
    assert shared_item["session"]["is_favorited"] is True

    unfavorite_response = await client.delete(
        f"/api/assistant/sessions/{first_session_id}/favorite",
        headers=viewer_headers,
    )
    assert unfavorite_response.status_code == 200
    assert unfavorite_response.json()["is_favorited"] is False


@pytest.mark.anyio
async def test_assistant_stream_chat_returns_sse_events(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="stream@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    response = await client.post(
        "/api/assistant/chat/stream",
        json={"message": "请流式回复我"},
    )

    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"

    events = [json.loads(line[6:]) for line in lines[:-1]]
    assert events[0]["type"] == "session"
    assert events[1]["type"] == "turn_start"
    assert any(event["type"] == "activity" for event in events)
    assert any(event["type"] == "tool_use" for event in events)
    assert any(event["type"] == "skill_use" for event in events)
    assert any(event["type"] == "delta" for event in events)
    assert events[-1]["type"] == "complete"

    turn_id = next(event["turn_id"] for event in events if event["type"] == "turn_start")
    with sqlite3.connect(settings.assistant_db_path) as connection:
        persisted_event_types = [
            row[0]
            for row in connection.execute(
                "SELECT event_type FROM assistant_runtime_events WHERE turn_id = ? ORDER BY created_at ASC",
                (turn_id,),
            ).fetchall()
        ]

    assert "delta" not in persisted_event_types
    assert "message" in persisted_event_types
    assert "usage" in persisted_event_types
    assert "complete" in persisted_event_types
    assert len(persisted_event_types) < len(events)


@pytest.mark.anyio
async def test_assistant_session_detail_does_not_load_latest_trace_by_default(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="default-trace@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    chat_response = await client.post(
        "/api/assistant/chat",
        json={"message": "创建一轮普通历史对话"},
    )
    session_id = chat_response.json()["session"]["session_id"]
    real_service = get_assistant_service()

    class NoDefaultTraceService:
        def __getattr__(self, name):
            return getattr(real_service, name)

        def get_latest_session_turn_trace(self, *args, **kwargs):
            raise AssertionError("会话详情默认不应读取 latest trace")

    app.dependency_overrides[get_assistant_service] = lambda: NoDefaultTraceService()

    detail_response = await client.get(f"/api/assistant/sessions/{session_id}")

    assert detail_response.status_code == 200
    detail_payload = detail_response.json()
    assert detail_payload["latest_turn_trace"] is None
    assert detail_payload["latest_turn"]["status"] == "completed"


@pytest.mark.anyio
async def test_assistant_session_detail_exposes_owner_test_data_plan_but_shared_view_does_not(
    client: httpx.AsyncClient,
) -> None:
    token = await _register_user(client, email="test-data-plan@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    chat_response = await client.post(
        "/api/assistant/chat",
        json={"message": "准备增加测试资产"},
    )
    payload = chat_response.json()
    session_id = payload["session"]["session_id"]
    turn_id = payload["turn_id"]
    user_id = get_auth_service().get_current_user(token).user_id
    now = datetime.now(timezone.utc)
    get_assistant_store().create_test_data_plan(
        DataPlanRecord(
            plan_id="tdp_owner_only",
            user_id=user_id,
            session_id=session_id,
            prepared_turn_id=turn_id,
            executed_turn_id=None,
            organization_key="coinex",
            task_name="adjust_user_asset",
            environment="2",
            parameters={"uid": "123", "asset": "USDT", "amount": "100"},
            redacted_parameters={"uid": "123", "asset": "USDT", "amount": "100"},
            risk_level="medium",
            summary="环境 2 执行 adjust_user_asset；uid=123, asset=USDT, amount=100",
            status="prepared",
            upstream_run_id=None,
            upstream_status=None,
            result=None,
            error_message=None,
            created_at=now,
            expires_at=now + timedelta(minutes=10),
            executed_at=None,
            updated_at=now,
        )
    )

    detail_response = await client.get(f"/api/assistant/sessions/{session_id}")
    plan = detail_response.json()["test_data_plans"][0]

    assert plan["plan_id"] == "tdp_owner_only"
    assert plan["can_confirm"] is True
    assert plan["confirmation_phrase"] == "确认执行 tdp_owner_only"
    assert "parameters" not in plan

    share_response = await client.put(
        f"/api/assistant/sessions/{session_id}/share",
        json={"share_type": "public", "member_user_ids": []},
        headers={"Origin": "http://localhost:4173"},
    )
    share_token = share_response.json()["share_token"]
    client.cookies.clear()
    shared_payload = (await client.get(f"/api/assistant/shared/{share_token}")).json()

    assert "test_data_plans" not in shared_payload


@pytest.mark.anyio
async def test_test_data_secret_can_only_be_revealed_once_by_session_owner(
    client: httpx.AsyncClient,
) -> None:
    token = await _register_user(client, email="test-data-secret@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    chat_payload = (
        await client.post("/api/assistant/chat", json={"message": "注册测试用户"})
    ).json()
    session_id = chat_payload["session"]["session_id"]
    turn_id = chat_payload["turn_id"]
    user_id = get_auth_service().get_current_user(token).user_id
    now = datetime.now(timezone.utc)
    get_assistant_store().create_test_data_plan(
        DataPlanRecord(
            plan_id="tdp_secret_once",
            user_id=user_id,
            session_id=session_id,
            prepared_turn_id=turn_id,
            executed_turn_id=turn_id,
            organization_key="coinex",
            task_name="register_user",
            environment="3",
            parameters={"email": "new-user@corp.test"},
            redacted_parameters={"email": "ne***@corp.test"},
            risk_level="medium",
            summary="环境 3 注册用户",
            status="completed",
            upstream_run_id="1200",
            upstream_status="success",
            result={"request_id": 1200, "status": "success"},
            error_message=None,
            created_at=now,
            expires_at=now + timedelta(minutes=10),
            executed_at=now,
            updated_at=now,
            secret={"password": "Tt1!one-time-password"},
        )
    )

    detail = (await client.get(f"/api/assistant/sessions/{session_id}")).json()
    assert detail["test_data_plans"][0]["has_secret"] is True
    assert "secret" not in detail["test_data_plans"][0]

    reveal_path = f"/api/assistant/sessions/{session_id}/test-data-plans/tdp_secret_once/reveal-secret"
    first = await client.post(reveal_path)
    second = await client.post(reveal_path)

    assert first.status_code == 200
    assert first.json()["secret"] == {"password": "Tt1!one-time-password"}
    assert second.status_code == 409


@pytest.mark.anyio
async def test_assistant_service_stream_chat_survives_consumer_cancellation() -> None:
    from app.business.assistant.service import AssistantService

    started = asyncio.Event()
    finished = asyncio.Event()

    class TestAssistantService(AssistantService):
        async def _execute_turn(
            self,
            *,
            user,
            session_id,
            message,
            context_items,
            organization_key=None,
            rerun_turn_id=None,
        ):
            del user, session_id, message, context_items, organization_key, rerun_turn_id
            started.set()
            yield {"type": "session", "session_id": "session-1"}, SimpleNamespace()
            await asyncio.sleep(0.02)
            finished.set()
            yield {"type": "complete", "turn_id": "turn-1"}, SimpleNamespace()

    service = TestAssistantService(
        store=SimpleNamespace(),
        runtime_client=SimpleNamespace(provider="mock"),
        system_prompt="system",
        runtime_working_directory="/tmp",
    )

    stream = service.stream_chat(
        user=SimpleNamespace(user_id="user-1"),
        session_id=None,
        message="长任务",
        context_items=[],
    )

    first_event = await anext(stream)
    assert first_event["type"] == "session"
    await started.wait()

    await stream.aclose()

    await asyncio.wait_for(finished.wait(), timeout=0.2)


@pytest.mark.anyio
async def test_assistant_stop_turn_cancels_running_task_and_allows_next_message(tmp_path: Path) -> None:
    from app.business.assistant.service import AssistantService

    started = asyncio.Event()
    cancelled = asyncio.Event()

    class SlowThenCompleteRuntimeClient:
        provider = "mock"

        def __init__(self) -> None:
            self.calls = 0

        async def create_or_resume_session(self, *, runtime_session_id, working_directory, system_prompt):
            del system_prompt
            return RuntimeSession(
                provider=self.provider,
                session_id=runtime_session_id or "runtime-session-1",
                working_directory=working_directory,
            )

        async def send_message_stream(self, *, session, message, metadata):
            del message, metadata
            self.calls += 1
            yield RuntimeEvent("session", {"provider": self.provider, "session_id": session.session_id})
            if self.calls == 1:
                started.set()
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            else:
                yield RuntimeEvent("message", {"content": "第二个问题已发送。", "session_id": session.session_id})
                yield RuntimeEvent("complete", {"session_id": session.session_id})

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    store = SQLiteAssistantStore(db_path=str(tmp_path / "assistant.sqlite3"))
    runtime_client = SlowThenCompleteRuntimeClient()
    service = AssistantService(
        store=store,
        runtime_client=runtime_client,
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
    )
    user = SimpleNamespace(user_id="user-1")

    stream = service.stream_chat(user=user, session_id=None, message="第一个长问题", context_items=[])
    session_event = await anext(stream)
    turn_start_event = await anext(stream)
    assert session_event["type"] == "session"
    assert turn_start_event["type"] == "turn_start"
    await asyncio.wait_for(started.wait(), timeout=0.2)

    stopped_turn = await service.stop_turn(user, str(turn_start_event["turn_id"]))

    assert stopped_turn.status == "failed"
    assert stopped_turn.error_message == service.stopped_turn_error_message
    await asyncio.wait_for(cancelled.wait(), timeout=0.2)
    assert service.get_active_session_turn(user, str(session_event["session_id"])) is None

    result = await service.chat(
        user=user,
        session_id=str(session_event["session_id"]),
        message="第二个问题",
        context_items=[],
    )

    assert result.assistant_message.content == "第二个问题已发送。"
    await stream.aclose()


@pytest.mark.anyio
async def test_assistant_service_resume_turn_stream_replays_snapshot_and_continues_live_events() -> None:
    from app.business.assistant.service import AssistantService

    started = asyncio.Event()
    release = asyncio.Event()
    turn_id = "turn-reconnect"
    session_id = "session-reconnect"
    now = datetime.now(timezone.utc)

    class FakeStore:
        def __init__(self) -> None:
            self.turn = AssistantTurnRecord(
                turn_id=turn_id,
                session_id=session_id,
                user_message_id="user-message-1",
                assistant_message_id=None,
                started_at=now,
                completed_at=None,
                status="running",
                error_message=None,
            )
            self.session = AssistantSessionRecord(
                session_id=session_id,
                user_id="user-1",
                title="断线恢复",
                created_at=now,
                updated_at=now,
            )
            self.runtime_events: list[AssistantRuntimeEventRecord] = []

        def get_turn(self, value: str):
            return self.turn if value == turn_id else None

        def get_session(self, value: str, user_id: str):
            if value == session_id and user_id == "user-1":
                return self.session
            return None

        def list_runtime_events(self, value: str):
            if value != turn_id:
                return []
            return list(self.runtime_events)

        def list_runtime_events_by_session_id(self, value: str, *, event_type=None):
            return [event for event in self.runtime_events
                    if event.session_id == value and (event_type is None or event.event_type == event_type)]

        def list_answer_citations(self, value: str):
            del value
            return []

        def list_messages(self, session_value: str, user_id: str):
            del session_value, user_id
            return []

    store = FakeStore()

    class TestAssistantService(AssistantService):
        async def _execute_turn(
            self,
            *,
            user,
            session_id,
            message,
            context_items,
            organization_key=None,
            rerun_turn_id=None,
        ):
            del user, session_id, message, context_items, organization_key, rerun_turn_id
            yield {"type": "session", "session_id": store.session.session_id}, SimpleNamespace()
            yield {"type": "turn_start", "turn_id": turn_id, "session_id": store.session.session_id}, SimpleNamespace()

            first_event = AssistantRuntimeEventRecord(
                event_id="runtime-1",
                session_id=store.session.session_id,
                turn_id=turn_id,
                event_type="delta",
                payload={"text": "第一段"},
                created_at=datetime.now(timezone.utc),
            )
            store.runtime_events.append(first_event)
            started.set()
            yield {"type": "delta", "delta": "第一段"}, SimpleNamespace()

            await release.wait()

            second_event = AssistantRuntimeEventRecord(
                event_id="runtime-2",
                session_id=store.session.session_id,
                turn_id=turn_id,
                event_type="delta",
                payload={"text": "第二段"},
                created_at=datetime.now(timezone.utc),
            )
            store.runtime_events.append(second_event)
            store.turn = AssistantTurnRecord(
                turn_id=store.turn.turn_id,
                session_id=store.turn.session_id,
                user_message_id=store.turn.user_message_id,
                assistant_message_id=None,
                started_at=store.turn.started_at,
                completed_at=None,
                status="running",
                error_message=None,
            )
            yield {"type": "delta", "delta": "第二段"}, SimpleNamespace()
            yield {"type": "complete", "turn_id": turn_id, "session_id": store.session.session_id}, SimpleNamespace()

    service = TestAssistantService(
        store=store,
        runtime_client=SimpleNamespace(provider="mock"),
        system_prompt="system",
        runtime_working_directory="/tmp",
    )

    stream = service.stream_chat(
        user=SimpleNamespace(user_id="user-1"),
        session_id=None,
        message="断线恢复",
        context_items=[],
    )

    assert (await anext(stream))["type"] == "session"
    turn_start_event = await anext(stream)
    assert turn_start_event["type"] == "turn_start"
    await started.wait()
    await stream.aclose()

    first_resumed_stream = service.resume_turn_stream(SimpleNamespace(user_id="user-1"), turn_id)
    second_resumed_stream = service.resume_turn_stream(SimpleNamespace(user_id="user-1"), turn_id)
    first_replayed_event, second_replayed_event = await asyncio.gather(
        anext(first_resumed_stream),
        anext(second_resumed_stream),
    )
    assert first_replayed_event == {"type": "delta", "delta": "第一段"}
    assert second_replayed_event == first_replayed_event

    release.set()
    first_live_event, second_live_event = await asyncio.gather(
        anext(first_resumed_stream),
        anext(second_resumed_stream),
    )
    assert first_live_event == {"type": "delta", "delta": "第二段"}
    assert second_live_event == first_live_event

    first_complete_event, second_complete_event = await asyncio.gather(
        anext(first_resumed_stream),
        anext(second_resumed_stream),
    )
    assert first_complete_event["type"] == "complete"
    assert second_complete_event == first_complete_event


@pytest.mark.anyio
async def test_assistant_service_preserves_partial_reply_when_runtime_fails_after_output(tmp_path: Path) -> None:
    from app.business.assistant.service import AssistantService

    class PartialFailureRuntimeClient:
        provider = "claude_code"

        async def create_or_resume_session(
            self,
            *,
            runtime_session_id,
            working_directory,
            system_prompt,
        ) -> RuntimeSession:
            del runtime_session_id, system_prompt
            return RuntimeSession(
                provider=self.provider,
                session_id="runtime-session-1",
                working_directory=working_directory,
            )

        async def send_message_stream(
            self,
            *,
            session,
            message,
            metadata,
        ):
            del message, metadata
            yield RuntimeEvent("delta", {"text": "这段回答应该被保留。", "session_id": session.session_id})
            raise AssistantRuntimeError(
                "Command failed with exit code 1 (exit code: 1)\n"
                "Error output: RuntimeError: 真正的 stderr 原因"
            )

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=PartialFailureRuntimeClient(),
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
    )
    user = SimpleNamespace(user_id="user-1")

    events = []
    async for event in service.stream_chat(
        user=user,
        session_id=None,
        message="请保留部分回答",
        context_items=[],
    ):
        events.append(event)

    session_event = next(event for event in events if event["type"] == "session")
    session_id = session_event["session_id"]
    message_event = next(event for event in events if event["type"] == "message")
    error_event = next(event for event in events if event["type"] == "error")

    assert "真正的 stderr 原因" in error_event["message"]
    assert message_event["message"]["content"] == "这段回答应该被保留。"

    persisted_messages = store.list_messages(session_id, user.user_id)
    assert [message.role for message in persisted_messages] == ["user", "assistant"]
    assert persisted_messages[-1].content == "这段回答应该被保留。"

    persisted_turn = store.list_turns(session_id, user.user_id)[-1]
    assert persisted_turn.status == "failed"
    assert persisted_turn.assistant_message_id == persisted_messages[-1].message_id
    assert "真正的 stderr 原因" in (persisted_turn.error_message or "")


@pytest.mark.anyio
async def test_assistant_failed_turn_can_be_rerun_with_original_message_and_context(tmp_path: Path) -> None:
    from app.business.assistant.service import AssistantService

    class FailsOnceRuntimeClient:
        provider = "mock"

        def __init__(self) -> None:
            self.calls = 0
            self.seen_messages: list[str] = []

        async def create_or_resume_session(
            self,
            *,
            runtime_session_id,
            working_directory,
            system_prompt,
        ) -> RuntimeSession:
            del runtime_session_id, system_prompt
            return RuntimeSession(
                provider=self.provider,
                session_id="runtime-session-1",
                working_directory=working_directory,
            )

        async def send_message_stream(
            self,
            *,
            session,
            message,
            metadata,
        ):
            del metadata
            self.calls += 1
            self.seen_messages.append(message)
            if self.calls == 1:
                raise AssistantRuntimeError("第一次执行失败")
            yield RuntimeEvent("delta", {"text": "重跑成功。", "session_id": session.session_id})
            yield RuntimeEvent("message", {"content": "重跑成功。", "session_id": session.session_id})

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    runtime_client = FailsOnceRuntimeClient()
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=runtime_client,
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
    )
    user = SimpleNamespace(user_id="user-1")
    context_item = AssistantContextInput(
        context_key="reference:coupon",
        label="优惠券规则",
        content="指定参考文档",
        source_type="reference",
        source_uri="knowledge/requirements/coupon.md",
        metadata={"scope": "requirements"},
    )

    failed_events = []
    async for event in service.stream_chat(
        user=user,
        session_id=None,
        message="请总结这份规则",
        context_items=[context_item],
    ):
        failed_events.append(event)

    session_id = next(event["session_id"] for event in failed_events if event["type"] == "session")
    failed_turn_id = next(event["turn_id"] for event in failed_events if event["type"] == "turn_start")
    assert any(event["type"] == "error" and "第一次执行失败" in event["message"] for event in failed_events)

    rerun_events = []
    async for event in service.stream_rerun_turn(user, failed_turn_id):
        rerun_events.append(event)

    rerun_turn_id = next(event["turn_id"] for event in rerun_events if event["type"] == "turn_start")
    assert any(event["type"] == "complete" and event["turn_id"] == rerun_turn_id for event in rerun_events)

    turns = store.list_turns(session_id, user.user_id)
    messages = store.list_messages(session_id, user.user_id)
    assert [turn.status for turn in turns] == ["failed", "completed"]
    assert [message.role for message in messages] == ["user", "assistant"]
    assert turns[0].user_message_id == messages[0].message_id
    assert turns[1].user_message_id == messages[0].message_id
    assert messages[-1].content == "重跑成功。"

    rerun_requests = store.list_turn_context_requests(rerun_turn_id)
    assert len(rerun_requests) == 1
    assert rerun_requests[0].context_key == context_item.context_key
    assert rerun_requests[0].source_uri == context_item.source_uri
    assert "请总结这份规则" in runtime_client.seen_messages[0]
    assert runtime_client.seen_messages[1] == runtime_client.seen_messages[0]


@pytest.mark.anyio
async def test_assistant_turn_rolls_back_workspace_code_changes(tmp_path: Path) -> None:
    from app.business.assistant.service import AssistantService

    def run_git(repo_dir: Path, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout

    repo_dir = tmp_path / "workspace/knowledge/code/repo-a"
    repo_dir.mkdir(parents=True)
    run_git(repo_dir, "init")
    tracked_file = repo_dir / "tracked.txt"
    tracked_file.write_text("original\n", encoding="utf-8")
    run_git(repo_dir, "add", "tracked.txt")
    run_git(repo_dir, "-c", "user.email=test@example.com", "-c", "user.name=Test User", "commit", "-m", "init")

    class MutatingRuntimeClient:
        provider = "mutating"

        async def create_or_resume_session(
            self,
            *,
            runtime_session_id,
            working_directory,
            system_prompt,
        ) -> RuntimeSession:
            del runtime_session_id, system_prompt
            return RuntimeSession(provider=self.provider, session_id="runtime-session-1", working_directory=working_directory)

        async def send_message_stream(
            self,
            *,
            session,
            message,
            metadata,
        ):
            del session, message, metadata
            tracked_file.write_text("modified by agent\n", encoding="utf-8")
            (repo_dir / "generated.txt").write_text("temporary\n", encoding="utf-8")
            yield RuntimeEvent("delta", {"text": "已完成。"})
            yield RuntimeEvent("complete", {"result": "已完成。"})

        async def list_available_skills(self, *, working_directory):
            del working_directory
            return []

    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=MutatingRuntimeClient(),
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
        protected_code_root=str(tmp_path / "workspace/knowledge/code"),
    )
    user = SimpleNamespace(user_id="user-1")

    events = []
    async for event in service.stream_chat(user=user, session_id=None, message="修改代码", context_items=[]):
        events.append(event)

    assert tracked_file.read_text(encoding="utf-8") == "original\n"
    assert (repo_dir / "generated.txt").exists()
    assert run_git(repo_dir, "status", "--porcelain") == "?? generated.txt\n"
    assert any(event.get("type") == "workspace_guard" for event in events)

    turn_id = next(event["turn_id"] for event in events if event["type"] == "turn_start")
    persisted_event_types = [event.event_type for event in store.list_runtime_events(turn_id)]
    assert "workspace_guard" in persisted_event_types


def test_claude_runtime_exception_uses_captured_stderr_details() -> None:
    from app.integrations.agent_runtime.claude_runtime import ClaudeCodeAgentRuntimeClient

    class FakeProcessError(Exception):
        def __init__(self) -> None:
            self.exit_code = 1
            self.stderr = "Check stderr output for details"
            super().__init__(
                "Command failed with exit code 1 (exit code: 1)\n"
                "Error output: Check stderr output for details"
            )

    client = ClaudeCodeAgentRuntimeClient(
        model="test-model",
        max_turns=4,
        permission_mode="default",
        allowed_tools=[],
        cli_path=None,
    )

    error = client._normalize_runtime_exception(
        FakeProcessError(),
        ["RuntimeError: first line", "ValueError: second line"],
    )

    assert "Check stderr output for details" not in str(error)
    assert "RuntimeError: first line" in str(error)
    assert "ValueError: second line" in str(error)


def test_store_can_batch_list_session_knowledge_scope_ids(tmp_path: Path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))

    session_a = store.create_session("user-1", "会话 A")
    session_b = store.create_session("user-1", "会话 B")
    store.create_session("user-2", "其他用户会话")

    user_message_a = store.append_message(session_a.session_id, "user-1", "user", "看看需求和代码")
    turn_a = store.create_turn(session_a.session_id, "user-1", user_message_a.message_id)
    store.record_turn_context_requests(
        turn_a.turn_id,
        [
            SimpleNamespace(
                context_key="knowledge-scope:requirements",
                label="@需求文档",
                content=None,
                source_type="knowledge_scope",
                source_uri="knowledge/requirements",
                metadata={"scope": "requirements"},
            )
        ],
    )
    store.record_turn_context_usage(
        turn_a.turn_id,
        [
            SimpleNamespace(
                context_key="knowledge/code",
                label="@代码",
                content=None,
                source_type="knowledge_scope",
                source_uri="knowledge/code",
                metadata={"scope": "code"},
            )
        ],
    )

    user_message_b = store.append_message(session_b.session_id, "user-1", "user", "看看业务")
    turn_b = store.create_turn(session_b.session_id, "user-1", user_message_b.message_id)
    store.record_turn_context_requests(
        turn_b.turn_id,
        [
            SimpleNamespace(
                context_key="knowledge-scope:business",
                label="@业务文档",
                content=None,
                source_type="knowledge_scope",
                source_uri="knowledge/business-docs",
                metadata={"scope": "business"},
            )
        ],
    )

    result = store.list_sessions_knowledge_scope_ids(
        [session_a.session_id, session_b.session_id, "missing-session"],
        "user-1",
    )

    assert result[session_a.session_id] == ["requirements", "code"]
    assert result[session_b.session_id] == ["business"]
    assert "missing-session" not in result


def test_store_reuses_connection_per_thread_and_configures_sqlite(tmp_path: Path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))

    first_connection = store._connect()
    second_connection = store._connect()

    assert first_connection is second_connection

    journal_mode = first_connection.execute("PRAGMA journal_mode").fetchone()[0]
    busy_timeout = first_connection.execute("PRAGMA busy_timeout").fetchone()[0]
    synchronous = first_connection.execute("PRAGMA synchronous").fetchone()[0]

    assert str(journal_mode).lower() == "wal"
    assert busy_timeout == store.busy_timeout_ms
    assert synchronous == 1

    results: queue.Queue[sqlite3.Connection] = queue.Queue()

    def read_connection_from_other_thread() -> None:
        results.put(store._connect())

    worker = threading.Thread(target=read_connection_from_other_thread)
    worker.start()
    worker.join()

    other_thread_connection = results.get_nowait()
    assert other_thread_connection is not first_connection


@pytest.mark.anyio
async def test_assistant_resume_turn_stream_replays_completed_turn(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="resume@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    chat_response = await client.post(
        "/api/assistant/chat",
        json={"message": "请给我一个可恢复的流式结果"},
    )
    assert chat_response.status_code == 200
    turn_id = chat_response.json()["turn_id"]

    response = await client.get(f"/api/assistant/turns/{turn_id}/stream")

    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"

    events = [json.loads(line[6:]) for line in lines[:-1]]
    assert any(event["type"] == "message" for event in events)
    assert events[-1]["type"] == "complete"

    session_id = chat_response.json()["session"]["session_id"]
    share_response = await client.put(
        f"/api/assistant/sessions/{session_id}/share",
        json={"share_type": "public", "member_user_ids": []},
    )
    assert share_response.status_code == 200
    share_token = share_response.json()["share_token"]

    get_assistant_store().append_runtime_event(
        session_id,
        turn_id,
        "tool_use",
        {"tool_name": "Read", "tool_input": {"file_path": "workspace/private-context.md"}},
    )
    get_assistant_store().append_runtime_event(
        session_id,
        turn_id,
        "read",
        {"path": "workspace/private-context.md"},
    )
    get_assistant_store().append_runtime_event(
        session_id,
        turn_id,
        "skill_use",
        {
            "skill_id": "workspace/private-skill/SKILL.md",
            "skill_name": "private-skill",
            "path": "workspace/private-skill/SKILL.md",
        },
    )

    client.cookies.clear()
    shared_response = await client.get(f"/api/assistant/shared/{share_token}/turns/{turn_id}/stream")

    assert shared_response.status_code == 200
    shared_lines = [line for line in shared_response.text.splitlines() if line.startswith("data: ")]
    shared_events = [json.loads(line[6:]) for line in shared_lines[:-1]]
    assert any(event["type"] == "message" for event in shared_events)
    assert {"type": "tool_use", "tool_name": "Read"} in shared_events
    assert {"type": "activity", "message": "工具正在执行", "activity_kind": "tool_progress"} in shared_events
    assert {"type": "skill_use", "skill_name": "private-skill"} in shared_events
    assert shared_events[-1]["type"] == "complete"
    assert all(
        event["type"]
        in {"delta", "citations", "message", "complete", "error", "activity", "tool_use", "skill_use"}
        for event in shared_events
    )
    serialized_shared_events = json.dumps(shared_events, ensure_ascii=False)
    assert "tool_input" not in serialized_shared_events
    assert "private-context.md" not in serialized_shared_events
    assert "workspace/private-skill" not in serialized_shared_events


@pytest.mark.anyio
async def test_assistant_session_detail_includes_latest_failed_turn_trace(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="failed-trace@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    create_response = await client.post("/api/assistant/sessions", json={"title": "失败会话"})
    assert create_response.status_code == 201
    session_id = create_response.json()["session_id"]

    current_user = get_auth_service().get_current_user(token)
    assert current_user is not None
    store = get_assistant_store()
    user_message = store.append_message(session_id, current_user.user_id, "user", "查看一下目前项目中都用那些风控点")
    turn = store.create_turn(session_id, current_user.user_id, user_message.message_id)
    store.append_runtime_event(
        session_id,
        turn.turn_id,
        "tool_use",
        {"tool_name": "Read", "tool_input": {"file_path": "source/backend/app/business/assistant/service.py"}},
    )
    store.append_runtime_event(
        session_id,
        turn.turn_id,
        "read",
        {"path": "source/backend/app/business/assistant/service.py"},
    )
    store.fail_turn(turn.turn_id, "Command failed with exit code 1")

    detail_response = await client.get(f"/api/assistant/sessions/{session_id}")

    assert detail_response.status_code == 200
    payload = detail_response.json()
    assert payload["active_turn"] is None
    assert payload["latest_turn"]["turn_id"] == turn.turn_id
    assert payload["latest_turn"]["status"] == "failed"
    assert payload["latest_turn_trace"] is None

    trace_response = await client.get(f"/api/assistant/sessions/{session_id}/latest-trace")
    assert trace_response.status_code == 200
    trace_payload = trace_response.json()
    assert trace_payload["turn"]["turn_id"] == turn.turn_id
    assert trace_payload["turn"]["status"] == "failed"
    assert any(event["type"] == "read" for event in trace_payload["runtime_events"])


@pytest.mark.anyio
async def test_assistant_session_detail_includes_active_turn(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="active-turn@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    create_response = await client.post("/api/assistant/sessions", json={"title": "进行中会话"})
    assert create_response.status_code == 201
    session_id = create_response.json()["session_id"]

    current_user = get_auth_service().get_current_user(token)
    assert current_user is not None
    store = get_assistant_store()
    service = get_assistant_service()
    user_message = store.append_message(session_id, current_user.user_id, "user", "继续分析这个会话")
    turn = store.create_turn(session_id, current_user.user_id, user_message.message_id)
    service._register_turn_stream(turn.turn_id)

    detail_response = await client.get(f"/api/assistant/sessions/{session_id}")

    assert detail_response.status_code == 200
    payload = detail_response.json()
    assert payload["active_turn"]["turn_id"] == turn.turn_id
    assert payload["active_turn"]["status"] == "running"
    assert payload["latest_turn"]["turn_id"] == turn.turn_id
    assert payload["latest_turn"]["status"] == "running"
    assert payload["latest_turn_trace"] is None

    trace_response = await client.get(f"/api/assistant/sessions/{session_id}/latest-trace")
    assert trace_response.status_code == 200
    trace_payload = trace_response.json()
    assert trace_payload["turn"]["turn_id"] == turn.turn_id
    assert trace_payload["turn"]["status"] == "running"


@pytest.mark.anyio
async def test_assistant_chat_rejects_when_session_has_active_turn(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="busy-turn@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    create_response = await client.post("/api/assistant/sessions", json={"title": "忙碌会话"})
    assert create_response.status_code == 201
    session_id = create_response.json()["session_id"]

    current_user = get_auth_service().get_current_user(token)
    assert current_user is not None
    store = get_assistant_store()
    service = get_assistant_service()
    user_message = store.append_message(session_id, current_user.user_id, "user", "第一轮还在执行")
    turn = store.create_turn(session_id, current_user.user_id, user_message.message_id)
    service._register_turn_stream(turn.turn_id)

    response = await client.post(
        "/api/assistant/chat",
        json={"session_id": session_id, "message": "第二轮应该等待"},
    )

    assert response.status_code == 409
    assert "仍有回复正在生成" in response.json()["detail"]
    messages = store.list_messages(session_id, current_user.user_id)
    assert [message.content for message in messages] == ["第一轮还在执行"]


@pytest.mark.anyio
async def test_assistant_stream_chat_rejects_when_session_has_active_turn(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="busy-stream@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    create_response = await client.post("/api/assistant/sessions", json={"title": "忙碌流式会话"})
    assert create_response.status_code == 201
    session_id = create_response.json()["session_id"]

    current_user = get_auth_service().get_current_user(token)
    assert current_user is not None
    store = get_assistant_store()
    service = get_assistant_service()
    user_message = store.append_message(session_id, current_user.user_id, "user", "第一轮流式还在执行")
    turn = store.create_turn(session_id, current_user.user_id, user_message.message_id)
    service._register_turn_stream(turn.turn_id)

    response = await client.post(
        "/api/assistant/chat/stream",
        json={"session_id": session_id, "message": "第二轮流式应该等待"},
    )

    assert response.status_code == 409
    assert "仍有回复正在生成" in response.json()["detail"]


@pytest.mark.anyio
async def test_assistant_session_detail_reconciles_orphaned_running_turn(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="orphaned-turn@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    create_response = await client.post("/api/assistant/sessions", json={"title": "僵死进行中会话"})
    assert create_response.status_code == 201
    session_id = create_response.json()["session_id"]

    current_user = get_auth_service().get_current_user(token)
    assert current_user is not None
    store = get_assistant_store()
    user_message = store.append_message(session_id, current_user.user_id, "user", "请继续")
    turn = store.create_turn(session_id, current_user.user_id, user_message.message_id)
    store.append_runtime_event(
        session_id,
        turn.turn_id,
        "activity",
        {"message": "继续读取注册相关的核心文件。", "kind": "status"},
    )

    detail_response = await client.get(f"/api/assistant/sessions/{session_id}")

    assert detail_response.status_code == 200
    payload = detail_response.json()
    assert payload["active_turn"] is None
    assert payload["latest_turn"]["turn_id"] == turn.turn_id
    assert payload["latest_turn"]["status"] == "failed"
    assert payload["latest_turn_trace"] is None

    trace_response = await client.get(f"/api/assistant/sessions/{session_id}/latest-trace")
    assert trace_response.status_code == 200
    trace_payload = trace_response.json()
    assert trace_payload["turn"]["turn_id"] == turn.turn_id
    assert trace_payload["turn"]["status"] == "failed"
    assert "Agent 断开连接" in trace_payload["turn"]["error_message"]
    assert any(
        event["type"] == "activity" and "执行状态已丢失" in event["payload"].get("message", "")
        for event in trace_payload["runtime_events"]
    )


@pytest.mark.anyio
async def test_assistant_resume_turn_stream_marks_orphaned_running_turn_failed(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="orphaned-resume@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    create_response = await client.post("/api/assistant/sessions", json={"title": "断线恢复"})
    assert create_response.status_code == 201
    session_id = create_response.json()["session_id"]

    current_user = get_auth_service().get_current_user(token)
    assert current_user is not None
    store = get_assistant_store()
    user_message = store.append_message(session_id, current_user.user_id, "user", "继续")
    turn = store.create_turn(session_id, current_user.user_id, user_message.message_id)
    store.append_runtime_event(
        session_id,
        turn.turn_id,
        "activity",
        {"message": "继续读取注册相关的核心文件。", "kind": "status"},
    )

    response = await client.get(f"/api/assistant/turns/{turn.turn_id}/stream")

    assert response.status_code == 200
    lines = [line for line in response.text.splitlines() if line.startswith("data: ")]
    assert lines[-1] == "data: [DONE]"
    events = [json.loads(line[6:]) for line in lines[:-1]]
    assert events[-1]["type"] == "error"
    assert "Agent 断开连接" in events[-1]["message"]

    refreshed_turn = store.get_turn(turn.turn_id)
    assert refreshed_turn is not None
    assert refreshed_turn.status == "failed"


@pytest.mark.anyio
async def test_assistant_requires_authentication(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/assistant/sessions")

    assert response.status_code == 401
    assert response.json()["detail"] == "缺少应用会话。"


@pytest.mark.anyio
async def test_assistant_skills_mounts_and_trace_endpoints(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="trace@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    create_response = await client.post("/api/assistant/sessions", json={"title": "Runtime 会话"})
    assert create_response.status_code == 201
    session_id = create_response.json()["session_id"]

    mounts_response = await client.post(
        f"/api/assistant/sessions/{session_id}/mounts",
        json={
            "mounts": [
                {
                    "context_key": "prd:runtime-design",
                    "label": "Runtime 迁移设计",
                    "content": "优先检查 agent runtime 抽象与 SSE 事件",
                    "source_type": "document",
                    "source_uri": "source/docs/architecture/assistant-agent-runtime-migration-design.md",
                }
            ]
        },
    )
    assert mounts_response.status_code == 200
    assert mounts_response.json()[0]["context_key"] == "prd:runtime-design"

    chat_response = await client.post(
        "/api/assistant/chat",
        json={
            "session_id": session_id,
            "message": "请基于设计文档分析当前迁移状态",
            "context_items": [
                {
                    "context_key": "focus:backend",
                    "label": "后端改造重点",
                    "content": "service/store/runtime",
                    "source_type": "note",
                }
            ],
        },
    )
    assert chat_response.status_code == 200
    payload = chat_response.json()
    turn_id = payload["turn_id"]

    skills_response = await client.get("/api/assistant/skills")
    assert skills_response.status_code == 200
    assert any(skill["name"] == "ui-design" for skill in skills_response.json())

    detail_response = await client.get(f"/api/assistant/sessions/{session_id}")
    assert detail_response.status_code == 200
    assert detail_response.json()["mounts"][0]["label"] == "Runtime 迁移设计"

    trace_response = await client.get(f"/api/assistant/sessions/{session_id}/turns/{turn_id}/trace")
    assert trace_response.status_code == 200
    trace_payload = trace_response.json()
    assert trace_payload["turn"]["status"] == "completed"
    assert any(event["type"] == "read" for event in trace_payload["runtime_events"])
    assert trace_payload["context_requests"][0]["context_key"] == "focus:backend"
    assert any(item["source_type"] == "file" for item in trace_payload["context_usage"])
    assert trace_payload["citations"]


@pytest.mark.anyio
async def test_assistant_chat_uses_explicit_knowledge_scope_in_runtime_message(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="scopes@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    response = await client.post(
        "/api/assistant/chat",
        json={
            "message": "请帮我分析体验金活动需求",
            "context_items": [
                {
                    "context_key": "knowledge-scope:requirements",
                    "label": "@需求文档",
                    "source_type": "knowledge_scope",
                    "source_uri": "knowledge/requirements",
                    "metadata": {
                        "scope": "requirements",
                        "explicit": True,
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    content = response.json()["assistant_message"]["content"]
    assert "本轮显式知识范围" in content
    assert "coinex/knowledge/requirements" in content
    assert "/coinex/knowledge/requirements" not in content
    assert "@需求文档" in content
    assert "requirements" in response.json()["session"]["knowledge_scope_ids"]


@pytest.mark.anyio
async def test_assistant_chat_persists_user_reference_and_direct_read_instruction(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="reference@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    source_uri = "workspace/knowledge/requirements/docs/合约交易/25年交易重点优化需求.md"
    response = await client.post(
        "/api/assistant/chat",
        json={
            "message": "帮我总结一下这个文档",
            "context_items": [
                {
                    "context_key": f"at-file:{source_uri}",
                    "label": "@25年交易重点优化需求.md",
                    "source_type": "reference",
                    "source_uri": source_uri,
                    "metadata": {
                        "scope": "requirements",
                    },
                }
            ],
        },
    )

    assert response.status_code == 200
    content = response.json()["assistant_message"]["content"]
    assert "本轮已明确指定具体引用对象" in content
    assert "knowledge/requirements/docs/合约交易/25年交易重点优化需求.md" in content
    assert "不要为了定位这些已指定对象再搜索" in content

    session_id = response.json()["session"]["session_id"]
    detail_response = await client.get(f"/api/assistant/sessions/{session_id}")
    assert detail_response.status_code == 200
    user_message = next(message for message in detail_response.json()["messages"] if message["role"] == "user")
    assert user_message["context_requests"][0]["label"] == "@25年交易重点优化需求.md"
    assert user_message["context_requests"][0]["source_uri"] == source_uri


class _SkillCapturingRuntimeClient:
    provider = "skill-capturing"

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def create_or_resume_session(
        self,
        *,
        runtime_session_id,
        working_directory,
        system_prompt,
    ) -> RuntimeSession:
        del system_prompt
        return RuntimeSession(
            provider=self.provider,
            session_id=runtime_session_id or "runtime-session-skill",
            working_directory=working_directory,
        )

    async def send_message_stream(
        self,
        *,
        session,
        message,
        metadata,
    ):
        del metadata
        self.messages.append(message)
        yield RuntimeEvent(
            "session",
            {
                "provider": self.provider,
                "session_id": session.session_id,
                "working_directory": session.working_directory,
            },
        )
        yield RuntimeEvent(
            "message",
            {
                "content": f"已处理：{message[:12]}",
                "session_id": session.session_id,
            },
        )

    async def list_available_skills(self, *, working_directory):
        del working_directory
        return []


def _write_skill_fixture(tmp_path: Path, relative_path: str) -> None:
    skill_dir = tmp_path / "workspace" / ".claude" / "skills" / relative_path
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: 演示技能\ndescription: 演示用途。\n---\n\n# 演示技能\n",
        encoding="utf-8",
    )


def _build_skill_service(tmp_path: Path, runtime_client) -> "AssistantService":
    from app.business.assistant.service import AssistantService

    return AssistantService(
        store=SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3")),
        runtime_client=runtime_client,
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
    )


def _skill_context_item(skill_id: str, *, name: str | None = None) -> AssistantContextInput:
    # skill_id 为发现根下的相对路径冒号形式（如 demo-skill:SKILL.md），name 是目录名
    display_name = name or skill_id
    return AssistantContextInput(
        context_key=f"at-skill:{skill_id}",
        label=f"@{display_name}",
        source_type="skill",
        metadata={"skill_id": skill_id, "skill_name": display_name},
    )


@pytest.mark.anyio
async def test_skill_context_item_renders_runtime_instruction(tmp_path: Path) -> None:
    _write_skill_fixture(tmp_path, "demo-skill")
    runtime_client = _SkillCapturingRuntimeClient()
    service = _build_skill_service(tmp_path, runtime_client)
    user = SimpleNamespace(user_id="user-1")

    await service.chat(
        user,
        None,
        "帮我按演示技能输出",
        context_items=[_skill_context_item("demo-skill:SKILL.md", name="demo-skill")],
    )

    message = runtime_client.messages[-1]
    assert "本轮指定启用 Skill" in message
    assert ".agents/skills/demo-skill/SKILL.md" in message
    assert "at-skill:demo-skill:SKILL.md" not in message
    assert "本轮指定上下文" not in message


@pytest.mark.anyio
async def test_skill_context_item_mixed_with_reference_and_scope(tmp_path: Path) -> None:
    _write_skill_fixture(tmp_path, "demo-skill")
    runtime_client = _SkillCapturingRuntimeClient()
    service = _build_skill_service(tmp_path, runtime_client)
    user = SimpleNamespace(user_id="user-1")

    await service.chat(
        user,
        None,
        "结合需求文档和演示技能分析",
        context_items=[
            AssistantContextInput(
                context_key="knowledge-scope:requirements",
                label="@需求文档",
                source_type="knowledge_scope",
                source_uri="knowledge/requirements",
                metadata={"scope": "requirements", "explicit": True},
            ),
            AssistantContextInput(
                context_key="at-file:knowledge/requirements/docs/示例.md",
                label="@示例.md",
                source_type="reference",
                source_uri="knowledge/requirements/docs/示例.md",
                metadata={"scope": "requirements"},
            ),
            _skill_context_item("demo-skill:SKILL.md", name="demo-skill"),
        ],
    )

    message = runtime_client.messages[-1]
    assert message.count("本轮指定启用 Skill") == 1
    assert ".agents/skills/demo-skill/SKILL.md" in message
    context_section = message.split("本轮指定上下文：", 1)[1].split("本轮指定启用 Skill：", 1)[0]
    assert "@示例.md" in context_section
    assert "@需求文档" not in context_section
    assert "demo-skill" not in context_section
    direct_section = message.split("本轮已明确指定具体引用对象：", 1)[1]
    assert "knowledge/requirements/docs/示例.md" in direct_section
    assert "at-skill:demo-skill:SKILL.md" not in direct_section
    assert "SKILL.md" not in direct_section
    assert "本轮显式知识范围与知识库目录" in message
    assert "显式点亮" not in message


@pytest.mark.anyio
async def test_unknown_skill_context_item_is_skipped(tmp_path: Path) -> None:
    _write_skill_fixture(tmp_path, "demo-skill")
    runtime_client = _SkillCapturingRuntimeClient()
    service = _build_skill_service(tmp_path, runtime_client)
    user = SimpleNamespace(user_id="user-1")

    result = await service.chat(
        user,
        None,
        "用一个不存在的技能",
        context_items=[_skill_context_item("missing-skill")],
    )

    assert result.assistant_message is not None
    message = runtime_client.messages[-1]
    assert "本轮指定启用 Skill" not in message


@pytest.mark.anyio
async def test_duplicate_skill_context_items_render_once(tmp_path: Path) -> None:
    _write_skill_fixture(tmp_path, "demo-skill")
    runtime_client = _SkillCapturingRuntimeClient()
    service = _build_skill_service(tmp_path, runtime_client)
    user = SimpleNamespace(user_id="user-1")

    await service.chat(
        user,
        None,
        "重复指定同一个技能",
        context_items=[
            _skill_context_item("demo-skill:SKILL.md", name="demo-skill"),
            _skill_context_item("demo-skill:SKILL.md", name="demo-skill"),
        ],
    )

    message = runtime_client.messages[-1]
    assert message.count(".agents/skills/demo-skill/SKILL.md") == 1


@pytest.mark.anyio
async def test_skill_context_item_resolves_by_name_when_id_missing(tmp_path: Path) -> None:
    _write_skill_fixture(tmp_path, "demo-skill")
    runtime_client = _SkillCapturingRuntimeClient()
    service = _build_skill_service(tmp_path, runtime_client)
    user = SimpleNamespace(user_id="user-1")

    await service.chat(
        user,
        None,
        "只有名称的技能引用",
        context_items=[
            AssistantContextInput(
                context_key="note:manual",
                label="@demo-skill",
                source_type="skill",
            )
        ],
    )

    message = runtime_client.messages[-1]
    assert "本轮指定启用 Skill" in message
    assert ".agents/skills/demo-skill/SKILL.md" in message


@pytest.mark.anyio
async def test_assistant_chat_skill_context_item_persists_context_request(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="skill-mention@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    response = await client.post(
        "/api/assistant/chat",
        json={
            "message": "帮我写一个新需求",
            "context_items": [
                {
                    # skill_id 使用列表接口返回的冒号形式（含 SKILL.md），前端原样回传
                    "context_key": "at-skill:prd-writer:SKILL.md",
                    "label": "@prd-writer",
                    "source_type": "skill",
                    "source_uri": "workspace/.claude/skills/prd-writer/SKILL.md",
                    "metadata": {"skill_id": "prd-writer:SKILL.md", "skill_name": "prd-writer"},
                }
            ],
        },
    )

    assert response.status_code == 200

    session_id = response.json()["session"]["session_id"]
    detail_response = await client.get(f"/api/assistant/sessions/{session_id}")
    assert detail_response.status_code == 200
    user_message = next(message for message in detail_response.json()["messages"] if message["role"] == "user")
    skill_requests = [item for item in user_message["context_requests"] if item["source_type"] == "skill"]
    assert [item["label"] for item in skill_requests] == ["@prd-writer"]
    assert skill_requests[0]["metadata"]["skill_id"] == "prd-writer:SKILL.md"


@pytest.mark.anyio
async def test_assistant_skills_endpoint_lists_project_skills(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="skill-list@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    response = await client.get("/api/assistant/skills")

    assert response.status_code == 200
    payload = response.json()
    assert payload
    assert all({"skill_id", "name", "description", "path"} <= set(item) for item in payload)
    # fixture 将 settings.skills_root 指向仓库根 .claude/skills，ui-design 是其中的固定 skill；
    # skill_id 为发现根下的相对路径冒号形式（含 SKILL.md），前端原样回传用于精确匹配
    ui_design = next(item for item in payload if item["name"] == "ui-design")
    assert ui_design["skill_id"] == "ui-design:SKILL.md"
    assert ui_design["path"].endswith(".claude/skills/ui-design/SKILL.md")


@pytest.mark.anyio
async def test_assistant_chat_infers_knowledge_scope_from_message_when_not_selected(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="infer@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    response = await client.post(
        "/api/assistant/chat",
        json={
            "message": "请分析 coupon_service.py 的实现影响和回归风险",
        },
    )

    assert response.status_code == 200
    content = response.json()["assistant_message"]["content"]
    assert "本轮自动推断的优先知识范围" in content
    assert "@代码范围" in content
    assert "coinex/knowledge/code" in content
    assert "/coinex/knowledge/code" not in content
    assert "code" in response.json()["session"]["knowledge_scope_ids"]
