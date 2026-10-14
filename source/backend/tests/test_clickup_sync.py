import importlib
from pathlib import Path

from app.business.my_tasks import MyTaskStore
from app.jobs import clickup_sync as clickup_job_sync
from app.jobs.clickup_sync import _parse_task_list_specs

clickup_script_sync = importlib.import_module("app.utils.clickup.sync")


def test_parse_task_list_specs_keeps_duplicate_output_dirs() -> None:
    specs = [
        "实现中=901000001663",
        "梳理中=901000001842",
        "梳理中=901000001848",
        "历史迭代=901000001856",
        "https://acme.clickup.com/9000000001/v/l/8cabcde-6638",
    ]

    assert _parse_task_list_specs(specs) == [
        ("实现中", "901000001663"),
        ("梳理中", "901000001842"),
        ("梳理中", "901000001848"),
        ("历史迭代", "901000001856"),
        ("8cabcde-6638", "8cabcde-6638"),
    ]


def test_relocate_stale_list_task_files_moves_history_and_deleted(monkeypatch, tmp_path: Path) -> None:
    tasks_dir = tmp_path / "tasks" / "实现中"
    tasks_dir.mkdir(parents=True)
    active_file = tasks_dir / "活跃任务__task3.md"
    history_file = tasks_dir / "历史任务__task1.md"
    deleted_file = tasks_dir / "删除任务__task2.md"
    other_file = tasks_dir / "其他列表任务__task4.md"
    for task_file in (active_file, history_file, deleted_file, other_file):
        task_file.write_text("# task", encoding="utf-8")

    remote_states = {
        "task1": ("exists", {"list": {"name": "历史迭代"}}),
        "task2": ("deleted", None),
        "task4": ("exists", {"list": {"name": "其他列表"}}),
    }

    monkeypatch.setattr(clickup_script_sync, "_fetch_remote_task_state", lambda task_id: remote_states[task_id])

    stats = clickup_script_sync.SyncStats()
    clickup_script_sync._relocate_stale_list_task_files(tasks_dir, {"task3"}, stats)

    assert active_file.exists()
    assert not history_file.exists()
    assert not deleted_file.exists()
    assert other_file.exists()
    assert (tmp_path / "tasks" / "历史迭代" / history_file.name).exists()
    assert (tmp_path / "tasks" / "_deleted" / deleted_file.name).exists()


def test_history_iteration_sync_indexes_without_fetching_comments_or_writing_markdown(
    monkeypatch,
    tmp_path: Path,
) -> None:
    raw_task = {
        "id": "task1",
        "name": "历史任务",
        "date_updated": "1785292800000",
        "status": {"status": "测试中", "type": "custom"},
        "assignees": [
            {
                "username": "Jordan（张三）",
                "email": "jordan.lee@corp.test",
            }
        ],
        "custom_fields": [],
    }
    monkeypatch.setattr(
        clickup_script_sync,
        "fetch_list_info",
        lambda list_id: {"id": list_id, "name": "历史迭代"},
    )
    monkeypatch.setattr(
        clickup_script_sync,
        "fetch_list_tasks",
        lambda list_id: [raw_task],
    )
    monkeypatch.setattr(
        clickup_script_sync,
        "fetch_list_all_tasks",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("不应拉取历史任务评论")),
    )

    tasks_dir = tmp_path / "workspace/coinex/knowledge/requirements/tasks/历史迭代"
    store = MyTaskStore(str(tmp_path / "assistant.sqlite3"))
    stats = clickup_script_sync.SyncStats()

    task_ids = clickup_script_sync._sync_list_tasks(
        "history-list",
        tasks_dir,
        stats,
        force=False,
        task_store=store,
        base_dir=tmp_path,
    )

    assert task_ids == {"task1"}
    assert stats.skipped == 1
    assert len(store.list_for_email(email="jordan.lee@corp.test")) == 1
    assert not list(tasks_dir.glob("*.md"))


def test_clickup_job_syncs_figma_assets_for_changed_requirement_and_linked_doc(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from app.utils.figma import FigmaAssetSyncResult

    requirements_root = tmp_path / "requirements"
    task_file = requirements_root / "tasks/实现中/设计需求.md"
    linked_doc = requirements_root / "tasks/实现中/linked_docs/设计说明/_index.md"
    task_file.parent.mkdir(parents=True)
    linked_doc.parent.mkdir(parents=True)
    figma_url = "https://www.figma.com/design/FileKey12345/Page?node-id=1-2"
    task_file.write_text(f"设计稿：{figma_url}", encoding="utf-8")
    linked_doc.write_text(f"详细设计：{figma_url}", encoding="utf-8")
    calls: list[tuple[str, Path]] = []

    def fake_sync(content: str, *, images_dir: Path, **_: object) -> FigmaAssetSyncResult:
        calls.append((content, images_dir))
        return FigmaAssetSyncResult(content=f"{content}\n\n## Figma 设计稿（本地同步）\n")

    monkeypatch.setattr(clickup_job_sync, "sync_requirement_figma_assets", fake_sync)
    errors = clickup_job_sync._sync_figma_assets_for_changed_requirements(
        requirements_root,
        [
            "tasks/实现中/设计需求.md",
            "tasks/实现中/linked_docs/设计说明/_index.md",
        ],
    )

    assert errors == 0
    assert len(calls) == 2
    assert {images_dir for _, images_dir in calls} == {requirements_root / "tasks/实现中/images"}
    assert "## Figma 设计稿（本地同步）" in task_file.read_text(encoding="utf-8")
    assert "## Figma 设计稿（本地同步）" in linked_doc.read_text(encoding="utf-8")
