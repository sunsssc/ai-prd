from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from urllib.parse import quote, unquote, urlencode, urlparse
from uuid import uuid4

from app.integrations.agent_runtime import AgentRuntimeClient
from app.utils.clickup.comments import ClickUpCommentClient


REVIEW_TYPE = "tech_review"
SKILL_NAME = "requirement-check"
CLICKUP_REVIEW_ARCHIVE_DOC_NAME = "需求评审归档"
AUTO_REVIEW_REQUIRED_TASK_STATUS = "待确认"
AUTO_REVIEW_EXCLUDED_TASK_LIST_KEYWORD = "梳理中"
AUTO_REVIEW_IGNORED_SECTION_TITLES = frozenset({"评论"})
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RequirementReviewJob:
    job_id: str
    source_path: str
    source_hash: str
    review_type: str
    skill_name: str
    status: str
    review_id: str | None
    review_path: str | None
    error_message: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True)
class RequirementReviewClickUpPublishJob:
    job_id: str
    review_id: str
    review_path: str
    source_path: str
    task_id: str
    workspace_id: str
    folder_id: str
    doc_id: str | None
    page_id: str | None
    doc_url: str | None
    status: str
    error_message: str | None
    attempts: int
    created_at: str
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True)
class RequirementReviewRecord:
    review_id: str
    review_type: str
    path: str
    source_path: str
    source_paths: tuple[str, ...]
    source_hash: str
    status: str
    risk_level: str
    created_at: str
    completed_at: str | None
    is_read: bool = False


@dataclass(frozen=True)
class RequirementReviewDetail:
    review_id: str
    review_type: str
    path: str
    source_path: str
    source_paths: tuple[str, ...]
    source_hash: str
    status: str
    risk_level: str
    created_at: str
    completed_at: str | None
    is_read: bool
    source_type: str
    source_meta_path: str
    skill: str
    content: str
    frontmatter: dict[str, str]


@dataclass(frozen=True)
class RequirementReviewChangeAssessment:
    changed_characters: int
    ordinary_threshold: int
    should_auto_review: bool
    is_new_file: bool = False


