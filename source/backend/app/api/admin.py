from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.business.assistant.models import AssistantFileArtifactRecord, AssistantMessageRecord, AssistantSessionRecord
from app.business.assistant.service import AssistantService
from app.business.assistant.store import SQLiteAssistantStore
from app.core.config import settings
from app.business.workspace import WorkspaceAccessError, WorkspaceAccessService
from app.core.dependencies import (
    get_assistant_service,
    get_assistant_store,
    get_auth_service,
    get_auth_store,
    get_notification_service,
    get_oa_company_domains,
    get_oa_employee_directory,
    get_permission_service,
    get_permission_store,
    get_workspace_access_service,
)
from app.integrations.vinotech_oa import OaEmployeeDirectory
from app.services.permission_service import BusinessPermissionService, UAT_ENVIRONMENT_CONTROL_TASK
from app.services.permission_store import (
    PermissionPolicyRecord,
    SQLitePermissionStore,
)
from app.schemas.assistant import AssistantContextItem, AssistantFileArtifactResponse
from app.services.auth_models import UserRecord
from app.services.auth_service import AuthService
from app.services.auth_store import SQLiteAuthStore
from app.services.notification_service import AuthNotificationService

router = APIRouter(prefix="/admin", tags=["admin"])


class SetRoleRequest(BaseModel):
    role: str


class AdminUserOrganizationResponse(BaseModel):
    organization_key: str
    organization_role: str
    is_default: bool


class AdminUserResponse(BaseModel):
    user_id: str
    email: str
    name: str | None
    avatar_url: str | None
    status: str
    role: str
    agent_access: str
    auth_provider: str
    created_at: datetime
    last_login_at: datetime
    organizations: list[AdminUserOrganizationResponse] = Field(default_factory=list)
    department_name: str | None = None
    # 用工形式展示值：全职/兼职/实习/未填写；不在 OA 在职目录时为 None（前端按离职展示）
    employment_form: str | None = None
    oa_directory_loaded: bool = False


class AdminOrganizationResponse(BaseModel):
    organization_id: str
    organization_key: str
    display_name: str
    status: str
    created_at: datetime
    updated_at: datetime


class AdminOrganizationUpsertRequest(BaseModel):
    organization_key: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=120)
    status: str = "active"


class AdminOrganizationMembershipResponse(BaseModel):
    membership_id: str
    organization_id: str
    organization_key: str
    user_id: str
    organization_role: str
    source: str
    is_default: bool
    status: str
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AdminOrganizationMembershipCreateRequest(BaseModel):
    organization_key: str = Field(min_length=1, max_length=64)
    organization_role: str = "member"
    is_default: bool = False
    status: str = "active"
    expires_at: datetime | None = None


class AdminOrganizationMembershipUpdateRequest(BaseModel):
    organization_role: str | None = None
    is_default: bool | None = None
    status: str | None = None
    expires_at: datetime | None = None


class AdminOrganizationAccessReviewRequest(BaseModel):
    review_comment: str | None = Field(default=None, max_length=500)


class AdminAgentAccessRequestResponse(BaseModel):
    request_id: str
    user_id: str
    user_email: str
    user_name: str | None = None
    reason: str
    status: str
    reviewed_by_user_id: str | None = None
    reviewed_at: datetime | None = None
    review_comment: str | None = None
    created_at: datetime


class AdminSetAgentAccessRequest(BaseModel):
    agent_access: str = Field(pattern="^(active|none)$")


class AdminOrganizationAccessRequestResponse(BaseModel):
    request_id: str
    requester_user_id: str
    source_organization_key: str | None = None
    target_organization_key: str
    reason: str
    requested_scope: str | None = None
    status: str
    reviewed_by_user_id: str | None = None
    reviewed_at: datetime | None = None
    review_comment: str | None = None
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class AdminChatOwnerResponse(BaseModel):
    user_id: str
    email: str | None = None
    name: str | None = None


class AdminChatSessionResponse(BaseModel):
    session_id: str
    title: str
    owner: AdminChatOwnerResponse
    has_active_turn: bool
    message_count: int
    last_message_preview: str | None
    created_at: datetime
    updated_at: datetime
    runtime_provider: str | None
    status: str


class AdminChatMessageResponse(BaseModel):
    message_id: str
    session_id: str
    role: str
    content: str
    created_at: datetime
    context_requests: list[AssistantContextItem] = Field(default_factory=list)
    file_artifacts: list[AssistantFileArtifactResponse] = Field(default_factory=list)


class AdminChatSessionDetailResponse(BaseModel):
    session: AdminChatSessionResponse
    messages: list[AdminChatMessageResponse]


class AdminImageUsageResponse(BaseModel):
    user_id: str
    email: str
    name: str | None = None
    role: str
    status: str
    daily_used: int
    weekly_used: int
    total_used: int


class AdminPermissionDepartmentResponse(BaseModel):
    key: str
    name: str
    parent_key: str | None = None
    path: str


class AdminPermissionDepartmentsResponse(BaseModel):
    loaded: bool
    departments: list[AdminPermissionDepartmentResponse]


