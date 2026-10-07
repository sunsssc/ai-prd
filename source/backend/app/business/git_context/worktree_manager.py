from __future__ import annotations

import fcntl
import os
import re
import shlex
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from app.business.git_context.models import GitRepository, PreparedGitWorktree
from app.business.git_context.repository_lock import repository_lock


FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class GitWorktreeManager:
    def __init__(self, *, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.root = self.workspace_root / "runtime/code-review-worktrees"
        self.lock_root = self.root / ".locks"

    def fetch_repository(self, repository: GitRepository) -> None:
        self._validate_source_repository(repository)
        with self._repository_lock(repository=repository):
            self._run_git(
                ["fetch", repository.origin, "--prune"],
                cwd=repository.workspace_repo_path,
                timeout=300,
            )

    def fetch_pull_request_head(
        self,
        *,
        repository: GitRepository,
        pr_number: int,
        expected_sha: str,
    ) -> None:
        if pr_number <= 0:
            raise ValueError("PR number 必须为正整数。")
        sha = expected_sha.strip().lower()
        self._validate_sha(sha)
        self._validate_source_repository(repository)
        remote_ref = f"refs/remotes/{repository.origin}/pull/{pr_number}/head"
        refspec = f"+refs/pull/{pr_number}/head:{remote_ref}"
        with self._repository_lock(repository=repository):
            self._run_git(
                ["fetch", repository.origin, refspec],
                cwd=repository.workspace_repo_path,
                timeout=300,
            )
            fetched_sha = self._run_git(
                ["rev-parse", "--verify", f"{remote_ref}^{{commit}}"],
                cwd=repository.workspace_repo_path,
                timeout=60,
            ).strip().lower()
        if fetched_sha != sha:
            raise RuntimeError("GitHub PR ref 与服务端读取的 head_sha 不一致。")

    def fetch_branch(self, *, repository: GitRepository, branch: str) -> None:
        normalized = branch.strip()
        self._validate_branch(repository, normalized)
        self._validate_source_repository(repository)
        with self._repository_lock(repository=repository):
            self._fetch_branch_ref(repository=repository, branch=normalized)

    def resolve_branch_tip(self, *, repository: GitRepository, branch: str) -> str:
        normalized = branch.strip()
        self._validate_branch(repository, normalized)
        with self._repository_lock(repository=repository):
            self._fetch_branch_ref(repository=repository, branch=normalized)
            sha = self._run_git(
                ["rev-parse", "--verify", f"refs/remotes/{repository.origin}/{normalized}^{{commit}}"],
                cwd=repository.workspace_repo_path,
                timeout=60,
            ).strip().lower()
        self._validate_sha(sha)
        return sha

    def ensure_revision(
        self,
        *,
        repository: GitRepository,
        resolved_sha: str,
        fetch: bool,
    ) -> PreparedGitWorktree:
        sha = resolved_sha.strip().lower()
        self._validate_sha(sha)
        self._validate_source_repository(repository)
        if fetch:
            self.fetch_repository(repository)
        self._ensure_commit_available(repository=repository, sha=sha)
        path = self.revision_path(repository=repository, resolved_sha=sha)
        with self._revision_lock(repository=repository, resolved_sha=sha):
            if path.exists() or path.is_symlink():
                if self._is_valid_revision_worktree(repository=repository, path=path, resolved_sha=sha):
                    return PreparedGitWorktree(repository=repository, resolved_sha=sha, path=path)
                self._remove_controlled_worktree(repository=repository, path=path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._run_git(
                ["worktree", "add", "--detach", str(path), sha],
                cwd=repository.workspace_repo_path,
                timeout=180,
            )
            if not self._is_valid_revision_worktree(repository=repository, path=path, resolved_sha=sha):
                raise RuntimeError("创建 revision worktree 后校验失败。")
        return PreparedGitWorktree(repository=repository, resolved_sha=sha, path=path)

    def revision_path(self, *, repository: GitRepository, resolved_sha: str) -> Path:
        owner, repo = repository.repo_full_name.split("/", 1)
        path = self.root / owner / repo / "revisions" / resolved_sha
        self._ensure_under_root(path)
        return path

    @contextmanager
    def context_lock(
        self,
        *,
        repo_full_name: str,
        pr_number: int,
        resolved_sha: str,
    ) -> Iterator[None]:
        self._validate_sha(resolved_sha)
        owner, repo = repo_full_name.split("/", 1)
        self.lock_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.lock_root / f"context__{owner}__{repo}__PR_{pr_number}__{resolved_sha}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def is_commit_available(self, *, repository: GitRepository, sha: str) -> bool:
        try:
            self._ensure_commit_available(repository=repository, sha=sha)
        except RuntimeError:
            return False
        return True

    def diff(
        self,
        *,
        prepared_worktree: PreparedGitWorktree,
        base_sha: str,
        name_status: bool = False,
    ) -> str:
        self._validate_sha(base_sha.strip().lower())
        self._ensure_commit_available(repository=prepared_worktree.repository, sha=base_sha)
        args = ["diff"]
        if name_status:
            args.append("--name-status")
        args.append(f"{base_sha}...{prepared_worktree.resolved_sha}")
        return self._run_git(args, cwd=prepared_worktree.path, timeout=300)

    def _validate_source_repository(self, repository: GitRepository) -> None:
        if not repository.workspace_repo_path.is_dir():
            raise FileNotFoundError(f"workspace_repo_path 不是目录：{repository.workspace_repo_path}")
        self._run_git(
            ["rev-parse", "--is-inside-work-tree"],
            cwd=repository.workspace_repo_path,
            timeout=60,
        )

    def _validate_branch(self, repository: GitRepository, branch: str) -> None:
        if not branch:
            raise ValueError("分支名不能为空。")
        self._run_git(
            ["check-ref-format", "--branch", branch],
            cwd=repository.workspace_repo_path,
            timeout=60,
        )

    def _validate_sha(self, sha: str) -> None:
        if not FULL_SHA.fullmatch(sha):
            raise ValueError("resolved_sha 必须是服务端解析出的完整 commit SHA。")

    def _ensure_commit_available(self, *, repository: GitRepository, sha: str) -> None:
        self._run_git(
            ["cat-file", "-e", f"{sha}^{{commit}}"],
            cwd=repository.workspace_repo_path,
            timeout=60,
        )

    def _is_valid_revision_worktree(
        self,
        *,
        repository: GitRepository,
        path: Path,
        resolved_sha: str,
    ) -> bool:
        if not path.is_dir() or path.is_symlink():
            return False
        try:
            registered_paths = self._registered_worktree_paths(repository)
            if path.resolve() not in registered_paths:
                return False
            current_head = self._run_git(["rev-parse", "HEAD"], cwd=path, timeout=60).strip().lower()
            if current_head != resolved_sha:
                return False
            return not self._run_git(["status", "--porcelain"], cwd=path, timeout=60).strip()
        except RuntimeError:
            return False

    def _registered_worktree_paths(self, repository: GitRepository) -> set[Path]:
        output = self._run_git(
            ["worktree", "list", "--porcelain"],
            cwd=repository.workspace_repo_path,
            timeout=60,
        )
        return {
            Path(line.removeprefix("worktree ")).resolve()
            for line in output.splitlines()
            if line.startswith("worktree ")
        }

    def _remove_controlled_worktree(self, *, repository: GitRepository, path: Path) -> None:
        self._ensure_under_root(path)
        registered = path.resolve() in self._registered_worktree_paths(repository)
        if registered:
            self._run_git(
                ["worktree", "remove", "--force", str(path)],
                cwd=repository.workspace_repo_path,
                timeout=180,
            )
        elif path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_symlink() or child.is_file():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
            path.rmdir()

    def _ensure_under_root(self, path: Path) -> None:
        try:
            path.resolve(strict=False).relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"路径不在受控 worktree 根目录：{path}") from exc

    @contextmanager
    def _revision_lock(
        self,
        *,
        repository: GitRepository,
        resolved_sha: str,
    ) -> Iterator[None]:
        owner, repo = repository.repo_full_name.split("/", 1)
        self.lock_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.lock_root / f"{owner}__{repo}__{resolved_sha}.lock"
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @contextmanager
    def _repository_lock(self, *, repository: GitRepository) -> Iterator[None]:
        with repository_lock(
            lock_root=self.lock_root,
            repository_path=repository.workspace_repo_path,
        ):
            yield

    def _fetch_branch_ref(self, *, repository: GitRepository, branch: str) -> None:
        refspec = f"+refs/heads/{branch}:refs/remotes/{repository.origin}/{branch}"
        self._run_git(
            ["fetch", repository.origin, refspec],
            cwd=repository.workspace_repo_path,
            timeout=300,
        )

    def _run_git(self, args: list[str], *, cwd: Path, timeout: int) -> str:
        command = ["git", *args]
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            rendered = " ".join(shlex.quote(part) for part in command)
            raise RuntimeError(f"命令执行失败：{rendered}\n{detail[:1000]}")
        return result.stdout
