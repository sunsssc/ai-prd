from functools import lru_cache
from pathlib import Path

from fastapi import Cookie, Depends, Header, HTTPException

from app.business.assistant.service import AssistantService
from app.business.assistant.git_context import AssistantGitContextResolver
from app.business.assistant.test_data_tools import AssistantTestDataTools
from app.business.assistant.doco_tools import AssistantDocoTools
from app.business.business_doc_updates import BusinessDocUpdateService, BusinessDocUpdateStore, GitHubPullRequestClient
from app.business.code_reviews import CodeReviewService, CodeReviewStore
from app.business.git_context import GitRepositoryCatalog, GitWorktreeManager
from app.business.my_tasks import MyTaskService, MyTaskStore
from app.business.requirement_pr_links import RequirementPrLinkService, RequirementPrLinkStore
from app.business.requirement_reviews import RequirementReviewService, RequirementReviewStore
from app.business.security_scan import SecurityScanService, SecurityScanStore
from app.business.workspace import (
    SQLiteSkillOwnershipStore,
    SQLiteWorkspaceAccessStore,
    WorkspaceAccessService,
    WorkspaceBrowserService,
)
from app.business.assistant.store import SQLiteAssistantStore
from app.core.config import settings
from app.integrations.agent_runtime import ClaudeCodeAgentRuntimeClient, CodexAgentRuntimeClient, MockAgentRuntimeClient
from app.integrations.llm.client import OpenAICompatibleAssistantLLMClient
from app.integrations.test_data_platform import TestDataPlatformClient
from app.integrations.doco import DocoClient
from app.integrations.vinotech_oa import OaEmployeeDirectory, VinotechOaClient
from app.services.email_service import DebugEmailSender, SMTPEmailSender
from app.services.auth_models import UserRecord
from app.services.auth_service import (
    AccessPolicyService,
    AuthService,
    InMemoryOAuthStateStore,
)
from app.services.auth_store import SQLiteAuthStore
from app.services.google_oauth import GoogleOAuthService
from app.services.notification_service import AuthNotificationService, SlackWebApiMessenger
from app.services.permission_service import BusinessPermissionService
from app.services.permission_store import SQLitePermissionStore
from app.utils.clickup.comments import ClickUpCommentClient


@lru_cache
def get_google_oauth_service() -> GoogleOAuthService:
    return GoogleOAuthService(settings)


@lru_cache
def get_access_policy_service() -> AccessPolicyService:
    return AccessPolicyService(settings)


@lru_cache
def get_auth_store() -> SQLiteAuthStore:
    return SQLiteAuthStore(
        db_path=settings.auth_db_path,
        session_ttl_seconds=settings.session_ttl_seconds,
    )


@lru_cache
def get_permission_store() -> SQLitePermissionStore:
    return SQLitePermissionStore(db_path=settings.auth_db_path)


@lru_cache
def get_permission_service() -> BusinessPermissionService:
    return BusinessPermissionService(
        store=get_permission_store(),
        oa_directory=get_oa_employee_directory(),
        workspace_access=get_workspace_access_service(),
        allowed_organizations=settings.test_data_allowed_organizations,
        allowed_tasks=settings.test_data_allowed_tasks,
        blocked_tasks=settings.test_data_blocked_tasks,
        name_llm_client=get_lightweight_llm_client(),
    )


@lru_cache
def get_email_sender() -> DebugEmailSender | SMTPEmailSender:
    if settings.email_delivery_mode.lower() == "smtp":
        return SMTPEmailSender(
            host=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_username,
            password=settings.smtp_password,
            use_ssl=settings.smtp_use_ssl,
            use_tls=settings.smtp_use_tls,
            sender_name=settings.email_sender_name,
            sender_address=settings.email_sender_address,
        )
    return DebugEmailSender()


@lru_cache
def get_oauth_state_store() -> InMemoryOAuthStateStore:
    return InMemoryOAuthStateStore(settings.oauth_state_ttl_seconds)


