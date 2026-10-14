from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.business.assistant.git_context import AssistantGitContextResolver, GitContextRequest
from app.business.assistant.service import AssistantService
from app.business.assistant.store import SQLiteAssistantStore
from app.business.business_doc_updates.service import (
    GitHubCliPullRequestClient,
    PullRequestFile,
    PullRequestSnapshot,
)
from app.business.code_reviews.review_context import CodeReviewContextPreparer
from app.business.git_context import GitRepositoryCatalog, GitWorktreeManager
from app.business.workspace.access import SQLiteWorkspaceAccessStore, WorkspaceAccessService
from app.integrations.agent_runtime.models import RuntimeEvent, RuntimeSession
from app.integrations.agent_runtime.mock_runtime import MockAgentRuntimeClient
from app.services.auth_models import UserRecord


class _GitHubClient:
    def __init__(self, snapshot: PullRequestSnapshot) -> None:
        self.snapshot = snapshot
        self.calls = 0
        self.error: Exception | None = None

    def fetch_pull_request(self, *, repo_full_name: str, pr_number: int) -> PullRequestSnapshot:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert repo_full_name == self.snapshot.repo_full_name
        assert pr_number == self.snapshot.pr_number
        return self.snapshot


class _FailingGitContextResolver:
    calls = 0

    def prepare(self, **_kwargs):
        self.calls += 1
        raise RuntimeError("github unavailable")


def test_follow_uses_immutable_revision_and_reports_version_update(tmp_path: Path) -> None:
    repository_path, base_sha, head_sha = _create_repository(tmp_path)
    snapshot = _snapshot(base_sha=base_sha, head_sha=head_sha)
    github = _GitHubClient(snapshot)
    resolver, store, session, manager = _build_resolver(
        tmp_path,
        repository_path=repository_path,
        github=github,
        default=snapshot,
    )

    first = resolver.prepare(user=_user(), session=session, explicit_requests=[])
    first_worktree = Path(first.mounts[0].host_path)
    assert first_worktree.name == head_sha
    assert _git(first_worktree, "rev-parse", "HEAD") == head_sha
    user_message, first_turn = store.start_turn(
        session.session_id,
        session.user_id,
        "分析 PR",
        git_context_requests=first.requests,
        git_context_snapshots=first.snapshots,
    )
    store.fail_turn(first_turn.turn_id, "runtime failed")
    trace_service = AssistantService(
        store=store,
        runtime_client=SimpleNamespace(provider="mock"),
        system_prompt="",
        runtime_working_directory=str(tmp_path),
    )
    trace = trace_service.get_turn_trace(_user(), session.session_id, first_turn.turn_id)
    assert trace is not None
    assert trace.git_scopes[0].resolved_sha == head_sha

    next_sha = _commit_and_push(repository_path, "value = 2\n")
    github.snapshot = replace(snapshot, head_sha=next_sha)
    second = resolver.prepare(user=_user(), session=session, explicit_requests=[])

    assert Path(second.mounts[0].host_path).name == next_sha
    assert _git(first_worktree, "rev-parse", "HEAD") == head_sha
    assert len(second.update_messages) == 1
    assert head_sha in second.update_messages[0]
    assert next_sha in second.update_messages[0]
    assert manager.revision_path(
        repository=resolver.catalog.get(snapshot.repo_full_name),
        resolved_sha=head_sha,
    ).is_dir()
    assert user_message.content == "分析 PR"


def test_follow_remote_failure_does_not_fall_back_to_old_sha(tmp_path: Path) -> None:
    repository_path, base_sha, head_sha = _create_repository(tmp_path)
    github = _GitHubClient(_snapshot(base_sha=base_sha, head_sha=head_sha))
    resolver, store, session, _manager = _build_resolver(
        tmp_path,
        repository_path=repository_path,
        github=github,
        default=github.snapshot,
    )
    github.error = RuntimeError("github unavailable")

    with pytest.raises(RuntimeError, match="github unavailable"):
        resolver.prepare(user=_user(), session=session, explicit_requests=[])

    assert store.list_turns(session.session_id, session.user_id) == []


