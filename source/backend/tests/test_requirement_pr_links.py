from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from app.business.requirement_pr_links import RequirementPrLinkService, RequirementPrLinkStore


@dataclass(frozen=True)
class _PrSnapshot:
    pr_number: int
    title: str = "调整优惠券状态"
    body: str = ""
    base_ref: str = "main"
    draft: bool = False
    author_login: str = "author"
    html_url: str = ""
    updated_at: str = "2026-07-01T00:00:00Z"
    merged_at: str = ""

    def __post_init__(self) -> None:
        if not self.html_url:
            object.__setattr__(self, "html_url", f"https://github.com/example-org/example_backend/pull/{self.pr_number}")


class _FakeGitHubClient:
    def __init__(self, *, details: dict[int, _PrSnapshot] | None = None, failing: set[int] | None = None) -> None:
        self.details = details or {}
        self.failing = failing or set()
        self.fetched: list[int] = []

    def fetch_pull_request(self, *, repo_full_name: str, pr_number: int) -> _PrSnapshot:
        del repo_full_name
        self.fetched.append(pr_number)
        if pr_number in self.failing:
            raise RuntimeError("github boom")
        return self.details[pr_number]


def _build_service(tmp_path: Path) -> RequirementPrLinkService:
    return RequirementPrLinkService(
        base_dir=tmp_path,
        requirements_root=tmp_path / "workspace/coinex/knowledge/requirements",
        store=RequirementPrLinkStore(str(tmp_path / "assistant.sqlite3")),
    )


def _write_requirement(tmp_path: Path, relative_path: str, content: str = "# 需求\n\n本地需求内容。") -> str:
    path = tmp_path / "workspace/coinex/knowledge/requirements/tasks" / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"workspace/coinex/knowledge/requirements/tasks/{relative_path}"