@lru_cache
def get_auth_service() -> AuthService:
    return AuthService(
        access_policy=get_access_policy_service(),
        auth_store=get_auth_store(),
        email_allowed_domain=settings.email_login_allowed_domain,
        email_sender=get_email_sender(),
        verification_code_length=settings.email_verification_code_length,
        verification_ttl_seconds=settings.email_verification_ttl_seconds,
        verification_max_attempts=settings.email_verification_max_attempts,
        verification_cooldown_seconds=settings.email_verification_cooldown_seconds,
        verification_debug_response=settings.email_verification_debug_response,
        verification_dev_shortcut_enabled=settings.email_verification_dev_shortcut_enabled,
        app_env=settings.app_env,
        session_jwt_secret=settings.session_jwt_secret,
        session_jwt_issuer=settings.session_jwt_issuer,
        admin_session_jwt_issuer=settings.admin_session_jwt_issuer,
        admin_accounts=settings.auth_admin_accounts,
        auto_approved_accounts=settings.auth_auto_approved_accounts,
    )


@lru_cache
def get_notification_service() -> AuthNotificationService:
    messenger = SlackWebApiMessenger(bot_token=settings.slack_bot_token) if settings.slack_bot_token else None
    return AuthNotificationService(
        messenger=messenger,
        admin_accounts=settings.notification_admin_accounts,
        app_name=settings.app_name,
        admin_frontend_url=settings.admin_frontend_url,
        frontend_app_url=settings.frontend_app_url,
    )


@lru_cache
def get_assistant_store() -> SQLiteAssistantStore:
    return SQLiteAssistantStore(db_path=settings.assistant_db_path)


@lru_cache
def get_requirement_review_store() -> RequirementReviewStore:
    return RequirementReviewStore(db_path=settings.assistant_db_path)


@lru_cache
def get_business_doc_update_store() -> BusinessDocUpdateStore:
    return BusinessDocUpdateStore(db_path=settings.assistant_db_path)


@lru_cache
def get_code_review_store() -> CodeReviewStore:
    return CodeReviewStore(db_path=settings.assistant_db_path)


@lru_cache
def get_requirement_pr_link_store() -> RequirementPrLinkStore:
    return RequirementPrLinkStore(db_path=settings.assistant_db_path)


@lru_cache
def get_security_scan_store() -> SecurityScanStore:
    return SecurityScanStore(db_path=settings.assistant_db_path)


@lru_cache
def get_my_task_store() -> MyTaskStore:
    return MyTaskStore(db_path=settings.assistant_db_path)


@lru_cache
def get_my_task_service() -> MyTaskService:
    return MyTaskService(
        store=get_my_task_store(),
        pr_link_store=get_requirement_pr_link_store(),
        code_review_store=get_code_review_store(),
    )


@lru_cache
def get_requirement_pr_link_service() -> RequirementPrLinkService:
    return RequirementPrLinkService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        store=get_requirement_pr_link_store(),
    )


@lru_cache
def get_workspace_access_store() -> SQLiteWorkspaceAccessStore:
    return SQLiteWorkspaceAccessStore(
        db_path=settings.auth_db_path,
        base_dir=Path(settings.ai_working_directory),
        default_organization_key=settings.default_organization_key,
    )


@lru_cache
def get_skill_ownership_store() -> SQLiteSkillOwnershipStore:
    return SQLiteSkillOwnershipStore(db_path=settings.auth_db_path)


@lru_cache
def get_workspace_access_service() -> WorkspaceAccessService:
    return WorkspaceAccessService(
        store=get_workspace_access_store(),
        base_dir=Path(settings.ai_working_directory),
    )


@lru_cache
def get_github_pull_request_client() -> GitHubPullRequestClient:
    return GitHubPullRequestClient(
        token=settings.github_api_token,
        api_base_url=settings.github_api_base_url,
    )


@lru_cache
def get_git_worktree_manager() -> GitWorktreeManager:
    return GitWorktreeManager(
        workspace_root=Path(settings.ai_working_directory) / "workspace",
    )


@lru_cache
def get_git_repository_catalog() -> GitRepositoryCatalog:
    return GitRepositoryCatalog(
        base_dir=Path(settings.ai_working_directory),
        config_path=settings.code_review_project_config_path,
        github_client=get_github_pull_request_client(),
        github_cli_account=settings.github_cli_account,
    )


