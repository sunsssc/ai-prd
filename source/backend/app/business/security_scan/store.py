from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


@dataclass(frozen=True)
class SecurityScanJob:
    job_id: str
    target_type: str
    target_value: str
    context_json: str | None
    status: str
    verdict: str | None
    reason: str | None
    findings_json: str | None
    stage_dir: str | None
    error_message: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None


class SecurityScanStore:
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
                CREATE TABLE IF NOT EXISTS security_scan_jobs (
                    job_id TEXT PRIMARY KEY,
                    target_type TEXT NOT NULL CHECK(target_type IN ('path', 'url')),
                    target_value TEXT NOT NULL,
                    context_json TEXT,
                    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'done', 'failed')),
                    verdict TEXT CHECK(verdict IN ('safe', 'unsafe')),
                    reason TEXT,
                    findings_json TEXT,
                    stage_dir TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_security_scan_jobs_status_created
                ON security_scan_jobs(status, created_at ASC);
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> SecurityScanJob:
        return SecurityScanJob(**{key: row[key] for key in row.keys()})

    def create_job(self, *, job_id: str, target_type: str, target_value: str, context_json: str | None) -> SecurityScanJob:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO security_scan_jobs (job_id, target_type, target_value, context_json, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (job_id, target_type, target_value, context_json, self._utc_now()),
            )
            row = connection.execute("SELECT * FROM security_scan_jobs WHERE job_id = ?", (job_id,)).fetchone()
        assert row is not None
        return self._row_to_job(row)

    def get_job(self, *, job_id: str) -> SecurityScanJob | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT * FROM security_scan_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_job(row)

    def list_pending_jobs(self, *, limit: int = 5) -> list[SecurityScanJob]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM security_scan_jobs WHERE status = 'pending' ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._row_to_job(row) for row in rows]

    def mark_job_running(self, job_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE security_scan_jobs
                SET status = 'running', started_at = ?, error_message = NULL
                WHERE job_id = ? AND status = 'pending'
                """,
                (self._utc_now(), job_id),
            )
        return cursor.rowcount > 0

    def update_stage_dir(self, *, job_id: str, stage_dir: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE security_scan_jobs SET stage_dir = ? WHERE job_id = ?",
                (stage_dir, job_id),
            )

    def complete_job(self, *, job_id: str, verdict: str, reason: str, findings_json: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE security_scan_jobs
                SET status = 'done', verdict = ?, reason = ?, findings_json = ?, completed_at = ?
                WHERE job_id = ? AND status = 'running'
                """,
                (verdict, reason, findings_json, self._utc_now(), job_id),
            )

    def fail_job(self, *, job_id: str, error_message: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE security_scan_jobs
                SET status = 'failed', error_message = ?, completed_at = ?
                WHERE job_id = ? AND status IN ('pending', 'running')
                """,
                (error_message[:2000], self._utc_now(), job_id),
            )

    def fail_stale_running_jobs(self, *, cutoff_started_at: str, error_message: str) -> list[SecurityScanJob]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM security_scan_jobs WHERE status = 'running' AND started_at < ?",
                (cutoff_started_at,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE security_scan_jobs
                    SET status = 'failed', error_message = ?, completed_at = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (error_message[:2000], self._utc_now(), row["job_id"]),
                )
        return [self._row_to_job(row) for row in rows]

    def purge_jobs_older_than(self, *, cutoff_created_at: str) -> list[SecurityScanJob]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM security_scan_jobs WHERE created_at < ? AND status IN ('done', 'failed')",
                (cutoff_created_at,),
            ).fetchall()
            for row in rows:
                connection.execute("DELETE FROM security_scan_jobs WHERE job_id = ?", (row["job_id"],))
        return [self._row_to_job(row) for row in rows]
