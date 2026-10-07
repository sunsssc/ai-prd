from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AssistantGitScopeResponse(BaseModel):
    repo_full_name: str
    strategy: str
    selector_type: str | None = None
    selector_value: str | None = None
    resolved_sha: str | None = None


class AssistantSessionResponse(BaseModel):
    session_id: str
    title: str
    message_count: int
    knowledge_scope_ids: list[str] = Field(default_factory=list)
    last_message_preview: str | None = None
    created_at: datetime
    updated_at: datetime
    runtime_provider: str | None = None
    runtime_session_id: str | None = None
    runtime_working_directory: str | None = None
    active_organization_key: str | None = None
    runtime_workspace_profile: dict[str, object] | None = None
    status: str = "active"
    has_active_turn: bool = False
    is_favorited: bool = False
    git_scopes: list[AssistantGitScopeResponse] = Field(default_factory=list)


class AssistantSessionFavoriteResponse(BaseModel):
    session_id: str
    is_favorited: bool


class AssistantMessageResponse(BaseModel):
    message_id: str
    session_id: str
    turn_id: str | None = None
    role: str
    content: str
    created_at: datetime
    feedback: str | None = None
    citations: list["AssistantCitationResponse"] = Field(default_factory=list)
    context_requests: list["AssistantContextItem"] = Field(default_factory=list)
    file_artifacts: list["AssistantFileArtifactResponse"] = Field(default_factory=list)
    git_scopes: list[AssistantGitScopeResponse] = Field(default_factory=list)
    author: "AssistantShareUserResponse | None" = None


class AssistantTurnResponse(BaseModel):
    turn_id: str
    session_id: str
    user_message_id: str
    assistant_message_id: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    status: str
    error_message: str | None = None


class AssistantTimelineNoticeResponse(BaseModel):
    event_id: str
    turn_id: str
    kind: str
    message: str
    created_at: datetime
    accepted: int = 0
    blocked: int = 0
    window: str | None = None
    used: int | None = None
    limit: int | None = None
    resets_at: datetime | None = None


class AssistantTestDataPlanResponse(BaseModel):
    plan_id: str
    prepared_turn_id: str
    executed_turn_id: str | None = None
    task_name: str
    environment: str
    risk_level: str
    summary: str
    status: str
    upstream_run_id: str | None = None
    upstream_status: str | None = None
    result: dict[str, object] | None = None
    error_message: str | None = None
    created_at: datetime
    expires_at: datetime
    executed_at: datetime | None = None
    can_confirm: bool = False
    confirmation_phrase: str | None = None
    has_secret: bool = False


class AssistantTestDataSecretResponse(BaseModel):
    plan_id: str
    secret: dict[str, object]


class AssistantMcpApprovalResponse(BaseModel):
    approval_id: str
    session_id: str
    turn_id: str
    server_name: str
    tool_name: str | None = None
    message: str
    description: str
    arguments: Any = None
    status: Literal["pending", "approved", "declined", "expired"]
    created_at: datetime
    expires_at: datetime
    resolved_at: datetime | None = None
    resolved_by: str | None = None


class AssistantMcpApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approved", "declined"]


class AssistantSessionDetailResponse(BaseModel):
    session: AssistantSessionResponse
    messages: list[AssistantMessageResponse]
    timeline_notices: list[AssistantTimelineNoticeResponse] = Field(default_factory=list)
    test_data_plans: list[AssistantTestDataPlanResponse] = Field(default_factory=list)
    mcp_approvals: list[AssistantMcpApprovalResponse] = Field(default_factory=list)
    mounts: list["AssistantContextItem"] = Field(default_factory=list)
    active_turn: AssistantTurnResponse | None = None
    active_turn_requested_by: "AssistantShareUserResponse | None" = None
    latest_turn: AssistantTurnResponse | None = None
    latest_turn_trace: dict[str, object] | None = None


class AssistantShareUserResponse(BaseModel):
    user_id: str
    name: str | None = None
    email: str
    avatar_url: str | None = None


class AssistantShareRequest(BaseModel):
    share_type: str
    visibility: str = "anyone"
    member_user_ids: list[str] = Field(default_factory=list)

    @field_validator("share_type")
    @classmethod
    def validate_share_type(cls, value: str) -> str:
        if value not in {"public", "members"}:
            raise ValueError("共享类型只能是 public 或 members。")
        return value

    @field_validator("visibility")
    @classmethod
    def validate_visibility(cls, value: str) -> str:
        if value not in {"agent", "registered", "anyone"}:
            raise ValueError("可见范围只能是 agent、registered 或 anyone。")
        return value

    @field_validator("member_user_ids")
    @classmethod
    def normalize_member_user_ids(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip() for item in value if item.strip()))


class AssistantShareResponse(BaseModel):
    share_id: str
    session_id: str
    share_type: str
    visibility: str = "anyone"
    share_token: str
    share_url: str
    members: list[AssistantShareUserResponse] = Field(default_factory=list)
    created_at: datetime


class AssistantSharedSessionSummaryResponse(BaseModel):
    share_id: str
    share_type: str
    visibility: str = "anyone"
    share_token: str
    share_url: str
    owner: AssistantShareUserResponse
    members: list[AssistantShareUserResponse] = Field(default_factory=list)
    session: AssistantSessionResponse
    comment_count: int
    created_at: datetime


class AssistantSharedSessionDetailResponse(BaseModel):
    share: AssistantSharedSessionSummaryResponse
    messages: list[AssistantMessageResponse]
    timeline_notices: list[AssistantTimelineNoticeResponse] = Field(default_factory=list)
    active_turn: AssistantTurnResponse | None = None
    pending_follow_ups: list["AssistantSharedFollowUpResponse"] = Field(default_factory=list)
    failed_follow_ups: list["AssistantSharedFollowUpResponse"] = Field(default_factory=list)


