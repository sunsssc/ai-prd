from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.api.my_tasks import create_pull_request_assistant_session
from app.business.code_reviews import CodeReviewStore
from app.business.my_tasks import MyTaskService, MyTaskStore
from app.business.requirement_pr_links import RequirementPrLink, RequirementPrLinkStore
from app.schemas.my_tasks import MyTaskAssistantSessionRequest
from app.utils.clickup.sync import SyncStats, _sync_raw_tasks


def _raw_task(
    *,
    task_id: str = "86task1",
    title: str = "AI 任务看板",
    updated_at: str = "1785292800000",
    status: str = "实现中",
    status_type: str = "custom",
    list_name: str = "实现中",
    comments: list | None = None,
) -> dict:
    return {
        "id": task_id,
        "name": title,
        "url": f"https://app.clickup.com/t/{task_id}",
        "status": {"status": status, "type": status_type},
        "priority": {"priority": "high"},
        "date_updated": updated_at,
        "due_date": None,
        "_list_name": list_name,
        "assignees": [
            {
                "id": 1,
                "username": "Jordan（张三）",
                "email": "jordan.lee@corp.test",
            }
        ],
        "custom_fields": [
            {
                "name": "后端",
                "type": "users",
                "value": [
                    {
                        "id": 1,
                        "username": "Jordan（张三）",
                        "email": "jordan.lee@corp.test",
                    }
                ],
            },
            {
                "name": "抄送人",
                "type": "users",
                "value": [
                    {
                        "id": 2,
                        "username": "Observer",
                        "email": "observer@corp.test",
                    }
                ],
            },
        ],
        "comments": comments or [{"comment_text": [{"text": "部署在 test6 和 testadmin6"}]}],
    }


def _build_service(tmp_path: Path) -> MyTaskService:
    db_path = str(tmp_path / "assistant.sqlite3")
    return MyTaskService(
        store=MyTaskStore(db_path),
        pr_link_store=RequirementPrLinkStore(db_path),
        code_review_store=CodeReviewStore(db_path),
    )


