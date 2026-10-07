from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from app.business.requirement_pr_links.store import RequirementPrLink, RequirementPrLinkStore
from app.utils.clickup.task_link import extract_clickup_task_ids


logger = logging.getLogger(__name__)

_STATE_ORDER = {"draft": 0, "open": 0, "merged": 1, "closed": 2}


class PrLinkPullRequest(Protocol):
    pr_number: int
    title: str
    body: str
    base_ref: str
    draft: bool
    author_login: str
    html_url: str
    updated_at: str
    merged_at: str


class PrLinkGitHubClient(Protocol):
    def fetch_pull_request(self, *, repo_full_name: str, pr_number: int) -> PrLinkPullRequest: ...


class RequirementPrLinkService:
    def __init__(self, *, base_dir: Path, requirements_root: Path, store: RequirementPrLinkStore) -> None:
        self.base_dir = base_dir.resolve()
        self.requirements_root = requirements_root.resolve()
        self.store = store

    def sync_repository_pull_requests(
        self,
        *,
        repo_full_name: str,
        open_snapshots: tuple[PrLinkPullRequest, ...],
        github_client: PrLinkGitHubClient,
    ) -> None:
        now = _utc_now()
        seen_pr_numbers: set[int] = set()
        for snapshot in open_snapshots:
            seen_pr_numbers.add(snapshot.pr_number)
            state = "draft" if snapshot.draft else "open"
            links = tuple(
                RequirementPrLink(
                    task_id=task_id,
                    repo_full_name=repo_full_name,
                    pr_number=snapshot.pr_number,
                    pr_url=snapshot.html_url,
                    title=snapshot.title,
                    author_login=snapshot.author_login,
                    state=state,
                    base_ref=snapshot.base_ref,
                    github_updated_at=snapshot.updated_at,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                for task_id in extract_clickup_task_ids(snapshot.body)
            )
            self.store.sync_pr_links(repo_full_name=repo_full_name, pr_number=snapshot.pr_number, links=links)
        self._refresh_disappeared_pull_requests(
            repo_full_name=repo_full_name,
            seen_pr_numbers=seen_pr_numbers,
            github_client=github_client,
            now=now,
        )

    def list_pull_requests_for_requirement(self, *, source_path: str) -> tuple[RequirementPrLink, ...]:
        source_file = self._resolve_requirement_file(source_path)
        task_id = self._extract_task_id(source_file)
        if not task_id:
            return ()
        return self._sort_links(self.store.list_for_task(task_id=task_id))

    def _refresh_disappeared_pull_requests(
        self,
        *,
        repo_full_name: str,
        seen_pr_numbers: set[int],
        github_client: PrLinkGitHubClient,
        now: str,
    ) -> None:
        active_links = self.store.list_active_links(repo_full_name=repo_full_name)
        disappeared = sorted({link.pr_number for link in active_links if link.pr_number not in seen_pr_numbers})
        for pr_number in disappeared:
            try:
                detail = github_client.fetch_pull_request(repo_full_name=repo_full_name, pr_number=pr_number)
            except Exception:
                logger.warning(
                    "读取 PR 详情失败，保留需求关联等待下轮重试: repo=%s pr=%s",
                    repo_full_name,
                    pr_number,
                )
                continue
            new_state = "merged" if detail.merged_at else "closed"
            self.store.update_state(
                repo_full_name=repo_full_name,
                pr_number=pr_number,
                state=new_state,
                github_updated_at=detail.updated_at or detail.merged_at,
                last_seen_at=now,
            )

    def _sort_links(self, links: tuple[RequirementPrLink, ...]) -> tuple[RequirementPrLink, ...]:
        by_updated = sorted(links, key=lambda link: link.github_updated_at, reverse=True)
        return tuple(sorted(by_updated, key=lambda link: _STATE_ORDER.get(link.state, len(_STATE_ORDER))))

    def _resolve_requirement_file(self, source_path: str) -> Path:
        normalized = source_path.strip().strip("/")
        if not normalized:
            raise FileNotFoundError("需求文件不存在。")
        # 前端传入的是“展示路径”（如 /coinex/knowledge/requirements/tasks/xxx.md，相对 workspace 且省略 workspace 段），
        # 需先还原为宿主相对路径，再校验确实落在需求知识库内。
        host_relative = normalized if normalized.startswith("workspace/") else f"workspace/{normalized}"
        target = (self.base_dir / host_relative).resolve()
        try:
            target.relative_to(self.requirements_root)
        except ValueError as exc:
            raise FileNotFoundError("需求文件不存在。") from exc
        if not target.is_file() or target.suffix.lower() != ".md" or self._is_hidden_requirement_path(target):
            raise FileNotFoundError("需求文件不存在。")
        return target

    def _is_hidden_requirement_path(self, path: Path) -> bool:
        try:
            parts = path.resolve().relative_to(self.requirements_root).parts
        except ValueError:
            return True
        return any(part.startswith("_") for part in parts[:-1])

    def _extract_task_id(self, source_file: Path) -> str:
        markdown = source_file.read_text(encoding="utf-8", errors="replace")
        meta_match = re.search(r"^- Task ID:\s*`?([A-Za-z0-9_-]+)`?\s*$", markdown, flags=re.MULTILINE)
        if meta_match:
            return meta_match.group(1)
        filename_match = re.search(r"__([A-Za-z0-9_-]+)\.md$", source_file.name)
        if filename_match:
            return filename_match.group(1)
        return ""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
