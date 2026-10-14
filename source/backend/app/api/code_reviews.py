from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query

from app.business.code_reviews import CodeReviewService
from app.core.dependencies import get_code_review_service, get_current_app_user
from app.schemas.code_reviews import (
    CodeReviewDetailResponse,
    CodeReviewEmailJobResponse,
    CodeReviewJobListResponse,
    CodeReviewJobResponse,
    ManualCodeReviewJobRequest,
    CodeReviewPollResponse,
    CodeReviewSummaryResponse,
)
from app.services.auth_models import UserRecord

router = APIRouter(tags=["code-reviews"])


@router.post("/code-reviews/jobs", response_model=CodeReviewJobResponse)
async def create_manual_code_review_job(
    payload: ManualCodeReviewJobRequest,
    current_user: UserRecord = Depends(get_current_app_user),
    service: CodeReviewService = Depends(get_code_review_service),
) -> CodeReviewJobResponse:
    try:
        job = service.enqueue_manual_review(
            repo_full_name=payload.repo_full_name,
            pr_number=payload.pr_number,
            requested_by_email=current_user.email,
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if job.status == "pending":
        asyncio.create_task(service.run_job(job))
    return _serialize_job(job, service.get_job_review(job=job))


@router.get("/code-reviews/jobs", response_model=CodeReviewJobListResponse)
def list_code_review_jobs(
    repo_full_name: str | None = Query(default=None),
    pr_number: int | None = Query(default=None, gt=0),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: UserRecord = Depends(get_current_app_user),
    service: CodeReviewService = Depends(get_code_review_service),
) -> CodeReviewJobListResponse:
    del current_user
    jobs = service.list_jobs(repo_full_name=repo_full_name, pr_number=pr_number, status=status, limit=limit)
    return CodeReviewJobListResponse(jobs=[_serialize_job(job, service.get_job_review(job=job)) for job in jobs])


@router.get("/code-reviews/jobs/{job_id}", response_model=CodeReviewJobResponse)
def get_code_review_job(
    job_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    service: CodeReviewService = Depends(get_code_review_service),
) -> CodeReviewJobResponse:
    del current_user
    try:
        job = service.get_job(job_id=job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _serialize_job(job, service.get_job_review(job=job))


@router.get("/code-reviews/{review_id}", response_model=CodeReviewDetailResponse)
def get_code_review(
    review_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    service: CodeReviewService = Depends(get_code_review_service),
) -> CodeReviewDetailResponse:
    del current_user
    try:
        detail = service.get_review_detail(review_id=review_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return CodeReviewDetailResponse(**{**detail.__dict__, "frontmatter": detail.frontmatter or {}})


@router.post("/code-reviews/jobs/{job_id}/retry", response_model=CodeReviewJobResponse)
def retry_code_review_job(
    job_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    service: CodeReviewService = Depends(get_code_review_service),
) -> CodeReviewJobResponse:
    del current_user
    try:
        job = service.retry_job(job_id=job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_job(job, None)


@router.post("/code-reviews/email-jobs/{email_job_id}/retry", response_model=CodeReviewEmailJobResponse)
def retry_code_review_email_job(
    email_job_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    service: CodeReviewService = Depends(get_code_review_service),
) -> CodeReviewEmailJobResponse:
    del current_user
    try:
        job = service.retry_email_job(email_job_id=email_job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_email_job(job)


@router.post("/code-reviews/poll", response_model=CodeReviewPollResponse)
async def poll_code_reviews(
    current_user: UserRecord = Depends(get_current_app_user),
    service: CodeReviewService = Depends(get_code_review_service),
) -> CodeReviewPollResponse:
    del current_user
    try:
        jobs = await service.poll_once()
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return CodeReviewPollResponse(created_jobs=[_serialize_job(job, None) for job in jobs])


def _serialize_job(job: object, review: object | None) -> CodeReviewJobResponse:
    return CodeReviewJobResponse(
        job_id=str(getattr(job, "job_id")),
        repo_full_name=str(getattr(job, "repo_full_name")),
        pr_number=int(getattr(job, "pr_number")),
        pr_url=str(getattr(job, "pr_url")),
        base_ref=str(getattr(job, "base_ref")),
        base_sha=str(getattr(job, "base_sha")),
        head_sha=str(getattr(job, "head_sha")),
        diff_hash=str(getattr(job, "diff_hash")),
        requested_event_id=str(getattr(job, "requested_event_id")),
        requested_by_login=str(getattr(job, "requested_by_login")),
        requested_reviewer_logins=_parse_csv(str(getattr(job, "requested_reviewer_logins"))),
        requested_team_slugs=_parse_csv(str(getattr(job, "requested_team_slugs"))),
        notification_emails=_parse_csv(str(getattr(job, "notification_emails"))),
        review_type=str(getattr(job, "review_type")),
        skill_name=getattr(job, "skill_name"),
        review_policy_path=str(getattr(job, "review_policy_path")),
        status=str(getattr(job, "status")),
        review_id=getattr(job, "review_id"),
        review_path=getattr(job, "review_path"),
        error_message=getattr(job, "error_message"),
        created_at=str(getattr(job, "created_at")),
        started_at=getattr(job, "started_at"),
        completed_at=getattr(job, "completed_at"),
        review=_serialize_review(review) if review is not None else None,
    )


def _serialize_review(review: object) -> CodeReviewSummaryResponse:
    return CodeReviewSummaryResponse(
        review_id=str(getattr(review, "review_id")),
        review_type=str(getattr(review, "review_type")),
        path=str(getattr(review, "path")),
        repo_full_name=str(getattr(review, "repo_full_name")),
        pr_number=int(getattr(review, "pr_number")),
        pr_url=str(getattr(review, "pr_url")),
        base_ref=str(getattr(review, "base_ref")),
        base_sha=str(getattr(review, "base_sha")),
        head_sha=str(getattr(review, "head_sha")),
        diff_hash=str(getattr(review, "diff_hash")),
        status=str(getattr(review, "status")),
        risk_level=str(getattr(review, "risk_level")),
        created_at=str(getattr(review, "created_at")),
        completed_at=getattr(review, "completed_at"),
    )


def _serialize_email_job(job: object) -> CodeReviewEmailJobResponse:
    return CodeReviewEmailJobResponse(
        email_job_id=str(getattr(job, "email_job_id")),
        code_review_job_id=str(getattr(job, "code_review_job_id")),
        review_id=str(getattr(job, "review_id")),
        recipient_email=str(getattr(job, "recipient_email")),
        subject=str(getattr(job, "subject")),
        status=str(getattr(job, "status")),
        attempts=int(getattr(job, "attempts")),
        error_message=getattr(job, "error_message"),
        created_at=str(getattr(job, "created_at")),
        sent_at=getattr(job, "sent_at"),
    )


def _parse_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]
