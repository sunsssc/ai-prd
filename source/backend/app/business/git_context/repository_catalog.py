from __future__ import annotations

import json
import re
from pathlib import Path

from app.business.business_doc_updates.service import (
    GitHubCliPullRequestClient,
    GitHubPullRequestClient,
)
from app.business.git_context.models import GitRepository


class GitRepositoryCatalog:
    def __init__(
        self,
        *,
        base_dir: Path,
        config_path: str,
        github_client: GitHubPullRequestClient,
        github_cli_account: str,
    ) -> None:
        self.base_dir = base_dir.resolve()
        self.config_path = config_path.strip()
        self.github_client = github_client
        self.github_cli_account = github_cli_account.strip()

    def get(self, repo_full_name: str) -> GitRepository:
        normalized = repo_full_name.strip()
        payload = self._load_payload()
        for item in payload.get("repositories", []):
            if not isinstance(item, dict):
                continue
            if str(item.get("repo_full_name") or "").strip() != normalized or not bool(item.get("enabled", True)):
                continue
            return self._parse_repository(item)
        raise ValueError("该 GitHub 仓库未配置为受控代码仓库。")

    def list_for_organization(self, organization_key: str) -> tuple[GitRepository, ...]:
        normalized_organization_key = organization_key.strip().lower()
        repositories: list[GitRepository] = []
        for item in self._load_payload().get("repositories", []):
            if not isinstance(item, dict) or not bool(item.get("enabled", True)):
                continue
            repository = self._parse_repository(item)
            if repository.organization_key == normalized_organization_key:
                repositories.append(repository)
        return tuple(sorted(repositories, key=lambda item: item.repo_full_name))

    def resolve_for_organization(self, *, organization_key: str, repository: str) -> GitRepository:
        normalized = repository.strip()
        if not normalized:
            raise ValueError("仓库名不能为空。")
        candidates = [
            item
            for item in self.list_for_organization(organization_key)
            if item.repo_full_name == normalized or item.repo_name == normalized
        ]
        if not candidates:
            raise ValueError("该仓库未配置为当前组织的受控代码仓库。")
        if len(candidates) > 1:
            raise ValueError("仓库短名称有歧义，请使用 owner/repo。")
        return candidates[0]

    def github_client_for(
        self,
        repository: GitRepository,
    ) -> GitHubPullRequestClient | GitHubCliPullRequestClient:
        if repository.github_access == "api":
            return self.github_client
        if repository.github_access != "gh_cli":
            raise ValueError(f"不支持的 GitHub 访问方式：{repository.github_access}")
        if not self.github_cli_account:
            raise ValueError("gh_cli 模式必须配置 GITHUB_CLI_ACCOUNT。")
        return GitHubCliPullRequestClient(
            repo_full_name=repository.repo_full_name,
            working_directory=repository.workspace_repo_path,
            account=self.github_cli_account,
        )

    def _load_payload(self) -> dict[str, object]:
        if not self.config_path:
            raise ValueError("受控代码仓库配置不存在。")
        config_file = Path(self.config_path)
        if not config_file.is_absolute():
            config_file = self.base_dir / config_file
        config_file = config_file.resolve()
        if not config_file.is_file():
            raise FileNotFoundError(f"受控代码仓库配置不存在：{config_file}")
        payload = json.loads(config_file.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("受控代码仓库配置必须是 JSON object。")
        return payload

    def _parse_repository(self, item: dict[str, object]) -> GitRepository:
        repo_full_name = str(item.get("repo_full_name") or "").strip()
        _split_repo_full_name(repo_full_name)
        configured_path = str(item.get("workspace_repo_path") or "").strip()
        if not configured_path:
            raise ValueError("workspace_repo_path 未配置。")
        workspace_repo_path = Path(configured_path)
        if not workspace_repo_path.is_absolute():
            workspace_repo_path = self.base_dir / workspace_repo_path
        workspace_repo_path = workspace_repo_path.resolve()

        base_branches = tuple(
            str(value).strip()
            for value in item.get("base_branches", [])
            if str(value).strip()
        ) if isinstance(item.get("base_branches"), list) else ()
        default_branch = str(item.get("default_branch") or "").strip() or (base_branches[0] if base_branches else "")
        if not default_branch:
            raise ValueError("default_branch 未配置。")

        organization_key = str(item.get("organization_key") or "").strip().lower()
        if not organization_key:
            organization_key = _organization_key_from_workspace_path(
                base_dir=self.base_dir,
                workspace_repo_path=workspace_repo_path,
            )
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", organization_key):
            raise ValueError("organization_key 格式无效。")

        origin = str(item.get("origin") or "origin").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", origin):
            raise ValueError("origin 格式无效。")
        github_access = str(item.get("github_access") or "api").strip().lower()
        return GitRepository(
            repo_full_name=repo_full_name,
            workspace_repo_path=workspace_repo_path,
            organization_key=organization_key,
            origin=origin,
            default_branch=default_branch,
            github_access=github_access,
        )


def _organization_key_from_workspace_path(*, base_dir: Path, workspace_repo_path: Path) -> str:
    try:
        relative = workspace_repo_path.relative_to(base_dir / "workspace")
    except ValueError as exc:
        raise ValueError("organization_key 未配置，且无法从 workspace_repo_path 解析。") from exc
    parts = relative.parts
    if len(parts) < 4 or parts[1:3] != ("knowledge", "code"):
        raise ValueError("organization_key 未配置，且 workspace_repo_path 不符合组织知识库路径。")
    return parts[0].lower()


def _split_repo_full_name(repo_full_name: str) -> tuple[str, str]:
    parts = tuple(part.strip() for part in repo_full_name.split("/") if part.strip())
    if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        raise ValueError(f"GitHub 仓库名格式无效：{repo_full_name}")
    return parts[0], parts[1]
