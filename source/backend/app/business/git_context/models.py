from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GitRepository:
    repo_full_name: str
    workspace_repo_path: Path
    organization_key: str
    origin: str
    default_branch: str
    github_access: str

    @property
    def repo_name(self) -> str:
        return self.repo_full_name.split("/", 1)[1]


@dataclass(frozen=True)
class PreparedGitWorktree:
    repository: GitRepository
    resolved_sha: str
    path: Path
