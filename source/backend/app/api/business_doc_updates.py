from __future__ import annotations

import json

from fastapi import APIRouter, Cookie, Depends, HTTPException, Query

from app.business.business_doc_updates import BusinessDocUpdateService
from app.core.config import settings
from app.core.dependencies import get_auth_service, get_business_doc_update_service, get_current_app_user
from app.schemas.business_doc_updates import (
    BusinessDocUpdateActionRequest,
    BusinessDocUpdateDetailResponse,
    BusinessDocUpdateItemResponse,
    BusinessDocUpdateListResponse,
    BusinessDocUpdateRunResponse,
    BusinessDocUpdateSummaryResponse,
)
from app.services.auth_models import UserRecord
from app.services.auth_service import AuthService

router = APIRouter(tags=["business-doc-updates"])


def _require_admin(
    auth_service: AuthService = Depends(get_auth_service),
    admin_session_cookie: str | None = Cookie(default=None, alias=settings.admin_session_cookie_name),
) -> UserRecord:
    if admin_session_cookie is None:
        raise HTTPException(status_code=401, detail="缺少管理员会话。")
    user = auth_service.get_current_admin(admin_session_cookie)
    if user is None:
        raise HTTPException(status_code=401, detail="管理员会话无效或已过期。")
    return user


@router.post("/business-docs/update-runs/full", response_model=BusinessDocUpdateRunResponse, status_code=202)
def create_full_business_doc_update_run(
    admin_user: UserRecord = Depends(_require_admin),
    update_service: BusinessDocUpdateService = Depends(get_business_doc_update_service),
) -> BusinessDocUpdateRunResponse:
    del admin_user
    try:
        run = update_service.create_full_run()
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_run(run, [])


@router.get("/business-docs/update-runs", response_model=list[BusinessDocUpdateRunResponse])
def list_business_doc_update_runs(
    limit: int = Query(default=50, ge=1, le=100),
    admin_user: UserRecord = Depends(_require_admin),
    update_service: BusinessDocUpdateService = Depends(get_business_doc_update_service),
) -> list[BusinessDocUpdateRunResponse]:
    del admin_user
    return [_serialize_run(run, []) for run in update_service.list_runs(limit=limit)]


