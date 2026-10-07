from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from app.business.git_context.models import GitRepository
from app.business.git_context.worktree_manager import GitWorktreeManager
from app.utils.clickup.task_link import extract_clickup_task_ids


FINDING_HEADING_INSTRUCTION = (
    "主要发现中的每条 finding 必须使用三级标题作为问题标题，格式为 "
    "`### P1 — 问题标题`、`### P2 — 问题标题` 等；不要把问题标题写成普通列表项。"
)
CODE_REVIEW_CONTEXT_ROOT = Path("runtime/code-review-contexts")


class ReviewContextJob(Protocol):
    repo_full_name: str
    pr_number: int
    pr_url: str
    base_ref: str
    base_sha: str
    head_sha: str


class ReviewContextRepositoryConfig(Protocol):
    repo_full_name: str
    workspace_repo_path: str
    organization_key: str
    origin: str
    default_branch: str
    github_access: str


class ReviewContextSnapshot(Protocol):
    body: str


@dataclass(frozen=True)
class CodeReviewPreparedContext:
    repo_full_name: str
    pr_number: int
    pr_url: str
    base_ref: str
    base_sha: str
    head_sha: str
    pull_request_path: str
    diff_path: str
    changed_files_path: str
    requirement_paths: tuple[str, ...]
    worktree_path: str
    context_dir_path: str


