from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock, local
from uuid import uuid4

from app.integrations.agent_runtime.models import RuntimeWorkspaceMount, RuntimeWorkspacePlan
from app.services.auth_models import UserRecord


class WorkspaceAccessError(ValueError):
    """Workspace 授权解析失败。"""


@dataclass(slots=True)
class OrganizationRecord:
    organization_id: str
    organization_key: str
    display_name: str
    status: str
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class OrganizationMembershipRecord:
    membership_id: str
    organization_id: str
    organization_key: str
    user_id: str
    organization_role: str
    source: str
    is_default: bool
    status: str
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class OrganizationAccessRequestRecord:
    request_id: str
    requester_user_id: str
    source_organization_id: str | None
    source_organization_key: str | None
    target_organization_id: str
    target_organization_key: str
    reason: str
    requested_scope: str | None
    status: str
    reviewed_by_user_id: str | None
    reviewed_at: datetime | None
    review_comment: str | None
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class AccessibleWorkspaceRecord:
    workspace_id: str
    workspace_key: str
    organization_key: str | None
    workspace_type: str
    display_name: str
    sandbox_path: str
    permission: str


class SQLiteWorkspaceAccessStore:
    connection_timeout_seconds = 0.5

    def __init__(self, db_path: str, *, base_dir: Path, default_organization_key: str = "coinex") -> None:
        self.db_path = Path(db_path)
        self.base_dir = base_dir.resolve()
        self.default_organization_key = default_organization_key
        self._lock = Lock()
        self._thread_local = local()
        self._initialize()

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS orgs (
                    org_id TEXT PRIMARY KEY,
                    org_key TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS user_org_access (
                    membership_id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    org_role TEXT NOT NULL,
                    source TEXT NOT NULL,
                    approved_request_id TEXT,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(org_id, user_id),
                    FOREIGN KEY(org_id) REFERENCES orgs(org_id)
                );

                CREATE TABLE IF NOT EXISTS org_access_requests (
                    request_id TEXT PRIMARY KEY,
                    requester_user_id TEXT NOT NULL,
                    source_org_id TEXT,
                    target_org_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    requested_scope TEXT,
                    status TEXT NOT NULL,
                    reviewed_by_user_id TEXT,
                    reviewed_at TEXT,
                    review_comment TEXT,
                    expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(source_org_id) REFERENCES orgs(org_id),
                    FOREIGN KEY(target_org_id) REFERENCES orgs(org_id)
                );

                CREATE TABLE IF NOT EXISTS dept_memberships (
                    membership_id TEXT PRIMARY KEY,
                    org_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    dept_key TEXT,
                    dept_name TEXT,
                    is_default INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(org_id, user_id, dept_key),
                    FOREIGN KEY(org_id) REFERENCES orgs(org_id)
                );

                CREATE TABLE IF NOT EXISTS workspaces (
                    workspace_id TEXT PRIMARY KEY,
                    org_id TEXT,
                    workspace_key TEXT NOT NULL UNIQUE,
                    workspace_type TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    host_path TEXT NOT NULL,
                    default_sandbox_path TEXT NOT NULL,
                    default_permission TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(org_id) REFERENCES orgs(org_id)
                );

                CREATE TABLE IF NOT EXISTS workspace_grants (
                    grant_id TEXT PRIMARY KEY,
                    org_id TEXT,
                    subject_type TEXT NOT NULL,
                    subject_key TEXT NOT NULL,
                    workspace_id TEXT NOT NULL,
                    permission TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(subject_type, subject_key, workspace_id),
                    FOREIGN KEY(org_id) REFERENCES orgs(org_id),
                    FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id)
                );

                CREATE TABLE IF NOT EXISTS user_workspaces (
                    user_workspace_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    personal_workspace_id TEXT NOT NULL,
                    personal_workspace_status TEXT NOT NULL,
                    storage_quota_bytes INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id),
                    FOREIGN KEY(personal_workspace_id) REFERENCES workspaces(workspace_id)
                );
                """
            )
            self._migrate_workspace_schema(connection)
        self.upsert_organization(
            organization_key=self.default_organization_key,
            display_name="CoinEx" if self.default_organization_key == "coinex" else self.default_organization_key,
        )

    def _migrate_workspace_schema(self, connection: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(workspaces)").fetchall()
        }
        if "default_mount_path" in columns and "default_sandbox_path" not in columns:
            connection.execute("ALTER TABLE workspaces RENAME COLUMN default_mount_path TO default_sandbox_path")

    def upsert_organization(self, *, organization_key: str, display_name: str, status: str = "active") -> OrganizationRecord:
        key = self._normalize_key(organization_key, "组织键不能为空。")
        now = datetime.now(timezone.utc)
        organization_id = f"org_{key}"
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT created_at FROM orgs WHERE org_key = ?",
                (key,),
            ).fetchone()
            created_at = datetime.fromisoformat(existing["created_at"]) if existing else now
            connection.execute(
                """
                INSERT INTO orgs (
                    org_id, org_key, display_name, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(org_key) DO UPDATE SET
                    display_name = excluded.display_name,
                    status = excluded.status,
                    updated_at = excluded.updated_at
                """,
                (organization_id, key, display_name.strip() or key, status, created_at.isoformat(), now.isoformat()),
            )
        organization = self.get_organization_by_key(key)
        assert organization is not None
        self.ensure_default_organization_workspaces(organization)
        return organization

    def list_organizations(self) -> list[OrganizationRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT org_id AS organization_id, org_key AS organization_key,
                       display_name, status, created_at, updated_at
                FROM orgs
                ORDER BY org_key ASC
                """
            ).fetchall()
        return [self._row_to_organization(row) for row in rows]

    def get_organization_by_key(self, organization_key: str) -> OrganizationRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT org_id AS organization_id, org_key AS organization_key,
                       display_name, status, created_at, updated_at
                FROM orgs
                WHERE org_key = ?
                """,
                (organization_key,),
            ).fetchone()
        return self._row_to_organization(row) if row else None

    def get_organization_by_id(self, organization_id: str) -> OrganizationRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT org_id AS organization_id, org_key AS organization_key,
                       display_name, status, created_at, updated_at
                FROM orgs
                WHERE org_id = ?
                """,
                (organization_id,),
            ).fetchone()
        return self._row_to_organization(row) if row else None

    def ensure_default_organization_workspaces(self, organization: OrganizationRecord) -> None:
        workspace_key = f"org:{organization.organization_key}:knowledge"
        self._upsert_workspace(
            organization_id=organization.organization_id,
            workspace_id=self._workspace_id(workspace_key),
            workspace_key=workspace_key,
            workspace_type="organization_knowledge",
            display_name="组织知识库",
            host_path=self._knowledge_root(organization.organization_key),
            default_sandbox_path=f"/{organization.organization_key}/knowledge",
            default_permission="read",
        )
        self._upsert_grant(
            organization_id=organization.organization_id,
            subject_type="org_default",
            subject_key=f"org:{organization.organization_key}",
            workspace_key=workspace_key,
            permission="read",
        )
        self._deactivate_legacy_knowledge_workspaces(organization)

    def _deactivate_legacy_knowledge_workspaces(self, organization: OrganizationRecord) -> None:
        legacy_keys = [
            f"org:{organization.organization_key}:knowledge-{scope}"
            for scope in ("requirements", "business-docs", "code")
        ]
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                UPDATE workspaces
                SET status = 'disabled', updated_at = ?
                WHERE workspace_key = ?
                """,
                [(now, workspace_key) for workspace_key in legacy_keys],
            )

    def list_user_memberships(self, user_id: str) -> list[OrganizationMembershipRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*, o.org_key AS organization_key
                FROM user_org_access AS m
                INNER JOIN orgs AS o ON o.org_id = m.org_id
                WHERE m.user_id = ?
                ORDER BY m.is_default DESC, o.org_key ASC
                """,
                (user_id,),
            ).fetchall()
        return [self._row_to_membership(row) for row in rows]

    def list_active_user_memberships(self, user_id: str) -> list[OrganizationMembershipRecord]:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT m.*, o.org_key AS organization_key
                FROM user_org_access AS m
                INNER JOIN orgs AS o ON o.org_id = m.org_id
                WHERE m.user_id = ?
                  AND m.status = 'active'
                  AND o.status = 'active'
                  AND (m.expires_at IS NULL OR m.expires_at > ?)
                ORDER BY m.is_default DESC, o.org_key ASC
                """,
                (user_id, now),
            ).fetchall()
        return [self._row_to_membership(row) for row in rows]

    def upsert_user_membership(
        self,
        *,
        user_id: str,
        organization_key: str,
        organization_role: str = "member",
        source: str = "admin_assigned",
        is_default: bool = False,
        status: str = "active",
        expires_at: datetime | None = None,
        approved_request_id: str | None = None,
    ) -> OrganizationMembershipRecord:
        if organization_role not in {"member", "admin"}:
            raise WorkspaceAccessError("组织角色只能是 member 或 admin。")
        if status not in {"active", "disabled"}:
            raise WorkspaceAccessError("组织成员状态只能是 active 或 disabled。")
        organization = self.get_organization_by_key(organization_key)
        if organization is None:
            raise WorkspaceAccessError("组织不存在。")

        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT membership_id, created_at
                FROM user_org_access
                WHERE org_id = ? AND user_id = ?
                """,
                (organization.organization_id, user_id),
            ).fetchone()
            membership_id = existing["membership_id"] if existing else str(uuid4())
            created_at = datetime.fromisoformat(existing["created_at"]) if existing else now
            if is_default:
                connection.execute(
                    """
                    UPDATE user_org_access
                    SET is_default = 0, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (now.isoformat(), user_id),
                )
            connection.execute(
                """
                INSERT INTO user_org_access (
                    membership_id, org_id, user_id, org_role, source,
                    approved_request_id, is_default, status, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(org_id, user_id) DO UPDATE SET
                    org_role = excluded.org_role,
                    source = excluded.source,
                    approved_request_id = COALESCE(excluded.approved_request_id, user_org_access.approved_request_id),
                    is_default = excluded.is_default,
                    status = excluded.status,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    membership_id,
                    organization.organization_id,
                    user_id,
                    organization_role,
                    source,
                    approved_request_id,
                    int(is_default),
                    status,
                    expires_at.isoformat() if expires_at else None,
                    created_at.isoformat(),
                    now.isoformat(),
                ),
            )
        membership = self.get_membership(membership_id)
        assert membership is not None
        return membership

    def update_user_membership(
        self,
        *,
        membership_id: str,
        organization_role: str | None = None,
        is_default: bool | None = None,
        status: str | None = None,
        expires_at: datetime | None = None,
    ) -> OrganizationMembershipRecord | None:
        existing = self.get_membership(membership_id)
        if existing is None:
            return None
        next_role = organization_role or existing.organization_role
        next_default = existing.is_default if is_default is None else is_default
        next_status = status or existing.status
        if next_role not in {"member", "admin"}:
            raise WorkspaceAccessError("组织角色只能是 member 或 admin。")
        if next_status not in {"active", "disabled"}:
            raise WorkspaceAccessError("组织成员状态只能是 active 或 disabled。")
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            if next_default:
                connection.execute(
                    """
                    UPDATE user_org_access
                    SET is_default = 0, updated_at = ?
                    WHERE user_id = ?
                    """,
                    (now.isoformat(), existing.user_id),
                )
            connection.execute(
                """
                UPDATE user_org_access
                SET org_role = ?, is_default = ?, status = ?, expires_at = ?, updated_at = ?
                WHERE membership_id = ?
                """,
                (
                    next_role,
                    int(next_default),
                    next_status,
                    expires_at.isoformat() if expires_at else None,
                    now.isoformat(),
                    membership_id,
                ),
            )
        return self.get_membership(membership_id)

    def get_membership(self, membership_id: str) -> OrganizationMembershipRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT m.*, o.org_key AS organization_key
                FROM user_org_access AS m
                INNER JOIN orgs AS o ON o.org_id = m.org_id
                WHERE m.membership_id = ?
                """,
                (membership_id,),
            ).fetchone()
        return self._row_to_membership(row) if row else None

    def create_access_request(
        self,
        *,
        requester_user_id: str,
        target_organization_key: str,
        reason: str,
        requested_scope: str | None,
        source_organization_key: str | None,
        expires_at: datetime | None,
    ) -> OrganizationAccessRequestRecord:
        target = self.get_organization_by_key(target_organization_key)
        if target is None:
            raise WorkspaceAccessError("目标组织不存在。")
        source = self.get_organization_by_key(source_organization_key) if source_organization_key else None
        now = datetime.now(timezone.utc)
        request_id = str(uuid4())
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO org_access_requests (
                    request_id, requester_user_id, source_org_id, target_org_id,
                    reason, requested_scope, status, expires_at, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
                """,
                (
                    request_id,
                    requester_user_id,
                    source.organization_id if source else None,
                    target.organization_id,
                    reason.strip(),
                    requested_scope.strip() if requested_scope else None,
                    expires_at.isoformat() if expires_at else None,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        request = self.get_access_request(request_id)
        assert request is not None
        return request

    def list_access_requests(
        self,
        *,
        requester_user_id: str | None = None,
        status: str | None = None,
    ) -> list[OrganizationAccessRequestRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if requester_user_id:
            clauses.append("r.requester_user_id = ?")
            params.append(requester_user_id)
        if status:
            clauses.append("r.status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    r.*,
                    source.org_key AS source_organization_key,
                    target.org_key AS target_organization_key
                FROM org_access_requests AS r
                LEFT JOIN orgs AS source ON source.org_id = r.source_org_id
                INNER JOIN orgs AS target ON target.org_id = r.target_org_id
                {where}
                ORDER BY r.created_at DESC
                """,
                params,
            ).fetchall()
        return [self._row_to_access_request(row) for row in rows]

    def get_access_request(self, request_id: str) -> OrganizationAccessRequestRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    r.*,
                    source.org_key AS source_organization_key,
                    target.org_key AS target_organization_key
                FROM org_access_requests AS r
                LEFT JOIN orgs AS source ON source.org_id = r.source_org_id
                INNER JOIN orgs AS target ON target.org_id = r.target_org_id
                WHERE r.request_id = ?
                """,
                (request_id,),
            ).fetchone()
        return self._row_to_access_request(row) if row else None

    def review_access_request(
        self,
        *,
        request_id: str,
        reviewer_user_id: str,
        status: str,
        review_comment: str | None,
    ) -> OrganizationAccessRequestRecord:
        if status not in {"approved", "rejected"}:
            raise WorkspaceAccessError("审批状态只能是 approved 或 rejected。")
        request = self.get_access_request(request_id)
        if request is None:
            raise WorkspaceAccessError("申请不存在。")
        if request.status != "pending":
            raise WorkspaceAccessError("该申请已处理。")
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE org_access_requests
                SET status = ?, reviewed_by_user_id = ?, reviewed_at = ?, review_comment = ?, updated_at = ?
                WHERE request_id = ?
                """,
                (
                    status,
                    reviewer_user_id,
                    now.isoformat(),
                    review_comment.strip() if review_comment else None,
                    now.isoformat(),
                    request_id,
                ),
            )
        updated = self.get_access_request(request_id)
        assert updated is not None
        if status == "approved":
            self.upsert_user_membership(
                user_id=request.requester_user_id,
                organization_key=request.target_organization_key,
                source="approved_request",
                is_default=False,
                status="active",
                expires_at=request.expires_at,
                approved_request_id=request.request_id,
            )
        return updated

    def ensure_personal_workspace(self, *, user_id: str, organization: OrganizationRecord) -> None:
        workspace_key = f"user:{user_id}"
        workspace_id = self._workspace_id(workspace_key)
        host_path = self._user_root(user_id)
        self._upsert_workspace(
            organization_id=None,
            workspace_id=workspace_id,
            workspace_key=workspace_key,
            workspace_type="user",
            display_name="个人工作区",
            host_path=host_path,
            default_sandbox_path="/me",
            default_permission="write",
        )
        self._upsert_grant(
            organization_id=None,
            subject_type="user",
            subject_key=f"user:{user_id}",
            workspace_key=workspace_key,
            permission="write",
        )
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO user_workspaces (
                    user_workspace_id, user_id, personal_workspace_id,
                    personal_workspace_status, created_at, updated_at
                ) VALUES (?, ?, ?, 'active', ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    personal_workspace_id = excluded.personal_workspace_id,
                    personal_workspace_status = 'active',
                    updated_at = excluded.updated_at
                """,
                (
                    str(uuid4()),
                    user_id,
                    workspace_id,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        for child in ["files", "artifacts", ".claude/skills"]:
            (host_path / child).mkdir(parents=True, exist_ok=True)

    def list_accessible_workspaces(self, *, user: UserRecord, organization_key: str) -> list[AccessibleWorkspaceRecord]:
        plan = self.resolve_plan_records(user=user, organization_key=organization_key)
        return [
            AccessibleWorkspaceRecord(
                workspace_id=record["workspace_id"],
                workspace_key=record["workspace_key"],
                organization_key=organization_key,
                workspace_type=record["workspace_type"],
                display_name=record["display_name"],
                sandbox_path=record["default_sandbox_path"],
                permission=record["permission"],
            )
            for record in plan
        ]

    def resolve_plan_records(self, *, user: UserRecord, organization_key: str) -> list[sqlite3.Row]:
        organization = self.get_organization_by_key(organization_key)
        if organization is None or organization.status != "active":
            raise WorkspaceAccessError("组织不存在或已停用。")
        self.ensure_default_organization_workspaces(organization)
        self.ensure_personal_workspace(user_id=user.user_id, organization=organization)

        subject_keys = [
            ("org_default", f"org:{organization.organization_key}"),
            ("user", f"user:{user.user_id}"),
        ]

        filters = " OR ".join("(g.subject_type = ? AND g.subject_key = ?)" for _ in subject_keys)
        params: list[object] = []
        for subject_type, subject_key in subject_keys:
            params.extend([subject_type, subject_key])
        params.append(organization.organization_id)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    w.workspace_id,
                    w.workspace_key,
                    w.workspace_type,
                    w.display_name,
                    w.host_path,
                    w.default_sandbox_path,
                    CASE
                        WHEN MAX(CASE g.permission WHEN 'write' THEN 2 ELSE 1 END) = 2 THEN 'write'
                        ELSE 'read'
                    END AS permission
                FROM workspace_grants AS g
                INNER JOIN workspaces AS w ON w.workspace_id = g.workspace_id
                WHERE ({filters})
                  AND g.status = 'active'
                  AND w.status = 'active'
                  AND (w.org_id = ? OR w.org_id IS NULL)
                GROUP BY
                    w.workspace_id, w.workspace_key, w.workspace_type, w.display_name,
                    w.host_path, w.default_sandbox_path
                ORDER BY w.default_sandbox_path ASC
                """,
                params,
            ).fetchall()
        return rows

    def _ensure_department_workspaces(self, user: UserRecord, organization: OrganizationRecord) -> None:
        labels = {
            "product": "产品部门",
            "engineering": "研发部门",
            "admin": "管理员共享区",
        }
        for department_key in self._department_keys_for_user(user):
            workspace_key = f"dept:{organization.organization_key}:{department_key}"
            host_path = self._department_root(organization.organization_key) / department_key
            self._upsert_workspace(
                organization_id=organization.organization_id,
                workspace_id=self._workspace_id(workspace_key),
                workspace_key=workspace_key,
                workspace_type="department",
                display_name=labels.get(department_key, department_key),
                host_path=host_path,
                default_sandbox_path=f"/{organization.organization_key}/departments/{department_key}",
                default_permission="read",
            )
            self._upsert_grant(
                organization_id=organization.organization_id,
                subject_type="department",
                subject_key=f"dept:{organization.organization_key}:{department_key}",
                workspace_key=workspace_key,
                permission="read",
            )

    def _upsert_workspace(
        self,
        *,
        organization_id: str | None,
        workspace_id: str,
        workspace_key: str,
        workspace_type: str,
        display_name: str,
        host_path: Path,
        default_sandbox_path: str,
        default_permission: str,
    ) -> None:
        if default_permission not in {"read", "write"}:
            raise WorkspaceAccessError("Workspace 权限只能是 read 或 write。")
        if not default_sandbox_path.startswith("/") or "//" in default_sandbox_path:
            raise WorkspaceAccessError("Workspace 沙箱路径必须是以 / 开头的规范路径。")
        logical_path = host_path.absolute()
        self._assert_allowed_host_path(logical_path)
        resolved_path = logical_path.resolve()
        self._assert_allowed_symlink_target(logical_path, resolved_path)
        resolved_path.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT created_at FROM workspaces WHERE workspace_key = ?",
                (workspace_key,),
            ).fetchone()
            created_at = datetime.fromisoformat(existing["created_at"]) if existing else now
            connection.execute(
                """
                INSERT INTO workspaces (
                    workspace_id, org_id, workspace_key, workspace_type, display_name,
                    host_path, default_sandbox_path, default_permission, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(workspace_key) DO UPDATE SET
                    org_id = excluded.org_id,
                    workspace_type = excluded.workspace_type,
                    display_name = excluded.display_name,
                    host_path = excluded.host_path,
                    default_sandbox_path = excluded.default_sandbox_path,
                    default_permission = excluded.default_permission,
                    status = 'active',
                    updated_at = excluded.updated_at
                """,
                (
                    workspace_id,
                    organization_id,
                    workspace_key,
                    workspace_type,
                    display_name,
                    str(resolved_path),
                    default_sandbox_path,
                    default_permission,
                    created_at.isoformat(),
                    now.isoformat(),
                ),
            )

    def _upsert_grant(
        self,
        *,
        organization_id: str | None,
        subject_type: str,
        subject_key: str,
        workspace_key: str,
        permission: str,
    ) -> None:
        if permission not in {"read", "write"}:
            raise WorkspaceAccessError("Workspace grant 权限只能是 read 或 write。")
        with self._connect() as connection:
            workspace = connection.execute(
                "SELECT workspace_id FROM workspaces WHERE workspace_key = ?",
                (workspace_key,),
            ).fetchone()
        if workspace is None:
            raise WorkspaceAccessError("Workspace 不存在。")
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                """
                SELECT grant_id, created_at
                FROM workspace_grants
                WHERE subject_type = ? AND subject_key = ? AND workspace_id = ?
                """,
                (subject_type, subject_key, workspace["workspace_id"]),
            ).fetchone()
            grant_id = existing["grant_id"] if existing else str(uuid4())
            created_at = datetime.fromisoformat(existing["created_at"]) if existing else now
            connection.execute(
                """
                INSERT INTO workspace_grants (
                    grant_id, org_id, subject_type, subject_key, workspace_id,
                    permission, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?)
                ON CONFLICT(subject_type, subject_key, workspace_id) DO UPDATE SET
                    org_id = excluded.org_id,
                    permission = excluded.permission,
                    status = 'active',
                    updated_at = excluded.updated_at
                """,
                (
                    grant_id,
                    organization_id,
                    subject_type,
                    subject_key,
                    workspace["workspace_id"],
                    permission,
                    created_at.isoformat(),
                    now.isoformat(),
                ),
            )

    def _assert_allowed_host_path(self, path: Path) -> None:
        workspace_root = (self.base_dir / "workspace").absolute()
        try:
            relative = path.relative_to(workspace_root)
        except ValueError as exc:
            raise WorkspaceAccessError(f"Workspace host_path 不在允许目录内：{path}") from exc

        parts = relative.parts
        if len(parts) >= 2 and parts[1] in {"knowledge", "departments"}:
            return
        if parts and parts[0] == "users":
            return
        if len(parts) >= 2 and parts[0] == "runtime" and parts[1] == "containers":
            return
        raise WorkspaceAccessError(f"Workspace host_path 不在允许目录内：{path}")

    def _assert_allowed_symlink_target(self, logical_path: Path, resolved_path: Path) -> None:
        if logical_path == resolved_path:
            return
        workspace_root = (self.base_dir / "workspace").absolute()
        relative = logical_path.relative_to(workspace_root)
        main_workspace_root = self._main_checkout_workspace_root()
        if main_workspace_root is not None and resolved_path == (main_workspace_root / relative).resolve():
            return
        raise WorkspaceAccessError(f"Workspace 软链接目标不是主检出目录的对应路径：{resolved_path}")

    def _main_checkout_workspace_root(self) -> Path | None:
        git_file = self.base_dir / ".git"
        if not git_file.is_file():
            return None
        value = git_file.read_text(encoding="utf-8").strip()
        if not value.startswith("gitdir:"):
            return None
        git_dir = Path(value.removeprefix("gitdir:").strip())
        if not git_dir.is_absolute():
            git_dir = self.base_dir / git_dir
        commondir_file = git_dir / "commondir"
        if not commondir_file.is_file():
            return None
        common_git_dir = (git_dir / commondir_file.read_text(encoding="utf-8").strip()).resolve()
        main_checkout_root = common_git_dir.parent
        if (main_checkout_root / ".git").resolve() != common_git_dir:
            return None
        return (main_checkout_root / "workspace").resolve()

    def _knowledge_root(self, organization_key: str) -> Path:
        return self.base_dir / "workspace" / self._safe_segment(organization_key) / "knowledge"

    def _department_root(self, organization_key: str) -> Path:
        return self.base_dir / "workspace" / self._safe_segment(organization_key) / "departments"

    def _user_root(self, user_id: str) -> Path:
        return self.base_dir / "workspace" / "users" / self._safe_segment(user_id)

    def _workspace_id(self, workspace_key: str) -> str:
        return "ws_" + re.sub(r"[^A-Za-z0-9_]+", "_", workspace_key).strip("_")

    def _department_keys_for_user(self, user: UserRecord) -> list[str]:
        values = " ".join(
            value
            for value in [user.department_name or "", user.access_group or ""]
            if value
        ).lower()
        keys: list[str] = []
        if "产品" in values or "product" in values:
            keys.append("product")
        if "研发" in values or "engineering" in values or "developer" in values:
            keys.append("engineering")
        if user.access_group == "admin" or user.role == "admin":
            keys.append("admin")
        return list(dict.fromkeys(keys))

    def _normalize_key(self, value: str, error_message: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise WorkspaceAccessError(error_message)
        if normalized in {"me", "tmp"}:
            raise WorkspaceAccessError("组织键不能使用保留路径名 me 或 tmp。")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", normalized):
            raise WorkspaceAccessError("组织键只能包含小写字母、数字、下划线或中划线。")
        return normalized

    def _safe_segment(self, value: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
        return safe or "unknown"

    def _row_to_organization(self, row: sqlite3.Row) -> OrganizationRecord:
        return OrganizationRecord(
            organization_id=row["organization_id"],
            organization_key=row["organization_key"],
            display_name=row["display_name"],
            status=row["status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _row_to_membership(self, row: sqlite3.Row) -> OrganizationMembershipRecord:
        return OrganizationMembershipRecord(
            membership_id=row["membership_id"],
            organization_id=row["org_id"],
            organization_key=row["organization_key"],
            user_id=row["user_id"],
            organization_role=row["org_role"],
            source=row["source"],
            is_default=bool(row["is_default"]),
            status=row["status"],
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _row_to_access_request(self, row: sqlite3.Row) -> OrganizationAccessRequestRecord:
        return OrganizationAccessRequestRecord(
            request_id=row["request_id"],
            requester_user_id=row["requester_user_id"],
            source_organization_id=row["source_org_id"],
            source_organization_key=row["source_organization_key"],
            target_organization_id=row["target_org_id"],
            target_organization_key=row["target_organization_key"],
            reason=row["reason"],
            requested_scope=row["requested_scope"],
            status=row["status"],
            reviewed_by_user_id=row["reviewed_by_user_id"],
            reviewed_at=datetime.fromisoformat(row["reviewed_at"]) if row["reviewed_at"] else None,
            review_comment=row["review_comment"],
            expires_at=datetime.fromisoformat(row["expires_at"]) if row["expires_at"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = getattr(self._thread_local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.db_path, timeout=self.connection_timeout_seconds)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(f"PRAGMA busy_timeout = {int(self.connection_timeout_seconds * 1000)}")
            self._thread_local.connection = connection
        return connection


class WorkspaceAccessService:
    def __init__(self, *, store: SQLiteWorkspaceAccessStore, base_dir: Path) -> None:
        self.store = store
        self.base_dir = base_dir.resolve()

    def list_user_organizations(self, user: UserRecord) -> list[OrganizationMembershipRecord]:
        return self.store.list_active_user_memberships(user.user_id)

    def resolve_active_organization(self, user: UserRecord, organization_key: str | None) -> OrganizationMembershipRecord:
        if user.status != "active":
            raise WorkspaceAccessError("账号未启用，不能启动 Assistant Runtime。")
        memberships = self.store.list_active_user_memberships(user.user_id)
        if organization_key:
            requested = organization_key.strip().lower()
            for membership in memberships:
                if membership.organization_key == requested:
                    return membership
            raise WorkspaceAccessError("你没有访问该组织的权限。")
        if not memberships:
            raise WorkspaceAccessError("等待管理员分配组织后才能启动 Assistant Runtime。")
        defaults = [membership for membership in memberships if membership.is_default]
        if len(memberships) == 1:
            return memberships[0]
        if len(defaults) == 1:
            return defaults[0]
        raise WorkspaceAccessError("你属于多个组织，请先选择本次会话使用的组织。")

    def resolve_runtime_plan(
        self,
        *,
        user: UserRecord,
        organization_key: str | None,
        session_id: str,
        organization_keys: list[str] | None = None,
        extra_readonly_mounts: list[RuntimeWorkspaceMount] | None = None,
    ) -> RuntimeWorkspacePlan:
        membership = self.resolve_active_organization(user, organization_key)
        plan_organization_keys = self._resolve_plan_organization_keys(
            user=user,
            active_organization_key=membership.organization_key,
            organization_keys=organization_keys,
        )
        records = []
        for plan_organization_key in plan_organization_keys:
            records.extend(self.store.resolve_plan_records(user=user, organization_key=plan_organization_key))
        deduped_records = {
            (row["workspace_key"], row["default_sandbox_path"]): row
            for row in records
        }
        mounts = [
            RuntimeWorkspaceMount(
                workspace_id=row["workspace_id"],
                workspace_key=row["workspace_key"],
                host_path=row["host_path"],
                sandbox_path=row["default_sandbox_path"],
                permission=row["permission"],
            )
            for row in deduped_records.values()
        ]
        tmp_host = self.base_dir / "workspace/runtime/containers" / self._safe_segment(session_id) / "tmp"
        tmp_host.mkdir(parents=True, exist_ok=True)
        mounts.append(
            RuntimeWorkspaceMount(
                workspace_id=f"runtime_tmp_{session_id}",
                workspace_key=f"runtime:tmp:{session_id}",
                host_path=str(tmp_host.resolve()),
                sandbox_path="/tmp",
                permission="write",
            )
        )
        for mount in extra_readonly_mounts or []:
            self._validate_backend_readonly_mount(mount)
            mounts.append(mount)
        host_shadow_root = self._materialize_host_runtime_view(session_id=session_id, mounts=mounts)
        return RuntimeWorkspacePlan(
            user_id=user.user_id,
            session_id=session_id,
            organization_key=membership.organization_key,
            sandbox_cwd="/",
            host_shadow_root=str(host_shadow_root),
            mounts=mounts,
        )

    def _resolve_plan_organization_keys(
        self,
        *,
        user: UserRecord,
        active_organization_key: str,
        organization_keys: list[str] | None,
    ) -> list[str]:
        if not organization_keys:
            return [active_organization_key]
        requested_keys = [key.strip().lower() for key in organization_keys if key.strip()]
        if active_organization_key not in requested_keys:
            requested_keys.insert(0, active_organization_key)
        memberships = {
            membership.organization_key
            for membership in self.store.list_active_user_memberships(user.user_id)
        }
        unauthorized = [key for key in requested_keys if key not in memberships]
        if unauthorized:
            raise WorkspaceAccessError("你没有访问该组织的权限。")
        return list(dict.fromkeys(requested_keys))

    def list_accessible_workspaces(self, *, user: UserRecord, organization_key: str | None) -> list[AccessibleWorkspaceRecord]:
        membership = self.resolve_active_organization(user, organization_key)
        return self.store.list_accessible_workspaces(user=user, organization_key=membership.organization_key)

    def create_access_request(
        self,
        *,
        user: UserRecord,
        target_organization_key: str,
        reason: str,
        requested_scope: str | None,
        expires_at: datetime | None,
    ) -> OrganizationAccessRequestRecord:
        memberships = self.store.list_active_user_memberships(user.user_id)
        source_key = memberships[0].organization_key if memberships else None
        return self.store.create_access_request(
            requester_user_id=user.user_id,
            target_organization_key=target_organization_key,
            reason=reason,
            requested_scope=requested_scope,
            source_organization_key=source_key,
            expires_at=expires_at,
        )

    def _materialize_host_runtime_view(self, *, session_id: str, mounts: list[RuntimeWorkspaceMount]) -> Path:
        root = self.base_dir / "workspace/runtime/sessions" / self._safe_segment(session_id) / "root"
        root.mkdir(parents=True, exist_ok=True)
        git_mounts = [mount for mount in mounts if mount.sandbox_path.startswith("/repos/")]
        attachment_mounts = [mount for mount in mounts if mount.sandbox_path.startswith("/attachments/")]
        for mount in mounts:
            if mount in git_mounts or mount in attachment_mounts:
                continue
            target = root / mount.sandbox_path.removeprefix("/").strip("/")
            source = Path(mount.host_path).resolve()
            self._replace_path_with_link(target, source)
        self._materialize_git_runtime_view(root=root, mounts=git_mounts)
        self._materialize_attachment_runtime_view(root=root, mounts=attachment_mounts)
        self._materialize_runtime_skills_view(root=root, mounts=mounts)
        return root.resolve()

    def _materialize_attachment_runtime_view(
        self,
        *,
        root: Path,
        mounts: list[RuntimeWorkspaceMount],
    ) -> None:
        views_root = root / ".attachment-views"
        views_root.mkdir(parents=True, exist_ok=True)
        attachments_target = root / "attachments"
        active_view: Path | None = None
        if mounts:
            active_view = views_root / uuid4().hex
            active_view.mkdir()
            for mount in mounts:
                target = active_view / Path(mount.sandbox_path).name
                target.symlink_to(Path(mount.host_path).resolve())
            self._replace_path_with_link(attachments_target, active_view)
        elif attachments_target.is_symlink() or attachments_target.is_file():
            attachments_target.unlink()
        elif attachments_target.exists():
            shutil.rmtree(attachments_target)

        for child in views_root.iterdir():
            if active_view is None or child.resolve() != active_view.resolve():
                shutil.rmtree(child, ignore_errors=True)

    def _materialize_runtime_skills_view(self, *, root: Path, mounts: list[RuntimeWorkspaceMount]) -> None:
        skills_view = root / ".agents/skills"
        if skills_view.is_symlink() or (skills_view.exists() and not skills_view.is_dir()):
            skills_view.unlink()
        elif skills_view.exists():
            shutil.rmtree(skills_view)
        skills_view.mkdir(parents=True)

        skill_sources = self._skill_directories(self.base_dir / "workspace/.claude/skills")
        personal_mount = next(
            (mount for mount in mounts if mount.sandbox_path == "/me"),
            None,
        )
        if personal_mount is not None:
            personal_skills_root = Path(personal_mount.host_path).resolve() / ".claude/skills"
            skill_sources.update(self._skill_directories(personal_skills_root))

        for skill_name, source in sorted(skill_sources.items()):
            (skills_view / skill_name).symlink_to(source, target_is_directory=True)

    @staticmethod
    def _skill_directories(skills_root: Path) -> dict[str, Path]:
        if not skills_root.is_dir():
            return {}
        return {
            child.name: child.resolve()
            for child in skills_root.iterdir()
            if child.is_dir() and (child / "SKILL.md").is_file()
        }

    def _replace_path_with_link(self, target: Path, source: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink() and Path(os.readlink(target)) == source:
            return
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        temporary.symlink_to(source, target_is_directory=source.is_dir())
        backup: Path | None = None
        if target.exists() and not target.is_symlink():
            backup = target.with_name(f".{target.name}.{uuid4().hex}.backup")
            os.replace(target, backup)
        try:
            os.replace(temporary, target)
        except Exception:
            temporary.unlink(missing_ok=True)
            if backup is not None:
                os.replace(backup, target)
            raise
        if backup is not None:
            if backup.is_dir():
                shutil.rmtree(backup)
            else:
                backup.unlink()

    def _validate_backend_readonly_mount(self, mount: RuntimeWorkspaceMount) -> None:
        sandbox_parts = Path(mount.sandbox_path).parts
        if mount.permission != "read" or not sandbox_parts or sandbox_parts[0] != "/":
            raise WorkspaceAccessError("后端只允许生成只读 Runtime 挂载。")
        source = Path(mount.host_path).resolve()
        if (
            len(sandbox_parts) == 4
            and sandbox_parts[1] == "repos"
            and sandbox_parts[3] in {"code", "context"}
        ):
            controlled_roots = (
                self.base_dir / "workspace/runtime/code-review-worktrees",
                self.base_dir / "workspace/runtime/code-review-contexts",
            )
            if not source.is_dir() or not any(
                source.is_relative_to(root.resolve())
                for root in controlled_roots
            ):
                raise WorkspaceAccessError("精确 Git Context 来源不在受控 runtime 根目录。")
            return
        if len(sandbox_parts) == 3 and sandbox_parts[1] == "attachments":
            uploads_root = (self.base_dir / "workspace/runtime/uploads").resolve()
            if (
                not source.is_file()
                or source.name != sandbox_parts[2]
                or not source.is_relative_to(uploads_root)
                or mount.workspace_key != f"attachment:{source.stem}"
            ):
                raise WorkspaceAccessError("上传附件来源或挂载路径不在受控 runtime 上传目录。")
            return
        else:
            raise WorkspaceAccessError("精确 Git Context 挂载路径必须由后端生成在 /repos/{repo}/code|context。")

    def _materialize_git_runtime_view(
        self,
        *,
        root: Path,
        mounts: list[RuntimeWorkspaceMount],
    ) -> None:
        views_root = root / ".git-context-views"
        views_root.mkdir(parents=True, exist_ok=True)
        view = views_root / uuid4().hex
        view.mkdir()
        try:
            for mount in mounts:
                parts = Path(mount.sandbox_path).parts
                target = view / parts[2] / parts[3]
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(Path(mount.host_path).resolve(), target_is_directory=True)
            repos_target = root / "repos"
            previous_view = (
                (repos_target.parent / os.readlink(repos_target)).resolve()
                if repos_target.is_symlink()
                else None
            )
            self._replace_path_with_link(repos_target, view.resolve())
        except Exception:
            shutil.rmtree(view, ignore_errors=True)
            raise
        if (
            previous_view is not None
            and previous_view != view.resolve()
            and previous_view.is_relative_to(views_root.resolve())
        ):
            shutil.rmtree(previous_view, ignore_errors=True)

    def _safe_segment(self, value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "unknown"


def runtime_workspace_plan_to_json(plan: RuntimeWorkspacePlan) -> str:
    return json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True)