class AdminPermissionPolicyDepartmentResponse(BaseModel):
    department_key: str
    department_path_snapshot: str
    include_children: bool


class AdminPermissionPolicyResponse(BaseModel):
    policy_id: str
    policy_name: str
    organization_key: str
    capability_key: str
    task_scope_type: str
    status: str
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    description: str | None = None
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime
    matched_user_count: int = 0
    departments: list[AdminPermissionPolicyDepartmentResponse] = Field(default_factory=list)
    tasks: list[str] = Field(default_factory=list)


class AdminPermissionPolicyUpsertRequest(BaseModel):
    policy_name: str | None = Field(default=None, max_length=120)
    organization_key: str = Field(min_length=1, max_length=64)
    capability_key: str = Field(default="test_data", min_length=1, max_length=80)
    department_keys: list[str] = Field(min_length=1)
    include_children: bool = True
    task_scope_type: str = "all"
    tasks: list[str] = Field(default_factory=list)
    status: str = "enabled"
    starts_at: datetime | None = None
    expires_at: datetime | None = None
    description: str | None = Field(default=None, max_length=1000)


class AdminPermissionPolicyStatusRequest(BaseModel):
    status: str


class AdminPermissionPolicyAuditResponse(BaseModel):
    audit_id: str
    policy_id: str
    action: str
    actor_user_id: str
    changes: dict[str, object]
    created_at: datetime


class AdminPermissionDecisionLogResponse(BaseModel):
    decision_id: str
    user_id: str | None
    email: str
    organization_key: str
    capability_key: str
    task_name: str | None
    allowed: bool
    reason: str
    matched_policy_id: str | None
    matched_policy_name: str | None
    department_snapshot: dict[str, object]
    session_id: str | None
    turn_id: str | None
    request_id: str | None
    decided_at: datetime


def _require_admin(
    auth_service: AuthService = Depends(get_auth_service),
    admin_session_cookie: str | None = Cookie(default=None, alias=settings.admin_session_cookie_name),
) -> UserRecord:
    if admin_session_cookie is None:
        raise HTTPException(status_code=401, detail="缺少管理员会话。")
    user = auth_service.get_current_admin(admin_session_cookie)
    if user is None:
        raise HTTPException(status_code=401, detail="管理员会话无效或已过期。")
    if user.status != "active":
        raise HTTPException(status_code=403, detail="管理员账号当前不可用。")
    return user


def _serialize_user_organization(membership: object) -> AdminUserOrganizationResponse:
    return AdminUserOrganizationResponse(
        organization_key=str(getattr(membership, "organization_key")),
        organization_role=str(getattr(membership, "organization_role")),
        is_default=bool(getattr(membership, "is_default")),
    )


EMPLOYMENT_FORM_LABELS = {1: "全职", 2: "兼职", 3: "实习"}


def _serialize(
    user: UserRecord,
    memberships: list[object] | None = None,
    department_name: str | None = None,
    employment_form: str | None = None,
    oa_directory_loaded: bool = False,
) -> AdminUserResponse:
    return AdminUserResponse(
        user_id=user.user_id,
        email=user.email,
        name=user.name,
        avatar_url=user.avatar_url,
        status=user.status,
        role=user.role,
        agent_access=user.agent_access,
        auth_provider=user.auth_provider,
        created_at=user.created_at,
        last_login_at=user.last_login_at,
        organizations=[
            _serialize_user_organization(membership)
            for membership in (memberships or [])
            if getattr(membership, "status", None) == "active"
        ],
        department_name=department_name,
        employment_form=employment_form,
        oa_directory_loaded=oa_directory_loaded,
    )


def _serialize_organization(organization: object) -> AdminOrganizationResponse:
    return AdminOrganizationResponse(
        organization_id=str(getattr(organization, "organization_id")),
        organization_key=str(getattr(organization, "organization_key")),
        display_name=str(getattr(organization, "display_name")),
        status=str(getattr(organization, "status")),
        created_at=getattr(organization, "created_at"),
        updated_at=getattr(organization, "updated_at"),
    )


def _serialize_membership(membership: object) -> AdminOrganizationMembershipResponse:
    return AdminOrganizationMembershipResponse(
        membership_id=str(getattr(membership, "membership_id")),
        organization_id=str(getattr(membership, "organization_id")),
        organization_key=str(getattr(membership, "organization_key")),
        user_id=str(getattr(membership, "user_id")),
        organization_role=str(getattr(membership, "organization_role")),
        source=str(getattr(membership, "source")),
        is_default=bool(getattr(membership, "is_default")),
        status=str(getattr(membership, "status")),
        expires_at=getattr(membership, "expires_at"),
        created_at=getattr(membership, "created_at"),
        updated_at=getattr(membership, "updated_at"),
    )


