import asyncio
import os
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.router import api_router
from app.core.config import BASE_DIR, settings
from app.core.dependencies import (
    get_auth_store,
    get_business_doc_update_service,
    get_code_review_service,
    get_requirement_review_service,
    get_security_scan_service,
)
from app.core.logging import configure_logging
from app.integrations.agent_runtime import perform_claude_runtime_startup_check
from app.jobs.clickup_sync import clickup_sync_loop
from app.jobs.code_sync import code_sync_loop

configure_logging(settings.log_level)
logger = logging.getLogger("uvicorn.error")


def _export_claude_cli_env() -> None:
    """将 Claude CLI 相关配置写入 os.environ 供 claude CLI 子进程继承。
    未配置时不注入，本地可依赖 claude login 状态。
    """
    if settings.ai_cli_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = settings.ai_cli_api_key
    if settings.ai_cli_base_url and not os.environ.get("ANTHROPIC_BASE_URL"):
        os.environ["ANTHROPIC_BASE_URL"] = settings.ai_cli_base_url
    if settings.claude_code_max_output_tokens > 0 and not os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"):
        os.environ["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(settings.claude_code_max_output_tokens)


@asynccontextmanager
async def lifespan(_: FastAPI):
    _export_claude_cli_env()
    get_auth_store()
    logger.info(
        "Assistant startup config: provider=%s python=%s cwd=%s cli_path=%s",
        settings.ai_provider,
        os.sys.executable,
        settings.ai_working_directory,
        settings.ai_cli_path or "<auto>",
    )
    if settings.ai_provider.strip().lower() in {"claude_code", "claude", "anthropic"}:
        report = perform_claude_runtime_startup_check(settings.ai_cli_path)
        logger.info(
            "Claude runtime startup check: sdk_importable=%s sdk_version=%s cli_found=%s cli_path=%s cli_version=%s",
            report["sdk_importable"],
            report["sdk_version"],
            report["cli_found"],
            report["cli_path"],
            report["cli_version"],
        )
    code_review_service = get_code_review_service() if settings.code_review_project_config_path else None
    business_doc_update_service = get_business_doc_update_service()
    code_sync_default_branches: dict[str, str] = {}
    if code_review_service is not None:
        project_config = code_review_service.load_project_config()
        if project_config is not None:
            code_sync_default_branches = {
                Path(repository.workspace_repo_path).name: repository.default_branch
                for repository in project_config.repositories
                if repository.enabled
            }
    code_sync_default_branches.update(business_doc_update_service.code_sync_default_branches())
    code_sync_task = asyncio.create_task(
        code_sync_loop(
            code_root=Path(settings.knowledge_code_root),
            runtime_base_dir=Path(settings.ai_working_directory),
            interval_seconds=settings.code_sync_interval_seconds,
            default_branches=code_sync_default_branches,
            repository_paths=business_doc_update_service.code_sync_repository_paths(),
            on_repo_synced=lambda result: business_doc_update_service.enqueue_incremental(
                repo_key=result.repo_name,
                current_head=result.new_head,
            ),
        )
    )
    logger.info("代码同步任务已注册，root=%s", settings.knowledge_code_root)
    business_doc_update_task = asyncio.create_task(
        business_doc_update_service.job_loop(interval_seconds=settings.business_doc_update_interval_seconds)
    )
    logger.info("业务文档更新任务已注册，root=%s", settings.knowledge_business_root)

    code_review_tasks: list[asyncio.Task[None]] = []
    if settings.code_review_project_config_path:
        assert code_review_service is not None
        code_review_tasks = [
            asyncio.create_task(code_review_service.poll_loop()),
            asyncio.create_task(code_review_service.job_loop(interval_seconds=settings.code_review_job_interval_seconds)),
            asyncio.create_task(code_review_service.email_job_loop(interval_seconds=settings.code_review_email_interval_seconds)),
        ]
        logger.info(
            "PR 代码自动 Review 任务已注册，config=%s github_cli_account=%s",
            settings.code_review_project_config_path,
            settings.github_cli_account or "<未配置>",
        )
    else:
        logger.info("CODE_REVIEW_PROJECT_CONFIG_PATH 未配置，跳过 PR 代码自动 Review 任务")

    security_scan_task = asyncio.create_task(
        get_security_scan_service().job_loop(interval_seconds=settings.security_scan_job_interval_seconds)
    )
    logger.info("安全扫描任务已注册，root=%s", settings.security_scan_root)

    review_service = None
    clickup_publish_task = None
    if (
        settings.clickup_review_publish_enabled
        and settings.clickup_api_token
        and settings.clickup_workspace_id
        and settings.clickup_review_doc_folder_id
    ):
        review_service = get_requirement_review_service()
        clickup_publish_task = asyncio.create_task(review_service.clickup_publish_job_loop())
        logger.info("ClickUp 评审发布任务已注册，folder=%s", settings.clickup_review_doc_folder_id)
    elif not settings.clickup_review_publish_enabled:
        logger.info("ClickUp 评审发布开关未开启（CLICKUP_REVIEW_PUBLISH_ENABLED=false），跳过发布任务")

    sync_task = None
    if settings.clickup_api_token and settings.clickup_folder_id and settings.clickup_workspace_id:
        sync_task = asyncio.create_task(
            clickup_sync_loop(
                folder_id=settings.clickup_folder_id,
                workspace_id=settings.clickup_workspace_id,
                docs_dir=Path(settings.knowledge_requirements_root) / "docs",
                runtime_base_dir=Path(settings.ai_working_directory),
                interval_seconds=settings.clickup_sync_interval_seconds,
                task_list_specs=settings.clickup_task_lists,
                review_service=review_service or get_requirement_review_service(),
            )
        )
        logger.info("ClickUp 同步任务已注册，folder=%s", settings.clickup_folder_id)
    else:
        logger.info("ClickUp 同步条件未满足（需配置 CLICKUP_API_TOKEN、CLICKUP_FOLDER_ID、CLICKUP_WORKSPACE_ID），跳过同步任务")
    yield
    code_sync_task.cancel()
    try:
        await code_sync_task
    except asyncio.CancelledError:
        pass
    if sync_task:
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass
    business_doc_update_task.cancel()
    try:
        await business_doc_update_task
    except asyncio.CancelledError:
        pass
    for task in code_review_tasks:
        task.cancel()
    for task in code_review_tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass
    security_scan_task.cancel()
    try:
        await security_scan_task
    except asyncio.CancelledError:
        pass
    if clickup_publish_task:
        clickup_publish_task.cancel()
        try:
            await clickup_publish_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(api_router, prefix=settings.api_prefix)


@app.middleware("http")
async def log_slow_requests(request: Request, call_next):
    started_at = perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    except Exception:
        elapsed_ms = (perf_counter() - started_at) * 1000
        logger.exception(
            "请求异常: method=%s path=%s status=%d elapsed_ms=%.1f",
            request.method,
            request.url.path,
            status_code,
            elapsed_ms,
        )
        raise
    finally:
        elapsed_ms = (perf_counter() - started_at) * 1000
        if elapsed_ms >= settings.slow_request_threshold_ms:
            logger.warning(
                "慢请求: method=%s path=%s status=%d elapsed_ms=%.1f threshold_ms=%d",
                request.method,
                request.url.path,
                status_code,
                elapsed_ms,
                settings.slow_request_threshold_ms,
            )


_FRONTEND_DIST = BASE_DIR / "source/frontend/dist"

if _FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=str(_FRONTEND_DIST / "assets")), name="assets")

    @app.get("/admin/", include_in_schema=False)
    @app.get("/admin", include_in_schema=False)
    def serve_admin() -> FileResponse:
        return FileResponse(str(_FRONTEND_DIST / "admin.html"))

    @app.get("/{full_path:path}", include_in_schema=False)
    def serve_spa(full_path: str) -> FileResponse:
        spa_file = _FRONTEND_DIST / full_path
        if spa_file.is_file():
            return FileResponse(str(spa_file))
        return FileResponse(str(_FRONTEND_DIST / "index.html"))


@app.get("/")
def root() -> dict[str, str]:
    return {"service": settings.app_name}
