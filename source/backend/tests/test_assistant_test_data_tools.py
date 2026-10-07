from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from app.business.assistant.store import SQLiteAssistantStore
from app.business.assistant.service import AssistantService
from app.business.assistant.test_data_tools import AssistantTestDataTools
from app.integrations.agent_runtime import RuntimeEvent, RuntimeSession
from app.integrations.test_data_platform import TestDataPlatformError as PlatformError
from app.services.auth_models import UserRecord


class FakeTestDataClient:
    def __init__(self) -> None:
        self.execute_calls: list[dict[str, object]] = []
        self.query_calls: list[dict[str, object]] = []
        self.execute_error: PlatformError | None = None

    async def list_tasks(self) -> object:
        return {
            "tasks": [
                {
                    "name": "adjust_user_asset",
                    "fields": [
                        {"name": "env", "required": True, "default": "1", "type": "text"},
                        {"name": "identifier", "required": True, "default": None, "type": "text"},
                        {"name": "account", "required": False, "default": "0", "type": "text"},
                        {"name": "asset", "required": True, "default": None, "type": "text"},
                        {"name": "amount", "required": True, "default": None, "type": "text"},
                    ],
                },
                {
                    "name": "update_kyc_status",
                    "fields": [
                        {"name": "env", "required": True, "default": "1", "type": "text"},
                        {"name": "identifier", "required": True, "default": None, "type": "text"},
                        {"name": "level", "required": True, "default": "basic", "type": "text"},
                        {"name": "action", "required": True, "default": "save", "type": "text"},
                        {"name": "status", "required": True, "default": "passed", "type": "text"},
                    ],
                },
                {
                    "name": "register_user",
                    "fields": [
                        {"name": "env", "required": True, "default": "1", "type": "text"},
                        {"name": "email", "required": True, "default": None, "type": "text"},
                        {"name": "password", "required": True, "default": None, "type": "password"},
                        {"name": "device", "required": False, "default": "makedata-register-user", "type": "text"},
                    ],
                },
                {
                    "name": "user_2fa_operation",
                    "fields": [
                        {"name": "env", "required": True, "default": "1", "type": "text"},
                        {"name": "identifier", "required": True, "default": None, "type": "text"},
                        {"name": "action", "required": True, "default": None, "type": "text"},
                        {"name": "totp_key", "required": False, "default": None, "type": "text"},
                        {"name": "new_email", "required": False, "default": None, "type": "text"},
                        {"name": "country_code", "required": False, "default": None, "type": "text"},
                        {"name": "mobile", "required": False, "default": None, "type": "text"},
                        {"name": "credential_id", "required": False, "default": None, "type": "text"},
                    ],
                },
                {
                    "name": "query_user_assets",
                    "kind": "query",
                    "fields": [
                        {"name": "env", "required": False, "default": "1", "type": "text"},
                        {"name": "identifier", "required": True, "default": None, "type": "text"},
                        {"name": "account", "required": False, "default": "all", "type": "text"},
                        {"name": "exclude_subaccounts", "required": False, "default": True, "type": "boolean"},
                    ],
                },
                {"name": "track_market_price"},
            ]
        }

    async def execute(self, *, task: str, environment: str, parameters: dict) -> object:
        self.execute_calls.append(
            {"task": task, "environment": environment, "parameters": parameters}
        )
        if self.execute_error is not None:
            raise self.execute_error
        return {"run_id": 42, "status": "submitted", "email": "user@example.com"}

    async def query(self, *, operation: str, environment: str, parameters: dict) -> object:
        self.query_calls.append(
            {"operation": operation, "environment": environment, "parameters": parameters}
        )
        return {"status": "success", "assets": [{"asset": "USDT", "available": "100"}]}

    async def get_request(self, run_id: str) -> object:
        return {"run_id": run_id, "status": "completed", "result": {"success": True}}


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
        yield RuntimeEvent("message", {"content": "已连接造数工具"})
        yield RuntimeEvent("complete", {"result": "已连接造数工具"})


