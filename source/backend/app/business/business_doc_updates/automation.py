from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from threading import Lock
from typing import Any
from uuid import uuid4

from app.integrations.agent_runtime import AgentRuntimeClient, RuntimeSession


ARTIFACT_TYPE = "business_doc_update"
SKILL_NAME = "business-doc-updater"
SOURCE_TYPE = "business_doc"
SYSTEM_ACTOR = "system"
logger = logging.getLogger(__name__)

_RISK_FLAGS = {
    "rule_deletion",
    "permission",
    "funds",
    "risk_control",
    "low_confidence",
    "cross_repo_conflict",
    "insufficient_evidence",
}
_EVIDENCE_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "repo_key": {"type": "string", "minLength": 1},
        "commit": {"type": "string", "minLength": 1},
        "file": {"type": "string", "minLength": 1},
        "symbol": {"type": "string", "minLength": 1},
    },
    "required": ["repo_key", "commit", "file", "symbol"],
}
_PROPOSAL_OUTPUT_PROPERTIES = {
    "result_type": {
        "type": "string",
        "enum": ["update_required", "no_update_needed", "new_doc_candidate"],
    },
    "stable_business_change": {"type": "boolean"},
    "summary": {"type": "string"},
    "reason": {"type": "string"},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "evidence": {"type": "array", "minItems": 1, "items": _EVIDENCE_OUTPUT_SCHEMA},
    "risk_flags": {
        "type": "array",
        "items": {"type": "string", "enum": sorted(_RISK_FLAGS)},
    },
    "cross_repo_conflict": {"type": "boolean"},
    "replacement_markdown": {"type": "string"},
}
_PROPOSAL_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": _PROPOSAL_OUTPUT_PROPERTIES,
    "required": list(_PROPOSAL_OUTPUT_PROPERTIES),
}
_REVISION_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        **_PROPOSAL_OUTPUT_PROPERTIES,
        "review_resolution": {"type": "string", "enum": ["agree", "disagree"]},
    },
    "required": [*_PROPOSAL_OUTPUT_PROPERTIES, "review_resolution"],
}
_REVIEW_OUTPUT_PROPERTIES = {
    "decision": {"type": "string", "enum": ["approve", "reject", "manual"]},
    "feedback": {"type": "string"},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "missing_evidence": {"type": "array", "items": {"type": "string"}},
    "boundary_violations": {"type": "array", "items": {"type": "string"}},
    "risk_flags": {
        "type": "array",
        "items": {"type": "string", "enum": sorted(_RISK_FLAGS)},
    },
}
_REVIEW_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": _REVIEW_OUTPUT_PROPERTIES,
    "required": list(_REVIEW_OUTPUT_PROPERTIES),
}


@dataclass(frozen=True)
class BusinessDocRepositoryConfig:
    repo_key: str
    repo_full_name: str
    repo_path: Path
    default_branch: str
    role: str
    scopes: tuple[str, ...]
    priority: str

    @property
    def required(self) -> bool:
        return self.priority == "P0"


@dataclass(frozen=True)
class RepositorySnapshot:
    repo_key: str
    repo_full_name: str
    repo_path: str
    default_branch: str
    branch: str
    head_sha: str
    role: str
    scopes: tuple[str, ...]
    priority: str


@dataclass(frozen=True)
class BusinessDocUpdateRun:
    run_id: str
    mode: str
    status: str
    snapshot_id: str
    repo_key: str | None
    base_sha: str | None
    head_sha: str | None
    total_items: int
    completed_items: int
    failed_items: int
    snapshot_json: str
    error_message: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True)
class BusinessDocUpdateItem:
    item_id: str
    run_id: str
    source_path: str
    source_hash: str
    result_type: str | None
    status: str
    update_id: str | None
    update_path: str | None
    evidence_json: str
    review_status: str | None
    review_feedback: str | None
    generator_review: str | None
    confidence: float | None
    apply_status: str | None
    error_message: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None


@dataclass(frozen=True)
class BusinessDocRepoCursor:
    repo_key: str
    analyzed_head: str
    last_run_id: str
    updated_at: str


@dataclass(frozen=True)
class BusinessDocUpdateRecord:
    update_id: str
    artifact_type: str
    path: str
    source_path: str | None
    run_id: str
    mode: str
    repo_key: str | None
    base_sha: str | None
    head_sha: str | None
    status: str
    review_status: str | None
    confidence: float | None
    apply_status: str | None
    created_at: str
    completed_at: str | None


@dataclass(frozen=True)
class BusinessDocUpdateDetail(BusinessDocUpdateRecord):
    skill: str = SKILL_NAME
    affected_docs: tuple[str, ...] = ()
    content: str = ""
    evidence: tuple[dict[str, object], ...] = ()
    review_feedback: str | None = None
    generator_review: str | None = None
    frontmatter: dict[str, str] | None = None