class CodeReviewContextPreparer:
    def __init__(
        self,
        *,
        base_dir: Path,
        workspace_root: Path,
        requirements_root: Path,
        worktree_manager: GitWorktreeManager,
    ) -> None:
        self.base_dir = base_dir.resolve()
        self.workspace_root = workspace_root.resolve()
        self.requirements_root = requirements_root.resolve()
        self.worktree_manager = worktree_manager

    def prepare(
        self,
        *,
        job: ReviewContextJob,
        snapshot: ReviewContextSnapshot,
        repo_config: ReviewContextRepositoryConfig,
    ) -> CodeReviewPreparedContext:
        worktree_path = self.workspace_root / CODE_REVIEW_CONTEXT_ROOT
        workspace_repo_path: Path | None = None
        try:
            repository = self._repository(repo_config, job.repo_full_name)
            worktree_path = self.worktree_manager.revision_path(
                repository=repository,
                resolved_sha=job.head_sha,
            )
            workspace_repo_path = repository.workspace_repo_path
            self.worktree_manager.fetch_pull_request_head(
                repository=repository,
                pr_number=job.pr_number,
                expected_sha=job.head_sha,
            )
            self.worktree_manager.fetch_branch(
                repository=repository,
                branch=job.base_ref,
            )
            prepared_worktree = self.worktree_manager.ensure_revision(
                repository=repository,
                resolved_sha=job.head_sha,
                fetch=False,
            )
            if not self.worktree_manager.is_commit_available(repository=repository, sha=job.base_sha):
                self.worktree_manager.fetch_branch(
                    repository=repository,
                    branch=job.base_ref,
                )
            if not self.worktree_manager.is_commit_available(repository=repository, sha=job.base_sha):
                raise RuntimeError(f"无法在本地仓库解析 PR base_sha：{job.base_sha}")

            context_dir = self.code_review_context_dir(
                repo_full_name=job.repo_full_name,
                pr_number=job.pr_number,
                head_sha=job.head_sha,
            )
            with self.worktree_manager.context_lock(
                repo_full_name=job.repo_full_name,
                pr_number=job.pr_number,
                resolved_sha=job.head_sha,
            ):
                context_dir.mkdir(parents=True, exist_ok=True)
                pull_request_path = context_dir / "pull-request.md"
                diff_path = context_dir / "diff.patch"
                changed_files_path = context_dir / "changed-files.txt"
                pull_request_path.write_text(
                    self._pull_request_markdown(job=job, snapshot=snapshot),
                    encoding="utf-8",
                )
                diff_path.write_text(
                    self.worktree_manager.diff(
                        prepared_worktree=prepared_worktree,
                        base_sha=job.base_sha,
                    ),
                    encoding="utf-8",
                )
                changed_files_path.write_text(
                    self.worktree_manager.diff(
                        prepared_worktree=prepared_worktree,
                        base_sha=job.base_sha,
                        name_status=True,
                    ),
                    encoding="utf-8",
                )

                source_requirement_paths = self.requirement_paths_for_clickup_task_ids(extract_clickup_task_ids(snapshot.body))
                requirement_paths = self._write_requirement_context_files(
                    context_dir=context_dir,
                    requirement_paths=source_requirement_paths,
                )
            return CodeReviewPreparedContext(
                repo_full_name=job.repo_full_name,
                pr_number=job.pr_number,
                pr_url=job.pr_url,
                base_ref=job.base_ref,
                base_sha=job.base_sha,
                head_sha=job.head_sha,
                pull_request_path=self.workspace_display_path(pull_request_path),
                diff_path=self.workspace_display_path(diff_path),
                changed_files_path=self.workspace_display_path(changed_files_path),
                requirement_paths=requirement_paths,
                worktree_path=self.workspace_display_path(prepared_worktree.path),
                context_dir_path=self.workspace_display_path(context_dir),
            )
        except Exception as exc:
            raise RuntimeError(
                self._format_context_failure(
                    job=job,
                    repo_config=repo_config,
                    workspace_repo_path=workspace_repo_path,
                    worktree_path=worktree_path,
                    reason=str(exc),
                )
            ) from exc

    def code_review_context_dir(self, *, repo_full_name: str, pr_number: int, head_sha: str) -> Path:
        owner, repo = split_repo_full_name(repo_full_name)
        return self.workspace_root / CODE_REVIEW_CONTEXT_ROOT / owner / repo / f"PR_{pr_number}" / head_sha

    def requirement_paths_for_clickup_task_ids(self, task_ids: tuple[str, ...]) -> tuple[str, ...]:
        requirements_root = self.requirements_root / "tasks"
        if not task_ids or not requirements_root.is_dir():
            return ()
        found: dict[str, float] = {}
        for task_id in task_ids:
            for path in requirements_root.rglob(f"*__{task_id}.md"):
                if not path.is_file():
                    continue
                relative_to_tasks = path.relative_to(requirements_root)
                if any(part.startswith("_") for part in relative_to_tasks.parts):
                    continue
                workspace_path = self.workspace_display_path(path)
                found[workspace_path] = path.stat().st_mtime
        return tuple(path for path, _ in sorted(found.items(), key=lambda item: item[1], reverse=True))

    def workspace_display_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.workspace_root).as_posix()

    def _write_requirement_context_files(self, *, context_dir: Path, requirement_paths: tuple[str, ...]) -> tuple[str, ...]:
        requirements_dir = context_dir / "requirements"
        if requirements_dir.is_dir():
            for path in requirements_dir.glob("*.md"):
                if path.is_file():
                    path.unlink()
        if not requirement_paths:
            return ()

        requirements_dir.mkdir(parents=True, exist_ok=True)
        copied_paths: list[str] = []
        index_lines = ["# Requirement Index", ""]
        for index, requirement_path in enumerate(requirement_paths, start=1):
            source_path = (self.workspace_root / requirement_path).resolve()
            try:
                source_path.relative_to(self.workspace_root)
            except ValueError as exc:
                raise ValueError(f"需求文档路径不在 workspace 内：{requirement_path}") from exc
            content = source_path.read_text(encoding="utf-8")
            target_path = requirements_dir / f"requirement-{index}.md"
            target_path.write_text(
                "\n".join(
                    [
                        f"# Requirement {index}",
                        "",
                        f"Original path: `{requirement_path}`",
                        "",
                        "---",
                        "",
                        content,
                    ]
                ),
                encoding="utf-8",
            )
            copied_paths.append(self.workspace_display_path(target_path))
            index_lines.append(f"- `requirement-{index}.md`: `{requirement_path}`")

        (requirements_dir / "index.md").write_text("\n".join(index_lines) + "\n", encoding="utf-8")
        return tuple(copied_paths)

    def _repository(
        self,
        repo_config: ReviewContextRepositoryConfig,
        repo_full_name: str,
    ) -> GitRepository:
        if not repo_config.workspace_repo_path:
            raise ValueError("workspace_repo_path 未配置。")
        workspace_repo_path = Path(repo_config.workspace_repo_path)
        if not workspace_repo_path.is_absolute():
            workspace_repo_path = self.base_dir / workspace_repo_path
        workspace_repo_path = workspace_repo_path.resolve()
        organization_key = repo_config.organization_key.strip().lower()
        if not organization_key:
            relative = workspace_repo_path.relative_to(self.workspace_root)
            organization_key = relative.parts[0]
        return GitRepository(
            repo_full_name=repo_full_name,
            workspace_repo_path=workspace_repo_path,
            organization_key=organization_key,
            origin=repo_config.origin,
            default_branch=repo_config.default_branch,
            github_access=repo_config.github_access,
        )

    def _pull_request_markdown(
        self,
        *,
        job: ReviewContextJob,
        snapshot: ReviewContextSnapshot,
    ) -> str:
        return "\n".join(
            [
                f"# PR #{job.pr_number}",
                "",
                f"- Title: {getattr(snapshot, 'title', '') or '（无标题）'}",
                f"- Repository: `{job.repo_full_name}`",
                f"- URL: {job.pr_url}",
                f"- Author: `{getattr(snapshot, 'author_login', '') or 'unknown'}`",
                f"- State: `{getattr(snapshot, 'state', '') or 'unknown'}`",
                f"- Base: `{job.base_ref}` `{job.base_sha}`",
                f"- Head: `{job.head_sha}`",
                "",
                "## Description",
                "",
                snapshot.body.strip() or "（无描述）",
                "",
            ]
        )

    def _format_context_failure(
        self,
        *,
        job: ReviewContextJob,
        repo_config: ReviewContextRepositoryConfig,
        workspace_repo_path: Path | None,
        worktree_path: Path,
        reason: str,
    ) -> str:
        resolved_repo_path = str(workspace_repo_path) if workspace_repo_path is not None else (repo_config.workspace_repo_path or "<未配置>")
        return "\n".join(
            [
                "PR 代码 Review 上下文准备失败。",
                f"repo_full_name: {job.repo_full_name}",
                f"pr_number: {job.pr_number}",
                f"base_sha: {job.base_sha}",
                f"head_sha: {job.head_sha}",
                f"workspace_repo_path: {resolved_repo_path}",
                f"worktree_path: {worktree_path}",
                f"失败原因: {reason}",
            ]
        )

def split_repo_full_name(repo_full_name: str) -> tuple[str, str]:
    parts = tuple(part.strip() for part in repo_full_name.split("/") if part.strip())
    if len(parts) != 2 or not all(re.match(r"^[A-Za-z0-9_.-]+$", part) for part in parts):
        raise ValueError(f"GitHub 仓库名格式无效：{repo_full_name}")
    return parts[0], parts[1]
