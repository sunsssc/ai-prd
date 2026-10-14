from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.code_reviews import _serialize_job
from app.business.assistant.service import AssistantService
from app.business.git_context import GitRepositoryCatalog
from app.business.my_tasks import MyTaskService
from app.core.dependencies import (
    get_assistant_service,
    get_current_app_user,
    get_git_repository_catalog,
    get_my_task_service,
)
from app.schemas.my_tasks import (
    MyTaskAssistantSessionRequest,
    MyTaskAssistantSessionResponse,
    MyTaskEnvironmentUpdateRequest,
    MyTaskListResponse,
    MyTaskPullRequestResponse,
    MyTaskResponse,
)
from app.services.auth_models import UserRecord


router = APIRouter(prefix="/my-tasks", tags=["my-tasks"])


@router.get("", response_model=MyTaskListResponse)
def list_my_tasks(
    scope: str = Query(default="active"),
    assignment_scope: str = Query(default="mine"),
    include_cc: bool = Query(default=False),
    statuses: list[str] | None = Query(default=None, alias="status"),
    roles: list[str] | None = Query(default=None, alias="role"),
    query: str = Query(default="", alias="q", max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=100),
    current_user: UserRecord = Depends(get_current_app_user),
    service: MyTaskService = Depends(get_my_task_service),
) -> MyTaskListResponse:
    try:
        result = service.list_tasks(
            email=current_user.email,
            scope=scope,
            assignment_scope=assignment_scope,
            include_cc=include_cc,
            roles=tuple(roles) if roles is not None else None,
            statuses=tuple(statuses) if statuses is not None else None,
            query=query,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return MyTaskListResponse(
        tasks=[
            MyTaskResponse(
                task_id=item.task.task_id,
                title=item.task.title,
                status=item.task.status,
                is_completed=item.is_completed,
                list_name=item.task.list_name,
                clickup_url=item.task.clickup_url,
                priority=item.task.priority,
                updated_at=item.task.updated_at,
                due_date=item.task.due_date,
                source_path=_display_source_path(item.task.source_path),
                designers=list(item.task.designers),
                figma_url=item.task.figma_url,
                relations=list(item.relations),
                detected_environments=list(item.task.detected_environments),
                manual_environment=item.manual_environment,
                pull_requests=[
                    MyTaskPullRequestResponse(
                        task_id=pr.link.task_id,
                        repo_full_name=pr.link.repo_full_name,
                        pr_number=pr.link.pr_number,
                        pr_url=pr.link.pr_url,
                        title=pr.link.title,
                        author_login=pr.link.author_login,
                        state=pr.link.state,
                        base_ref=pr.link.base_ref,
                        github_updated_at=pr.link.github_updated_at,
                        review_job=_serialize_job(pr.review_job, None)
                        if pr.review_job is not None
                        else None,
                    )
                    for pr in item.pull_requests
                ],
            )
            for item in result.items
        ],
        environment_options=list(service.environment_options()),
        total=result.total,
        page=result.page,
        page_size=result.page_size,
        total_pages=result.total_pages,
        scope_counts=result.scope_counts,
        status_options=list(result.status_options),
        active_status_options=list(result.active_status_options),
        completed_status_options=list(result.completed_status_options),
        role_options=list(result.role_options),
    )


@router.put("/{task_id}/environment", response_model=MyTaskListResponse)
def update_my_task_environment(
    task_id: str,
    payload: MyTaskEnvironmentUpdateRequest,
    current_user: UserRecord = Depends(get_current_app_user),
    service: MyTaskService = Depends(get_my_task_service),
) -> MyTaskListResponse:
    try:
        service.set_environment(
            task_id=task_id,
            environment=payload.environment,
            updated_by_email=current_user.email,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return list_my_tasks(
        scope="all",
        include_cc=True,
        statuses=None,
        roles=None,
        query="",
        page=1,
        page_size=50,
        current_user=current_user,
        service=service,
    )


@router.post(
    "/{task_id}/pull-requests/assistant-session",
    response_model=MyTaskAssistantSessionResponse,
)
def create_pull_request_assistant_session(
    task_id: str,
    payload: MyTaskAssistantSessionRequest,
    current_user: UserRecord = Depends(get_current_app_user),
    service: MyTaskService = Depends(get_my_task_service),
    repository_catalog: GitRepositoryCatalog = Depends(get_git_repository_catalog),
    assistant_service: AssistantService = Depends(get_assistant_service),
) -> MyTaskAssistantSessionResponse:
    try:
        service.require_pull_request_access(
            task_id=task_id,
            repo_full_name=payload.repo_full_name,
            pr_number=payload.pr_number,
        )
        repository = repository_catalog.get(payload.repo_full_name)
        session = assistant_service.create_pull_request_session(
            current_user,
            task_id=task_id,
            repo_full_name=payload.repo_full_name,
            pr_number=payload.pr_number,
            resolved_sha=None,
            organization_key=repository.organization_key,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return MyTaskAssistantSessionResponse(
        session_id=session.session_id,
        resolved_sha=None,
    )


def _display_source_path(source_path: str) -> str:
    normalized = source_path.replace("\\", "/")
    if "/workspace/" in normalized:
        normalized = normalized.split("/workspace/", 1)[1]
    normalized = normalized.removeprefix("workspace/")
    return "/" + normalized.lstrip("/")
