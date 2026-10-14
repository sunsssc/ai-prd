from __future__ import annotations

from pydantic import BaseModel, Field, field_validator

from app.schemas.business_doc_updates import BusinessDocUpdateBadgeResponse
from app.schemas.requirement_reviews import RequirementReviewBadgeResponse


class WorkspaceTreeNodeResponse(BaseModel):
    id: str
    kind: str
    name: str
    path: str
    children: list["WorkspaceTreeNodeResponse"] = Field(default_factory=list)
    has_children: bool = False
    children_loaded: bool = True
    child_count: int | None = None
    updated_at: str | None = None
    content_type: str | None = None
    content_path: str | None = None
    review_badge: RequirementReviewBadgeResponse | None = None
    doc_update_badge: BusinessDocUpdateBadgeResponse | None = None
    creator_user_id: str | None = None
    creator_name: str | None = None
    creator_email: str | None = None
    ownership_status: str | None = None
    can_edit: bool = False
    can_delete: bool = False
    requires_admin_override: bool = False


class WorkspaceSyncSummaryResponse(BaseModel):
    synced_at: str | None = None
    changed_count: int = 0
    sample_files: list[str] = Field(default_factory=list)
    errors: int = 0
    last_changed_at: str | None = None
    last_changed_count: int = 0
    last_sample_files: list[str] = Field(default_factory=list)


class WorkspaceChildrenResponse(BaseModel):
    root: WorkspaceTreeNodeResponse
    default_file_path: str | None = None
    sync_summary: WorkspaceSyncSummaryResponse | None = None


class WorkspaceKnowledgeIndexResponse(BaseModel):
    prefixes: list[str] = Field(default_factory=list)
    directories: list[tuple[int, str, int]] = Field(default_factory=list)
    files: list[tuple[int, str]] = Field(default_factory=list)


class OrganizationMembershipResponse(BaseModel):
    membership_id: str
    organization_key: str
    organization_role: str
    is_default: bool
    status: str
    expires_at: str | None = None


class WorkspaceListItemResponse(BaseModel):
    workspace_id: str
    workspace_key: str
    organization_key: str | None = None
    type: str
    display_name: str
    permission: str
    sandbox_path: str


class OrganizationAccessRequestCreateRequest(BaseModel):
    target_organization_key: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=1000)
    requested_scope: str | None = Field(default=None, max_length=500)
    expires_at: str | None = Field(default=None, max_length=40)

    @field_validator("target_organization_key", mode="before")
    @classmethod
    def normalize_target_organization_key(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("目标组织不能为空。")
        return normalized

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("申请原因不能为空。")
        return normalized

    @field_validator("requested_scope", "expires_at", mode="before")
    @classmethod
    def normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class OrganizationAccessRequestResponse(BaseModel):
    request_id: str
    requester_user_id: str
    source_organization_key: str | None = None
    target_organization_key: str
    reason: str
    requested_scope: str | None = None
    status: str
    reviewed_by_user_id: str | None = None
    reviewed_at: str | None = None
    review_comment: str | None = None
    expires_at: str | None = None
    created_at: str
    updated_at: str


class WorkspaceFileResponse(BaseModel):
    id: str
    name: str
    path: str
    updated_at: str | None = None
    content_type: str
    content: str


class WorkspaceFileUpdateRequest(BaseModel):
    path: str = Field(min_length=1, max_length=400)
    content: str
    edit_summary: str | None = Field(default=None, max_length=120)
    admin_override_confirmed: bool = False

    @field_validator("path", mode="before")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("路径不能为空。")
        return normalized


class WorkspaceRequirementSourceRefreshRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)

    @field_validator("path", mode="before")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("路径不能为空。")
        return normalized


class WorkspaceRequirementSourceRefreshResponse(BaseModel):
    path: str
    source_type: str
    changed: bool
    updated_at: str | None = None
    message: str


class WorkspaceRequirementClickUpContentUpdateRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(default="", max_length=500_000)
    base_content: str = Field(max_length=500_000)

    @field_validator("path", mode="before")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空。")
        return normalized


