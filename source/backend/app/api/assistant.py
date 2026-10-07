from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from time import perf_counter
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import FileResponse, StreamingResponse

from app.core.config import settings
from app.core.dependencies import (
    get_assistant_service,
    get_assistant_store,
    get_auth_store,
    get_current_app_user,
    get_notification_service,
    get_optional_app_user,
)
from app.schemas.assistant import (
    AssistantChatRequest,
    AssistantMcpApprovalDecisionRequest,
    AssistantMcpApprovalResponse,
    AssistantChatResponse,
    AssistantCitationResponse,
    AssistantCommentCreateRequest,
    AssistantCommentResponse,
    AssistantCommentUpdateRequest,
    AssistantContextItem,
    AssistantCreateSessionRequest,
    AssistantDocoSettingsRequest,
    AssistantDocoSettingsResponse,
    AssistantFileArtifactResponse,
    AssistantGitScopeResponse,
    AssistantMessageFeedbackRequest,
    AssistantMountsRequest,
    AssistantMessageResponse,
    AssistantRenameSessionRequest,
    AssistantSessionDetailResponse,
    AssistantSessionFavoriteResponse,
    AssistantSessionResponse,
    AssistantShareRequest,
    AssistantShareResponse,
    AssistantSharedFollowUpRequest,
    AssistantSharedFollowUpResponse,
    AssistantSharedSessionDetailResponse,
    AssistantSharedSessionSummaryResponse,
    AssistantShareUserResponse,
    AssistantSkillResponse,
    AssistantTimelineNoticeResponse,
    AssistantTestDataPlanResponse,
    AssistantTestDataSecretResponse,
    AssistantTurnResponse,
    AssistantTurnTraceResponse,
    AssistantUploadedFileResponse,
    AssistantUploadedImageResponse,
    ImageGenerationQuotaResponse,
    ImageGenerationQuotaWindow,
)
from app.business.assistant.models import (
    AssistantFileArtifactRecord,
    AssistantMessageRecord,
    AssistantSessionRecord,
    SessionCommentRecord,
    SessionShareRecord,
    SharedSessionFollowUpRecord,
    SharedSessionSummaryRecord,
    TestDataPlanRecord,
    DocoCredentialRecord,
    UploadedFileRecord,
    UploadedImageRecord,
)
from app.business.assistant.service import (
    AssistantService,
    AssistantSessionBusyError,
    TurnTrace,
    requires_runtime_rebuild,
)
from app.business.assistant.store import SQLiteAssistantStore
from app.business.assistant.mcp_approvals import McpApprovalConflict
from app.integrations.agent_runtime import AssistantRuntimeError
from app.services.auth_models import UserRecord
from app.services.auth_store import SQLiteAuthStore
from app.services.notification_service import AuthNotificationService

router = APIRouter(prefix="/assistant", tags=["assistant"])
logger = logging.getLogger("uvicorn.error")


@router.get("/doco/settings", response_model=AssistantDocoSettingsResponse)
def get_doco_settings(
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_store: SQLiteAssistantStore = Depends(get_assistant_store),
) -> AssistantDocoSettingsResponse:
    credentials = assistant_store.get_doco_credentials(user_id=current_user.user_id)
    return AssistantDocoSettingsResponse(
        configured=credentials is not None,
        default_knowledge_base_id=credentials.default_knowledge_base_id if credentials else None,
        updated_at=credentials.updated_at if credentials else None,
    )


@router.put("/doco/settings", response_model=AssistantDocoSettingsResponse)
def update_doco_settings(
    body: AssistantDocoSettingsRequest,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_store: SQLiteAssistantStore = Depends(get_assistant_store),
) -> AssistantDocoSettingsResponse:
    now = datetime.now(timezone.utc)
    existing = assistant_store.get_doco_credentials(user_id=current_user.user_id)
    api_token = body.api_token or (existing.api_token if existing else "")
    if not api_token:
        raise HTTPException(status_code=400, detail="请先配置当前用户的 Doco Token。")
    credentials = assistant_store.upsert_doco_credentials(
        DocoCredentialRecord(
            user_id=current_user.user_id,
            api_token=api_token,
            default_knowledge_base_id=body.default_knowledge_base_id,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )
    )
    return AssistantDocoSettingsResponse(
        configured=True,
        default_knowledge_base_id=credentials.default_knowledge_base_id,
        updated_at=credentials.updated_at,
    )


@router.delete("/doco/settings", response_model=AssistantDocoSettingsResponse)
def delete_doco_settings(
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_store: SQLiteAssistantStore = Depends(get_assistant_store),
) -> AssistantDocoSettingsResponse:
    assistant_store.delete_doco_credentials(user_id=current_user.user_id)
    return AssistantDocoSettingsResponse(configured=False)


