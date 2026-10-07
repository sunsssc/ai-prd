from __future__ import annotations

from pydantic import BaseModel, Field


class CodeReviewSummaryResponse(BaseModel):
    review_id: str
    review_type: str
    path: str
    repo_full_name: str
    pr_number: int
    pr_url: str
    base_ref: str
    base_sha: str
    head_sha: str
    diff_hash: str
    status: str
    risk_level: str
    created_at: str
    completed_at: str | None = None


class CodeReviewDetailResponse(CodeReviewSummaryResponse):
    requested_event_id: str
    requested_by_login: str
    requested_reviewer_logins: list[str] = Field(default_factory=list)
    requested_team_slugs: list[str] = Field(default_factory=list)
    notification_emails: list[str] = Field(default_factory=list)
    review_policy: str
    content: str
    frontmatter: dict[str, str] = Field(default_factory=dict)


class CodeReviewJobResponse(BaseModel):
    job_id: str
    repo_full_name: str
    pr_number: int
    pr_url: str
    base_ref: str
    base_sha: str
    head_sha: str
    diff_hash: str
    requested_event_id: str
    requested_by_login: str
    requested_reviewer_logins: list[str] = Field(default_factory=list)
    requested_team_slugs: list[str] = Field(default_factory=list)
    notification_emails: list[str] = Field(default_factory=list)
    review_type: str
    skill_name: str | None = None
    review_policy_path: str
    status: str
    review_id: str | None = None
    review_path: str | None = None
    error_message: str | None = None
    created_at: str
    started_at: str | None = None
    completed_at: str | None = None
    review: CodeReviewSummaryResponse | None = None


class CodeReviewJobListResponse(BaseModel):
    jobs: list[CodeReviewJobResponse] = Field(default_factory=list)


class CodeReviewEmailJobResponse(BaseModel):
    email_job_id: str
    code_review_job_id: str
    review_id: str
    recipient_email: str
    subject: str
    status: str
    attempts: int
    error_message: str | None = None
    created_at: str
    sent_at: str | None = None


class CodeReviewPollResponse(BaseModel):
    created_jobs: list[CodeReviewJobResponse] = Field(default_factory=list)


class ManualCodeReviewJobRequest(BaseModel):
    repo_full_name: str = Field(min_length=1)
    pr_number: int = Field(gt=0)