@lru_cache
def get_assistant_runtime_client() -> MockAgentRuntimeClient | ClaudeCodeAgentRuntimeClient | CodexAgentRuntimeClient:
    return _build_runtime_client(max_turns=settings.ai_max_turns, effort=settings.ai_effort)


@lru_cache
def get_code_review_runtime_client() -> MockAgentRuntimeClient | ClaudeCodeAgentRuntimeClient | CodexAgentRuntimeClient:
    return _build_runtime_client(max_turns=settings.code_review_ai_max_turns, effort=settings.code_review_ai_effort)


@lru_cache
def get_security_scan_runtime_client() -> MockAgentRuntimeClient | ClaudeCodeAgentRuntimeClient | CodexAgentRuntimeClient:
    return _build_runtime_client(max_turns=settings.security_scan_ai_max_turns, effort=settings.security_scan_ai_effort)


@lru_cache
def get_lightweight_llm_client() -> OpenAICompatibleAssistantLLMClient | None:
    if not settings.ai_light_api_key:
        return None
    return OpenAICompatibleAssistantLLMClient(
        api_key=settings.ai_light_api_key,
        model=settings.ai_light_model,
        base_url=settings.ai_light_base_url,
        max_tokens=settings.ai_light_max_tokens,
        timeout_seconds=settings.ai_light_timeout_seconds,
    )


def _build_runtime_client(*, max_turns: int, effort: str) -> MockAgentRuntimeClient | ClaudeCodeAgentRuntimeClient | CodexAgentRuntimeClient:
    provider = settings.ai_provider.strip().lower()
    if provider in {"claude_code", "claude", "anthropic"}:
        return ClaudeCodeAgentRuntimeClient(
            model=settings.ai_model,
            max_turns=max_turns,
            effort=effort,
            permission_mode=settings.ai_permission_mode,
            allowed_tools=settings.ai_allowed_tools,
            skills_root=settings.skills_root,
            cli_path=settings.ai_cli_path,
            api_key=settings.ai_api_key,
            base_url=settings.ai_base_url,
            max_output_tokens=settings.claude_code_max_output_tokens,
            runtime_python_venv_path=settings.ai_runtime_python_venv_path,
        )
    if provider == "codex":
        return CodexAgentRuntimeClient(
            model=settings.ai_model,
            effort=effort,
            sandbox=settings.ai_codex_sandbox,
            writable_roots=settings.ai_codex_writable_roots,
            skills_root=settings.skills_root,
            cli_path=settings.ai_cli_path,
            api_key=settings.ai_api_key,
        )
    return MockAgentRuntimeClient(skills_root=settings.skills_root)


@lru_cache
def get_assistant_service() -> AssistantService:
    return AssistantService(
        store=get_assistant_store(),
        runtime_client=get_assistant_runtime_client(),
        system_prompt=settings.ai_system_prompt,
        runtime_working_directory=settings.ai_working_directory,
        uploaded_files_root=settings.assistant_image_upload_root,
        workspace_access=get_workspace_access_service(),
        protected_code_root=settings.knowledge_code_root,
        image_generation_daily_limit=settings.image_generation_daily_limit,
        image_generation_weekly_limit=settings.image_generation_weekly_limit,
        git_context_resolver=get_assistant_git_context_resolver(),
        lightweight_llm_client=get_lightweight_llm_client(),
        test_data_tools=get_assistant_test_data_tools(),
        doco_tools=get_assistant_doco_tools(),
    )


def get_assistant_doco_tools() -> AssistantDocoTools:
    return AssistantDocoTools(
        store=get_assistant_store(),
        client_factory=lambda api_token: DocoClient(
            base_url=settings.doco_base_url,
            api_token=api_token,
            timeout_seconds=settings.doco_timeout_seconds,
        ),
        allowed_organizations=settings.doco_allowed_organizations,
    )


