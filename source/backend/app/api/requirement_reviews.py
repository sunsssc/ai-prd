from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query

from app.business.requirement_reviews import RequirementReviewService
from app.core.dependencies import get_current_app_user, get_requirement_review_service
from app.schemas.requirement_reviews import (
    RequirementReviewDetailResponse,
    RequirementReviewJobResponse,
    RequirementReviewListResponse,
    RequirementReviewRebuildRequest,
    RequirementReviewSummaryResponse,
)
from app.services.auth_models import UserRecord

router = APIRouter(prefix="/requirements/reviews", tags=["requirement-reviews"])


@router.get("", response_model=RequirementReviewListResponse)
def list_requirement_reviews(
    source_path: str = Query(min_length=1),
    current_user: UserRecord = Depends(get_current_app_user),
    review_service: RequirementReviewService = Depends(get_requirement_review_service),
) -> RequirementReviewListResponse:
    try:
        reviews = review_service.list_reviews(source_path=source_path, user_id=current_user.user_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RequirementReviewListResponse(
        source_path=source_path,
        reviews=[_serialize_review(review) for review in reviews],
    )


@router.post("/jobs", response_model=RequirementReviewJobResponse)
async def create_requirement_review_job(
    body: RequirementReviewRebuildRequest,
    current_user: UserRecord = Depends(get_current_app_user),
    review_service: RequirementReviewService = Depends(get_requirement_review_service),
) -> RequirementReviewJobResponse:
    del current_user
    if body.review_type != "tech_review":
        raise HTTPException(status_code=400, detail="当前仅支持 tech_review。")
    try:
        job = review_service.enqueue_review(source_path=body.source_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if job.status == "pending":
        asyncio.create_task(review_service.run_job(job))
    return _serialize_job(job, review=None)


@router.get("/jobs/{job_id}", response_model=RequirementReviewJobResponse)
def get_requirement_review_job(
    job_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    review_service: RequirementReviewService = Depends(get_requirement_review_service),
) -> RequirementReviewJobResponse:
    try:
        job = review_service.get_job(job_id=job_id)
        review = review_service.get_job_review(job=job, user_id=current_user.user_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _serialize_job(job, review=review)


@router.get("/{review_id}", response_model=RequirementReviewDetailResponse)
def get_requirement_review(
    review_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    review_service: RequirementReviewService = Depends(get_requirement_review_service),
) -> RequirementReviewDetailResponse:
    try:
        detail = review_service.get_review_detail(review_id=review_id, user_id=current_user.user_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RequirementReviewDetailResponse(**detail.__dict__)


@router.post("/{review_id}/read", response_model=RequirementReviewDetailResponse)
def mark_requirement_review_read(
    review_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    review_service: RequirementReviewService = Depends(get_requirement_review_service),
) -> RequirementReviewDetailResponse:
    try:
        detail = review_service.mark_read(review_id=review_id, user_id=current_user.user_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return RequirementReviewDetailResponse(**detail.__dict__)


@router.post("/rebuild", response_model=RequirementReviewSummaryResponse)
async def rebuild_requirement_review(
    body: RequirementReviewRebuildRequest,
    current_user: UserRecord = Depends(get_current_app_user),
    review_service: RequirementReviewService = Depends(get_requirement_review_service),
) -> RequirementReviewSummaryResponse:
    del current_user
    if body.review_type != "tech_review":
        raise HTTPException(status_code=400, detail="当前仅支持 tech_review。")
    try:
        review = await review_service.rebuild_review(source_path=body.source_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return _serialize_review(review)


def _serialize_job(job: object, review: object | None) -> RequirementReviewJobResponse:
    return RequirementReviewJobResponse(
        job_id=str(getattr(job, "job_id")),
        source_path=str(getattr(job, "source_path")),
        source_hash=str(getattr(job, "source_hash")),
        review_type=str(getattr(job, "review_type")),
        skill_name=str(getattr(job, "skill_name")),
        status=str(getattr(job, "status")),
        review_id=getattr(job, "review_id"),
        review_path=getattr(job, "review_path"),
        error_message=getattr(job, "error_message"),
        created_at=str(getattr(job, "created_at")),
        started_at=getattr(job, "started_at"),
        completed_at=getattr(job, "completed_at"),
        review=_serialize_review(review) if review is not None else None,
    )


def _serialize_review(review: object) -> RequirementReviewSummaryResponse:
    return RequirementReviewSummaryResponse(
        review_id=str(getattr(review, "review_id")),
        review_type=str(getattr(review, "review_type")),
        path=str(getattr(review, "path")),
        source_paths=list(getattr(review, "source_paths", ())),
        status=str(getattr(review, "status")),
        risk_level=str(getattr(review, "risk_level")),
        source_hash=str(getattr(review, "source_hash")),
        is_read=bool(getattr(review, "is_read", False)),
        created_at=str(getattr(review, "created_at")),
        completed_at=getattr(review, "completed_at"),
    )