@pytest.mark.anyio
async def test_new_turn_starts_baseline_without_preparing_session_git_default(tmp_path: Path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    resolver = _FailingGitContextResolver()
    service = AssistantService(
        store=store,
        runtime_client=MockAgentRuntimeClient(),
        system_prompt="",
        runtime_working_directory=str(tmp_path),
        git_context_resolver=resolver,
    )
    session = service.create_session(_user(), organization_key="coinex")

    result = await service.chat(_user(), session.session_id, "分析 PR")

    assert result.turn.status == "completed"
    assert resolver.calls == 0
    assert store.list_turn_git_context_snapshots(result.turn.turn_id) == []


class _MountingRuntime:
    provider = "mock"

    async def create_or_resume_session(self, **kwargs) -> RuntimeSession:
        self.dynamic_tools = kwargs["dynamic_tools"]
        self.session = RuntimeSession(
            provider=self.provider,
            session_id=kwargs.get("runtime_session_id") or "runtime-1",
            working_directory=kwargs["workspace_plan"].host_shadow_root,
            system_prompt=kwargs["system_prompt"],
            workspace_plan=kwargs["workspace_plan"],
            dynamic_tools=self.dynamic_tools,
            dynamic_read_roots=kwargs["dynamic_read_roots"],
        )
        return self.session

    async def send_message_stream(self, *, session, message, metadata):
        del message, metadata
        first = await self.dynamic_tools[0].handler(
            {
                "repository": "example_backend",
                "reference_type": "pull_request",
                "reference": "1234",
            }
        )
        assert first.is_error is False
        second = await self.dynamic_tools[0].handler(
            {
                "repository": "example-org/example_backend",
                "reference_type": "branch",
                "reference": "main",
            }
        )
        assert second.is_error is False
        third = await self.dynamic_tools[0].handler(
            {
                "repository": "coinex_comment",
                "reference_type": "branch",
                "reference": "main",
            }
        )
        assert third.is_error is False
        assert session.workspace_plan is not None
        yield RuntimeEvent("delta", {"text": "已读取精确代码"})
        yield RuntimeEvent("message", {"content": "已读取精确代码"})
        yield RuntimeEvent("complete", {"result": "已读取精确代码"})


@pytest.mark.anyio
async def test_agent_mount_updates_running_turn_snapshot_and_scope_event(tmp_path: Path) -> None:
    repository_path, base_sha, head_sha = _create_repository(tmp_path)
    github = _GitHubClient(_snapshot(base_sha=base_sha, head_sha=head_sha))
    resolver, store, session, _manager = _build_resolver(
        tmp_path,
        repository_path=repository_path,
        github=github,
        default=github.snapshot,
    )
    config_path = tmp_path / "workspace/config/code-auto-review.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["repositories"].append(
        {
            "repo_full_name": "coinex/coinex_comment",
            "enabled": True,
            "github_access": "api",
            "workspace_repo_path": str(repository_path),
            "organization_key": "coinex",
            "origin": "origin",
            "default_branch": "main",
        }
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")
    workspace_store = SQLiteWorkspaceAccessStore(
        str(tmp_path / "workspace/runtime/db/workspace-access.sqlite3"),
        base_dir=tmp_path,
    )
    workspace_store.upsert_user_membership(
        user_id=_user().user_id,
        organization_key="coinex",
        source="test",
        is_default=True,
    )
    service = AssistantService(
        store=store,
        runtime_client=_MountingRuntime(),
        system_prompt="",
        runtime_working_directory=str(tmp_path),
        git_context_resolver=resolver,
        workspace_access=WorkspaceAccessService(store=workspace_store, base_dir=tmp_path),
    )

    events = [
        event
        async for event in service.stream_chat(
            _user(),
            session.session_id,
            "检查这个改动",
        )
    ]

    turn_start = next(event for event in events if event["type"] == "turn_start")
    scope_events = [event for event in events if event["type"] == "git_scope"]
    assert turn_start["git_scopes"] == []
    assert len(scope_events) == 3
    assert scope_events[0]["git_scopes"][0]["selector_type"] == "pull_request"
    assert scope_events[1]["git_scopes"][0]["selector_type"] == "branch"
    final_scopes = {
        item["repo_full_name"]: item
        for item in scope_events[-1]["git_scopes"]
    }
    assert set(final_scopes) == {
        "example-org/example_backend",
        "coinex/coinex_comment",
    }
    assert final_scopes["example-org/example_backend"]["selector_value"] == "main"
    assert final_scopes["example-org/example_backend"]["resolved_sha"] == head_sha
    assert final_scopes["coinex/coinex_comment"]["selector_value"] == "main"
    assert final_scopes["coinex/coinex_comment"]["resolved_sha"] == head_sha

    turns = store.list_turns(session.session_id, _user().user_id)
    snapshots = store.list_turn_git_context_snapshots(turns[0].turn_id)
    requests = store.list_turn_git_context_requests(turns[0].turn_id)
    assert len(snapshots) == 2
    assert all(snapshot.selector_type == "branch" for snapshot in snapshots)
    assert all(snapshot.selector_value == "main" for snapshot in snapshots)
    assert len(requests) == 3
    assert all(request.authorization_source == "agent_tool" for request in requests)
    mounted_code = Path(service.runtime_client.session.working_directory) / "repos/example_backend/code"
    mounted_comment = Path(service.runtime_client.session.working_directory) / "repos/coinex_comment/code"
    assert mounted_code.resolve().name == head_sha
    assert mounted_comment.resolve().name == head_sha


def test_failed_turn_rerun_is_pinned_without_remote_check(tmp_path: Path) -> None:
    repository_path, base_sha, head_sha = _create_repository(tmp_path)
    github = _GitHubClient(_snapshot(base_sha=base_sha, head_sha=head_sha))
    resolver, store, session, _manager = _build_resolver(
        tmp_path,
        repository_path=repository_path,
        github=github,
        default=github.snapshot,
    )
    first = resolver.prepare(user=_user(), session=session, explicit_requests=[])
    _message, turn = store.start_turn(
        session.session_id,
        session.user_id,
        "分析 PR",
        git_context_snapshots=first.snapshots,
    )
    store.fail_turn(turn.turn_id, "failed")
    original = store.list_turn_git_context_snapshots(turn.turn_id)
    calls_before_rerun = github.calls
    github.error = RuntimeError("must not be called")

    rerun = resolver.prepare(
        user=_user(),
        session=session,
        explicit_requests=[],
        rerun_snapshots=original,
    )

    assert github.calls == calls_before_rerun
    assert rerun.snapshots[0]["strategy"] == "pinned"
    assert rerun.snapshots[0]["resolved_sha"] == head_sha


def test_baseline_does_not_create_revision_worktree(tmp_path: Path) -> None:
    repository_path, base_sha, head_sha = _create_repository(tmp_path)
    github = _GitHubClient(_snapshot(base_sha=base_sha, head_sha=head_sha))
    resolver, _store, session, manager = _build_resolver(
        tmp_path,
        repository_path=repository_path,
        github=github,
        default=None,
    )

    prepared = resolver.prepare(
        user=_user(),
        session=session,
        explicit_requests=[
            GitContextRequest(
                repo_full_name="example-org/example_backend",
                strategy="baseline",
            )
        ],
    )

    assert prepared.mounts == []
    assert prepared.snapshots[0]["logical_code_path"] == "/coinex/knowledge/code/example_backend"
    assert not manager.root.joinpath("example-org/example_backend/revisions").exists()
    assert github.calls == 0


def test_turn_scope_contains_only_the_selected_repositories(tmp_path: Path) -> None:
    repository_path, base_sha, head_sha = _create_repository(tmp_path)
    github = _GitHubClient(_snapshot(base_sha=base_sha, head_sha=head_sha))
    resolver, _store, session, _manager = _build_resolver(
        tmp_path,
        repository_path=repository_path,
        github=github,
        default=None,
    )
    config_path = tmp_path / "workspace/config/code-auto-review.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["repositories"].extend(
        [
            {
                "repo_full_name": "coinex/coinex_comment",
                "enabled": True,
                "github_access": "api",
                "workspace_repo_path": str(repository_path),
                "organization_key": "coinex",
                "origin": "origin",
                "default_branch": "master",
            },
            {
                "repo_full_name": "coinex/coinex_frontend",
                "enabled": True,
                "github_access": "api",
                "workspace_repo_path": str(repository_path),
                "organization_key": "coinex",
                "origin": "origin",
                "default_branch": "main",
            },
        ]
    )
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    prepared = resolver.prepare(
        user=_user(),
        session=session,
        explicit_requests=[
            GitContextRequest(repo_full_name="example-org/example_backend", strategy="baseline"),
            GitContextRequest(repo_full_name="coinex/coinex_comment", strategy="baseline"),
        ],
    )

    assert [snapshot["repo_full_name"] for snapshot in prepared.snapshots] == [
        "example-org/example_backend",
        "coinex/coinex_comment",
    ]
    assert all(snapshot["strategy"] == "baseline" for snapshot in prepared.snapshots)
    assert all(snapshot["source"] == "baseline" for snapshot in prepared.snapshots)
    assert prepared.mounts == []
    assert github.calls == 0


