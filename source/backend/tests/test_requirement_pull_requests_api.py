from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from conftest import reset_dependency_overrides
from app.business.requirement_pr_links import RequirementPrLinkService, RequirementPrLinkStore
from app.core.dependencies import get_current_app_user, get_requirement_pr_link_service
from app.main import app


@dataclass(frozen=True)
class _Snapshot:
    pr_number: int = 1234
    title: str = "调整优惠券状态"
    body: str = "需求：86abcde"
    base_ref: str = "main"
    draft: bool = False
    author_login: str = "author"
    html_url: str = "https://github.com/example-org/example_backend/pull/1234"
    updated_at: str = "2026-07-01T00:00:00Z"
    merged_at: str = ""


class _NoFetchClient:
    def fetch_pull_request(self, *, repo_full_name: str, pr_number: int) -> _Snapshot:
        raise AssertionError("查询接口不应触发 GitHub 详情读取")


@pytest.fixture
def pr_link_env(tmp_path: Path):
    requirements_root = tmp_path / "workspace/coinex/knowledge/requirements"
    service = RequirementPrLinkService(
        base_dir=tmp_path,
        requirements_root=requirements_root,
        store=RequirementPrLinkStore(str(tmp_path / "assistant.sqlite3")),
    )
    task_file = requirements_root / "tasks/in_progress/coupon__86abcde.md"
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text("# 需求\n", encoding="utf-8")
    source_path = "workspace/coinex/knowledge/requirements/tasks/in_progress/coupon__86abcde.md"
    return service, source_path


def _override(service: RequirementPrLinkService) -> None:
    app.dependency_overrides[get_current_app_user] = lambda: object()
    app.dependency_overrides[get_requirement_pr_link_service] = lambda: service


@pytest.mark.anyio
async def test_requirement_pull_requests_endpoint_returns_links(client, pr_link_env) -> None:
    service, source_path = pr_link_env
    service.sync_repository_pull_requests(
        repo_full_name="example-org/example_backend",
        open_snapshots=(_Snapshot(),),
        github_client=_NoFetchClient(),
    )
    _override(service)
    try:
        response = await client.get("/api/requirements/pull-requests", params={"source_path": source_path})
    finally:
        reset_dependency_overrides()

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_path"] == source_path
    assert len(payload["pull_requests"]) == 1
    pr = payload["pull_requests"][0]
    assert pr["task_id"] == "86abcde"
    assert pr["repo_full_name"] == "example-org/example_backend"
    assert pr["pr_number"] == 1234
    assert pr["state"] == "open"
    assert pr["pr_url"] == "https://github.com/example-org/example_backend/pull/1234"


@pytest.mark.anyio
async def test_requirement_pull_requests_endpoint_rejects_path_outside_requirements(client, pr_link_env) -> None:
    service, _ = pr_link_env
    _override(service)
    try:
        response = await client.get("/api/requirements/pull-requests", params={"source_path": "outside.md"})
    finally:
        reset_dependency_overrides()

    assert response.status_code == 404