@lru_cache
def get_assistant_test_data_tools() -> AssistantTestDataTools | None:
    if not settings.test_help_token:
        return None
    return AssistantTestDataTools(
        store=get_assistant_store(),
        client=TestDataPlatformClient(
            base_url=settings.test_help_base_url,
            api_token=settings.test_help_token,
            timeout_seconds=settings.test_data_timeout_seconds,
        ),
        workspace_access=get_workspace_access_service(),
        allowed_organizations=settings.test_data_allowed_organizations,
        allowed_environments=settings.test_data_allowed_environments,
        allowed_tasks=settings.test_data_allowed_tasks,
        blocked_tasks=settings.test_data_blocked_tasks,
        plan_ttl_seconds=settings.test_data_plan_ttl_seconds,
        admin_only=settings.test_data_admin_only,
        permission_service=get_permission_service(),
    )


@lru_cache
def get_assistant_git_context_resolver() -> AssistantGitContextResolver:
    code_review_service = get_code_review_service()
    return AssistantGitContextResolver(
        store=get_assistant_store(),
        catalog=get_git_repository_catalog(),
        worktree_manager=get_git_worktree_manager(),
        pr_context_preparer=code_review_service.context_preparer,
        workspace_root=Path(settings.ai_working_directory) / "workspace",
    )


@lru_cache
def get_requirement_review_service() -> RequirementReviewService:
    return RequirementReviewService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        skills_root=Path(settings.skills_root),
        store=get_requirement_review_store(),
        runtime_client=get_assistant_runtime_client(),
        clickup_comment_client=get_clickup_comment_client(),
        requirement_review_auto_enabled=settings.requirement_review_auto_enabled,
        clickup_review_publish_enabled=settings.clickup_review_publish_enabled,
        clickup_workspace_id=settings.clickup_workspace_id,
        clickup_review_doc_folder_id=settings.clickup_review_doc_folder_id,
        frontend_app_url=settings.frontend_app_url,
    )


@lru_cache
def get_clickup_comment_client() -> ClickUpCommentClient | None:
    if not settings.clickup_api_token:
        return None
    return ClickUpCommentClient(
        api_token=settings.clickup_api_token,
        base_url=settings.clickup_api_base_url,
        v3_base_url=settings.clickup_api_v3_base_url,
    )


@lru_cache
def get_business_doc_update_service() -> BusinessDocUpdateService:
    return BusinessDocUpdateService(
        base_dir=Path(settings.ai_working_directory),
        business_docs_root=Path(settings.knowledge_business_root),
        skills_root=Path(settings.business_doc_update_skills_root),
        store=get_business_doc_update_store(),
        runtime_client=get_assistant_runtime_client(),
        project_config_path=settings.code_review_project_config_path,
        auto_apply_confidence=settings.business_doc_auto_apply_confidence,
    )


@lru_cache
def get_code_review_service() -> CodeReviewService:
    return CodeReviewService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        skills_root=Path(settings.skills_root),
        store=get_code_review_store(),
        runtime_client=get_code_review_runtime_client(),
        github_client=get_github_pull_request_client(),
        email_sender=get_email_sender(),
        pr_link_service=get_requirement_pr_link_service(),
        config_path=settings.code_review_project_config_path,
        github_cli_account=settings.github_cli_account,
        default_poll_interval_seconds=settings.code_review_poll_interval_seconds,
        email_max_attempts=settings.code_review_email_max_attempts,
        running_timeout_seconds=settings.code_review_running_timeout_seconds,
        worktree_manager=get_git_worktree_manager(),
    )


@lru_cache
def get_security_scan_service() -> SecurityScanService:
    return SecurityScanService(
        store=get_security_scan_store(),
        runtime_client=get_security_scan_runtime_client(),
        scan_root=Path(settings.security_scan_root),
        allowed_roots=list(settings.security_scan_allowed_roots),
        running_timeout_seconds=settings.security_scan_running_timeout_seconds,
        retention_days=settings.security_scan_job_retention_days,
        max_concurrent=settings.security_scan_max_concurrent,
        max_archive_bytes=settings.security_scan_max_archive_bytes,
        max_extracted_bytes=settings.security_scan_max_extracted_bytes,
        max_files=settings.security_scan_max_files,
        url_max_fetches=settings.security_scan_url_max_fetches,
        url_max_response_bytes=settings.security_scan_url_max_response_bytes,
        url_fetch_timeout_seconds=settings.security_scan_url_fetch_timeout_seconds,
        url_max_redirects=settings.security_scan_url_max_redirects,
        url_max_text_chars=settings.security_scan_url_max_text_chars,
    )