def test_client_cannot_supply_git_context() -> None:
    from pydantic import ValidationError

    from app.schemas.assistant import AssistantChatRequest

    with pytest.raises(ValidationError, match="git_contexts"):
        AssistantChatRequest.model_validate(
            {
                "message": "检查 PR",
                "git_contexts": [
                    {
                        "repo_full_name": "example-org/example_backend",
                        "strategy": "follow",
                        "selector_type": "pull_request",
                        "selector_value": "1234",
                    }
                ],
            }
        )


def test_pull_request_session_persists_atomic_follow_default(tmp_path: Path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=SimpleNamespace(provider="mock"),
        system_prompt="",
        runtime_working_directory=str(tmp_path),
    )
    sha = "a" * 40

    session = service.create_pull_request_session(
        _user(),
        task_id="86task",
        repo_full_name="example-org/example_backend",
        pr_number=1234,
        resolved_sha=sha,
        organization_key="coinex",
    )

    defaults = store.get_session_git_context_defaults(session.session_id, _user().user_id)
    assert session.git_context_defaults[0].selector_value == "1234"
    assert len(defaults) == 1
    assert defaults[0].default_strategy == "follow"
    assert defaults[0].selector_type == "pull_request"
    assert defaults[0].selector_value == "1234"
    assert defaults[0].last_resolved_sha == sha
    assert defaults[0].authorization_source == "task_pr"
    assert defaults[0].authorization_key == "86task"


