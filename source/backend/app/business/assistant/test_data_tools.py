from __future__ import annotations

import json
import re
import secrets
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, cast

from app.business.assistant.models import TestDataPlanRecord
from app.business.assistant.store import SQLiteAssistantStore
from app.business.workspace import WorkspaceAccessError, WorkspaceAccessService
from app.integrations.agent_runtime import RuntimeDynamicTool, RuntimeDynamicToolResult
from app.integrations.test_data_platform import TestDataPlatformError
from app.services.auth_models import UserRecord
from app.services.permission_service import BusinessPermissionService, TEST_DATA_CAPABILITY


class TestDataClient(Protocol):
    async def list_tasks(self) -> object: ...

    async def execute(
        self,
        *,
        task: str,
        environment: str,
        parameters: dict[str, Any],
    ) -> object: ...

    async def query(
        self,
        *,
        operation: str,
        environment: str,
        parameters: dict[str, Any],
    ) -> object: ...

    async def get_request(self, run_id: str) -> object: ...


class AssistantTestDataTools:
    system_prompt = (
        "当用户要求构造测试数据时，先调用 list_test_data_capabilities 获取当前能力，"
        "业务测试数据的查询类能力使用 query_test_data，查询不会修改数据，也不需要确认；"
        "部署分支、提交、进程和日志查询优先使用当前可用的 CoinEx Test Ops MCP，"
        "不属于测试造数工作流；"
        "写操作再调用 prepare_test_data 生成计划。prepare_test_data 不会执行写操作；"
        "必须把返回的确认短语原样展示给用户，"
        "只有用户在后续消息中明确回复该短语后，才能调用 execute_test_data。"
        "不得声称造数成功，除非工具返回 completed 或明确成功状态。"
    )
    _blocked_parameter_names = {"custom_ip", "ws_url"}
    _blocked_market_tasks = {
        "track_market_price",
        "start_market_tracker",
        "list_market_trackers",
        "stop_market_tracker",
        "restart_market_tracker",
        "get_market_tracker_log",
        "fix_market_price",
        "list_market_price_tasks",
        "cancel_market_price_task",
        "refix_market_price_task",
    }
    _sensitive_key_pattern = re.compile(
        r"password|passwd|token|secret|totp|otp|verification_code|sms_code|email_code",
        re.IGNORECASE,
    )
    _personal_key_pattern = re.compile(r"email|mobile|phone", re.IGNORECASE)
    _high_risk_values = {
        "clear",
        "delete",
        "remove",
        "unbind",
        "disable",
        "forbid",
        "blocked",
    }
    _two_fa_actions = {
        "clear_all",
        "bind_totp",
        "change_totp",
        "unbind_totp",
        "bind_mobile",
        "change_mobile",
        "unbind_mobile",
        "query_sms",
        "query_email_codes",
        "bind_email",
        "change_email",
        "unbind_email",
        "clear_webauthn",
        "unbind_webauthn",
    }

    def __init__(
        self,
        *,
        store: SQLiteAssistantStore,
        client: TestDataClient,
        workspace_access: WorkspaceAccessService | None,
        allowed_organizations: list[str],
        allowed_environments: list[str],
        allowed_tasks: list[str],
        blocked_tasks: list[str] | None = None,
        plan_ttl_seconds: int = 600,
        admin_only: bool = True,
        permission_service: BusinessPermissionService | None = None,
    ) -> None:
        self.store = store
        self.client = client
        self.workspace_access = workspace_access
        self.allowed_organizations = {item.strip().lower() for item in allowed_organizations if item.strip()}
        self.allowed_environments = {item.strip() for item in allowed_environments if item.strip()}
        self.allowed_tasks = {item.strip() for item in allowed_tasks if item.strip()}
        self.blocked_tasks = self._blocked_market_tasks | {
            item.strip() for item in (blocked_tasks or []) if item.strip()
        }
        self.plan_ttl_seconds = max(plan_ttl_seconds, 60)
        self.admin_only = admin_only
        self.permission_service = permission_service

    def is_available(self, *, user: UserRecord, organization_key: str) -> bool:
        normalized_org = organization_key.strip().lower()
        if not normalized_org or normalized_org not in self.allowed_organizations:
            return False
        if user.role == "admin":
            return True
        if self.workspace_access is None:
            return False
        try:
            membership = self.workspace_access.resolve_active_organization(user, normalized_org)
        except WorkspaceAccessError:
            return False
        return not self.admin_only or membership.organization_role == "admin"

    async def is_available_async(
        self,
        *,
        user: UserRecord,
        organization_key: str,
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> bool:
        if self.permission_service is None:
            return self.is_available(user=user, organization_key=organization_key)
        decision = await self.permission_service.check_capability_access(
            user,
            organization_key,
            TEST_DATA_CAPABILITY,
            session_id=session_id,
            turn_id=turn_id,
        )
        return decision.allowed

    def build_tools(
        self,
        *,
        user: UserRecord,
        session_id: str,
        turn_id: str,
        organization_key: str,
        user_message: str,
    ) -> list[RuntimeDynamicTool]:
        if self.permission_service is None and not self.is_available(user=user, organization_key=organization_key):
            return []

        async def check_access(task_name: str | None = None):
            if self.permission_service is None:
                if not self.is_available(user=user, organization_key=organization_key):
                    raise ValueError("当前账号没有测试造数权限。")
                return None
            decision = await self.permission_service.check_capability_access(
                user,
                organization_key,
                TEST_DATA_CAPABILITY,
                task_name,
                session_id=session_id,
                turn_id=turn_id,
            )
            if not decision.allowed:
                raise ValueError(decision.reason)
            return decision

        async def list_capabilities(arguments: dict[str, Any]) -> RuntimeDynamicToolResult:
            try:
                task_map = self._allowed_task_map(await self.client.list_tasks())
                if self.permission_service is not None:
                    decision, allowed_tasks = await self.permission_service.list_allowed_tasks(
                        user=user,
                        organization_key=organization_key,
                        task_names=list(task_map),
                        session_id=session_id,
                        turn_id=turn_id,
                    )
                    if not decision.allowed:
                        return self._error(decision.reason)
                    task_map = {name: descriptor for name, descriptor in task_map.items() if name in allowed_tasks}
                requested = str(arguments.get("task") or "").strip()
                if requested:
                    descriptor = task_map.get(requested)
                    if descriptor is None:
                        return self._error(f"任务 {requested} 不存在或未被 ai-prd 开放。")
                    tasks: object = {requested: descriptor}
                else:
                    tasks = task_map
                return self._ok(
                    {
                        "allowed_environments": sorted(self.allowed_environments),
                        "tasks": tasks,
                        "execution_rule": (
                            "本目录中的业务测试数据查询使用 query_test_data；"
                            "部署分支、提交、进程和日志查询优先使用当前可用的 CoinEx Test Ops MCP；"
                            "所有写操作必须先生成计划，并在后续消息中确认计划 ID。"
                        ),
                    }
                )
            except Exception as exc:
                return self._error(str(exc) or "读取造数能力失败。")

        async def query(arguments: dict[str, Any]) -> RuntimeDynamicToolResult:
            try:
                operation = str(arguments.get("operation") or "").strip()
                await check_access(operation)
                task_map = self._allowed_task_map(await self.client.list_tasks())
                descriptor = task_map.get(operation)
                if not isinstance(descriptor, Mapping) or descriptor.get("kind") != "query":
                    raise ValueError(f"查询能力 {operation or '-'} 不存在或未被 ai-prd 开放。")
                environment = str(arguments.get("environment") or "").strip()
                if not environment:
                    environment = "all" if operation == "query_environment" else "1"
                if operation != "query_environment" and environment not in self.allowed_environments:
                    raise ValueError(f"环境 {environment or '-'} 未被 ai-prd 开放。")
                if (
                    operation == "query_environment"
                    and environment != "all"
                    and environment not in self.allowed_environments
                ):
                    raise ValueError(f"环境 {environment or '-'} 未被 ai-prd 开放。")
                parameters = arguments.get("parameters")
                if not isinstance(parameters, dict):
                    raise ValueError("parameters 必须是 JSON 对象。")
                normalized = self._validate_query_input(
                    operation=operation,
                    environment=environment,
                    parameters=parameters,
                    descriptor=descriptor,
                )
                response = await self.client.query(
                    operation=operation,
                    environment=environment,
                    parameters=normalized,
                )
                return self._ok(
                    {
                        "operation": operation,
                        "environment": environment,
                        "result": self._sanitize_platform_response(operation, response),
                    }
                )
            except Exception as exc:
                return self._error(str(exc) or "查询测试数据失败。")

        async def prepare(arguments: dict[str, Any]) -> RuntimeDynamicToolResult:
            try:
                task = str(arguments.get("task") or "").strip()
                environment = str(arguments.get("environment") or "").strip()
                await check_access(task)
                parameters = arguments.get("parameters")
                if not isinstance(parameters, dict):
                    raise ValueError("parameters 必须是 JSON 对象。")
                candidate_parameters = dict(parameters)
                plan_secret: dict[str, object] | None = None
                trusted_sensitive_names: set[str] = set()
                if task == "register_user":
                    if candidate_parameters.get("password") not in (None, ""):
                        raise ValueError(
                            "注册密码不能通过对话传入；系统会生成一次性测试密码。"
                        )
                    generated_password = self._generate_test_password()
                    candidate_parameters["password"] = generated_password
                    plan_secret = {"password": generated_password}
                    trusted_sensitive_names.add("password")
                normalized_parameters = self._validate_plan_input(
                    task=task,
                    environment=environment,
                    parameters=candidate_parameters,
                    task_map=self._allowed_task_map(await self.client.list_tasks()),
                    trusted_sensitive_names=trusted_sensitive_names,
                )
                if task == "register_user":
                    normalized_parameters.pop("password", None)
                now = datetime.now(timezone.utc)
                redacted = cast(dict[str, Any], self._redact(normalized_parameters))
                risk_level = self._risk_level(task, normalized_parameters)
                plan_id = f"tdp_{secrets.token_hex(6)}"
                summary = self._summary(task, environment, redacted)
                record = TestDataPlanRecord(
                    plan_id=plan_id,
                    user_id=user.user_id,
                    session_id=session_id,
                    prepared_turn_id=turn_id,
                    executed_turn_id=None,
                    organization_key=organization_key,
                    task_name=task,
                    environment=environment,
                    parameters=normalized_parameters,
                    redacted_parameters=redacted,
                    risk_level=risk_level,
                    summary=summary,
                    status="prepared",
                    upstream_run_id=None,
                    upstream_status=None,
                    result=None,
                    error_message=None,
                    created_at=now,
                    expires_at=now + timedelta(seconds=self.plan_ttl_seconds),
                    executed_at=None,
                    updated_at=now,
                    secret=plan_secret,
                )
                self.store.create_test_data_plan(record)
                self.store.append_runtime_event(
                    session_id,
                    turn_id,
                    "activity",
                    {
                        "kind": "test_data_plan_prepared",
                        "message": f"已生成造数计划 {plan_id}",
                        "plan_id": plan_id,
                        "task": task,
                        "environment": environment,
                        "risk_level": risk_level,
                    },
                )
                return self._ok(
                    {
                        "plan_id": plan_id,
                        "status": "prepared",
                        "risk_level": risk_level,
                        "summary": summary,
                        "expires_at": record.expires_at.isoformat(),
                        "confirmation_phrase": f"确认执行 {plan_id}",
                    }
                )
            except Exception as exc:
                return self._error(str(exc) or "生成造数计划失败。")

        async def execute(arguments: dict[str, Any]) -> RuntimeDynamicToolResult:
            plan_id = str(arguments.get("plan_id") or "").strip()
            if not plan_id:
                return self._error("plan_id 不能为空。")
            confirmation_phrase = f"确认执行 {plan_id}"
            if " ".join(user_message.split()) != confirmation_phrase:
                return self._error(f"缺少用户明确确认。请让用户回复：{confirmation_phrase}")
            existing = self.store.get_test_data_plan(
                plan_id=plan_id,
                user_id=user.user_id,
                session_id=session_id,
            )
            if existing is None:
                return self._error("造数计划不存在或不属于当前会话。")
            try:
                await check_access(existing.task_name)
            except Exception as exc:
                return self._error(str(exc) or "当前账号没有测试造数权限。")
            plan = self.store.claim_test_data_plan_for_execution(
                plan_id=plan_id,
                user_id=user.user_id,
                session_id=session_id,
                executed_turn_id=turn_id,
            )
            if plan is None:
                if existing.status == "prepared" and existing.expires_at <= datetime.now(timezone.utc):
                    return self._error("造数计划已过期，请重新生成计划。")
                return self._ok(self._plan_result(existing))

            try:
                execution_parameters = dict(plan.parameters)
                if plan.task_name == "register_user":
                    password = str((plan.secret or {}).get("password") or "")
                    if not password:
                        return self._error("注册计划缺少服务端生成的一次性密码，请重新生成计划。")
                    execution_parameters["password"] = password
                response = await self.client.execute(
                    task=plan.task_name,
                    environment=plan.environment,
                    parameters=execution_parameters,
                )
                response_secret = self._extract_response_secret(plan.task_name, response)
                if response_secret:
                    self.store.merge_test_data_plan_secret(
                        plan_id=plan.plan_id,
                        secret=response_secret,
                    )
                response_dict = self._sanitize_platform_response(plan.task_name, response)
                run_id = self._extract_text(response, ("run_id", "request_id", "id"))
                upstream_status = self._extract_text(response, ("status", "state"))
                status = self._local_status(response, upstream_status)
                completed = self.store.finish_test_data_plan(
                    plan_id=plan.plan_id,
                    status=status,
                    upstream_run_id=run_id,
                    upstream_status=upstream_status,
                    result=response_dict,
                )
                self.store.append_runtime_event(
                    session_id,
                    turn_id,
                    "activity",
                    {
                        "kind": "test_data_execution",
                        "message": f"造数计划 {plan.plan_id} 已提交",
                        "plan_id": plan.plan_id,
                        "task": plan.task_name,
                        "environment": plan.environment,
                        "status": completed.status,
                        "run_id": completed.upstream_run_id,
                    },
                )
                return self._ok(self._plan_result(completed))
            except TestDataPlatformError as exc:
                status = "unknown" if exc.execution_state_unknown else "failed"
                failed = self.store.finish_test_data_plan(
                    plan_id=plan.plan_id,
                    status=status,
                    error_message=str(exc),
                )
                return self._error(
                    str(exc),
                    extra={
                        "plan_id": failed.plan_id,
                        "status": failed.status,
                        "retry_allowed": False,
                    },
                )
            except Exception as exc:
                failed = self.store.finish_test_data_plan(
                    plan_id=plan.plan_id,
                    status="unknown",
                    error_message=str(exc) or "造数执行状态未知。",
                )
                return self._error(
                    str(exc) or "造数执行状态未知。",
                    extra={
                        "plan_id": failed.plan_id,
                        "status": failed.status,
                        "retry_allowed": False,
                    },
                )

        async def get_request(arguments: dict[str, Any]) -> RuntimeDynamicToolResult:
            plan_id = str(arguments.get("plan_id") or "").strip()
            plan = self.store.get_test_data_plan(
                plan_id=plan_id,
                user_id=user.user_id,
                session_id=session_id,
            )
            if plan is None:
                return self._error("造数计划不存在或不属于当前会话。")
            if not plan.upstream_run_id:
                return self._ok(self._plan_result(plan))
            if plan.status in {"completed", "failed"}:
                return self._ok(self._plan_result(plan))
            try:
                response = await self.client.get_request(plan.upstream_run_id)
                response_secret = self._extract_response_secret(plan.task_name, response)
                if response_secret:
                    self.store.merge_test_data_plan_secret(
                        plan_id=plan.plan_id,
                        secret=response_secret,
                    )
                response_dict = self._sanitize_platform_response(plan.task_name, response)
                upstream_status = self._extract_text(response, ("status", "state"))
                status = self._local_status(response, upstream_status)
                updated = self.store.finish_test_data_plan(
                    plan_id=plan.plan_id,
                    status=status,
                    upstream_status=upstream_status,
                    result=response_dict,
                )
                return self._ok(self._plan_result(updated))
            except Exception as exc:
                return self._error(str(exc) or "查询造数状态失败。")

        return [
            RuntimeDynamicTool(
                name="list_test_data_capabilities",
                description=(
                    "读取当前 ai-prd 允许使用的测试造数任务、环境和参数说明。"
                        "用户询问能造什么数据，或准备造数但还不确定 task/参数时调用；"
                        "只读，不执行造数。"
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "task": {"type": "string", "description": "可选的任务代码。"},
                    },
                },
                handler=list_capabilities,
            ),
            RuntimeDynamicTool(
                name="query_test_data",
                description=(
                    "调用 UAT 造数平台的只读查询接口。"
                    "查询 KYC、2FA、P2P、风控、用户权限、资产或 Push 等业务测试数据时使用；"
                    "query_environment 是独立的环境监测接口；部署分支、提交、进程和日志查询"
                    "优先使用当前可用的 CoinEx Test Ops MCP（ops_list_services、ops_get_service_status 等）。"
                    "不会修改数据，也不需要确认。"
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["operation", "environment", "parameters"],
                    "properties": {
                        "operation": {"type": "string", "description": "查询能力代码。"},
                        "environment": {
                            "type": "string",
                            "description": "测试环境编号；query_environment 可使用 all。",
                        },
                        "parameters": {
                            "type": "object",
                            "description": "查询参数；不要包含 env 或敏感凭据。",
                        },
                    },
                },
                handler=query,
            ),
            RuntimeDynamicTool(
                name="prepare_test_data",
                description=(
                    "校验并生成一个不可修改的测试造数计划，但不执行。"
                    "所有写操作必须先调用本工具，再向用户展示确认短语。"
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["task", "environment", "parameters"],
                    "properties": {
                        "task": {"type": "string", "description": "任务代码。"},
                        "environment": {"type": "string", "description": "测试环境编号。"},
                        "parameters": {
                            "type": "object",
                            "description": "任务参数；不要包含 env、custom_ip、ws_url 或敏感凭据。",
                        },
                    },
                },
                handler=prepare,
            ),
            RuntimeDynamicTool(
                name="execute_test_data",
                description=(
                    "执行已准备的造数计划。"
                    "只有当前用户消息原样包含 prepare_test_data 返回的确认短语时才能调用；"
                    "只接受 plan_id，禁止重新传入任务参数。"
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["plan_id"],
                    "properties": {"plan_id": {"type": "string"}},
                },
                handler=execute,
            ),
            RuntimeDynamicTool(
                name="get_test_data_request",
                description="查询当前用户、当前会话中某个造数计划的执行状态；只读。",
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["plan_id"],
                    "properties": {"plan_id": {"type": "string"}},
                },
                handler=get_request,
            ),
        ]

    def _allowed_task_map(self, payload: object) -> dict[str, object]:
        task_map = self._task_map(payload)
        if not task_map:
            raise ValueError("造数平台任务目录格式无效。")
        return {
            name: descriptor
            for name, descriptor in task_map.items()
            if self._task_is_enabled(name)
        }

    def _task_is_enabled(self, task: str) -> bool:
        return task not in self.blocked_tasks and (
            not self.allowed_tasks or task in self.allowed_tasks
        )

    def _task_map(self, payload: object) -> dict[str, object]:
        if isinstance(payload, Mapping):
            for key in ("tasks", "data", "items"):
                nested = payload.get(key)
                if isinstance(nested, (Mapping, list)):
                    mapped = self._task_map(nested)
                    if mapped:
                        return mapped
            result: dict[str, object] = {}
            for key, value in payload.items():
                if isinstance(value, Mapping):
                    name = self._task_name(value) or str(key)
                    result[name] = value
            return result
        if isinstance(payload, list):
            result = {}
            for item in payload:
                if isinstance(item, str):
                    result[item] = {"name": item}
                elif isinstance(item, Mapping):
                    name = self._task_name(item)
                    if name:
                        result[name] = item
            return result
        return {}

    @staticmethod
    def _task_name(item: Mapping[str, object]) -> str:
        for key in ("name", "task", "task_name", "code", "script", "id"):
            value = str(item.get(key) or "").strip()
            if value:
                return value
        return ""

    def _validate_plan_input(
        self,
        *,
        task: str,
        environment: str,
        parameters: dict[str, Any],
        task_map: dict[str, object],
        trusted_sensitive_names: set[str] | None = None,
    ) -> dict[str, Any]:
        if not task or not self._task_is_enabled(task) or task not in task_map:
            raise ValueError(f"任务 {task or '-'} 不存在或未被 ai-prd 开放。")
        descriptor = task_map[task]
        if isinstance(descriptor, Mapping) and descriptor.get("kind") == "query":
            raise ValueError(f"任务 {task} 是只读查询能力，请使用 query_test_data。")
        if environment not in self.allowed_environments:
            raise ValueError(f"环境 {environment or '-'} 未被 ai-prd 开放。")
        normalized = self._normalize_request_parameters(
            parameters=parameters,
            environment=environment,
            descriptor=descriptor,
            trusted_sensitive_names=trusted_sensitive_names or set(),
        )
        if task in {
            "adjust_user_asset",
            "update_kyc_status",
            "risk_adjustment",
            "risk_user_permission",
            "user_2fa_operation",
            "update_p2p_status",
        } and not str(normalized.get("identifier") or "").strip():
            raise ValueError("identifier 不能为空，必须提供用户 UID 或邮箱。")
        if task == "user_2fa_operation":
            self._validate_two_fa_action(normalized)
        return normalized

    def _validate_query_input(
        self,
        *,
        operation: str,
        environment: str,
        parameters: dict[str, Any],
        descriptor: Mapping[str, object],
    ) -> dict[str, Any]:
        normalized = self._normalize_request_parameters(
            parameters=parameters,
            environment=environment,
            descriptor=descriptor,
            trusted_sensitive_names=set(),
        )
        if operation in {
            "query_kyc_status",
            "query_2fa_status",
            "query_p2p_status",
            "query_risk_status",
            "query_user_permissions",
            "query_user_assets",
        } and not str(normalized.get("identifier") or "").strip():
            raise ValueError("identifier 不能为空，必须提供用户 UID 或邮箱。")
        return normalized

    def _normalize_request_parameters(
        self,
        *,
        parameters: dict[str, Any],
        environment: str,
        descriptor: object,
        trusted_sensitive_names: set[str],
    ) -> dict[str, Any]:
        normalized = dict(parameters)
        supplied_env = normalized.pop("env", None)
        if supplied_env is not None and str(supplied_env) != environment:
            raise ValueError("parameters.env 与 environment 不一致。")
        self._validate_parameter_keys(
            normalized,
            trusted_sensitive_names=trusted_sensitive_names,
        )
        return self._apply_catalog_schema(normalized, descriptor)

    @staticmethod
    def _apply_catalog_schema(
        parameters: dict[str, Any],
        descriptor: object,
    ) -> dict[str, Any]:
        if not isinstance(descriptor, Mapping):
            return parameters
        fields = descriptor.get("fields")
        if not isinstance(fields, list):
            return parameters
        field_map = {
            str(field.get("name") or "").strip(): field
            for field in fields
            if isinstance(field, Mapping) and str(field.get("name") or "").strip()
        }
        allowed_names = set(field_map) - {"env"}
        unknown_names = sorted(set(parameters) - allowed_names)
        if unknown_names:
            raise ValueError(f"任务不支持参数：{', '.join(unknown_names)}。")

        normalized = dict(parameters)
        missing: list[str] = []
        for name, field in field_map.items():
            if name == "env" or not field.get("required"):
                continue
            value = normalized.get(name)
            if value not in (None, ""):
                continue
            default = field.get("default")
            if default not in (None, ""):
                normalized[name] = default
            else:
                missing.append(name)
        if missing:
            raise ValueError(f"缺少必填参数：{', '.join(missing)}。")

        for name, field in field_map.items():
            if field.get("type") != "checkbox" or name not in normalized:
                continue
            value = normalized[name]
            if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
                normalized[name] = value.strip().lower() == "true"
        for name, field in field_map.items():
            if name in normalized and normalized[name] is not None:
                normalized[name] = AssistantTestDataTools._normalize_schema_value(
                    name,
                    normalized[name],
                    field,
                )
        return normalized

    @staticmethod
    def _normalize_schema_value(name: str, value: object, field: Mapping[str, object]) -> object:
        enum = field.get("enum")
        if isinstance(enum, list) and value not in enum:
            values = ", ".join(str(item) for item in enum)
            raise ValueError(f"参数 {name} 的值不受支持，可选值：{values}。")

        field_type = str(field.get("type") or "")
        if field_type == "boolean" and isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            value = value.strip().lower() == "true"
        elif field_type == "boolean" and not isinstance(value, bool):
            raise ValueError(f"参数 {name} 必须是布尔值。")
        elif field_type == "integer":
            if isinstance(value, str) and value.strip().isdigit():
                value = int(value.strip())
            elif not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"参数 {name} 必须是整数。")
        elif field_type in {"string", "password"} and not isinstance(value, str):
            raise ValueError(f"参数 {name} 必须是字符串。")
        elif field_type == "array":
            if not isinstance(value, list):
                raise ValueError(f"参数 {name} 必须是数组。")
            item_schema = field.get("items")
            if isinstance(item_schema, Mapping):
                value = [
                    AssistantTestDataTools._normalize_schema_value(
                        f"{name}[{index}]",
                        item,
                        item_schema,
                    )
                    for index, item in enumerate(value)
                ]
        elif field_type == "object":
            if not isinstance(value, dict):
                raise ValueError(f"参数 {name} 必须是 JSON 对象。")
            properties = field.get("properties")
            required = field.get("required")
            if isinstance(properties, Mapping):
                required_names = set(required) if isinstance(required, list) else set()
                normalized_object = dict(value)
                missing: list[str] = []
                for property_name, property_schema in properties.items():
                    if property_name not in normalized_object or normalized_object[property_name] in (None, ""):
                        if property_name in required_names:
                            default = property_schema.get("default") if isinstance(property_schema, Mapping) else None
                            if default not in (None, ""):
                                normalized_object[property_name] = default
                            else:
                                missing.append(str(property_name))
                        continue
                    if isinstance(property_schema, Mapping):
                        normalized_object[property_name] = AssistantTestDataTools._normalize_schema_value(
                            f"{name}.{property_name}",
                            normalized_object[property_name],
                            property_schema,
                        )
                if missing:
                    raise ValueError(f"参数 {name} 缺少必填字段：{', '.join(missing)}。")
                value = normalized_object
        return value

    def _validate_parameter_keys(
        self,
        value: object,
        *,
        trusted_sensitive_names: set[str],
    ) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized_key = str(key).strip().lower()
                if normalized_key in self._blocked_parameter_names:
                    raise ValueError(f"参数 {normalized_key} 不允许通过对话工具设置。")
                if (
                    self._sensitive_key_pattern.search(normalized_key)
                    and normalized_key not in trusted_sensitive_names
                ):
                    raise ValueError(f"敏感参数 {normalized_key} 不允许出现在对话或工具参数中。")
                self._validate_parameter_keys(
                    item,
                    trusted_sensitive_names=trusted_sensitive_names,
                )
        elif isinstance(value, list):
            for item in value:
                self._validate_parameter_keys(
                    item,
                    trusted_sensitive_names=trusted_sensitive_names,
                )

    def _validate_two_fa_action(self, parameters: dict[str, Any]) -> None:
        action = str(parameters.get("action") or "query").strip().lower()
        if action not in self._two_fa_actions:
            raise ValueError(f"不支持的 2FA action：{action or '-'}。")
        required_by_action = {
            "bind_mobile": ("country_code", "mobile"),
            "change_mobile": ("country_code", "mobile"),
            "bind_email": ("new_email",),
            "change_email": ("new_email",),
            "unbind_webauthn": ("credential_id",),
        }
        missing = [
            name
            for name in required_by_action.get(action, ())
            if not str(parameters.get(name) or "").strip()
        ]
        if missing:
            raise ValueError(f"2FA action {action} 缺少参数：{', '.join(missing)}。")

    def _risk_level(self, task: str, parameters: dict[str, Any]) -> str:
        if task in {"risk_adjustment", "risk_user_permission"}:
            return "high"
        if task == "user_2fa_operation" and str(parameters.get("action") or "query") != "query":
            return "high"
        amount = parameters.get("amount")
        try:
            if amount is not None and float(amount) < 0:
                return "high"
        except (TypeError, ValueError):
            pass
        if self._is_truthy(parameters.get("ignore_whitelist")) or self._is_truthy(parameters.get("self_forbid")):
            return "high"
        for key in ("action", "status", "cleared_status"):
            value = str(parameters.get(key) or "").strip().lower()
            if value in self._high_risk_values:
                return "high"
        return "medium"

    @staticmethod
    def _is_truthy(value: object) -> bool:
        return value is True or (isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "on"})

    def _summary(self, task: str, environment: str, parameters: dict[str, Any]) -> str:
        visible_keys = (
            "identifier",
            "uid",
            "email",
            "account",
            "asset",
            "amount",
            "action",
            "level",
            "status",
            "risk_reason",
            "ignore_whitelist",
            "self_forbid",
            "user_identifier",
            "counter_identifier",
            "target_value",
        )
        details = [
            f"{key}={parameters[key]}"
            for key in visible_keys
            if key in parameters
        ]
        suffix = f"；{', '.join(details)}" if details else ""
        return f"环境 {environment} 执行 {task}{suffix}"

    def _redact(self, value: object) -> object:
        if isinstance(value, Mapping):
            result: dict[str, object] = {}
            for key, item in value.items():
                normalized_key = str(key)
                if self._sensitive_key_pattern.search(normalized_key):
                    result[normalized_key] = "***"
                elif self._personal_key_pattern.search(normalized_key):
                    result[normalized_key] = self._mask_personal(str(item))
                else:
                    result[normalized_key] = self._redact(item)
            return result
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            value = re.sub(
                r"([A-Za-z0-9._%+-]{1,2})[A-Za-z0-9._%+-]*(@[A-Za-z0-9.-]+\.[A-Za-z]{2,})",
                r"\1***\2",
                value,
            )
            return re.sub(r"mkd_[A-Za-z0-9_-]+", "mkd_***", value)
        return value

    def _sanitize_platform_response(self, task: str, payload: object) -> dict[str, object]:
        redacted = self._as_dict(self._redact(payload))
        stdout = redacted.pop("stdout", None)
        if isinstance(stdout, str) and stdout.strip():
            output = self._redact(self._parse_stdout_result(stdout))
            redacted["output"] = self._compact_task_output(task, output)
        stderr = redacted.get("stderr")
        if isinstance(stderr, str):
            if stderr.strip():
                redacted["stderr"] = stderr[-2000:]
            else:
                redacted.pop("stderr", None)
        return cast(dict[str, object], self._compact_value(redacted))

    def _extract_response_secret(self, task: str, payload: object) -> dict[str, object]:
        if task != "user_2fa_operation":
            return {}
        source = payload
        if isinstance(payload, Mapping) and isinstance(payload.get("stdout"), str):
            source = self._parse_stdout_result(str(payload["stdout"]))
        found: dict[str, object] = {}

        def visit(value: object) -> None:
            if isinstance(value, Mapping):
                for key, item in value.items():
                    normalized_key = str(key).strip().lower()
                    if (
                        self._sensitive_key_pattern.search(normalized_key)
                        or normalized_key in {"code", "codes", "recovery_codes"}
                    ) and item not in (None, "", [], {}):
                        found[normalized_key] = item
                    else:
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(source)
        return found

    @staticmethod
    def _generate_test_password() -> str:
        return f"Tt1!{secrets.token_urlsafe(12)}"

    @staticmethod
    def _parse_stdout_result(stdout: str) -> object:
        for line in reversed(stdout.splitlines()):
            candidate = line.strip()
            if not candidate:
                continue
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
        return stdout[-2000:]

    def _compact_task_output(self, task: str, output: object) -> object:
        if not isinstance(output, Mapping):
            return self._compact_value(output)
        if task not in {"risk_adjustment", "risk_user_permission"}:
            return self._compact_value(dict(output))

        compact: dict[str, object] = {}
        for key in ("uid", "email", "cleared", "self_forbid", "whitelists", "wool_risk"):
            if key in output:
                compact[key] = output[key]
        risk_records = output.get("risk_records")
        if isinstance(risk_records, list):
            compact["risk_records"] = [
                {
                    key: record.get(key)
                    for key in ("id", "reason", "status", "status_label", "source", "permissions")
                    if key in record
                }
                for record in risk_records[:20]
                if isinstance(record, Mapping)
            ]
        settings = output.get("settings")
        if isinstance(settings, list):
            compact["active_settings"] = [
                {key: item.get(key) for key in ("name", "desc", "type", "value") if key in item}
                for item in settings
                if isinstance(item, Mapping) and item.get("active")
            ][:50]
        tags = output.get("tags")
        if isinstance(tags, list):
            compact["enabled_tags"] = [
                {key: item.get(key) for key in ("name", "title", "status") if key in item}
                for item in tags
                if isinstance(item, Mapping) and item.get("enabled")
            ][:50]
        admin_records = output.get("admin_risk_records")
        if isinstance(admin_records, list):
            compact["admin_risk_records"] = [
                {
                    key: item.get(key)
                    for key in ("id", "risk_type", "permission", "status", "status_label")
                    if key in item
                }
                for item in admin_records[:20]
                if isinstance(item, Mapping)
            ]
        return self._compact_value(compact)

    def _compact_value(self, value: object, *, depth: int = 0) -> object:
        if depth >= 5:
            return "[内容已截断]"
        if isinstance(value, Mapping):
            return {
                str(key): self._compact_value(item, depth=depth + 1)
                for key, item in list(value.items())[:80]
            }
        if isinstance(value, list):
            return [self._compact_value(item, depth=depth + 1) for item in value[:50]]
        if isinstance(value, str) and len(value) > 2000:
            return f"{value[:2000]}…[已截断]"
        return value

    @staticmethod
    def _mask_personal(value: str) -> str:
        if "@" in value:
            local, domain = value.split("@", 1)
            return f"{local[:2]}***@{domain}"
        if len(value) > 4:
            return f"***{value[-4:]}"
        return "***"

    @staticmethod
    def _extract_text(payload: object, keys: tuple[str, ...]) -> str | None:
        if isinstance(payload, Mapping):
            for key in keys:
                value = payload.get(key)
                if value is not None and not isinstance(value, (dict, list)):
                    text = str(value).strip()
                    if text:
                        return text
            for value in payload.values():
                nested = AssistantTestDataTools._extract_text(value, keys)
                if nested:
                    return nested
        return None

    @staticmethod
    def _local_status(payload: object, upstream_status: str | None) -> str:
        normalized = (upstream_status or "").strip().lower()
        if normalized in {"completed", "success", "succeeded", "done"}:
            return "completed"
        if normalized in {"failed", "error", "cancelled", "canceled"}:
            return "failed"
        if isinstance(payload, Mapping) and not normalized:
            if payload.get("success") is True or payload.get("ok") is True:
                return "completed"
            if str(payload.get("code") or "").strip() in {"0", "200"}:
                return "completed"
        return "submitted"

    @staticmethod
    def _as_dict(value: object) -> dict[str, object]:
        if isinstance(value, dict):
            return value
        return {"value": value}

    @staticmethod
    def _plan_result(plan: TestDataPlanRecord) -> dict[str, object]:
        return {
            "plan_id": plan.plan_id,
            "status": plan.status,
            "task": plan.task_name,
            "environment": plan.environment,
            "risk_level": plan.risk_level,
            "summary": plan.summary,
            "run_id": plan.upstream_run_id,
            "upstream_status": plan.upstream_status,
            "result": plan.result,
            "error": plan.error_message,
            "retry_allowed": plan.status == "prepared",
        }

    @staticmethod
    def _ok(payload: dict[str, object]) -> RuntimeDynamicToolResult:
        return RuntimeDynamicToolResult(
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )

    @staticmethod
    def _error(
        message: str,
        *,
        extra: dict[str, object] | None = None,
    ) -> RuntimeDynamicToolResult:
        return RuntimeDynamicToolResult(
            content=json.dumps(
                {"error": message[:1000], **(extra or {})},
                ensure_ascii=False,
                sort_keys=True,
            ),
            is_error=True,
        )
