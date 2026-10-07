from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.business.business_doc_updates.service import (
    GitHubPullRequestClient,
    PullRequestReviewRequestEvent,
    PullRequestSnapshot,
)
from app.business.code_reviews.email import build_email_body, build_email_html_body
from app.business.code_reviews.polling import (
    CodeReviewPoller,
    CodeReviewProjectConfig,
)
from app.business.code_reviews.reports import (
    CodeReviewArtifacts,
    CodeReviewDetail,
    CodeReviewRecord,
    DEFAULT_REVIEW_POLICY_PATH,
)
from app.business.code_reviews.review_context import (
    CodeReviewContextPreparer,
    CodeReviewPreparedContext,
)
from app.business.git_context import GitWorktreeManager
from app.business.code_reviews.store import CodeReviewEmailJob, CodeReviewJob, CodeReviewStore
from app.business.requirement_pr_links import RequirementPrLinkService
from app.integrations.agent_runtime import AgentRuntimeClient
from app.services.email_service import EmailSender


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PreparedAssistantPullRequest:
    snapshot: PullRequestSnapshot
    context: CodeReviewPreparedContext
    organization_key: str


@dataclass(frozen=True)
class _PullRequestContextJob:
    repo_full_name: str
    pr_number: int
    pr_url: str
    base_ref: str
    base_sha: str
    head_sha: str