def test_sync_creates_links_from_url_and_loose_labels(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    snapshot = _PrSnapshot(
        pr_number=1234,
        body="https://app.clickup.com/t/86abcde\n需求：86fghij",
    )

    service.sync_repository_pull_requests(
        repo_full_name="example-org/example_backend",
        open_snapshots=(snapshot,),
        github_client=_FakeGitHubClient(),
    )

    links = service.store.list_for_task(task_id="86abcde")
    assert len(links) == 1
    assert links[0].pr_number == 1234
    assert links[0].state == "open"
    assert links[0].pr_url == "https://github.com/example-org/example_backend/pull/1234"
    assert service.store.list_for_task(task_id="86fghij")[0].pr_number == 1234


def test_sync_marks_draft_state(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    service.sync_repository_pull_requests(
        repo_full_name="example-org/example_backend",
        open_snapshots=(_PrSnapshot(pr_number=1, draft=True, body="需求：86draft1"),),
        github_client=_FakeGitHubClient(),
    )

    assert service.store.list_for_task(task_id="86draft1")[0].state == "draft"


def test_sync_replaces_links_when_body_changes_and_keeps_first_seen(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    repo = "example-org/example_backend"
    service.sync_repository_pull_requests(
        repo_full_name=repo,
        open_snapshots=(_PrSnapshot(pr_number=7, body="需求：86keep01\n需求：86drop01"),),
        github_client=_FakeGitHubClient(),
    )
    first_seen = service.store.list_for_task(task_id="86keep01")[0].first_seen_at

    service.sync_repository_pull_requests(
        repo_full_name=repo,
        open_snapshots=(_PrSnapshot(pr_number=7, body="需求：86keep01\n需求：86new001"),),
        github_client=_FakeGitHubClient(),
    )

    assert service.store.list_for_task(task_id="86drop01") == ()
    assert service.store.list_for_task(task_id="86new001")[0].pr_number == 7
    assert service.store.list_for_task(task_id="86keep01")[0].first_seen_at == first_seen


def test_sync_removes_all_links_when_body_has_no_task(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    repo = "example-org/example_backend"
    service.sync_repository_pull_requests(
        repo_full_name=repo,
        open_snapshots=(_PrSnapshot(pr_number=9, body="需求：86gone01"),),
        github_client=_FakeGitHubClient(),
    )

    service.sync_repository_pull_requests(
        repo_full_name=repo,
        open_snapshots=(_PrSnapshot(pr_number=9, body="没有关联任何需求"),),
        github_client=_FakeGitHubClient(),
    )

    assert service.store.list_for_task(task_id="86gone01") == ()


def test_sync_updates_disappeared_pr_to_merged_or_closed(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    repo = "example-org/example_backend"
    service.sync_repository_pull_requests(
        repo_full_name=repo,
        open_snapshots=(
            _PrSnapshot(pr_number=1, body="需求：86merge1"),
            _PrSnapshot(pr_number=2, body="需求：86close1"),
            _PrSnapshot(pr_number=3, body="需求：86fail01"),
        ),
        github_client=_FakeGitHubClient(),
    )

    github_client = _FakeGitHubClient(
        details={
            1: _PrSnapshot(pr_number=1, merged_at="2026-07-02T00:00:00Z", updated_at="2026-07-02T00:00:00Z"),
            2: _PrSnapshot(pr_number=2, updated_at="2026-07-03T00:00:00Z"),
        },
        failing={3},
    )
    # 本轮三个 PR 都已不在 Open 列表中
    service.sync_repository_pull_requests(
        repo_full_name=repo,
        open_snapshots=(),
        github_client=github_client,
    )

    assert service.store.list_for_task(task_id="86merge1")[0].state == "merged"
    assert service.store.list_for_task(task_id="86close1")[0].state == "closed"
    # 读取失败的 PR 保留原状态，等待下轮重试
    assert service.store.list_for_task(task_id="86fail01")[0].state == "open"


def test_sync_is_idempotent_across_repeated_polls(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    repo = "example-org/example_backend"
    snapshots = (_PrSnapshot(pr_number=5, body="需求：86same01"),)
    for _ in range(3):
        service.sync_repository_pull_requests(
            repo_full_name=repo,
            open_snapshots=snapshots,
            github_client=_FakeGitHubClient(),
        )

    assert len(service.store.list_for_task(task_id="86same01")) == 1


def test_list_for_requirement_reads_task_id_from_filename(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    source_path = _write_requirement(tmp_path, "in_progress/coupon__86abcde.md")
    service.sync_repository_pull_requests(
        repo_full_name="example-org/example_backend",
        open_snapshots=(_PrSnapshot(pr_number=11, body="需求：86abcde"),),
        github_client=_FakeGitHubClient(),
    )

    result = service.list_pull_requests_for_requirement(source_path=source_path)

    assert len(result) == 1
    assert result[0].task_id == "86abcde"
    assert result[0].pr_number == 11


def test_list_for_requirement_reads_task_id_from_meta(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    source_path = _write_requirement(
        tmp_path,
        "in_progress/coupon.md",
        content="# 需求\n\n- Task ID: `86metaz1`\n",
    )
    service.sync_repository_pull_requests(
        repo_full_name="example-org/example_backend",
        open_snapshots=(_PrSnapshot(pr_number=12, body="需求：86metaz1"),),
        github_client=_FakeGitHubClient(),
    )

    result = service.list_pull_requests_for_requirement(source_path=source_path)

    assert [link.pr_number for link in result] == [12]


def test_list_for_requirement_sorts_open_before_merged_before_closed(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    source_path = _write_requirement(tmp_path, "in_progress/coupon__86sort01.md")
    now = "2026-07-01T00:00:00Z"
    service.store.sync_pr_links(
        repo_full_name="example-org/example_backend",
        pr_number=1,
        links=(
            _link("86sort01", 1, state="closed", updated="2026-07-05T00:00:00Z", now=now),
        ),
    )
    service.store.sync_pr_links(
        repo_full_name="example-org/example_backend",
        pr_number=2,
        links=(
            _link("86sort01", 2, state="merged", updated="2026-07-06T00:00:00Z", now=now),
        ),
    )
    service.store.sync_pr_links(
        repo_full_name="example-org/example_backend",
        pr_number=3,
        links=(
            _link("86sort01", 3, state="open", updated="2026-07-03T00:00:00Z", now=now),
        ),
    )
    service.store.sync_pr_links(
        repo_full_name="example-org/example_backend",
        pr_number=4,
        links=(
            _link("86sort01", 4, state="open", updated="2026-07-04T00:00:00Z", now=now),
        ),
    )

    result = service.list_pull_requests_for_requirement(source_path=source_path)

    # Open（更新时间倒序）→ Merged → Closed
    assert [link.pr_number for link in result] == [4, 3, 2, 1]


def test_list_for_requirement_accepts_frontend_display_path(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    host_path = _write_requirement(tmp_path, "in_progress/coupon__86disp01.md")
    display_path = "/" + host_path.removeprefix("workspace/")
    service.sync_repository_pull_requests(
        repo_full_name="example-org/example_backend",
        open_snapshots=(_PrSnapshot(pr_number=21, body="需求：86disp01"),),
        github_client=_FakeGitHubClient(),
    )

    result = service.list_pull_requests_for_requirement(source_path=display_path)

    assert [link.pr_number for link in result] == [21]


def test_list_for_requirement_rejects_path_outside_requirements(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("# x", encoding="utf-8")

    with pytest.raises(FileNotFoundError):
        service.list_pull_requests_for_requirement(source_path="outside.md")


def test_list_for_requirement_rejects_deleted_requirement(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    source_path = _write_requirement(tmp_path, "_deleted/coupon__86del01.md")

    with pytest.raises(FileNotFoundError):
        service.list_pull_requests_for_requirement(source_path=source_path)


def test_list_for_requirement_without_task_id_returns_empty(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    source_path = _write_requirement(tmp_path, "in_progress/no_task_id.md")

    assert service.list_pull_requests_for_requirement(source_path=source_path) == ()


def _link(task_id: str, pr_number: int, *, state: str, updated: str, now: str):
    from app.business.requirement_pr_links import RequirementPrLink

    return RequirementPrLink(
        task_id=task_id,
        repo_full_name="example-org/example_backend",
        pr_number=pr_number,
        pr_url=f"https://github.com/example-org/example_backend/pull/{pr_number}",
        title=f"PR {pr_number}",
        author_login="author",
        state=state,
        base_ref="main",
        github_updated_at=updated,
        first_seen_at=now,
        last_seen_at=now,
    )