def _serialize_access_request(record: object) -> AdminOrganizationAccessRequestResponse:
    return AdminOrganizationAccessRequestResponse(
        request_id=str(getattr(record, "request_id")),
        requester_user_id=str(getattr(record, "requester_user_id")),
        source_organization_key=getattr(record, "source_organization_key"),
        target_organization_key=str(getattr(record, "target_organization_key")),
        reason=str(getattr(record, "reason")),
        requested_scope=getattr(record, "requested_scope"),
        status=str(getattr(record, "status")),
        reviewed_by_user_id=getattr(record, "reviewed_by_user_id"),
        reviewed_at=getattr(record, "reviewed_at"),
        review_comment=getattr(record, "review_comment"),
        expires_at=getattr(record, "expires_at"),
        created_at=getattr(record, "created_at"),
        updated_at=getattr(record, "updated_at"),
    )


def _serialize_chat_owner(user: UserRecord | None, fallback_user_id: str) -> AdminChatOwnerResponse:
    return AdminChatOwnerResponse(
        user_id=user.user_id if user is not None else fallback_user_id,
        email=user.email if user is not None else None,
        name=user.name if user is not None else None,
    )


def _serialize_chat_session(
    session: AssistantSessionRecord,
    owner: UserRecord | None,
    *,
    has_active_turn: bool = False,
) -> AdminChatSessionResponse:
    return AdminChatSessionResponse(
        session_id=session.session_id,
        title=session.title,
        owner=_serialize_chat_owner(owner, session.user_id),
        has_active_turn=has_active_turn,
        message_count=session.message_count,
        last_message_preview=session.last_message_preview,
        created_at=session.created_at,
        updated_at=session.updated_at,
        runtime_provider=session.runtime_provider,
        status=session.status,
    )


def _serialize_context_item(item: object) -> AssistantContextItem:
    return AssistantContextItem(
        context_key=str(getattr(item, "context_key")),
        label=str(getattr(item, "label")),
        content=getattr(item, "content"),
        source_type=str(getattr(item, "source_type")),
        source_uri=getattr(item, "source_uri"),
        metadata=dict(getattr(item, "metadata")),
    )


def _serialize_chat_message(
    message: AssistantMessageRecord,
    context_requests: list | None = None,
    file_artifacts: list[AssistantFileArtifactResponse] | None = None,
) -> AdminChatMessageResponse:
    return AdminChatMessageResponse(
        message_id=message.message_id,
        session_id=message.session_id,
        role=message.role,
        content=message.content,
        created_at=message.created_at,
        context_requests=[_serialize_context_item(item) for item in (context_requests or [])],
        file_artifacts=file_artifacts or [],
    )


def _serialize_chat_file_artifact(
    session_id: str,
    artifact: AssistantFileArtifactRecord,
) -> AssistantFileArtifactResponse:
    download_url = None
    if artifact.storage_status != "missing":
        download_url = f"/api/admin/assistant/sessions/{session_id}/files/{artifact.artifact_id}"
    return AssistantFileArtifactResponse(
        artifact_id=artifact.artifact_id,
        display_path=artifact.display_path,
        filename=artifact.filename,
        mime_type=artifact.mime_type,
        size_bytes=artifact.size_bytes,
        storage_status=artifact.storage_status,
        download_url=download_url,
    )


def _serialize_permission_policy(
    policy: PermissionPolicyRecord,
    *,
    matched_user_count: int = 0,
) -> AdminPermissionPolicyResponse:
    return AdminPermissionPolicyResponse(
        policy_id=policy.policy_id,
        policy_name=policy.policy_name,
        organization_key=policy.organization_key,
        capability_key=policy.capability_key,
        task_scope_type=policy.task_scope_type,
        status=policy.status,
        starts_at=policy.starts_at,
        expires_at=policy.expires_at,
        description=policy.description,
        created_by=policy.created_by,
        updated_by=policy.updated_by,
        created_at=policy.created_at,
        updated_at=policy.updated_at,
        matched_user_count=matched_user_count,
        departments=[
            AdminPermissionPolicyDepartmentResponse(
                department_key=item.department_key,
                department_path_snapshot=item.department_path_snapshot,
                include_children=item.include_children,
            )
            for item in policy.departments
        ],
        tasks=policy.tasks,
    )


async def _serialize_user_with_oa(
    user: UserRecord,
    workspace_access: WorkspaceAccessService,
    oa_directory: OaEmployeeDirectory,
) -> AdminUserResponse:
    snapshot = await oa_directory.snapshot()
    person = snapshot.people.get(user.email.strip().lower())
    return _serialize(
        user,
        workspace_access.store.list_user_memberships(user.user_id),
        department_name=person.department_name if person else None,
        employment_form=EMPLOYMENT_FORM_LABELS.get(person.employment_form, "未填写") if person else None,
        oa_directory_loaded=snapshot.loaded,
    )


