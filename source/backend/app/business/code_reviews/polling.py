from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from app.business.business_doc_updates.service import (
    GitHubCliPullRequestClient,
    GitHubPullRequestClient,
    PullRequestCommit,
    PullRequestFile,
    PullRequestReviewRequestEvent,
    PullRequestSnapshot,
    _hash_text,
)
from app.business.code_reviews.reports import DEFAULT_REVIEW_POLICY_PATH
from app.business.code_reviews.store import CodeReviewEmailJob, CodeReviewJob, CodeReviewStore
from app.business.requirement_pr_links import RequirementPrLinkService


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CodeReviewRepositoryConfig:
    repo_full_name: str
    enabled: bool
    pr_link_enabled: bool
    github_access: str
    workspace_repo_path: str
    organization_key: str
    origin: str
    default_branch: str
    base_branches: tuple[str, ...]
    notification_recipient_mode: str
    notification_cc_emails: tuple[str, ...]
    review_policy_path: str
    include_path_patterns: tuple[str, ...]
    exclude_path_patterns: tuple[str, ...]
    code_extensions: tuple[str, ...]


@dataclass(frozen=True)
class CodeReviewProjectConfig:
    enabled: bool
    poll_interval_seconds: int
    repositories: tuple[CodeReviewRepositoryConfig, ...]


