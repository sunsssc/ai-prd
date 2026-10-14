from __future__ import annotations

from typing import Any

import httpx


def _field(
    name: str,
    *,
    required: bool = False,
    field_type: str = "string",
    default: object = None,
    enum: tuple[str, ...] | None = None,
    description: str | None = None,
    items: dict[str, object] | None = None,
) -> dict[str, object]:
    field: dict[str, object] = {
        "name": name,
        "type": field_type,
        "required": required,
    }
    if default is not None:
        field["default"] = default
    if enum is not None:
        field["enum"] = list(enum)
    if description:
        field["description"] = description
    if items is not None:
        field["items"] = items
    return field


def _task(
    name: str,
    *,
    path: str,
    summary: str,
    fields: list[dict[str, object]],
    kind: str = "write",
) -> dict[str, object]:
    return {
        "name": name,
        "kind": kind,
        "method": "POST",
        "path": path,
        "summary": summary,
        "fields": fields,
    }


_USER_FIELDS = [
    _field("env", default="1", description="目标测试环境编号，例如 1-12。"),
    _field("identifier", required=True, description="用户 UID 或邮箱。"),
]


# 该目录与 /openapi.json 的直接业务接口保持一致。/makedata/tasks 仍用于发现平台
# 额外提供的动态任务，但已知接口必须使用这里的明确路由和请求体。
TEST_DATA_API_TASKS: tuple[dict[str, object], ...] = (
    _task(
        "query_kyc_status",
        path="/api/v1/users/kyc/query",
        summary="查询 KYC",
        kind="query",
        fields=[
            _field("env", default="1", description="目标测试环境编号，例如 1-12。"),
            _field("identifier", required=True, description="用户 UID 或邮箱。"),
            _field(
                "level",
                default="basic",
                enum=("basic", "pro", "institution"),
                description="KYC 级别。",
            ),
        ],
    ),
    _task(
        "update_kyc_status",
        path="/api/v1/users/kyc/update",
        summary="更新 KYC",
        fields=[
            _field("env", default="1", description="目标测试环境编号，例如 1-12。"),
            _field("identifier", required=True, description="用户 UID 或邮箱。"),
            _field("level", default="basic", enum=("basic", "pro", "institution")),
            _field("action", default="save", enum=("save", "create", "edit", "clear", "clear_all")),
            _field("status", default="passed", enum=("processing", "failed", "passed")),
            _field("reject_reason", default="OTHER"),
            _field("data", field_type="object", description="KYC 认证资料。"),
        ],
    ),
    _task(
        "query_2fa_status",
        path="/api/v1/users/2fa/query",
        summary="查询 2FA",
        kind="query",
        fields=_USER_FIELDS.copy(),
    ),
    _task(
        "user_2fa_operation",
        path="/api/v1/users/2fa/update",
        summary="更新 2FA",
        fields=[
            _field("env", default="1", description="目标测试环境编号，例如 1-12。"),
            _field("identifier", required=True, description="用户 UID 或邮箱。"),
            _field(
                "action",
                required=True,
                enum=(
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
                    "unbind_webauthn",
                    "clear_webauthn",
                    "clear_all",
                ),
                description="2FA 操作。",
            ),
            _field("totp_key", default=""),
            _field("new_email", default=""),
            _field("country_code", default=""),
            _field("mobile", default=""),
            _field("sms_type", default="MOBILE_BINDING"),
            _field("credential_id", default=""),
            _field("ip", default="127.0.0.1"),
        ],
    ),
    _task(
        "query_p2p_status",
        path="/api/v1/users/p2p/query",
        summary="查询 P2P 状态",
        kind="query",
        fields=_USER_FIELDS.copy(),
    ),
    _task(
        "update_p2p_status",
        path="/api/v1/users/p2p/update",
        summary="更新 P2P 状态",
        fields=[
            _field("env", default="1", description="目标测试环境编号，例如 1-12。"),
            _field("identifier", required=True, description="用户 UID 或邮箱。"),
            _field(
                "action",
                required=True,
                enum=("enable_p2p", "disable_p2p", "enable_p2p_merchant", "disable_p2p_merchant"),
                description="P2P 状态变更操作。",
            ),
            _field("ignore_requirements", default=False, field_type="boolean"),
            _field("ignore_merchant_requirements", default=False, field_type="boolean"),
            _field("clear_p2p_permission_records", default=False, field_type="boolean"),
            _field("clear_merchant_permission_records", default=False, field_type="boolean"),
        ],
    ),
    _task(
        "query_risk_status",
        path="/api/v1/users/risk/query",
        summary="查询风控状态",
        kind="query",
        fields=_USER_FIELDS.copy(),
    ),
    _task(
        "risk_adjustment",
        path="/api/v1/users/risk/update",
        summary="更新风控状态",
        fields=[
            _field("env", default="1", description="目标测试环境编号，例如 1-12。"),
            _field("identifier", required=True, description="用户 UID 或邮箱。"),
            _field(
                "action",
                required=True,
                enum=("hit_risk", "hit_admin_risk", "inject_similar_email_risk"),
                description="风控造数操作。",
            ),
            _field("risk_reason", default=""),
            _field("risk_source", default=""),
            _field("risk_detail", default="makedata hit risk"),
            _field("admin_risk_type", default=""),
            _field("ignore_whitelist", default=False, field_type="boolean"),
            _field("activity_id", default=""),
            _field("similar_email_count", default=3, field_type="integer"),
        ],
    ),
    _task(
        "query_user_permissions",
        path="/api/v1/users/permissions/query",
        summary="查询用户权限",
        kind="query",
        fields=_USER_FIELDS.copy(),
    ),
    _task(
        "risk_user_permission",
        path="/api/v1/users/permissions/update",
        summary="更新用户权限",
        fields=[
            _field("env", default="1", description="目标测试环境编号，例如 1-12。"),
            _field("identifier", required=True, description="用户 UID 或邮箱。"),
            _field("action", required=True, enum=("save", "clear_all"), description="用户权限变更操作。"),
            _field("settings", field_type="object", description="权限配置。"),
            _field("tags", field_type="object", description="用户标签。"),
            _field("cleared_status", default="none", enum=("none", "FORBIDDEN", "WITHDRAWAL_ONLY")),
            _field("cleared_remark", default="makedata risk permission"),
            _field("self_forbid", default=False, field_type="boolean"),
        ],
    ),
    _task(
        "query_environment",
        path="/api/v1/environments/query",
        summary="检测测试环境",
        kind="query",
        fields=[
            _field("env", default="all", description="环境编号或 all。"),
            _field(
                "with_jobs",
                default=False,
                field_type="boolean",
                description="是否检查服务器 Job/队列配置。",
            ),
        ],
    ),
    _task(
        "register_user",
        path="/api/v1/users/register",
        summary="注册用户",
        fields=[
            _field("env", default="1"),
            _field("email", required=True),
            _field("password", required=True, field_type="password"),
            _field("device", default="makedata-register-user"),
            _field("ip", default="127.0.0.1"),
            _field("lang", default="en_US"),
            _field("channel", default=""),
            _field("refer", default=""),
            _field("keep_plus", default=True, field_type="boolean"),
        ],
    ),
    _task(
        "adjust_user_asset",
        path="/api/v1/users/assets/update",
        summary="调整用户资产",
        fields=[
            _field("env", default="1", description="目标测试环境编号，例如 1-12。"),
            _field("identifier", required=True, description="用户 UID 或邮箱。"),
            _field("account", default="0", description="金融账户 ID。"),
            _field("asset", required=True),
            _field("amount", required=True),
            _field("type", default="ASSET_UPDATE"),
            _field("remark", default="makedata asset update"),
        ],
    ),
    _task(
        "query_user_assets",
        path="/api/v1/users/assets/query",
        summary="查询用户全部金融账户资产",
        kind="query",
        fields=[
            _field("env", default="1", description="目标测试环境编号，例如 1-12。"),
            _field("identifier", required=True, description="用户 UID 或邮箱。"),
            _field(
                "account",
                default="all",
                description="金融账户 ID：all、simulation_perpetual 或数字 account_id。",
            ),
            _field("exclude_subaccounts", default=True, field_type="boolean"),
        ],
    ),
    _task(
        "set_user_asset_targets",
        path="/api/v1/users/assets/targets/update",
        summary="批量设置用户账户目标可用余额",
        fields=[
            _field("env", default="1", description="目标测试环境编号，例如 1-12。"),
            _field("identifier", required=True, description="用户 UID 或邮箱。"),
            _field(
                "changes",
                required=True,
                field_type="array",
                items={
                    "type": "object",
                    "required": ["scope", "owner_uid", "asset", "target_available"],
                    "properties": {
                        "scope": {"type": "string", "enum": ["server", "perpetual"]},
                        "owner_uid": {"type": "integer"},
                        "account_id": {"type": "integer", "default": 0},
                        "asset": {"type": "string"},
                        "target_available": {"type": "string"},
                    },
                },
            ),
            _field("remark", default="makedata set target balance"),
        ],
    ),
    _task(
        "update_monitor_events",
        path="/api/v1/monitor-events/update",
        summary="创建或更新埋点",
        fields=[
            _field("env", default="1"),
            _field(
                "events",
                required=True,
                field_type="array",
                items={
                    "type": "object",
                    "required": ["id", "event_type", "name", "data_type", "time_range"],
                    "properties": {
                        "id": {"type": "integer"},
                        "event_type": {"type": "string"},
                        "name": {"type": "string"},
                        "data_type": {"type": "string", "enum": ["次数", "去重次数", "瞬间值"]},
                        "desc": {"type": "string", "default": ""},
                        "time_range": {"type": "string", "enum": ["每分钟/小时/天", "最近一段时间"]},
                    },
                },
            ),
        ],
    ),
    _task(
        "quick_activity_clone",
        path="/api/v1/activities/clone",
        summary="复制活动配置",
        fields=[
            _field("env", default="1"),
            _field(
                "activity_type",
                required=True,
                enum=(
                    "airdrop",
                    "trade_rank",
                    "deposit_bonus",
                    "dibs",
                    "business_ambassador",
                    "channel_activity",
                    "newbie_mission",
                    "routine_mission",
                    "calendar",
                    "charity",
                    "p2p",
                    "perpetual_special",
                    "mining",
                    "ieo",
                    "legacy_deposit",
                    "novice_package",
                    "perpetual_experience",
                    "launch_pool_mining",
                ),
            ),
            _field("activity_id", required=True, field_type="integer"),
            _field("name", default=""),
            _field("started_at", default="", description="UTC+8 ISO 时间；慈善活动可为空。"),
            _field("ended_at", default="", description="UTC+8 ISO 时间；慈善活动可为空。"),
        ],
    ),
    _task(
        "activity_participation",
        path="/api/v1/activities/participate",
        summary="模拟活动参与",
        fields=[
            _field("env", default="1"),
            _field(
                "activity_type",
                required=True,
                enum=(
                    "trade_rank",
                    "business_ambassador",
                    "airdrop",
                    "dibs",
                    "deposit_bonus",
                    "channel_activity",
                    "newbie_mission",
                    "routine_mission",
                    "p2p_merchant",
                    "mining",
                    "ieo",
                    "launch_pool_mining",
                    "perpetual_experience",
                ),
            ),
            _field("activity_id", required=True, field_type="integer"),
            _field("uids", required=True, field_type="array", items={"type": "integer"}),
            _field("config_id", field_type="integer"),
            _field("asset", default=""),
            _field("amount", default=""),
            _field("subscribe_count", field_type="integer"),
            _field("pool_id", field_type="integer"),
            _field("stake_amount", default=""),
        ],
    ),
    _task(
        "quick_equity_clone",
        path="/api/v1/equities/clone",
        summary="复制权益发放",
        fields=[
            _field("env", default="1"),
            _field("apply_id", required=True, field_type="integer"),
            _field("send_at", required=True, description="UTC+8 ISO 时间。"),
            _field("audience_mode", default="original", enum=("original", "specified")),
            _field(
                "users",
                field_type="array",
                description="指定用户的 UID 或邮箱。",
                items={"type": "string"},
            ),
        ],
    ),
    _task(
        "generate_trade_volume",
        path="/api/v1/markets/trade-volume/generate",
        summary="生成交易量或交易额",
        fields=[
            _field("env", default="1"),
            _field("trade_type", default="spot", enum=("spot", "perpetual")),
            _field("market", required=True),
            _field("user_identifier", required=True),
            _field("counter_identifier", required=True),
            _field("target_type", default="volume", enum=("volume", "turnover")),
            _field("target_value", required=True),
            _field("per_trade_turnover", default="100"),
            _field("confirm_timeout", default=10, field_type="integer"),
            _field("max_trades", default=10000, field_type="integer"),
            _field("auto_top_up", default=True, field_type="boolean"),
            _field("charge_fee", default=True, field_type="boolean"),
            _field("hedge_version", default=False, field_type="boolean"),
            _field("hide", default=False, field_type="boolean"),
        ],
    ),
    _task(
        "query_notifications",
        path="/api/v1/notifications/query",
        summary="查询 Push 场景或记录",
        kind="query",
        fields=[
            _field("env", default="1"),
            _field("action", default="list_scenes", enum=("list_scenes", "list_admin_pushes", "inspect_admin_push")),
            _field("app_push_id", default=""),
        ],
    ),
    _task(
        "send_notification",
        path="/api/v1/notifications/send",
        summary="发送或重发 Push",
        fields=[
            _field("env", default="1"),
            _field(
                "action",
                required=True,
                enum=(
                    "send",
                    "test_push",
                    "scan_admin_push_templates",
                    "resend_admin_push",
                    "resend_admin_push_to_user",
                    "resend_admin_push_to_users",
                ),
            ),
            _field("identifier", default=""),
            _field("uids", field_type="array", items={"type": "integer"}),
            _field("scene", default=""),
            _field("app_push_id", default=""),
            _field("params", field_type="object"),
        ],
    ),
)