def test_my_tasks_matches_company_email_and_excludes_cc_by_default(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    service.store.upsert_clickup_task(
        raw_task=_raw_task(),
        source_path="workspace/coinex/knowledge/requirements/tasks/实现中/task__86task1.md",
    )

    jordan_tasks = service.list_tasks(email="JORDAN.LEE@corp.test").items
    observer_default = service.list_tasks(email="observer@corp.test").items
    observer_with_cc = service.list_tasks(email="observer@corp.test", include_cc=True).items
    observer_by_role = service.list_tasks(
        email="observer@corp.test",
        roles=("抄送人",),
    ).items

    assert len(jordan_tasks) == 1
    assert jordan_tasks[0].is_completed is False
    assert jordan_tasks[0].relations == ("负责人", "后端")
    assert observer_default == ()
    assert observer_with_cc[0].relations == ("抄送人",)
    assert observer_by_role[0].relations == ("抄送人",)


def test_my_tasks_exposes_designer_and_figma_link_without_changing_task_relations(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    raw_task = _raw_task()
    raw_task["custom_fields"].extend(
        [
            {
                "name": "设计",
                "type": "users",
                "value": [
                    {"id": 3, "username": "riley han", "email": "riley@corp.test"},
                ],
            },
            {
                "name": "设计稿链接",
                "type": "url",
                "value": "https://www.figma.com/design/FileKey12345/Page?node-id=123-456",
            },
        ]
    )
    service.store.upsert_clickup_task(
        raw_task=raw_task,
        source_path="workspace/coinex/knowledge/requirements/tasks/实现中/task__86task1.md",
    )

    task = service.list_tasks(email="jordan.lee@corp.test").items[0]

    assert task.task.designers == ("riley han",)
    assert task.task.figma_url == "https://www.figma.com/design/FileKey12345/Page?node-id=123-456"
    assert task.relations == ("负责人", "后端")


def test_my_tasks_keeps_designer_when_figma_link_is_missing(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    raw_task = _raw_task()
    raw_task["custom_fields"].append(
        {
            "name": "设计",
            "type": "users",
            "value": [{"id": 3, "username": "riley han"}],
        }
    )
    service.store.upsert_clickup_task(
        raw_task=raw_task,
        source_path="workspace/coinex/knowledge/requirements/tasks/实现中/task__86task1.md",
    )

    task = service.list_tasks(email="jordan.lee@corp.test").items[0]

    assert task.task.designers == ("riley han",)
    assert task.task.figma_url == ""


def test_my_tasks_can_list_all_people_without_user_role_filters(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    service.store.upsert_clickup_task(
        raw_task=_raw_task(),
        source_path="workspace/coinex/knowledge/requirements/tasks/实现中/task__86task1.md",
    )
    other_task = _raw_task(task_id="other-task", title="别人的任务")
    other_task["assignees"] = [
        {
            "id": 3,
            "username": "Other User",
            "email": "other@corp.test",
        }
    ]
    other_task["custom_fields"] = []
    service.store.upsert_clickup_task(
        raw_task=other_task,
        source_path="workspace/coinex/knowledge/requirements/tasks/实现中/other-task.md",
    )

    mine = service.list_tasks(email="jordan.lee@corp.test", scope="all")
    everyone = service.list_tasks(
        email="jordan.lee@corp.test",
        assignment_scope="all",
        roles=("后端",),
        include_cc=True,
        query="别人的任务",
    )

    assert [item.task.task_id for item in mine.items] == ["86task1"]
    assert [item.task.task_id for item in everyone.items] == ["other-task"]
    assert everyone.role_options == ()


def test_my_tasks_deduplicates_by_task_id_and_keeps_latest_clickup_record(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    service.store.upsert_clickup_task(
        raw_task=_raw_task(updated_at="1785292800000", status="待确认"),
        source_path="workspace/coinex/knowledge/requirements/tasks/梳理中/task__86task1.md",
    )
    service.store.upsert_clickup_task(
        raw_task=_raw_task(updated_at="1785379200000", status="实现中"),
        source_path="workspace/coinex/knowledge/requirements/tasks/实现中/task__86task1.md",
    )
    service.store.upsert_clickup_task(
        raw_task=_raw_task(updated_at="1785206400000", status="Closed"),
        source_path="workspace/coinex/knowledge/requirements/tasks/历史迭代/task__86task1.md",
    )

    tasks = service.list_tasks(email="jordan.lee@corp.test", scope="all").items

    assert len(tasks) == 1
    assert tasks[0].task.status == "实现中"
    assert "/实现中/" in tasks[0].task.source_path


def test_my_tasks_detects_and_allows_manual_environment_override(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    service.store.upsert_clickup_task(
        raw_task=_raw_task(),
        source_path="workspace/coinex/knowledge/requirements/tasks/实现中/task__86task1.md",
    )

    detected = service.list_tasks(email="jordan.lee@corp.test").items[0]
    assert detected.task.detected_environments == ("test6",)

    service.set_environment(
        task_id="86task1",
        environment="testadmin3",
        updated_by_email="jordan.lee@corp.test",
    )
    overridden = service.list_tasks(email="jordan.lee@corp.test").items[0]
    assert overridden.manual_environment == "test3"
    assert "testadmin3" not in service.environment_options()

    service.set_environment(
        task_id="86task1",
        environment=None,
        updated_by_email="jordan.lee@corp.test",
    )
    assert service.list_tasks(email="jordan.lee@corp.test").items[0].manual_environment is None

    with pytest.raises(FileNotFoundError, match="与你无关"):
        service.set_environment(
            task_id="86task1",
            environment="test2",
            updated_by_email="unrelated@corp.test",
        )

    service.require_task_access(task_id="86task1", email="jordan.lee@corp.test")
    with pytest.raises(FileNotFoundError, match="与你无关"):
        service.require_task_access(task_id="86task1", email="unrelated@corp.test")


def test_my_tasks_uses_clickup_status_type_for_completed_scope(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    service.store.upsert_clickup_task(
        raw_task=_raw_task(status="自定义完成", status_type="closed"),
        source_path="workspace/coinex/knowledge/requirements/tasks/历史迭代/task__86task1.md",
    )

    assert service.list_tasks(email="jordan.lee@corp.test", scope="active").items == ()
    completed = service.list_tasks(email="jordan.lee@corp.test", scope="completed").items
    assert len(completed) == 1
    assert completed[0].is_completed is True


def test_my_tasks_treats_history_iteration_as_completed(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    service.store.upsert_clickup_task(
        raw_task=_raw_task(
            status="测试中",
            status_type="custom",
            list_name="历史迭代",
        ),
        source_path="workspace/coinex/knowledge/requirements/tasks/历史迭代/task__86task1.md",
    )

    assert service.list_tasks(email="jordan.lee@corp.test", scope="active").items == ()
    completed = service.list_tasks(email="jordan.lee@corp.test", scope="completed").items
    assert len(completed) == 1
    assert completed[0].is_completed is True


def test_my_tasks_paginates_and_returns_scope_counts(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    for index in range(55):
        task_id = f"task{index:02d}"
        service.store.upsert_clickup_task(
            raw_task=_raw_task(
                task_id=task_id,
                title=f"分页任务 {index:02d}",
                updated_at=str(1785292800000 + index),
            ),
            source_path=f"workspace/coinex/knowledge/requirements/tasks/实现中/{task_id}.md",
        )

    first_page = service.list_tasks(
        email="jordan.lee@corp.test",
        scope="all",
        page=1,
        page_size=50,
    )
    second_page = service.list_tasks(
        email="jordan.lee@corp.test",
        scope="all",
        page=2,
        page_size=50,
    )

    assert first_page.total == 55
    assert first_page.total_pages == 2
    assert first_page.page == 1
    assert len(first_page.items) == 50
    assert first_page.items[0].task.task_id == "task54"
    assert second_page.page == 2
    assert len(second_page.items) == 5
    assert second_page.scope_counts == {"active": 55, "completed": 0, "all": 55}


def test_my_tasks_filters_on_server_by_status_role_and_pr_query(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    service.store.upsert_clickup_task(
        raw_task=_raw_task(task_id="active", title="普通任务"),
        source_path="workspace/coinex/knowledge/requirements/tasks/实现中/active.md",
    )
    service.store.upsert_clickup_task(
        raw_task=_raw_task(
            task_id="history",
            title="历史任务",
            status="测试中",
            list_name="历史迭代",
        ),
        source_path="workspace/coinex/knowledge/requirements/tasks/历史迭代/history.md",
    )
    service.pr_link_store.sync_pr_links(
        repo_full_name="example-org/example_backend",
        pr_number=4321,
        links=(
            RequirementPrLink(
                task_id="active",
                repo_full_name="example-org/example_backend",
                pr_number=4321,
                pr_url="https://github.com/example-org/example_backend/pull/4321",
                title="分页关联 PR",
                author_login="author",
                state="open",
                base_ref="main",
                github_updated_at="2026-07-30T00:00:00Z",
                first_seen_at="2026-07-30T00:00:00Z",
                last_seen_at="2026-07-30T00:00:00Z",
            ),
        ),
    )

    completed = service.list_tasks(
        email="jordan.lee@corp.test",
        scope="completed",
        statuses=("测试中",),
    )
    searched = service.list_tasks(
        email="jordan.lee@corp.test",
        scope="all",
        query="分页关联",
    )
    backend_role = service.list_tasks(
        email="jordan.lee@corp.test",
        scope="all",
        roles=("后端",),
    )

    assert [item.task.task_id for item in completed.items] == ["history"]
    assert [item.task.task_id for item in searched.items] == ["active"]
    assert {item.task.task_id for item in backend_role.items} == {"active", "history"}


def test_clickup_sync_indexes_task_even_when_markdown_is_unchanged(tmp_path: Path) -> None:
    tasks_dir = tmp_path / "workspace/coinex/knowledge/requirements/tasks/实现中"
    tasks_dir.mkdir(parents=True)
    destination = tasks_dir / "AI 任务看板__86task1.md"
    destination.write_text("# 已存在", encoding="utf-8")
    destination.touch()
    store = MyTaskStore(str(tmp_path / "assistant.sqlite3"))
    stats = SyncStats()

    _sync_raw_tasks(
        [_raw_task(updated_at="1")],
        tasks_dir,
        stats,
        force=False,
        include_task_id=True,
        task_store=store,
        base_dir=tmp_path,
    )

    assert stats.skipped == 1
    assert len(store.list_for_email(email="jordan.lee@corp.test")) == 1


def test_task_pr_assistant_access_requires_exact_link_not_user_relation(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    service.store.upsert_clickup_task(
        raw_task=_raw_task(),
        source_path="workspace/coinex/knowledge/requirements/tasks/实现中/task__86task1.md",
    )
    service.pr_link_store.sync_pr_links(
        repo_full_name="example-org/example_backend",
        pr_number=1234,
        links=(
            RequirementPrLink(
                task_id="86task1",
                repo_full_name="example-org/example_backend",
                pr_number=1234,
                pr_url="https://github.com/example-org/example_backend/pull/1234",
                title="PR",
                author_login="author",
                state="open",
                base_ref="main",
                github_updated_at="2026-07-30T00:00:00Z",
                first_seen_at="2026-07-30T00:00:00Z",
                last_seen_at="2026-07-30T00:00:00Z",
            ),
        ),
    )

    link = service.require_pull_request_access(
        task_id="86task1",
        repo_full_name="example-org/example_backend",
        pr_number=1234,
    )
    assert link.pr_number == 1234

    with pytest.raises(FileNotFoundError, match="未关联"):
        service.require_pull_request_access(
            task_id="86task1",
            repo_full_name="example-org/example_backend",
            pr_number=4321,
        )
    outsider_link = service.require_pull_request_access(
        task_id="86task1",
        repo_full_name="example-org/example_backend",
        pr_number=1234,
    )
    assert outsider_link.pr_number == 1234


def test_task_pr_assistant_session_defers_remote_preparation(tmp_path: Path) -> None:
    service = _build_service(tmp_path)
    service.store.upsert_clickup_task(
        raw_task=_raw_task(),
        source_path="workspace/coinex/knowledge/requirements/tasks/实现中/task__86task1.md",
    )
    service.pr_link_store.sync_pr_links(
        repo_full_name="example-org/example_backend",
        pr_number=1234,
        links=(
            RequirementPrLink(
                task_id="86task1",
                repo_full_name="example-org/example_backend",
                pr_number=1234,
                pr_url="https://github.com/example-org/example_backend/pull/1234",
                title="PR",
                author_login="author",
                state="open",
                base_ref="main",
                github_updated_at="2026-07-30T00:00:00Z",
                first_seen_at="2026-07-30T00:00:00Z",
                last_seen_at="2026-07-30T00:00:00Z",
            ),
        ),
    )
    assistant_calls = []
    assistant_service = SimpleNamespace(
        create_pull_request_session=lambda user, **kwargs: (
            assistant_calls.append((user, kwargs))
            or SimpleNamespace(session_id="session-1")
        )
    )
    catalog = SimpleNamespace(
        get=lambda repo_full_name: (
            pytest.fail("查询仓库配置时不应访问远程")
            if repo_full_name != "example-org/example_backend"
            else SimpleNamespace(organization_key="coinex")
        )
    )
    current_user = SimpleNamespace(
        user_id="user-1",
        email="outsider@corp.test",
    )

    response = create_pull_request_assistant_session(
        task_id="86task1",
        payload=MyTaskAssistantSessionRequest(
            repo_full_name="example-org/example_backend",
            pr_number=1234,
        ),
        current_user=current_user,
        service=service,
        repository_catalog=catalog,
        assistant_service=assistant_service,
    )

    assert response.session_id == "session-1"
    assert response.resolved_sha is None
    assert assistant_calls == [
        (
            current_user,
            {
                "task_id": "86task1",
                "repo_full_name": "example-org/example_backend",
                "pr_number": 1234,
                "resolved_sha": None,
                "organization_key": "coinex",
            },
        )
    ]