def test_pull_request_session_can_defer_sha_resolution_until_first_turn(tmp_path: Path) -> None:
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    service = AssistantService(
        store=store,
        runtime_client=SimpleNamespace(provider="mock"),
        system_prompt="",
        runtime_working_directory=str(tmp_path),
    )

    session = service.create_pull_request_session(
        _user(),
        task_id="86task",
        repo_full_name="example-org/example_backend",
        pr_number=1234,
        resolved_sha=None,
        organization_key="coinex",
    )

    defaults = store.get_session_git_context_defaults(session.session_id, _user().user_id)
    assert len(defaults) == 1
    assert defaults[0].default_strategy == "follow"
    assert defaults[0].last_resolved_sha is None
    assert defaults[0].last_checked_at is None


def test_concurrent_revision_prepare_creates_one_worktree(tmp_path: Path) -> None:
    repository_path, _base_sha, head_sha = _create_repository(tmp_path)
    github = _GitHubClient(_snapshot(base_sha=head_sha, head_sha=head_sha))
    resolver, _store, _session, manager = _build_resolver(
        tmp_path,
        repository_path=repository_path,
        github=github,
        default=None,
    )
    repository = resolver.catalog.get("example-org/example_backend")

    with ThreadPoolExecutor(max_workers=4) as executor:
        prepared = list(
            executor.map(
                lambda _index: manager.ensure_revision(
                    repository=repository,
                    resolved_sha=head_sha,
                    fetch=False,
                ),
                range(4),
            )
        )

    assert len({item.path for item in prepared}) == 1
    registered = _git(repository_path, "worktree", "list", "--porcelain")
    assert registered.count(str(prepared[0].path)) == 1


def test_repository_catalog_passes_configured_account_to_github_cli(tmp_path: Path) -> None:
    repository_path, base_sha, head_sha = _create_repository(tmp_path)
    config_path = tmp_path / "workspace/config/code-auto-review.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "repo_full_name": "example-org/example_backend",
                        "enabled": True,
                        "github_access": "gh_cli",
                        "workspace_repo_path": str(repository_path),
                        "organization_key": "coinex",
                        "origin": "origin",
                        "default_branch": "main",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    catalog = GitRepositoryCatalog(
        base_dir=tmp_path,
        config_path=str(config_path),
        github_client=_GitHubClient(_snapshot(base_sha=base_sha, head_sha=head_sha)),
        github_cli_account="jordan-lee",
    )

    client = catalog.github_client_for(catalog.get("example-org/example_backend"))

    assert isinstance(client, GitHubCliPullRequestClient)
    assert client.account == "jordan-lee"