@router.get("/users", response_model=list[AdminUserResponse])
async def list_users(
    status: str | None = None,
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    workspace_access: WorkspaceAccessService = Depends(get_workspace_access_service),
    oa_directory: OaEmployeeDirectory = Depends(get_oa_employee_directory),
    _admin: UserRecord = Depends(_require_admin),
) -> list[AdminUserResponse]:
    users = auth_store.list_users(status=status)
    snapshot = await oa_directory.snapshot()
    company_domains = get_oa_company_domains()
    serialized: list[AdminUserResponse] = []
    for user in users:
        person = snapshot.people.get(user.email.strip().lower())
        # 公司域名且不在 OA 在职目录视为离职，自动封禁（跳过操作者本人，避免管理员把自己锁在外面）
        if (
            user.status == "active"
            and user.user_id != _admin.user_id
            and snapshot.is_left_employee(user.email, company_domains)
        ):
            user = auth_store.update_user_status(user.user_id, "blocked") or user
        serialized.append(
            _serialize(
                user,
                workspace_access.store.list_user_memberships(user.user_id),
                department_name=person.department_name if person else None,
                employment_form=EMPLOYMENT_FORM_LABELS.get(person.employment_form, "未填写") if person else None,
                oa_directory_loaded=snapshot.loaded,
            )
        )
    return serialized


def _grant_agent_access(
    auth_store: SQLiteAuthStore,
    workspace_access: WorkspaceAccessService,
    notification_service: AuthNotificationService,
    user: UserRecord,
    *,
    notify: bool,
) -> UserRecord:
    """开通 Agent 权限：置权限状态、加入默认组织，并按需通知用户。"""
    granted = auth_store.update_user_agent_access(user.user_id, "active") or user
    workspace_access.store.upsert_user_membership(
        user_id=user.user_id,
        organization_key=settings.default_organization_key,
        source="agent_access_approved",
        status="active",
    )
    if notify:
        notification_service.notify_agent_access_approved(granted)
    return granted


def _serialize_agent_access_request(record: object, requester: UserRecord | None) -> AdminAgentAccessRequestResponse:
    return AdminAgentAccessRequestResponse(
        request_id=str(getattr(record, "request_id")),
        user_id=str(getattr(record, "user_id")),
        user_email=requester.email if requester else "",
        user_name=requester.name if requester else None,
        reason=str(getattr(record, "reason")),
        status=str(getattr(record, "status")),
        reviewed_by_user_id=getattr(record, "reviewed_by_user_id"),
        reviewed_at=getattr(record, "reviewed_at"),
        review_comment=getattr(record, "review_comment"),
        created_at=getattr(record, "created_at"),
    )


@router.get("/agent-access/requests", response_model=list[AdminAgentAccessRequestResponse])
def list_agent_access_requests(
    status: str | None = Query(default="pending"),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    _admin: UserRecord = Depends(_require_admin),
) -> list[AdminAgentAccessRequestResponse]:
    if status not in {None, "", "pending", "approved", "rejected"}:
        raise HTTPException(status_code=400, detail="申请状态只能是 pending、approved 或 rejected。")
    requests = auth_store.list_agent_access_requests(status=status or None)
    requesters = {
        user.user_id: user
        for user in auth_store.list_users_by_ids([record.user_id for record in requests])
    }
    return [
        _serialize_agent_access_request(record, requesters.get(record.user_id))
        for record in requests
    ]