@router.get("/business-docs/update-runs/{run_id}", response_model=BusinessDocUpdateRunResponse)
def get_business_doc_update_run(
    run_id: str,
    admin_user: UserRecord = Depends(_require_admin),
    update_service: BusinessDocUpdateService = Depends(get_business_doc_update_service),
) -> BusinessDocUpdateRunResponse:
    del admin_user
    try:
        run = update_service.get_run(run_id=run_id)
        items = update_service.get_run_items(run_id=run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _serialize_run(run, items)


@router.post("/business-docs/update-runs/{run_id}/retry", response_model=BusinessDocUpdateRunResponse, status_code=202)
def retry_business_doc_update_run(
    run_id: str,
    admin_user: UserRecord = Depends(_require_admin),
    update_service: BusinessDocUpdateService = Depends(get_business_doc_update_service),
) -> BusinessDocUpdateRunResponse:
    del admin_user
    try:
        run = update_service.retry_run(run_id=run_id)
        items = update_service.get_run_items(run_id=run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_run(run, items)


@router.get("/business-docs/admin/updates/{update_id}", response_model=BusinessDocUpdateDetailResponse)
def get_admin_business_doc_update(
    update_id: str,
    admin_user: UserRecord = Depends(_require_admin),
    update_service: BusinessDocUpdateService = Depends(get_business_doc_update_service),
) -> BusinessDocUpdateDetailResponse:
    del admin_user
    try:
        detail = update_service.get_update_detail(update_id=update_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return BusinessDocUpdateDetailResponse(**detail.__dict__)


@router.post("/business-docs/admin/updates/{update_id}/ignore", response_model=BusinessDocUpdateDetailResponse)
def ignore_admin_business_doc_update(
    update_id: str,
    body: BusinessDocUpdateActionRequest | None = None,
    admin_user: UserRecord = Depends(_require_admin),
    update_service: BusinessDocUpdateService = Depends(get_business_doc_update_service),
) -> BusinessDocUpdateDetailResponse:
    try:
        detail = update_service.ignore_update(
            update_id=update_id,
            user_id=admin_user.user_id,
            reason=body.reason if body else None,
            actor_role="admin",
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BusinessDocUpdateDetailResponse(**detail.__dict__)


@router.post("/business-docs/admin/updates/{update_id}/apply", response_model=BusinessDocUpdateDetailResponse)
def apply_admin_business_doc_update(
    update_id: str,
    body: BusinessDocUpdateActionRequest | None = None,
    admin_user: UserRecord = Depends(_require_admin),
    update_service: BusinessDocUpdateService = Depends(get_business_doc_update_service),
) -> BusinessDocUpdateDetailResponse:
    try:
        detail = update_service.apply_update(
            update_id=update_id,
            user_id=admin_user.user_id,
            reason=body.reason if body else None,
            actor_role="admin",
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BusinessDocUpdateDetailResponse(**detail.__dict__)


@router.get("/business-docs/updates", response_model=BusinessDocUpdateListResponse)
def list_business_doc_updates(
    source_path: str = Query(min_length=1),
    current_user: UserRecord = Depends(get_current_app_user),
    update_service: BusinessDocUpdateService = Depends(get_business_doc_update_service),
) -> BusinessDocUpdateListResponse:
    del current_user
    try:
        updates = update_service.list_updates(source_path=source_path)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return BusinessDocUpdateListResponse(source_path=source_path, updates=[_serialize_update(update) for update in updates])


@router.get("/business-docs/updates/{update_id}", response_model=BusinessDocUpdateDetailResponse)
def get_business_doc_update(
    update_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    update_service: BusinessDocUpdateService = Depends(get_business_doc_update_service),
) -> BusinessDocUpdateDetailResponse:
    del current_user
    try:
        detail = update_service.get_update_detail(update_id=update_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return BusinessDocUpdateDetailResponse(**detail.__dict__)


@router.post("/business-docs/updates/{update_id}/ignore", response_model=BusinessDocUpdateDetailResponse)
def ignore_business_doc_update(
    update_id: str,
    body: BusinessDocUpdateActionRequest | None = None,
    current_user: UserRecord = Depends(get_current_app_user),
    update_service: BusinessDocUpdateService = Depends(get_business_doc_update_service),
) -> BusinessDocUpdateDetailResponse:
    try:
        detail = update_service.ignore_update(update_id=update_id, user_id=current_user.user_id, reason=body.reason if body else None)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BusinessDocUpdateDetailResponse(**detail.__dict__)


@router.post("/business-docs/updates/{update_id}/apply", response_model=BusinessDocUpdateDetailResponse)
def apply_business_doc_update(
    update_id: str,
    body: BusinessDocUpdateActionRequest | None = None,
    current_user: UserRecord = Depends(get_current_app_user),
    update_service: BusinessDocUpdateService = Depends(get_business_doc_update_service),
) -> BusinessDocUpdateDetailResponse:
    try:
        detail = update_service.apply_update(update_id=update_id, user_id=current_user.user_id, reason=body.reason if body else None)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return BusinessDocUpdateDetailResponse(**detail.__dict__)


def _serialize_run(run: object, items: list[object]) -> BusinessDocUpdateRunResponse:
    try:
        snapshots = json.loads(str(getattr(run, "snapshot_json")))
    except json.JSONDecodeError:
        snapshots = []
    return BusinessDocUpdateRunResponse(
        run_id=str(getattr(run, "run_id")),
        mode=str(getattr(run, "mode")),
        status=str(getattr(run, "status")),
        snapshot_id=str(getattr(run, "snapshot_id")),
        repo_key=getattr(run, "repo_key"),
        base_sha=getattr(run, "base_sha"),
        head_sha=getattr(run, "head_sha"),
        total_items=int(getattr(run, "total_items")),
        completed_items=int(getattr(run, "completed_items")),
        failed_items=int(getattr(run, "failed_items")),
        snapshots=snapshots if isinstance(snapshots, list) else [],
        error_message=getattr(run, "error_message"),
        created_at=str(getattr(run, "created_at")),
        started_at=getattr(run, "started_at"),
        completed_at=getattr(run, "completed_at"),
        items=[_serialize_item(item) for item in items],
    )


def _serialize_item(item: object) -> BusinessDocUpdateItemResponse:
    try:
        evidence = json.loads(str(getattr(item, "evidence_json")))
    except json.JSONDecodeError:
        evidence = []
    return BusinessDocUpdateItemResponse(
        item_id=str(getattr(item, "item_id")),
        source_path=str(getattr(item, "source_path")),
        result_type=getattr(item, "result_type"),
        status=str(getattr(item, "status")),
        update_id=getattr(item, "update_id"),
        update_path=getattr(item, "update_path"),
        evidence=evidence if isinstance(evidence, list) else [],
        review_status=getattr(item, "review_status"),
        review_feedback=getattr(item, "review_feedback"),
        generator_review=getattr(item, "generator_review"),
        confidence=getattr(item, "confidence"),
        apply_status=getattr(item, "apply_status"),
        error_message=getattr(item, "error_message"),
        created_at=str(getattr(item, "created_at")),
        started_at=getattr(item, "started_at"),
        completed_at=getattr(item, "completed_at"),
    )


def _serialize_update(update: object) -> BusinessDocUpdateSummaryResponse:
    return BusinessDocUpdateSummaryResponse(
        update_id=str(getattr(update, "update_id")),
        artifact_type=str(getattr(update, "artifact_type")),
        path=str(getattr(update, "path")),
        source_path=getattr(update, "source_path", None),
        run_id=str(getattr(update, "run_id")),
        mode=str(getattr(update, "mode")),
        repo_key=getattr(update, "repo_key"),
        base_sha=getattr(update, "base_sha"),
        head_sha=getattr(update, "head_sha"),
        status=str(getattr(update, "status")),
        review_status=getattr(update, "review_status"),
        confidence=getattr(update, "confidence"),
        apply_status=getattr(update, "apply_status"),
        created_at=str(getattr(update, "created_at")),
        completed_at=getattr(update, "completed_at"),
    )