class RequirementReviewStore:
    busy_timeout_ms = 5000

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self._lock = Lock()
        self._initialize()

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS requirement_review_reads (
                    read_id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL,
                    review_path TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    read_at TEXT NOT NULL,
                    UNIQUE(review_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS requirement_review_jobs (
                    job_id TEXT PRIMARY KEY,
                    source_path TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    review_type TEXT NOT NULL,
                    skill_name TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed')),
                    review_id TEXT,
                    review_path TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_requirement_review_reads_user
                ON requirement_review_reads(user_id, review_id);

                CREATE INDEX IF NOT EXISTS idx_requirement_review_jobs_status_created
                ON requirement_review_jobs(status, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_requirement_review_jobs_source
                ON requirement_review_jobs(source_path, source_hash, review_type, status);

                CREATE TABLE IF NOT EXISTS requirement_review_clickup_publish_jobs (
                    job_id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL,
                    review_path TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    task_id TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    folder_id TEXT NOT NULL,
                    doc_id TEXT,
                    page_id TEXT,
                    doc_url TEXT,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed')),
                    error_message TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE(review_id, task_id)
                );

                CREATE INDEX IF NOT EXISTS idx_requirement_review_clickup_publish_jobs_status_created
                ON requirement_review_clickup_publish_jobs(status, created_at ASC);
                """
            )

    def create_job(self, *, source_path: str, source_hash: str, review_type: str = REVIEW_TYPE, skill_name: str = SKILL_NAME) -> RequirementReviewJob:
        now = _utc_now()
        existing = self.get_active_job(source_path=source_path, source_hash=source_hash, review_type=review_type)
        if existing is not None:
            return existing

        job = RequirementReviewJob(
            job_id=str(uuid4()),
            source_path=source_path,
            source_hash=source_hash,
            review_type=review_type,
            skill_name=skill_name,
            status="pending",
            review_id=None,
            review_path=None,
            error_message=None,
            created_at=now,
            started_at=None,
            completed_at=None,
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO requirement_review_jobs (
                    job_id, source_path, source_hash, review_type, skill_name, status,
                    review_id, review_path, error_message, created_at, started_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.source_path,
                    job.source_hash,
                    job.review_type,
                    job.skill_name,
                    job.status,
                    job.review_id,
                    job.review_path,
                    job.error_message,
                    job.created_at,
                    job.started_at,
                    job.completed_at,
                ),
            )
        return job

    def get_active_job(self, *, source_path: str, source_hash: str, review_type: str) -> RequirementReviewJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM requirement_review_jobs
                WHERE source_path = ?
                  AND source_hash = ?
                  AND review_type = ?
                  AND status IN ('pending', 'running')
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (source_path, source_hash, review_type),
            ).fetchone()
        return self._row_to_job(row)

    def get_job(self, job_id: str) -> RequirementReviewJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM requirement_review_jobs
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        return self._row_to_job(row)

    def list_pending_jobs(self, *, limit: int = 5) -> list[RequirementReviewJob]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM requirement_review_jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_job(row) for row in rows if row is not None]

    def count_jobs_by_status(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM requirement_review_jobs
                GROUP BY status
                """
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def mark_job_running(self, job_id: str) -> bool:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE requirement_review_jobs
                SET status = 'running', started_at = ?, error_message = NULL
                WHERE job_id = ? AND status = 'pending'
                """,
                (now, job_id),
            )
        return cursor.rowcount > 0

    def complete_job(self, *, job_id: str, review_id: str, review_path: str) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE requirement_review_jobs
                SET status = 'completed',
                    review_id = ?,
                    review_path = ?,
                    completed_at = ?,
                    error_message = NULL
                WHERE job_id = ?
                """,
                (review_id, review_path, now, job_id),
            )

    def fail_job(self, *, job_id: str, error_message: str) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE requirement_review_jobs
                SET status = 'failed', completed_at = ?, error_message = ?
                WHERE job_id = ?
                """,
                (now, error_message[:2000], job_id),
            )

    def mark_read(self, *, review_id: str, review_path: str, source_path: str, user_id: str) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO requirement_review_reads (read_id, review_id, review_path, source_path, user_id, read_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(review_id, user_id)
                DO UPDATE SET review_path = excluded.review_path,
                              source_path = excluded.source_path,
                              read_at = excluded.read_at
                """,
                (str(uuid4()), review_id, review_path, source_path, user_id, now),
            )

    def get_read_review_ids(self, *, user_id: str, review_ids: list[str]) -> set[str]:
        if not review_ids:
            return set()
        placeholders = ",".join("?" for _ in review_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT review_id
                FROM requirement_review_reads
                WHERE user_id = ? AND review_id IN ({placeholders})
                """,
                [user_id, *review_ids],
            ).fetchall()
        return {str(row["review_id"]) for row in rows}

    def create_clickup_publish_job(
        self,
        *,
        review: RequirementReviewRecord,
        task_id: str,
        workspace_id: str,
        folder_id: str,
    ) -> RequirementReviewClickUpPublishJob:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO requirement_review_clickup_publish_jobs (
                    job_id, review_id, review_path, source_path, task_id, workspace_id, folder_id,
                    doc_id, page_id, doc_url, status, error_message, attempts, created_at, started_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, 'pending', NULL, 0, ?, NULL, NULL)
                ON CONFLICT(review_id, task_id)
                DO UPDATE SET review_path = excluded.review_path,
                              source_path = excluded.source_path,
                              workspace_id = excluded.workspace_id,
                              folder_id = excluded.folder_id,
                              status = CASE
                                  WHEN requirement_review_clickup_publish_jobs.status = 'completed'
                                  THEN requirement_review_clickup_publish_jobs.status
                                  ELSE 'pending'
                              END,
                              error_message = CASE
                                  WHEN requirement_review_clickup_publish_jobs.status = 'completed'
                                  THEN requirement_review_clickup_publish_jobs.error_message
                                  ELSE NULL
                              END,
                              completed_at = CASE
                                  WHEN requirement_review_clickup_publish_jobs.status = 'completed'
                                  THEN requirement_review_clickup_publish_jobs.completed_at
                                  ELSE NULL
                              END
                """,
                (str(uuid4()), review.review_id, review.path, review.source_path, task_id, workspace_id, folder_id, now),
            )
            row = connection.execute(
                """
                SELECT *
                FROM requirement_review_clickup_publish_jobs
                WHERE review_id = ? AND task_id = ?
                """,
                (review.review_id, task_id),
            ).fetchone()
        job = self._row_to_clickup_publish_job(row)
        if job is None:
            raise RuntimeError("ClickUp 发布任务创建失败。")
        return job

    def list_pending_clickup_publish_jobs(self, *, limit: int = 5) -> list[RequirementReviewClickUpPublishJob]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM requirement_review_clickup_publish_jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_clickup_publish_job(row) for row in rows if row is not None]

    def mark_clickup_publish_job_running(self, job_id: str) -> bool:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE requirement_review_clickup_publish_jobs
                SET status = 'running',
                    started_at = ?,
                    attempts = attempts + 1,
                    error_message = NULL
                WHERE job_id = ? AND status = 'pending'
                """,
                (now, job_id),
            )
        return cursor.rowcount > 0

    def complete_clickup_publish_job(self, *, job_id: str, doc_id: str, page_id: str, doc_url: str) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE requirement_review_clickup_publish_jobs
                SET status = 'completed',
                    doc_id = ?,
                    page_id = ?,
                    doc_url = ?,
                    completed_at = ?,
                    error_message = NULL
                WHERE job_id = ?
                """,
                (doc_id, page_id, doc_url, now, job_id),
            )

    def fail_clickup_publish_job(self, *, job_id: str, error_message: str) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE requirement_review_clickup_publish_jobs
                SET status = 'failed',
                    completed_at = ?,
                    error_message = ?
                WHERE job_id = ?
                """,
                (now, error_message[:2000], job_id),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    @staticmethod
    def _row_to_job(row: sqlite3.Row | None) -> RequirementReviewJob | None:
        if row is None:
            return None
        return RequirementReviewJob(
            job_id=row["job_id"],
            source_path=row["source_path"],
            source_hash=row["source_hash"],
            review_type=row["review_type"],
            skill_name=row["skill_name"],
            status=row["status"],
            review_id=row["review_id"],
            review_path=row["review_path"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _row_to_clickup_publish_job(row: sqlite3.Row | None) -> RequirementReviewClickUpPublishJob | None:
        if row is None:
            return None
        return RequirementReviewClickUpPublishJob(
            job_id=row["job_id"],
            review_id=row["review_id"],
            review_path=row["review_path"],
            source_path=row["source_path"],
            task_id=row["task_id"],
            workspace_id=row["workspace_id"],
            folder_id=row["folder_id"],
            doc_id=row["doc_id"],
            page_id=row["page_id"],
            doc_url=row["doc_url"],
            status=row["status"],
            error_message=row["error_message"],
            attempts=int(row["attempts"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )


class RequirementReviewService:
    def __init__(
        self,
        *,
        base_dir: Path,
        requirements_root: Path,
        skills_root: Path,
        store: RequirementReviewStore,
        runtime_client: AgentRuntimeClient,
        clickup_comment_client: ClickUpCommentClient | None = None,
        requirement_review_auto_enabled: bool = True,
        clickup_review_publish_enabled: bool = False,
        clickup_workspace_id: str = "",
        clickup_review_doc_folder_id: str = "",
        frontend_app_url: str = "/",
    ) -> None:
        self.base_dir = base_dir.resolve()
        self.requirements_root = requirements_root.resolve()
        self.skills_root = skills_root.resolve()
        self.reviews_root = self.base_dir / "workspace/knowledge/__reviews__/tech-review"
        self.store = store
        self.runtime_client = runtime_client
        self.clickup_comment_client = clickup_comment_client
        self.requirement_review_auto_enabled = requirement_review_auto_enabled
        self.clickup_review_publish_enabled = clickup_review_publish_enabled
        self.clickup_workspace_id = clickup_workspace_id
        self.clickup_review_doc_folder_id = clickup_review_doc_folder_id
        self.frontend_app_url = frontend_app_url

    def enqueue_changed_files(
        self,
        changed_files: list[str],
        *,
        change_assessments: dict[str, RequirementReviewChangeAssessment],
    ) -> list[RequirementReviewJob]:
        if not self.requirement_review_auto_enabled:
            return []
        jobs: list[RequirementReviewJob] = []
        for relative_path in changed_files:
            normalized_path = _normalize_changed_file_path(relative_path)
            source_path = self._changed_file_to_source_path(normalized_path)
            if source_path is None:
                continue
            assessment = change_assessments[normalized_path]
            for primary_source_path in self._changed_source_to_primary_source_paths(source_path):
                if not self._is_auto_review_eligible_source(primary_source_path):
                    continue
                source_hash = self.compute_source_hash(primary_source_path)
                latest = self.get_latest_review(primary_source_path, user_id=None)
                if latest and latest.status == "completed" and latest.source_hash == source_hash:
                    continue
                if latest is None and not assessment.is_new_file and not assessment.should_auto_review:
                    continue
                if (
                    latest
                    and latest.status == "completed"
                    and not assessment.should_auto_review
                ):
                    continue
                jobs.append(self.store.create_job(source_path=primary_source_path, source_hash=source_hash))
        return jobs

    async def process_pending_jobs(self, *, limit: int = 5) -> list[RequirementReviewRecord]:
        completed: list[RequirementReviewRecord] = []
        for job in self.store.list_pending_jobs(limit=limit):
            review = await self.run_job(job)
            if review is not None:
                completed.append(review)
        return completed

    async def rebuild_review(self, *, source_path: str) -> RequirementReviewRecord:
        self._resolve_source_path(source_path)
        source_hash = self.compute_source_hash(source_path)
        job = self.store.create_job(source_path=source_path, source_hash=source_hash)
        review = await self.run_job(job)
        if review is None:
            raise RuntimeError("需求评审生成失败。")
        return review

    def enqueue_review(self, *, source_path: str) -> RequirementReviewJob:
        self._resolve_source_path(source_path)
        source_hash = self.compute_source_hash(source_path)
        return self.store.create_job(source_path=source_path, source_hash=source_hash)

    def count_active_jobs(self) -> dict[str, int]:
        counts = self.store.count_jobs_by_status()
        pending = counts.get("pending", 0)
        running = counts.get("running", 0)
        return {
            "pending": pending,
            "running": running,
            "active": pending + running,
        }

    def get_job(self, *, job_id: str) -> RequirementReviewJob:
        job = self.store.get_job(job_id)
        if job is None:
            raise FileNotFoundError("评审任务不存在。")
        self._resolve_source_path(job.source_path)
        return job

    def get_job_review(self, *, job: RequirementReviewJob, user_id: str) -> RequirementReviewRecord | None:
        if job.status != "completed" or not job.review_id:
            return None
        for review in self.list_reviews(source_path=job.source_path, user_id=user_id):
            if review.review_id == job.review_id:
                return review
        return None

    async def run_job(self, job: RequirementReviewJob) -> RequirementReviewRecord | None:
        if not self.store.mark_job_running(job.job_id):
            return None
        try:
            source_paths = self._review_source_paths(job.source_path)
            source_hash = self.compute_source_hash(job.source_path)
            if source_hash != job.source_hash:
                self.store.fail_job(job_id=job.job_id, error_message="需求文件已更新，已创建新评审任务。")
                job = self.store.create_job(source_path=job.source_path, source_hash=source_hash)
                if not self.store.mark_job_running(job.job_id):
                    return None

            source_texts = [
                (path, self._resolve_source_path(path).read_text(encoding="utf-8", errors="replace"))
                for path in source_paths
            ]
            skill_path = self.skills_root / SKILL_NAME / "SKILL.md"
            if not skill_path.is_file():
                raise FileNotFoundError(f"缺少 Skill：{self._display_path(skill_path)}")
            skill_text = skill_path.read_text(encoding="utf-8", errors="replace")
            generated = await self._generate_review_markdown(
                source_path=job.source_path,
                source_hash=job.source_hash,
                source_texts=source_texts,
                skill_text=skill_text,
            )
            if not generated.strip():
                raise RuntimeError("需求评审 runtime 未返回有效评审正文。")
            review = self._write_review(job=job, generated_markdown=generated, source_paths=source_paths)
            self.store.complete_job(job_id=job.job_id, review_id=review.review_id, review_path=review.path)
            self._schedule_clickup_comment_job(review)
            return review
        except Exception as exc:
            self.store.fail_job(job_id=job.job_id, error_message=str(exc))
            return None

    def list_reviews(self, *, source_path: str, user_id: str) -> list[RequirementReviewRecord]:
        self._resolve_source_path(source_path)
        meta = self._read_meta(source_path)
        reviews = self._reviews_from_meta(meta)
        read_ids = self.store.get_read_review_ids(user_id=user_id, review_ids=[review.review_id for review in reviews])
        return [
            RequirementReviewRecord(
                review_id=review.review_id,
                review_type=review.review_type,
                path=review.path,
                source_path=review.source_path,
                source_paths=review.source_paths,
                source_hash=review.source_hash,
                status=review.status,
                risk_level=review.risk_level,
                created_at=review.created_at,
                completed_at=review.completed_at,
                is_read=review.review_id in read_ids,
            )
            for review in reviews
        ]

    def get_latest_review(self, source_path: str, user_id: str | None) -> RequirementReviewRecord | None:
        meta = self._read_meta(source_path)
        latest_id = meta.get("latest_reviews", {}).get(REVIEW_TYPE) if isinstance(meta.get("latest_reviews"), dict) else None
        reviews = self._reviews_from_meta(meta)
        review = next((item for item in reviews if item.review_id == latest_id), None)
        if review is None:
            return None
        if user_id is None:
            return review
        read_ids = self.store.get_read_review_ids(user_id=user_id, review_ids=[review.review_id])
        return RequirementReviewRecord(
            review_id=review.review_id,
            review_type=review.review_type,
            path=review.path,
            source_path=review.source_path,
            source_paths=review.source_paths,
            source_hash=review.source_hash,
            status=review.status,
            risk_level=review.risk_level,
            created_at=review.created_at,
            completed_at=review.completed_at,
            is_read=review.review_id in read_ids,
        )

    def get_review_detail(self, *, review_id: str, user_id: str) -> RequirementReviewDetail:
        review_file = self._find_review_file(review_id)
        raw = review_file.read_text(encoding="utf-8", errors="replace")
        frontmatter, body = _split_frontmatter(raw)
        source_path = frontmatter.get("source_path", "")
        source_meta_path = frontmatter.get("source_meta_path", "")
        review = RequirementReviewDetail(
            review_id=frontmatter.get("review_id", review_id),
            review_type=frontmatter.get("review_type", REVIEW_TYPE),
            path=self._display_path(review_file),
            source_path=source_path,
            source_paths=_parse_source_paths(frontmatter.get("source_paths", "")) or (source_path,),
            source_hash=frontmatter.get("source_hash", ""),
            status=frontmatter.get("status", "completed"),
            risk_level=frontmatter.get("risk_level", "medium"),
            created_at=frontmatter.get("created_at", ""),
            completed_at=frontmatter.get("completed_at") or None,
            is_read=False,
            source_type=frontmatter.get("source_type", "requirement_doc"),
            source_meta_path=source_meta_path,
            skill=frontmatter.get("skill", SKILL_NAME),
            content=body.strip(),
            frontmatter=frontmatter,
        )
        read_ids = self.store.get_read_review_ids(user_id=user_id, review_ids=[review.review_id])
        return RequirementReviewDetail(**{**review.__dict__, "is_read": review.review_id in read_ids})

    def mark_read(self, *, review_id: str, user_id: str) -> RequirementReviewDetail:
        detail = self.get_review_detail(review_id=review_id, user_id=user_id)
        self.store.mark_read(
            review_id=detail.review_id,
            review_path=detail.path,
            source_path=detail.source_path,
            user_id=user_id,
        )
        return self.get_review_detail(review_id=review_id, user_id=user_id)

    def build_review_badge(self, *, source_path: str, user_id: str) -> dict[str, object] | None:
        target = (self.base_dir / source_path.strip("/")).resolve()
        try:
            target.relative_to(self.requirements_root)
        except ValueError:
            return None
        if target.is_file():
            unread = self._unread_latest_reviews_for_file(self._display_path(target), user_id)
        elif target.is_dir():
            unread = []
            for path in target.rglob("*.md"):
                if self._is_hidden_requirement_path(path):
                    continue
                unread.extend(self._unread_latest_reviews_for_file(self._display_path(path), user_id))
        else:
            unread = []

        if not unread:
            return None
        latest = max(unread, key=lambda review: review.created_at or "")
        return {
            "type": REVIEW_TYPE,
            "unread_count": len(unread),
            "latest_review_id": latest.review_id,
        }

    def compute_source_hash(self, source_path: str) -> str:
        return self._compute_source_hash_for_paths(self._review_source_paths(source_path))

    async def _generate_review_markdown(self, *, source_path: str, source_hash: str, source_texts: list[tuple[str, str]], skill_text: str) -> str:
        source_sections: list[str] = []
        for index, (path, text) in enumerate(source_texts, 1):
            source_sections.extend(
                [
                    f"### 需求来源 {index}: `{path}`",
                    "",
                    text,
                    "",
                ]
            )
        prompt = "\n".join(
            [
                "请根据 requirement-check Skill 对以下需求范围生成一份统一需求完整性评审。",
                "如果需求范围同时包含 ClickUp task 和它引用的 doc，必须结合两者一起评审，不要只看其中一份。",
                "输出必须严格遵循 Skill 中的 Markdown 输出结构，只包含评审结论、主要问题、分维度评审三节。",
                "输出必须精简，优先结论和影响理解、验收、排期或上线质量的问题；不要展开完整检查过程、评分表或低价值常规建议。",
                "只输出 Markdown 报告正文，不要写入或修改任何文件。",
                f"主需求文件：`{source_path}`",
                f"内容快照 Hash：`{source_hash}`",
                "",
                "## Skill",
                skill_text,
                "",
                "## 需求范围",
                *source_sections,
            ]
        )
        session = await self.runtime_client.create_or_resume_session(
            runtime_session_id=None,
            working_directory=str(self.base_dir),
            system_prompt="你是 ai-prd 的自动需求评审执行器。只生成中文 Markdown 需求完整性评审报告，不执行文件写入。",
        )
        chunks: list[str] = []
        final_message = ""
        async for event in self.runtime_client.send_message_stream(
            session=session,
            message=prompt,
            metadata={"task": "requirement_tech_review", "source_path": source_path},
        ):
            if event.type == "delta":
                text = event.data.get("text")
                if isinstance(text, str):
                    chunks.append(text)
            if event.type == "message":
                content = event.data.get("content")
                if isinstance(content, str):
                    final_message = content
            if event.type == "complete" and not final_message:
                result = event.data.get("result")
                if isinstance(result, str):
                    final_message = result
        return (final_message or "".join(chunks)).strip()

    def _write_review(self, *, job: RequirementReviewJob, generated_markdown: str, source_paths: tuple[str, ...] | None = None) -> RequirementReviewRecord:
        now = datetime.now(timezone.utc)
        created_at = now.isoformat()
        review_id = f"review_{now.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
        review_dir = self.reviews_root / now.strftime("%Y") / now.strftime("%m")
        review_dir.mkdir(parents=True, exist_ok=True)
        review_file = review_dir / f"{review_id}.md"
        reviewed_source_paths = source_paths or self._review_source_paths(job.source_path)
        source_meta_path = self._source_meta_path(job.source_path)
        source_type = self._source_type(job.source_path)
        risk_level = _detect_risk_level(generated_markdown)
        body = generated_markdown.strip() or "未生成有效评审正文。"
        if not body.startswith("#"):
            body = f"# 评审结论\n\n{body}"
        body = _prepend_review_source_links(body, reviewed_source_paths)
        frontmatter = {
            "review_id": review_id,
            "review_type": REVIEW_TYPE,
            "source_type": source_type,
            "source_path": job.source_path,
            "source_paths": _format_source_paths(reviewed_source_paths),
            "source_meta_path": self._display_path(source_meta_path),
            "source_hash": job.source_hash,
            "skill": SKILL_NAME,
            "status": "completed",
            "risk_level": risk_level,
            "created_at": created_at,
            "completed_at": created_at,
        }
        review_file.write_text(f"{_format_frontmatter(frontmatter)}\n\n{body}\n", encoding="utf-8")
        review_path = self._display_path(review_file)
        review = RequirementReviewRecord(
            review_id=review_id,
            review_type=REVIEW_TYPE,
            path=review_path,
            source_path=job.source_path,
            source_paths=reviewed_source_paths,
            source_hash=job.source_hash,
            status="completed",
            risk_level=risk_level,
            created_at=created_at,
            completed_at=created_at,
        )
        for reviewed_source_path in reviewed_source_paths:
            self._update_meta(reviewed_source_path, review)
        return review

    def _update_meta(self, source_path: str, review: RequirementReviewRecord) -> None:
        meta_path = self._source_meta_path(source_path)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta = self._read_meta(source_path)
        reviews = [item for item in meta.get("reviews", []) if isinstance(item, dict)]
        reviews.append(
            {
                "review_id": review.review_id,
                "review_type": review.review_type,
                "path": review.path,
                "source_hash": review.source_hash,
                "source_path": review.source_path,
                "source_paths": list(review.source_paths),
                "status": review.status,
                "risk_level": review.risk_level,
                "created_at": review.created_at,
                "completed_at": review.completed_at,
            }
        )
        meta.update(
            {
                "source_path": source_path,
                "source_hash": review.source_hash,
                "source_type": self._source_type(source_path),
                "reviews": reviews,
                "latest_reviews": {**(meta.get("latest_reviews") if isinstance(meta.get("latest_reviews"), dict) else {}), REVIEW_TYPE: review.review_id},
                "updated_at": review.completed_at or review.created_at,
            }
        )
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    async def process_pending_clickup_publish_jobs(self, *, limit: int = 5) -> list[RequirementReviewClickUpPublishJob]:
        completed: list[RequirementReviewClickUpPublishJob] = []
        for job in self.store.list_pending_clickup_publish_jobs(limit=limit):
            processed = await self.run_clickup_publish_job(job)
            if processed is not None:
                completed.append(processed)
        return completed

    async def clickup_publish_job_loop(self, *, interval_seconds: int = 30, limit: int = 5) -> None:
        logger.info("ClickUp 评审发布任务 worker 已启动，间隔=%ds", interval_seconds)
        while True:
            try:
                await self.process_pending_clickup_publish_jobs(limit=limit)
            except Exception:
                logger.exception("ClickUp 评审发布任务 worker 出错，下次将继续重试")
            await asyncio.sleep(interval_seconds)

    async def run_clickup_publish_job(self, job: RequirementReviewClickUpPublishJob) -> RequirementReviewClickUpPublishJob | None:
        if self.clickup_comment_client is None:
            self.store.fail_clickup_publish_job(
                job_id=job.job_id,
                error_message="未配置 ClickUp 客户端，无法发布评审文档。",
            )
            return None
        if not self.store.mark_clickup_publish_job_running(job.job_id):
            return None
        try:
            review_file = self._resolve_review_path(job.review_path)
            raw = review_file.read_text(encoding="utf-8", errors="replace")
            frontmatter, body = _split_frontmatter(raw)
            review = RequirementReviewRecord(
                review_id=frontmatter.get("review_id", job.review_id),
                review_type=frontmatter.get("review_type", REVIEW_TYPE),
                path=self._display_path(review_file),
                source_path=frontmatter.get("source_path", job.source_path),
                source_paths=_parse_source_paths(frontmatter.get("source_paths", "")) or (job.source_path,),
                source_hash=frontmatter.get("source_hash", ""),
                status=frontmatter.get("status", "completed"),
                risk_level=frontmatter.get("risk_level", "medium"),
                created_at=frontmatter.get("created_at", ""),
                completed_at=frontmatter.get("completed_at") or None,
            )
            title = self._build_clickup_review_doc_title(review)
            month_name = self._clickup_review_month_name(review)
            doc_id = await self._ensure_clickup_review_archive_doc(
                workspace_id=job.workspace_id,
                folder_id=job.folder_id,
            )
            month_page_id = await self._ensure_clickup_review_month_page(
                workspace_id=job.workspace_id,
                doc_id=doc_id,
                month_name=month_name,
            )
            page_payload = await asyncio.to_thread(
                self.clickup_comment_client.create_doc_page,
                workspace_id=job.workspace_id,
                doc_id=doc_id,
                name=title,
                parent_page_id=month_page_id,
                sub_title="",
                content=self._build_clickup_review_doc_content(review=review, body=body),
                content_format="text/md",
            )
            page_id = _extract_clickup_response_id(page_payload, "page")
            if not page_id:
                raise RuntimeError(f"ClickUp 评审文档页面创建成功但未返回 page_id: {page_payload}")
            doc_url = _build_clickup_doc_url(workspace_id=job.workspace_id, doc_id=doc_id, page_id=page_id)
            comment_markdown = self._build_clickup_review_comment(review=review, doc_url=doc_url, body=body)
            await asyncio.to_thread(
                self.clickup_comment_client.create_task_comment,
                task_id=job.task_id,
                markdown=comment_markdown,
                notify_all=False,
            )
            self.store.complete_clickup_publish_job(job_id=job.job_id, doc_id=doc_id, page_id=page_id, doc_url=doc_url)
            return RequirementReviewClickUpPublishJob(
                **{
                    **job.__dict__,
                    "doc_id": doc_id,
                    "page_id": page_id,
                    "doc_url": doc_url,
                    "status": "completed",
                    "completed_at": _utc_now(),
                    "error_message": None,
                }
            )
        except Exception as exc:
            self.store.fail_clickup_publish_job(job_id=job.job_id, error_message=str(exc))
            logger.exception("ClickUp 评审发布任务失败: job_id=%s review_id=%s", job.job_id, job.review_id)
            return None

    async def _ensure_clickup_review_archive_doc(self, *, workspace_id: str, folder_id: str) -> str:
        docs = await asyncio.to_thread(
            self.clickup_comment_client.list_docs_in_folder,
            workspace_id=workspace_id,
            folder_id=folder_id,
        )
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            if str(doc.get("name") or "") == CLICKUP_REVIEW_ARCHIVE_DOC_NAME:
                doc_id = _extract_clickup_response_id(doc, "doc")
                if doc_id:
                    return doc_id
        payload = await asyncio.to_thread(
            self.clickup_comment_client.create_review_doc_in_folder,
            workspace_id=workspace_id,
            folder_id=folder_id,
            name=CLICKUP_REVIEW_ARCHIVE_DOC_NAME,
            visibility="PUBLIC",
        )
        doc_id = _extract_clickup_response_id(payload, "doc")
        if not doc_id:
            raise RuntimeError(f"ClickUp 评审归档 Doc 创建成功但未返回 doc_id: {payload}")
        return doc_id

    async def _ensure_clickup_review_month_page(self, *, workspace_id: str, doc_id: str, month_name: str) -> str:
        pages = await asyncio.to_thread(self.clickup_comment_client.list_doc_pages, doc_id=doc_id)
        for page in _flatten_clickup_pages(pages):
            if str(page.get("name") or "") == month_name:
                page_id = _extract_clickup_response_id(page, "page")
                if page_id:
                    return page_id
        payload = await asyncio.to_thread(
            self.clickup_comment_client.create_doc_page,
            workspace_id=workspace_id,
            doc_id=doc_id,
            name=month_name,
            sub_title="需求评审月度归档",
            content=f"# {month_name}\n\n本页归档 {month_name} 生成的需求评审。",
            content_format="text/md",
        )
        page_id = _extract_clickup_response_id(payload, "page")
        if not page_id:
            raise RuntimeError(f"ClickUp 月份归档页面创建成功但未返回 page_id: {payload}")
        return page_id

    def _schedule_clickup_comment_job(self, review: RequirementReviewRecord) -> None:
        if not self.clickup_review_publish_enabled:
            return
        if self.clickup_comment_client is None:
            return
        if self._source_type(review.source_path) != "requirement_task":
            return
        try:
            if not self.clickup_workspace_id or not self.clickup_review_doc_folder_id:
                raise ValueError("缺少 CLICKUP_WORKSPACE_ID 或 CLICKUP_REVIEW_DOC_FOLDER_ID，无法发布评审文档。")
            task_id = self._extract_clickup_task_id(review.source_path)
            self.store.create_clickup_publish_job(
                review=review,
                task_id=task_id,
                workspace_id=self.clickup_workspace_id,
                folder_id=self.clickup_review_doc_folder_id,
            )
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.info("ClickUp 评审发布任务已入队，将由后续后台任务处理: review_id=%s", review.review_id)
            else:
                loop.create_task(self.process_pending_clickup_publish_jobs(limit=1))
        except Exception:
            logger.exception("需求评审已生成，但创建 ClickUp 评审发布任务失败: review_id=%s", review.review_id)

    def _extract_clickup_task_id(self, source_path: str) -> str:
        source_file = self._resolve_source_path(source_path)
        markdown = source_file.read_text(encoding="utf-8", errors="replace")
        task_id_match = re.search(r"^- Task ID:\s*`?([A-Za-z0-9_-]+)`?\s*$", markdown, flags=re.MULTILINE)
        if task_id_match:
            return task_id_match.group(1)

        filename_match = re.search(r"__([A-Za-z0-9_-]+)\.md$", source_file.name)
        if filename_match:
            return filename_match.group(1)

        raise ValueError(f"无法从需求任务文件中识别 ClickUp Task ID: {source_path}")

    def _build_clickup_review_doc_title(self, review: RequirementReviewRecord) -> str:
        requirement_name = self._requirement_source_title(review.source_path)
        reviewed_at = _format_review_timestamp(review)
        return f"review_{requirement_name}_{reviewed_at}"

    def _clickup_review_month_name(self, review: RequirementReviewRecord) -> str:
        created_at = review.completed_at or review.created_at
        match = re.match(r"^(\d{4})-(\d{2})", created_at or "")
        if match:
            return f"{match.group(1)}-{match.group(2)}"
        review_id_match = re.match(r"^review_(\d{4})(\d{2})\d{2}_", review.review_id)
        if review_id_match:
            return f"{review_id_match.group(1)}-{review_id_match.group(2)}"
        return datetime.now(timezone.utc).strftime("%Y-%m")

    def _build_clickup_review_doc_content(self, *, review: RequirementReviewRecord, body: str) -> str:
        review_url = self._build_agent_review_url(review)
        source_title = self._requirement_source_title(review.source_path)
        source_url = self._requirement_source_clickup_url(review.source_path)
        source_text = _markdown_link(source_title, source_url) if source_url else source_title
        review_body = _strip_review_source_links_section(body).strip() or "未生成有效评审正文。"
        metadata = "\n".join(
            [
                f"- Review ID: [{review.review_id}]({review_url})",
                f"- 风险等级: {_format_risk_level_zh(review.risk_level)}",
                f"- 来源需求: {source_text}",
            ]
        )
        return f"## 元信息\n\n{metadata}\n\n{review_body}".strip()

    def _build_clickup_review_comment(self, *, review: RequirementReviewRecord, doc_url: str, body: str) -> str:
        review_url = self._build_agent_review_url(review)
        summary = _summarize_review_opinion(body=body)
        return "\n".join(
            [
                "# AI自动化需求评审已完成",
                "",
                f"- Review ID: [{review.review_id}]({review_url})",
                f"- 风险等级: {_format_risk_level_zh(review.risk_level)}",
                f"- 评审文档: [查看完整AI自动化需求评审]({doc_url})",
                "",
                f"评审意见：{summary}",
            ]
        )

    def _build_agent_review_url(self, review: RequirementReviewRecord) -> str:
        base_url = (self.frontend_app_url or "/").rstrip("/")
        query = urlencode(
            {
                "type": "requirements",
                "nodeId": review.source_path,
                "reviewId": review.review_id,
            },
            quote_via=quote,
        )
        return f"{base_url}/#/knowledge?{query}"

    def _requirement_source_title(self, source_path: str) -> str:
        source_file = self._resolve_source_path(source_path)
        markdown = source_file.read_text(encoding="utf-8", errors="replace")
        title_match = re.search(r"^- 标题:\s*(.+?)\s*$", markdown, flags=re.MULTILINE)
        if title_match:
            return title_match.group(1).strip("` ")
        heading_match = re.search(r"^#\s+(.+?)\s*$", markdown, flags=re.MULTILINE)
        if heading_match:
            return heading_match.group(1).strip()
        stem = Path(source_path).stem
        return re.sub(r"__[A-Za-z0-9_-]+$", "", stem).strip() or stem

    def _requirement_source_clickup_url(self, source_path: str) -> str:
        source_file = self._resolve_source_path(source_path)
        markdown = source_file.read_text(encoding="utf-8", errors="replace")
        link_match = re.search(r"^- 链接:\s*(https?://\S+)\s*$", markdown, flags=re.MULTILINE)
        if link_match:
            return link_match.group(1)
        task_id_match = re.search(r"^- Task ID:\s*`?([A-Za-z0-9_-]+)`?\s*$", markdown, flags=re.MULTILINE)
        if task_id_match:
            return f"https://app.clickup.com/t/{task_id_match.group(1)}"
        return ""

    def _reviews_from_meta(self, meta: dict[str, object]) -> list[RequirementReviewRecord]:
        source_path = str(meta.get("source_path") or "")
        reviews = []
        for item in meta.get("reviews", []):
            if not isinstance(item, dict):
                continue
            review_id = str(item.get("review_id") or "")
            path = str(item.get("path") or "")
            if not review_id or not path:
                continue
            reviews.append(
                RequirementReviewRecord(
                    review_id=review_id,
                    review_type=str(item.get("review_type") or REVIEW_TYPE),
                    path=path,
                    source_path=str(item.get("source_path") or source_path),
                    source_paths=_coerce_source_paths(item.get("source_paths")) or (str(item.get("source_path") or source_path),),
                    source_hash=str(item.get("source_hash") or ""),
                    status=str(item.get("status") or "completed"),
                    risk_level=str(item.get("risk_level") or "medium"),
                    created_at=str(item.get("created_at") or ""),
                    completed_at=str(item.get("completed_at") or "") or None,
                )
            )
        return sorted(reviews, key=lambda review: review.created_at, reverse=True)

    def _unread_latest_reviews_for_file(self, source_path: str, user_id: str) -> list[RequirementReviewRecord]:
        if not source_path.endswith(".md"):
            return []
        latest = self.get_latest_review(source_path, user_id=user_id)
        if latest is None or latest.status != "completed" or latest.is_read:
            return []
        try:
            if latest.source_hash != self._compute_source_hash_for_paths(latest.source_paths):
                return []
        except FileNotFoundError:
            return []
        return [latest]

    def _read_meta(self, source_path: str) -> dict[str, object]:
        meta_path = self._source_meta_path(source_path)
        if not meta_path.is_file():
            return {}
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def _find_review_file(self, review_id: str) -> Path:
        normalized = review_id.strip()
        if not re.match(r"^review_[0-9]{8}_[0-9]{6}_[a-f0-9]{6}$", normalized):
            raise FileNotFoundError("评审记录不存在。")
        matches = list(self.reviews_root.rglob(f"{normalized}.md"))
        if not matches:
            raise FileNotFoundError("评审记录不存在。")
        return matches[0]

    def _resolve_review_path(self, review_path: str) -> Path:
        normalized = review_path.strip().strip("/")
        target = (self.base_dir / normalized).resolve()
        try:
            target.relative_to(self.reviews_root)
        except ValueError as exc:
            raise FileNotFoundError("评审记录不存在。") from exc
        if not target.is_file() or target.suffix.lower() != ".md":
            raise FileNotFoundError("评审记录不存在。")
        return target

    def _changed_file_to_source_path(self, relative_path: str) -> str | None:
        normalized = _normalize_changed_file_path(relative_path)
        if not normalized or not normalized.endswith(".md"):
            return None
        if is_auto_review_ignored_requirement_path(normalized):
            return None
        if normalized.startswith("__meta__/") or "/__meta__/" in f"/{normalized}/":
            return None
        source_file = (self.requirements_root / normalized).resolve()
        if not source_file.is_file() or self._is_hidden_requirement_path(source_file):
            return None
        return self._display_path(source_file)

    def _changed_source_to_primary_source_paths(self, source_path: str) -> tuple[str, ...]:
        if self._source_type(source_path) == "requirement_task":
            return (source_path,)

        source_file = self._resolve_source_path(source_path)
        linked_tasks: list[str] = []
        tasks_root = self.requirements_root / "tasks"
        if tasks_root.is_dir():
            for task_file in sorted(tasks_root.rglob("*.md")):
                if self._is_hidden_requirement_path(task_file):
                    continue
                linked_docs = self._linked_requirement_doc_paths(task_file)
                if source_file in linked_docs:
                    linked_tasks.append(self._display_path(task_file))
        return tuple(linked_tasks) or (source_path,)

    def _is_auto_review_eligible_source(self, source_path: str) -> bool:
        if self._source_type(source_path) != "requirement_task":
            return False
        source_file = self._resolve_source_path(source_path)
        markdown = source_file.read_text(encoding="utf-8", errors="replace")
        if _extract_clickup_task_status(markdown) != AUTO_REVIEW_REQUIRED_TASK_STATUS:
            return False
        return not self._is_auto_review_excluded_task_list(source_file=source_file, markdown=markdown)

    def _is_auto_review_excluded_task_list(self, *, source_file: Path, markdown: str) -> bool:
        task_list = _extract_clickup_task_list(markdown)
        if AUTO_REVIEW_EXCLUDED_TASK_LIST_KEYWORD in task_list:
            return True
        try:
            relative_parts = source_file.relative_to(self.requirements_root / "tasks").parts
        except ValueError:
            return False
        return any(AUTO_REVIEW_EXCLUDED_TASK_LIST_KEYWORD in part for part in relative_parts[:-1])

    def _review_source_paths(self, primary_source_path: str) -> tuple[str, ...]:
        primary_file = self._resolve_source_path(primary_source_path)
        paths = [self._display_path(primary_file)]
        if self._source_type(primary_source_path) == "requirement_task":
            paths.extend(self._display_path(path) for path in self._linked_requirement_doc_paths(primary_file))
        return tuple(dict.fromkeys(paths))

    def _linked_requirement_doc_paths(self, source_file: Path) -> tuple[Path, ...]:
        try:
            source_file.relative_to(self.requirements_root / "tasks")
        except ValueError:
            return ()

        markdown = source_file.read_text(encoding="utf-8", errors="replace")
        linked_paths: list[Path] = []
        for raw_target in _extract_markdown_link_targets(markdown):
            parsed = urlparse(raw_target)
            if parsed.scheme or parsed.netloc:
                continue
            target_text = unquote(parsed.path).strip()
            if not target_text or not target_text.endswith(".md"):
                continue
            target = (source_file.parent / target_text).resolve()
            try:
                target.relative_to(self.requirements_root / "docs")
            except ValueError:
                continue
            if target.is_file() and not self._is_hidden_requirement_path(target):
                linked_paths.append(target)
        return tuple(dict.fromkeys(linked_paths))

    def _compute_source_hash_for_paths(self, source_paths: tuple[str, ...]) -> str:
        digest = hashlib.sha256()
        for item in source_paths:
            source_file = self._resolve_source_path(item)
            digest.update(item.encode("utf-8"))
            digest.update(b"\0")
            source_text = normalize_auto_review_source_markdown(
                source_file.read_text(encoding="utf-8", errors="replace")
            )
            digest.update(source_text.encode("utf-8"))
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"

    def _resolve_source_path(self, source_path: str) -> Path:
        normalized = source_path.strip().strip("/")
        if not normalized:
            raise FileNotFoundError("需求文件不存在。")
        # 前端传入的是“展示路径”（如 /coinex/knowledge/requirements/tasks/xxx.md，相对 workspace 且省略 workspace 段），
        # 需先还原为宿主相对路径；内部自动评审流程传入的已是宿主路径，保持不变。最终仍校验落在需求知识库内。
        host_relative = normalized if normalized.startswith("workspace/") else f"workspace/{normalized}"
        target = (self.base_dir / host_relative).resolve()
        try:
            target.relative_to(self.requirements_root)
        except ValueError as exc:
            raise FileNotFoundError("需求文件不存在。") from exc
        if not target.is_file() or target.suffix.lower() != ".md" or self._is_hidden_requirement_path(target):
            raise FileNotFoundError("需求文件不存在。")
        return target

    def _source_meta_path(self, source_path: str) -> Path:
        source_file = self._resolve_source_path(source_path)
        relative_path = source_file.relative_to(self.requirements_root).as_posix()
        return self.requirements_root / "__meta__" / f"{relative_path}.json"

    def _source_type(self, source_path: str) -> str:
        normalized = source_path.replace("\\", "/")
        if "/requirements/tasks/" in normalized:
            return "requirement_task"
        return "requirement_doc"

    def _display_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.base_dir).as_posix()

    def _is_hidden_requirement_path(self, path: Path) -> bool:
        try:
            parts = path.resolve().relative_to(self.requirements_root).parts
        except ValueError:
            return True
        return any(part.startswith("_") or (part.startswith("__") and part.endswith("__")) for part in parts[:-1])


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_review_timestamp(review: RequirementReviewRecord) -> str:
    for value in (review.completed_at, review.created_at):
        if not value:
            continue
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%Y%m%d_%H%M%S")
        except ValueError:
            continue
    match = re.match(r"^review_(\d{8}_\d{6})_", review.review_id)
    if match:
        return match.group(1)
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def _format_risk_level_zh(risk_level: str) -> str:
    normalized = _normalize_risk_level(risk_level) or risk_level.strip().lower()
    return {
        "high": "高",
        "medium": "中",
        "low": "低",
    }.get(normalized, risk_level or "未识别")


def _markdown_link(label: str, url: str) -> str:
    return f"[{_escape_markdown_link_label(label)}]({url})"


def _escape_markdown_link_label(label: str) -> str:
    return label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def _strip_review_source_links_section(markdown: str) -> str:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    result: list[str] = []
    index = 0
    while index < len(lines):
        if lines[index].strip() != "## 关联来源":
            result.append(lines[index])
            index += 1
            continue

        index += 1
        while index < len(lines) and not lines[index].strip():
            index += 1
        while index < len(lines):
            stripped = lines[index].strip()
            if not stripped or re.match(r"^[-*+]\s+", stripped):
                index += 1
                continue
            break
        while result and not result[-1].strip():
            result.pop()
        if result:
            result.append("")
    return "\n".join(result).strip()


def _summarize_review_opinion(*, body: str) -> str:
    for line in _strip_review_source_links_section(body).splitlines():
        summary = _plain_review_summary_line(line)
        if not summary:
            continue
        if re.match(r"^(整体问题级别|综合问题级别|整体风险等级|综合风险等级|问题级别|严重级别|风险等级)\s*[:：]", summary):
            continue
        return _limit_summary_sentence(summary)
    return "本次评审未生成明确结论，请打开评审文档查看详情。"


def _plain_review_summary_line(line: str) -> str:
    text = line.strip()
    if not text or text.startswith("#"):
        return ""
    text = re.sub(r"^>\s*", "", text)
    text = re.sub(r"^[-*+]\s+", "", text)
    text = re.sub(r"^\d+[.)、]\s+", "", text)
    text = re.sub(r"^\[[ xX]\]\s+", "", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    text = re.sub(r"_([^_]+)_", r"\1", text)
    text = re.sub(r"\[([^\]\n]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^(评审结论|结论|概要|总结)\s*[:：]\s*", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _limit_summary_sentence(text: str, *, max_length: int = 120) -> str:
    summary = text.strip()
    if len(summary) > max_length:
        summary = summary[:max_length].rstrip("，,；;、 ")
    if not summary.endswith(("。", "！", "？", ".", "!", "?")):
        summary = f"{summary}。"
    return summary


def _normalize_changed_file_path(relative_path: str) -> str:
    return relative_path.strip().replace("\\", "/").strip("/")


def is_auto_review_ignored_requirement_path(relative_path: str) -> bool:
    normalized = _normalize_changed_file_path(relative_path)
    parts = normalized.split("/")
    for index, part in enumerate(parts):
        if part != "linked_docs" or index + 1 >= len(parts):
            continue
        if parts[index + 1].startswith("review_"):
            return True
    return False


def normalize_auto_review_source_markdown(markdown: str) -> str:
    without_ignored_sections = _strip_auto_review_ignored_sections(markdown)
    lines = [
        line
        for line in without_ignored_sections.splitlines()
        if not _is_review_linked_doc_line(line)
    ]
    return "\n".join(lines).strip() + "\n"


def _strip_auto_review_ignored_sections(markdown: str) -> str:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    result: list[str] = []
    index = 0
    while index < len(lines):
        heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", lines[index])
        if not heading or heading.group(2).strip() not in AUTO_REVIEW_IGNORED_SECTION_TITLES:
            result.append(lines[index])
            index += 1
            continue

        skipped_level = len(heading.group(1))
        index += 1
        while index < len(lines):
            next_heading = re.match(r"^(#{1,6})\s+(.+?)\s*$", lines[index])
            if next_heading and len(next_heading.group(1)) <= skipped_level:
                break
            index += 1
        while result and not result[-1].strip():
            result.pop()
        if result:
            result.append("")
    return "\n".join(result)


def _is_review_linked_doc_line(line: str) -> bool:
    stripped = line.strip()
    if not re.match(r"^[-*+]\s+", stripped):
        return False
    return any(
        _is_review_linked_doc_target(target)
        for target in _extract_markdown_link_targets(stripped)
    )


def _is_review_linked_doc_target(target: str) -> bool:
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return False
    path = unquote(parsed.path).strip().replace("\\", "/")
    if not path:
        return False
    parts = [part for part in path.split("/") if part]
    for index, part in enumerate(parts):
        if (
            part == "linked_docs"
            and index + 1 < len(parts)
            and parts[index + 1].startswith("review_")
        ):
            return True
    return False


def _extract_markdown_link_targets(markdown: str) -> list[str]:
    targets: list[str] = []
    for match in re.finditer(r"!?\[[^\]]*\]\(([^)]+)\)", markdown):
        target = match.group(1).strip()
        if not target:
            continue
        if target.startswith("<") and target.endswith(">"):
            target = target[1:-1].strip()
        targets.append(target)
    return targets


def _extract_clickup_task_status(markdown: str) -> str:
    match = re.search(r"^\s*-\s*状态\s*[:：]\s*(.+?)\s*$", markdown, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _extract_clickup_task_list(markdown: str) -> str:
    match = re.search(r"^\s*-\s*List\s*[:：]\s*(.+?)\s*$", markdown, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def _format_source_paths(source_paths: tuple[str, ...]) -> str:
    return " | ".join(source_paths)


def _parse_source_paths(value: str) -> tuple[str, ...]:
    return tuple(path.strip() for path in value.split("|") if path.strip())


def _coerce_source_paths(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(path) for path in value if isinstance(path, str) and path)
    if isinstance(value, str):
        return _parse_source_paths(value)
    return ()


def _extract_clickup_response_id(payload: dict[str, object], kind: str) -> str:
    for key in ("id", f"{kind}_id"):
        value = payload.get(key)
        if value:
            return str(value)
    nested = payload.get(kind)
    if isinstance(nested, dict):
        for key in ("id", f"{kind}_id"):
            value = nested.get(key)
            if value:
                return str(value)
    data = payload.get("data")
    if isinstance(data, dict):
        for key in ("id", f"{kind}_id"):
            value = data.get(key)
            if value:
                return str(value)
        nested_data = data.get(kind)
        if isinstance(nested_data, dict):
            for key in ("id", f"{kind}_id"):
                value = nested_data.get(key)
                if value:
                    return str(value)
    return ""


def _build_clickup_doc_url(*, workspace_id: str, doc_id: str, page_id: str) -> str:
    return f"https://app.clickup.com/{workspace_id}/docs/{doc_id}/{page_id}"


def _flatten_clickup_pages(pages: list[dict[str, object]]) -> list[dict[str, object]]:
    flattened: list[dict[str, object]] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        flattened.append(page)
        children = page.get("pages")
        if isinstance(children, list):
            flattened.extend(_flatten_clickup_pages(children))
    return flattened


def _prepend_review_source_links(markdown: str, source_paths: tuple[str, ...]) -> str:
    if not source_paths or "## 关联来源" in markdown:
        return markdown

    lines = ["## 关联来源", ""]
    for source_path in source_paths:
        label = _review_source_link_label(source_path)
        lines.append(f"- [{label}](<{source_path}>)")
    source_section = "\n".join(lines)

    match = re.match(r"^(# .+?)(\n+)([\s\S]*)$", markdown)
    if not match:
        return f"{source_section}\n\n{markdown}"
    return f"{match.group(1)}\n\n{source_section}\n\n{match.group(3).lstrip()}"


def _review_source_link_label(source_path: str) -> str:
    normalized = source_path.replace("\\", "/")
    prefix = "Task" if "/requirements/tasks/" in normalized else "Doc"
    return f"{prefix}: {Path(normalized).name}"


def _format_frontmatter(metadata: dict[str, str]) -> str:
    lines = ["---"]
    lines.extend(f"{key}: {value}" for key, value in metadata.items())
    lines.append("---")
    return "\n".join(lines)


def _split_frontmatter(markdown: str) -> tuple[dict[str, str], str]:
    normalized = markdown.replace("\r\n", "\n")
    match = re.match(r"^---\n([\s\S]*?)\n---(?:\n+|$)", normalized)
    if not match:
        return {}, normalized
    metadata: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata, normalized[match.end():]


def _detect_risk_level(markdown: str) -> str:
    overall_risk = _detect_labeled_risk_level(markdown, labels=("整体问题级别", "综合问题级别", "整体风险等级", "综合风险等级"))
    if overall_risk:
        return overall_risk
    explicit_risk = _highest_risk_level(_detect_labeled_risk_levels(markdown, labels=("问题级别", "严重级别", "风险等级")))
    if explicit_risk:
        return explicit_risk
    conclusion_risk = _detect_conclusion_risk_level(markdown)
    if conclusion_risk:
        return conclusion_risk
    text = markdown.lower()
    if any(keyword in text for keyword in ["严重", "critical", "high risk", "risk: high"]):
        return "high"
    if any(keyword in text for keyword in ["low risk", "risk: low"]):
        return "low"
    return "medium"


def _detect_conclusion_risk_level(markdown: str) -> str | None:
    for line in markdown.splitlines():
        normalized = line.strip().lstrip("-*>#0123456789.、）) ").lower()
        if normalized.startswith(("高风险", "critical", "high risk")):
            return "high"
        if normalized.startswith(("中风险", "medium risk", "moderate risk")):
            return "medium"
        if normalized.startswith(("低风险", "low risk")):
            return "low"
    return None


def _detect_labeled_risk_level(markdown: str, *, labels: tuple[str, ...]) -> str | None:
    levels = _detect_labeled_risk_levels(markdown, labels=labels)
    return levels[0] if levels else None


def _detect_labeled_risk_levels(markdown: str, *, labels: tuple[str, ...]) -> list[str]:
    levels: list[str] = []
    for line in markdown.splitlines():
        if not any(label in line for label in labels):
            continue
        if "/" in line or "：" not in line and ":" not in line:
            continue
        pattern = rf"(?:\*+)?(?:{'|'.join(re.escape(label) for label in labels)})(?:\*+)?\s*[:：]\s*(?:`|\*|\s)*([^\s`*|/，,。；;）)]+)"
        match = re.search(pattern, line, flags=re.IGNORECASE)
        if not match:
            continue
        risk_level = _normalize_risk_level(match.group(1))
        if risk_level:
            levels.append(risk_level)
    return levels


def _highest_risk_level(levels: list[str]) -> str | None:
    order = {"low": 1, "medium": 2, "high": 3}
    return max(levels, key=lambda level: order.get(level, 0), default=None)


def _normalize_risk_level(value: str) -> str | None:
    normalized = value.strip().lower()
    if normalized.startswith(("高", "high")):
        return "high"
    if normalized.startswith(("中", "medium", "moderate")):
        return "medium"
    if normalized.startswith(("低", "low")):
        return "low"
    return None