class AssistantSharedFollowUpRequest(BaseModel):
    message: str = Field(min_length=1, max_length=12000)

    @field_validator("message", mode="before")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("追问不能为空。")
        return normalized


class AssistantSharedFollowUpResponse(BaseModel):
    request_id: str
    session_id: str
    content: str
    status: str
    requested_by: AssistantShareUserResponse
    queue_position: int = 0
    turn_id: str | None = None
    error_message: str | None = None
    requires_runtime_rebuild: bool = False
    can_rebuild_runtime: bool = False
    created_at: datetime


class AssistantCommentResponse(BaseModel):
    comment_id: str
    session_id: str
    message_id: str | None = None
    user: AssistantShareUserResponse
    content: str
    created_at: datetime
    updated_at: datetime
    can_edit: bool = False


class AssistantCommentCreateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)
    message_id: str | None = None

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("评论不能为空。")
        return normalized

    @field_validator("message_id", mode="before")
    @classmethod
    def normalize_optional_message_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class AssistantCommentUpdateRequest(BaseModel):
    content: str = Field(min_length=1, max_length=2000)

    @field_validator("content", mode="before")
    @classmethod
    def normalize_content(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("评论不能为空。")
        return normalized


class AssistantCreateSessionRequest(BaseModel):
    title: str | None = Field(default=None, max_length=40)
    organization_key: str | None = Field(default=None, max_length=64)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_optional_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.strip().split())
        return normalized or None

    @field_validator("organization_key", mode="before")
    @classmethod
    def normalize_optional_organization_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        return normalized or None


class AssistantRenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=40)

    @field_validator("title", mode="before")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        normalized = " ".join(value.strip().split())
        if not normalized:
            raise ValueError("标题不能为空。")
        return normalized


class AssistantDocoSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_token: str | None = Field(default=None, max_length=512)
    default_knowledge_base_id: int | None = Field(default=None, ge=1)

    @field_validator("api_token", mode="before")
    @classmethod
    def normalize_api_token(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("Doco Token 不能为空。")
        return normalized


class AssistantDocoSettingsResponse(BaseModel):
    configured: bool
    default_knowledge_base_id: int | None = None
    updated_at: datetime | None = None


class AssistantChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=12000)
    session_id: str | None = None
    organization_key: str | None = Field(default=None, max_length=64)
    context_items: list["AssistantContextItem"] = Field(default_factory=list)

    @field_validator("message", mode="before")
    @classmethod
    def normalize_message(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("消息不能为空。")
        return normalized

    @field_validator("organization_key", mode="before")
    @classmethod
    def normalize_optional_organization_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        return normalized or None


class AssistantChatResponse(BaseModel):
    session: AssistantSessionResponse
    user_message: AssistantMessageResponse
    assistant_message: AssistantMessageResponse
    session_created: bool
    turn_id: str | None = None
    citations: list["AssistantCitationResponse"] = Field(default_factory=list)


class AssistantUploadedImageResponse(BaseModel):
    image_id: str
    label: str
    file_name: str
    source_uri: str
    mime_type: str
    size_bytes: int
    context_item: "AssistantContextItem"


class AssistantUploadedFileResponse(BaseModel):
    file_id: str
    label: str
    file_name: str
    source_uri: str
    mime_type: str
    size_bytes: int
    source_type: str
    context_item: "AssistantContextItem"


class AssistantMessageFeedbackRequest(BaseModel):
    feedback: str | None = None

    @field_validator("feedback")
    @classmethod
    def validate_feedback(cls, value: str | None) -> str | None:
        if value not in {"like", "dislike", None}:
            raise ValueError("评价类型只能是 like、dislike 或空。")
        return value


class AssistantContextItem(BaseModel):
    context_key: str = Field(min_length=1, max_length=120)
    label: str = Field(min_length=1, max_length=120)
    content: str | None = Field(default=None, max_length=8000)
    source_type: str = Field(default="note", max_length=40)
    source_uri: str | None = Field(default=None, max_length=400)
    metadata: dict[str, object] = Field(default_factory=dict)

    @field_validator("context_key", "label", "source_type", mode="before")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = " ".join(value.strip().split())
        if not normalized:
            raise ValueError("字段不能为空。")
        return normalized

    @field_validator("content", "source_uri", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class AssistantMountsRequest(BaseModel):
    mounts: list[AssistantContextItem] = Field(default_factory=list)


class AssistantSkillResponse(BaseModel):
    skill_id: str
    name: str
    description: str
    path: str


class AssistantRuntimeEventResponse(BaseModel):
    event_id: str
    type: str
    payload: dict[str, object]
    created_at: datetime


class AssistantCitationResponse(BaseModel):
    citation_id: str
    citation_type: str
    label: str
    path: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    snippet: str | None = None
    metadata: dict[str, object] = Field(default_factory=dict)
    created_at: datetime


class AssistantFileArtifactResponse(BaseModel):
    artifact_id: str
    display_path: str
    filename: str
    mime_type: str
    size_bytes: int
    storage_status: str
    download_url: str | None = None


class AssistantTurnTraceResponse(BaseModel):
    turn: AssistantTurnResponse
    runtime_events: list[AssistantRuntimeEventResponse]
    context_requests: list[AssistantContextItem]
    context_usage: list[AssistantContextItem]
    citations: list[AssistantCitationResponse]
    git_scopes: list[AssistantGitScopeResponse] = Field(default_factory=list)


class ImageGenerationQuotaWindow(BaseModel):
    used: int
    limit: int
    resets_at: datetime | None = None


class ImageGenerationQuotaResponse(BaseModel):
    daily: ImageGenerationQuotaWindow
    weekly: ImageGenerationQuotaWindow
