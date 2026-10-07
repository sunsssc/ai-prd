from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.business.business_doc_updates.service import (
    GitHubCliPullRequestClient,
    PullRequestCommit,
    PullRequestFile,
    PullRequestReviewRequestEvent,
    PullRequestSnapshot,
)
from app.business.code_reviews import CodeReviewService, CodeReviewStore
from app.business.code_reviews.email import render_markdown_for_email
from app.business.code_reviews.review_context import extract_clickup_task_ids
from app.business.requirement_pr_links import RequirementPrLinkService, RequirementPrLinkStore
from app.integrations.agent_runtime.models import RuntimeEvent, RuntimeSession


_DEFAULT_CONFIG = object()


class _CodeReviewRuntime:
    provider = "test-code-review"

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.metadata: list[dict[str, object]] = []
        self.system_prompts: list[str] = []

    async def create_or_resume_session(self, *, runtime_session_id, working_directory, system_prompt):
        del runtime_session_id
        self.system_prompts.append(system_prompt)
        assert "你是 ai-prd 的 PR 自动代码 Review 执行器" in system_prompt
        assert "只在本次回复中直接输出中文 Markdown" in system_prompt
        assert "代码审查测试规范" in system_prompt
        assert "每条 finding 必须使用三级标题" in system_prompt
        return RuntimeSession(provider=self.provider, working_directory=working_directory, session_id="code-review-session")

    async def send_message_stream(self, *, session, message, metadata):
        del session
        self.messages.append(message)
        self.metadata.append(metadata)
        assert "最终 review 结果必须直接在本次回复中输出中文 Markdown" in message
        assert "路径均为当前工作目录下的相对路径" in message
        assert "禁止写入文件或只返回文件路径" in message
        assert "runtime/code-review-contexts/example-org/example_backend/PR_1234/" in message
        assert "/changed-files.txt" in message
        assert "/diff.patch" in message
        assert "/requirements/requirement-1.md" in message
        assert "runtime/code-review-worktrees/example-org/example_backend/revisions/" in message
        assert "coinex/knowledge/requirements/tasks/in_progress/coupon__86abc.md" not in message
        assert ".claude/skills/code-review-pr/SKILL.md" not in message
        assert "review-context.md" not in message
        assert "代码审查测试规范" not in message
        assert "# code-review-pr Skill" not in message
        assert "@@\n- status = 'available'" not in message
        assert "领取后增加待激活状态" not in message
        yield RuntimeEvent(
            "message",
            {
                "content": (
                    "# 代码 Review 报告\n\n"
                    "## 主要问题\n\n"
                    "- 严重级别：高\n"
                    "- `app/coupon/service.py` 缺少待激活状态的边界校验。\n\n"
                    "## 验证点\n\n"
                    "- 补充优惠券状态流转单元测试。"
                )
            },
        )

    async def list_available_skills(self, *, working_directory):
        del working_directory
        return []


class _EmptyCodeReviewRuntime(_CodeReviewRuntime):
    async def send_message_stream(self, *, session, message, metadata):
        del session, message, metadata
        yield RuntimeEvent("complete", {"result": ""})


class _FakeGitHubPullRequestClient:
    def __init__(
        self,
        *,
        snapshot: PullRequestSnapshot,
        events: tuple[PullRequestReviewRequestEvent, ...],
        commits: tuple[PullRequestCommit, ...],
    ) -> None:
        self.snapshot = snapshot
        self.events = events
        self.commits = commits

    def list_open_pull_requests(self, *, repo_full_name: str) -> tuple[PullRequestSnapshot, ...]:
        assert repo_full_name == self.snapshot.repo_full_name
        return (PullRequestSnapshot(**{**self.snapshot.__dict__, "files": ()}),)

    def fetch_pull_request(self, *, repo_full_name: str, pr_number: int) -> PullRequestSnapshot:
        assert repo_full_name == self.snapshot.repo_full_name
        assert pr_number == self.snapshot.pr_number
        return self.snapshot

    def fetch_pull_request_timeline(self, *, repo_full_name: str, pr_number: int) -> tuple[PullRequestReviewRequestEvent, ...]:
        assert repo_full_name == self.snapshot.repo_full_name
        assert pr_number == self.snapshot.pr_number
        return self.events

    def fetch_pull_request_commits(self, *, repo_full_name: str, pr_number: int) -> tuple[PullRequestCommit, ...]:
        assert repo_full_name == self.snapshot.repo_full_name
        assert pr_number == self.snapshot.pr_number
        return self.commits


