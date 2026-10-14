from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class RequirementReviewBadgeResponse(BaseModel):
    type: str
    unread_count: int = 0
    latest_review_id: str


class RequirementReviewSummaryResponse(BaseModel):
    review_id: str
    review_type: str
    path: str
    source_paths: list[str] = Field(default_factory=list)
    status: str
    risk_level: str
    source_hash: str
    is_read: bool = False
    created_at: str
    completed_at: str | None = None


class RequirementReviewListResponse(BaseModel):
    source_path: str
    reviews: list[RequirementReviewSummaryResponse] = Field(default_factory=list)


class RequirementReviewJobResponse(BaseModel):
    job_id: str
    source_path: str
    source_hash: str
    review_type: str
    skill_name: str
    status: str
    review_id: str | None = None
    review_path: str | None = None
    error_message: str | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    review: RequirementReviewSummaryResponse | None = None


class RequirementReviewDetailResponse(RequirementReviewSummaryResponse):
    source_path: str
    source_type: str
    source_meta_path: str
    skill: str
    content: str
    frontmatter: dict[str, str] = Field(default_factory=dict)


class RequirementReviewRebuildRequest(BaseModel):
    source_path: str = Field(min_length=1, max_length=400)
    review_type: str = "tech_review"

    @field_validator("source_path", "review_type", mode="before")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = " ".join(value.strip().split()) if value == "tech_review" else value.strip()
        if not normalized:
            raise ValueError("字段不能为空。")
        return normalized