@router.post("/agent-access/requests/{request_id}/approve", response_model=AdminAgentAccessRequestResponse)
def approve_agent_access_request(
    request_id: str,
    body: AdminOrganizationAccessReviewRequest,
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    auth_service: AuthService = Depends(get_auth_service),
    workspace_access: WorkspaceAccessService = Depends(get_workspace_access_service),
    notification_service: AuthNotificationService = Depends(get_notification_service),
    admin: UserRecord = Depends(_require_admin),
) -> AdminAgentAccessRequestResponse:
    try:
        request, user = auth_service.review_agent_access_request(
            request_id=request_id,
            reviewer_user_id=admin.user_id,
            approved=True,
            review_comment=body.review_comment,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    _grant_agent_access(auth_store, workspace_access, notification_service, user, notify=True)
    return _serialize_agent_access_request(request, user)


@router.post("/agent-access/requests/{request_id}/reject", response_model=AdminAgentAccessRequestResponse)
def reject_agent_access_request(
    request_id: str,
    body: AdminOrganizationAccessReviewRequest,
    auth_service: AuthService = Depends(get_auth_service),
    admin: UserRecord = Depends(_require_admin),
) -> AdminAgentAccessRequestResponse:
    try:
        request, user = auth_service.review_agent_access_request(
            request_id=request_id,
            reviewer_user_id=admin.user_id,
            approved=False,
            review_comment=body.review_comment,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_agent_access_request(request, user)


@router.post("/users/{user_id}/agent-access", response_model=AdminUserResponse)
async def set_user_agent_access(
    user_id: str,
    body: AdminSetAgentAccessRequest,
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    auth_service: AuthService = Depends(get_auth_service),
    workspace_access: WorkspaceAccessService = Depends(get_workspace_access_service),
    notification_service: AuthNotificationService = Depends(get_notification_service),
    oa_directory: OaEmployeeDirectory = Depends(get_oa_employee_directory),
    _admin: UserRecord = Depends(_require_admin),
) -> AdminUserResponse:
    try:
        user = auth_service.set_user_agent_access(user_id, body.agent_access)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if body.agent_access == "active":
        user = _grant_agent_access(auth_store, workspace_access, notification_service, user, notify=False)
    return await _serialize_user_with_oa(user, workspace_access, oa_directory)


@router.post("/users/{user_id}/block", response_model=AdminUserResponse)
async def block_user(
    user_id: str,
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    workspace_access: WorkspaceAccessService = Depends(get_workspace_access_service),
    oa_directory: OaEmployeeDirectory = Depends(get_oa_employee_directory),
    admin: UserRecord = Depends(_require_admin),
) -> AdminUserResponse:
    user = auth_store.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在。")
    if user.user_id == admin.user_id:
        raise HTTPException(status_code=400, detail="不能封禁自己。")
    updated = auth_store.update_user_status(user_id, "blocked")
    blocked_user = updated or user
    return await _serialize_user_with_oa(blocked_user, workspace_access, oa_directory)


@router.patch("/users/{user_id}/role", response_model=AdminUserResponse)
async def set_user_role(
    user_id: str,
    body: SetRoleRequest,
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    workspace_access: WorkspaceAccessService = Depends(get_workspace_access_service),
    oa_directory: OaEmployeeDirectory = Depends(get_oa_employee_directory),
    _admin: UserRecord = Depends(_require_admin),
) -> AdminUserResponse:
    if body.role not in {"member", "admin"}:
        raise HTTPException(status_code=400, detail="角色只能是 member 或 admin。")
    user = auth_store.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在。")
    updated = auth_store.update_user_role(user_id, body.role)
    role_user = updated or user
    return await _serialize_user_with_oa(role_user, workspace_access, oa_directory)


@router.get("/organizations", response_model=list[AdminOrganizationResponse])
def list_organizations(
    workspace_access: WorkspaceAccessService = Depends(get_workspace_access_service),
    _admin: UserRecord = Depends(_require_admin),
) -> list[AdminOrganizationResponse]:
    return [_serialize_organization(organization) for organization in workspace_access.store.list_organizations()]


@router.post("/organizations", response_model=AdminOrganizationResponse, status_code=201)
def upsert_organization(
    body: AdminOrganizationUpsertRequest,
    workspace_access: WorkspaceAccessService = Depends(get_workspace_access_service),
    _admin: UserRecord = Depends(_require_admin),
) -> AdminOrganizationResponse:
    if body.status not in {"active", "disabled"}:
        raise HTTPException(status_code=400, detail="组织状态只能是 active 或 disabled。")
    try:
        organization = workspace_access.store.upsert_organization(
            organization_key=body.organization_key,
            display_name=body.display_name,
            status=body.status,
        )
    except WorkspaceAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_organization(organization)


@router.get(
    "/users/{user_id}/organization-memberships",
    response_model=list[AdminOrganizationMembershipResponse],
)
def list_user_organization_memberships(
    user_id: str,
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    workspace_access: WorkspaceAccessService = Depends(get_workspace_access_service),
    _admin: UserRecord = Depends(_require_admin),
) -> list[AdminOrganizationMembershipResponse]:
    if auth_store.get_user_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail="用户不存在。")
    return [_serialize_membership(item) for item in workspace_access.store.list_user_memberships(user_id)]


@router.post(
    "/users/{user_id}/organization-memberships",
    response_model=AdminOrganizationMembershipResponse,
    status_code=201,
)
def create_user_organization_membership(
    user_id: str,
    body: AdminOrganizationMembershipCreateRequest,
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    workspace_access: WorkspaceAccessService = Depends(get_workspace_access_service),
    _admin: UserRecord = Depends(_require_admin),
) -> AdminOrganizationMembershipResponse:
    if auth_store.get_user_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail="用户不存在。")
    try:
        membership = workspace_access.store.upsert_user_membership(
            user_id=user_id,
            organization_key=body.organization_key,
            organization_role=body.organization_role,
            source="admin_assigned",
            is_default=body.is_default,
            status=body.status,
            expires_at=body.expires_at,
        )
    except WorkspaceAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_membership(membership)


@router.patch(
    "/users/{user_id}/organization-memberships/{membership_id}",
    response_model=AdminOrganizationMembershipResponse,
)
def update_user_organization_membership(
    user_id: str,
    membership_id: str,
    body: AdminOrganizationMembershipUpdateRequest,
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    workspace_access: WorkspaceAccessService = Depends(get_workspace_access_service),
    _admin: UserRecord = Depends(_require_admin),
) -> AdminOrganizationMembershipResponse:
    if auth_store.get_user_by_id(user_id) is None:
        raise HTTPException(status_code=404, detail="用户不存在。")
    existing_membership = workspace_access.store.get_membership(membership_id)
    if existing_membership is None or existing_membership.user_id != user_id:
        raise HTTPException(status_code=404, detail="组织成员关系不存在。")
    try:
        membership = workspace_access.store.update_user_membership(
            membership_id=membership_id,
            organization_role=body.organization_role,
            is_default=body.is_default,
            status=body.status,
            expires_at=body.expires_at,
        )
    except WorkspaceAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if membership is None:
        raise HTTPException(status_code=404, detail="组织成员关系不存在。")
    return _serialize_membership(membership)


