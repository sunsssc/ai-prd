from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.dependencies import get_active_account_user, get_auth_service, get_notification_service
from app.services.auth_models import UserRecord
from app.services.auth_service import AuthService
from app.services.notification_service import AuthNotificationService

router = APIRouter(prefix="/agent-access", tags=["agent-access"])


class AgentAccessRequestCreate(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class AgentAccessRequestResponse(BaseModel):
    request_id: str
    status: str
    reason: str
    review_comment: str | None = None
    reviewed_at: datetime | None = None
    created_at: datetime


class AgentAccessStatusResponse(BaseModel):
    agent_access: str
    latest_request: AgentAccessRequestResponse | None = None


def _serialize_request(record: object) -> AgentAccessRequestResponse:
    return AgentAccessRequestResponse(
        request_id=str(getattr(record, "request_id")),
        status=str(getattr(record, "status")),
        reason=str(getattr(record, "reason")),
        review_comment=getattr(record, "review_comment"),
        reviewed_at=getattr(record, "reviewed_at"),
        created_at=getattr(record, "created_at"),
    )


@router.post("/request", response_model=AgentAccessStatusResponse)
def request_agent_access(
    body: AgentAccessRequestCreate,
    user: UserRecord = Depends(get_active_account_user),
    auth_service: AuthService = Depends(get_auth_service),
    notification_service: AuthNotificationService = Depends(get_notification_service),
) -> AgentAccessStatusResponse:
    try:
        request, updated_user = auth_service.request_agent_access(user, body.reason)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    notification_service.notify_agent_access_requested(updated_user)
    return AgentAccessStatusResponse(
        agent_access=updated_user.agent_access,
        latest_request=_serialize_request(request),
    )


@router.get("/me", response_model=AgentAccessStatusResponse)
def get_agent_access_status(
    user: UserRecord = Depends(get_active_account_user),
    auth_service: AuthService = Depends(get_auth_service),
) -> AgentAccessStatusResponse:
    latest_request = auth_service.auth_store.get_latest_agent_access_request(user.user_id)
    return AgentAccessStatusResponse(
        agent_access=user.agent_access,
        latest_request=_serialize_request(latest_request) if latest_request else None,
    )