@router.post("/sessions", response_model=AssistantSessionResponse, status_code=status.HTTP_201_CREATED)
def create_session(
    body: AssistantCreateSessionRequest,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> AssistantSessionResponse:
    try:
        session = assistant_service.create_session(current_user, body.title, organization_key=body.organization_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_session(session)


@router.get("/sessions", response_model=list[AssistantSessionResponse])
def list_sessions(
    query: str | None = Query(default=None, max_length=120),
    favorited_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> list[AssistantSessionResponse]:
    started_at = perf_counter()
    sessions = assistant_service.list_sessions(
        current_user,
        query=query,
        favorited_only=favorited_only,
        limit=limit,
        offset=offset,
    )
    query_elapsed_ms = (perf_counter() - started_at) * 1000

    serialize_started_at = perf_counter()
    response = [_serialize_session(session) for session in sessions]
    serialize_elapsed_ms = (perf_counter() - serialize_started_at) * 1000
    total_elapsed_ms = (perf_counter() - started_at) * 1000
    if total_elapsed_ms >= settings.slow_request_threshold_ms:
        logger.warning(
            "慢会话列表: total_ms=%.1f query_ms=%.1f serialize_ms=%.1f sessions=%d limit=%d offset=%d has_query=%s",
            total_elapsed_ms,
            query_elapsed_ms,
            serialize_elapsed_ms,
            len(sessions),
            limit,
            offset,
            bool(query),
        )
    return response


@router.put("/sessions/{session_id}/favorite", response_model=AssistantSessionFavoriteResponse)
def favorite_session(
    session_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> AssistantSessionFavoriteResponse:
    try:
        assistant_service.set_session_favorite(current_user, session_id, is_favorited=True)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return AssistantSessionFavoriteResponse(session_id=session_id, is_favorited=True)


@router.delete("/sessions/{session_id}/favorite", response_model=AssistantSessionFavoriteResponse)
def unfavorite_session(
    session_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> AssistantSessionFavoriteResponse:
    try:
        assistant_service.set_session_favorite(current_user, session_id, is_favorited=False)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return AssistantSessionFavoriteResponse(session_id=session_id, is_favorited=False)


@router.get("/sessions/{session_id}", response_model=AssistantSessionDetailResponse)
def get_session(
    session_id: str,
    include_latest_trace: bool = Query(default=False),
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
) -> AssistantSessionDetailResponse:
    total_started_at = perf_counter()
    stage_timings: dict[str, float] = {}

    def record_stage(stage: str, started_at: float) -> None:
        stage_timings[stage] = (perf_counter() - started_at) * 1000

    stage_started_at = perf_counter()
    session = assistant_service.get_session(current_user, session_id)
    record_stage("session", stage_started_at)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在。")

    stage_started_at = perf_counter()
    messages = assistant_service.get_session_messages(current_user, session_id)
    record_stage("messages", stage_started_at)

    stage_started_at = perf_counter()
    turn_id_by_message_id = _build_turn_id_by_message_id(
        assistant_service.store.list_turns(session_id, current_user.user_id)
    )
    requester_by_turn = assistant_service.store.list_shared_follow_up_requesters_by_turn(session_id)
    git_scopes_by_turn = assistant_service.store.list_session_turn_git_context_snapshots(session_id)
    record_stage("turns", stage_started_at)

    stage_started_at = perf_counter()
    active_turn = assistant_service.get_active_session_turn(current_user, session_id)
    session.has_active_turn = active_turn is not None
    active_turn_requester = None
    if active_turn is not None:
        requester_id = requester_by_turn.get(active_turn.turn_id)
        if requester_id is not None:
            active_turn_requester = _serialize_share_user(
                auth_store.get_user_by_id(requester_id),
                requester_id,
            )
    record_stage("active_turn", stage_started_at)

    stage_started_at = perf_counter()
    latest_turn = assistant_service.get_latest_session_turn(current_user, session_id)
    record_stage("latest_turn", stage_started_at)

    latest_turn_trace = None
    if include_latest_trace:
        stage_started_at = perf_counter()
        latest_turn_trace = assistant_service.get_latest_session_turn_trace(current_user, session_id)
        record_stage("latest_trace", stage_started_at)

    stage_started_at = perf_counter()
    citations_by_message = assistant_service.store.list_citations_by_session_id(session_id)
    record_stage("citations", stage_started_at)

    stage_started_at = perf_counter()
    artifacts_by_message = _group_file_artifacts_by_message(
        assistant_service.list_session_file_artifacts(session_id),
        download_url_builder=lambda artifact: _build_owner_file_download_url(session_id, artifact),
    )
    record_stage("file_artifacts", stage_started_at)

    stage_started_at = perf_counter()
    context_requests_by_message = assistant_service.store.list_context_requests_by_session_id(session_id)
    record_stage("context_requests", stage_started_at)

    timeline_notices = _serialize_timeline_notices(
        assistant_service.store.list_runtime_activity_events_by_kinds(
            session_id,
            ("image_quota", "git_context_updated"),
        )
    )

    stage_started_at = perf_counter()
    mounts = assistant_service.list_session_mounts(current_user, session_id)
    record_stage("mounts", stage_started_at)

    show_message_authors = (
        bool(requester_by_turn)
        or assistant_service.get_session_share(current_user, session_id) is not None
    )
    stage_started_at = perf_counter()
    response = AssistantSessionDetailResponse(
        session=_serialize_session(session),
        messages=[
            _serialize_message(
                message,
                citations_by_message.get(message.message_id, []),
                context_requests_by_message.get(message.message_id, []),
                turn_id=turn_id_by_message_id.get(message.message_id),
                file_artifacts=artifacts_by_message.get(message.message_id, []),
                git_scopes=git_scopes_by_turn.get(turn_id_by_message_id.get(message.message_id, ""), []),
                author=(
                    _serialize_shared_message_author(
                        message,
                        turn_id_by_message_id.get(message.message_id),
                        requester_by_turn,
                        session.user_id,
                        auth_store,
                    )
                    if show_message_authors
                    else None
                ),
            )
            for message in messages
        ],
        timeline_notices=timeline_notices,
        test_data_plans=[
            _serialize_test_data_plan(plan)
            for plan in assistant_service.list_session_test_data_plans(current_user, session_id)
        ],
        mcp_approvals=assistant_service.mcp_approvals.list_for_session(session_id),
        mounts=[_serialize_context_item(mount) for mount in mounts],
        active_turn=_serialize_turn(active_turn) if active_turn is not None else None,
        active_turn_requested_by=active_turn_requester,
        latest_turn=_serialize_turn(latest_turn) if latest_turn is not None else None,
        latest_turn_trace=_serialize_turn_trace(latest_turn_trace).model_dump(mode="json") if latest_turn_trace is not None else None,
    )
    record_stage("serialize", stage_started_at)

    total_elapsed_ms = (perf_counter() - total_started_at) * 1000
    if total_elapsed_ms >= settings.slow_request_threshold_ms:
        logger.warning(
            "慢会话详情: session_id=%s total_ms=%.1f session_ms=%.1f messages_ms=%.1f active_turn_ms=%.1f "
            "turns_ms=%.1f latest_turn_ms=%.1f latest_trace_ms=%.1f citations_ms=%.1f context_requests_ms=%.1f "
            "file_artifacts_ms=%.1f mounts_ms=%.1f serialize_ms=%.1f messages=%d citations=%d context_requests=%d mounts=%d include_latest_trace=%s",
            session_id,
            total_elapsed_ms,
            stage_timings.get("session", 0.0),
            stage_timings.get("messages", 0.0),
            stage_timings.get("active_turn", 0.0),
            stage_timings.get("turns", 0.0),
            stage_timings.get("latest_turn", 0.0),
            stage_timings.get("latest_trace", 0.0),
            stage_timings.get("citations", 0.0),
            stage_timings.get("context_requests", 0.0),
            stage_timings.get("file_artifacts", 0.0),
            stage_timings.get("mounts", 0.0),
            stage_timings.get("serialize", 0.0),
            len(messages),
            sum(len(citations) for citations in citations_by_message.values()),
            sum(len(requests) for requests in context_requests_by_message.values()),
            len(mounts),
            include_latest_trace,
        )
    return response


@router.post(
    "/sessions/{session_id}/test-data-plans/{plan_id}/reveal-secret",
    response_model=AssistantTestDataSecretResponse,
)
def reveal_test_data_plan_secret(
    session_id: str,
    plan_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> AssistantTestDataSecretResponse:
    try:
        secret = assistant_service.consume_session_test_data_plan_secret(
            current_user,
            session_id,
            plan_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return AssistantTestDataSecretResponse(plan_id=plan_id, secret=secret)


@router.patch("/sessions/{session_id}", response_model=AssistantSessionResponse)
def rename_session(
    session_id: str,
    body: AssistantRenameSessionRequest,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> AssistantSessionResponse:
    session = assistant_service.rename_session(current_user, session_id, body.title)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在。")
    return _serialize_session(session)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    session_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> Response:
    deleted = assistant_service.delete_session(current_user, session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="会话不存在。")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/users", response_model=list[AssistantShareUserResponse])
def search_users(
    query: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=20, ge=1, le=50),
    current_user: UserRecord = Depends(get_current_app_user),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
) -> list[AssistantShareUserResponse]:
    del current_user
    users = auth_store.search_users(query or "", limit=limit)
    return [_serialize_share_user(user) for user in users]


@router.get("/image-generation/quota", response_model=ImageGenerationQuotaResponse)
def get_image_generation_quota(
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_store: SQLiteAssistantStore = Depends(get_assistant_store),
) -> ImageGenerationQuotaResponse:
    now = datetime.now(timezone.utc)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
    daily_limit = settings.image_generation_daily_limit
    weekly_limit = settings.image_generation_weekly_limit
    return ImageGenerationQuotaResponse(
        daily=ImageGenerationQuotaWindow(
            used=assistant_store.count_image_generation_usage(user_id=current_user.user_id, since=day_start),
            limit=daily_limit,
            resets_at=day_start + timedelta(days=1) if daily_limit > 0 else None,
        ),
        weekly=ImageGenerationQuotaWindow(
            used=assistant_store.count_image_generation_usage(user_id=current_user.user_id, since=week_start),
            limit=weekly_limit,
            resets_at=week_start + timedelta(days=7) if weekly_limit > 0 else None,
        ),
    )


@router.put("/sessions/{session_id}/share", response_model=AssistantShareResponse)
def upsert_session_share(
    session_id: str,
    body: AssistantShareRequest,
    request: Request,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    notification_service: AuthNotificationService = Depends(get_notification_service),
) -> AssistantShareResponse:
    member_user_ids = [user_id for user_id in body.member_user_ids if user_id != current_user.user_id]
    if body.share_type == "members" and not member_user_ids:
        raise HTTPException(status_code=400, detail="members 模式至少需要选择一个成员。")
    missing_member_ids = [
        user_id
        for user_id in member_user_ids
        if auth_store.get_user_by_id(user_id) is None
    ]
    if missing_member_ids:
        raise HTTPException(status_code=400, detail="共享成员不存在。")

    try:
        previous_share = assistant_service.get_session_share(current_user, session_id)
        share = assistant_service.upsert_session_share(
            current_user,
            session_id,
            share_type=body.share_type,
            member_user_ids=member_user_ids,
            visibility=body.visibility,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    previous_member_ids = set(previous_share.member_user_ids) if previous_share is not None else set()
    new_member_ids = [
        member_user_id
        for member_user_id in share.member_user_ids
        if member_user_id not in previous_member_ids
    ]
    share_url = _build_share_url(request, share.share_token)
    for member in auth_store.list_users_by_ids(new_member_ids):
        notification_service.notify_session_shared(
            owner=current_user,
            recipient=member,
            share_url=share_url,
        )
    return _serialize_share(share, request, auth_store)


@router.get("/sessions/{session_id}/share")
def get_session_share(
    session_id: str,
    request: Request,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
):
    try:
        share = assistant_service.get_session_share(current_user, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if share is None:
        return None
    return _serialize_share(share, request, auth_store)


@router.delete("/sessions/{session_id}/share", status_code=status.HTTP_204_NO_CONTENT)
def revoke_session_share(
    session_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> Response:
    try:
        assistant_service.revoke_session_share(current_user, session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/shared", response_model=list[AssistantSharedSessionSummaryResponse])
def list_shared_sessions(
    request: Request,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
) -> list[AssistantSharedSessionSummaryResponse]:
    summaries = assistant_service.list_shared_sessions(current_user)
    return [
        _serialize_shared_summary(summary, request, auth_store, viewer_user_id=current_user.user_id)
        for summary in summaries
    ]


@router.get("/shared/{share_token}", response_model=AssistantSharedSessionDetailResponse)
async def get_shared_session(
    share_token: str,
    request: Request,
    current_user: UserRecord | None = Depends(get_optional_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
) -> AssistantSharedSessionDetailResponse:
    shared = assistant_service.get_shared_session(current_user, share_token)
    if shared is None:
        raise HTTPException(status_code=404, detail="共享会话不存在或无权限访问。")
    summary, messages = shared
    turn_id_by_message_id = _build_turn_id_by_message_id(
        assistant_service.store.list_turns(summary.session.session_id, summary.session.user_id)
    )
    requester_by_turn = assistant_service.store.list_shared_follow_up_requesters_by_turn(
        summary.session.session_id
    )
    git_scopes_by_turn = assistant_service.store.list_session_turn_git_context_snapshots(
        summary.session.session_id
    )
    citations_by_message = assistant_service.store.list_citations_by_session_id(summary.session.session_id)
    context_requests_by_message = assistant_service.store.list_context_requests_by_session_id(summary.session.session_id)
    artifacts_by_message = _group_file_artifacts_by_message(
        assistant_service.list_session_file_artifacts(summary.session.session_id),
        download_url_builder=lambda artifact: _build_shared_file_download_url(share_token, artifact),
    )
    owner = auth_store.get_user_by_id(summary.session.user_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="共享会话所有者不存在。")
    active_turn = assistant_service.get_active_session_turn(owner, summary.session.session_id)
    summary.session.has_active_turn = active_turn is not None
    assistant_service.store.reconcile_processing_shared_follow_ups(summary.session.session_id)
    pending_follow_ups = assistant_service.store.list_pending_shared_follow_ups(summary.session.session_id)
    failed_follow_ups = assistant_service.store.list_recent_failed_shared_follow_ups(summary.session.session_id)
    runtime_rebuild_pending = any(
        requires_runtime_rebuild(item.error_message) for item in failed_follow_ups
    )
    if pending_follow_ups and not runtime_rebuild_pending:
        assistant_service.start_shared_follow_up_runner(owner, summary.session.session_id)
    return AssistantSharedSessionDetailResponse(
        share=_serialize_shared_summary(
            summary,
            request,
            auth_store,
            viewer_user_id=current_user.user_id if current_user is not None else None,
        ),
        messages=[
            _serialize_message(
                message,
                citations_by_message.get(message.message_id, []),
                context_requests_by_message.get(message.message_id, []),
                turn_id=turn_id_by_message_id.get(message.message_id),
                file_artifacts=artifacts_by_message.get(message.message_id, []),
                git_scopes=git_scopes_by_turn.get(turn_id_by_message_id.get(message.message_id, ""), []),
                author=_serialize_shared_message_author(
                    message,
                    turn_id_by_message_id.get(message.message_id),
                    requester_by_turn,
                    summary.session.user_id,
                    auth_store,
                ),
            )
            for message in messages
        ],
        timeline_notices=_serialize_timeline_notices(
            assistant_service.store.list_runtime_activity_events_by_kinds(
                summary.session.session_id,
                ("image_quota", "git_context_updated"),
            )
        ),
        active_turn=_serialize_turn(active_turn) if active_turn is not None else None,
        pending_follow_ups=[
            _serialize_shared_follow_up(
                item,
                auth_store,
                queue_position=index,
                current_user_id=current_user.user_id if current_user is not None else None,
                owner_id=summary.session.user_id,
            )
            for index, item in enumerate(pending_follow_ups, start=1)
        ],
        failed_follow_ups=[
            _serialize_shared_follow_up(
                item,
                auth_store,
                queue_position=0,
                current_user_id=current_user.user_id if current_user is not None else None,
                owner_id=summary.session.user_id,
            )
            for item in failed_follow_ups
        ],
    )


@router.post(
    "/shared/{share_token}/follow-ups",
    response_model=AssistantSharedFollowUpResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_shared_follow_up(
    share_token: str,
    body: AssistantSharedFollowUpRequest,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
) -> AssistantSharedFollowUpResponse:
    try:
        follow_up, session = assistant_service.enqueue_shared_follow_up(
            current_user,
            share_token,
            body.message,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    owner = auth_store.get_user_by_id(session.user_id)
    if owner is None:
        assistant_service.store.complete_shared_follow_up(
            follow_up.request_id,
            status="failed",
            error_message="共享会话所有者不存在。",
        )
        raise HTTPException(status_code=404, detail="共享会话所有者不存在。")
    assistant_service.start_shared_follow_up_runner(owner, session.session_id)
    pending = assistant_service.store.list_pending_shared_follow_ups(session.session_id)
    queue_position = next(
        (index for index, item in enumerate(pending, start=1) if item.request_id == follow_up.request_id),
        0,
    )
    return _serialize_shared_follow_up(
        follow_up,
        auth_store,
        queue_position=queue_position,
        current_user_id=current_user.user_id,
        owner_id=session.user_id,
    )


@router.post(
    "/shared/{share_token}/follow-ups/{request_id}/rebuild-runtime",
    response_model=AssistantSharedFollowUpResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def rebuild_shared_follow_up_runtime(
    share_token: str,
    request_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
) -> AssistantSharedFollowUpResponse:
    shared = assistant_service.get_shared_session(current_user, share_token)
    if shared is None:
        raise HTTPException(status_code=404, detail="共享会话不存在或无权限访问。")
    summary, _messages = shared
    owner = auth_store.get_user_by_id(summary.session.user_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="共享会话所有者不存在。")
    try:
        follow_up, session = assistant_service.rebuild_shared_follow_up_runtime(
            current_user,
            owner,
            share_token,
            request_id,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except AssistantSessionBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_shared_follow_up(
        follow_up,
        auth_store,
        queue_position=1,
        current_user_id=current_user.user_id,
        owner_id=session.user_id,
    )


@router.get("/sessions/{session_id}/files/{artifact_id}")
def download_session_file_artifact(
    session_id: str,
    artifact_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> FileResponse:
    download = assistant_service.get_owner_file_artifact_download(current_user, session_id, artifact_id)
    if download is None:
        raise HTTPException(status_code=404, detail="附件不存在或无权限访问。")
    return FileResponse(download.file_path, media_type=download.mime_type, filename=download.filename)


@router.get("/shared/{share_token}/files/{artifact_id}")
def download_shared_file_artifact(
    share_token: str,
    artifact_id: str,
    current_user: UserRecord | None = Depends(get_optional_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> FileResponse:
    download = assistant_service.get_shared_file_artifact_download(current_user, share_token, artifact_id)
    if download is None:
        raise HTTPException(status_code=404, detail="附件不存在或无权限访问。")
    return FileResponse(download.file_path, media_type=download.mime_type, filename=download.filename)


@router.get("/sessions/{session_id}/comments", response_model=list[AssistantCommentResponse])
def list_session_comments(
    session_id: str,
    message_id: str | None = Query(default=None),
    current_user: UserRecord | None = Depends(get_optional_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
) -> list[AssistantCommentResponse]:
    try:
        comments = assistant_service.list_session_comments(current_user, session_id, message_id=message_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [_serialize_comment(comment, current_user, auth_store) for comment in comments]


@router.post("/sessions/{session_id}/comments", response_model=AssistantCommentResponse, status_code=status.HTTP_201_CREATED)
def create_session_comment(
    session_id: str,
    body: AssistantCommentCreateRequest,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
) -> AssistantCommentResponse:
    try:
        comment = assistant_service.create_session_comment(
            current_user,
            session_id,
            content=body.content,
            message_id=body.message_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _serialize_comment(comment, current_user, auth_store)


@router.patch("/comments/{comment_id}", response_model=AssistantCommentResponse)
def update_session_comment(
    comment_id: str,
    body: AssistantCommentUpdateRequest,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
) -> AssistantCommentResponse:
    comment = assistant_service.update_session_comment(current_user, comment_id, body.content)
    if comment is None:
        raise HTTPException(status_code=404, detail="评论不存在或无权限编辑。")
    return _serialize_comment(comment, current_user, auth_store)


@router.delete("/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session_comment(
    comment_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> Response:
    deleted = assistant_service.delete_session_comment(current_user, comment_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="评论不存在或无权限删除。")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/uploads/images", response_model=AssistantUploadedImageResponse, status_code=status.HTTP_201_CREATED)
async def upload_image(
    request: Request,
    filename: str = Query(default="image", max_length=160),
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> AssistantUploadedImageResponse:
    data = await request.body()
    content_type = request.headers.get("content-type", "")
    try:
        uploaded = assistant_service.save_uploaded_image(
            user=current_user,
            original_filename=filename,
            content_type=content_type,
            data=data,
            upload_root=settings.assistant_image_upload_root,
            max_bytes=settings.assistant_image_upload_max_bytes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_uploaded_image(uploaded)


@router.post("/uploads/files", response_model=AssistantUploadedFileResponse, status_code=status.HTTP_201_CREATED)
async def upload_file(
    request: Request,
    filename: str = Query(default="file", max_length=160),
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> AssistantUploadedFileResponse:
    data = await request.body()
    content_type = request.headers.get("content-type", "")
    try:
        uploaded = assistant_service.save_uploaded_file(
            user=current_user,
            original_filename=filename,
            content_type=content_type,
            data=data,
            upload_root=settings.assistant_image_upload_root,
            max_bytes=settings.assistant_image_upload_max_bytes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _serialize_uploaded_file(uploaded)


@router.get("/uploads/images/{image_id}")
def get_uploaded_image(
    image_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> FileResponse:
    try:
        image = assistant_service.get_uploaded_image_file(
            user=current_user,
            image_id=image_id,
            upload_root=settings.assistant_image_upload_root,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if image is None:
        raise HTTPException(status_code=404, detail="图片不存在或无权限访问。")

    return FileResponse(
        image.file_path,
        media_type=image.mime_type,
        filename=image.file_name,
        content_disposition_type="inline",
    )


@router.post("/chat", response_model=AssistantChatResponse)
async def chat(
    body: AssistantChatRequest,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> AssistantChatResponse:
    try:
        result = await assistant_service.chat(
            current_user,
            body.session_id,
            body.message,
            context_items=[_deserialize_context_item(item) for item in body.context_items],
            organization_key=body.organization_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssistantSessionBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AssistantRuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return AssistantChatResponse(
        session=_serialize_session(result.session),
        user_message=_serialize_message(
            result.user_message,
            turn_id=result.turn.turn_id,
            git_scopes=assistant_service.store.list_turn_git_context_snapshots(result.turn.turn_id),
        ),
        assistant_message=_serialize_message(
            result.assistant_message,
            turn_id=result.turn.turn_id,
            git_scopes=assistant_service.store.list_turn_git_context_snapshots(result.turn.turn_id),
        ),
        session_created=result.session_created,
        turn_id=result.turn.turn_id,
        citations=[_serialize_citation(citation) for citation in result.citations],
    )


@router.put("/messages/{message_id}/feedback", response_model=AssistantMessageResponse)
def set_message_feedback(
    message_id: str,
    body: AssistantMessageFeedbackRequest,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> AssistantMessageResponse:
    try:
        message = assistant_service.set_message_feedback(current_user, message_id, body.feedback)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _serialize_message(message)


@router.post("/chat/stream")
async def stream_chat(
    body: AssistantChatRequest,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> StreamingResponse:
    try:
        if body.session_id:
            assistant_service.ensure_session_can_start_turn(current_user, body.session_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssistantSessionBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def event_generator():
        try:
            async for event in assistant_service.stream_chat(
                current_user,
                body.session_id,
                body.message,
                context_items=[_deserialize_context_item(item) for item in body.context_items],
                organization_key=body.organization_key,
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except ValueError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        except AssistantSessionBusyError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        except AssistantRuntimeError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/turns/{turn_id}/stream")
async def resume_turn_stream(
    turn_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> StreamingResponse:
    async def event_generator():
        try:
            async for event in assistant_service.resume_turn_stream(current_user, turn_id):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except ValueError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        except AssistantRuntimeError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/shared/{share_token}/turns/{turn_id}/stream")
async def resume_shared_turn_stream(
    share_token: str,
    turn_id: str,
    current_user: UserRecord | None = Depends(get_optional_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> StreamingResponse:
    shared = assistant_service.get_shared_session(current_user, share_token)
    turn = assistant_service.store.get_turn(turn_id)
    if shared is None or turn is None or turn.session_id != shared[0].session.session_id:
        raise HTTPException(status_code=404, detail="轮次不存在或无权限访问。")

    async def event_generator():
        try:
            async for event in assistant_service.resume_shared_turn_stream(current_user, share_token, turn_id):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except ValueError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        except AssistantRuntimeError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/turns/{turn_id}/mcp-approvals/{approval_id}", response_model=AssistantMcpApprovalResponse)
async def decide_mcp_approval(
    turn_id: str,
    approval_id: str,
    body: AssistantMcpApprovalDecisionRequest,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> AssistantMcpApprovalResponse:
    try:
        record = assistant_service.decide_mcp_approval(current_user, turn_id, approval_id, body.decision)
    except McpApprovalConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return AssistantMcpApprovalResponse(**record)


@router.post("/turns/{turn_id}/stop", response_model=AssistantTurnResponse)
async def stop_turn(
    turn_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> AssistantTurnResponse:
    try:
        turn = await assistant_service.stop_turn(current_user, turn_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _serialize_turn(turn)


@router.post("/turns/{turn_id}/rerun/stream")
async def rerun_turn_stream(
    turn_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> StreamingResponse:
    async def event_generator():
        try:
            async for event in assistant_service.stream_rerun_turn(current_user, turn_id):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except ValueError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        except AssistantSessionBusyError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        except AssistantRuntimeError as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/skills", response_model=list[AssistantSkillResponse])
async def list_skills(
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> list[AssistantSkillResponse]:
    del current_user
    skills = await assistant_service.list_available_skills()
    return [AssistantSkillResponse(**skill) for skill in skills]


@router.post("/sessions/{session_id}/mounts", response_model=list[AssistantContextItem])
def replace_session_mounts(
    session_id: str,
    body: AssistantMountsRequest,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> list[AssistantContextItem]:
    try:
        mounts = assistant_service.replace_session_mounts(
            current_user,
            session_id,
            [_deserialize_context_item(item) for item in body.mounts],
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return [_serialize_context_item(mount) for mount in mounts]


@router.get("/sessions/{session_id}/turns/{turn_id}/trace", response_model=AssistantTurnTraceResponse)
def get_turn_trace(
    session_id: str,
    turn_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> AssistantTurnTraceResponse:
    trace = assistant_service.get_turn_trace(current_user, session_id, turn_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="轮次不存在。")
    return _serialize_turn_trace(trace)


@router.get("/sessions/{session_id}/latest-trace", response_model=AssistantTurnTraceResponse | None)
def get_latest_turn_trace(
    session_id: str,
    current_user: UserRecord = Depends(get_current_app_user),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> AssistantTurnTraceResponse | None:
    session = assistant_service.get_session(current_user, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="会话不存在。")

    trace = assistant_service.get_latest_session_turn_trace(current_user, session_id)
    return _serialize_turn_trace(trace) if trace is not None else None


def _build_share_url(request: Request, share_token: str) -> str:
    base_url = _resolve_frontend_base_url(request)
    return f"{base_url}/#/shared/{share_token}"


def _resolve_frontend_base_url(request: Request) -> str:
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")

    referer = request.headers.get("referer")
    if referer:
        parsed = urlsplit(referer)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"

    return str(request.base_url).rstrip("/")


def _serialize_share_user(user: UserRecord | None, fallback_user_id: str | None = None) -> AssistantShareUserResponse:
    if user is None:
        return AssistantShareUserResponse(
            user_id=fallback_user_id or "",
            email="",
            name="未知用户",
            avatar_url=None,
        )
    return AssistantShareUserResponse(
        user_id=user.user_id,
        email=user.email,
        name=user.name,
        avatar_url=user.avatar_url,
    )


def _serialize_share(
    share: SessionShareRecord,
    request: Request,
    auth_store: SQLiteAuthStore,
) -> AssistantShareResponse:
    members = auth_store.list_users_by_ids(share.member_user_ids)
    return AssistantShareResponse(
        share_id=share.share_id,
        session_id=share.session_id,
        share_type=share.share_type,
        visibility=share.visibility,
        share_token=share.share_token,
        share_url=_build_share_url(request, share.share_token),
        members=[_serialize_share_user(member) for member in members],
        created_at=share.created_at,
    )


def _serialize_shared_summary(
    summary: SharedSessionSummaryRecord,
    request: Request,
    auth_store: SQLiteAuthStore,
    viewer_user_id: str | None = None,
) -> AssistantSharedSessionSummaryResponse:
    owner = auth_store.get_user_by_id(summary.share.owner_id)
    members = (
        auth_store.list_users_by_ids(summary.share.member_user_ids)
        if viewer_user_id == summary.share.owner_id
        else []
    )
    return AssistantSharedSessionSummaryResponse(
        share_id=summary.share.share_id,
        share_type=summary.share.share_type,
        visibility=summary.share.visibility,
        share_token=summary.share.share_token,
        share_url=_build_share_url(request, summary.share.share_token),
        owner=_serialize_share_user(owner, summary.share.owner_id),
        members=[_serialize_share_user(member) for member in members],
        session=_serialize_session(summary.session),
        comment_count=summary.comment_count,
        created_at=summary.share.created_at,
    )


def _serialize_shared_follow_up(
    follow_up: SharedSessionFollowUpRecord,
    auth_store: SQLiteAuthStore,
    *,
    queue_position: int,
    current_user_id: str | None = None,
    owner_id: str | None = None,
) -> AssistantSharedFollowUpResponse:
    requested_by = auth_store.get_user_by_id(follow_up.requested_by_user_id)
    rebuild_required = requires_runtime_rebuild(follow_up.error_message)
    return AssistantSharedFollowUpResponse(
        request_id=follow_up.request_id,
        session_id=follow_up.session_id,
        content=follow_up.content,
        status="running" if follow_up.status == "processing" else follow_up.status,
        requested_by=_serialize_share_user(requested_by, follow_up.requested_by_user_id),
        queue_position=queue_position,
        turn_id=follow_up.turn_id,
        error_message=follow_up.error_message,
        requires_runtime_rebuild=rebuild_required,
        can_rebuild_runtime=(
            rebuild_required
            and current_user_id is not None
            and current_user_id in {follow_up.requested_by_user_id, owner_id}
        ),
        created_at=follow_up.created_at,
    )


def _serialize_shared_message_author(
    message: AssistantMessageRecord,
    turn_id: str | None,
    requester_by_turn: dict[str, str],
    owner_user_id: str,
    auth_store: SQLiteAuthStore,
) -> AssistantShareUserResponse | None:
    if message.role != "user":
        return None
    author_user_id = requester_by_turn.get(turn_id) if turn_id is not None else None
    author_user_id = author_user_id or owner_user_id
    return _serialize_share_user(auth_store.get_user_by_id(author_user_id), author_user_id)


def _build_owner_file_download_url(session_id: str, artifact: AssistantFileArtifactRecord) -> str | None:
    if artifact.storage_status == "missing":
        return None
    return f"/api/assistant/sessions/{session_id}/files/{artifact.artifact_id}"


def _build_shared_file_download_url(share_token: str, artifact: AssistantFileArtifactRecord) -> str | None:
    if artifact.storage_status == "missing":
        return None
    return f"/api/assistant/shared/{share_token}/files/{artifact.artifact_id}"


def _serialize_file_artifact(
    artifact: AssistantFileArtifactRecord,
    *,
    download_url: str | None,
) -> AssistantFileArtifactResponse:
    return AssistantFileArtifactResponse(
        artifact_id=artifact.artifact_id,
        display_path=artifact.display_path,
        filename=artifact.filename,
        mime_type=artifact.mime_type,
        size_bytes=artifact.size_bytes,
        storage_status=artifact.storage_status,
        download_url=download_url,
    )


def _group_file_artifacts_by_message(
    artifacts: list[AssistantFileArtifactRecord],
    *,
    download_url_builder,
) -> dict[str, list[AssistantFileArtifactResponse]]:
    result: dict[str, list[AssistantFileArtifactResponse]] = {}
    for artifact in artifacts:
        if artifact.message_id is None:
            continue
        result.setdefault(artifact.message_id, []).append(
            _serialize_file_artifact(artifact, download_url=download_url_builder(artifact))
        )
    return result


def _serialize_comment(
    comment: SessionCommentRecord,
    current_user: UserRecord | None,
    auth_store: SQLiteAuthStore,
) -> AssistantCommentResponse:
    author = auth_store.get_user_by_id(comment.user_id)
    return AssistantCommentResponse(
        comment_id=comment.comment_id,
        session_id=comment.session_id,
        message_id=comment.message_id,
        user=_serialize_share_user(author, comment.user_id),
        content=comment.content,
        created_at=comment.created_at,
        updated_at=comment.updated_at,
        can_edit=current_user is not None and comment.user_id == current_user.user_id,
    )


def _serialize_session(session: AssistantSessionRecord) -> AssistantSessionResponse:
    return AssistantSessionResponse(
        session_id=session.session_id,
        title=session.title,
        message_count=session.message_count,
        knowledge_scope_ids=list(session.knowledge_scope_ids),
        last_message_preview=session.last_message_preview,
        created_at=session.created_at,
        updated_at=session.updated_at,
        runtime_provider=session.runtime_provider,
        runtime_session_id=session.runtime_session_id,
        runtime_working_directory=_sanitize_runtime_working_directory(session),
        active_organization_key=session.active_organization_key,
        runtime_workspace_profile=_sanitize_runtime_workspace_profile(session.runtime_workspace_profile),
        status=session.status,
        has_active_turn=session.has_active_turn,
        is_favorited=session.is_favorited,
        git_scopes=[_serialize_git_scope(item, session_default=True) for item in session.git_context_defaults],
    )


def _sanitize_runtime_working_directory(session: AssistantSessionRecord) -> str | None:
    profile = session.runtime_workspace_profile or {}
    sandbox_cwd = profile.get("sandbox_cwd")
    if isinstance(sandbox_cwd, str) and sandbox_cwd:
        return sandbox_cwd
    return None


def _sanitize_runtime_workspace_profile(profile: dict[str, object] | None) -> dict[str, object] | None:
    if not profile:
        return None
    sanitized: dict[str, object] = {
        "session_id": profile.get("session_id"),
        "organization_key": profile.get("organization_key"),
        "sandbox_cwd": profile.get("sandbox_cwd"),
    }
    mounts = profile.get("mounts")
    if isinstance(mounts, list):
        sanitized["mounts"] = [
            {
                "workspace_key": mount.get("workspace_key"),
                "sandbox_path": mount.get("sandbox_path"),
                "permission": mount.get("permission"),
            }
            for mount in mounts
            if isinstance(mount, dict)
        ]
    return sanitized


def _serialize_message(
    message: AssistantMessageRecord,
    citations: list | None = None,
    context_requests: list | None = None,
    *,
    turn_id: str | None = None,
    file_artifacts: list[AssistantFileArtifactResponse] | None = None,
    git_scopes: list[object] | None = None,
    author: AssistantShareUserResponse | None = None,
) -> AssistantMessageResponse:
    return AssistantMessageResponse(
        message_id=message.message_id,
        session_id=message.session_id,
        turn_id=turn_id,
        role=message.role,
        content=message.content,
        created_at=message.created_at,
        feedback=message.feedback,
        citations=[_serialize_citation(c) for c in (citations or [])],
        context_requests=[_serialize_context_item(item) for item in (context_requests or [])],
        file_artifacts=file_artifacts or [],
        git_scopes=[_serialize_git_scope(item) for item in (git_scopes or [])],
        author=author,
    )


def _serialize_git_scope(item: object, *, session_default: bool = False) -> AssistantGitScopeResponse:
    return AssistantGitScopeResponse(
        repo_full_name=str(getattr(item, "repo_full_name")),
        strategy=str(
            getattr(item, "default_strategy")
            if session_default
            else getattr(item, "strategy")
        ),
        selector_type=getattr(item, "selector_type"),
        selector_value=getattr(item, "selector_value"),
        resolved_sha=(
            getattr(item, "last_resolved_sha")
            if session_default
            else getattr(item, "resolved_sha")
        ),
    )


def _build_turn_id_by_message_id(turns: list[object]) -> dict[str, str]:
    turn_id_by_message_id: dict[str, str] = {}
    for turn in turns:
        turn_id = str(getattr(turn, "turn_id"))
        user_message_id = getattr(turn, "user_message_id", None)
        assistant_message_id = getattr(turn, "assistant_message_id", None)
        if user_message_id and user_message_id not in turn_id_by_message_id:
            turn_id_by_message_id[str(user_message_id)] = turn_id
        if assistant_message_id:
            turn_id_by_message_id[str(assistant_message_id)] = turn_id
    return turn_id_by_message_id


def _serialize_turn(turn: object) -> AssistantTurnResponse:
    return AssistantTurnResponse(
        turn_id=str(getattr(turn, "turn_id")),
        session_id=str(getattr(turn, "session_id")),
        user_message_id=str(getattr(turn, "user_message_id")),
        assistant_message_id=getattr(turn, "assistant_message_id"),
        started_at=getattr(turn, "started_at"),
        completed_at=getattr(turn, "completed_at"),
        status=str(getattr(turn, "status")),
        error_message=getattr(turn, "error_message"),
    )


def _serialize_context_item(item: object) -> AssistantContextItem:
    return AssistantContextItem(
        context_key=str(getattr(item, "context_key")),
        label=str(getattr(item, "label")),
        content=getattr(item, "content"),
        source_type=str(getattr(item, "source_type")),
        source_uri=getattr(item, "source_uri"),
        metadata=dict(getattr(item, "metadata")),
    )


def _serialize_uploaded_image(image: UploadedImageRecord) -> AssistantUploadedImageResponse:
    return AssistantUploadedImageResponse(
        image_id=image.image_id,
        label=image.label,
        file_name=image.file_name,
        source_uri=image.source_uri,
        mime_type=image.mime_type,
        size_bytes=image.size_bytes,
        context_item=_serialize_context_item(image.context_item),
    )


def _serialize_uploaded_file(file: UploadedFileRecord) -> AssistantUploadedFileResponse:
    return AssistantUploadedFileResponse(
        file_id=file.file_id,
        label=file.label,
        file_name=file.file_name,
        source_uri=file.source_uri,
        mime_type=file.mime_type,
        size_bytes=file.size_bytes,
        source_type=file.source_type,
        context_item=_serialize_context_item(file.context_item),
    )


def _deserialize_context_item(item: AssistantContextItem):
    from app.business.assistant.models import AssistantContextInput

    return AssistantContextInput(
        context_key=item.context_key,
        label=item.label,
        content=item.content,
        source_type=item.source_type,
        source_uri=item.source_uri,
        metadata=item.metadata,
    )


def _serialize_citation(citation: object) -> AssistantCitationResponse:
    return AssistantCitationResponse(
        citation_id=str(getattr(citation, "citation_id")),
        citation_type=str(getattr(citation, "citation_type")),
        label=str(getattr(citation, "label")),
        path=getattr(citation, "path"),
        line_start=getattr(citation, "line_start"),
        line_end=getattr(citation, "line_end"),
        snippet=getattr(citation, "snippet"),
        metadata=dict(getattr(citation, "metadata")),
        created_at=getattr(citation, "created_at"),
    )


def _serialize_timeline_notices(events: list[object]) -> list[AssistantTimelineNoticeResponse]:
    notices: list[AssistantTimelineNoticeResponse] = []
    for event in events:
        if getattr(event, "event_type", None) != "activity":
            continue
        payload = dict(getattr(event, "payload", {}) or {})
        kind = str(payload.get("kind") or "")
        if kind not in {"image_quota", "git_context_updated"}:
            continue
        notices.append(
            AssistantTimelineNoticeResponse(
                event_id=str(getattr(event, "event_id")),
                turn_id=str(getattr(event, "turn_id")),
                kind=kind,
                message=str(
                    payload.get("message")
                    or ("本轮生图受到额度限制。" if kind == "image_quota" else "代码上下文已更新。")
                ),
                created_at=getattr(event, "created_at"),
                accepted=int(payload.get("accepted") or 0),
                blocked=int(payload.get("blocked") or 0),
                window=str(payload.get("window") or "") or None,
                used=int(payload["used"]) if payload.get("used") is not None else None,
                limit=int(payload["limit"]) if payload.get("limit") is not None else None,
                resets_at=payload.get("resets_at"),
            )
        )
    return notices


def _serialize_test_data_plan(plan: TestDataPlanRecord) -> AssistantTestDataPlanResponse:
    now = datetime.now(timezone.utc)
    expired = plan.status == "prepared" and plan.expires_at <= now
    status_value = "expired" if expired else plan.status
    can_confirm = status_value == "prepared"
    return AssistantTestDataPlanResponse(
        plan_id=plan.plan_id,
        prepared_turn_id=plan.prepared_turn_id,
        executed_turn_id=plan.executed_turn_id,
        task_name=plan.task_name,
        environment=plan.environment,
        risk_level=plan.risk_level,
        summary=plan.summary,
        status=status_value,
        upstream_run_id=plan.upstream_run_id,
        upstream_status=plan.upstream_status,
        result=plan.result,
        error_message=plan.error_message,
        created_at=plan.created_at,
        expires_at=plan.expires_at,
        executed_at=plan.executed_at,
        can_confirm=can_confirm,
        confirmation_phrase=f"确认执行 {plan.plan_id}" if can_confirm else None,
        has_secret=bool(plan.status == "completed" and plan.secret and not plan.secret_revealed_at),
    )


def _serialize_turn_trace(trace: TurnTrace) -> AssistantTurnTraceResponse:
    return AssistantTurnTraceResponse(
        turn=AssistantTurnResponse(
            turn_id=trace.turn.turn_id,
            session_id=trace.turn.session_id,
            user_message_id=trace.turn.user_message_id,
            assistant_message_id=trace.turn.assistant_message_id,
            started_at=trace.turn.started_at,
            completed_at=trace.turn.completed_at,
            status=trace.turn.status,
            error_message=trace.turn.error_message,
        ),
        runtime_events=[
            {
                "event_id": event["event_id"],
                "type": event["type"],
                "payload": event["payload"],
                "created_at": event["created_at"],
            }
            for event in trace.runtime_events
        ],
        context_requests=[_serialize_context_item(item) for item in trace.context_requests],
        context_usage=[_serialize_context_item(item) for item in trace.context_usage],
        citations=[_serialize_citation(item) for item in trace.citations],
        git_scopes=[_serialize_git_scope(item) for item in trace.git_scopes],
    )