@router.get("/organization-access-requests", response_model=list[AdminOrganizationAccessRequestResponse])
def list_organization_access_requests(
    status: str | None = Query(default=None, max_length=40),
    workspace_access: WorkspaceAccessService = Depends(get_workspace_access_service),
    _admin: UserRecord = Depends(_require_admin),
) -> list[AdminOrganizationAccessRequestResponse]:
    return [
        _serialize_access_request(record)
        for record in workspace_access.store.list_access_requests(status=status)
    ]


@router.post(
    "/organization-access-requests/{request_id}/approve",
    response_model=AdminOrganizationAccessRequestResponse,
)
def approve_organization_access_request(
    request_id: str,
    body: AdminOrganizationAccessReviewRequest,
    workspace_access: WorkspaceAccessService = Depends(get_workspace_access_service),
    admin: UserRecord = Depends(_require_admin),
) -> AdminOrganizationAccessRequestResponse:
    try:
        record = workspace_access.store.review_access_request(
            request_id=request_id,
            reviewer_user_id=admin.user_id,
            status="approved",
            review_comment=body.review_comment,
        )
    except WorkspaceAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_access_request(record)


@router.post(
    "/organization-access-requests/{request_id}/reject",
    response_model=AdminOrganizationAccessRequestResponse,
)
def reject_organization_access_request(
    request_id: str,
    body: AdminOrganizationAccessReviewRequest,
    workspace_access: WorkspaceAccessService = Depends(get_workspace_access_service),
    admin: UserRecord = Depends(_require_admin),
) -> AdminOrganizationAccessRequestResponse:
    try:
        record = workspace_access.store.review_access_request(
            request_id=request_id,
            reviewer_user_id=admin.user_id,
            status="rejected",
            review_comment=body.review_comment,
        )
    except WorkspaceAccessError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_access_request(record)


@router.get("/permissions/departments", response_model=AdminPermissionDepartmentsResponse)
async def list_permission_departments(
    oa_directory: OaEmployeeDirectory = Depends(get_oa_employee_directory),
    _admin: UserRecord = Depends(_require_admin),
) -> AdminPermissionDepartmentsResponse:
    snapshot = await oa_directory.snapshot()
    departments = sorted(snapshot.departments.values(), key=lambda item: item.path)
    return AdminPermissionDepartmentsResponse(
        loaded=snapshot.loaded,
        departments=[
            AdminPermissionDepartmentResponse(
                key=item.key,
                name=item.name,
                parent_key=item.parent_key,
                path=item.path,
            )
            for item in departments
        ],
    )


@router.get("/permissions/task-catalog")
def list_permission_task_catalog(
    _admin: UserRecord = Depends(_require_admin),
) -> list[dict[str, object]]:
    return [
        {
            "task_name": UAT_ENVIRONMENT_CONTROL_TASK,
            "display_name": "UAT 环境控制",
            "description": "授权后可使用系统当前开放的 UAT 环境和造数任务。",
        }
    ]


@router.get("/permissions/policies", response_model=list[AdminPermissionPolicyResponse])
async def list_permission_policies(
    status: str | None = Query(default=None),
    permission_store: SQLitePermissionStore = Depends(get_permission_store),
    permission_service: BusinessPermissionService = Depends(get_permission_service),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    _admin: UserRecord = Depends(_require_admin),
) -> list[AdminPermissionPolicyResponse]:
    if status not in {None, "", "enabled", "disabled"}:
        raise HTTPException(status_code=400, detail="策略状态只能是 enabled 或 disabled。")
    users = auth_store.list_users()
    policies = [
        policy
        for policy in permission_store.list_policies(status=status or None)
        if policy.status in {"enabled", "disabled"}
    ]
    return [
        _serialize_permission_policy(
            policy,
            matched_user_count=await permission_service.count_policy_matches(
                policy_id=policy.policy_id,
                users=users,
            ),
        )
        for policy in policies
    ]


