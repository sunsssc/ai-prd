from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from uuid import uuid4


@dataclass(slots=True)
class PermissionPolicyDepartmentRecord:
    department_key: str
    department_path_snapshot: str
    include_children: bool


@dataclass(slots=True)
class PermissionPolicyRecord:
    policy_id: str
    policy_name: str
    organization_key: str
    capability_key: str
    task_scope_type: str
    status: str
    starts_at: datetime | None
    expires_at: datetime | None
    description: str | None
    created_by: str
    updated_by: str
    created_at: datetime
    updated_at: datetime
    departments: list[PermissionPolicyDepartmentRecord] = field(default_factory=list)
    tasks: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PermissionPolicyAuditRecord:
    audit_id: str
    policy_id: str
    action: str
    actor_user_id: str
    changes: dict[str, object]
    created_at: datetime


class SQLitePermissionStore:
    """权限策略和授权决策的持久化存储。与认证库共用 SQLite 文件但独立建表。"""

    connection_timeout_seconds = 0.2

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self._lock = Lock()
        self._initialize()

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS permission_policies (
                    policy_id TEXT PRIMARY KEY,
                    policy_name TEXT NOT NULL,
                    organization_key TEXT NOT NULL,
                    capability_key TEXT NOT NULL,
                    task_scope_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    starts_at TEXT,
                    expires_at TEXT,
                    description TEXT,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_permission_policies_lookup
                    ON permission_policies(organization_key, capability_key, status);

                CREATE TABLE IF NOT EXISTS permission_policy_departments (
                    policy_id TEXT NOT NULL,
                    department_key TEXT NOT NULL,
                    department_path_snapshot TEXT NOT NULL,
                    include_children INTEGER NOT NULL,
                    PRIMARY KEY(policy_id, department_key),
                    FOREIGN KEY(policy_id) REFERENCES permission_policies(policy_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS permission_policy_tasks (
                    policy_id TEXT NOT NULL,
                    task_name TEXT NOT NULL,
                    PRIMARY KEY(policy_id, task_name),
                    FOREIGN KEY(policy_id) REFERENCES permission_policies(policy_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS permission_policy_audit_logs (
                    audit_id TEXT PRIMARY KEY,
                    policy_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    actor_user_id TEXT NOT NULL,
                    changes_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(policy_id) REFERENCES permission_policies(policy_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS permission_decision_logs (
                    decision_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    email TEXT NOT NULL,
                    organization_key TEXT NOT NULL,
                    capability_key TEXT NOT NULL,
                    task_name TEXT,
                    allowed INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    matched_policy_id TEXT,
                    matched_policy_name TEXT,
                    department_snapshot_json TEXT NOT NULL,
                    session_id TEXT,
                    turn_id TEXT,
                    request_id TEXT,
                    decided_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_permission_decision_logs_time
                    ON permission_decision_logs(decided_at DESC);
                """
            )

    def list_policies(
        self,
        *,
        organization_key: str | None = None,
        capability_key: str | None = None,
        status: str | None = None,
    ) -> list[PermissionPolicyRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if organization_key:
            clauses.append("organization_key = ?")
            params.append(organization_key)
        if capability_key:
            clauses.append("capability_key = ?")
            params.append(capability_key)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM permission_policies {where} ORDER BY updated_at DESC, policy_id",
                params,
            ).fetchall()
            return [self._load_policy(connection, row) for row in rows]

    def get_policy(self, policy_id: str) -> PermissionPolicyRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM permission_policies WHERE policy_id = ?",
                (policy_id,),
            ).fetchone()
            return self._load_policy(connection, row) if row is not None else None

    def update_policy_status(
        self,
        *,
        policy_id: str,
        status: str,
        actor_user_id: str,
    ) -> PermissionPolicyRecord | None:
        if status not in {"enabled", "disabled"}:
            raise ValueError("策略状态只能是 enabled 或 disabled。")
        existing = self.get_policy(policy_id)
        if existing is None or existing.status == "deleted":
            return None
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE permission_policies SET status = ?, updated_by = ?, updated_at = ? WHERE policy_id = ?",
                (status, actor_user_id, now.isoformat(), policy_id),
            )
            connection.execute(
                """
                INSERT INTO permission_policy_audit_logs (
                    audit_id, policy_id, action, actor_user_id, changes_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    policy_id,
                    "status_changed",
                    actor_user_id,
                    json.dumps({"from": existing.status, "to": status}, ensure_ascii=False, sort_keys=True),
                    now.isoformat(),
                ),
            )
        return self.get_policy(policy_id)

    def delete_policy(self, *, policy_id: str, actor_user_id: str) -> PermissionPolicyRecord | None:
        existing = self.get_policy(policy_id)
        if existing is None:
            return None
        if existing.status == "deleted":
            return existing
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE permission_policies SET status = 'deleted', updated_by = ?, updated_at = ? WHERE policy_id = ?",
                (actor_user_id, now.isoformat(), policy_id),
            )
            connection.execute(
                """
                INSERT INTO permission_policy_audit_logs (
                    audit_id, policy_id, action, actor_user_id, changes_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    policy_id,
                    "deleted",
                    actor_user_id,
                    json.dumps({"from": existing.status, "to": "deleted"}, ensure_ascii=False, sort_keys=True),
                    now.isoformat(),
                ),
            )
        return self.get_policy(policy_id)

    def save_policy(
        self,
        policy: PermissionPolicyRecord,
        *,
        actor_user_id: str,
        action: str,
        changes: dict[str, object],
    ) -> PermissionPolicyRecord:
        with self._lock, self._connect() as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute(
                """
                INSERT INTO permission_policies (
                    policy_id, policy_name, organization_key, capability_key,
                    task_scope_type, status, starts_at, expires_at, description,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(policy_id) DO UPDATE SET
                    policy_name = excluded.policy_name,
                    organization_key = excluded.organization_key,
                    capability_key = excluded.capability_key,
                    task_scope_type = excluded.task_scope_type,
                    status = excluded.status,
                    starts_at = excluded.starts_at,
                    expires_at = excluded.expires_at,
                    description = excluded.description,
                    updated_by = excluded.updated_by,
                    updated_at = excluded.updated_at
                """,
                (
                    policy.policy_id,
                    policy.policy_name,
                    policy.organization_key,
                    policy.capability_key,
                    policy.task_scope_type,
                    policy.status,
                    policy.starts_at.isoformat() if policy.starts_at else None,
                    policy.expires_at.isoformat() if policy.expires_at else None,
                    policy.description,
                    policy.created_by,
                    policy.updated_by,
                    policy.created_at.isoformat(),
                    policy.updated_at.isoformat(),
                ),
            )
            connection.execute(
                "DELETE FROM permission_policy_departments WHERE policy_id = ?",
                (policy.policy_id,),
            )
            connection.executemany(
                """
                INSERT INTO permission_policy_departments (
                    policy_id, department_key, department_path_snapshot, include_children
                ) VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        policy.policy_id,
                        item.department_key,
                        item.department_path_snapshot,
                        int(item.include_children),
                    )
                    for item in policy.departments
                ],
            )
            connection.execute(
                "DELETE FROM permission_policy_tasks WHERE policy_id = ?",
                (policy.policy_id,),
            )
            connection.executemany(
                "INSERT INTO permission_policy_tasks (policy_id, task_name) VALUES (?, ?)",
                [(policy.policy_id, task_name) for task_name in policy.tasks],
            )
            connection.execute(
                """
                INSERT INTO permission_policy_audit_logs (
                    audit_id, policy_id, action, actor_user_id, changes_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    policy.policy_id,
                    action,
                    actor_user_id,
                    json.dumps(changes, ensure_ascii=False, sort_keys=True),
                    policy.updated_at.isoformat(),
                ),
            )
        return policy

    def list_policy_audits(self, policy_id: str) -> list[PermissionPolicyAuditRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM permission_policy_audit_logs
                WHERE policy_id = ? ORDER BY created_at DESC
                """,
                (policy_id,),
            ).fetchall()
        return [
            PermissionPolicyAuditRecord(
                audit_id=row["audit_id"],
                policy_id=row["policy_id"],
                action=row["action"],
                actor_user_id=row["actor_user_id"],
                changes=json.loads(row["changes_json"]),
                created_at=datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def append_decision_log(
        self,
        *,
        user_id: str | None,
        email: str,
        organization_key: str,
        capability_key: str,
        task_name: str | None,
        allowed: bool,
        reason: str,
        matched_policy_id: str | None,
        matched_policy_name: str | None,
        department_snapshot: dict[str, object],
        session_id: str | None = None,
        turn_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO permission_decision_logs (
                    decision_id, user_id, email, organization_key, capability_key,
                    task_name, allowed, reason, matched_policy_id, matched_policy_name,
                    department_snapshot_json, session_id, turn_id, request_id, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    user_id,
                    email,
                    organization_key,
                    capability_key,
                    task_name,
                    int(allowed),
                    reason,
                    matched_policy_id,
                    matched_policy_name,
                    json.dumps(department_snapshot, ensure_ascii=False, sort_keys=True),
                    session_id,
                    turn_id,
                    request_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def list_decision_logs(self, *, limit: int = 100) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM permission_decision_logs ORDER BY decided_at DESC LIMIT ?",
                (max(1, min(limit, 500)),),
            ).fetchall()
        return [
            {
                "decision_id": row["decision_id"],
                "user_id": row["user_id"],
                "email": row["email"],
                "organization_key": row["organization_key"],
                "capability_key": row["capability_key"],
                "task_name": row["task_name"],
                "allowed": bool(row["allowed"]),
                "reason": row["reason"],
                "matched_policy_id": row["matched_policy_id"],
                "matched_policy_name": row["matched_policy_name"],
                "department_snapshot": json.loads(row["department_snapshot_json"]),
                "session_id": row["session_id"],
                "turn_id": row["turn_id"],
                "request_id": row["request_id"],
                "decided_at": datetime.fromisoformat(row["decided_at"]),
            }
            for row in rows
        ]

    def _load_policy(self, connection: sqlite3.Connection, row: sqlite3.Row) -> PermissionPolicyRecord:
        departments = connection.execute(
            """
            SELECT department_key, department_path_snapshot, include_children
            FROM permission_policy_departments WHERE policy_id = ? ORDER BY department_key
            """,
            (row["policy_id"],),
        ).fetchall()
        tasks = connection.execute(
            "SELECT task_name FROM permission_policy_tasks WHERE policy_id = ? ORDER BY task_name",
            (row["policy_id"],),
        ).fetchall()
        return PermissionPolicyRecord(
            policy_id=row["policy_id"],
            policy_name=row["policy_name"],
            organization_key=row["organization_key"],
            capability_key=row["capability_key"],
            task_scope_type=row["task_scope_type"],
            status=row["status"],
            starts_at=datetime.fromisoformat(row["starts_at"]) if row["starts_at"] else None,
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            description=row["description"],
            created_by=row["created_by"],
            updated_by=row["updated_by"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            departments=[
                PermissionPolicyDepartmentRecord(
                    department_key=item["department_key"],
                    department_path_snapshot=item["department_path_snapshot"],
                    include_children=bool(item["include_children"]),
                )
                for item in departments
            ],
            tasks=[item["task_name"] for item in tasks],
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.db_path,
            timeout=self.connection_timeout_seconds,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
