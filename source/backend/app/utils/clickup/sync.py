"""
ClickUp 增量同步核心库。

通过对比本地文件的修改时间（mtime）与 ClickUp API 返回的 date_updated，
只下载有变化的内容，不维护额外的状态文件。
"""

import re
from pathlib import Path
from typing import List, Optional

import click
import requests

from app.utils.clickup.export_task import (
    CLICKUP_FOLDER_URL_PATTERN,
    DEFAULT_REQUIREMENTS_ROOT,
    _base_v2,
    _download_images,
    _headers,
    _load_env,
    _normalize,
    _render_doc_page,
    _safe_filename,
    extract_folder_id,
    extract_list_id,
    fetch_doc_page,
    fetch_folder_all_tasks,
    fetch_folder_docs,
    fetch_folder_info,
    fetch_list_tasks,
    fetch_list_all_tasks,
    fetch_list_info,
    normalize_task,
    render_markdown,
)
from app.business.my_tasks import MyTaskStore

_TASK_ID_SUFFIX_PATTERN = re.compile(r"__(?P<task_id>[A-Za-z0-9]+)\.md$")
_HISTORY_ITERATION_NAME = "历史迭代"


# ---------------------------------------------------------------------------
# 增量判断：用本地文件 mtime 与 API date_updated 对比
# ---------------------------------------------------------------------------

def _needs_update(dest: Path, api_date_seconds: float, force: bool) -> bool:
    """
    dest 不存在，或 mtime 早于 api_date_seconds，或 force=True 时返回 True。
    api_date_seconds 统一传入秒级浮点数。
    """
    if force or not dest.exists():
        return True
    return dest.stat().st_mtime < api_date_seconds


# ---------------------------------------------------------------------------
# 带 date_updated 的页面树展开
# ---------------------------------------------------------------------------

def _extract_pages_with_date(
    pages: list,
    depth: int = 0,
    rel_dir: Path = Path("."),
) -> List[dict]:
    result = []
    for page in pages:
        safe = _safe_filename(page.get("name", "unnamed"))
        sub = page.get("pages") or []
        result.append({
            "id": page.get("id"),
            "name": page.get("name", ""),
            "date_updated": page.get("date_updated", 0),  # 秒级整数
            "_depth": depth,
            "_rel_dir": rel_dir / safe if sub else rel_dir,
            "_safe_name": "_index" if sub else safe,
        })
        if sub:
            result.extend(_extract_pages_with_date(sub, depth + 1, rel_dir / safe))
    return result


def _fetch_pages_with_date(doc_id: str) -> List[dict]:
    r = requests.get(
        f"{_base_v2()}/doc/{doc_id}/page",
        headers=_headers(),
        timeout=60,
    )
    if r.status_code >= 400:
        return []
    return _extract_pages_with_date(r.json().get("pages") or [])


# ---------------------------------------------------------------------------
# 同步统计
# ---------------------------------------------------------------------------

class SyncStats:
    def __init__(self) -> None:
        self.added = 0
        self.updated = 0
        self.skipped = 0
        self.errors = 0

    def print_summary(self) -> None:
        parts = [f"新增 {self.added} 个", f"更新 {self.updated} 个", f"跳过 {self.skipped} 个"]
        if self.errors:
            parts.append(f"失败 {self.errors} 个")
        click.echo(f"\n同步完成: {', '.join(parts)}")


def _parse_task_list_specs(raw_specs: list[str]) -> list[tuple[str, str]]:
    """解析 CLICKUP_TASK_LISTS 配置项，返回 [(output_name, list_id), ...] 列表。
    格式：'目录名=list_id' 或纯 list_id（目录名默认为 list_id 本身）。
    """
    parsed: list[tuple[str, str]] = []
    for raw_spec in raw_specs:
        spec = raw_spec.strip()
        if not spec:
            continue
        if "=" in spec:
            output_name, list_value = spec.split("=", 1)
        else:
            list_value = spec
            output_name = extract_list_id(list_value)
        output_name = output_name.strip()
        list_id = extract_list_id(list_value.strip())
        if output_name and list_id:
            parsed.append((output_name, list_id))
    return parsed


# ---------------------------------------------------------------------------
# 核心同步逻辑
# ---------------------------------------------------------------------------

def _sync_tasks(
    folder_id: str,
    tasks_dir: Path,
    stats: SyncStats,
    force: bool,
    task_store: MyTaskStore | None = None,
    base_dir: Path | None = None,
) -> None:
    click.echo("同步 Tasks ...", err=True)
    raw_tasks = fetch_folder_all_tasks(folder_id)
    if not raw_tasks:
        click.echo("  没有找到任何 Task。", err=True)
        return

    _sync_raw_tasks(
        raw_tasks,
        tasks_dir,
        stats,
        force,
        include_task_id=False,
        task_store=task_store,
        base_dir=base_dir,
    )