def _user(*, role: str = "admin") -> UserRecord:
    now = datetime.now(timezone.utc)
    return UserRecord(
        user_id="user-1",
        email="admin@corp.test",
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


def _tool_service(store: SQLiteAssistantStore, client: FakeTestDataClient) -> AssistantTestDataTools:
    return AssistantTestDataTools(
        store=store,
        client=client,
        workspace_access=None,
        allowed_organizations=["coinex"],
        allowed_environments=["1", "2"],
        allowed_tasks=[
            "adjust_user_asset",
            "update_kyc_status",
            "register_user",
            "user_2fa_operation",
            "query_user_assets",
        ],
        admin_only=True,
    )


def _tool(tools, name: str):
    return next(item for item in tools if item.name == name)


@pytest.mark.anyio
async def test_prepare_requires_later_exact_confirmation_and_executes_once(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    client = FakeTestDataClient()
    service = _tool_service(store, client)
    session = store.create_session("user-1", "造数", active_organization_key="coinex")
    _, prepare_turn = store.start_turn(session.session_id, "user-1", "给用户增加资产")
    prepare_tools = service.build_tools(
        user=_user(),
        session_id=session.session_id,
        turn_id=prepare_turn.turn_id,
        organization_key="coinex",
        user_message="给用户增加资产",
    )

    prepared = await _tool(prepare_tools, "prepare_test_data").handler(
        {
            "task": "adjust_user_asset",
            "environment": "2",
            "parameters": {
                "identifier": "123",
                "asset": "USDT",
                "amount": "100",
            },
        }
    )
    prepared_payload = json.loads(prepared.content)
    plan_id = prepared_payload["plan_id"]

    assert prepared.is_error is False
    assert prepared_payload["confirmation_phrase"] == f"确认执行 {plan_id}"
    assert "identifier=123" in prepared_payload["summary"]
    stored = store.get_test_data_plan(
        plan_id=plan_id,
        user_id="user-1",
        session_id=session.session_id,
    )
    assert stored is not None
    assert stored.parameters["identifier"] == "123"
    assert stored.redacted_parameters["identifier"] == "123"

    missing_confirmation = await _tool(prepare_tools, "execute_test_data").handler(
        {"plan_id": plan_id}
    )
    assert missing_confirmation.is_error is True
    assert client.execute_calls == []

    negated_tools = service.build_tools(
        user=_user(),
        session_id=session.session_id,
        turn_id=prepare_turn.turn_id,
        organization_key="coinex",
        user_message=f"不确认执行 {plan_id}",
    )
    negated_confirmation = await _tool(negated_tools, "execute_test_data").handler(
        {"plan_id": plan_id}
    )
    assert negated_confirmation.is_error is True
    assert client.execute_calls == []

    store.fail_turn(prepare_turn.turn_id, "test turn finished")
    confirmation = f"确认执行 {plan_id}"
    _, execute_turn = store.start_turn(session.session_id, "user-1", confirmation)
    execute_tools = service.build_tools(
        user=_user(),
        session_id=session.session_id,
        turn_id=execute_turn.turn_id,
        organization_key="coinex",
        user_message=confirmation,
    )
    execute_tool = _tool(execute_tools, "execute_test_data")

    first = await execute_tool.handler({"plan_id": plan_id})
    second = await execute_tool.handler({"plan_id": plan_id})

    assert first.is_error is False
    assert json.loads(first.content)["status"] == "submitted"
    assert second.is_error is False
    assert len(client.execute_calls) == 1
    assert client.execute_calls[0] == {
        "task": "adjust_user_asset",
        "environment": "2",
        "parameters": {
            "identifier": "123",
            "asset": "USDT",
            "amount": "100",
        },
    }

    status = await _tool(execute_tools, "get_test_data_request").handler(
        {"plan_id": plan_id}
    )
    status_payload = json.loads(status.content)
    assert status_payload["status"] == "completed"
    assert status_payload["run_id"] == "42"
    assert status_payload["result"]["result"]["success"] is True


@pytest.mark.anyio
async def test_query_uses_new_readonly_endpoint_without_confirmation(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    client = FakeTestDataClient()
    service = _tool_service(store, client)
    session = store.create_session("user-1", "查询资产", active_organization_key="coinex")
    _, turn = store.start_turn(session.session_id, "user-1", "查询用户资产")
    query = _tool(
        service.build_tools(
            user=_user(),
            session_id=session.session_id,
            turn_id=turn.turn_id,
            organization_key="coinex",
            user_message="查询用户资产",
        ),
        "query_test_data",
    )

    result = await query.handler(
        {
            "operation": "query_user_assets",
            "environment": "2",
            "parameters": {"identifier": "123"},
        }
    )

    payload = json.loads(result.content)
    assert result.is_error is False
    assert payload["operation"] == "query_user_assets"
    assert payload["result"]["status"] == "success"
    assert client.execute_calls == []
    assert client.query_calls == [
        {
            "operation": "query_user_assets",
            "environment": "2",
            "parameters": {"identifier": "123"},
        }
    ]

    prepare = _tool(
        service.build_tools(
            user=_user(),
            session_id=session.session_id,
            turn_id=turn.turn_id,
            organization_key="coinex",
            user_message="查询用户资产",
        ),
        "prepare_test_data",
    )
    prepare_result = await prepare.handler(
        {
            "task": "query_user_assets",
            "environment": "2",
            "parameters": {"identifier": "123"},
        }
    )
    assert prepare_result.is_error is True


@pytest.mark.anyio
async def test_prepare_blocks_unapproved_environment_task_and_sensitive_parameters(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = _tool_service(store, FakeTestDataClient())
    session = store.create_session("user-1", "造数", active_organization_key="coinex")
    _, turn = store.start_turn(session.session_id, "user-1", "造数")
    prepare = _tool(
        service.build_tools(
            user=_user(),
            session_id=session.session_id,
            turn_id=turn.turn_id,
            organization_key="coinex",
            user_message="造数",
        ),
        "prepare_test_data",
    )

    base_parameters = {"identifier": "1", "asset": "USDT", "amount": "10"}
    invalid_environment = await prepare.handler(
        {"task": "adjust_user_asset", "environment": "99", "parameters": base_parameters}
    )
    invalid_task = await prepare.handler(
        {"task": "track_market_price", "environment": "1", "parameters": {}}
    )
    blocked_network = await prepare.handler(
        {
            "task": "adjust_user_asset",
            "environment": "1",
            "parameters": {**base_parameters, "custom_ip": "127.0.0.1"},
        }
    )
    blocked_secret = await prepare.handler(
        {
            "task": "adjust_user_asset",
            "environment": "1",
            "parameters": {**base_parameters, "password": "secret"},
        }
    )
    missing_required = await prepare.handler(
        {
            "task": "adjust_user_asset",
            "environment": "1",
            "parameters": {"identifier": "1", "asset": "USDT"},
        }
    )
    unknown_parameter = await prepare.handler(
        {
            "task": "adjust_user_asset",
            "environment": "1",
            "parameters": {**base_parameters, "unexpected": "value"},
        }
    )

    assert all(
        result.is_error
        for result in (
            invalid_environment,
            invalid_task,
            blocked_network,
            blocked_secret,
            missing_required,
            unknown_parameter,
        )
    )


@pytest.mark.anyio
async def test_register_user_uses_server_generated_one_time_password(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    client = FakeTestDataClient()
    service = _tool_service(store, client)
    session = store.create_session("user-1", "注册测试用户", active_organization_key="coinex")
    _, prepare_turn = store.start_turn(session.session_id, "user-1", "注册一个测试用户")
    tools = service.build_tools(
        user=_user(),
        session_id=session.session_id,
        turn_id=prepare_turn.turn_id,
        organization_key="coinex",
        user_message="注册一个测试用户",
    )

    prepared = await _tool(tools, "prepare_test_data").handler(
        {
            "task": "register_user",
            "environment": "1",
            "parameters": {"email": "new-user@corp.test"},
        }
    )
    plan_id = json.loads(prepared.content)["plan_id"]
    plan = store.get_test_data_plan(
        plan_id=plan_id,
        user_id="user-1",
        session_id=session.session_id,
    )

    assert prepared.is_error is False
    assert plan is not None
    assert "password" not in plan.parameters
    assert str(plan.secret["password"]).startswith("Tt1!")

    store.fail_turn(prepare_turn.turn_id, "prepared")
    confirmation = f"确认执行 {plan_id}"
    _, execute_turn = store.start_turn(session.session_id, "user-1", confirmation)
    execute = _tool(
        service.build_tools(
            user=_user(),
            session_id=session.session_id,
            turn_id=execute_turn.turn_id,
            organization_key="coinex",
            user_message=confirmation,
        ),
        "execute_test_data",
    )
    result = await execute.handler({"plan_id": plan_id})
    status_result = await _tool(
        service.build_tools(
            user=_user(),
            session_id=session.session_id,
            turn_id=execute_turn.turn_id,
            organization_key="coinex",
            user_message=confirmation,
        ),
        "get_test_data_request",
    ).handler({"plan_id": plan_id})

    assert result.is_error is False
    assert json.loads(status_result.content)["status"] == "completed"
    assert client.execute_calls[0]["parameters"]["password"] == plan.secret["password"]
    first_secret = store.consume_test_data_plan_secret(
        plan_id=plan_id,
        user_id="user-1",
        session_id=session.session_id,
    )
    second_secret = store.consume_test_data_plan_secret(
        plan_id=plan_id,
        user_id="user-1",
        session_id=session.session_id,
    )
    assert first_secret == {"password": plan.secret["password"]}
    assert second_secret is None


@pytest.mark.anyio
async def test_two_fa_actions_validate_inputs_and_keep_secrets_out_of_tool_result(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = _tool_service(store, FakeTestDataClient())
    session = store.create_session("user-1", "2FA", active_organization_key="coinex")
    _, turn = store.start_turn(session.session_id, "user-1", "调整 2FA")
    prepare = _tool(
        service.build_tools(
            user=_user(),
            session_id=session.session_id,
            turn_id=turn.turn_id,
            organization_key="coinex",
            user_message="调整 2FA",
        ),
        "prepare_test_data",
    )

    query = await prepare.handler(
        {
            "task": "user_2fa_operation",
            "environment": "1",
            "parameters": {"identifier": "user@corp.test", "action": "query_sms"},
        }
    )
    missing_mobile = await prepare.handler(
        {
            "task": "user_2fa_operation",
            "environment": "1",
            "parameters": {"identifier": "user@corp.test", "action": "bind_mobile"},
        }
    )
    supplied_totp = await prepare.handler(
        {
            "task": "user_2fa_operation",
            "environment": "1",
            "parameters": {
                "identifier": "user@corp.test",
                "action": "bind_totp",
                "totp_key": "secret-key",
            },
        }
    )
    generated_totp = await prepare.handler(
        {
            "task": "user_2fa_operation",
            "environment": "1",
            "parameters": {"identifier": "user@corp.test", "action": "bind_totp"},
        }
    )
    raw_response = {
        "request_id": 9,
        "status": "success",
        "stdout": '{"email":"user@corp.test","totp_key":"ABC123"}\n',
    }
    secret = service._extract_response_secret("user_2fa_operation", raw_response)
    sanitized = service._sanitize_platform_response("user_2fa_operation", raw_response)

    assert query.is_error is False
    assert missing_mobile.is_error is True
    assert supplied_totp.is_error is True
    assert generated_totp.is_error is False
    assert json.loads(generated_totp.content)["risk_level"] == "high"
    assert secret == {"totp_key": "ABC123"}
    assert "ABC123" not in json.dumps(sanitized, ensure_ascii=False)


@pytest.mark.anyio
async def test_ambiguous_execute_failure_is_not_retried(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    client = FakeTestDataClient()
    client.execute_error = PlatformError(
        "timed out",
        execution_state_unknown=True,
    )
    service = _tool_service(store, client)
    session = store.create_session("user-1", "造数", active_organization_key="coinex")
    _, prepare_turn = store.start_turn(session.session_id, "user-1", "造数")
    tools = service.build_tools(
        user=_user(),
        session_id=session.session_id,
        turn_id=prepare_turn.turn_id,
        organization_key="coinex",
        user_message="造数",
    )
    prepared = await _tool(tools, "prepare_test_data").handler(
        {
            "task": "adjust_user_asset",
            "environment": "1",
            "parameters": {"identifier": "1", "asset": "USDT", "amount": 10},
        }
    )
    plan_id = json.loads(prepared.content)["plan_id"]
    store.fail_turn(prepare_turn.turn_id, "test turn finished")
    confirmation = f"确认执行 {plan_id}"
    _, execute_turn = store.start_turn(session.session_id, "user-1", confirmation)
    execute = _tool(
        service.build_tools(
            user=_user(),
            session_id=session.session_id,
            turn_id=execute_turn.turn_id,
            organization_key="coinex",
            user_message=confirmation,
        ),
        "execute_test_data",
    )

    first = await execute.handler({"plan_id": plan_id})
    second = await execute.handler({"plan_id": plan_id})

    assert first.is_error is True
    assert json.loads(first.content)["status"] == "unknown"
    assert second.is_error is False
    assert json.loads(second.content)["retry_allowed"] is False
    assert len(client.execute_calls) == 1


def test_tools_default_to_admin_only(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = _tool_service(store, FakeTestDataClient())

    assert service.is_available(user=_user(role="member"), organization_key="coinex") is False
    assert service.is_available(user=_user(), organization_key="other") is False


@pytest.mark.anyio
async def test_task_catalog_discovers_new_tasks_without_static_allowlist(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantTestDataTools(
        store=store,
        client=FakeTestDataClient(),
        workspace_access=None,
        allowed_organizations=["coinex"],
        allowed_environments=[str(index) for index in range(1, 13)],
        allowed_tasks=[],
        blocked_tasks=["track_market_price"],
        admin_only=True,
    )

    task_map = service._allowed_task_map(await service.client.list_tasks())

    assert "register_user" in task_map
    assert "user_2fa_operation" in task_map
    assert "track_market_price" not in task_map


def test_platform_stdout_is_compacted_and_redacted_before_returning_to_model(tmp_path) -> None:
    service = _tool_service(
        SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3")),
        FakeTestDataClient(),
    )
    stdout_payload = {
        "uid": 23167,
        "email": "jordan.lee@corp.test",
        "risk_records": [
            {
                "id": 1,
                "reason": "TEST_RISK",
                "status": "AUDITED",
                "permissions": ["BALANCE_OUT_DISABLED"],
                "detail": "不应返回的完整风控详情",
            }
        ],
        "settings": [
            {"name": "active_one", "desc": "开启项", "active": True, "type": "bool", "value": True},
            {"name": "inactive_one", "desc": "关闭项", "active": False, "type": "bool", "value": False},
        ],
        "tags": [{"name": "STAFF", "title": "员工", "enabled": True, "status": "PASSED"}],
    }
    response = service._sanitize_platform_response(
        "risk_adjustment",
        {
            "request_id": 1114,
            "status": "success",
            "token": "mkd_sensitive_value",
            "stdout": f"initializing\n{json.dumps(stdout_payload, ensure_ascii=False)}\n",
            "stderr": "",
        },
    )
    serialized = json.dumps(response, ensure_ascii=False)

    assert response["request_id"] == 1114
    assert response["output"]["email"] == "jo***@corp.test"
    assert response["output"]["active_settings"] == [
        {"name": "active_one", "desc": "开启项", "type": "bool", "value": True}
    ]
    assert "inactive_one" not in serialized
    assert "不应返回的完整风控详情" not in serialized
    assert "mkd_sensitive_value" not in serialized
    assert "jordan.lee@corp.test" not in serialized


def test_test_data_plan_schema_migration_adds_one_time_secret_columns() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE assistant_test_data_plans (plan_id TEXT PRIMARY KEY)")
    store = object.__new__(SQLiteAssistantStore)

    store._ensure_test_data_plan_columns(connection)

    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(assistant_test_data_plans)").fetchall()
    }
    assert {"secret_json", "secret_revealed_at"} <= columns


@pytest.mark.anyio
async def test_assistant_injects_test_data_tools_without_git_resolver(tmp_path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    runtime = CapturingRuntime()
    test_data_tools = _tool_service(store, FakeTestDataClient())
    assistant = AssistantService(
        store=store,
        runtime_client=runtime,
        system_prompt="system",
        runtime_working_directory=str(tmp_path),
        test_data_tools=test_data_tools,
    )
    session = assistant.create_session(_user(), organization_key="coinex")

    result = await assistant.chat(_user(), session.session_id, "能造哪些测试数据？")

    assert result.turn.status == "completed"
    assert [tool.name for tool in runtime.kwargs["dynamic_tools"]] == [
        "list_test_data_capabilities",
        "query_test_data",
        "prepare_test_data",
        "execute_test_data",
        "get_test_data_request",
    ]
    assert "必须把返回的确认短语原样展示给用户" in runtime.kwargs["system_prompt"]
    assert "dynamic_read_roots" not in runtime.kwargs
