from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.business.requirement_pr_links import RequirementPrLinkService
from app.core.dependencies import get_current_app_user, get_requirement_pr_link_service
from app.schemas.requirement_pull_requests import (
    RequirementPullRequestListResponse,
    RequirementPullRequestResponse,
)
from app.services.auth_models import UserRecord


router = APIRouter(prefix="/requirements", tags=["requirement-pull-requests"])


@router.get("/pull-requests", response_model=RequirementPullRequestListResponse)
def list_requirement_pull_requests(
    source_path: str = Query(min_length=1),
    current_user: UserRecord = Depends(get_current_app_user),
    pr_link_service: RequirementPrLinkService = Depends(get_requirement_pr_link_service),
) -> RequirementPullRequestListResponse:
    del current_user
    try:
        pull_requests = pr_link_service.list_pull_requests_for_requirement(source_path=source_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RequirementPullRequestListResponse(
        source_path=source_path,
        pull_requests=[RequirementPullRequestResponse(**link.__dict__) for link in pull_requests],
    )
