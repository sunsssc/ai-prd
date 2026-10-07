"""
ClickUp 增量同步后台任务。

在 FastAPI lifespan 启动时通过 asyncio.create_task 运行，
每隔 interval_seconds 秒执行一次增量同步。
同步逻辑复用 app.utils.clickup 中的实现。
"""

import asyncio
import difflib
import logging
import os
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from time import perf_counter

from app.business.requirement_reviews import RequirementReviewChangeAssessment, RequirementReviewService
from app.business.my_tasks import MyTaskStore
from app.business.requirement_reviews.service import (
    is_auto_review_ignored_requirement_path,
    normalize_auto_review_source_markdown,
)
from app.core.config import settings
from app.jobs.sync_state import write_sync_state
from app.utils.clickup.export_task import _load_env, invalidate_doc_page_index_cache
from app.utils.clickup.sync import SyncStats, _parse_task_list_specs, _sync_docs, _sync_grouped_task_lists
from app.utils.figma import sync_requirement_figma_assets

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MarkdownSnapshot:
    mtime: float
    text: str
    raw_text: str


AUTO_REVIEW_MIN_ORDINARY_CHANGED_CHARS = 20
AUTO_REVIEW_MAX_ORDINARY_CHANGED_CHARS = 200
AUTO_REVIEW_ORDINARY_CHANGE_RATIO = 0.01


def _snapshot_markdown_files(root: Path) -> dict[str, MarkdownSnapshot]:
    snapshot: dict[str, MarkdownSnapshot] = {}
    if not root.exists() or not root.is_dir():
        return snapshot

    for path in root.rglob("*.md"):
        if path.is_file():
            relative_path = path.relative_to(root).as_posix()
            if is_auto_review_ignored_requirement_path(relative_path):
                continue
            raw_text = path.read_text(encoding="utf-8", errors="replace")
            snapshot[path.as_posix()] = MarkdownSnapshot(
                mtime=path.stat().st_mtime,
                text=normalize_auto_review_source_markdown(raw_text),
                raw_text=raw_text,
            )
    return snapshot


def _collect_changed_files(
    before: dict[str, MarkdownSnapshot],
    after: dict[str, MarkdownSnapshot],
    base_path: Path,
) -> tuple[list[str], dict[str, RequirementReviewChangeAssessment]]:
    changed: list[str] = []
    change_assessments: dict[str, RequirementReviewChangeAssessment] = {}
    for path, snapshot in after.items():
        before_snapshot = before.get(path)
        if before_snapshot is None or before_snapshot.text != snapshot.text:
            relative_path = Path(path).relative_to(base_path).as_posix()
            changed.append(relative_path)
            change_assessments[relative_path] = _assess_review_change(
                before_snapshot.text if before_snapshot is not None else "",
                snapshot.text,
                is_new_file=before_snapshot is None,
            )
    return sorted(changed), change_assessments


def _collect_raw_changed_files(
    before: dict[str, MarkdownSnapshot],
    after: dict[str, MarkdownSnapshot],
    base_path: Path,
) -> list[str]:
    return sorted(
        Path(path).relative_to(base_path).as_posix()
        for path, snapshot in after.items()
        if path not in before or before[path].raw_text != snapshot.raw_text
    )


def _count_changed_characters(before: str, after: str) -> int:
    total = 0
    matcher = difflib.SequenceMatcher(a=before, b=after, autojunk=False)
    for tag, before_start, before_end, after_start, after_end in matcher.get_opcodes():
        if tag == "equal":
            continue
        before_count = _count_non_whitespace_characters(before[before_start:before_end])
        after_count = _count_non_whitespace_characters(after[after_start:after_end])
        total += max(before_count, after_count) if tag == "replace" else before_count + after_count
    return total


def _assess_review_change(before: str, after: str, *, is_new_file: bool = False) -> RequirementReviewChangeAssessment:
    normalized_before = normalize_auto_review_source_markdown(before)
    normalized_after = normalize_auto_review_source_markdown(after)
    changed_characters = _count_changed_characters(normalized_before, normalized_after)
    ordinary_threshold = _ordinary_change_threshold(normalized_after)
    return RequirementReviewChangeAssessment(
        changed_characters=changed_characters,
        ordinary_threshold=ordinary_threshold,
        should_auto_review=changed_characters >= ordinary_threshold,
        is_new_file=is_new_file,
    )


def _count_non_whitespace_characters(text: str) -> int:
    return sum(1 for char in text if not char.isspace())


def _ordinary_change_threshold(text: str) -> int:
    document_size = _count_non_whitespace_characters(text)
    dynamic_threshold = ceil(document_size * AUTO_REVIEW_ORDINARY_CHANGE_RATIO)
    return min(
        max(dynamic_threshold, AUTO_REVIEW_MIN_ORDINARY_CHANGED_CHARS),
        AUTO_REVIEW_MAX_ORDINARY_CHANGED_CHARS,
    )