def _build_resolver(
    tmp_path: Path,
    *,
    repository_path: Path,
    github: _GitHubClient,
    default: PullRequestSnapshot | None,
):
    config_path = tmp_path / "workspace/config/code-auto-review.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "repositories": [
                    {
                        "repo_full_name": "example-org/example_backend",
                        "enabled": True,
                        "github_access": "api",
                        "workspace_repo_path": str(repository_path),
                        "organization_key": "coinex",
                        "origin": "origin",
                        "default_branch": "main",
                        "base_branches": ["main"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    store = SQLiteAssistantStore(str(tmp_path / "assistant.sqlite3"))
    session = store.create_session(
        _user().user_id,
        "Git Context",
        active_organization_key="coinex",
        git_context_default=(
            {
                "repo_full_name": default.repo_full_name,
                "default_strategy": "follow",
                "selector_type": "pull_request",
                "selector_value": str(default.pr_number),
                "last_resolved_sha": default.head_sha,
                "logical_path": "/repos/example_backend/code",
                "authorization_source": "task_pr",
                "authorization_key": "86task",
                "last_checked_at": None,
            }
            if default is not None
            else None
        ),
    )
    manager = GitWorktreeManager(workspace_root=tmp_path / "workspace")
    preparer = CodeReviewContextPreparer(
        base_dir=tmp_path,
        workspace_root=tmp_path / "workspace",
        requirements_root=tmp_path / "workspace/coinex/knowledge/requirements",
        worktree_manager=manager,
    )
    catalog = GitRepositoryCatalog(
        base_dir=tmp_path,
        config_path=str(config_path),
        github_client=github,
        github_cli_account="jordan-lee",
    )
    resolver = AssistantGitContextResolver(
        store=store,
        catalog=catalog,
        worktree_manager=manager,
        pr_context_preparer=preparer,
        workspace_root=tmp_path / "workspace",
    )
    return resolver, store, session, manager


def _create_repository(tmp_path: Path) -> tuple[Path, str, str]:
    bare = tmp_path / "remote.git"
    repository = tmp_path / "workspace/coinex/knowledge/code/example_backend"
    repository.mkdir(parents=True)
    _git(bare.parent, "init", "--bare", str(bare))
    _git(repository, "init")
    _git(repository, "checkout", "-b", "main")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    (repository / "service.py").write_text("value = 0\n", encoding="utf-8")
    _git(repository, "add", "service.py")
    _git(repository, "commit", "-m", "base")
    base_sha = _git(repository, "rev-parse", "HEAD")
    (repository / "service.py").write_text("value = 1\n", encoding="utf-8")
    _git(repository, "add", "service.py")
    _git(repository, "commit", "-m", "head")
    head_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "remote", "add", "origin", str(bare))
    _git(repository, "push", "-u", "origin", "main")
    _git(repository, "push", "origin", f"{head_sha}:refs/pull/1234/head")
    return repository, base_sha, head_sha


def _commit_and_push(repository: Path, content: str) -> str:
    (repository / "service.py").write_text(content, encoding="utf-8")
    _git(repository, "add", "service.py")
    _git(repository, "commit", "-m", "update")
    _git(repository, "push", "origin", "main")
    head_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "push", "--force", "origin", f"{head_sha}:refs/pull/1234/head")
    return head_sha


def _snapshot(*, base_sha: str, head_sha: str) -> PullRequestSnapshot:
    return PullRequestSnapshot(
        repo_full_name="example-org/example_backend",
        pr_number=1234,
        title="Update service",
        body="https://app.clickup.com/t/86task",
        base_ref="main",
        base_sha=base_sha,
        head_sha=head_sha,
        draft=False,
        files=(PullRequestFile(filename="service.py", status="modified", patch=""),),
        author_login="author",
        html_url="https://github.com/example-org/example_backend/pull/1234",
    )


def _user() -> UserRecord:
    now = datetime.now(timezone.utc)
    return UserRecord(
        user_id="user-1",
        email="user@corp.test",
        name="User",
        avatar_url=None,
        status="active",
        role="member",
        default_role="member",
        agent_access="active",
        auth_provider="email",
        hosted_domain="corp.test",
        email_verified=True,
        department_name=None,
        access_group=None,
        created_at=now,
        first_login_at=now,
        last_login_at=now,
    )


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError((result.stderr or result.stdout).strip())
    return result.stdout.strip()