def _sync_list_tasks(
    list_id: str,
    tasks_dir: Path,
    stats: SyncStats,
    force: bool,
    task_store: MyTaskStore | None = None,
    base_dir: Path | None = None,
) -> set[str]:
    list_info = fetch_list_info(list_id)
    list_name = _normalize(list_info.get("name") or list_id)
    click.echo(f"同步 List Tasks [{list_name}] ({list_id}) ...", err=True)
    if list_name == _HISTORY_ITERATION_NAME:
        raw_tasks = fetch_list_tasks(list_id)
        for raw in raw_tasks:
            raw["_list_name"] = list_name
        _sync_raw_tasks(
            raw_tasks,
            tasks_dir,
            stats,
            force,
            include_task_id=True,
            task_store=task_store,
            base_dir=base_dir,
            index_only=True,
        )
        return {str(raw.get("id")) for raw in raw_tasks if raw.get("id")}

    raw_tasks = fetch_list_all_tasks(list_id, list_name=list_name)
    if not raw_tasks:
        click.echo("  没有找到任何 Task。", err=True)
        return set()

    _sync_raw_tasks(
        raw_tasks,
        tasks_dir,
        stats,
        force,
        include_task_id=True,
        task_store=task_store,
        base_dir=base_dir,
    )
    return {str(raw.get("id")) for raw in raw_tasks if raw.get("id")}


def _relocate_stale_list_task_files(
    tasks_dir: Path,
    current_task_ids: set[str],
    stats: SyncStats,
) -> None:
    if not tasks_dir.exists() or not tasks_dir.is_dir():
        return

    tasks_root = tasks_dir.parent
    for task_file in sorted(tasks_dir.glob("*.md")):
        task_id = _extract_task_id_from_task_file(task_file)
        if not task_id or task_id in current_task_ids:
            continue

        try:
            remote_state, remote_task = _fetch_remote_task_state(task_id)
        except Exception as e:
            stats.errors += 1
            click.echo(f"  [清理失败] task {task_id}: {e}", err=True)
            continue

        if remote_state == "deleted":
            dest = _move_task_file(task_file, tasks_root / "_deleted")
            click.echo(f"  [已移至 _deleted] {dest.name}", err=True)
            continue

        if remote_task and _is_history_iteration_task(remote_task):
            dest = _move_task_file(task_file, tasks_root / _HISTORY_ITERATION_NAME)
            click.echo(f"  [已移至历史迭代] {dest.name}", err=True)
            continue

        list_name = _remote_task_list_name(remote_task) if remote_task else "未知列表"
        click.echo(f"  [保留] task {task_id} 已不在当前列表，远端位置: {list_name}", err=True)


def _extract_task_id_from_task_file(task_file: Path) -> Optional[str]:
    match = _TASK_ID_SUFFIX_PATTERN.search(task_file.name)
    if not match:
        return None
    return match.group("task_id")


def _fetch_remote_task_state(task_id: str) -> tuple[str, Optional[dict]]:
    response = requests.get(
        f"{_base_v2()}/task/{task_id}",
        headers=_headers(),
        timeout=30,
    )
    if response.status_code == 404:
        return "deleted", None
    if response.status_code >= 400:
        raise click.ClickException(
            f"ClickUp API 请求失败 (task/{task_id}): "
            f"HTTP {response.status_code} {response.text[:500]}"
        )
    return "exists", response.json()


def _remote_task_list_name(task: Optional[dict]) -> str:
    if not isinstance(task, dict):
        return "未知列表"
    list_data = task.get("list") if isinstance(task.get("list"), dict) else {}
    return _normalize(list_data.get("name") or list_data.get("id")) or "未知列表"


def _is_history_iteration_task(task: dict) -> bool:
    candidate_names = []
    for key in ("list", "folder", "space"):
        value = task.get(key)
        if isinstance(value, dict):
            candidate_names.append(_normalize(value.get("name") or value.get("id")))
    return any(_HISTORY_ITERATION_NAME in name for name in candidate_names if name)


def _move_task_file(task_file: Path, target_dir: Path) -> Path:
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / task_file.name
    if dest.exists():
        task_file.unlink()
        return dest
    task_file.rename(dest)
    return dest