class CodeReviewPoller:
    def __init__(
        self,
        *,
        base_dir: Path,
        store: CodeReviewStore,
        github_client: GitHubPullRequestClient,
        pr_link_service: RequirementPrLinkService,
        config_path: str,
        github_cli_account: str,
        default_poll_interval_seconds: int,
        default_code_extensions: tuple[str, ...],
    ) -> None:
        self.base_dir = base_dir.resolve()
        self.store = store
        self.github_client = github_client
        self.pr_link_service = pr_link_service
        self.config_path = config_path.strip()
        self.github_cli_account = github_cli_account.strip()
        self.default_poll_interval_seconds = default_poll_interval_seconds
        self.default_code_extensions = default_code_extensions

    def load_project_config(self) -> CodeReviewProjectConfig | None:
        if not self.config_path:
            return None
        config_file = (self.base_dir / self.config_path).resolve() if not Path(self.config_path).is_absolute() else Path(self.config_path).resolve()
        if not config_file.is_file():
            raise FileNotFoundError(f"代码 Review 项目配置不存在：{config_file}")
        payload = json.loads(config_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("代码 Review 项目配置必须是 JSON object。")
        repositories: list[CodeReviewRepositoryConfig] = []
        for item in payload.get("repositories", []):
            if not isinstance(item, dict):
                continue
            repo_full_name = str(item.get("repo_full_name") or "").strip()
            if not repo_full_name:
                continue
            base_branches = _string_tuple(item.get("base_branches") or ["main", "master"])
            repositories.append(
                CodeReviewRepositoryConfig(
                    repo_full_name=repo_full_name,
                    enabled=bool(item.get("enabled", True)),
                    pr_link_enabled=(
                        bool(item["pr_link_enabled"])
                        if "pr_link_enabled" in item
                        else bool(item.get("enabled", True))
                    ),
                    github_access=str(item.get("github_access") or "api").strip().lower(),
                    workspace_repo_path=str(item.get("workspace_repo_path") or "").strip(),
                    organization_key=str(item.get("organization_key") or "").strip().lower(),
                    origin=str(item.get("origin") or "origin").strip(),
                    default_branch=str(item.get("default_branch") or "").strip() or base_branches[0],
                    base_branches=base_branches,
                    notification_recipient_mode=_notification_recipient_mode(item.get("notification_recipient_mode")),
                    notification_cc_emails=_string_tuple(item.get("notification_cc_emails") or []),
                    review_policy_path=str(item.get("review_policy_path") or DEFAULT_REVIEW_POLICY_PATH).strip()
                    or DEFAULT_REVIEW_POLICY_PATH,
                    include_path_patterns=_string_tuple(item.get("include_paths") or item.get("include_path_patterns") or []),
                    exclude_path_patterns=_string_tuple(item.get("exclude_paths") or item.get("exclude_path_patterns") or []),
                    code_extensions=tuple(
                        extension if extension.startswith(".") else f".{extension}"
                        for extension in _string_tuple(item.get("code_extensions") or self.default_code_extensions)
                    ),
                )
            )
        return CodeReviewProjectConfig(
            enabled=bool(payload.get("enabled", True)),
            poll_interval_seconds=max(int(payload.get("poll_interval_seconds") or self.default_poll_interval_seconds), 10),
            repositories=tuple(repositories),
        )

    async def poll_once(self, *, config: CodeReviewProjectConfig | None = None) -> list[CodeReviewJob]:
        project_config = config or self.load_project_config()
        if project_config is None:
            return []
        created: list[CodeReviewJob] = []
        for repo_config in project_config.repositories:
            if not repo_config.enabled and not repo_config.pr_link_enabled:
                continue
            try:
                created.extend(
                    await self._poll_repository(
                        repo_config,
                        sync_pr_links=repo_config.pr_link_enabled,
                        create_review_jobs=project_config.enabled and repo_config.enabled,
                    )
                )
                self.store.record_poll_success(repo_full_name=repo_config.repo_full_name)
            except Exception as exc:
                self.store.record_poll_error(repo_full_name=repo_config.repo_full_name, error_message=str(exc))
                logger.exception("PR 代码自动 Review 仓库轮询失败: repo=%s", repo_config.repo_full_name)
        return created

    def repo_config_for_job(self, job: CodeReviewJob) -> CodeReviewRepositoryConfig:
        config = self.load_project_config()
        if config is None:
            raise ValueError("代码 Review 项目配置不存在。")
        repo_config = next((item for item in config.repositories if item.repo_full_name == job.repo_full_name and item.enabled), None)
        if repo_config is None:
            raise ValueError("该 GitHub 仓库未配置为代码 Review 来源。")
        if repo_config.base_branches and job.base_ref not in repo_config.base_branches:
            raise ValueError("PR 目标分支不在代码 Review 范围内。")
        return repo_config

    def github_client_for(self, repo_config: CodeReviewRepositoryConfig) -> GitHubPullRequestClient | GitHubCliPullRequestClient:
        if repo_config.github_access == "api":
            return self.github_client
        if repo_config.github_access != "gh_cli":
            raise ValueError(f"不支持的代码 Review GitHub 访问方式：{repo_config.github_access}")
        if not self.github_cli_account:
            raise ValueError("gh_cli 模式必须配置 GITHUB_CLI_ACCOUNT。")
        if not repo_config.workspace_repo_path:
            raise ValueError("gh_cli 模式必须配置 workspace_repo_path。")
        workspace_repo_path = Path(repo_config.workspace_repo_path)
        if not workspace_repo_path.is_absolute():
            workspace_repo_path = self.base_dir / workspace_repo_path
        return GitHubCliPullRequestClient(
            repo_full_name=repo_config.repo_full_name,
            working_directory=workspace_repo_path,
            account=self.github_cli_account,
        )

    def filter_files(
        self,
        files: tuple[PullRequestFile, ...],
        repo_config: CodeReviewRepositoryConfig,
    ) -> tuple[PullRequestFile, ...]:
        valid: list[PullRequestFile] = []
        code_extensions = {extension.lower() for extension in repo_config.code_extensions}
        for file in files:
            filename = file.filename.strip()
            if not filename or Path(filename).suffix.lower() not in code_extensions:
                continue
            if repo_config.include_path_patterns and not any(_path_matches(filename, pattern) for pattern in repo_config.include_path_patterns):
                continue
            if repo_config.exclude_path_patterns and any(_path_matches(filename, pattern) for pattern in repo_config.exclude_path_patterns):
                continue
            valid.append(file)
        return tuple(valid)

    def notification_emails_for_retry(
        self,
        repo_config: CodeReviewRepositoryConfig,
        job: CodeReviewJob,
    ) -> tuple[str, ...]:
        github_client = self.github_client_for(repo_config)
        snapshot = github_client.fetch_pull_request(repo_full_name=job.repo_full_name, pr_number=job.pr_number)
        commits = github_client.fetch_pull_request_commits(repo_full_name=job.repo_full_name, pr_number=job.pr_number)
        return self._author_commit_emails(author_login=snapshot.author_login, commits=commits)

    def optional_notification_cc_emails_for_email_job(self, email_job: CodeReviewEmailJob) -> tuple[str, ...]:
        try:
            return self.notification_cc_emails_for_email_job(email_job)
        except (FileNotFoundError, ValueError) as exc:
            logger.warning(
                "代码 Review 邮件 Cc 配置不可用，继续发送主收件人邮件: email_job_id=%s error=%s",
                email_job.email_job_id,
                exc,
            )
            return ()

    def notification_cc_emails_for_email_job(self, email_job: CodeReviewEmailJob) -> tuple[str, ...]:
        job = self.store.get_job(email_job.code_review_job_id)
        if job is None:
            raise FileNotFoundError("代码 Review 任务不存在。")
        repo_config = self.repo_config_for_job(job)
        if repo_config.notification_recipient_mode != "all":
            return ()
        recipient_email = email_job.recipient_email.strip().lower()
        return tuple(email for email in self._normalized_cc_emails(repo_config) if email.lower() != recipient_email)

    def notification_recipient_emails_for_job(self, job: CodeReviewJob) -> tuple[str, ...]:
        if _is_manual_review_job(job):
            return ()
        repo_config = self.repo_config_for_job(job)
        if repo_config.notification_recipient_mode == "cc":
            return self._normalized_cc_emails(repo_config)
        return tuple(dict.fromkeys(_string_tuple(job.notification_emails)))

    def optional_notification_cc_emails_for_job(self, job: CodeReviewJob) -> tuple[str, ...]:
        try:
            return self.notification_cc_emails_for_job(job)
        except (FileNotFoundError, ValueError) as exc:
            logger.warning(
                "代码 Review 失败告警 Cc 配置不可用，跳过告警邮件: job_id=%s error=%s",
                job.job_id,
                exc,
            )
            return ()

    def notification_cc_emails_for_job(self, job: CodeReviewJob) -> tuple[str, ...]:
        if _is_manual_review_job(job):
            return ()
        repo_config = self.repo_config_for_job(job)
        return self._normalized_cc_emails(repo_config)

    def _normalized_cc_emails(self, repo_config: CodeReviewRepositoryConfig) -> tuple[str, ...]:
        return tuple(
            dict.fromkeys(
                email.strip()
                for email in repo_config.notification_cc_emails
                if email.strip()
            )
        )

    async def _poll_repository(
        self,
        repo_config: CodeReviewRepositoryConfig,
        *,
        sync_pr_links: bool = True,
        create_review_jobs: bool = True,
    ) -> list[CodeReviewJob]:
        created: list[CodeReviewJob] = []
        github_client = self.github_client_for(repo_config)
        listed_pull_requests = github_client.list_open_pull_requests(repo_full_name=repo_config.repo_full_name)
        # 需求—PR 关联发现在代码 Review 的 Reviewer/Draft/文件类型过滤之前执行，
        # 复用同一次 Open PR 列表请求，Draft 与未指定 Reviewer 的 PR 也会建立关联。
        if sync_pr_links:
            self.pr_link_service.sync_repository_pull_requests(
                repo_full_name=repo_config.repo_full_name,
                open_snapshots=listed_pull_requests,
                github_client=github_client,
            )
        if not create_review_jobs:
            return created
        for listed_pr in listed_pull_requests:
            if listed_pr.draft or listed_pr.state != "open":
                continue
            if repo_config.base_branches and listed_pr.base_ref not in repo_config.base_branches:
                continue
            if self.store.has_job_for_pr_head(
                repo_full_name=repo_config.repo_full_name,
                pr_number=listed_pr.pr_number,
                head_sha=listed_pr.head_sha,
            ):
                continue
            if not listed_pr.requested_reviewer_logins and not listed_pr.requested_team_slugs:
                continue
            events = sorted(
                github_client.fetch_pull_request_timeline(repo_full_name=repo_config.repo_full_name, pr_number=listed_pr.pr_number),
                key=lambda event: event.created_at or event.event_id,
            )
            requested_event = self._matching_current_review_request_event(listed_pr, events)
            if requested_event is None:
                continue
            reviewer_logins, team_slugs = self._review_request_targets(requested_event)
            snapshot = github_client.fetch_pull_request(repo_full_name=repo_config.repo_full_name, pr_number=listed_pr.pr_number)
            files = self.filter_files(snapshot.files, repo_config)
            if not files:
                continue
            filtered_snapshot = PullRequestSnapshot(**{**snapshot.__dict__, "files": files})
            emails = self._author_commit_emails(
                author_login=filtered_snapshot.author_login,
                commits=github_client.fetch_pull_request_commits(
                    repo_full_name=repo_config.repo_full_name,
                    pr_number=listed_pr.pr_number,
                ),
            )
            job = self.store.create_job(
                snapshot=filtered_snapshot,
                diff_hash=_hash_text(filtered_snapshot.diff_text),
                requested_event=requested_event,
                requested_reviewer_logins=reviewer_logins,
                requested_team_slugs=team_slugs,
                notification_emails=emails,
                review_policy_path=repo_config.review_policy_path,
            )
            created.append(job)
        return created

    def _matching_current_review_request_event(
        self,
        listed_pr: PullRequestSnapshot,
        events: tuple[PullRequestReviewRequestEvent, ...],
    ) -> PullRequestReviewRequestEvent | None:
        current_reviewers = set(listed_pr.requested_reviewer_logins)
        current_teams = set(listed_pr.requested_team_slugs)
        return next(
            (
                event
                for event in events
                if (event.reviewer_login and event.reviewer_login in current_reviewers)
                or (event.team_slug and event.team_slug in current_teams)
            ),
            None,
        )

    def _review_request_targets(
        self,
        event: PullRequestReviewRequestEvent,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        reviewer_logins = (event.reviewer_login,) if event.reviewer_login else ()
        team_slugs = (event.team_slug,) if event.team_slug else ()
        return reviewer_logins, team_slugs

    def _author_commit_emails(self, *, author_login: str, commits: tuple[PullRequestCommit, ...]) -> tuple[str, ...]:
        normalized_author = author_login.strip().lower()
        if not normalized_author:
            return ()
        emails: list[str] = []
        for commit in commits:
            if commit.author_login.strip().lower() != normalized_author:
                continue
            if commit.author_email:
                emails.append(commit.author_email)
        return tuple(dict.fromkeys(email.strip() for email in emails if email.strip()))


def _string_tuple(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        return tuple(item.strip() for item in value.split(",") if item.strip())
    if isinstance(value, (list, tuple, set)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _path_matches(path: str, pattern: str) -> bool:
    normalized = pattern.strip()
    if not normalized:
        return False
    if normalized.endswith("/"):
        return path.startswith(normalized)
    if "*" in normalized:
        regex = "^" + re.escape(normalized).replace(r"\*", ".*") + "$"
        return re.match(regex, path) is not None
    return path == normalized or path.startswith(f"{normalized}/")


def _notification_recipient_mode(value: object) -> str:
    mode = str(value or "all").strip().lower()
    if mode not in {"all", "creator", "cc"}:
        raise ValueError("notification_recipient_mode 只能配置为 all、creator 或 cc。")
    return mode


def _is_manual_review_job(job: CodeReviewJob) -> bool:
    return job.requested_event_id.startswith("manual:")