def _run_sync(
    folder_id: str,
    workspace_id: str,
    docs_dir: Path,
    task_list_specs: list[str],
) -> tuple[SyncStats, list[str], dict[str, RequirementReviewChangeAssessment]]:
    """执行一次增量同步（同步阻塞函数，由 asyncio.to_thread 调用）。

    docs_dir: 对应 knowledge_requirements_root/docs，docs 直接写入此目录。
              CLICKUP_TASK_LISTS 写入 knowledge_requirements_root/tasks/<配置目录名>/。
    """
    if settings.clickup_api_token and not os.environ.get("CLICKUP_API_TOKEN"):
        os.environ["CLICKUP_API_TOKEN"] = settings.clickup_api_token
    _load_env()

    requirements_root = docs_dir.parent
    before = _snapshot_markdown_files(requirements_root)

    stats = SyncStats()
    if task_list_specs:
        _sync_grouped_task_lists(
            task_list_specs,
            requirements_root / "tasks",
            stats,
            force=False,
            task_store=MyTaskStore(settings.assistant_db_path),
            base_dir=Path(settings.ai_working_directory),
        )
    _sync_docs(workspace_id, folder_id, docs_dir, stats, force=False)

    clickup_snapshot = _snapshot_markdown_files(requirements_root)
    clickup_changed_files = _collect_raw_changed_files(before, clickup_snapshot, requirements_root)
    stats.errors += _sync_figma_assets_for_changed_requirements(requirements_root, clickup_changed_files)

    after = _snapshot_markdown_files(requirements_root)
    changed_files, change_assessments = _collect_changed_files(before, after, requirements_root)
    if changed_files:
        invalidate_doc_page_index_cache(docs_dir)
    return stats, changed_files, change_assessments


def _sync_figma_assets_for_changed_requirements(requirements_root: Path, changed_files: list[str]) -> int:
    errors = 0
    for relative_path in changed_files:
        file_path = requirements_root / relative_path
        if not file_path.is_file():
            continue
        content = file_path.read_text(encoding="utf-8", errors="replace")
        result = sync_requirement_figma_assets(
            content,
            images_dir=_figma_images_dir(requirements_root, file_path),
            markdown_dir=file_path.parent,
            token=settings.figma_access_token,
            request_interval_seconds=settings.figma_request_interval_seconds,
            max_retries=settings.figma_max_retries,
            retry_base_delay_seconds=settings.figma_retry_base_delay_seconds,
        )
        errors += result.errors
        if result.content != content:
            file_path.write_text(result.content, encoding="utf-8")
    return errors


def _figma_images_dir(requirements_root: Path, file_path: Path) -> Path:
    relative_path = file_path.resolve().relative_to(requirements_root.resolve())
    if relative_path.parts and relative_path.parts[0] == "docs":
        return requirements_root / "docs" / "images"
    if len(relative_path.parts) >= 2 and relative_path.parts[0] == "tasks":
        return requirements_root / "tasks" / relative_path.parts[1] / "images"
    return file_path.parent / "images"


async def clickup_sync_loop(
    folder_id: str,
    workspace_id: str,
    docs_dir: Path,
    runtime_base_dir: Path,
    interval_seconds: int,
    task_list_specs: list[str] | None = None,
    review_service: RequirementReviewService | None = None,
) -> None:
    """
    后台定时同步循环。在 lifespan 中通过 asyncio.create_task 启动。
    首次启动时立即执行一次，之后每隔 interval_seconds 秒执行一次。
    单次同步出错时记录日志并继续，不中断循环。
    """
    logger.info(
        "ClickUp 同步任务已启动，folder=%s，workspace=%s，task_lists=%d，间隔=%ds，docs=%s",
        folder_id,
        workspace_id,
        len(task_list_specs or []),
        interval_seconds,
        docs_dir,
    )
    while True:
        started_at = perf_counter()
        try:
            stats, changed_files, change_assessments = await asyncio.to_thread(
                _run_sync,
                folder_id,
                workspace_id,
                docs_dir,
                list(task_list_specs or []),
            )
            write_sync_state(
                base_dir=runtime_base_dir,
                scope="requirements",
                added=stats.added,
                updated=stats.updated,
                changed_files=changed_files,
                errors=stats.errors,
            )
            review_jobs_needed = 0
            active_review_jobs = {"pending": 0, "running": 0, "active": 0}
            if review_service is not None and changed_files:
                jobs = review_service.enqueue_changed_files(
                    changed_files,
                    change_assessments=change_assessments,
                )
                review_jobs_needed = len(jobs)
                active_review_jobs = review_service.count_active_jobs()
                if jobs:
                    asyncio.create_task(review_service.process_pending_jobs(limit=len(jobs)))
            elif review_service is not None:
                active_review_jobs = review_service.count_active_jobs()
            elapsed_ms = (perf_counter() - started_at) * 1000
            fetched_count = stats.added + stats.updated + stats.skipped + stats.errors
            logger.info(
                (
                    "ClickUp 同步完成：耗时 %.1fms，读取需求 %d 个，新增 %d 个，更新 %d 个，"
                    "跳过 %d 个，失败 %d 个，变更文件 %d 个，需要 review %d 个，"
                    "当前 review 任务 %d 个（待执行 %d 个，执行中 %d 个）"
                ),
                elapsed_ms,
                fetched_count,
                stats.added,
                stats.updated,
                stats.skipped,
                stats.errors,
                len(changed_files),
                review_jobs_needed,
                active_review_jobs["active"],
                active_review_jobs["pending"],
                active_review_jobs["running"],
            )
        except Exception:
            logger.exception("ClickUp 同步出错，下次将继续重试")
        await asyncio.sleep(interval_seconds)
