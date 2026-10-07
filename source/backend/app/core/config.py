import os
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parents[4]


def _load_env_file_values(paths: list[Path]) -> dict[str, str]:
    values: dict[str, str] = {}
    for path in paths:
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            key = key.strip()
            value = raw_value.strip().strip('"').strip("'")
            values[key] = value
    return values


_ENV_FILE_VALUES = _load_env_file_values(
    [
        BASE_DIR / ".env",
        BASE_DIR / "source/backend/.env",
    ]
)


def _env_value(key: str, default: str = "") -> str:
    value = os.getenv(key)
    if value not in (None, ""):
        return value
    return _ENV_FILE_VALUES.get(key, default)


def _env_csv_list(key: str, default: list[str] | None = None) -> list[str]:
    raw_value = _env_value(key)
    if not raw_value:
        return list(default or [])
    return [item.strip() for item in raw_value.split(",") if item.strip()]


DEFAULT_ORGANIZATION_KEY = _env_value("DEFAULT_ORGANIZATION_KEY", default="coinex")
DEFAULT_ORGANIZATION_KNOWLEDGE_ROOT = BASE_DIR / "workspace" / DEFAULT_ORGANIZATION_KEY / "knowledge"


class Settings(BaseSettings):
    app_env: str = _env_value("APP_ENV", default="development")
    app_name: str = "ai-prd"
    log_level: str = _env_value("LOG_LEVEL", default="INFO")
    api_prefix: str = "/api"
    auth_db_path: str = str(BASE_DIR / "workspace/runtime/db/auth.sqlite3")
    assistant_db_path: str = str(BASE_DIR / "workspace/runtime/db/assistant.sqlite3")
    assistant_image_upload_root: str = str(BASE_DIR / "workspace/runtime/uploads/assistant-images")
    assistant_image_upload_max_bytes: int = int(_env_value("ASSISTANT_IMAGE_UPLOAD_MAX_BYTES", default="10485760"))
    # 单用户 Codex 生图额度上限（按"成功落盘的图"计数）；<= 0 表示不限制该项。
    # 每日按 UTC 自然日、每周按 UTC 自然周（周一为起点）统计。
    image_generation_daily_limit: int = int(_env_value("IMAGE_GENERATION_DAILY_LIMIT", default="5"))
    image_generation_weekly_limit: int = int(_env_value("IMAGE_GENERATION_WEEKLY_LIMIT", default="10"))
    knowledge_requirements_root: str = str(DEFAULT_ORGANIZATION_KNOWLEDGE_ROOT / "requirements")
    knowledge_business_root: str = str(DEFAULT_ORGANIZATION_KNOWLEDGE_ROOT / "business-docs")
    knowledge_code_root: str = str(DEFAULT_ORGANIZATION_KNOWLEDGE_ROOT / "code")
    default_organization_key: str = DEFAULT_ORGANIZATION_KEY
    skills_root: str = str(BASE_DIR / "workspace/.claude/skills")
    business_doc_update_skills_root: str = str(BASE_DIR / ".claude/skills")
    workspace_file_preview_limit: int = int(_env_value("WORKSPACE_FILE_PREVIEW_LIMIT", default="100000"))
    slow_request_threshold_ms: int = int(_env_value("SLOW_REQUEST_THRESHOLD_MS", default="500"))
    ai_provider: str = _env_value("AI_PROVIDER", default="mock")
    ai_model: str = _env_value("AI_MODEL", default="claude-sonnet-4-20250514")
    ai_api_key: str = _env_value("AI_API_KEY")
    ai_base_url: str = _env_value("AI_BASE_URL", default="https://api.anthropic.com")
    ai_light_base_url: str = _env_value("AI_LIGHT_BASE_URL", default="http://127.0.0.1:8003")
    ai_light_api_key: str = _env_value("AI_LIGHT_API_KEY")
    ai_light_model: str = _env_value("AI_LIGHT_MODEL", default="qwen3.6-27b-int4")
    ai_light_max_tokens: int = int(_env_value("AI_LIGHT_MAX_TOKENS", default="2048"))
    ai_light_timeout_seconds: float = float(_env_value("AI_LIGHT_TIMEOUT_SECONDS", default="300"))
    test_help_base_url: str = _env_value(
        "TEST_HELP_BASE_URL",
        default="https://testhelp.internal.test",
    )
    test_help_token: str = _env_value("TEST_HELP_TOKEN")
    test_data_allowed_organizations: list[str] = _env_csv_list(
        "TEST_DATA_ALLOWED_ORGANIZATIONS",
        default=["coinex"],
    )
    test_data_allowed_environments: list[str] = _env_csv_list(
        "TEST_DATA_ALLOWED_ENVIRONMENTS",
        default=[str(index) for index in range(1, 13)],
    )
    test_data_allowed_tasks: list[str] = _env_csv_list(
        "TEST_DATA_ALLOWED_TASKS",
        default=[],
    )
    test_data_blocked_tasks: list[str] = _env_csv_list(
        "TEST_DATA_BLOCKED_TASKS",
        default=["track_market_price"],
    )
    test_data_timeout_seconds: float = float(_env_value("TEST_DATA_TIMEOUT_SECONDS", default="60"))
    oa_base_url: str = _env_value("OA_BASE_URL", default="https://oa.internal.test")
    oa_access_id: str = _env_value("OA_ACCESS_ID")
    oa_secret_key: str = _env_value("OA_SECRET_KEY")
    oa_timeout_seconds: float = float(_env_value("OA_TIMEOUT_SECONDS", default="10"))
    oa_directory_cache_ttl_seconds: int = int(_env_value("OA_DIRECTORY_CACHE_TTL_SECONDS", default="300"))
    # OA 目录本地缓存文件路径；每次成功拉取后更新，OA 接口失败时作为兜底数据源（留空不启用）
    oa_directory_cache_file: str | None = _env_value("OA_DIRECTORY_CACHE_FILE") or None
    # 公司邮箱域名（逗号分隔）；仅这些域名的账号在不在 OA 在职目录时会被判定离职并自动封禁
    oa_company_domains: str = _env_value("OA_COMPANY_DOMAINS", default="corp.test")
    test_data_plan_ttl_seconds: int = int(_env_value("TEST_DATA_PLAN_TTL_SECONDS", default="600"))
    test_data_admin_only: bool = _env_value("TEST_DATA_ADMIN_ONLY", default="true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    doco_base_url: str = _env_value("DOCO_BASE_URL", default="https://api.doco.page/api/v1")
    doco_timeout_seconds: float = float(_env_value("DOCO_TIMEOUT_SECONDS", default="30"))
    doco_allowed_organizations: list[str] = _env_csv_list(
        "DOCO_ALLOWED_ORGANIZATIONS",
        default=["coinex"],
    )
    ai_cli_path: str | None = _env_value("AI_CLI_PATH") or None
    ai_cli_api_key: str = _env_value("AI_CLI_API_KEY")
    ai_cli_base_url: str = _env_value("AI_CLI_BASE_URL")
    claude_code_max_output_tokens: int = int(_env_value("CLAUDE_CODE_MAX_OUTPUT_TOKENS", default="64000"))
    ai_runtime_python_venv_path: str = _env_value(
        "AI_RUNTIME_PYTHON_VENV_PATH",
        default=str(BASE_DIR / "workspace/runtime/python/.venv"),
    )
    ai_max_tokens: int = int(_env_value("AI_MAX_TOKENS", default="2048"))
    ai_timeout_seconds: float = 60.0
    ai_working_directory: str = str(BASE_DIR)
    ai_max_turns: int = int(_env_value("AI_MAX_TURNS", default="12"))
    ai_effort: str = _env_value("AI_EFFORT", default="medium")
    ai_permission_mode: str = _env_value("AI_PERMISSION_MODE", default="default")
    # auto 按每个 RuntimeWorkspacePlan 的 write 挂载决定 Codex sandbox。
    ai_codex_sandbox: str = _env_value("AI_CODEX_SANDBOX", default="auto")
    ai_codex_writable_roots: list[str] = []
    ai_allowed_tools: list[str] = [
        "Glob",
        "Grep",
        "LS",
        "Read",
        "Task",
        "Bash(rg *)",
        "Bash(git status *)",
        "Bash(git diff *)",
        "Bash(git show *)",
        "Bash(git log *)",
        "Bash(git blame *)",
        "Bash(git grep *)",
        "Bash(git ls-files *)",
        "Bash(git rev-parse *)",
        "Bash(find *)",
        "Bash(sed -n *)",
        "Bash(head *)",
        "Bash(tail *)",
        "Bash(wc *)",
    ]
    ai_system_prompt: str = _env_value("AI_SYSTEM_PROMPT")
    google_client_id: str = _env_value("GOOGLE_CLIENT_ID")
    google_client_secret: str = _env_value("GOOGLE_CLIENT_SECRET")
    google_redirect_uri: str = _env_value("GOOGLE_REDIRECT_URI", default="http://localhost:8000/api/auth/google/callback")
    google_scopes: list[str] = _env_csv_list("GOOGLE_SCOPES", default=["openid", "email", "profile"])
    google_allowed_domains: list[str] = _env_csv_list("GOOGLE_ALLOWED_DOMAINS")
    google_allowed_departments: list[str] = _env_csv_list("GOOGLE_ALLOWED_DEPARTMENTS")
    frontend_app_url: str = _env_value("FRONTEND_APP_URL", default="/")
    google_fetch_people_profile: bool = _env_value("GOOGLE_FETCH_PEOPLE_PROFILE", default="false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    auth_admin_accounts: list[str] = _env_csv_list("AUTH_ADMIN_ACCOUNTS")
    auth_auto_approved_accounts: list[str] = _env_csv_list("AUTH_AUTO_APPROVED_ACCOUNTS")
    notification_admin_accounts: list[str] = _env_csv_list("NOTIFICATION_ADMIN_ACCOUNTS")
    slack_bot_token: str = _env_value("SLACK_BOT_TOKEN")
    oauth_state_cookie_name: str = "ai_prd_oauth_state"
    admin_oauth_state_cookie_name: str = "ai_prd_admin_oauth_state"
    session_cookie_name: str = "ai_prd_session"
    admin_session_cookie_name: str = "ai_prd_admin_session"
    session_jwt_secret: str = _env_value("SESSION_JWT_SECRET", default="development-session-secret")
    session_jwt_issuer: str = "ai-prd"
    admin_session_jwt_issuer: str = "ai-prd-admin"
    auth_cookie_secure: bool = False
    oauth_state_ttl_seconds: int = 600
    session_ttl_seconds: int = 259200  # 3 days
    admin_google_redirect_uri: str = _env_value("ADMIN_GOOGLE_REDIRECT_URI", default="http://localhost:8000/api/admin/auth/google/callback")
    admin_frontend_url: str = _env_value("ADMIN_FRONTEND_URL", default="/admin/")
    email_auth_enabled: bool = _env_value("EMAIL_AUTH_ENABLED", default="false").lower() in {"1", "true", "yes", "on"}
    email_login_allowed_domain: str = _env_value("EMAIL_LOGIN_ALLOWED_DOMAIN", default="corp.test")
    email_verification_code_length: int = int(_env_value("EMAIL_VERIFICATION_CODE_LENGTH", default="6"))
    email_verification_ttl_seconds: int = int(_env_value("EMAIL_VERIFICATION_TTL_SECONDS", default="300"))
    email_verification_max_attempts: int = int(_env_value("EMAIL_VERIFICATION_MAX_ATTEMPTS", default="5"))
    email_verification_cooldown_seconds: int = int(_env_value("EMAIL_VERIFICATION_COOLDOWN_SECONDS", default="60"))
    email_delivery_mode: str = _env_value("EMAIL_DELIVERY_MODE", default="smtp")
    email_sender_name: str = _env_value("EMAIL_SENDER_NAME", default="ai-prd")
    email_sender_address: str = _env_value("EMAIL_SENDER_ADDRESS", default="no-reply@corp.test")
    smtp_host: str = _env_value("SMTP_HOST")
    smtp_port: int = int(_env_value("SMTP_PORT", default="587"))
    smtp_username: str = _env_value("SMTP_USERNAME")
    smtp_password: str = _env_value("SMTP_PASSWORD")
    smtp_use_ssl: bool = _env_value("SMTP_USE_SSL", default="false").lower() in {"1", "true", "yes", "on"}
    smtp_use_tls: bool = _env_value("SMTP_USE_TLS", default="true").lower() in {"1", "true", "yes", "on"}
    email_verification_debug_response: bool = _env_value("EMAIL_VERIFICATION_DEBUG_RESPONSE", default="false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    email_verification_dev_shortcut_enabled: bool = _env_value(
        "EMAIL_VERIFICATION_DEV_SHORTCUT_ENABLED",
        default="true",
    ).lower() in {"1", "true", "yes", "on"}
    clickup_api_token: str = _env_value("CLICKUP_API_TOKEN")
    clickup_api_base_url: str = _env_value("CLICKUP_API_BASE_URL", default="https://api.clickup.com/api/v2")
    clickup_api_v3_base_url: str = _env_value("CLICKUP_API_V3_BASE_URL", default="https://api.clickup.com/api/v3")
    clickup_folder_id: str = _env_value("CLICKUP_FOLDER_ID")
    clickup_workspace_id: str = _env_value("CLICKUP_WORKSPACE_ID")
    requirement_review_auto_enabled: bool = _env_value("REQUIREMENT_REVIEW_AUTO_ENABLED", default="true").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    clickup_review_publish_enabled: bool = _env_value("CLICKUP_REVIEW_PUBLISH_ENABLED", default="false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    clickup_review_doc_folder_id: str = _env_value("CLICKUP_REVIEW_DOC_FOLDER_ID")
    clickup_task_lists: list[str] = _env_csv_list("CLICKUP_TASK_LISTS")
    clickup_sync_interval_seconds: int = int(_env_value("CLICKUP_SYNC_INTERVAL_SECONDS", default="3600"))
    figma_access_token: str = _env_value("FIGMA_ACCESS_TOKEN")
    figma_request_interval_seconds: float = float(_env_value("FIGMA_REQUEST_INTERVAL_SECONDS", default="1.0"))
    figma_max_retries: int = int(_env_value("FIGMA_MAX_RETRIES", default="4"))
    figma_retry_base_delay_seconds: float = float(_env_value("FIGMA_RETRY_BASE_DELAY_SECONDS", default="2.0"))
    code_sync_interval_seconds: int = int(_env_value("CODE_SYNC_INTERVAL_SECONDS", default="3600"))
    github_api_token: str = _env_value("GITHUB_API_TOKEN")
    github_api_base_url: str = _env_value("GITHUB_API_BASE_URL", default="https://api.github.com")
    github_cli_account: str = _env_value("GITHUB_CLI_ACCOUNT")
    deploy_webhook_secret: str = _env_value("DEPLOY_WEBHOOK_SECRET")
    deploy_webhook_repository: str = _env_value("DEPLOY_WEBHOOK_REPOSITORY")
    deploy_webhook_branch: str = _env_value("DEPLOY_WEBHOOK_BRANCH", default="main")
    deploy_script_path: str = _env_value("DEPLOY_SCRIPT_PATH", default=str(BASE_DIR / "deployment/deploy-from-github.sh"))
    deploy_log_path: str = _env_value("DEPLOY_LOG_PATH", default=str(BASE_DIR / "workspace/logs/deploy-webhook.log"))
    deploy_skip_frontend: bool = _env_value("DEPLOY_SKIP_FRONTEND", default="false").lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    business_doc_update_interval_seconds: int = int(_env_value("BUSINESS_DOC_UPDATE_INTERVAL_SECONDS", default="30"))
    business_doc_auto_apply_confidence: float = float(_env_value("BUSINESS_DOC_AUTO_APPLY_CONFIDENCE", default="0.85"))
    code_review_project_config_path: str = _env_value("CODE_REVIEW_PROJECT_CONFIG_PATH")
    code_review_poll_interval_seconds: int = int(_env_value("CODE_REVIEW_POLL_INTERVAL_SECONDS", default="300"))
    code_review_job_interval_seconds: int = int(_env_value("CODE_REVIEW_JOB_INTERVAL_SECONDS", default="30"))
    code_review_email_interval_seconds: int = int(_env_value("CODE_REVIEW_EMAIL_INTERVAL_SECONDS", default="30"))
    code_review_email_max_attempts: int = int(_env_value("CODE_REVIEW_EMAIL_MAX_ATTEMPTS", default="3"))
    code_review_running_timeout_seconds: int = int(_env_value("CODE_REVIEW_RUNNING_TIMEOUT_SECONDS", default="3600"))
    code_review_ai_max_turns: int = int(_env_value("CODE_REVIEW_AI_MAX_TURNS", default="100"))
    code_review_ai_effort: str = _env_value("CODE_REVIEW_AI_EFFORT", default=_env_value("AI_EFFORT", default="medium"))
    security_scan_shared_secret: str = _env_value("SECURITY_SCAN_SHARED_SECRET")
    security_scan_allowed_roots: list[str] = _env_csv_list("SECURITY_SCAN_ALLOWED_ROOTS")
    security_scan_root: str = str(BASE_DIR / "workspace/runtime/security-scan")
    security_scan_ai_max_turns: int = int(_env_value("SECURITY_SCAN_AI_MAX_TURNS", default="60"))
    security_scan_ai_effort: str = _env_value("SECURITY_SCAN_AI_EFFORT", default=_env_value("AI_EFFORT", default="medium"))
    security_scan_running_timeout_seconds: int = int(_env_value("SECURITY_SCAN_RUNNING_TIMEOUT_SECONDS", default="600"))
    security_scan_job_interval_seconds: int = int(_env_value("SECURITY_SCAN_JOB_INTERVAL_SECONDS", default="30"))
    security_scan_job_retention_days: int = int(_env_value("SECURITY_SCAN_JOB_RETENTION_DAYS", default="30"))
    security_scan_max_concurrent: int = int(_env_value("SECURITY_SCAN_MAX_CONCURRENT", default="2"))
    security_scan_max_archive_bytes: int = int(_env_value("SECURITY_SCAN_MAX_ARCHIVE_BYTES", default="524288000"))
    security_scan_max_extracted_bytes: int = int(_env_value("SECURITY_SCAN_MAX_EXTRACTED_BYTES", default="1610612736"))
    security_scan_max_files: int = int(_env_value("SECURITY_SCAN_MAX_FILES", default="10000"))
    security_scan_url_max_fetches: int = int(_env_value("SECURITY_SCAN_URL_MAX_FETCHES", default="12"))
    security_scan_url_max_response_bytes: int = int(_env_value("SECURITY_SCAN_URL_MAX_RESPONSE_BYTES", default="2097152"))
    security_scan_url_fetch_timeout_seconds: float = float(_env_value("SECURITY_SCAN_URL_FETCH_TIMEOUT_SECONDS", default="20"))
    security_scan_url_max_redirects: int = int(_env_value("SECURITY_SCAN_URL_MAX_REDIRECTS", default="5"))
    security_scan_url_max_text_chars: int = int(_env_value("SECURITY_SCAN_URL_MAX_TEXT_CHARS", default="120000"))

    model_config = SettingsConfigDict(
        env_prefix="",
        extra="ignore",
    )

    @field_validator(
        "google_scopes",
        "google_allowed_domains",
        "google_allowed_departments",
        "ai_allowed_tools",
        "ai_codex_writable_roots",
        "auth_admin_accounts",
        "auth_auto_approved_accounts",
        "notification_admin_accounts",
        "test_data_allowed_organizations",
        "test_data_allowed_environments",
        "test_data_allowed_tasks",
        "test_data_blocked_tasks",
        "doco_allowed_organizations",
        "security_scan_allowed_roots",
        mode="before",
    )
    @classmethod
    def parse_csv_list(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("business_doc_auto_apply_confidence")
    @classmethod
    def validate_business_doc_auto_apply_confidence(cls, value: float) -> float:
        if not 0 <= value <= 1:
            raise ValueError("BUSINESS_DOC_AUTO_APPLY_CONFIDENCE 必须在 0 到 1 之间。")
        return value


settings = Settings()