class CodeReviewService:
    default_code_extensions = (
        ".c",
        ".cc",
        ".cpp",
        ".cs",
        ".go",
        ".java",
        ".js",
        ".jsx",
        ".kt",
        ".php",
        ".py",
        ".rb",
        ".rs",
        ".sql",
        ".ts",
        ".tsx",
    )

    def __init__(
        self,
        *,
        base_dir: Path,
        requirements_root: Path,
        skills_root: Path,
        store: CodeReviewStore,
        runtime_client: AgentRuntimeClient,
        github_client: GitHubPullRequestClient,
        email_sender: EmailSender,
        pr_link_service: RequirementPrLinkService,
        config_path: str = "",
        github_cli_account: str = "",
        default_poll_interval_seconds: int = 300,
        email_max_attempts: int = 3,
        running_timeout_seconds: int = 3600,
        worktree_manager: GitWorktreeManager | None = None,
    ) -> None:
        self.base_dir = base_dir.resolve()
        self.workspace_root = self.base_dir / "workspace"
        self.skills_root = skills_root.resolve()
        self.reviews_root = self.workspace_root / "knowledge/__reviews__/code-review"
        self.review_artifacts = CodeReviewArtifacts(base_dir=self.base_dir, reviews_root=self.reviews_root)
        self.worktree_manager = worktree_manager or GitWorktreeManager(workspace_root=self.workspace_root)
        self.context_preparer = CodeReviewContextPreparer(
            base_dir=self.base_dir,
            workspace_root=self.workspace_root,
            requirements_root=requirements_root,
            worktree_manager=self.worktree_manager,
        )
        self.store = store
        self.runtime_client = runtime_client
        self.github_client = github_client
        self.email_sender = email_sender
        self.config_path = config_path.strip()
        self.default_poll_interval_seconds = default_poll_interval_seconds
        self.poller = CodeReviewPoller(
            base_dir=self.base_dir,
            store=self.store,
            github_client=self.github_client,
            pr_link_service=pr_link_service,
            config_path=self.config_path,
            github_cli_account=github_cli_account,
            default_poll_interval_seconds=self.default_poll_interval_seconds,
            default_code_extensions=self.default_code_extensions,
        )
        self.email_max_attempts = email_max_attempts
        self.running_timeout_seconds = max(running_timeout_seconds, 60)

    def load_project_config(self) -> CodeReviewProjectConfig | None:
        return self.poller.load_project_config()

    async def poll_loop(self) -> None:
        logger.info("PR 代码自动 Review 轮询任务已启动")
        while True:
            interval_seconds = self.default_poll_interval_seconds
            try:
                config = self.load_project_config()
                if config is not None:
                    interval_seconds = config.poll_interval_seconds
                    await self.poll_once(config=config)
            except Exception:
                logger.exception("PR 代码自动 Review 轮询出错，下次将继续重试")
            await asyncio.sleep(interval_seconds)

    async def job_loop(self, *, interval_seconds: int = 30, limit: int = 5) -> None:
        logger.info("PR 代码自动 Review worker 已启动，间隔=%ds", interval_seconds)
        while True:
            try:
                self.recover_stale_running_jobs()
                await self.process_pending_jobs(limit=limit)
            except Exception:
                logger.exception("PR 代码自动 Review worker 出错，下次将继续重试")
            await asyncio.sleep(interval_seconds)

    async def email_job_loop(self, *, interval_seconds: int = 30, limit: int = 5) -> None:
        logger.info("PR 代码自动 Review 邮件 worker 已启动，间隔=%ds", interval_seconds)
        while True:
            try:
                self.process_retryable_email_jobs(limit=limit)
            except Exception:
                logger.exception("PR 代码自动 Review 邮件 worker 出错，下次将继续重试")
            await asyncio.sleep(interval_seconds)

    async def poll_once(self, *, config: CodeReviewProjectConfig | None = None) -> list[CodeReviewJob]:
        return await self.poller.poll_once(config=config)

    def enqueue_manual_review(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        requested_by_email: str,
    ) -> CodeReviewJob:
        config = self.load_project_config()
        if config is None:
            raise ValueError("代码 Review 项目配置不存在。")
        repo_config = next(
            (
                item
                for item in config.repositories
                if item.repo_full_name == repo_full_name and item.enabled
            ),
            None,
        )
        if repo_config is None:
            raise ValueError("该 GitHub 仓库未配置为代码 Review 来源。")
        github_client = self.poller.github_client_for(repo_config)
        snapshot = github_client.fetch_pull_request(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )
        if snapshot.state != "open":
            raise ValueError("PR 已关闭，不能发起代码 Review。")
        if snapshot.draft:
            raise ValueError("Draft PR 不能发起代码 Review。")
        if repo_config.base_branches and snapshot.base_ref not in repo_config.base_branches:
            raise ValueError("PR 目标分支不在代码 Review 范围内。")
        files = self.poller.filter_files(snapshot.files, repo_config)
        if not files:
            raise ValueError("PR 没有有效代码文件变化。")
        snapshot = PullRequestSnapshot(**{**snapshot.__dict__, "files": files})
        existing = self.store.get_job_for_pr_head(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
            head_sha=snapshot.head_sha,
        )
        if existing is not None:
            if existing.status == "failed":
                return self.store.retry_job(job_id=existing.job_id, notification_emails=())
            return existing
        requested_event = PullRequestReviewRequestEvent(
            event_id=f"manual:{requested_by_email.strip().lower()}:{snapshot.head_sha}",
            actor_login=requested_by_email.strip().lower(),
            reviewer_login="",
            team_slug="",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        return self.store.create_job(
            snapshot=snapshot,
            diff_hash="sha256:" + hashlib.sha256(snapshot.diff_text.encode("utf-8")).hexdigest(),
            requested_event=requested_event,
            requested_reviewer_logins=(),
            requested_team_slugs=(),
            notification_emails=(),
            review_policy_path=repo_config.review_policy_path,
        )

    def prepare_assistant_pull_request(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
    ) -> PreparedAssistantPullRequest:
        config = self.load_project_config()
        if config is None:
            raise ValueError("受控代码仓库配置不存在。")
        repo_config = next(
            (
                item
                for item in config.repositories
                if item.repo_full_name == repo_full_name and item.enabled
            ),
            None,
        )
        if repo_config is None:
            raise ValueError("该 GitHub 仓库未配置为受控代码仓库。")
        snapshot = self.poller.github_client_for(repo_config).fetch_pull_request(
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )
        context = self.context_preparer.prepare(
            job=_PullRequestContextJob(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                pr_url=snapshot.html_url,
                base_ref=snapshot.base_ref,
                base_sha=snapshot.base_sha,
                head_sha=snapshot.head_sha,
            ),
            snapshot=snapshot,
            repo_config=repo_config,
        )
        organization_key = repo_config.organization_key.strip().lower()
        if not organization_key:
            repo_path = Path(repo_config.workspace_repo_path)
            if not repo_path.is_absolute():
                repo_path = self.base_dir / repo_path
            organization_key = repo_path.resolve().relative_to(self.workspace_root).parts[0]
        return PreparedAssistantPullRequest(
            snapshot=snapshot,
            context=context,
            organization_key=organization_key,
        )

    async def process_pending_jobs(self, *, limit: int = 5) -> list[CodeReviewRecord]:
        completed: list[CodeReviewRecord] = []
        for job in self.store.list_pending_jobs(limit=limit):
            review = await self.run_job(job)
            if review is not None:
                completed.append(review)
        return completed

    def recover_stale_running_jobs(self) -> list[CodeReviewJob]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.running_timeout_seconds)
        stale_jobs = self.store.fail_stale_running_jobs(
            cutoff_started_at=cutoff.isoformat(),
            error_message=f"代码 Review 任务运行超过 {self.running_timeout_seconds} 秒，已标记为失败以便排查或重试。",
        )
        for job in stale_jobs:
            logger.warning(
                "PR 代码自动 Review running 任务超时回收: job_id=%s repo=%s pr=%s started_at=%s",
                job.job_id,
                job.repo_full_name,
                job.pr_number,
                job.started_at,
            )
        return stale_jobs

    async def run_job(self, job: CodeReviewJob) -> CodeReviewRecord | None:
        if not self.store.mark_job_running(job.job_id):
            return None
        try:
            logger.info("PR 代码自动 Review 任务开始: job_id=%s repo=%s pr=%s", job.job_id, job.repo_full_name, job.pr_number)
            repo_config = self.poller.repo_config_for_job(job)
            github_client = self.poller.github_client_for(repo_config)
            snapshot = github_client.fetch_pull_request(repo_full_name=job.repo_full_name, pr_number=job.pr_number)
            if snapshot.state != "open":
                raise ValueError("PR 已关闭，代码 Review 任务跳过。")
            if snapshot.draft:
                raise ValueError("Draft PR 不生成代码 Review。")
            if snapshot.base_sha != job.base_sha:
                raise ValueError("PR base_sha 已变化，旧 base_sha 任务跳过。")
            if snapshot.head_sha != job.head_sha:
                raise ValueError("PR 已更新，旧 head_sha 任务跳过。")
            files = self.poller.filter_files(snapshot.files, repo_config)
            if not files:
                raise ValueError("PR 没有有效代码文件变化。")
            snapshot = PullRequestSnapshot(**{**snapshot.__dict__, "files": files})
            prepared_context = self.context_preparer.prepare(job=job, snapshot=snapshot, repo_config=repo_config)
            generated = await self._generate_review_markdown(
                job=job,
                prepared_context=prepared_context,
            )
            if not generated.strip():
                raise RuntimeError("代码 Review runtime 未返回有效评审正文。")
            review = self.review_artifacts.write_review(job=job, snapshot=snapshot, generated_markdown=generated)
            self.store.complete_job(job_id=job.job_id, review_id=review.review_id, review_path=review.path)
            refreshed_job = self.store.get_job(job.job_id) or job
            self.store.create_email_jobs(
                job=refreshed_job,
                review_id=review.review_id,
                subject=f"[AI Code Review] {job.repo_full_name} PR #{job.pr_number}: {snapshot.title}",
                recipient_emails=self.poller.notification_recipient_emails_for_job(refreshed_job),
            )
            logger.info(
                "PR 代码自动 Review 任务完成: job_id=%s repo=%s pr=%s review_id=%s review_path=%s",
                job.job_id,
                job.repo_full_name,
                job.pr_number,
                review.review_id,
                review.path,
            )
            return review
        except Exception as exc:
            error_message = str(exc)
            self.store.fail_job(job_id=job.job_id, error_message=error_message)
            self._send_failure_alert(job=job, error_message=error_message)
            logger.exception("PR 代码自动 Review 任务失败: job_id=%s repo=%s pr=%s", job.job_id, job.repo_full_name, job.pr_number)
            return None

    def process_retryable_email_jobs(self, *, limit: int = 5) -> list[CodeReviewEmailJob]:
        sent: list[CodeReviewEmailJob] = []
        for job in self.store.list_retryable_email_jobs(limit=limit, max_attempts=self.email_max_attempts):
            processed = self.run_email_job(job)
            if processed is not None and processed.status == "sent":
                sent.append(processed)
        return sent

    def run_email_job(self, job: CodeReviewEmailJob) -> CodeReviewEmailJob | None:
        if not self.store.mark_email_job_running(job.email_job_id):
            return None
        try:
            review = self.get_review_detail(review_id=job.review_id)
            text = build_email_body(review)
            html = build_email_html_body(review)
            cc = self.poller.optional_notification_cc_emails_for_email_job(job)
            self.email_sender.send_message(to=job.recipient_email, subject=job.subject, text=text, html=html, cc=cc)
            self.store.complete_email_job(email_job_id=job.email_job_id)
        except Exception as exc:
            self.store.fail_email_job(email_job_id=job.email_job_id, error_message=str(exc))
        return self.store.get_email_job(job.email_job_id)

    def _send_failure_alert(self, *, job: CodeReviewJob, error_message: str) -> None:
        recipients = self.poller.optional_notification_cc_emails_for_job(job)
        if not recipients:
            return
        subject = f"[AI Code Review][告警] {job.repo_full_name} PR #{job.pr_number}: 自动审核失败"
        text = "\n".join(
            [
                "PR 自动代码 Review 执行失败，正常审核结果通知不会发送。",
                "",
                f"仓库：{job.repo_full_name}",
                f"PR：#{job.pr_number}",
                f"链接：{job.pr_url}",
                f"Base：{job.base_ref} {job.base_sha}",
                f"Head：{job.head_sha}",
                f"触发人：{job.requested_by_login or '-'}",
                "",
                "错误信息：",
                error_message or "-",
            ]
        )
        for recipient in recipients:
            try:
                self.email_sender.send_message(to=recipient, subject=subject, text=text)
            except Exception:
                logger.exception(
                    "PR 代码自动 Review 失败告警邮件发送失败: job_id=%s recipient=%s",
                    job.job_id,
                    recipient,
                )

    def retry_job(self, *, job_id: str) -> CodeReviewJob:
        job = self.get_job(job_id=job_id)
        repo_config = self.poller.repo_config_for_job(job)
        emails = self.poller.notification_emails_for_retry(repo_config, job)
        return self.store.retry_job(job_id=job_id, notification_emails=emails)

    def retry_email_job(self, *, email_job_id: str) -> CodeReviewEmailJob:
        return self.store.retry_email_job(email_job_id=email_job_id)

    def get_job(self, *, job_id: str) -> CodeReviewJob:
        job = self.store.get_job(job_id)
        if job is None:
            raise FileNotFoundError("代码 Review 任务不存在。")
        return job

    def list_jobs(
        self,
        *,
        repo_full_name: str | None = None,
        pr_number: int | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[CodeReviewJob]:
        return self.store.list_jobs(repo_full_name=repo_full_name, pr_number=pr_number, status=status, limit=limit)

    def get_job_review(self, *, job: CodeReviewJob) -> CodeReviewRecord | None:
        return self.review_artifacts.get_job_review(job=job)

    def get_review_detail(self, *, review_id: str) -> CodeReviewDetail:
        return self.review_artifacts.get_review_detail(review_id=review_id)

    async def _generate_review_markdown(
        self,
        *,
        job: CodeReviewJob,
        prepared_context: CodeReviewPreparedContext,
    ) -> str:
        return await self._send_review_prompt(
            job=job,
            prompt=self._build_context_review_prompt(prepared_context),
            system_prompt=self._build_review_system_prompt(job.review_policy_path),
            metadata={
                "review_policy_path": job.review_policy_path,
                "pull_request_path": prepared_context.pull_request_path,
                "diff_path": prepared_context.diff_path,
                "changed_files_path": prepared_context.changed_files_path,
                "worktree_path": prepared_context.worktree_path,
            },
        )

    def _build_context_review_prompt(self, prepared_context: CodeReviewPreparedContext) -> str:
        requirement_lines = _format_requirement_prompt_lines(prepared_context.requirement_paths)
        return "\n".join(
            [
                "请为以下 GitHub PR 执行代码审查。最终 review 结果必须直接在本次回复中输出中文 Markdown；不要创建、写入或修改任何文件。",
                "",
                "## 审查目标",
                "",
                f"- 仓库: `{prepared_context.repo_full_name}`",
                f"- PR: `#{prepared_context.pr_number}`",
                f"- PR URL: {prepared_context.pr_url}",
                f"- Base: `{prepared_context.base_ref}` `{prepared_context.base_sha}`",
                f"- Head: `{prepared_context.head_sha}`",
                "",
                "## 必读资料",
                "",
                "请按顺序读取并使用以下资料：",
                "以下路径均为当前工作目录下的相对路径，请直接按原样读取，不要转换成去掉 `workspace` 的绝对路径。",
                "",
                f"1. PR metadata: `{prepared_context.pull_request_path}`",
                "2." + requirement_lines[0][2:],
                *requirement_lines[1:],
                f"3. Changed files: `{prepared_context.changed_files_path}`",
                f"4. Diff: `{prepared_context.diff_path}`",
                f"5. PR 分支完整代码目录: `{prepared_context.worktree_path}`",
                "",
                "## 审查要求",
                "",
                "- 按系统提示词中的代码审查规范和输出格式生成报告。",
                "- 结合需求文档判断代码是否完整覆盖需求。",
                "- 重点审查代码相对需求的正确性、遗漏、越界改动、风险和测试缺口。",
                "- 必要时进入 PR 分支代码目录读取相邻代码、调用关系和测试。",
                "- 最终报告必须直接作为 assistant 回复输出，禁止写入文件或只返回文件路径。",
            ]
        )

    def _build_review_system_prompt(self, review_policy_path: str) -> str:
        policy_path = self._resolve_review_policy_path(review_policy_path)
        policy_text = policy_path.read_text(encoding="utf-8").strip()
        return "\n\n".join(
            [
                "你是 ai-prd 的 PR 自动代码 Review 执行器。只在本次回复中直接输出中文 Markdown 代码审查报告，不创建、不写入、不修改任何文件。",
                "以下是必须遵循的代码审查规范：",
                policy_text,
            ]
        )

    def _resolve_review_policy_path(self, review_policy_path: str) -> Path:
        normalized = review_policy_path.strip() or DEFAULT_REVIEW_POLICY_PATH
        path = Path(normalized)
        if not path.is_absolute():
            path = self.base_dir / path
        path = path.resolve()
        if path.is_file():
            return path
        if normalized == DEFAULT_REVIEW_POLICY_PATH:
            packaged_path = Path(__file__).with_name("default_review_policy.md").resolve()
            if packaged_path.is_file():
                return packaged_path
        raise FileNotFoundError(f"代码 Review policy 文件不存在：{path}")

    async def _send_review_prompt(
        self,
        *,
        job: CodeReviewJob,
        prompt: str,
        system_prompt: str,
        metadata: dict[str, object],
    ) -> str:
        session = await self.runtime_client.create_or_resume_session(
            runtime_session_id=None,
            working_directory=str(self.base_dir),
            system_prompt=system_prompt,
        )
        chunks: list[str] = []
        final_message = ""
        async for event in self.runtime_client.send_message_stream(
            session=session,
            message=prompt,
            metadata={"task": "code_review", "repo_full_name": job.repo_full_name, "pr_number": job.pr_number, **metadata},
        ):
            if event.type == "delta":
                text = event.data.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            if event.type == "message":
                content = event.data.get("content")
                if isinstance(content, str):
                    final_message = content
            if event.type == "complete" and not final_message:
                result = event.data.get("result")
                if isinstance(result, str):
                    final_message = result
        return (final_message or "".join(chunks)).strip()


def _format_requirement_prompt_lines(requirement_paths: tuple[str, ...]) -> list[str]:
    if not requirement_paths:
        return ["1. 需求文档: 无"]
    return ["1. 需求文档:", *(f"   - `{path}`" for path in requirement_paths)]