def _sync_raw_tasks(
    raw_tasks: List[dict],
    tasks_dir: Path,
    stats: SyncStats,
    force: bool,
    *,
    include_task_id: bool,
    task_store: MyTaskStore | None = None,
    base_dir: Path | None = None,
    index_only: bool = False,
) -> None:
    tasks_dir.mkdir(parents=True, exist_ok=True)
    images_dir = tasks_dir / "images"
    linked_docs_dir = tasks_dir / "linked_docs"

    for raw in raw_tasks:
        task_id = raw.get("id", "unknown")
        # v2 API 返回毫秒字符串，转为秒级浮点
        api_date = int(raw.get("date_updated", "0")) / 1000

        safe_title = _safe_filename(raw.get("name") or task_id)
        suffix = f"__{task_id}" if include_task_id else ""
        dest = tasks_dir / f"{safe_title}{suffix}.md"

        if task_store is not None:
            source_path = _relative_source_path(dest, base_dir=base_dir)
            task_store.upsert_clickup_task(raw_task=raw, source_path=source_path)

        if index_only:
            stats.skipped += 1
            continue

        if not _needs_update(dest, api_date, force):
            stats.skipped += 1
            continue

        is_new = not dest.exists()
        try:
            task = normalize_task(
                task_id,
                raw,
                images_dir=images_dir,
                linked_docs_dir=linked_docs_dir,
                markdown_dir=tasks_dir,
            )
            content = render_markdown(task)
            dest.write_text(content, encoding="utf-8")

            if is_new:
                stats.added += 1
                click.echo(f"  [新增] {task['title']}", err=True)
            else:
                stats.updated += 1
                click.echo(f"  [更新] {task['title']}", err=True)
        except Exception as e:
            stats.errors += 1
            click.echo(f"  [失败] task {task_id}: {e}", err=True)


def _sync_grouped_task_lists(
    task_list_specs: list[str],
    tasks_root: Path,
    stats: SyncStats,
    force: bool,
    task_store: MyTaskStore | None = None,
    base_dir: Path | None = None,
) -> None:
    grouped: dict[str, list[str]] = {}
    for output_name, list_id in _parse_task_list_specs(task_list_specs):
        grouped.setdefault(output_name, []).append(list_id)
    for output_name, list_ids in grouped.items():
        output_dir = tasks_root / _safe_filename(output_name)
        current_ids: set[str] = set()
        for list_id in list_ids:
            current_ids.update(
                _sync_list_tasks(
                    list_id,
                    output_dir,
                    stats,
                    force,
                    task_store=task_store,
                    base_dir=base_dir,
                )
            )
        _relocate_stale_list_task_files(output_dir, current_ids, stats)


def _relative_source_path(path: Path, *, base_dir: Path | None) -> str:
    if base_dir is not None:
        try:
            return path.resolve().relative_to(base_dir.resolve()).as_posix()
        except ValueError:
            pass
    return path.as_posix()


def _sync_docs(
    workspace_id: str,
    folder_id: str,
    docs_dir: Path,
    stats: SyncStats,
    force: bool,
) -> None:
    click.echo("同步 Docs ...", err=True)
    docs = fetch_folder_docs(workspace_id, folder_id)
    if not docs:
        click.echo("  没有找到任何 Doc。", err=True)
        return

    docs_dir.mkdir(parents=True, exist_ok=True)
    images_dir = docs_dir / "images"

    for doc in docs:
        doc_id = doc.get("id", "")
        doc_name = _normalize(doc.get("name") or doc_id)
        click.echo(f"  Doc [{doc_name}] ({doc_id})", err=True)

        pages = _fetch_pages_with_date(doc_id)
        if not pages:
            click.echo("    无页面，跳过。", err=True)
            continue

        doc_dir = docs_dir / _safe_filename(doc_name)

        for page in pages:
            page_id = page["id"]
            api_date = float(page["date_updated"])  # 已是秒级整数

            page_dir = doc_dir / page["_rel_dir"]
            dest = page_dir / f"{page['_safe_name']}.md"

            if not _needs_update(dest, api_date, force):
                stats.skipped += 1
                continue

            is_new = not dest.exists()
            try:
                page_dir.mkdir(parents=True, exist_ok=True)
                page_data = fetch_doc_page(workspace_id, doc_id, page_id)
                raw_content = _normalize(page_data.get("content") or "")
                raw_content = _download_images(raw_content, images_dir, page_dir)
                dest.write_text(
                    _render_doc_page(page["name"], page_id, doc_id, workspace_id, raw_content),
                    encoding="utf-8",
                )

                if is_new:
                    stats.added += 1
                    click.echo(f"    [新增] {page['name']}", err=True)
                else:
                    stats.updated += 1
                    click.echo(f"    [更新] {page['name']}", err=True)
            except Exception as e:
                stats.errors += 1
                click.echo(f"    [失败] page {page_id}: {e}", err=True)