class WorkspaceRequirementClickUpContentResponse(BaseModel):
    path: str
    source_type: str
    title: str
    content: str
    local_synced: bool = True
    message: str = ""


class WorkspaceRequirementContentHistoryItemResponse(BaseModel):
    version_id: str
    created_at: str
    title: str
    content_length: int


class WorkspaceRequirementContentHistoryResponse(BaseModel):
    path: str
    versions: list[WorkspaceRequirementContentHistoryItemResponse] = Field(default_factory=list)


class WorkspaceRequirementContentHistoryRestoreRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)

    @field_validator("path", mode="before")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("路径不能为空。")
        return normalized


class WorkspaceRequirementClickUpEditAccessResponse(BaseModel):
    path: str
    source_type: str
    can_edit: bool
    reason: str = ""


class WorkspaceRequirementCommentCreateRequest(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(min_length=1, max_length=10000)
    parent_comment_id: str | None = Field(default=None, max_length=80)

    @field_validator("path", "content", mode="before")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空。")
        return normalized

    @field_validator("parent_comment_id", mode="before")
    @classmethod
    def normalize_parent_comment_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class WorkspaceRequirementCommentResponse(BaseModel):
    comment_id: str
    author: str
    created_at: str
    content: str
    replies: list["WorkspaceRequirementCommentResponse"] = Field(default_factory=list)


class WorkspaceRequirementCommentsResponse(BaseModel):
    comments: list[WorkspaceRequirementCommentResponse] = Field(default_factory=list)


class WorkspaceSkillFileRequest(BaseModel):
    path: str = Field(min_length=1, max_length=240)
    content: str = ""

    @field_validator("path", mode="before")
    @classmethod
    def normalize_path(cls, value: str) -> str:
        normalized = value.strip().replace("\\", "/").strip("/")
        if not normalized:
            raise ValueError("文件路径不能为空。")
        return normalized


class WorkspaceSkillCreateRequest(BaseModel):
    name: str | None = Field(default=None, max_length=80)
    folder_name: str | None = Field(default=None, max_length=80)
    description: str = Field(default="", max_length=240)
    content: str = ""
    intent: str = Field(default="", max_length=1200)
    files: list[WorkspaceSkillFileRequest] = Field(default_factory=list, max_length=200)

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        return normalized

    @field_validator("folder_name", mode="before")
    @classmethod
    def normalize_folder_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().replace("\\", "/").strip("/")
        if not normalized:
            return None
        return normalized


class WorkspaceSkillImportCommitRequest(BaseModel):
    token: str = Field(min_length=1, max_length=120)
    folder_name: str = Field(min_length=1, max_length=80)
    skill_md_mode: str = Field(default="existing", pattern="^(existing|manual|generated)$")
    skill_md_content: str = ""

    @field_validator("token", "folder_name", mode="before")
    @classmethod
    def normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空。")
        return normalized


class WorkspaceSkillImportGenerateRequest(BaseModel):
    token: str = Field(min_length=1, max_length=120)
    folder_name: str = Field(min_length=1, max_length=80)

    @field_validator("token", "folder_name", mode="before")
    @classmethod
    def normalize_generate_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空。")
        return normalized


class WorkspaceSkillEntryCreateRequest(BaseModel):
    parent_path: str = Field(min_length=1, max_length=400)
    name: str = Field(min_length=1, max_length=120)
    kind: str = Field(pattern="^(file|folder)$")
    content: str = ""
    admin_override_confirmed: bool = False

    @field_validator("parent_path", "name", mode="before")
    @classmethod
    def normalize_entry_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空。")
        return normalized


class WorkspaceSkillEntryMoveRequest(BaseModel):
    path: str = Field(min_length=1, max_length=400)
    destination_path: str = Field(min_length=1, max_length=400)
    admin_override_confirmed: bool = False

    @field_validator("path", "destination_path", mode="before")
    @classmethod
    def normalize_move_path(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("路径不能为空。")
        return normalized
