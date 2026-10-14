from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4

from app.business.business_doc_updates.service import PullRequestReviewRequestEvent, PullRequestSnapshot


REVIEW_TYPE = "code_review"


@dataclass(frozen=True)
class CodeReviewJob:
    job_id: str
    repo_full_name: str
    pr_number: int
    pr_url: str
    base_ref: str
    base_sha: str
    head_sha: str
    diff_hash: str
    requested_event_id: str
    requested_by_login: str
    requested_reviewer_logins: str
    requested_team_slugs: str
    notification_emails: str
    review_type: str
    skill_name: str | None
    review_policy_path: str
    status: str
    review_id: str | None
    review_path: str | None
    error_message: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True)
class CodeReviewEmailJob:
    email_job_id: str
    code_review_job_id: str
    review_id: str
    recipient_email: str
    subject: str
    status: str
    attempts: int
    error_message: str | None
    created_at: str
    sent_at: str | None


class CodeReviewStore:
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
                CREATE TABLE IF NOT EXISTS code_review_poll_state (
                    repo_full_name TEXT PRIMARY KEY,
                    last_polled_at TEXT,
                    last_event_cursor TEXT,
                    error_message TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS code_review_jobs (
                    job_id TEXT PRIMARY KEY,
                    repo_full_name TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    pr_url TEXT NOT NULL,
                    base_ref TEXT NOT NULL,
                    base_sha TEXT NOT NULL,
                    head_sha TEXT NOT NULL,
                    diff_hash TEXT NOT NULL,
                    requested_event_id TEXT NOT NULL,
                    requested_by_login TEXT NOT NULL,
                    requested_reviewer_logins TEXT NOT NULL,
                    requested_team_slugs TEXT NOT NULL,
                    notification_emails TEXT NOT NULL,
                    review_type TEXT NOT NULL,
                    skill_name TEXT,
                    review_policy_path TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed')),
                    review_id TEXT,
                    review_path TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE(repo_full_name, pr_number, head_sha)
                );

                CREATE INDEX IF NOT EXISTS idx_code_review_jobs_status_created
                ON code_review_jobs(status, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_code_review_jobs_review_id
                ON code_review_jobs(review_id);

                CREATE TABLE IF NOT EXISTS code_review_email_jobs (
                    email_job_id TEXT PRIMARY KEY,
                    code_review_job_id TEXT NOT NULL,
                    review_id TEXT NOT NULL,
                    recipient_email TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'sent', 'failed')),
                    attempts INTEGER NOT NULL DEFAULT 0,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    sent_at TEXT,
                    UNIQUE(code_review_job_id, recipient_email)
                );

                CREATE INDEX IF NOT EXISTS idx_code_review_email_jobs_status_created
                ON code_review_email_jobs(status, created_at ASC);
                """
            )
            self._ensure_code_review_jobs_schema(connection)

    def _ensure_code_review_jobs_schema(self, connection: sqlite3.Connection) -> None:
        table_info = connection.execute("PRAGMA table_info(code_review_jobs)").fetchall()
        columns = {row["name"] for row in table_info}
        if "review_policy_path" not in columns:
            connection.execute(
                "ALTER TABLE code_review_jobs ADD COLUMN review_policy_path TEXT NOT NULL DEFAULT ''"
            )
        if "skill_name" not in columns:
            connection.execute("ALTER TABLE code_review_jobs ADD COLUMN skill_name TEXT")
        table_sql_row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'code_review_jobs'"
        ).fetchone()
        normalized_table_sql = "".join(str(table_sql_row["sql"] or "").lower().split()) if table_sql_row else ""
        has_legacy_unique = "unique(repo_full_name,pr_number)" in normalized_table_sql
        skill_name_not_null = any(
            row["name"] == "skill_name" and int(row["notnull"]) == 1
            for row in table_info
        )
        if has_legacy_unique or skill_name_not_null:
            self._rebuild_code_review_jobs(connection)

    def _rebuild_code_review_jobs(self, connection: sqlite3.Connection) -> None:
        connection.execute("ALTER TABLE code_review_jobs RENAME TO code_review_jobs_old")
        connection.execute(
            """
            CREATE TABLE code_review_jobs (
                job_id TEXT PRIMARY KEY,
                repo_full_name TEXT NOT NULL,
                pr_number INTEGER NOT NULL,
                pr_url TEXT NOT NULL,
                base_ref TEXT NOT NULL,
                base_sha TEXT NOT NULL,
                head_sha TEXT NOT NULL,
                diff_hash TEXT NOT NULL,
                requested_event_id TEXT NOT NULL,
                requested_by_login TEXT NOT NULL,
                requested_reviewer_logins TEXT NOT NULL,
                requested_team_slugs TEXT NOT NULL,
                notification_emails TEXT NOT NULL,
                review_type TEXT NOT NULL,
                skill_name TEXT,
                review_policy_path TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed')),
                review_id TEXT,
                review_path TEXT,
                error_message TEXT,
                created_at TEXT NOT NULL,
                started_at TEXT,
                completed_at TEXT,
                UNIQUE(repo_full_name, pr_number, head_sha)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO code_review_jobs (
                job_id, repo_full_name, pr_number, pr_url, base_ref, base_sha, head_sha,
                diff_hash, requested_event_id, requested_by_login, requested_reviewer_logins,
                requested_team_slugs, notification_emails, review_type, skill_name, review_policy_path, status,
                review_id, review_path, error_message, created_at, started_at, completed_at
            )
            SELECT
                job_id, repo_full_name, pr_number, pr_url, base_ref, base_sha, head_sha,
                diff_hash, requested_event_id, requested_by_login, requested_reviewer_logins,
                requested_team_slugs, notification_emails, review_type, skill_name, review_policy_path, status,
                review_id, review_path, error_message, created_at, started_at, completed_at
            FROM code_review_jobs_old
            """
        )
        connection.execute("DROP TABLE code_review_jobs_old")
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_code_review_jobs_status_created
            ON code_review_jobs(status, created_at ASC)
            """
        )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_code_review_jobs_review_id
            ON code_review_jobs(review_id)
            """
        )

    def has_job_for_pr_head(self, *, repo_full_name: str, pr_number: int, head_sha: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM code_review_jobs
                WHERE repo_full_name = ? AND pr_number = ? AND head_sha = ?
                LIMIT 1
                """,
                (repo_full_name, pr_number, head_sha),
            ).fetchone()
        return row is not None

    def create_job(
        self,
        *,
        snapshot: PullRequestSnapshot,
        diff_hash: str,
        requested_event: PullRequestReviewRequestEvent,
        requested_reviewer_logins: tuple[str, ...],
        requested_team_slugs: tuple[str, ...],
        notification_emails: tuple[str, ...],
        review_policy_path: str,
    ) -> CodeReviewJob:
        now = _utc_now()
        job = CodeReviewJob(
            job_id=str(uuid4()),
            repo_full_name=snapshot.repo_full_name,
            pr_number=snapshot.pr_number,
            pr_url=snapshot.html_url or f"https://github.com/{snapshot.repo_full_name}/pull/{snapshot.pr_number}",
            base_ref=snapshot.base_ref,
            base_sha=snapshot.base_sha,
            head_sha=snapshot.head_sha,
            diff_hash=diff_hash,
            requested_event_id=requested_event.event_id,
            requested_by_login=requested_event.actor_login,
            requested_reviewer_logins=_format_csv(requested_reviewer_logins),
            requested_team_slugs=_format_csv(requested_team_slugs),
            notification_emails=_format_csv(notification_emails),
            review_type=REVIEW_TYPE,
            skill_name=None,
            review_policy_path=review_policy_path,
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
                INSERT INTO code_review_jobs (
                    job_id, repo_full_name, pr_number, pr_url, base_ref, base_sha, head_sha,
                    diff_hash, requested_event_id, requested_by_login, requested_reviewer_logins,
                    requested_team_slugs, notification_emails, review_type, skill_name, review_policy_path, status,
                    review_id, review_path, error_message, created_at, started_at, completed_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.repo_full_name,
                    job.pr_number,
                    job.pr_url,
                    job.base_ref,
                    job.base_sha,
                    job.head_sha,
                    job.diff_hash,
                    job.requested_event_id,
                    job.requested_by_login,
                    job.requested_reviewer_logins,
                    job.requested_team_slugs,
                    job.notification_emails,
                    job.review_type,
                    job.skill_name,
                    job.review_policy_path,
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

    def get_job(self, job_id: str) -> CodeReviewJob | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM code_review_jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._row_to_job(row)

    def get_job_by_review_id(self, review_id: str) -> CodeReviewJob | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM code_review_jobs WHERE review_id = ?", (review_id,)).fetchone()
        return self._row_to_job(row)

    def get_job_for_pr_head(self, *, repo_full_name: str, pr_number: int, head_sha: str) -> CodeReviewJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM code_review_jobs
                WHERE repo_full_name = ? AND pr_number = ? AND head_sha = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (repo_full_name, pr_number, head_sha),
            ).fetchone()
        return self._row_to_job(row)

    def get_latest_job_for_pr(self, *, repo_full_name: str, pr_number: int) -> CodeReviewJob | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM code_review_jobs
                WHERE repo_full_name = ? AND pr_number = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (repo_full_name, pr_number),
            ).fetchone()
        return self._row_to_job(row)

    def list_jobs(
        self,
        *,
        repo_full_name: str | None = None,
        pr_number: int | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[CodeReviewJob]:
        clauses: list[str] = []
        params: list[object] = []
        if repo_full_name:
            clauses.append("repo_full_name = ?")
            params.append(repo_full_name)
        if pr_number is not None:
            clauses.append("pr_number = ?")
            params.append(pr_number)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM code_review_jobs
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
        return [job for row in rows if (job := self._row_to_job(row)) is not None]

    def list_pending_jobs(self, *, limit: int = 5) -> list[CodeReviewJob]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM code_review_jobs
                WHERE status = 'pending'
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [job for row in rows if (job := self._row_to_job(row)) is not None]

    def mark_job_running(self, job_id: str) -> bool:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE code_review_jobs
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
                UPDATE code_review_jobs
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
                UPDATE code_review_jobs
                SET status = 'failed', completed_at = ?, error_message = ?
                WHERE job_id = ?
                """,
                (now, error_message[:2000], job_id),
            )

    def fail_stale_running_jobs(self, *, cutoff_started_at: str, error_message: str) -> list[CodeReviewJob]:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM code_review_jobs
                WHERE status = 'running'
                  AND started_at IS NOT NULL
                  AND started_at < ?
                ORDER BY started_at ASC
                """,
                (cutoff_started_at,),
            ).fetchall()
            connection.execute(
                """
                UPDATE code_review_jobs
                SET status = 'failed', completed_at = ?, error_message = ?
                WHERE status = 'running'
                  AND started_at IS NOT NULL
                  AND started_at < ?
                """,
                (now, error_message[:2000], cutoff_started_at),
            )
        return [job for row in rows if (job := self._row_to_job(row)) is not None]

    def retry_job(self, *, job_id: str, notification_emails: tuple[str, ...] | None = None) -> CodeReviewJob:
        now = _utc_now()
        emails_value = _format_csv(notification_emails) if notification_emails is not None else None
        with self._lock, self._connect() as connection:
            if emails_value is None:
                cursor = connection.execute(
                    """
                    UPDATE code_review_jobs
                    SET status = 'pending',
                        started_at = NULL,
                        completed_at = NULL,
                        error_message = NULL,
                        created_at = ?
                    WHERE job_id = ? AND status = 'failed'
                    """,
                    (now, job_id),
                )
            else:
                cursor = connection.execute(
                    """
                    UPDATE code_review_jobs
                    SET status = 'pending',
                        notification_emails = ?,
                        started_at = NULL,
                        completed_at = NULL,
                        error_message = NULL,
                        created_at = ?
                    WHERE job_id = ? AND status = 'failed'
                    """,
                    (emails_value, now, job_id),
                )
            row = connection.execute("SELECT * FROM code_review_jobs WHERE job_id = ?", (job_id,)).fetchone()
        job = self._row_to_job(row)
        if job is None:
            raise FileNotFoundError("代码 Review 任务不存在。")
        if cursor.rowcount == 0 and job.status != "pending":
            raise ValueError("只有 failed 状态的代码 Review 任务可以重试。")
        return job

    def create_email_jobs(
        self,
        *,
        job: CodeReviewJob,
        review_id: str,
        subject: str,
        recipient_emails: tuple[str, ...] | None = None,
    ) -> list[CodeReviewEmailJob]:
        now = _utc_now()
        emails = recipient_emails if recipient_emails is not None else _parse_csv(job.notification_emails)
        with self._lock, self._connect() as connection:
            for email in emails:
                connection.execute(
                    """
                    INSERT INTO code_review_email_jobs (
                        email_job_id, code_review_job_id, review_id, recipient_email,
                        subject, status, attempts, error_message, created_at, sent_at
                    )
                    VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, ?, NULL)
                    ON CONFLICT(code_review_job_id, recipient_email) DO NOTHING
                    """,
                    (str(uuid4()), job.job_id, review_id, email, subject, now),
                )
            rows = connection.execute(
                """
                SELECT *
                FROM code_review_email_jobs
                WHERE code_review_job_id = ?
                ORDER BY created_at ASC
                """,
                (job.job_id,),
            ).fetchall()
        return [email_job for row in rows if (email_job := self._row_to_email_job(row)) is not None]

    def get_email_job(self, email_job_id: str) -> CodeReviewEmailJob | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM code_review_email_jobs WHERE email_job_id = ?", (email_job_id,)).fetchone()
        return self._row_to_email_job(row)

    def list_retryable_email_jobs(self, *, limit: int = 5, max_attempts: int = 3) -> list[CodeReviewEmailJob]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM code_review_email_jobs
                WHERE status = 'pending' OR (status = 'failed' AND attempts < ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (max_attempts, limit),
            ).fetchall()
        return [job for row in rows if (job := self._row_to_email_job(row)) is not None]

    def mark_email_job_running(self, email_job_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE code_review_email_jobs
                SET status = 'running',
                    attempts = attempts + 1,
                    error_message = NULL
                WHERE email_job_id = ? AND status IN ('pending', 'failed')
                """,
                (email_job_id,),
            )
        return cursor.rowcount > 0

    def complete_email_job(self, *, email_job_id: str) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE code_review_email_jobs
                SET status = 'sent', sent_at = ?, error_message = NULL
                WHERE email_job_id = ?
                """,
                (now, email_job_id),
            )

    def fail_email_job(self, *, email_job_id: str, error_message: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE code_review_email_jobs
                SET status = 'failed', error_message = ?
                WHERE email_job_id = ?
                """,
                (error_message[:2000], email_job_id),
            )

    def retry_email_job(self, *, email_job_id: str) -> CodeReviewEmailJob:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE code_review_email_jobs
                SET status = 'pending', error_message = NULL
                WHERE email_job_id = ? AND status = 'failed'
                """,
                (email_job_id,),
            )
            row = connection.execute("SELECT * FROM code_review_email_jobs WHERE email_job_id = ?", (email_job_id,)).fetchone()
        job = self._row_to_email_job(row)
        if job is None:
            raise FileNotFoundError("代码 Review 邮件任务不存在。")
        if cursor.rowcount == 0 and job.status != "pending":
            raise ValueError("只有 failed 状态的邮件任务可以重试。")
        return job

    def record_poll_success(self, *, repo_full_name: str, event_cursor: str | None = None) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO code_review_poll_state (repo_full_name, last_polled_at, last_event_cursor, error_message, updated_at)
                VALUES (?, ?, ?, NULL, ?)
                ON CONFLICT(repo_full_name)
                DO UPDATE SET last_polled_at = excluded.last_polled_at,
                              last_event_cursor = COALESCE(excluded.last_event_cursor, code_review_poll_state.last_event_cursor),
                              error_message = NULL,
                              updated_at = excluded.updated_at
                """,
                (repo_full_name, now, event_cursor, now),
            )

    def record_poll_error(self, *, repo_full_name: str, error_message: str) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO code_review_poll_state (repo_full_name, last_polled_at, last_event_cursor, error_message, updated_at)
                VALUES (?, NULL, NULL, ?, ?)
                ON CONFLICT(repo_full_name)
                DO UPDATE SET error_message = excluded.error_message,
                              updated_at = excluded.updated_at
                """,
                (repo_full_name, error_message[:2000], now),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    @staticmethod
    def _row_to_job(row: sqlite3.Row | None) -> CodeReviewJob | None:
        if row is None:
            return None
        return CodeReviewJob(
            job_id=row["job_id"],
            repo_full_name=row["repo_full_name"],
            pr_number=int(row["pr_number"]),
            pr_url=row["pr_url"],
            base_ref=row["base_ref"],
            base_sha=row["base_sha"],
            head_sha=row["head_sha"],
            diff_hash=row["diff_hash"],
            requested_event_id=row["requested_event_id"],
            requested_by_login=row["requested_by_login"],
            requested_reviewer_logins=row["requested_reviewer_logins"],
            requested_team_slugs=row["requested_team_slugs"],
            notification_emails=row["notification_emails"],
            review_type=row["review_type"],
            skill_name=row["skill_name"],
            review_policy_path=row["review_policy_path"],
            status=row["status"],
            review_id=row["review_id"],
            review_path=row["review_path"],
            error_message=row["error_message"],
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )

    @staticmethod
    def _row_to_email_job(row: sqlite3.Row | None) -> CodeReviewEmailJob | None:
        if row is None:
            return None
        return CodeReviewEmailJob(
            email_job_id=row["email_job_id"],
            code_review_job_id=row["code_review_job_id"],
            review_id=row["review_id"],
            recipient_email=row["recipient_email"],
            subject=row["subject"],
            status=row["status"],
            attempts=int(row["attempts"]),
            error_message=row["error_message"],
            created_at=row["created_at"],
            sent_at=row["sent_at"],
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_csv(values: tuple[str, ...]) -> str:
    return ",".join(value.strip() for value in values if value.strip())


def _parse_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())