async def _save_permission_policy(
    *,
    body: AdminPermissionPolicyUpsertRequest,
    policy_id: str | None,
    admin: UserRecord,
    permission_service: BusinessPermissionService,
) -> AdminPermissionPolicyResponse:
    if body.status not in {"enabled", "disabled"}:
        raise HTTPException(status_code=400, detail="策略状态只能是 enabled 或 disabled。")
    if body.capability_key != "test_data":
        raise HTTPException(status_code=400, detail="当前仅支持测试造数能力。")
    if body.task_scope_type != "all":
        raise HTTPException(status_code=400, detail="当前仅支持 UAT 环境控制，暂不按平台内部任务拆分权限。")
    try:
        policy = await permission_service.build_policy(
            policy_id=policy_id,
            policy_name=body.policy_name,
            organization_key=body.organization_key,
            capability_key=body.capability_key,
            department_keys=body.department_keys,
            include_children=body.include_children,
            task_scope_type=body.task_scope_type,
            tasks=body.tasks,
            status=body.status,
            starts_at=body.starts_at,
            expires_at=body.expires_at,
            description=body.description,
            actor_user_id=admin.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_permission_policy(policy)


@router.post("/permissions/policies", response_model=AdminPermissionPolicyResponse, status_code=201)
async def create_permission_policy(
    body: AdminPermissionPolicyUpsertRequest,
    permission_service: BusinessPermissionService = Depends(get_permission_service),
    admin: UserRecord = Depends(_require_admin),
) -> AdminPermissionPolicyResponse:
    return await _save_permission_policy(
        body=body,
        policy_id=None,
        admin=admin,
        permission_service=permission_service,
    )


@router.patch("/permissions/policies/{policy_id}", response_model=AdminPermissionPolicyResponse)
async def update_permission_policy(
    policy_id: str,
    body: AdminPermissionPolicyUpsertRequest,
    permission_store: SQLitePermissionStore = Depends(get_permission_store),
    permission_service: BusinessPermissionService = Depends(get_permission_service),
    admin: UserRecord = Depends(_require_admin),
) -> AdminPermissionPolicyResponse:
    existing = permission_store.get_policy(policy_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="权限策略不存在。")
    if existing.status == "deleted":
        raise HTTPException(status_code=410, detail="权限策略已删除。")
    return await _save_permission_policy(
        body=body,
        policy_id=policy_id,
        admin=admin,
        permission_service=permission_service,
    )


@router.patch("/permissions/policies/{policy_id}/status", response_model=AdminPermissionPolicyResponse)
def update_permission_policy_status(
    policy_id: str,
    body: AdminPermissionPolicyStatusRequest,
    permission_store: SQLitePermissionStore = Depends(get_permission_store),
    admin: UserRecord = Depends(_require_admin),
) -> AdminPermissionPolicyResponse:
    try:
        policy = permission_store.update_policy_status(
            policy_id=policy_id,
            status=body.status,
            actor_user_id=admin.user_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if policy is None:
        raise HTTPException(status_code=404, detail="权限策略不存在或已删除。")
    return _serialize_permission_policy(policy)


@router.delete("/permissions/policies/{policy_id}", status_code=204)
def delete_permission_policy(
    policy_id: str,
    permission_store: SQLitePermissionStore = Depends(get_permission_store),
    admin: UserRecord = Depends(_require_admin),
) -> None:
    policy = permission_store.delete_policy(policy_id=policy_id, actor_user_id=admin.user_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="权限策略不存在。")


@router.get(
    "/permissions/policies/{policy_id}/audit",
    response_model=list[AdminPermissionPolicyAuditResponse],
)
def list_permission_policy_audit(
    policy_id: str,
    permission_store: SQLitePermissionStore = Depends(get_permission_store),
    _admin: UserRecord = Depends(_require_admin),
) -> list[AdminPermissionPolicyAuditResponse]:
    if permission_store.get_policy(policy_id) is None:
        raise HTTPException(status_code=404, detail="权限策略不存在。")
    return [
        AdminPermissionPolicyAuditResponse(
            audit_id=item.audit_id,
            policy_id=item.policy_id,
            action=item.action,
            actor_user_id=item.actor_user_id,
            changes=item.changes,
            created_at=item.created_at,
        )
        for item in permission_store.list_policy_audits(policy_id)
    ]


@router.get("/permissions/decision-logs", response_model=list[AdminPermissionDecisionLogResponse])
def list_permission_decision_logs(
    limit: int = Query(default=100, ge=1, le=500),
    permission_store: SQLitePermissionStore = Depends(get_permission_store),
    _admin: UserRecord = Depends(_require_admin),
) -> list[AdminPermissionDecisionLogResponse]:
    return [AdminPermissionDecisionLogResponse(**item) for item in permission_store.list_decision_logs(limit=limit)]


@router.get("/permissions/check")
async def check_permission_user(
    user_id: str = Query(min_length=1),
    organization_key: str = Query(min_length=1),
    task_name: str | None = Query(default=None),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    permission_service: BusinessPermissionService = Depends(get_permission_service),
    workspace_access: WorkspaceAccessService = Depends(get_workspace_access_service),
    _admin: UserRecord = Depends(_require_admin),
) -> dict[str, object]:
    user = auth_store.get_user_by_id(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="用户不存在。")
    inspected = await permission_service.inspect_user(
        user=user,
        organization_key=organization_key,
        task_name=task_name,
    )
    decision = inspected["decision"]
    policies = inspected["matched_policies"]
    memberships = workspace_access.store.list_user_memberships(user.user_id)
    return {
        "user": {
            "user_id": user.user_id,
            "email": user.email,
            "name": user.name,
            "status": user.status,
            "agent_access": user.agent_access,
            "role": user.role,
        },
        "organization_key": organization_key.strip().lower(),
        "organization_memberships": [
            {
                "organization_key": item.organization_key,
                "organization_role": item.organization_role,
                "status": item.status,
            }
            for item in memberships
        ],
        "oa_directory_loaded": inspected["oa_directory_loaded"],
        "department_snapshot": inspected["department_snapshot"],
        "matched_policies": [_serialize_permission_policy(policy).model_dump() for policy in policies],
        "decision": decision.to_dict(),
    }


@router.get("/assistant/sessions", response_model=list[AdminChatSessionResponse])
def list_chat_sessions(
    query: str | None = Query(default=None, max_length=120),
    user_id: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    assistant_store: SQLiteAssistantStore = Depends(get_assistant_store),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    _admin: UserRecord = Depends(_require_admin),
) -> list[AdminChatSessionResponse]:
    user_ids = [user_id] if user_id else None

    sessions = assistant_store.list_all_sessions(query=query, user_ids=user_ids, limit=limit, offset=offset)
    running_session_ids = assistant_store.list_running_turn_session_ids(
        [session.session_id for session in sessions]
    )
    owners = {user.user_id: user for user in auth_store.list_users_by_ids([session.user_id for session in sessions])}
    return [
        _serialize_chat_session(
            session,
            owners.get(session.user_id),
            has_active_turn=session.session_id in running_session_ids,
        )
        for session in sessions
    ]


@router.get("/assistant/sessions/{session_id}", response_model=AdminChatSessionDetailResponse)
def get_chat_session(
    session_id: str,
    assistant_store: SQLiteAssistantStore = Depends(get_assistant_store),
    assistant_service: AssistantService = Depends(get_assistant_service),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    _admin: UserRecord = Depends(_require_admin),
) -> AdminChatSessionDetailResponse:
    session = assistant_store.get_session_by_id(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在。")

    owner = auth_store.get_user_by_id(session.user_id)
    messages = assistant_store.list_messages_by_session_id(session.session_id, session.user_id)
    context_requests_by_message = assistant_store.list_context_requests_by_session_id(session.session_id)
    file_artifacts_by_message: dict[str, list[AssistantFileArtifactResponse]] = {}
    for artifact in assistant_service.ensure_session_file_artifacts_by_session(session):
        if artifact.message_id is None:
            continue
        file_artifacts_by_message.setdefault(artifact.message_id, []).append(
            _serialize_chat_file_artifact(session.session_id, artifact)
        )
    return AdminChatSessionDetailResponse(
        session=_serialize_chat_session(
            session,
            owner,
            has_active_turn=assistant_store.get_active_turn(session.session_id, session.user_id) is not None,
        ),
        messages=[
            _serialize_chat_message(
                message,
                context_requests_by_message.get(message.message_id, []),
                file_artifacts_by_message.get(message.message_id, []),
            )
            for message in messages
        ],
    )


@router.get("/assistant/sessions/{session_id}/files/{artifact_id}")
def get_chat_file_artifact(
    session_id: str,
    artifact_id: str,
    assistant_service: AssistantService = Depends(get_assistant_service),
    _admin: UserRecord = Depends(_require_admin),
) -> FileResponse:
    download = assistant_service.get_admin_file_artifact_download(session_id, artifact_id)
    if download is None:
        raise HTTPException(status_code=404, detail="附件不存在。")
    return FileResponse(
        download.file_path,
        media_type=download.mime_type,
        filename=download.filename,
        content_disposition_type="inline",
    )


@router.get("/assistant/uploads/images/{image_id}")
def get_chat_uploaded_image(
    image_id: str,
    assistant_service: AssistantService = Depends(get_assistant_service),
    _admin: UserRecord = Depends(_require_admin),
) -> FileResponse:
    try:
        image = assistant_service.get_uploaded_image_file_for_admin(
            image_id=image_id,
            upload_root=settings.assistant_image_upload_root,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if image is None:
        raise HTTPException(status_code=404, detail="图片不存在。")

    return FileResponse(
        image.file_path,
        media_type=image.mime_type,
        filename=image.file_name,
        content_disposition_type="inline",
    )


@router.get("/image-generation/usage", response_model=list[AdminImageUsageResponse])
def list_image_generation_usage(
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    assistant_store: SQLiteAssistantStore = Depends(get_assistant_store),
    _admin: UserRecord = Depends(_require_admin),
) -> list[AdminImageUsageResponse]:
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    summary = assistant_store.image_generation_usage_summary_by_user(day_start=day_start, week_start=week_start)
    rows = [
        AdminImageUsageResponse(
            user_id=user.user_id,
            email=user.email,
            name=user.name,
            role=user.role,
            status=user.status,
            daily_used=summary.get(user.user_id, {"total": 0, "daily": 0, "weekly": 0})["daily"],
            weekly_used=summary.get(user.user_id, {"total": 0, "daily": 0, "weekly": 0})["weekly"],
            total_used=summary.get(user.user_id, {"total": 0, "daily": 0, "weekly": 0})["total"],
        )
        for user in auth_store.list_users()
    ]
    rows.sort(key=lambda item: (-item.total_used, -item.weekly_used, -item.daily_used, item.email))
    return rows