@lru_cache
def get_oa_employee_directory() -> OaEmployeeDirectory:
    client = None
    if settings.oa_access_id and settings.oa_secret_key:
        client = VinotechOaClient(
            base_url=settings.oa_base_url,
            access_id=settings.oa_access_id,
            secret_key=settings.oa_secret_key,
            timeout_seconds=settings.oa_timeout_seconds,
        )
    return OaEmployeeDirectory(
        client=client,
        cache_ttl_seconds=settings.oa_directory_cache_ttl_seconds,
        cache_file=Path(settings.oa_directory_cache_file) if settings.oa_directory_cache_file else None,
    )


def get_oa_company_domains() -> set[str]:
    return {domain.strip().lower() for domain in settings.oa_company_domains.split(",") if domain.strip()}


@lru_cache
def get_workspace_browser_service() -> WorkspaceBrowserService:
    return WorkspaceBrowserService(
        base_dir=Path(settings.ai_working_directory),
        knowledge_roots={
            "requirements": Path(settings.knowledge_requirements_root),
            "business": Path(settings.knowledge_business_root),
            "code": Path(settings.knowledge_code_root),
        },
        skills_root=Path(settings.skills_root),
        preview_limit=settings.workspace_file_preview_limit,
    )


async def _resolve_active_account_user(
    *,
    auth_service: AuthService,
    auth_store: SQLiteAuthStore,
    oa_directory: OaEmployeeDirectory,
    authorization: str | None,
    session_cookie: str | None,
) -> UserRecord:
    """账号级校验：会话有效且账号处于激活状态（可访问通用功能）。"""
    token = _extract_session_token(authorization, session_cookie)
    if token is None:
        raise HTTPException(status_code=401, detail="缺少应用会话。")

    user = auth_service.get_current_user(token)
    if user is None:
        raise HTTPException(status_code=401, detail="应用会话无效或已过期。")
    if user.status == "active":
        snapshot = await oa_directory.snapshot()
        if snapshot.is_left_employee(user.email, get_oa_company_domains()):
            auth_store.update_user_status(user.user_id, "blocked")
            raise HTTPException(status_code=403, detail="账号已被禁用（已离开 OA 组织）。")
    if user.status == "blocked":
        raise HTTPException(status_code=403, detail="账号已被封禁。")
    if user.status != "active":
        raise HTTPException(status_code=403, detail="账号当前不可用。")
    return user


async def get_active_account_user(
    auth_service: AuthService = Depends(get_auth_service),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    oa_directory: OaEmployeeDirectory = Depends(get_oa_employee_directory),
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> UserRecord:
    return await _resolve_active_account_user(
        auth_service=auth_service,
        auth_store=auth_store,
        oa_directory=oa_directory,
        authorization=authorization,
        session_cookie=session_cookie,
    )


async def get_current_app_user(
    auth_service: AuthService = Depends(get_auth_service),
    auth_store: SQLiteAuthStore = Depends(get_auth_store),
    oa_directory: OaEmployeeDirectory = Depends(get_oa_employee_directory),
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> UserRecord:
    """Agent 级校验：账号激活且已获得 Agent 使用权限。"""
    user = await _resolve_active_account_user(
        auth_service=auth_service,
        auth_store=auth_store,
        oa_directory=oa_directory,
        authorization=authorization,
        session_cookie=session_cookie,
    )
    if user.agent_access == "pending":
        raise HTTPException(status_code=403, detail="Agent 使用权限申请正在审核中。")
    if user.agent_access == "rejected":
        raise HTTPException(status_code=403, detail="Agent 使用权限申请未通过。")
    if user.agent_access != "active":
        raise HTTPException(status_code=403, detail="尚未获得 Agent 使用权限，请先提交申请。")
    return user


def get_optional_app_user(
    auth_service: AuthService = Depends(get_auth_service),
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> UserRecord | None:
    token = _extract_session_token(authorization, session_cookie)
    if token is None:
        return None
    user = auth_service.get_current_user(token)
    if user is None or user.status != "active":
        return None
    return user


def _extract_session_token(authorization: str | None, session_cookie: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return session_cookie
