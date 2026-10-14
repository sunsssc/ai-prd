from __future__ import annotations

from dataclasses import dataclass

from app.business.code_reviews import CodeReviewJob, CodeReviewStore
from app.business.my_tasks.store import (
    CC_RELATION,
    MyTaskRecord,
    MyTaskStore,
    environment_options,
    is_completed_task,
    is_supported_environment,
    normalize_environment,
)
from app.business.requirement_pr_links import RequirementPrLink, RequirementPrLinkStore


@dataclass(frozen=True)
class MyTaskPullRequest:
    link: RequirementPrLink
    review_job: CodeReviewJob | None


@dataclass(frozen=True)
class MyTaskItem:
    task: MyTaskRecord
    is_completed: bool
    relations: tuple[str, ...]
    manual_environment: str | None
    pull_requests: tuple[MyTaskPullRequest, ...]


@dataclass(frozen=True)
class MyTaskPage:
    items: tuple[MyTaskItem, ...]
    total: int
    page: int
    page_size: int
    total_pages: int
    scope_counts: dict[str, int]
    status_options: tuple[str, ...]
    active_status_options: tuple[str, ...]
    completed_status_options: tuple[str, ...]
    role_options: tuple[str, ...]


class MyTaskService:
    def __init__(
        self,
        *,
        store: MyTaskStore,
        pr_link_store: RequirementPrLinkStore,
        code_review_store: CodeReviewStore,
    ) -> None:
        self.store = store
        self.pr_link_store = pr_link_store
        self.code_review_store = code_review_store

    def list_tasks(
        self,
        *,
        email: str,
        scope: str = "active",
        assignment_scope: str = "mine",
        include_cc: bool = False,
        roles: tuple[str, ...] | None = None,
        statuses: tuple[str, ...] | None = None,
        query: str = "",
        page: int = 1,
        page_size: int = 50,
    ) -> MyTaskPage:
        if scope not in {"active", "completed", "all"}:
            raise ValueError("scope 只能是 active、completed 或 all。")
        if assignment_scope not in {"mine", "all"}:
            raise ValueError("assignment_scope 只能是 mine 或 all。")

        is_mine = assignment_scope == "mine"
        effective_roles = roles if is_mine else None
        effective_include_cc = include_cc if is_mine else False
        role_options = self.store.list_role_options_for_email(email=email) if is_mine else ()
        if effective_roles is not None and any(role not in role_options for role in effective_roles):
            raise ValueError("包含不支持的任务角色。")

        task_page = self.store.list_page_for_email(
            email=email,
            assignment_scope=assignment_scope,
            scope=scope,
            include_cc=effective_include_cc,
            roles=effective_roles,
            statuses=statuses,
            query=query,
            page=page,
            page_size=page_size,
        )
        items: list[MyTaskItem] = []
        for task in task_page.tasks:
            terminal = is_completed_task(task)
            relations = self.store.list_relations(task_id=task.task_id, email=email)
            if is_mine and roles is None and not include_cc and relations == (CC_RELATION,):
                continue
            pull_requests = tuple(
                MyTaskPullRequest(
                    link=link,
                    review_job=self.code_review_store.get_latest_job_for_pr(
                        repo_full_name=link.repo_full_name,
                        pr_number=link.pr_number,
                    ),
                )
                for link in self.pr_link_store.list_for_task(task_id=task.task_id)
            )
            items.append(
                MyTaskItem(
                    task=task,
                    is_completed=terminal,
                    relations=relations,
                    manual_environment=self.store.get_environment_override(task_id=task.task_id),
                    pull_requests=pull_requests,
                )
            )
        status_pairs = self.store.list_status_options_for_email(
            email=email,
            assignment_scope=assignment_scope,
        )
        return MyTaskPage(
            items=tuple(items),
            total=task_page.total,
            page=task_page.page,
            page_size=task_page.page_size,
            total_pages=(task_page.total + task_page.page_size - 1) // task_page.page_size,
            scope_counts=self.store.count_scopes_for_email(
                email=email,
                assignment_scope=assignment_scope,
                include_cc=effective_include_cc,
                roles=effective_roles,
                query=query,
            ),
            status_options=tuple(sorted({status for status, _ in status_pairs})),
            active_status_options=tuple(
                sorted({status for status, completed in status_pairs if not completed})
            ),
            completed_status_options=tuple(
                sorted({status for status, completed in status_pairs if completed})
            ),
            role_options=role_options,
        )

    def set_environment(self, *, task_id: str, environment: str | None, updated_by_email: str) -> None:
        normalized = normalize_environment(environment or "")
        if normalized and not is_supported_environment(normalized):
            raise ValueError("测试环境只能选择 testN 或 cbi。")
        if not self.store.list_relations(task_id=task_id, email=updated_by_email):
            raise FileNotFoundError("任务不存在或与你无关。")
        self.store.set_environment_override(
            task_id=task_id,
            environment=normalized or None,
            updated_by_email=updated_by_email,
        )

    def environment_options(self) -> tuple[str, ...]:
        return environment_options(self.store.list_detected_environments())

    def require_task_access(self, *, task_id: str, email: str) -> None:
        if not self.store.list_relations(task_id=task_id, email=email):
            raise FileNotFoundError("任务不存在或与你无关。")

    def require_pull_request_access(
        self,
        *,
        task_id: str,
        repo_full_name: str,
        pr_number: int,
    ) -> RequirementPrLink:
        link = self.pr_link_store.get_for_task_pr(
            task_id=task_id,
            repo_full_name=repo_full_name,
            pr_number=pr_number,
        )
        if link is None:
            raise FileNotFoundError("该 PR 未关联当前 Task。")
        return link