_DIRECT_WRITE_PATHS = {
    str(task["name"]): str(task["path"])
    for task in TEST_DATA_API_TASKS
    if task["kind"] == "write"
}
_DIRECT_QUERY_PATHS = {
    str(task["name"]): str(task["path"])
    for task in TEST_DATA_API_TASKS
    if task["kind"] == "query"
}


class TestDataPlatformError(RuntimeError):
    """测试造数平台调用失败。"""

    def __init__(self, message: str, *, execution_state_unknown: bool = False) -> None:
        super().__init__(message)
        self.execution_state_unknown = execution_state_unknown


class TestDataPlatformClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token.strip()
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def list_tasks(self) -> object:
        payload = await self._request_json("GET", "/api/v1/makedata/tasks")
        return self._merge_task_catalog(payload)

    async def execute(
        self,
        *,
        task: str,
        environment: str,
        parameters: dict[str, Any],
    ) -> object:
        path = _DIRECT_WRITE_PATHS.get(task)
        if path is not None:
            return await self._request_json(
                "POST",
                path,
                json_body={**parameters, "env": environment},
                write_request=True,
            )
        return await self._request_json(
            "POST",
            "/api/v1/makedata/execute",
            json_body={
                "task": task,
                "params": {
                    **parameters,
                    "env": environment,
                },
            },
            write_request=True,
        )

    async def query(
        self,
        *,
        operation: str,
        environment: str,
        parameters: dict[str, Any],
    ) -> object:
        path = _DIRECT_QUERY_PATHS.get(operation)
        if path is None:
            raise TestDataPlatformError(f"不支持的测试造数查询：{operation or '-'}。")
        return await self._request_json(
            "POST",
            path,
            json_body={**parameters, "env": environment},
        )

    async def get_request(self, run_id: str) -> object:
        normalized = run_id.strip()
        if not normalized or not normalized.isdigit() or int(normalized) <= 0:
            raise TestDataPlatformError("造数请求 ID 必须是正整数。")
        return await self._request_json(
            "GET",
            f"/api/v1/makedata/requests/{normalized}",
        )

    @staticmethod
    def _merge_task_catalog(payload: object) -> dict[str, object]:
        tasks: dict[str, object] = {
            str(task["name"]): dict(task)
            for task in TEST_DATA_API_TASKS
        }
        for item in TestDataPlatformClient._catalog_items(payload):
            name = TestDataPlatformClient._task_name(item)
            if name and name not in tasks:
                tasks[name] = item
        return {"tasks": list(tasks.values())}

    @staticmethod
    def _catalog_items(payload: object) -> list[dict[str, object]]:
        if isinstance(payload, dict):
            for key in ("tasks", "data", "items"):
                nested = payload.get(key)
                if isinstance(nested, (dict, list)):
                    items = TestDataPlatformClient._catalog_items(nested)
                    if items:
                        return items
            items: list[dict[str, object]] = []
            for key, value in payload.items():
                if not isinstance(value, dict):
                    continue
                item = dict(value)
                if not TestDataPlatformClient._task_name(item):
                    item["name"] = str(key)
                items.append(item)
            return items
        if isinstance(payload, list):
            return [
                {"name": item} if isinstance(item, str) else item
                for item in payload
                if isinstance(item, (str, dict))
            ]
        return []

    @staticmethod
    def _task_name(item: dict[str, object]) -> str:
        for key in ("name", "task", "task_name", "code", "script", "id"):
            value = str(item.get(key) or "").strip()
            if value:
                return value
        return ""

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        write_request: bool = False,
    ) -> object:
        if not self.api_token:
            raise TestDataPlatformError("测试造数平台 API Token 未配置。")
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    json=json_body,
                )
        except httpx.RequestError as exc:
            raise TestDataPlatformError(
                f"测试造数平台连接失败：{exc}",
                execution_state_unknown=write_request,
            ) from exc

        if response.status_code >= 400:
            detail = response.text.strip()[:1000]
            raise TestDataPlatformError(
                f"测试造数平台请求失败：HTTP {response.status_code} {detail}",
                execution_state_unknown=write_request and response.status_code >= 500,
            )
        try:
            return response.json()
        except ValueError as exc:
            raise TestDataPlatformError(
                "测试造数平台返回了非 JSON 响应。",
                execution_state_unknown=write_request,
            ) from exc
