from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.business.assistant.models import (
    AssistantSessionRecord,
    TurnGitContextSnapshotRecord,
)
from app.business.assistant.store import SQLiteAssistantStore
from app.business.code_reviews.review_context import (
    CodeReviewContextPreparer,
    CodeReviewPreparedContext,
)
from app.business.git_context import GitRepositoryCatalog, GitWorktreeManager
from app.integrations.agent_runtime.models import RuntimeWorkspaceMount
from app.services.auth_models import UserRecord


@dataclass(frozen=True)
class GitContextRequest:
    repo_full_name: str
    strategy: str
    selector_type: str | None = None
    selector_value: str | None = None
    authorization_source: str = "agent_tool"


@dataclass(frozen=True)
class PreparedTurnGitContexts:
    requests: list[dict[str, object]]
    snapshots: list[dict[str, object]]
    mounts: list[RuntimeWorkspaceMount]
    update_messages: list[str]


@dataclass(frozen=True)
class _PullRequestJob:
    repo_full_name: str
    pr_number: int
    pr_url: str
    base_ref: str
    base_sha: str
    head_sha: str


class AssistantGitContextResolver:
    def __init__(
        self,
        *,
        store: SQLiteAssistantStore,
        catalog: GitRepositoryCatalog,
        worktree_manager: GitWorktreeManager,
        pr_context_preparer: CodeReviewContextPreparer,
        workspace_root: Path,
    ) -> None:
        self.store = store
        self.catalog = catalog
        self.worktree_manager = worktree_manager
        self.pr_context_preparer = pr_context_preparer
        self.workspace_root = workspace_root.resolve()

    def prepare(
        self,
        *,
        user: UserRecord,
        session: AssistantSessionRecord,
        explicit_requests: list[GitContextRequest],
        rerun_snapshots: list[TurnGitContextSnapshotRecord] | None = None,
        apply_session_defaults: bool = True,
    ) -> PreparedTurnGitContexts:
        if rerun_snapshots is not None:
            return self._prepare_rerun(session=session, snapshots=rerun_snapshots)

        defaults = (
            {
                item.repo_full_name: item
                for item in self.store.get_session_git_context_defaults(session.session_id, user.user_id)
            }
            if apply_session_defaults
            else {}
        )
        requested_by_repo: dict[str, GitContextRequest] = {}
        for request in explicit_requests:
            if request.repo_full_name in requested_by_repo:
                raise ValueError("同一 Turn 不能重复指定同一仓库的 Git Context。")
            requested_by_repo[request.repo_full_name] = request

        repo_names = list(dict.fromkeys([*defaults.keys(), *requested_by_repo.keys()]))
        prepared_requests: list[dict[str, object]] = []
        prepared_snapshots: list[dict[str, object]] = []
        mounts: list[RuntimeWorkspaceMount] = []
        update_messages: list[str] = []
        for repo_full_name in repo_names:
            explicit = requested_by_repo.get(repo_full_name)
            default = defaults.get(repo_full_name)
            if explicit is not None:
                strategy = explicit.strategy
                selector_type = explicit.selector_type
                selector_value = explicit.selector_value
                source = "turn_explicit"
                authorization_source = explicit.authorization_source
                authorization_key = session.session_id
                prepared_requests.append(
                    {
                        "repo_full_name": repo_full_name,
                        "strategy": strategy,
                        "selector_type": selector_type,
                        "selector_value": selector_value,
                        "authorization_source": authorization_source,
                        "authorization_key": authorization_key,
                    }
                )
            elif default is not None:
                strategy = default.default_strategy
                selector_type = default.selector_type
                selector_value = default.selector_value
                source = "session_default"
                authorization_source = default.authorization_source
                authorization_key = default.authorization_key
            else:
                continue

            repository = self.catalog.get(repo_full_name)
            self._ensure_repository_organization(session=session, repository_organization=repository.organization_key)
            if strategy == "baseline":
                snapshot = {
                    "repo_full_name": repo_full_name,
                    "strategy": "baseline",
                    "selector_type": None,
                    "selector_value": None,
                    "resolved_sha": None,
                    "logical_code_path": f"/{repository.organization_key}/knowledge/code/{repository.repo_name}",
                    "logical_context_path": None,
                    "source": "baseline",
                }
                prepared_snapshots.append(snapshot)
                continue

            if strategy == "follow":
                prepared, context = self._prepare_follow(
                    repository=repository,
                    selector_type=selector_type,
                    selector_value=selector_value,
                )
            elif strategy == "pinned":
                if default is None or explicit is not None or not default.last_resolved_sha:
                    raise ValueError("pinned 只能使用服务端已验证的 Session 或历史 Turn revision。")
                prepared = self.worktree_manager.ensure_revision(
                    repository=repository,
                    resolved_sha=default.last_resolved_sha,
                    fetch=False,
                )
                context = self._existing_pr_context(
                    repo_full_name=repo_full_name,
                    selector_type=selector_type,
                    selector_value=selector_value,
                    resolved_sha=prepared.resolved_sha,
                )
            else:
                raise ValueError("Git Context strategy 只能是 baseline、follow 或 pinned。")

            logical_code_path = f"/repos/{repository.repo_name}/code"
            logical_context_path = f"/repos/{repository.repo_name}/context" if context else None
            prepared_snapshots.append(
                {
                    "repo_full_name": repo_full_name,
                    "strategy": strategy,
                    "selector_type": selector_type,
                    "selector_value": selector_value,
                    "resolved_sha": prepared.resolved_sha,
                    "logical_code_path": logical_code_path,
                    "logical_context_path": logical_context_path,
                    "source": source,
                }
            )
            mounts.append(
                RuntimeWorkspaceMount(
                    workspace_id=f"git:{repo_full_name}:{prepared.resolved_sha}:code",
                    workspace_key=f"git:{repo_full_name}:code",
                    host_path=str(prepared.path),
                    sandbox_path=logical_code_path,
                    permission="read",
                )
            )
            if context is not None:
                mounts.append(
                    RuntimeWorkspaceMount(
                        workspace_id=f"git:{repo_full_name}:{prepared.resolved_sha}:context",
                        workspace_key=f"git:{repo_full_name}:context",
                        host_path=str(self.workspace_root / context.context_dir_path),
                        sandbox_path=logical_context_path or "",
                        permission="read",
                    )
                )
            previous = self.store.get_previous_git_context_snapshot(
                session_id=session.session_id,
                repo_full_name=repo_full_name,
                selector_type=selector_type or "",
                selector_value=selector_value or "",
            )
            if previous is not None and previous.resolved_sha != prepared.resolved_sha:
                update_messages.append(
                    self._version_update_message(
                        repo_full_name=repo_full_name,
                        selector_type=selector_type or "",
                        selector_value=selector_value or "",
                        old_sha=previous.resolved_sha or "",
                        new_sha=prepared.resolved_sha,
                    )
                )
        return PreparedTurnGitContexts(
            requests=prepared_requests,
            snapshots=prepared_snapshots,
            mounts=mounts,
            update_messages=update_messages,
        )

    def _prepare_follow(self, *, repository, selector_type: str | None, selector_value: str | None):
        if selector_type == "pull_request":
            try:
                pr_number = int(selector_value or "")
            except ValueError as exc:
                raise ValueError("PR number 必须为正整数。") from exc
            if pr_number <= 0:
                raise ValueError("PR number 必须为正整数。")
            snapshot = self.catalog.github_client_for(repository).fetch_pull_request(
                repo_full_name=repository.repo_full_name,
                pr_number=pr_number,
            )
            job = _PullRequestJob(
                repo_full_name=repository.repo_full_name,
                pr_number=pr_number,
                pr_url=snapshot.html_url,
                base_ref=snapshot.base_ref,
                base_sha=snapshot.base_sha,
                head_sha=snapshot.head_sha,
            )
            context = self.pr_context_preparer.prepare(
                job=job,
                snapshot=snapshot,
                repo_config=_RepositoryConfigAdapter(repository),
            )
            prepared = self.worktree_manager.ensure_revision(
                repository=repository,
                resolved_sha=snapshot.head_sha,
                fetch=False,
            )
            return prepared, context
        if selector_type == "branch":
            sha = self.worktree_manager.resolve_branch_tip(
                repository=repository,
                branch=selector_value or "",
            )
            return (
                self.worktree_manager.ensure_revision(
                    repository=repository,
                    resolved_sha=sha,
                    fetch=False,
                ),
                None,
            )
        raise ValueError("follow 必须指定 pull_request 或 branch selector。")

    def _prepare_rerun(
        self,
        *,
        session: AssistantSessionRecord,
        snapshots: list[TurnGitContextSnapshotRecord],
    ) -> PreparedTurnGitContexts:
        prepared_snapshots: list[dict[str, object]] = []
        mounts: list[RuntimeWorkspaceMount] = []
        for snapshot in snapshots:
            repository = self.catalog.get(snapshot.repo_full_name)
            self._ensure_repository_organization(session=session, repository_organization=repository.organization_key)
            if snapshot.strategy == "baseline":
                prepared_snapshots.append(
                    {
                        "repo_full_name": snapshot.repo_full_name,
                        "strategy": "baseline",
                        "selector_type": None,
                        "selector_value": None,
                        "resolved_sha": snapshot.resolved_sha,
                        "logical_code_path": snapshot.logical_code_path,
                        "logical_context_path": None,
                        "source": "rerun_snapshot",
                    }
                )
                continue
            if not snapshot.resolved_sha:
                raise ValueError("原 Turn 缺少 resolved_sha，无法固定版本重跑。")
            prepared = self.worktree_manager.ensure_revision(
                repository=repository,
                resolved_sha=snapshot.resolved_sha,
                fetch=False,
            )
            context_host = None
            if snapshot.logical_context_path:
                context = self._existing_pr_context(
                    repo_full_name=snapshot.repo_full_name,
                    selector_type=snapshot.selector_type,
                    selector_value=snapshot.selector_value,
                    resolved_sha=snapshot.resolved_sha,
                )
                if context is None:
                    raise FileNotFoundError("原 Turn 的 PR Context 不存在，无法固定版本重跑。")
                context_host = self.workspace_root / context.context_dir_path
            prepared_snapshots.append(
                {
                    "repo_full_name": snapshot.repo_full_name,
                    "strategy": "pinned",
                    "selector_type": snapshot.selector_type,
                    "selector_value": snapshot.selector_value,
                    "resolved_sha": snapshot.resolved_sha,
                    "logical_code_path": snapshot.logical_code_path,
                    "logical_context_path": snapshot.logical_context_path,
                    "source": "rerun_snapshot",
                }
            )
            mounts.append(
                RuntimeWorkspaceMount(
                    workspace_id=f"git:{snapshot.repo_full_name}:{prepared.resolved_sha}:code",
                    workspace_key=f"git:{snapshot.repo_full_name}:code",
                    host_path=str(prepared.path),
                    sandbox_path=snapshot.logical_code_path,
                    permission="read",
                )
            )
            if context_host is not None and snapshot.logical_context_path:
                mounts.append(
                    RuntimeWorkspaceMount(
                        workspace_id=f"git:{snapshot.repo_full_name}:{prepared.resolved_sha}:context",
                        workspace_key=f"git:{snapshot.repo_full_name}:context",
                        host_path=str(context_host),
                        sandbox_path=snapshot.logical_context_path,
                        permission="read",
                    )
                )
        return PreparedTurnGitContexts(
            requests=[],
            snapshots=prepared_snapshots,
            mounts=mounts,
            update_messages=[],
        )

    def _existing_pr_context(
        self,
        *,
        repo_full_name: str,
        selector_type: str | None,
        selector_value: str | None,
        resolved_sha: str,
    ) -> CodeReviewPreparedContext | None:
        if selector_type != "pull_request":
            return None
        context_dir = self.pr_context_preparer.code_review_context_dir(
            repo_full_name=repo_full_name,
            pr_number=int(selector_value or "0"),
            head_sha=resolved_sha,
        )
        if not context_dir.is_dir():
            return None
        return CodeReviewPreparedContext(
            repo_full_name=repo_full_name,
            pr_number=int(selector_value or "0"),
            pr_url="",
            base_ref="",
            base_sha="",
            head_sha=resolved_sha,
            pull_request_path=self.pr_context_preparer.workspace_display_path(context_dir / "pull-request.md"),
            diff_path=self.pr_context_preparer.workspace_display_path(context_dir / "diff.patch"),
            changed_files_path=self.pr_context_preparer.workspace_display_path(context_dir / "changed-files.txt"),
            requirement_paths=(),
            worktree_path="",
            context_dir_path=self.pr_context_preparer.workspace_display_path(context_dir),
        )

    def _ensure_repository_organization(
        self,
        *,
        session: AssistantSessionRecord,
        repository_organization: str,
    ) -> None:
        if session.active_organization_key != repository_organization:
            raise ValueError("仓库不属于当前 Session 绑定的组织。")

    @staticmethod
    def _version_update_message(
        *,
        repo_full_name: str,
        selector_type: str,
        selector_value: str,
        old_sha: str,
        new_sha: str,
    ) -> str:
        selector = f"PR #{selector_value}" if selector_type == "pull_request" else f"分支 {selector_value}"
        return "\n".join(
            [
                "代码上下文已更新：",
                f"- {repo_full_name} {selector}",
                f"- 原版本：{old_sha}",
                f"- 当前版本：{new_sha}",
                "",
                "此前会话结论可能基于旧版本。本轮必须重新读取相关代码和 diff 后再回答。",
            ]
        )


class _RepositoryConfigAdapter:
    def __init__(self, repository) -> None:
        self.repo_full_name = repository.repo_full_name
        self.workspace_repo_path = str(repository.workspace_repo_path)
        self.organization_key = repository.organization_key
        self.origin = repository.origin
        self.default_branch = repository.default_branch
        self.github_access = repository.github_access