class _CapturingEmailSender:
    def __init__(self, *, fail_first: bool = False) -> None:
        self.fail_first = fail_first
        self.messages: list[dict[str, str]] = []

    def send_verification_code(self, *, email: str, code: str, purpose: str) -> None:
        del email, code, purpose

    def send_message(
        self,
        *,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        cc: tuple[str, ...] = (),
    ) -> None:
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError("smtp down")
        self.messages.append({"to": to, "subject": subject, "text": text, "html": html or "", "cc": ",".join(cc)})


@pytest.mark.anyio
async def test_code_review_poll_does_not_run_without_explicit_config(tmp_path: Path) -> None:
    service = _build_service(tmp_path, config_payload=None)

    assert await service.poll_once() == []


@pytest.mark.anyio
async def test_code_review_poll_syncs_pr_links_when_auto_review_is_disabled(tmp_path: Path) -> None:
    config = _config_payload()
    config["enabled"] = False
    service = _build_service(tmp_path, config_payload=config)

    assert await service.poll_once() == []
    links = service.poller.pr_link_service.store.list_for_task(task_id="86abc")
    assert [link.pr_number for link in links] == [1234]


@pytest.mark.anyio
async def test_code_review_poll_syncs_pr_links_for_link_only_repository(tmp_path: Path) -> None:
    config = _config_payload()
    config["repositories"][0]["enabled"] = False
    config["repositories"][0]["pr_link_enabled"] = True
    service = _build_service(tmp_path, config_payload=config)

    assert await service.poll_once() == []
    links = service.poller.pr_link_service.store.list_for_task(task_id="86abc")
    assert [link.pr_number for link in links] == [1234]


@pytest.mark.anyio
async def test_code_review_poll_can_disable_pr_links_without_disabling_review(tmp_path: Path) -> None:
    config = _config_payload()
    config["repositories"][0]["pr_link_enabled"] = False
    service = _build_service(tmp_path, config_payload=config)

    assert len(await service.poll_once()) == 1
    assert service.poller.pr_link_service.store.list_for_task(task_id="86abc") == ()


def test_code_review_gh_cli_client_uses_project_account(tmp_path: Path) -> None:
    config = _config_payload()
    config["repositories"][0]["github_access"] = "gh_cli"
    service = _build_service(tmp_path, config_payload=config, github_cli_account="jordan-lee")
    project_config = service.load_project_config()

    assert project_config is not None
    client = service.poller.github_client_for(project_config.repositories[0])
    assert isinstance(client, GitHubCliPullRequestClient)
    assert client.account == "jordan-lee"