def load_business_doc_repository_configs(*, base_dir: Path, config_path: str) -> tuple[BusinessDocRepositoryConfig, ...]:
    if not config_path.strip():
        return ()
    candidate = Path(config_path)
    config_file = candidate.resolve() if candidate.is_absolute() else (base_dir / candidate).resolve()
    if not config_file.is_file():
        raise FileNotFoundError(f"代码项目配置不存在：{config_file}")
    payload = json.loads(config_file.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("代码项目配置必须是 JSON object。")
    configs: list[BusinessDocRepositoryConfig] = []
    for raw in payload.get("repositories", []):
        if not isinstance(raw, dict) or not bool(raw.get("business_doc_enabled", False)):
            continue
        repo_full_name = str(raw.get("repo_full_name") or "").strip()
        workspace_repo_path = str(raw.get("workspace_repo_path") or "").strip()
        default_branch = str(raw.get("default_branch") or "").strip()
        role = str(raw.get("business_doc_role") or "").strip().lower()
        if not repo_full_name or not workspace_repo_path or not default_branch:
            raise ValueError("启用业务文档更新的仓库必须配置 repo_full_name、workspace_repo_path 和 default_branch。")
        if role not in {"domain", "gateway", "frontend"}:
            raise ValueError(f"仓库 {repo_full_name} 的 business_doc_role 无效。")
        repo_path = Path(workspace_repo_path)
        if not repo_path.is_absolute():
            repo_path = base_dir / repo_path
        scopes = tuple(_normalize_scope(value) for value in raw.get("business_doc_scopes", []) if str(value).strip())
        priority = str(raw.get("business_doc_priority") or "P1").strip().upper()
        if priority not in {"P0", "P1"}:
            raise ValueError(f"仓库 {repo_full_name} 的 business_doc_priority 无效。")
        configs.append(
            BusinessDocRepositoryConfig(
                repo_key=repo_path.name,
                repo_full_name=repo_full_name,
                repo_path=repo_path.resolve(),
                default_branch=default_branch,
                role=role,
                scopes=scopes,
                priority=priority,
            )
        )
    repo_keys = [config.repo_key for config in configs]
    if len(repo_keys) != len(set(repo_keys)):
        raise ValueError("业务文档仓库配置的 repo_key 重复。")
    return tuple(configs)


class BusinessDocUpdateStore:
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
                CREATE TABLE IF NOT EXISTS business_doc_update_runs (
                    run_id TEXT PRIMARY KEY,
                    mode TEXT NOT NULL CHECK(mode IN ('full', 'incremental')),
                    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'completed_with_errors', 'failed')),
                    snapshot_id TEXT NOT NULL,
                    repo_key TEXT,
                    base_sha TEXT,
                    head_sha TEXT,
                    total_items INTEGER NOT NULL DEFAULT 0,
                    completed_items INTEGER NOT NULL DEFAULT 0,
                    failed_items INTEGER NOT NULL DEFAULT 0,
                    snapshot_json TEXT NOT NULL DEFAULT '[]',
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_business_doc_runs_full_snapshot
                ON business_doc_update_runs(mode, snapshot_id) WHERE mode = 'full';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_business_doc_runs_incremental_range
                ON business_doc_update_runs(mode, repo_key, base_sha, head_sha) WHERE mode = 'incremental';
                CREATE INDEX IF NOT EXISTS idx_business_doc_runs_status_created
                ON business_doc_update_runs(status, created_at ASC);

                CREATE TABLE IF NOT EXISTS business_doc_update_items (
                    item_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    source_hash TEXT NOT NULL,
                    result_type TEXT CHECK(result_type IN ('update_required', 'no_update_needed', 'new_doc_candidate')),
                    status TEXT NOT NULL CHECK(status IN ('pending', 'running', 'completed', 'failed')),
                    update_id TEXT,
                    update_path TEXT,
                    evidence_json TEXT NOT NULL DEFAULT '[]',
                    review_status TEXT,
                    review_feedback TEXT,
                    generator_review TEXT,
                    confidence REAL,
                    apply_status TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    UNIQUE(run_id, source_path),
                    FOREIGN KEY(run_id) REFERENCES business_doc_update_runs(run_id)
                );
                CREATE INDEX IF NOT EXISTS idx_business_doc_items_status_created
                ON business_doc_update_items(status, created_at ASC);
                CREATE INDEX IF NOT EXISTS idx_business_doc_items_update_id
                ON business_doc_update_items(update_id);

                CREATE TABLE IF NOT EXISTS business_doc_repo_cursors (
                    repo_key TEXT PRIMARY KEY,
                    analyzed_head TEXT NOT NULL,
                    last_run_id TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            self._initialize_actions(connection)

    def _initialize_actions(self, connection: sqlite3.Connection) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'business_doc_update_actions'"
        ).fetchone()
        existing_sql = str(row["sql"] or "") if row is not None else ""
        if existing_sql and "actor_role" not in existing_sql:
            connection.executescript(
                """
                ALTER TABLE business_doc_update_actions RENAME TO business_doc_update_actions_legacy;
                CREATE TABLE business_doc_update_actions (
                    action_id TEXT PRIMARY KEY,
                    update_id TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('applied', 'ignored', 'superseded')),
                    user_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL
                );
                INSERT INTO business_doc_update_actions (action_id, update_id, action, user_id, actor_role, reason, created_at)
                SELECT action_id, update_id, action, user_id, 'user', reason, created_at
                FROM business_doc_update_actions_legacy;
                DROP TABLE business_doc_update_actions_legacy;
                """
            )
        else:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS business_doc_update_actions (
                    action_id TEXT PRIMARY KEY,
                    update_id TEXT NOT NULL,
                    action TEXT NOT NULL CHECK(action IN ('applied', 'ignored', 'superseded')),
                    user_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_business_doc_update_actions_update_id ON business_doc_update_actions(update_id, created_at DESC)"
        )

    def create_run(
        self,
        *,
        mode: str,
        snapshot_id: str,
        snapshots: tuple[RepositorySnapshot, ...],
        repo_key: str | None = None,
        base_sha: str | None = None,
        head_sha: str | None = None,
        status: str = "pending",
        error_message: str | None = None,
    ) -> BusinessDocUpdateRun:
        now = _utc_now()
        run_id = str(uuid4())
        snapshot_json = json.dumps([snapshot.__dict__ for snapshot in snapshots], ensure_ascii=False, sort_keys=True)
        with self._lock, self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO business_doc_update_runs (
                        run_id, mode, status, snapshot_id, repo_key, base_sha, head_sha,
                        snapshot_json, error_message, created_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        mode,
                        status,
                        snapshot_id,
                        repo_key,
                        base_sha,
                        head_sha,
                        snapshot_json,
                        error_message,
                        now,
                        now if status == "failed" else None,
                    ),
                )
            except sqlite3.IntegrityError:
                if mode == "full":
                    row = connection.execute(
                        "SELECT * FROM business_doc_update_runs WHERE mode = 'full' AND snapshot_id = ?",
                        (snapshot_id,),
                    ).fetchone()
                else:
                    row = connection.execute(
                        """
                        SELECT * FROM business_doc_update_runs
                        WHERE mode = 'incremental' AND repo_key = ? AND base_sha = ? AND head_sha = ?
                        """,
                        (repo_key, base_sha, head_sha),
                    ).fetchone()
                run = self._row_to_run(row)
                if run is None:
                    raise
                return run
        run = self.get_run(run_id)
        if run is None:
            raise RuntimeError("业务文档更新运行创建失败。")
        return run

    def add_items(self, run_id: str, items: tuple[tuple[str, str], ...]) -> None:
        now = _utc_now()
        with self._lock, self._connect() as connection:
            for source_path, source_hash in items:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO business_doc_update_items (
                        item_id, run_id, source_path, source_hash, status, evidence_json, created_at
                    ) VALUES (?, ?, ?, ?, 'pending', '[]', ?)
                    """,
                    (str(uuid4()), run_id, source_path, source_hash, now),
                )
            total = connection.execute(
                "SELECT COUNT(*) AS count FROM business_doc_update_items WHERE run_id = ?", (run_id,)
            ).fetchone()["count"]
            connection.execute(
                "UPDATE business_doc_update_runs SET total_items = ? WHERE run_id = ?",
                (int(total), run_id),
            )

    def list_runs(self, *, limit: int = 50) -> list[BusinessDocUpdateRun]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM business_doc_update_runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [run for row in rows if (run := self._row_to_run(row)) is not None]

    def get_active_incremental_run(self, repo_key: str) -> BusinessDocUpdateRun | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM business_doc_update_runs
                WHERE mode = 'incremental' AND repo_key = ? AND status IN ('pending', 'running')
                ORDER BY created_at LIMIT 1
                """,
                (repo_key,),
            ).fetchone()
        return self._row_to_run(row)

    def get_run(self, run_id: str) -> BusinessDocUpdateRun | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM business_doc_update_runs WHERE run_id = ?", (run_id,)).fetchone()
        return self._row_to_run(row)

    def fail_empty_run(self, *, run_id: str, error_message: str) -> BusinessDocUpdateRun:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE business_doc_update_runs
                SET status = 'failed', error_message = ?, completed_at = ?
                WHERE run_id = ? AND total_items = 0
                """,
                (error_message[:2000], _utc_now(), run_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("只有空运行可以标记为预检失败。")
        run = self.get_run(run_id)
        if run is None:
            raise FileNotFoundError("业务文档更新运行不存在。")
        return run

    def reopen_empty_run(self, *, run_id: str, error_message: str | None) -> BusinessDocUpdateRun:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE business_doc_update_runs
                SET status = 'pending', error_message = ?, started_at = NULL, completed_at = NULL
                WHERE run_id = ? AND total_items = 0 AND status IN ('failed', 'completed', 'completed_with_errors')
                """,
                (error_message, run_id),
            )
            if cursor.rowcount == 0:
                raise ValueError("只有已结束的空运行可以重新打开。")
        run = self.get_run(run_id)
        if run is None:
            raise FileNotFoundError("业务文档更新运行不存在。")
        return run

    def list_run_items(self, run_id: str) -> list[BusinessDocUpdateItem]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM business_doc_update_items WHERE run_id = ? ORDER BY created_at, source_path", (run_id,)
            ).fetchall()
        return [item for row in rows if (item := self._row_to_item(row)) is not None]

    def claim_pending_items(self, *, limit: int = 3) -> list[BusinessDocUpdateItem]:
        now = _utc_now()
        claimed: list[BusinessDocUpdateItem] = []
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """
                SELECT i.* FROM business_doc_update_items i
                JOIN business_doc_update_runs r ON r.run_id = i.run_id
                WHERE i.status = 'pending' AND r.status IN ('pending', 'running')
                ORDER BY r.created_at, i.created_at LIMIT ?
                """,
                (limit,),
            ).fetchall()
            for row in rows:
                cursor = connection.execute(
                    "UPDATE business_doc_update_items SET status = 'running', started_at = ? WHERE item_id = ? AND status = 'pending'",
                    (now, row["item_id"]),
                )
                if cursor.rowcount:
                    connection.execute(
                        "UPDATE business_doc_update_runs SET status = 'running', started_at = COALESCE(started_at, ?) WHERE run_id = ?",
                        (now, row["run_id"]),
                    )
                    refreshed = connection.execute(
                        "SELECT * FROM business_doc_update_items WHERE item_id = ?", (row["item_id"],)
                    ).fetchone()
                    item = self._row_to_item(refreshed)
                    if item is not None:
                        claimed.append(item)
        return claimed

    def complete_item(
        self,
        *,
        item_id: str,
        result_type: str,
        update_id: str | None,
        update_path: str | None,
        evidence: list[dict[str, object]],
        review_status: str | None,
        review_feedback: str | None,
        generator_review: str | None,
        confidence: float,
        apply_status: str | None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE business_doc_update_items
                SET status = 'completed', result_type = ?, update_id = ?, update_path = ?, evidence_json = ?,
                    review_status = ?, review_feedback = ?, generator_review = ?, confidence = ?, apply_status = ?,
                    error_message = NULL, completed_at = ?
                WHERE item_id = ?
                """,
                (
                    result_type,
                    update_id,
                    update_path,
                    json.dumps(evidence, ensure_ascii=False),
                    review_status,
                    review_feedback,
                    generator_review,
                    confidence,
                    apply_status,
                    _utc_now(),
                    item_id,
                ),
            )

    def fail_item(self, *, item_id: str, error_message: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE business_doc_update_items SET status = 'failed', error_message = ?, completed_at = ? WHERE item_id = ?",
                (error_message[:2000], _utc_now(), item_id),
            )

    def refresh_run(self, run_id: str) -> BusinessDocUpdateRun:
        with self._lock, self._connect() as connection:
            counts = connection.execute(
                """
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                       SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed,
                       SUM(CASE WHEN status IN ('pending', 'running') THEN 1 ELSE 0 END) AS active
                FROM business_doc_update_items WHERE run_id = ?
                """,
                (run_id,),
            ).fetchone()
            total = int(counts["total"] or 0)
            completed = int(counts["completed"] or 0)
            failed = int(counts["failed"] or 0)
            active = int(counts["active"] or 0)
            run = self._row_to_run(
                connection.execute("SELECT * FROM business_doc_update_runs WHERE run_id = ?", (run_id,)).fetchone()
            )
            if run is None:
                raise FileNotFoundError("业务文档更新运行不存在。")
            status = run.status
            completed_at = run.completed_at
            if status != "failed" and total == 0:
                status = "completed_with_errors" if run.error_message else "completed"
                completed_at = _utc_now()
            elif status != "failed" and active == 0:
                status = "completed_with_errors" if failed or run.error_message else "completed"
                completed_at = _utc_now()
            connection.execute(
                """
                UPDATE business_doc_update_runs
                SET status = ?, total_items = ?, completed_items = ?, failed_items = ?, completed_at = ?
                WHERE run_id = ?
                """,
                (status, total, completed, failed, completed_at, run_id),
            )
        refreshed = self.get_run(run_id)
        if refreshed is None:
            raise FileNotFoundError("业务文档更新运行不存在。")
        return refreshed

    def retry_failed_items(self, run_id: str) -> BusinessDocUpdateRun:
        with self._lock, self._connect() as connection:
            run = self._row_to_run(
                connection.execute("SELECT * FROM business_doc_update_runs WHERE run_id = ?", (run_id,)).fetchone()
            )
            if run is None:
                raise FileNotFoundError("业务文档更新运行不存在。")
            cursor = connection.execute(
                """
                UPDATE business_doc_update_items
                SET status = 'pending', error_message = NULL, started_at = NULL, completed_at = NULL
                WHERE run_id = ? AND status = 'failed'
                """,
                (run_id,),
            )
            if cursor.rowcount == 0:
                raise ValueError("该运行没有可重试的失败任务。")
            connection.execute(
                """
                UPDATE business_doc_update_runs
                SET status = 'pending', failed_items = 0, error_message = NULL, completed_at = NULL
                WHERE run_id = ?
                """,
                (run_id,),
            )
        refreshed = self.get_run(run_id)
        if refreshed is None:
            raise FileNotFoundError("业务文档更新运行不存在。")
        return refreshed

    def requeue_running(self) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE business_doc_update_items SET status = 'pending', started_at = NULL WHERE status = 'running'"
            )
            connection.execute(
                "UPDATE business_doc_update_runs SET status = 'pending', started_at = NULL WHERE status = 'running'"
            )

    def get_cursor(self, repo_key: str) -> BusinessDocRepoCursor | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM business_doc_repo_cursors WHERE repo_key = ?", (repo_key,)
            ).fetchone()
        if row is None:
            return None
        return BusinessDocRepoCursor(
            repo_key=row["repo_key"], analyzed_head=row["analyzed_head"], last_run_id=row["last_run_id"], updated_at=row["updated_at"]
        )

    def set_cursor(self, *, repo_key: str, analyzed_head: str, last_run_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO business_doc_repo_cursors (repo_key, analyzed_head, last_run_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(repo_key) DO UPDATE SET analyzed_head = excluded.analyzed_head,
                    last_run_id = excluded.last_run_id, updated_at = excluded.updated_at
                """,
                (repo_key, analyzed_head, last_run_id, _utc_now()),
            )

    def get_item_by_update_id(self, update_id: str) -> BusinessDocUpdateItem | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM business_doc_update_items WHERE update_id = ?", (update_id,)
            ).fetchone()
        return self._row_to_item(row)

    def set_item_apply_status(self, *, update_id: str, apply_status: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE business_doc_update_items SET apply_status = ? WHERE update_id = ?",
                (apply_status, update_id),
            )

    def record_action(self, *, update_id: str, action: str, user_id: str, actor_role: str, reason: str | None) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO business_doc_update_actions (action_id, update_id, action, user_id, actor_role, reason, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (str(uuid4()), update_id, action, user_id, actor_role, reason, _utc_now()),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @staticmethod
    def _row_to_run(row: sqlite3.Row | None) -> BusinessDocUpdateRun | None:
        if row is None:
            return None
        return BusinessDocUpdateRun(**{field: row[field] for field in BusinessDocUpdateRun.__dataclass_fields__})

    @staticmethod
    def _row_to_item(row: sqlite3.Row | None) -> BusinessDocUpdateItem | None:
        if row is None:
            return None
        values = {field: row[field] for field in BusinessDocUpdateItem.__dataclass_fields__}
        values["confidence"] = float(values["confidence"]) if values["confidence"] is not None else None
        return BusinessDocUpdateItem(**values)


class BusinessDocUpdateService:
    def __init__(
        self,
        *,
        base_dir: Path,
        business_docs_root: Path,
        skills_root: Path,
        store: BusinessDocUpdateStore,
        runtime_client: AgentRuntimeClient,
        project_config_path: str,
        auto_apply_confidence: float = 0.85,
    ) -> None:
        self.base_dir = base_dir.resolve()
        self.business_docs_mount_root = business_docs_root.absolute()
        self.knowledge_mount_root = business_docs_root.absolute().parent
        self.knowledge_root = self.knowledge_mount_root.resolve()
        self.business_docs_root = business_docs_root.resolve()
        self.skills_root = skills_root.resolve()
        self.reviews_root = self.knowledge_root / "__reviews__" / "business-doc-update"
        self.store = store
        self.runtime_client = runtime_client
        self.project_config_path = project_config_path
        self.auto_apply_confidence = auto_apply_confidence
        self._business_doc_git_lock = Lock()

    def repository_configs(self) -> tuple[BusinessDocRepositoryConfig, ...]:
        return load_business_doc_repository_configs(base_dir=self.base_dir, config_path=self.project_config_path)

    def code_sync_default_branches(self) -> dict[str, str]:
        return {config.repo_key: config.default_branch for config in self.repository_configs()}

    def code_sync_repository_paths(self) -> dict[str, Path]:
        return {config.repo_key: config.repo_path for config in self.repository_configs()}

    def create_full_run(self) -> BusinessDocUpdateRun:
        configs = self.repository_configs()
        if not configs:
            raise ValueError("没有启用 business_doc_enabled 的仓库。")
        snapshots, errors = self._capture_snapshots(configs)
        required_errors = [message for config, message in errors if config.required]
        doc_paths = self._business_doc_paths()
        docs_error = self._business_docs_preflight_error(doc_paths)
        fatal_errors = [*required_errors, *([docs_error] if docs_error else [])]
        error_message = "；".join([*(message for _, message in errors), *([docs_error] if docs_error else [])]) or None
        snapshot_id = _hash_json(
            {
                "snapshots": [snapshot.__dict__ for snapshot in snapshots],
                "errors": [(config.repo_key, message) for config, message in errors],
            }
        )
        run = self.store.create_run(
            mode="full",
            snapshot_id=snapshot_id,
            snapshots=snapshots,
            status="failed" if fatal_errors else "pending",
            error_message=error_message,
        )
        if fatal_errors:
            return self.store.fail_empty_run(run_id=run.run_id, error_message=error_message or "全量校准预检失败。")
        if run.total_items:
            return run
        if run.status in {"failed", "completed", "completed_with_errors"}:
            run = self.store.reopen_empty_run(run_id=run.run_id, error_message=error_message)
        items = tuple((path, self.compute_source_hash(path)) for path in doc_paths)
        self.store.add_items(run.run_id, items)
        refreshed = self.store.get_run(run.run_id)
        if refreshed is None:
            raise RuntimeError("全量校准运行创建失败。")
        return refreshed

    def enqueue_incremental(self, *, repo_key: str, current_head: str | None = None) -> BusinessDocUpdateRun | None:
        config = next((item for item in self.repository_configs() if item.repo_key == repo_key), None)
        if config is None:
            return None
        active = self.store.get_active_incremental_run(repo_key)
        if active is not None:
            return active
        snapshot = self._capture_snapshot(config)
        if current_head and current_head != snapshot.head_sha:
            logger.info("仓库 %s 在同步回调后继续前进，按最新 HEAD %s 建立增量任务", repo_key, snapshot.head_sha)
        head_sha = snapshot.head_sha
        cursor = self.store.get_cursor(repo_key)
        if cursor is None:
            logger.info("仓库 %s 尚未建立全量基线，跳过增量任务", repo_key)
            return None
        if cursor.analyzed_head == head_sha:
            return None
        if not _git_is_ancestor(config.repo_path, cursor.analyzed_head, head_sha):
            message = "analyzed_head 不是当前 HEAD 的祖先，需要重新全量校准。"
            return self.store.create_run(
                mode="incremental",
                snapshot_id=_hash_json([snapshot.__dict__]),
                snapshots=(snapshot,),
                repo_key=repo_key,
                base_sha=cursor.analyzed_head,
                head_sha=head_sha,
                status="failed",
                error_message=message,
            )
        snapshots, snapshot_errors = self._capture_snapshots(self.repository_configs())
        trigger_snapshot = next((value for value in snapshots if value.repo_key == repo_key), None)
        if trigger_snapshot is None:
            raise ValueError(f"增量仓库快照不可用：{repo_key}")
        snapshot_payload = {
            "snapshots": [value.__dict__ for value in snapshots],
            "errors": [(value.repo_key, message) for value, message in snapshot_errors],
        }
        run = self.store.create_run(
            mode="incremental",
            snapshot_id=_hash_json(snapshot_payload),
            snapshots=snapshots,
            repo_key=repo_key,
            base_sha=cursor.analyzed_head,
            head_sha=head_sha,
            error_message="；".join(message for _, message in snapshot_errors) or None,
        )
        if run.total_items:
            return run
        candidate_paths = self._scoped_document_paths(config)
        if not candidate_paths:
            candidate_paths = ("",)
        items = tuple((path, self.compute_source_hash(path) if path else "") for path in candidate_paths)
        self.store.add_items(run.run_id, items)
        refreshed = self.store.get_run(run.run_id)
        if refreshed is None:
            raise RuntimeError("增量更新运行创建失败。")
        return refreshed

    def recover_incremental_runs(self) -> list[BusinessDocUpdateRun]:
        self.store.requeue_running()
        created: list[BusinessDocUpdateRun] = []
        for config in self.repository_configs():
            try:
                run = self.enqueue_incremental(repo_key=config.repo_key)
            except Exception:
                logger.exception("恢复业务文档增量运行失败: repo=%s", config.repo_key)
                continue
            if run is not None:
                created.append(run)
        return created

    async def process_pending_items(self, *, limit: int = 3) -> list[BusinessDocUpdateItem]:
        concurrency = min(max(limit, 1), 3)
        processed: list[BusinessDocUpdateItem] = []
        active: dict[asyncio.Task[None], BusinessDocUpdateItem] = {}
        try:
            while True:
                available_slots = concurrency - len(active)
                if available_slots:
                    claimed = self.store.claim_pending_items(limit=available_slots)
                    processed.extend(claimed)
                    active.update({asyncio.create_task(self._process_item(item)): item for item in claimed})
                if not active:
                    break
                done, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    await task
                    active.pop(task)
        finally:
            if active:
                for task in active:
                    task.cancel()
                await asyncio.gather(*active, return_exceptions=True)

        run_ids = {item.run_id for item in processed}
        for run_id in run_ids:
            run = self.store.refresh_run(run_id)
            if run.status in {"completed", "completed_with_errors"} and not run.failed_items:
                self._advance_run_cursors(run)
                for snapshot in self._cursor_snapshots(run):
                    self.enqueue_incremental(repo_key=snapshot.repo_key)
        return processed

    async def job_loop(self, *, interval_seconds: int = 30) -> None:
        self.recover_incremental_runs()
        logger.info("业务文档自动更新 worker 已启动，间隔=%ds", interval_seconds)
        while True:
            try:
                await self.process_pending_items(limit=3)
            except Exception:
                logger.exception("业务文档自动更新 worker 出错，下次将继续重试")
            await asyncio.sleep(interval_seconds)

    def list_runs(self, *, limit: int = 50) -> list[BusinessDocUpdateRun]:
        return self.store.list_runs(limit=limit)

    def get_run(self, *, run_id: str) -> BusinessDocUpdateRun:
        run = self.store.get_run(run_id)
        if run is None:
            raise FileNotFoundError("业务文档更新运行不存在。")
        return run

    def get_run_items(self, *, run_id: str) -> list[BusinessDocUpdateItem]:
        self.get_run(run_id=run_id)
        return self.store.list_run_items(run_id)

    def retry_run(self, *, run_id: str) -> BusinessDocUpdateRun:
        return self.store.retry_failed_items(run_id)

    def list_updates(self, *, source_path: str) -> list[BusinessDocUpdateRecord]:
        self._resolve_business_doc_path(source_path)
        return self._updates_from_meta(self._read_meta(source_path))

    def get_update_detail(self, *, update_id: str) -> BusinessDocUpdateDetail:
        item = self.store.get_item_by_update_id(update_id)
        if item is None or not item.update_path:
            raise FileNotFoundError("业务文档更新提案不存在。")
        update_file = self._resolve_update_path(item.update_path)
        raw = update_file.read_text(encoding="utf-8", errors="replace")
        frontmatter, body = _split_frontmatter(raw)
        run = self.get_run(run_id=item.run_id)
        evidence = _json_list(item.evidence_json)
        return BusinessDocUpdateDetail(
            update_id=update_id,
            artifact_type=ARTIFACT_TYPE,
            path=item.update_path,
            source_path=item.source_path or None,
            run_id=run.run_id,
            mode=run.mode,
            repo_key=run.repo_key,
            base_sha=run.base_sha,
            head_sha=run.head_sha,
            status=frontmatter.get("status", item.apply_status or "pending"),
            review_status=item.review_status,
            confidence=item.confidence,
            apply_status=item.apply_status,
            created_at=frontmatter.get("created_at", item.created_at),
            completed_at=item.completed_at,
            affected_docs=(item.source_path,) if item.source_path else (),
            content=body.strip(),
            evidence=tuple(value for value in evidence if isinstance(value, dict)),
            review_feedback=item.review_feedback,
            generator_review=item.generator_review,
            frontmatter=frontmatter,
        )

    def ignore_update(
        self,
        *,
        update_id: str,
        user_id: str,
        reason: str | None = None,
        actor_role: str = "user",
    ) -> BusinessDocUpdateDetail:
        detail = self.get_update_detail(update_id=update_id)
        if detail.status != "pending":
            raise ValueError("只有 pending 状态的提案可以忽略。")
        self.store.record_action(update_id=update_id, action="ignored", user_id=user_id, actor_role=actor_role, reason=reason)
        self._set_update_status(detail, "ignored")
        self.store.set_item_apply_status(update_id=update_id, apply_status="ignored")
        return self.get_update_detail(update_id=update_id)

    def apply_update(
        self,
        *,
        update_id: str,
        user_id: str,
        reason: str | None = None,
        actor_role: str = "user",
    ) -> BusinessDocUpdateDetail:
        detail = self.get_update_detail(update_id=update_id)
        if detail.status != "pending":
            raise ValueError("只有 pending 状态的提案可以应用。")
        self._apply_detail(detail)
        self.store.record_action(update_id=update_id, action="applied", user_id=user_id, actor_role=actor_role, reason=reason)
        self._set_update_status(detail, "applied")
        self.store.set_item_apply_status(update_id=update_id, apply_status="applied")
        return self.get_update_detail(update_id=update_id)

    def build_doc_update_badge(self, *, source_path: str) -> dict[str, object] | None:
        try:
            target = self._resolve_business_doc_target(source_path)
        except FileNotFoundError:
            return None
        paths = [target] if target.is_file() else list(target.rglob("*.md")) if target.is_dir() else []
        pending: list[BusinessDocUpdateRecord] = []
        for path in paths:
            if self._is_hidden_business_doc_path(path):
                continue
            for update in self._updates_from_meta(self._read_meta(self._display_path(path))):
                if update.status == "pending" and update.source_path and self._stored_hash(update.source_path, update.update_id) == self.compute_source_hash(update.source_path):
                    pending.append(update)
        if not pending:
            return None
        latest = max(pending, key=lambda value: value.created_at)
        return {"type": ARTIFACT_TYPE, "pending_count": len({value.update_id for value in pending}), "latest_update_id": latest.update_id}

    def compute_source_hash(self, source_path: str) -> str:
        source_file = self._resolve_business_doc_path(source_path)
        digest = hashlib.sha256()
        digest.update(source_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(source_file.read_bytes())
        return f"sha256:{digest.hexdigest()}"

    async def _process_item(self, item: BusinessDocUpdateItem) -> None:
        try:
            run = self.get_run(run_id=item.run_id)
            snapshots = tuple(RepositorySnapshot(**value) for value in json.loads(run.snapshot_json))
            source_text = self._resolve_business_doc_path(item.source_path).read_text(encoding="utf-8") if item.source_path else ""
            diff_text = self._run_diff(run, snapshots)
            skill_text = self._skill_text()
            generator_session = await self.runtime_client.create_or_resume_session(
                runtime_session_id=None,
                working_directory=str(self.base_dir),
                system_prompt=_GENERATOR_SYSTEM_PROMPT,
            )
            proposal = await self._send_json(
                session=generator_session,
                prompt=self._generation_prompt(run, snapshots, item.source_path, source_text, diff_text, skill_text),
                metadata={
                    "task": "business_doc_update_generation",
                    "run_id": run.run_id,
                    "item_id": item.item_id,
                    "output_schema": _PROPOSAL_OUTPUT_SCHEMA,
                },
            )
            proposal = _validate_proposal(proposal, source_path=item.source_path)
            if proposal["result_type"] == "no_update_needed":
                self.store.complete_item(
                    item_id=item.item_id,
                    result_type="no_update_needed",
                    update_id=None,
                    update_path=None,
                    evidence=proposal["evidence"],
                    review_status="not_required",
                    review_feedback=None,
                    generator_review=None,
                    confidence=float(proposal["confidence"]),
                    apply_status="not_applicable",
                )
                return

            review_session = await self.runtime_client.create_or_resume_session(
                runtime_session_id=None,
                working_directory=str(self.base_dir),
                system_prompt=_REVIEWER_SYSTEM_PROMPT,
            )
            review = await self._send_json(
                session=review_session,
                prompt=self._review_prompt(run, snapshots, item.source_path, source_text, diff_text, proposal),
                metadata={
                    "task": "business_doc_update_review",
                    "run_id": run.run_id,
                    "item_id": item.item_id,
                    "output_schema": _REVIEW_OUTPUT_SCHEMA,
                },
            )
            try:
                review = _validate_review(review)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{exc} 原始审核结果：{_safe_agent_payload(review)}") from exc
            revised = await self._send_json(
                session=generator_session,
                prompt=self._revision_prompt(proposal, review),
                metadata={
                    "task": "business_doc_update_revision",
                    "run_id": run.run_id,
                    "item_id": item.item_id,
                    "output_schema": _REVISION_OUTPUT_SCHEMA,
                },
            )
            revised = _validate_proposal(revised, source_path=item.source_path, require_resolution=True)
            if revised["result_type"] == "no_update_needed":
                self.store.complete_item(
                    item_id=item.item_id,
                    result_type="no_update_needed",
                    update_id=None,
                    update_path=None,
                    evidence=revised["evidence"],
                    review_status=review["decision"],
                    review_feedback=review["feedback"],
                    generator_review=revised["review_resolution"],
                    confidence=float(revised["confidence"]),
                    apply_status="not_applicable",
                )
                return

            update_id, update_path = self._write_proposal(run, item, revised, review)
            self.store.complete_item(
                item_id=item.item_id,
                result_type=revised["result_type"],
                update_id=update_id,
                update_path=update_path,
                evidence=revised["evidence"],
                review_status=review["decision"],
                review_feedback=review["feedback"],
                generator_review=revised["review_resolution"],
                confidence=float(revised["confidence"]),
                apply_status="manual_required",
            )
            auto_apply = run.mode == "incremental" and self._can_auto_apply(run, revised, review, item)
            apply_status = "manual_required"
            if auto_apply:
                detail = self.get_update_detail(update_id=update_id)
                try:
                    self._apply_detail(detail)
                except ValueError:
                    apply_status = "manual_required"
                else:
                    self.store.record_action(
                        update_id=update_id,
                        action="applied",
                        user_id=SYSTEM_ACTOR,
                        actor_role="generator_agent",
                        reason="通过独立审核与生成 Agent 复核，满足自动应用条件。",
                    )
                    self._set_update_status(detail, "applied")
                    apply_status = "applied"
            self.store.complete_item(
                item_id=item.item_id,
                result_type=revised["result_type"],
                update_id=update_id,
                update_path=update_path,
                evidence=revised["evidence"],
                review_status=review["decision"],
                review_feedback=review["feedback"],
                generator_review=revised["review_resolution"],
                confidence=float(revised["confidence"]),
                apply_status=apply_status,
            )
        except Exception as exc:
            self.store.fail_item(item_id=item.item_id, error_message=str(exc))
            logger.exception("业务文档分析任务失败: item_id=%s", item.item_id)

    async def _send_json(self, *, session: RuntimeSession, prompt: str, metadata: dict[str, object]) -> dict[str, Any]:
        chunks: list[str] = []
        final_message = ""
        async for event in self.runtime_client.send_message_stream(session=session, message=prompt, metadata=metadata):
            if event.type == "session" and str(event.data.get("session_id") or "").strip():
                session.session_id = str(event.data["session_id"])
            elif event.type == "delta" and isinstance(event.data.get("text"), str):
                chunks.append(str(event.data["text"]))
            elif event.type == "message" and isinstance(event.data.get("content"), str):
                final_message = str(event.data["content"])
            elif event.type == "complete" and not final_message and isinstance(event.data.get("result"), str):
                final_message = str(event.data["result"])
        raw = (final_message or "".join(chunks)).strip()
        if not raw:
            raise ValueError("Agent 未返回最终 JSON。")
        match = re.fullmatch(r"(?:```json\s*)?([\s\S]*?)(?:\s*```)?", raw)
        if match is None:
            raise ValueError("Agent 未返回合法 JSON。")
        payload = json.loads(match.group(1))
        if not isinstance(payload, dict):
            raise ValueError("Agent JSON 必须是 object。")
        return payload

    def _capture_snapshots(
        self, configs: tuple[BusinessDocRepositoryConfig, ...]
    ) -> tuple[tuple[RepositorySnapshot, ...], list[tuple[BusinessDocRepositoryConfig, str]]]:
        snapshots: list[RepositorySnapshot] = []
        errors: list[tuple[BusinessDocRepositoryConfig, str]] = []
        for config in configs:
            try:
                snapshots.append(self._capture_snapshot(config))
            except Exception as exc:
                errors.append((config, f"{config.repo_key}: {exc}"))
        role_priority = {"domain": 0, "gateway": 1, "frontend": 2}
        snapshots.sort(key=lambda snapshot: (role_priority[snapshot.role], snapshot.repo_key))
        return tuple(snapshots), errors

    def _capture_snapshot(self, config: BusinessDocRepositoryConfig) -> RepositorySnapshot:
        if not config.repo_path.is_dir() or not (config.repo_path / ".git").exists():
            raise FileNotFoundError("仓库路径不存在或不是 Git 仓库。")
        branch = _git(config.repo_path, "branch", "--show-current")
        if branch != config.default_branch:
            raise ValueError(f"当前分支 {branch or '(detached)'} 与默认分支 {config.default_branch} 不一致。")
        if _git(config.repo_path, "status", "--porcelain"):
            raise ValueError("仓库 checkout 不干净。")
        return RepositorySnapshot(
            repo_key=config.repo_key,
            repo_full_name=config.repo_full_name,
            repo_path=str(config.repo_path),
            default_branch=config.default_branch,
            branch=branch,
            head_sha=_git(config.repo_path, "rev-parse", "HEAD"),
            role=config.role,
            scopes=config.scopes,
            priority=config.priority,
        )

    def _business_doc_paths(self) -> tuple[str, ...]:
        if not self.business_docs_root.is_dir():
            return ()
        return tuple(
            self._display_path(path)
            for path in sorted(self.business_docs_root.rglob("*.md"))
            if not self._is_hidden_business_doc_path(path)
        )

    def _business_docs_preflight_error(self, doc_paths: tuple[str, ...]) -> str | None:
        mount_path = self.knowledge_mount_root / "business-docs"
        if not self.business_docs_root.is_dir():
            return f"业务文档根目录不存在：{mount_path}"
        if not doc_paths:
            return f"业务文档根目录没有可分析的 Markdown：{mount_path}"
        return None

    def _scoped_document_paths(self, config: BusinessDocRepositoryConfig) -> tuple[str, ...]:
        paths: list[str] = []
        for display_path in self._business_doc_paths():
            relative = self._resolve_business_doc_path(display_path).relative_to(self.business_docs_root).as_posix()
            if not config.scopes or any(_scope_allows(scope, relative) for scope in config.scopes):
                paths.append(display_path)
        return tuple(paths)

    def _run_diff(self, run: BusinessDocUpdateRun, snapshots: tuple[RepositorySnapshot, ...]) -> str:
        if run.mode == "full":
            return "全量校准：请通过配置的仓库路径和固定 HEAD 使用 git show/grep 读取当前代码，不切换分支。"
        if not snapshots or not run.repo_key or not run.base_sha or not run.head_sha:
            raise ValueError("增量运行缺少代码版本。")
        trigger_snapshot = next((snapshot for snapshot in snapshots if snapshot.repo_key == run.repo_key), None)
        if trigger_snapshot is None:
            raise ValueError("增量运行缺少触发仓库快照。")
        repo_path = Path(trigger_snapshot.repo_path)
        return _git(repo_path, "diff", "--find-renames", "--find-copies", "--unified=80", f"{run.base_sha}..{run.head_sha}")[:180000]

    def _skill_text(self) -> str:
        skill_path = self.skills_root / SKILL_NAME / "SKILL.md"
        if not skill_path.is_file():
            raise FileNotFoundError(f"缺少 Skill：{skill_path}")
        return skill_path.read_text(encoding="utf-8", errors="replace")

    def _generation_prompt(
        self,
        run: BusinessDocUpdateRun,
        snapshots: tuple[RepositorySnapshot, ...],
        source_path: str,
        source_text: str,
        diff_text: str,
        skill_text: str,
    ) -> str:
        return "\n".join(
            [
                "按 Skill 和以下代码证据判断业务文档是否需要更新。默认不更新，只允许稳定业务语义变化。",
                "当前只负责生成阶段；平台会在你返回后另行启动独立审核。不得启动子 Agent，也不得自行执行审核。",
                "只输出 JSON，不要写文件。result_type 只能是 update_required、no_update_needed、new_doc_candidate。",
                "update_required 必须给出目标文档完整 replacement_markdown；无法给出完整正文时必须改为 no_update_needed 或 new_doc_candidate。不得包含类名、函数名、接口参数、表字段、内部调用链和一般异常分支。",
                "evidence 每项必须含 repo_key、commit、file、symbol；risk_flags 从 rule_deletion、permission、funds、risk_control、low_confidence、cross_repo_conflict、insufficient_evidence 中选择。",
                "输出字段：result_type, stable_business_change, summary, reason, confidence(0..1), evidence, risk_flags, cross_repo_conflict, replacement_markdown。",
                f"运行：{run.mode} {run.base_sha or '-'}..{run.head_sha or '-'}",
                f"目标：{source_path or '无现有文档，仅判断是否存在新文档候选'}",
                "仓库快照（领域服务证据优先于网关，网关优先于前端）：",
                json.dumps([snapshot.__dict__ for snapshot in snapshots], ensure_ascii=False, indent=2),
                "代码差异/读取说明：",
                diff_text,
                "当前业务文档：",
                source_text,
                "Skill：",
                skill_text,
            ]
        )

    def _review_prompt(
        self,
        run: BusinessDocUpdateRun,
        snapshots: tuple[RepositorySnapshot, ...],
        source_path: str,
        source_text: str,
        diff_text: str,
        proposal: dict[str, Any],
    ) -> str:
        return "\n".join(
            [
                "独立、从反方审核以下业务文档提案。检查代码证据、稳定业务语义、遗漏、跨仓库冲突和业务文档边界。",
                "当前只负责这一轮独立审核；不得启动子 Agent，也不得代替生成 Agent 修改提案。",
                "只输出 JSON：decision(approve/reject/manual), feedback, confidence(0..1), missing_evidence(list), boundary_violations(list), risk_flags(list)。",
                f"risk_flags 必须是数组且只能从 {', '.join(sorted(_RISK_FLAGS))} 中选择；无风险时输出空数组 []。",
                f"运行：{run.mode}；目标：{source_path or '新文档候选'}",
                "仓库快照：",
                json.dumps([snapshot.__dict__ for snapshot in snapshots], ensure_ascii=False, indent=2),
                "代码差异/读取说明：",
                diff_text,
                "当前文档：",
                source_text,
                "生成提案：",
                json.dumps(proposal, ensure_ascii=False, indent=2),
            ]
        )

    @staticmethod
    def _revision_prompt(proposal: dict[str, Any], review: dict[str, Any]) -> str:
        return "\n".join(
            [
                "这是唯一一次复核。根据独立审核修正或撤销原提案，只输出完整 JSON。",
                "当前只负责生成 Agent 复核；不得启动子 Agent，也不得再次发起审核。",
                "保留生成字段，并新增 review_resolution，值为 agree 或 disagree；不得开启下一轮审核。",
                "撤销提案时 result_type 必须是 no_update_needed，不能写 no_update；保留 update_required 时必须输出完整 replacement_markdown。",
                "原提案：",
                json.dumps(proposal, ensure_ascii=False, indent=2),
                "独立审核：",
                json.dumps(review, ensure_ascii=False, indent=2),
            ]
        )

    def _can_auto_apply(
        self,
        run: BusinessDocUpdateRun,
        proposal: dict[str, Any],
        review: dict[str, Any],
        item: BusinessDocUpdateItem,
    ) -> bool:
        return bool(
            item.source_path
            and not run.error_message
            and proposal["result_type"] == "update_required"
            and proposal["stable_business_change"]
            and proposal["review_resolution"] == "agree"
            and review["decision"] == "approve"
            and float(proposal["confidence"]) >= self.auto_apply_confidence
            and float(review["confidence"]) >= self.auto_apply_confidence
            and not proposal["risk_flags"]
            and not review["risk_flags"]
            and not proposal["cross_repo_conflict"]
            and self.compute_source_hash(item.source_path) == item.source_hash
        )

    def _write_proposal(
        self,
        run: BusinessDocUpdateRun,
        item: BusinessDocUpdateItem,
        proposal: dict[str, Any],
        review: dict[str, Any],
    ) -> tuple[str, str]:
        if item.source_path:
            self._supersede_pending(item.source_path)
        now = datetime.now(timezone.utc)
        update_id = f"doc_update_{now.strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}"
        update_dir = self.reviews_root / now.strftime("%Y") / now.strftime("%m")
        update_dir.mkdir(parents=True, exist_ok=True)
        update_file = update_dir / f"{update_id}.md"
        status = "pending"
        frontmatter = {
            "update_id": update_id,
            "artifact_type": ARTIFACT_TYPE,
            "source_type": "code_snapshot",
            "run_id": run.run_id,
            "mode": run.mode,
            "repo_key": run.repo_key or "multiple",
            "base_sha": run.base_sha or "",
            "head_sha": run.head_sha or "",
            "status": status,
            "affected_docs": item.source_path,
            "affected_doc_hash": item.source_hash,
            "skill": SKILL_NAME,
            "created_at": now.isoformat(),
        }
        replacement = str(proposal.get("replacement_markdown") or "").rstrip()
        original = self._resolve_business_doc_path(item.source_path).read_text(encoding="utf-8") if item.source_path else ""
        markdown_diff = "\n".join(
            difflib.unified_diff(
                original.splitlines(),
                replacement.splitlines(),
                fromfile=item.source_path or "/dev/null",
                tofile=item.source_path or "new-business-document.md",
                lineterm="",
            )
        )
        body = "\n".join(
            [
                "# 业务文档更新提案",
                "",
                "## 稳定业务语义变化",
                str(proposal["summary"]),
                "",
                "## 生成 Agent 结论",
                str(proposal["reason"]),
                "",
                "## 独立审核 Agent",
                str(review["feedback"]),
                "",
                "## 生成 Agent 复核",
                str(proposal["review_resolution"]),
                "",
                "## 代码证据",
                "```json",
                json.dumps(proposal["evidence"], ensure_ascii=False, indent=2),
                "```",
                "",
                "## Markdown diff",
                "```diff",
                markdown_diff or "（正文无变化）",
                "```",
                "",
                "## 最终文档正文",
                f"```business-doc-update path={item.source_path} mode=replace" if item.source_path else "```text",
                replacement or "需要新建业务文档，未自动创建。",
                "```",
            ]
        )
        update_file.write_text(f"{_format_frontmatter(frontmatter)}\n\n{body}\n", encoding="utf-8")
        update_path = self._display_path(update_file)
        if item.source_path:
            self._append_meta(
                source_path=item.source_path,
                update_id=update_id,
                update_path=update_path,
                run=run,
                source_hash=item.source_hash,
                evidence=proposal["evidence"],
                created_at=now.isoformat(),
            )
        return update_id, update_path

    def _apply_detail(self, detail: BusinessDocUpdateDetail) -> None:
        if not detail.source_path or detail.frontmatter is None:
            raise ValueError("新文档候选不能自动应用。")
        replacements = _extract_replacement_blocks(detail.content)
        replacement = replacements.get(detail.source_path)
        if not replacement:
            raise ValueError("提案没有声明目标文档的完整替换正文。")
        source_file = self._resolve_business_doc_path(detail.source_path)
        replacement_text = replacement.rstrip() + "\n"
        with self._business_doc_git_lock:
            current_text = source_file.read_text(encoding="utf-8")
            current_hash = self.compute_source_hash(detail.source_path)
            if current_hash != detail.frontmatter.get("affected_doc_hash") and current_text != replacement_text:
                raise ValueError(f"业务文档已被修改，请人工确认：{detail.source_path}")
            repo_root, repo_relative_path = self._prepare_business_doc_git_publish(
                source_file=source_file,
                update_id=detail.update_id,
            )
            current_text = source_file.read_text(encoding="utf-8")
            current_hash = self.compute_source_hash(detail.source_path)
            if current_hash != detail.frontmatter.get("affected_doc_hash") and current_text != replacement_text:
                raise ValueError(f"业务文档已被修改，请人工确认：{detail.source_path}")
            if current_text != replacement_text:
                source_file.write_text(replacement_text, encoding="utf-8")
            self._commit_and_push_business_doc(
                repo_root=repo_root,
                repo_relative_path=repo_relative_path,
                update_id=detail.update_id,
            )

    def _prepare_business_doc_git_publish(self, *, source_file: Path, update_id: str) -> tuple[Path, str]:
        try:
            repo_root = Path(_git(source_file.parent, "rev-parse", "--show-toplevel")).resolve()
            repo_relative_path = source_file.relative_to(repo_root).as_posix()
            _git(repo_root, "ls-files", "--error-unmatch", "--", repo_relative_path)
            branch = _git(repo_root, "branch", "--show-current")
            if not branch:
                raise RuntimeError("业务文档仓库处于 detached HEAD。")
            upstream_remote, _ = _git_upstream(repo_root, branch)
            if _git_has_changes(repo_root, "--cached", "--", repo_relative_path):
                raise RuntimeError(f"目标文档存在已暂存的人工修改：{repo_relative_path}")
            _git(repo_root, "fetch", "--quiet", upstream_remote)
            self._push_recoverable_business_doc_commits(repo_root=repo_root, update_id=update_id)
            behind, ahead = _git_divergence(repo_root)
            if behind or ahead:
                raise RuntimeError(f"业务文档仓库未与上游同步：behind={behind}, ahead={ahead}。")
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"业务文档 Git 发布预检失败：{exc}") from exc
        return repo_root, repo_relative_path

    def _push_recoverable_business_doc_commits(self, *, repo_root: Path, update_id: str) -> None:
        behind, ahead = _git_divergence(repo_root)
        if not ahead:
            return
        subjects = _git(repo_root, "log", "--format=%s", "@{upstream}..HEAD").splitlines()
        marker = "[business-doc-update:"
        if behind or not subjects or any(marker not in subject for subject in subjects):
            raise RuntimeError(f"业务文档仓库存在非自动更新的未推送提交：behind={behind}, ahead={ahead}。")
        _git_push_upstream(repo_root)
        if _git(repo_root, "rev-parse", "HEAD") != _git(repo_root, "rev-parse", "@{upstream}"):
            raise RuntimeError(f"业务文档提交推送后未到达上游：{update_id}。")

    def _commit_and_push_business_doc(self, *, repo_root: Path, repo_relative_path: str, update_id: str) -> None:
        try:
            if _git_has_changes(repo_root, "--", repo_relative_path):
                commit_message = f"add: 更新业务文档：{repo_relative_path} [business-doc-update:{update_id}]"
                _git(repo_root, "commit", "--only", "-m", commit_message, "--", repo_relative_path)
            _git_push_upstream(repo_root)
            if _git(repo_root, "rev-parse", "HEAD") != _git(repo_root, "rev-parse", "@{upstream}"):
                raise RuntimeError("业务文档提交推送后未到达上游。")
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            raise ValueError(f"业务文档 Git 提交或推送失败：{exc}") from exc

    def _set_update_status(self, detail: BusinessDocUpdateDetail, status: str) -> None:
        update_file = self._resolve_update_path(detail.path)
        frontmatter, body = _split_frontmatter(update_file.read_text(encoding="utf-8"))
        frontmatter["status"] = status
        frontmatter["completed_at"] = _utc_now()
        update_file.write_text(f"{_format_frontmatter(frontmatter)}\n\n{body.strip()}\n", encoding="utf-8")
        if detail.source_path:
            self._update_meta_status(detail.source_path, detail.update_id, status)

    def _supersede_pending(self, source_path: str) -> None:
        for update in self._updates_from_meta(self._read_meta(source_path)):
            if update.status != "pending":
                continue
            detail = self.get_update_detail(update_id=update.update_id)
            self.store.record_action(
                update_id=update.update_id,
                action="superseded",
                user_id=SYSTEM_ACTOR,
                actor_role="system",
                reason="同一文档出现覆盖更新的新代码区间。",
            )
            self._set_update_status(detail, "superseded")

    def _append_meta(
        self,
        *,
        source_path: str,
        update_id: str,
        update_path: str,
        run: BusinessDocUpdateRun,
        source_hash: str,
        evidence: list[dict[str, object]],
        created_at: str,
    ) -> None:
        meta = self._read_meta(source_path)
        updates = [value for value in meta.get("doc_updates", []) if isinstance(value, dict)]
        updates.append(
            {
                "update_id": update_id,
                "artifact_type": ARTIFACT_TYPE,
                "path": update_path,
                "run_id": run.run_id,
                "mode": run.mode,
                "repo_key": run.repo_key,
                "base_sha": run.base_sha,
                "head_sha": run.head_sha,
                "source_hash": source_hash,
                "status": "pending",
                "evidence": evidence,
                "created_at": created_at,
            }
        )
        meta.update(
            {
                "source_path": source_path,
                "source_hash": source_hash,
                "source_type": SOURCE_TYPE,
                "doc_updates": updates,
                "latest_doc_updates": {ARTIFACT_TYPE: update_id},
                "code_evidence": evidence,
                "updated_at": created_at,
            }
        )
        meta_path = self._source_meta_path(source_path)
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _update_meta_status(self, source_path: str, update_id: str, status: str) -> None:
        meta = self._read_meta(source_path)
        updates: list[dict[str, object]] = []
        for raw in meta.get("doc_updates", []):
            if not isinstance(raw, dict):
                continue
            updates.append({**raw, "status": status, "completed_at": _utc_now()} if raw.get("update_id") == update_id else raw)
        meta["doc_updates"] = updates
        meta["source_hash"] = self.compute_source_hash(source_path)
        meta["updated_at"] = _utc_now()
        meta_path = self._source_meta_path(source_path)
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _updates_from_meta(self, meta: dict[str, object]) -> list[BusinessDocUpdateRecord]:
        source_path = str(meta.get("source_path") or "")
        records: list[BusinessDocUpdateRecord] = []
        for raw in meta.get("doc_updates", []):
            if not isinstance(raw, dict) or not raw.get("update_id") or not raw.get("path"):
                continue
            stored_item = self.store.get_item_by_update_id(str(raw["update_id"]))
            records.append(
                BusinessDocUpdateRecord(
                    update_id=str(raw["update_id"]),
                    artifact_type=ARTIFACT_TYPE,
                    path=str(raw["path"]),
                    source_path=source_path or None,
                    run_id=str(raw.get("run_id") or ""),
                    mode=str(raw.get("mode") or ""),
                    repo_key=str(raw.get("repo_key") or "") or None,
                    base_sha=str(raw.get("base_sha") or "") or None,
                    head_sha=str(raw.get("head_sha") or "") or None,
                    status=str(raw.get("status") or "pending"),
                    review_status=stored_item.review_status if stored_item else None,
                    confidence=stored_item.confidence if stored_item else None,
                    apply_status=stored_item.apply_status if stored_item else None,
                    created_at=str(raw.get("created_at") or ""),
                    completed_at=str(raw.get("completed_at") or "") or None,
                )
            )
        return sorted(records, key=lambda value: value.created_at, reverse=True)

    def _stored_hash(self, source_path: str, update_id: str) -> str:
        for raw in self._read_meta(source_path).get("doc_updates", []):
            if isinstance(raw, dict) and raw.get("update_id") == update_id:
                return str(raw.get("source_hash") or "")
        return ""

    def _read_meta(self, source_path: str) -> dict[str, object]:
        meta_path = self._source_meta_path(source_path)
        if not meta_path.is_file():
            return {}
        try:
            value = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def _advance_run_cursors(self, run: BusinessDocUpdateRun) -> None:
        if run.failed_items:
            return
        for snapshot in self._cursor_snapshots(run):
            self.store.set_cursor(repo_key=snapshot.repo_key, analyzed_head=snapshot.head_sha, last_run_id=run.run_id)

    @staticmethod
    def _cursor_snapshots(run: BusinessDocUpdateRun) -> tuple[RepositorySnapshot, ...]:
        snapshots = tuple(RepositorySnapshot(**value) for value in json.loads(run.snapshot_json))
        if run.mode == "full":
            return snapshots
        return tuple(snapshot for snapshot in snapshots if snapshot.repo_key == run.repo_key)

    def _resolve_business_doc_target(self, source_path: str) -> Path:
        raw_path = source_path.strip()
        if raw_path.startswith("/"):
            try:
                mounted_relative = self.business_docs_mount_root.relative_to(self.base_dir)
            except ValueError as exc:
                raise FileNotFoundError("业务文档不存在。") from exc
            if not mounted_relative.parts or mounted_relative.parts[0] != "workspace":
                raise FileNotFoundError("业务文档不存在。")
            public_root = "/" + Path(*mounted_relative.parts[1:]).as_posix()
            if raw_path == public_root:
                target = self.business_docs_mount_root
            elif raw_path.startswith(f"{public_root}/"):
                target = self.business_docs_mount_root / raw_path.removeprefix(f"{public_root}/")
            else:
                raise FileNotFoundError("业务文档不存在。")
        else:
            target = self.base_dir / raw_path
        target = target.resolve()
        try:
            target.relative_to(self.business_docs_root)
        except ValueError as exc:
            raise FileNotFoundError("业务文档不存在。") from exc
        return target

    def _resolve_business_doc_path(self, source_path: str) -> Path:
        target = self._resolve_business_doc_target(source_path)
        if not target.is_file() or target.suffix.lower() != ".md" or self._is_hidden_business_doc_path(target):
            raise FileNotFoundError("业务文档不存在。")
        return target

    def _resolve_update_path(self, update_path: str) -> Path:
        target = (self.base_dir / update_path.strip("/")).resolve()
        try:
            target.relative_to(self.reviews_root)
        except ValueError as exc:
            raise FileNotFoundError("业务文档更新提案不存在。") from exc
        if not target.is_file() or target.suffix.lower() != ".md":
            raise FileNotFoundError("业务文档更新提案不存在。")
        return target

    def _source_meta_path(self, source_path: str) -> Path:
        source_file = self._resolve_business_doc_path(source_path)
        return self.business_docs_root / "__meta__" / f"{source_file.relative_to(self.business_docs_root).as_posix()}.json"

    def _display_path(self, path: Path) -> str:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(self.knowledge_root)
        except ValueError:
            return resolved.relative_to(self.base_dir).as_posix()
        mounted = self.knowledge_mount_root / relative
        return mounted.relative_to(self.base_dir).as_posix()

    def _is_hidden_business_doc_path(self, path: Path) -> bool:
        try:
            parts = path.resolve().relative_to(self.business_docs_root).parts
        except ValueError:
            return True
        return any(part.startswith(".") or part.startswith("_") or part == "html" for part in parts[:-1])


_GENERATOR_SYSTEM_PROMPT = "你是业务文档生成 Agent。只依据指定 commit 的代码证据维护稳定业务骨架，默认不更新，只输出严格 JSON。"
_REVIEWER_SYSTEM_PROMPT = "你是独立业务文档审核 Agent。不得继承生成 Agent 的结论，只依据代码证据反向核验，只输出严格 JSON。"
_SENSITIVE_AGENT_PAYLOAD_KEYS = ("api_key", "authorization", "cookie", "password", "secret", "token")


def _safe_agent_payload(payload: dict[str, Any]) -> str:
    def sanitize(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                str(key): "[REDACTED]" if any(part in str(key).lower() for part in _SENSITIVE_AGENT_PAYLOAD_KEYS) else sanitize(item)
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    rendered = json.dumps(sanitize(payload), ensure_ascii=False, separators=(",", ":"), default=str)
    return rendered if len(rendered) <= 1200 else rendered[:1199] + "…"


def _validate_proposal(payload: dict[str, Any], *, source_path: str, require_resolution: bool = False) -> dict[str, Any]:
    result_type = str(payload.get("result_type") or "")
    if result_type not in {"update_required", "no_update_needed", "new_doc_candidate"}:
        raise ValueError("生成 Agent result_type 无效。")
    confidence = float(payload.get("confidence", -1))
    if not 0 <= confidence <= 1:
        raise ValueError("生成 Agent confidence 无效。")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError("生成 Agent evidence 无效。")
    for item in evidence:
        if not isinstance(item, dict) or not all(
            str(item.get(key) or "").strip() for key in ("repo_key", "commit", "file", "symbol")
        ):
            raise ValueError("生成 Agent 代码证据不完整。")
    flags = payload.get("risk_flags")
    if not isinstance(flags, list) or any(str(flag) not in _RISK_FLAGS for flag in flags):
        raise ValueError("生成 Agent risk_flags 无效。")
    if result_type == "update_required" and (not source_path or not str(payload.get("replacement_markdown") or "").strip()):
        raise ValueError("更新提案缺少目标文档完整正文。")
    if require_resolution and str(payload.get("review_resolution") or "") not in {"agree", "disagree"}:
        raise ValueError("生成 Agent 复核结论无效。")
    return {
        **payload,
        "result_type": result_type,
        "confidence": confidence,
        "evidence": evidence,
        "risk_flags": [str(flag) for flag in flags],
        "stable_business_change": bool(payload.get("stable_business_change")),
        "cross_repo_conflict": bool(payload.get("cross_repo_conflict")),
        "summary": str(payload.get("summary") or ""),
        "reason": str(payload.get("reason") or ""),
        "replacement_markdown": str(payload.get("replacement_markdown") or ""),
        "review_resolution": str(payload.get("review_resolution") or "") if require_resolution else "",
    }


def _validate_review(payload: dict[str, Any]) -> dict[str, Any]:
    decision = str(payload.get("decision") or "")
    if decision not in {"approve", "reject", "manual"}:
        raise ValueError("审核 Agent decision 无效。")
    confidence = float(payload.get("confidence", -1))
    if not 0 <= confidence <= 1:
        raise ValueError("审核 Agent confidence 无效。")
    flags = payload.get("risk_flags")
    if not isinstance(flags, list) or any(str(flag) not in _RISK_FLAGS for flag in flags):
        raise ValueError("审核 Agent risk_flags 无效。")
    return {**payload, "decision": decision, "confidence": confidence, "risk_flags": [str(flag) for flag in flags], "feedback": str(payload.get("feedback") or "")}


def _normalize_scope(value: object) -> str:
    raw = str(value).strip().replace("\\", "/").strip("/")
    path = PurePosixPath(raw)
    if not raw or path.is_absolute() or ".." in path.parts or any(part.startswith(".") for part in path.parts):
        raise ValueError(f"business_doc_scopes 路径无效：{value}")
    return path.as_posix()


def _scope_allows(scope: str, relative_path: str) -> bool:
    return relative_path == scope or relative_path.startswith(scope.rstrip("/") + "/")


def _git(repo_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo_path, capture_output=True, text=True, timeout=120, check=False
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip()[:2000])
    return result.stdout.strip()


def _git_has_changes(repo_path: Path, *args: str) -> bool:
    result = subprocess.run(
        ["git", "diff", "--quiet", *args], cwd=repo_path, capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError((result.stderr or result.stdout).strip()[:2000])
    return result.returncode == 1


def _git_divergence(repo_path: Path) -> tuple[int, int]:
    raw = _git(repo_path, "rev-list", "--left-right", "--count", "@{upstream}...HEAD")
    values = raw.split()
    if len(values) != 2:
        raise RuntimeError(f"无法读取业务文档仓库与上游的差异：{raw}")
    return int(values[0]), int(values[1])


def _git_upstream(repo_path: Path, branch: str) -> tuple[str, str]:
    remote = _git(repo_path, "config", "--get", f"branch.{branch}.remote")
    merge_ref = _git(repo_path, "config", "--get", f"branch.{branch}.merge")
    if not remote or remote == "." or not merge_ref.startswith("refs/heads/"):
        raise RuntimeError(f"业务文档分支 {branch} 没有可推送的远程上游。")
    return remote, merge_ref


def _git_push_upstream(repo_path: Path) -> None:
    branch = _git(repo_path, "branch", "--show-current")
    if not branch:
        raise RuntimeError("业务文档仓库处于 detached HEAD。")
    remote, merge_ref = _git_upstream(repo_path, branch)
    _git(repo_path, "push", remote, f"HEAD:{merge_ref}")


def _git_is_ancestor(repo_path: Path, base_sha: str, head_sha: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", base_sha, head_sha],
        cwd=repo_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError((result.stderr or result.stdout).strip()[:2000])
    return result.returncode == 0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_json(value: object) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(raw.encode('utf-8')).hexdigest()}"


def _json_list(value: str) -> list[object]:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    return parsed if isinstance(parsed, list) else []


def _format_frontmatter(metadata: dict[str, str]) -> str:
    return "\n".join(["---", *(f"{key}: {value}" for key, value in metadata.items()), "---"])


def _split_frontmatter(markdown: str) -> tuple[dict[str, str], str]:
    match = re.match(r"^---\n([\s\S]*?)\n---(?:\n+|$)", markdown.replace("\r\n", "\n"))
    if not match:
        return {}, markdown
    metadata: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            metadata[key.strip()] = value.strip()
    return metadata, markdown[match.end():]


def _extract_replacement_blocks(markdown: str) -> dict[str, str]:
    pattern = re.compile(
        r"```business-doc-update\s+path=([^\s`]+)\s+mode=replace\s*\n([\s\S]*?)\n```",
        flags=re.IGNORECASE,
    )
    return {match.group(1).strip(): match.group(2).strip() for match in pattern.finditer(markdown)}
