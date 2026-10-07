from __future__ import annotations

from pydantic import BaseModel, Field


class RequirementPullRequestResponse(BaseModel):
    task_id: str
    repo_full_name: str
    pr_number: int
    pr_url: str
    title: str
    author_login: str
    state: str
    base_ref: str
    github_updated_at: str
    first_seen_at: str
    last_seen_at: str


class RequirementPullRequestListResponse(BaseModel):
    source_path: str
    pull_requests: list[RequirementPullRequestResponse] = Field(default_factory=list)
