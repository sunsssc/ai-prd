from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class BusinessDocUpdateBadgeResponse(BaseModel):
    type: str
    pending_count: int = 0
    latest_update_id: str


class BusinessDocUpdateSummaryResponse(BaseModel):
    update_id: str
    artifact_type: str
    path: str
    source_path: str | None = None
    run_id: str
    mode: str
    repo_key: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    status: str
    review_status: str | None = None
    confidence: float | None = None
    apply_status: str | None = None
    created_at: str
    completed_at: str | None = None


class BusinessDocUpdateListResponse(BaseModel):
    source_path: str
    updates: list[BusinessDocUpdateSummaryResponse] = Field(default_factory=list)


class BusinessDocUpdateDetailResponse(BusinessDocUpdateSummaryResponse):
    skill: str
    affected_docs: list[str] = Field(default_factory=list)
    content: str
    evidence: list[dict[str, object]] = Field(default_factory=list)
    review_feedback: str | None = None
    generator_review: str | None = None
    frontmatter: dict[str, str] = Field(default_factory=dict)


class BusinessDocUpdateItemResponse(BaseModel):
    item_id: str
    source_path: str
    result_type: str | None = None
    status: str
    update_id: str | None = None
    update_path: str | None = None
    evidence: list[dict[str, object]] = Field(default_factory=list)
    review_status: str | None = None
    review_feedback: str | None = None
    generator_review: str | None = None
    confidence: float | None = None
    apply_status: str | None = None
    error_message: str | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None


class BusinessDocUpdateRunResponse(BaseModel):
    run_id: str
    mode: str
    status: str
    snapshot_id: str
    repo_key: str | None = None
    base_sha: str | None = None
    head_sha: str | None = None
    total_items: int
    completed_items: int
    failed_items: int
    snapshots: list[dict[str, object]] = Field(default_factory=list)
    error_message: str | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    items: list[BusinessDocUpdateItemResponse] = Field(default_factory=list)


class BusinessDocUpdateActionRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=1000)

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_optional_reason(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None
