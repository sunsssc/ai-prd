from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.code_reviews import CodeReviewJobResponse


class MyTaskPullRequestResponse(BaseModel):
    task_id: str
    repo_full_name: str
    pr_number: int
    pr_url: str
    title: str
    author_login: str
    state: str
    base_ref: str
    github_updated_at: str
    review_job: CodeReviewJobResponse | None = None


class MyTaskResponse(BaseModel):
    task_id: str
    title: str
    status: str
    is_completed: bool
    list_name: str
    clickup_url: str
    priority: str
    updated_at: str
    due_date: str
    source_path: str
    designers: list[str] = Field(default_factory=list)
    figma_url: str = ""
    relations: list[str] = Field(default_factory=list)
    detected_environments: list[str] = Field(default_factory=list)
    manual_environment: str | None = None
    pull_requests: list[MyTaskPullRequestResponse] = Field(default_factory=list)


class MyTaskListResponse(BaseModel):
    tasks: list[MyTaskResponse] = Field(default_factory=list)
    environment_options: list[str] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50
    total_pages: int = 0
    scope_counts: dict[str, int] = Field(default_factory=dict)
    status_options: list[str] = Field(default_factory=list)
    active_status_options: list[str] = Field(default_factory=list)
    completed_status_options: list[str] = Field(default_factory=list)
    role_options: list[str] = Field(default_factory=list)


class MyTaskEnvironmentUpdateRequest(BaseModel):
    environment: str | None = None


class MyTaskAssistantSessionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo_full_name: str = Field(min_length=3, max_length=200)
    pr_number: int = Field(gt=0)


class MyTaskAssistantSessionResponse(BaseModel):
    session_id: str
    resolved_sha: str | None = None