def test_github_cli_client_isolates_configured_account_from_ambient_auth(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        env = kwargs["env"]
        assert isinstance(env, dict)
        if command[1:3] == ["auth", "token"]:
            assert command[3:] == ["--hostname", "github.com", "--user", "jordan-lee"]
            assert "GH_TOKEN" not in env
            assert "GITHUB_TOKEN" not in env
            assert "GH_ENTERPRISE_TOKEN" not in env
            assert "GITHUB_ENTERPRISE_TOKEN" not in env
            return subprocess.CompletedProcess(command, 0, stdout="selected-token\n", stderr="")
        assert command[1:4] == ["api", "--hostname", "github.com"]
        assert env["GH_TOKEN"] == "selected-token"
        assert env["GH_HOST"] == "github.com"
        assert env["GH_PROMPT_DISABLED"] == "1"
        assert "GITHUB_TOKEN" not in env
        return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

    monkeypatch.setenv("GH_TOKEN", "ambient-gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "ambient-github-token")
    monkeypatch.setenv("GH_ENTERPRISE_TOKEN", "ambient-enterprise-token")
    monkeypatch.setattr(subprocess, "run", fake_run)
    client = GitHubCliPullRequestClient(
        repo_full_name="example-org/example_backend",
        working_directory=tmp_path,
        account="jordan-lee",
    )

    assert client.list_open_pull_requests(repo_full_name="example-org/example_backend") == ()
    assert client.list_open_pull_requests(repo_full_name="example-org/example_backend") == ()
    assert sum(command[1:3] == ["auth", "token"] for command, _ in calls) == 1
    assert all("switch" not in command for command, _ in calls)


def test_github_cli_client_reports_missing_configured_account(tmp_path: Path) -> None:
    client = GitHubCliPullRequestClient(
        repo_full_name="example-org/example_backend",
        working_directory=tmp_path,
        account="",
    )

    with pytest.raises(ValueError, match="GITHUB_CLI_ACCOUNT 未配置"):
        client.list_open_pull_requests(repo_full_name="example-org/example_backend")


def test_manual_code_review_uses_current_pr_head_without_github_side_effects(tmp_path: Path) -> None:
    config = _config_payload()
    config["enabled"] = False
    service = _build_service(tmp_path, config_payload=config)

    created = service.enqueue_manual_review(
        repo_full_name="example-org/example_backend",
        pr_number=1234,
        requested_by_email="jordan.lee@corp.test",
    )
    duplicated = service.enqueue_manual_review(
        repo_full_name="example-org/example_backend",
        pr_number=1234,
        requested_by_email="jordan.lee@corp.test",
    )

    assert created.job_id == duplicated.job_id
    assert created.requested_event_id.startswith("manual:jordan.lee@corp.test:")
    assert created.notification_emails == ""
    assert service.poller.notification_recipient_emails_for_job(created) == ()
    assert service.poller.notification_cc_emails_for_job(created) == ()


@pytest.mark.anyio
async def test_code_review_poll_creates_first_review_request_job_once(tmp_path: Path) -> None:
    service = _build_service(tmp_path)

    created = await service.poll_once()
    duplicated = await service.poll_once()

    assert len(created) == 1
    assert duplicated == []
    job = created[0]
    assert job.repo_full_name == "example-org/example_backend"
    assert job.pr_number == 1234
    assert job.requested_event_id == "event-1"
    assert job.requested_by_login == "requester"
    assert job.requested_reviewer_logins == "alice"
    assert job.notification_emails == "author@corp.test"
    assert job.skill_name is None


def test_code_review_store_makes_existing_skill_name_column_nullable(tmp_path: Path) -> None:
    db_path = tmp_path / "assistant.sqlite3"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE code_review_jobs (
                job_id TEXT PRIMARY KEY,
                repo_full_name TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                pr_url TEXT NOT NULL,
                base_ref TEXT NOT NULL,
                base_sha TEXT NOT NULL,
                head_sha TEXT NOT NULL,
                diff_hash TEXT NOT NULL,
                requested_event_id TEXT NOT NULL,
                requested_by_login TEXT NOT NULL,
                requested_reviewer_logins TEXT NOT NULL,
                requested_team_slugs TEXT NOT NULL,
                notification_emails TEXT NOT NULL,
                review_type TEXT NOT NULL,
                skill_name TEXT NOT NULL,
                review_policy_path TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed')),
                review_id TEXT,
                review_path TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                UNIQUE(repo_full_name, pr_number)
            )
            """
        )
    store = CodeReviewStore(str(db_path))
    with store._connect() as connection:
        skill_name_column = next(
            row
            for row in connection.execute("PRAGMA table_info(code_review_jobs)").fetchall()
            if row["name"] == "skill_name"
        )
    assert int(skill_name_column["notnull"]) == 0

    created = store.create_job(
        snapshot=_snapshot(),
        diff_hash="sha256:test",
        requested_event=_events()[0],
        requested_reviewer_logins=("alice",),
        requested_team_slugs=(),
        notification_emails=("author@corp.test",),
        review_policy_path="workspace/config/code-review-policy.md",
    )

    assert created.skill_name is None
    loaded = store.get_job(created.job_id)
    assert loaded is not None
    assert loaded.skill_name is None


@pytest.mark.anyio
async def test_code_review_poll_creates_job_without_author_commit_email(tmp_path: Path) -> None:
    service = _build_service(
        tmp_path,
        commits=(PullRequestCommit(author_login="other-author", author_email="other@corp.test"),),
    )

    created = await service.poll_once()

    assert len(created) == 1
    assert created[0].notification_emails == ""


@pytest.mark.anyio
async def test_code_review_poll_skips_historical_review_request_when_no_current_request(tmp_path: Path) -> None:
    service = _build_service(
        tmp_path,
        snapshot=PullRequestSnapshot(**{**_snapshot().__dict__, "requested_reviewer_logins": ()}),
    )

    assert await service.poll_once() == []


@pytest.mark.anyio
async def test_code_review_poll_uses_unique_author_commit_emails(tmp_path: Path) -> None:
    service = _build_service(
        tmp_path,
        commits=(
            PullRequestCommit(author_login="author", author_email="author@corp.test"),
            PullRequestCommit(author_login="author", author_email="author@corp.test"),
            PullRequestCommit(author_login="author", author_email="author.personal@example.com"),
            PullRequestCommit(author_login="other-author", author_email="other@corp.test"),
        ),
    )

    created = await service.poll_once()

    assert len(created) == 1
    assert created[0].notification_emails == "author@corp.test,author.personal@example.com"


@pytest.mark.anyio
async def test_code_review_run_job_writes_report_and_creates_email_job(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    job = (await service.poll_once())[0]

    review = await service.run_job(job)

    assert review is not None
    assert review.risk_level == "high"
    review_file = tmp_path / review.path
    assert review_file.is_file()
    assert "缺少待激活状态" in review_file.read_text(encoding="utf-8")
    assert len(service.runtime_client.messages) == 1
    assert len(service.runtime_client.system_prompts) == 1
    assert service.runtime_client.metadata[0]["review_policy_path"] == "workspace/config/code-review-policy.md"
    revision = job.head_sha
    assert service.runtime_client.metadata[0]["changed_files_path"] == (
        f"runtime/code-review-contexts/example-org/example_backend/PR_1234/{revision}/changed-files.txt"
    )
    assert service.runtime_client.metadata[0]["diff_path"] == (
        f"runtime/code-review-contexts/example-org/example_backend/PR_1234/{revision}/diff.patch"
    )
    assert service.runtime_client.metadata[0]["worktree_path"] == (
        f"runtime/code-review-worktrees/example-org/example_backend/revisions/{revision}"
    )

    worktree_dir = tmp_path / f"workspace/runtime/code-review-worktrees/example-org/example_backend/revisions/{revision}"
    context_dir = tmp_path / f"workspace/runtime/code-review-contexts/example-org/example_backend/PR_1234/{revision}"
    assert _git(worktree_dir, "rev-parse", "HEAD") == job.head_sha
    assert "pending_activation" in (context_dir / "diff.patch").read_text(encoding="utf-8")
    assert "M\tapp/coupon/service.py" in (context_dir / "changed-files.txt").read_text(encoding="utf-8")
    requirement_copy = context_dir / "requirements/requirement-1.md"
    assert requirement_copy.is_file()
    requirement_text = requirement_copy.read_text(encoding="utf-8")
    assert "Original path: `coinex/knowledge/requirements/tasks/in_progress/coupon__86abc.md`" in requirement_text
    assert "本地需求内容" in requirement_text
    assert not (context_dir / "review-context.md").exists()
    prompt = service.runtime_client.messages[0]
    assert f"`runtime/code-review-worktrees/example-org/example_backend/revisions/{revision}`" in prompt
    assert f"`runtime/code-review-contexts/example-org/example_backend/PR_1234/{revision}/requirements/requirement-1.md`" in prompt
    assert "`coinex/knowledge/requirements/tasks/in_progress/coupon__86abc.md`" not in prompt
    assert "`workspace/runtime/" not in prompt
    assert "作者" not in prompt
    assert "每条 finding 必须使用三级标题" not in prompt

    email_jobs = service.store.list_retryable_email_jobs()
    assert len(email_jobs) == 1
    assert email_jobs[0].recipient_email == "author@corp.test"
    assert email_jobs[0].status == "pending"


@pytest.mark.anyio
async def test_code_review_run_job_fails_when_runtime_returns_empty_report(tmp_path: Path) -> None:
    service = _build_service(tmp_path, runtime_client=_EmptyCodeReviewRuntime())
    job = (await service.poll_once())[0]

    review = await service.run_job(job)

    assert review is None
    failed = service.store.get_job(job.job_id)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.review_id is None
    assert "未返回有效评审正文" in (failed.error_message or "")
    assert service.store.list_retryable_email_jobs() == []


@pytest.mark.anyio
async def test_code_review_recovers_stale_running_job(tmp_path: Path) -> None:
    service = _build_service(tmp_path, running_timeout_seconds=3600)
    job = (await service.poll_once())[0]
    assert service.store.mark_job_running(job.job_id)
    stale_started_at = (datetime.now(timezone.utc) - timedelta(seconds=7200)).isoformat()
    with service.store._connect() as connection:
        connection.execute("UPDATE code_review_jobs SET started_at = ? WHERE job_id = ?", (stale_started_at, job.job_id))

    recovered = service.recover_stale_running_jobs()

    assert [item.job_id for item in recovered] == [job.job_id]
    updated = service.store.get_job(job.job_id)
    assert updated is not None
    assert updated.status == "failed"
    assert "运行超过 3600 秒" in (updated.error_message or "")


@pytest.mark.anyio
async def test_code_review_retry_refreshes_author_commit_email(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    job = (await service.poll_once())[0]
    service.store.fail_job(job_id=job.job_id, error_message="runtime failed")
    service.github_client.commits = (
        PullRequestCommit(author_login="author", author_email="updated-author@corp.test"),
    )

    retried = service.retry_job(job_id=job.job_id)

    assert retried.status == "pending"
    assert retried.notification_emails == "updated-author@corp.test"


@pytest.mark.anyio
async def test_code_review_email_job_can_retry_after_failure(tmp_path: Path) -> None:
    email_sender = _CapturingEmailSender(fail_first=True)
    service = _build_service(tmp_path, email_sender=email_sender)
    job = (await service.poll_once())[0]
    review = await service.run_job(job)
    assert review is not None
    email_job = service.store.list_retryable_email_jobs()[0]

    failed = service.run_email_job(email_job)
    assert failed is not None
    assert failed.status == "failed"
    assert failed.attempts == 1

    pending = service.retry_email_job(email_job_id=failed.email_job_id)
    sent = service.run_email_job(pending)

    assert sent is not None
    assert sent.status == "sent"
    assert sent.attempts == 2
    assert email_sender.messages[0]["to"] == "author@corp.test"
    assert email_sender.messages[0]["cc"] == ""
    assert "我是 ai-prd 的代码审查 agent" in email_sender.messages[0]["text"]
    assert "你好，author" in email_sender.messages[0]["text"]
    assert "你的 PR 已邀请审核人（@alice）" in email_sender.messages[0]["text"]
    assert "AI 自动审核意见" in email_sender.messages[0]["html"]
    assert '<h1 style="font-size:22px' in email_sender.messages[0]["html"]
    assert "缺少待激活状态" in email_sender.messages[0]["html"]


def test_code_review_email_styles_bullet_finding_title() -> None:
    html = render_markdown_for_email(
        "## 主要发现\n\n"
        "- **[高] SpotCalculator.get_depth_snapshot_data 字段顺序疑似对调**\n"
        "- **位置：** app/business/market_liquidity.py\n"
        "- **问题：** 字段映射可能错位。"
    )

    assert "list-style-type:none" in html
    assert "border-left:4px solid #b53333" in html
    assert "display:block;font-size:15px" in html
    assert '<strong>位置：</strong> app/business/market_liquidity.py' in html


@pytest.mark.anyio
async def test_code_review_email_job_sends_configured_cc_emails(tmp_path: Path) -> None:
    email_sender = _CapturingEmailSender()
    service = _build_service(
        tmp_path,
        email_sender=email_sender,
        config_payload=_config_payload(
            notification_cc_emails=[
                "observer@corp.test",
                "author@corp.test",
                "observer@corp.test",
            ],
        ),
    )
    job = (await service.poll_once())[0]
    review = await service.run_job(job)
    assert review is not None
    email_job = service.store.list_retryable_email_jobs()[0]

    sent = service.run_email_job(email_job)

    assert sent is not None
    assert sent.status == "sent"
    assert email_sender.messages[0]["to"] == "author@corp.test"
    assert email_sender.messages[0]["cc"] == "observer@corp.test"


@pytest.mark.anyio
async def test_code_review_email_job_creator_mode_skips_configured_cc_emails(tmp_path: Path) -> None:
    email_sender = _CapturingEmailSender()
    service = _build_service(
        tmp_path,
        email_sender=email_sender,
        config_payload=_config_payload(
            notification_recipient_mode="creator",
            notification_cc_emails=["observer@corp.test"],
        ),
    )
    job = (await service.poll_once())[0]
    review = await service.run_job(job)
    assert review is not None
    email_job = service.store.list_retryable_email_jobs()[0]

    sent = service.run_email_job(email_job)

    assert sent is not None
    assert sent.status == "sent"
    assert email_sender.messages[0]["to"] == "author@corp.test"
    assert email_sender.messages[0]["cc"] == ""


@pytest.mark.anyio
async def test_code_review_email_job_cc_mode_sends_only_configured_cc_emails(tmp_path: Path) -> None:
    email_sender = _CapturingEmailSender()
    service = _build_service(
        tmp_path,
        email_sender=email_sender,
        config_payload=_config_payload(
            notification_recipient_mode="cc",
            notification_cc_emails=[
                "observer@corp.test",
                "ops@corp.test",
                "observer@corp.test",
            ],
        ),
    )
    job = (await service.poll_once())[0]
    review = await service.run_job(job)
    assert review is not None

    sent = service.process_retryable_email_jobs()

    assert len(sent) == 2
    assert [message["to"] for message in email_sender.messages] == [
        "observer@corp.test",
        "ops@corp.test",
    ]
    assert all(message["cc"] == "" for message in email_sender.messages)
    assert "author@corp.test" not in [message["to"] for message in email_sender.messages]


def test_code_review_config_rejects_unknown_notification_recipient_mode(tmp_path: Path) -> None:
    service = _build_service(tmp_path, config_payload=_config_payload(notification_recipient_mode="invalid"))

    with pytest.raises(ValueError, match="notification_recipient_mode"):
        service.load_project_config()


@pytest.mark.anyio
async def test_code_review_email_job_sends_without_cc_when_config_disabled(tmp_path: Path) -> None:
    email_sender = _CapturingEmailSender()
    service = _build_service(
        tmp_path,
        email_sender=email_sender,
        config_payload=_config_payload(notification_cc_emails=["observer@corp.test"]),
    )
    job = (await service.poll_once())[0]
    review = await service.run_job(job)
    assert review is not None
    email_job = service.store.list_retryable_email_jobs()[0]
    config_path = tmp_path / "workspace/config/code-auto-review.json"
    config_path.write_text(json.dumps({"enabled": False, "repositories": []}), encoding="utf-8")

    sent = service.run_email_job(email_job)

    assert sent is not None
    assert sent.status == "sent"
    assert sent.error_message is None
    assert email_sender.messages[0]["to"] == "author@corp.test"
    assert email_sender.messages[0]["cc"] == ""


def test_code_review_paths_are_pr_scoped_and_workspace_relative(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    snapshot = service.github_client.snapshot
    repo_config = service.load_project_config().repositories[0]
    repository = service.context_preparer._repository(repo_config, "example-org/example_backend")
    worktree_path = service.worktree_manager.revision_path(
        repository=repository,
        resolved_sha=snapshot.head_sha,
    )
    context_dir = service.context_preparer.code_review_context_dir(
        repo_full_name="example-org/example_backend",
        pr_number=8784,
        head_sha=snapshot.head_sha,
    )

    assert service.context_preparer.workspace_display_path(worktree_path) == (
        f"runtime/code-review-worktrees/example-org/example_backend/revisions/{snapshot.head_sha}"
    )
    assert service.context_preparer.workspace_display_path(context_dir / "diff.patch") == (
        f"runtime/code-review-contexts/example-org/example_backend/PR_8784/{snapshot.head_sha}/diff.patch"
    )


def test_code_review_clickup_task_links_parse_supported_forms() -> None:
    assert extract_clickup_task_ids(
        "\n".join(
            [
                "https://app.clickup.com/t/86abc",
                "https://acme.clickup.com/t/86def",
                "https://acme.clickup.com/t/9000000001/86ghi",
                "https://app.clickup.com/t/86abc",
            ]
        )
    ) == ("86abc", "86def", "86ghi")


def test_code_review_clickup_task_ids_parse_loose_labels() -> None:
    assert extract_clickup_task_ids(
        "\n".join(
            [
                "Clickup: 86eybp7yz",
                "需求：86label01",
                "需求：clickup 86label02",
                "clickup 86label03",
                "需求:86label01",
            ]
        )
    ) == ("86eybp7yz", "86label01", "86label02", "86label03")


def test_code_review_clickup_task_ids_mix_links_and_labels_deduped() -> None:
    assert extract_clickup_task_ids(
        "\n".join(
            [
                "https://app.clickup.com/t/86abcde",
                "ClickUp: https://acme.clickup.com/t/9000000001/86url001",
                "需求：86abcde",
                "Clickup: 86fghij",
            ]
        )
    ) == ("86abcde", "86url001", "86fghij")


def test_code_review_clickup_task_ids_ignore_short_or_unlabeled_tokens() -> None:
    assert extract_clickup_task_ids("需求：86 以及无关的 12345 文本") == ()


def test_code_review_requirement_paths_skip_hidden_and_sort_newer(tmp_path: Path) -> None:
    service = _build_service(tmp_path, create_default_requirement=False)
    older = _write_requirement(tmp_path, "done/older__86abc.md")
    newer = _write_requirement(tmp_path, "in_progress/newer__86abc.md")
    hidden = _write_requirement(tmp_path, "_deleted/hidden__86abc.md")
    os.utime(older, (100, 100))
    os.utime(newer, (200, 200))
    os.utime(hidden, (300, 300))

    assert service.context_preparer.requirement_paths_for_clickup_task_ids(("86abc",)) == (
        "coinex/knowledge/requirements/tasks/in_progress/newer__86abc.md",
        "coinex/knowledge/requirements/tasks/done/older__86abc.md",
    )


@pytest.mark.anyio
async def test_code_review_rebuilds_worktree_when_head_changes(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    job = (await service.poll_once())[0]
    source_repo = tmp_path / "workspace/coinex/knowledge/code/example_backend"
    repo_config = service.load_project_config().repositories[0]
    repository = service.context_preparer._repository(repo_config, job.repo_full_name)
    worktree_path = service.worktree_manager.revision_path(
        repository=repository,
        resolved_sha=job.head_sha,
    )
    _git(source_repo, "worktree", "add", "--detach", str(worktree_path), job.base_sha)
    assert _git(worktree_path, "rev-parse", "HEAD") == job.base_sha

    review = await service.run_job(job)

    assert review is not None
    assert _git(worktree_path, "rev-parse", "HEAD") == job.head_sha


@pytest.mark.anyio
async def test_code_review_fetches_base_branch_before_preparing_context(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    source_repo = tmp_path / "workspace/coinex/knowledge/code/example_backend"
    assert not _git_succeeds(source_repo, "rev-parse", "refs/remotes/origin/main")
    fetched_branches: list[str] = []
    original_fetch_branch = service.worktree_manager.fetch_branch

    def record_fetch_branch(*, repository, branch: str) -> None:
        fetched_branches.append(branch)
        original_fetch_branch(repository=repository, branch=branch)

    service.context_preparer.worktree_manager.fetch_branch = record_fetch_branch  # type: ignore[method-assign]

    job = (await service.poll_once())[0]
    review = await service.run_job(job)

    assert review is not None
    assert fetched_branches == ["main"]
    assert _git(source_repo, "rev-parse", "refs/remotes/origin/main") == _git(source_repo, "rev-parse", "refs/heads/main")


@pytest.mark.anyio
async def test_code_review_fails_without_workspace_repo_path(tmp_path: Path) -> None:
    service = _build_service(tmp_path, config_payload=_config_payload(workspace_repo_path=""))
    job = (await service.poll_once())[0]

    review = await service.run_job(job)

    assert review is None
    failed = service.store.get_job(job.job_id)
    assert failed is not None
    assert failed.status == "failed"
    assert "workspace_repo_path 未配置" in (failed.error_message or "")
    assert "repo_full_name: example-org/example_backend" in (failed.error_message or "")
    assert f"base_sha: {job.base_sha}" in (failed.error_message or "")
    assert f"head_sha: {job.head_sha}" in (failed.error_message or "")
    assert "worktree_path:" in (failed.error_message or "")
    assert service.runtime_client.messages == []


@pytest.mark.anyio
async def test_code_review_failure_sends_alert_to_configured_cc_emails(tmp_path: Path) -> None:
    email_sender = _CapturingEmailSender()
    service = _build_service(
        tmp_path,
        email_sender=email_sender,
        config_payload=_config_payload(
            workspace_repo_path="",
            notification_cc_emails=[
                "observer@corp.test",
                "ops@corp.test",
                "observer@corp.test",
            ],
        ),
    )
    job = (await service.poll_once())[0]

    review = await service.run_job(job)

    assert review is None
    assert service.store.list_retryable_email_jobs() == []
    assert [message["to"] for message in email_sender.messages] == [
        "observer@corp.test",
        "ops@corp.test",
    ]
    assert all(message["cc"] == "" for message in email_sender.messages)
    assert all("[AI Code Review][告警]" in message["subject"] for message in email_sender.messages)
    assert "正常审核结果通知不会发送" in email_sender.messages[0]["text"]
    assert "workspace_repo_path 未配置" in email_sender.messages[0]["text"]
    assert "author@corp.test" not in [message["to"] for message in email_sender.messages]


def _build_service(
    tmp_path: Path,
    *,
    config_payload: dict[str, object] | None | object = _DEFAULT_CONFIG,
    email_sender: _CapturingEmailSender | None = None,
    runtime_client: object | None = None,
    snapshot: PullRequestSnapshot | None = None,
    commits: tuple[PullRequestCommit, ...] | None = None,
    github_cli_account: str = "",
    running_timeout_seconds: int = 3600,
    create_default_requirement: bool = True,
) -> CodeReviewService:
    skills_root = tmp_path / "workspace/.claude/skills"
    policy_file = tmp_path / "workspace/config/code-review-policy.md"
    policy_file.parent.mkdir(parents=True, exist_ok=True)
    policy_file.write_text(
        "# 代码审查测试规范\n\n每条 finding 必须使用三级标题作为问题标题。",
        encoding="utf-8",
    )
    if snapshot is None:
        snapshot = _create_repo_snapshot(tmp_path)
    if create_default_requirement:
        _write_requirement(tmp_path, "in_progress/coupon__86abc.md")
    config_path = ""
    if config_payload is _DEFAULT_CONFIG:
        config_payload = _config_payload()
    if isinstance(config_payload, dict):
        config_file = tmp_path / "workspace/config/code-auto-review.json"
        config_file.parent.mkdir(parents=True, exist_ok=True)
        config_file.write_text(json.dumps(config_payload, ensure_ascii=False), encoding="utf-8")
        config_path = "workspace/config/code-auto-review.json"

    return CodeReviewService(
        base_dir=tmp_path,
        requirements_root=tmp_path / "workspace/coinex/knowledge/requirements",
        skills_root=skills_root,
        store=CodeReviewStore(str(tmp_path / "assistant.sqlite3")),
        runtime_client=runtime_client or _CodeReviewRuntime(),
        github_client=_FakeGitHubPullRequestClient(
            snapshot=snapshot,
            events=_events(),
            commits=commits if commits is not None else _commits(),
        ),
        email_sender=email_sender or _CapturingEmailSender(),
        pr_link_service=RequirementPrLinkService(
            base_dir=tmp_path,
            requirements_root=tmp_path / "workspace/coinex/knowledge/requirements",
            store=RequirementPrLinkStore(str(tmp_path / "assistant.sqlite3")),
        ),
        config_path=config_path,
        github_cli_account=github_cli_account,
        running_timeout_seconds=running_timeout_seconds,
    )


def _config_payload(
    *,
    notification_recipient_mode: str | None = None,
    notification_cc_emails: list[str] | None = None,
    workspace_repo_path: str = "workspace/coinex/knowledge/code/example_backend",
    review_policy_path: str = "workspace/config/code-review-policy.md",
) -> dict[str, object]:
    repo_config: dict[str, object] = {
        "repo_full_name": "example-org/example_backend",
        "enabled": True,
        "github_access": "api",
        "workspace_repo_path": workspace_repo_path,
        "base_branches": ["main"],
        "review_policy_path": review_policy_path,
    }
    if notification_recipient_mode is not None:
        repo_config["notification_recipient_mode"] = notification_recipient_mode
    if notification_cc_emails is not None:
        repo_config["notification_cc_emails"] = notification_cc_emails
    return {
        "enabled": True,
        "poll_interval_seconds": 300,
        "repositories": [repo_config],
    }


def _create_repo_snapshot(tmp_path: Path) -> PullRequestSnapshot:
    repo = tmp_path / "workspace/coinex/knowledge/code/example_backend"
    (repo / "app/coupon").mkdir(parents=True)
    _git(repo, "init")
    _git(repo, "checkout", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "app/coupon/service.py").write_text("status = 'available'\n", encoding="utf-8")
    _git(repo, "add", "app/coupon/service.py")
    _git(repo, "commit", "-m", "base")
    base_sha = _git(repo, "rev-parse", "HEAD")
    (repo / "app/coupon/service.py").write_text("status = 'pending_activation'\n", encoding="utf-8")
    _git(repo, "add", "app/coupon/service.py")
    _git(repo, "commit", "-m", "head")
    head_sha = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/pull/1234/head", head_sha)
    _git(repo, "remote", "add", "origin", str(repo))
    return _snapshot(base_sha=base_sha, head_sha=head_sha, body="领取后增加待激活状态。\nhttps://app.clickup.com/t/86abc")


def _snapshot(
    *,
    base_sha: str = "base-sha",
    head_sha: str = "head-sha",
    body: str = "领取后增加待激活状态。",
) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        repo_full_name="example-org/example_backend",
        pr_number=1234,
        title="调整优惠券状态",
        body=body,
        base_ref="main",
        base_sha=base_sha,
        head_sha=head_sha,
        draft=False,
        files=(
            PullRequestFile(
                filename="app/coupon/service.py",
                status="modified",
                patch="@@\n- status = 'available'\n+ status = 'pending_activation'\n",
            ),
        ),
        state="open",
        author_login="author",
        html_url="https://github.com/example-org/example_backend/pull/1234",
        requested_reviewer_logins=("alice",),
    )


def _write_requirement(tmp_path: Path, relative_path: str) -> Path:
    path = tmp_path / "workspace/coinex/knowledge/requirements/tasks" / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# 需求\n\n本地需求内容。", encoding="utf-8")
    return path


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise AssertionError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout.strip()


def _git_succeeds(repo: Path, *args: str) -> bool:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _commits() -> tuple[PullRequestCommit, ...]:
    return (
        PullRequestCommit(author_login="author", author_email="author@corp.test"),
    )


def _events() -> tuple[PullRequestReviewRequestEvent, ...]:
    return (
        PullRequestReviewRequestEvent(
            event_id="event-1",
            actor_login="requester",
            reviewer_login="alice",
            team_slug="",
            created_at="2026-06-02T15:30:00Z",
        ),
    )
