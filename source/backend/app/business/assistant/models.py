from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

# 分享可见范围（share_type=public 时生效）：
# - agent：仅具有 Agent 访问权限的用户可见
# - registered：所有注册用户（已登录）可见
# - anyone：任何人可见，含未登录用户
SHARE_VISIBILITY_AGENT = "agent"
SHARE_VISIBILITY_REGISTERED = "registered"
SHARE_VISIBILITY_ANYONE = "anyone"
SHARE_VISIBILITY_SCOPES = (
    SHARE_VISIBILITY_AGENT,
    SHARE_VISIBILITY_REGISTERED,
    SHARE_VISIBILITY_ANYONE,
)
SHARE_TYPE_PUBLIC = "public"
SHARE_TYPE_MEMBERS = "members"


@dataclass(slots=True)
class AssistantSessionRecord:
    session_id: str
    user_id: str
    title: str
    created_at: datetime
    updated_at: datetime
    message_count: int = 0
    last_message_preview: str | None = None
    runtime_provider: str | None = None
    runtime_session_id: str | None = None
    runtime_working_directory: str | None = None
    active_organization_key: str | None = None
    runtime_workspace_profile: dict[str, Any] | None = None
    status: str = "active"
    has_active_turn: bool = False
    is_favorited: bool = False
    knowledge_scope_ids: list[str] = field(default_factory=list)
    git_context_defaults: list["SessionGitContextDefaultRecord"] = field(default_factory=list)


@dataclass(slots=True)
class AssistantMessageRecord:
    message_id: str
    session_id: str
    role: str
    content: str
    created_at: datetime
    feedback: str | None = None


@dataclass(slots=True)
class AssistantMessageFeedbackRecord:
    feedback_id: str
    message_id: str
    session_id: str
    user_id: str
    feedback: str
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class AssistantTurnRecord:
    turn_id: str
    session_id: str
    user_message_id: str
    assistant_message_id: str | None
    started_at: datetime
    completed_at: datetime | None
    status: str
    error_message: str | None = None


@dataclass(slots=True)
class TestDataPlanRecord:
    plan_id: str
    user_id: str
    session_id: str
    prepared_turn_id: str
    executed_turn_id: str | None
    organization_key: str
    task_name: str
    environment: str
    parameters: dict[str, Any]
    redacted_parameters: dict[str, Any]
    risk_level: str
    summary: str
    status: str
    upstream_run_id: str | None
    upstream_status: str | None
    result: dict[str, Any] | None
    error_message: str | None
    created_at: datetime
    expires_at: datetime
    executed_at: datetime | None
    updated_at: datetime
    secret: dict[str, Any] | None = None
    secret_revealed_at: datetime | None = None


@dataclass(slots=True)
class DocoCredentialRecord:
    """当前用户自己的 Doco 凭证和默认知识库。"""

    user_id: str
    api_token: str
    default_knowledge_base_id: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class DocoDocumentMappingRecord:
    """本地文档 ↔ Doco 文档 的幂等映射（跨会话长存，不随会话删除）。"""

    mapping_id: str
    user_id: str
    local_path: str
    document_id: str
    title: str
    knowledge_base_id: int
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class SessionGitContextDefaultRecord:
    default_id: str
    session_id: str
    user_id: str
    repo_full_name: str
    default_strategy: str
    selector_type: str
    selector_value: str
    last_resolved_sha: str | None
    logical_path: str
    authorization_source: str
    authorization_key: str
    last_checked_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class TurnGitContextRequestRecord:
    request_id: str
    turn_id: str
    repo_full_name: str
    strategy: str
    selector_type: str | None
    selector_value: str | None
    authorization_source: str
    authorization_key: str
    created_at: datetime


@dataclass(slots=True)
class TurnGitContextSnapshotRecord:
    snapshot_id: str
    turn_id: str
    repo_full_name: str
    strategy: str
    selector_type: str | None
    selector_value: str | None
    resolved_sha: str | None
    logical_code_path: str
    logical_context_path: str | None
    source: str
    resolved_at: datetime


@dataclass(slots=True)
class AssistantContextInput:
    context_key: str
    label: str
    content: str | None = None
    source_type: str = "note"
    source_uri: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class UploadedImageRecord:
    image_id: str
    label: str
    file_name: str
    source_uri: str
    mime_type: str
    size_bytes: int
    context_item: AssistantContextInput


@dataclass(slots=True)
class UploadedImageFileRecord:
    image_id: str
    file_path: str
    file_name: str
    mime_type: str


@dataclass(slots=True)
class UploadedFileRecord:
    file_id: str
    label: str
    file_name: str
    source_uri: str
    mime_type: str
    size_bytes: int
    source_type: str
    context_item: AssistantContextInput


@dataclass(slots=True)
class SessionContextMountRecord:
    mount_id: str
    session_id: str
    user_id: str
    context_key: str
    label: str
    content: str | None
    source_type: str
    source_uri: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class TurnContextRecord:
    entry_id: str
    turn_id: str
    context_key: str
    label: str
    content: str | None
    source_type: str
    source_uri: str | None
    metadata: dict[str, Any]
    created_at: datetime


@dataclass(slots=True)
class AnswerCitationRecord:
    citation_id: str
    turn_id: str
    assistant_message_id: str | None
    citation_type: str
    label: str
    path: str | None
    line_start: int | None
    line_end: int | None
    snippet: str | None
    metadata: dict[str, Any]
    created_at: datetime


@dataclass(slots=True)
class AssistantFileArtifactRecord:
    artifact_id: str
    session_id: str
    message_id: str | None
    turn_id: str | None
    owner_user_id: str
    display_path: str
    source_path: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    storage_status: str
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class SharedSessionFileArtifactRecord:
    shared_file_id: str
    share_id: str
    artifact_id: str
    snapshot_path: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    created_at: datetime


@dataclass(slots=True)
class AssistantRuntimeEventRecord:
    event_id: str
    session_id: str
    turn_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass(slots=True)
class SessionShareRecord:
    share_id: str
    session_id: str
    owner_id: str
    share_type: str
    share_token: str
    is_active: bool
    created_at: datetime
    member_user_ids: list[str] = field(default_factory=list)
    # share_type=public 时的可见范围：agent | registered | anyone
    visibility: str = "anyone"


@dataclass(slots=True)
class SharedSessionFollowUpRecord:
    request_id: str
    share_id: str
    session_id: str
    requested_by_user_id: str
    content: str
    status: str
    turn_id: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


@dataclass(slots=True)
class SharedSessionSummaryRecord:
    share: SessionShareRecord
    session: AssistantSessionRecord
    comment_count: int = 0


@dataclass(slots=True)
class SessionCommentRecord:
    comment_id: str
    session_id: str
    message_id: str | None
    user_id: str
    content: str
    created_at: datetime
    updated_at: datetime
