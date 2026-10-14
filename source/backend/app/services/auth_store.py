from __future__ import annotations

import sqlite3
from hashlib import sha256
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, local
from uuid import uuid4

from app.services.auth_models import (
    AccessDecision,
    AgentAccessRequestRecord,
    SessionRecord,
    UserRecord,
    VerificationCodeRecord,
    VerificationConsumeResult,
)


@dataclass(slots=True)
class ExternalIdentityRecord:
    identity_id: str
    user_id: str
    provider: str
    provider_user_id: str | None
    provider_email: str
    hosted_domain: str | None
    email_verified: bool
    linked_at: datetime
    last_verified_at: datetime


class SQLiteAuthStore:
    connection_timeout_seconds = 0.2

    def __init__(self, db_path: str, session_ttl_seconds: int) -> None:
        self.db_path = Path(db_path)
        self.session_ttl_seconds = session_ttl_seconds
        self._lock = Lock()
        self._thread_local = local()
        self._initialize()

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            self._migrate(connection)
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    email TEXT NOT NULL UNIQUE,
                    name TEXT,
                    avatar_url TEXT,
                    status TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'member',
                    default_role TEXT NOT NULL,
                    agent_access TEXT NOT NULL DEFAULT 'none',
                    auth_provider TEXT NOT NULL,
                    hosted_domain TEXT,
                    email_verified INTEGER NOT NULL,
                    department_name TEXT,
                    access_group TEXT,
                    created_at TEXT NOT NULL,
                    first_login_at TEXT NOT NULL,
                    last_login_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS agent_access_requests (
                    request_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reviewed_by_user_id TEXT,
                    reviewed_at TEXT,
                    review_comment TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS external_identities (
                    identity_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    provider_user_id TEXT,
                    provider_email TEXT NOT NULL,
                    hosted_domain TEXT,
                    email_verified INTEGER NOT NULL,
                    linked_at TEXT NOT NULL,
                    last_verified_at TEXT NOT NULL,
                    UNIQUE(provider, provider_email),
                    UNIQUE(provider, provider_user_id),
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    token TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS access_decision_logs (
                    decision_id TEXT PRIMARY KEY,
                    user_id TEXT,
                    email TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    result TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    matched_department TEXT,
                    access_group TEXT,
                    decided_at TEXT NOT NULL,
                    FOREIGN KEY(user_id) REFERENCES users(user_id)
                );

                CREATE TABLE IF NOT EXISTS email_verification_codes (
                    verification_id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    code_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );
                """
            )

    def _migrate(self, connection: sqlite3.Connection) -> None:
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'users'"
        ).fetchone()
        if table_exists is None:
            return

        existing_cols = {row[1] for row in connection.execute("PRAGMA table_info(users)").fetchall()}
        if "role" not in existing_cols:
            connection.execute("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member'")
        if "agent_access" not in existing_cols:
            # 旧模型迁移：注册审核制 → 注册即激活 + Agent 权限独立申请。
            # 已通过旧审核的用户直接获得 Agent 权限；待审核/已拒绝的用户激活账号，
            # Agent 权限回到未申请状态，由用户按需重新申请。
            connection.execute("ALTER TABLE users ADD COLUMN agent_access TEXT NOT NULL DEFAULT 'none'")
            connection.execute("UPDATE users SET agent_access = 'active' WHERE status = 'active'")
            connection.execute("UPDATE users SET status = 'active' WHERE status IN ('pending', 'rejected')")

    def list_users(self, status: str | None = None) -> list[UserRecord]:
        where = "WHERE status = ?" if status else ""
        params = (status,) if status else ()
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM users {where} ORDER BY created_at DESC",
                params,
            ).fetchall()
        return [user for row in rows if (user := self._row_to_user(row)) is not None]

    def update_user_status(self, user_id: str, status: str) -> UserRecord | None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE users SET status = ? WHERE user_id = ?",
                (status, user_id),
            )
        return self.get_user_by_id(user_id)

    def update_user_role(self, user_id: str, role: str) -> UserRecord | None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE users SET role = ? WHERE user_id = ?",
                (role, user_id),
            )
        return self.get_user_by_id(user_id)

    def update_user_agent_access(self, user_id: str, agent_access: str) -> UserRecord | None:
        if agent_access not in {"none", "pending", "active", "rejected"}:
            raise ValueError("Agent 权限状态只能是 none、pending、active 或 rejected。")
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE users SET agent_access = ? WHERE user_id = ?",
                (agent_access, user_id),
            )
        return self.get_user_by_id(user_id)

    def create_agent_access_request(self, *, user_id: str, reason: str) -> AgentAccessRequestRecord:
        now = datetime.now(timezone.utc)
        request_id = str(uuid4())
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_access_requests (
                    request_id, user_id, reason, status, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending', ?, ?)
                """,
                (request_id, user_id, reason.strip(), now.isoformat(), now.isoformat()),
            )
        request = self.get_agent_access_request(request_id)
        assert request is not None
        return request

    def get_agent_access_request(self, request_id: str) -> AgentAccessRequestRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_access_requests WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return self._row_to_agent_access_request(row)

    def get_latest_agent_access_request(self, user_id: str) -> AgentAccessRequestRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM agent_access_requests
                WHERE user_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return self._row_to_agent_access_request(row)

    def list_agent_access_requests(
        self,
        *,
        status: str | None = None,
        user_id: str | None = None,
    ) -> list[AgentAccessRequestRecord]:
        clauses: list[str] = []
        params: list[object] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM agent_access_requests {where} ORDER BY created_at DESC",
                params,
            ).fetchall()
        return [record for row in rows if (record := self._row_to_agent_access_request(row)) is not None]

    def review_agent_access_request(
        self,
        *,
        request_id: str,
        status: str,
        reviewer_user_id: str,
        review_comment: str | None,
    ) -> AgentAccessRequestRecord | None:
        if status not in {"approved", "rejected"}:
            raise ValueError("审核结果只能是 approved 或 rejected。")
        request = self.get_agent_access_request(request_id)
        if request is None:
            return None
        if request.status != "pending":
            raise ValueError("该申请已处理。")
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE agent_access_requests
                SET status = ?, reviewed_by_user_id = ?, reviewed_at = ?, review_comment = ?, updated_at = ?
                WHERE request_id = ? AND status = 'pending'
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
        return self.get_agent_access_request(request_id)

    def upsert_google_user(
        self,
        *,
        email: str,
        name: str | None,
        avatar_url: str | None,
        hosted_domain: str | None,
        email_verified: bool,
        google_user_id: str,
        decision: AccessDecision,
        initial_status: str = "active",
        initial_role: str = "member",
        initial_agent_access: str = "none",
    ) -> UserRecord:
        now = datetime.now(timezone.utc)
        existing_identity = self.get_external_identity("google", google_user_id)
        existing_user = self.get_user_by_id(existing_identity.user_id) if existing_identity else self.get_user_by_email(email)

        with self._lock, self._connect() as connection:
            if existing_user is None:
                user = UserRecord(
                    user_id=str(uuid4()),
                    email=email,
                    name=name,
                    avatar_url=avatar_url,
                    status=initial_status,
                    role=initial_role,
                    default_role="member",
                    agent_access=initial_agent_access,
                    auth_provider="google",
                    hosted_domain=hosted_domain,
                    email_verified=email_verified,
                    department_name=decision.matched_department,
                    access_group=decision.access_group,
                    created_at=now,
                    first_login_at=now,
                    last_login_at=now,
                )
                connection.execute(
                    """
                    INSERT INTO users (
                        user_id, email, name, avatar_url, status, role, default_role, agent_access, auth_provider,
                        hosted_domain, email_verified, department_name, access_group,
                        created_at, first_login_at, last_login_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    self._user_params(user),
                )
            else:
                user = UserRecord(
                    user_id=existing_user.user_id,
                    email=email,
                    name=name,
                    avatar_url=avatar_url,
                    status=existing_user.status,
                    role=existing_user.role,
                    default_role=existing_user.default_role,
                    agent_access=existing_user.agent_access,
                    auth_provider="google",
                    hosted_domain=hosted_domain,
                    email_verified=email_verified,
                    department_name=decision.matched_department,
                    access_group=decision.access_group,
                    created_at=existing_user.created_at,
                    first_login_at=existing_user.first_login_at,
                    last_login_at=now,
                )
                connection.execute(
                    """
                    UPDATE users
                    SET email = ?, name = ?, avatar_url = ?, auth_provider = ?,
                        hosted_domain = ?, email_verified = ?, department_name = ?, access_group = ?, last_login_at = ?
                    WHERE user_id = ?
                    """,
                    (
                        user.email,
                        user.name,
                        user.avatar_url,
                        user.auth_provider,
                        user.hosted_domain,
                        int(user.email_verified),
                        user.department_name,
                        user.access_group,
                        user.last_login_at.isoformat(),
                        user.user_id,
                    ),
                )

            self._upsert_external_identity(
                connection,
                ExternalIdentityRecord(
                    identity_id=existing_identity.identity_id if existing_identity else str(uuid4()),
                    user_id=user.user_id,
                    provider="google",
                    provider_user_id=google_user_id,
                    provider_email=email,
                    hosted_domain=hosted_domain,
                    email_verified=email_verified,
                    linked_at=existing_identity.linked_at if existing_identity else now,
                    last_verified_at=now,
                ),
            )
            return user

    def create_email_user(
        self,
        email: str,
        name: str | None,
        *,
        initial_status: str = "active",
        initial_role: str = "member",
        initial_agent_access: str = "none",
    ) -> UserRecord:
        now = datetime.now(timezone.utc)
        normalized_email = email.lower()
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT * FROM users WHERE lower(email) = lower(?)",
                (normalized_email,),
            ).fetchone()
            if existing is not None:
                raise ValueError("该邮箱已经注册。")

            domain = normalized_email.rsplit("@", 1)[-1]
            user = UserRecord(
                user_id=str(uuid4()),
                email=normalized_email,
                name=name,
                avatar_url=None,
                status=initial_status,
                role=initial_role,
                default_role="member",
                agent_access=initial_agent_access,
                auth_provider="email",
                hosted_domain=domain,
                email_verified=True,
                department_name=None,
                access_group="member",
                created_at=now,
                first_login_at=now,
                last_login_at=now,
            )
            connection.execute(
                """
                INSERT INTO users (
                    user_id, email, name, avatar_url, status, role, default_role, agent_access, auth_provider,
                    hosted_domain, email_verified, department_name, access_group,
                    created_at, first_login_at, last_login_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                self._user_params(user),
            )
            self._upsert_external_identity(
                connection,
                ExternalIdentityRecord(
                    identity_id=str(uuid4()),
                    user_id=user.user_id,
                    provider="email",
                    provider_user_id=normalized_email,
                    provider_email=normalized_email,
                    hosted_domain=domain,
                    email_verified=True,
                    linked_at=now,
                    last_verified_at=now,
                ),
            )
            return user

    def touch_email_login(self, email: str) -> UserRecord | None:
        existing_user = self.get_user_by_email(email)
        if existing_user is None:
            return None

        now = datetime.now(timezone.utc)
        updated = UserRecord(
            user_id=existing_user.user_id,
            email=existing_user.email,
            name=existing_user.name,
            avatar_url=existing_user.avatar_url,
            status=existing_user.status,
            role=existing_user.role,
            default_role=existing_user.default_role,
            agent_access=existing_user.agent_access,
            auth_provider=existing_user.auth_provider,
            hosted_domain=existing_user.hosted_domain,
            email_verified=existing_user.email_verified,
            department_name=existing_user.department_name,
            access_group=existing_user.access_group,
            created_at=existing_user.created_at,
            first_login_at=existing_user.first_login_at,
            last_login_at=now,
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE users
                SET last_login_at = ?, status = ?, auth_provider = ?
                WHERE user_id = ?
                """,
                (updated.last_login_at.isoformat(), updated.status, "email", updated.user_id),
            )
            identity = self.get_external_identity("email", updated.email)
            self._upsert_external_identity(
                connection,
                ExternalIdentityRecord(
                    identity_id=identity.identity_id if identity else str(uuid4()),
                    user_id=updated.user_id,
                    provider="email",
                    provider_user_id=updated.email,
                    provider_email=updated.email,
                    hosted_domain=updated.hosted_domain,
                    email_verified=True,
                    linked_at=identity.linked_at if identity else now,
                    last_verified_at=now,
                ),
            )
        return updated

    def create_session(self, user_id: str, token: str) -> SessionRecord:
        now = datetime.now(timezone.utc)
        session = SessionRecord(
            token=token,
            user_id=user_id,
            expires_at=now + timedelta(seconds=self.session_ttl_seconds),
            created_at=now,
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO sessions (token, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
                (
                    session.token,
                    session.user_id,
                    session.created_at.isoformat(),
                    session.expires_at.isoformat(),
                ),
            )
        return session

    def get_session(self, token: str) -> SessionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT token, user_id, created_at, expires_at FROM sessions WHERE token = ?",
                (token,),
            ).fetchone()
        if row is None:
            return None

        session = SessionRecord(
            token=row["token"],
            user_id=row["user_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
        )
        if session.expires_at <= datetime.now(timezone.utc):
            return None
        return session

    def delete_session(self, token: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM sessions WHERE token = ?", (token,))

    def issue_verification_code(
        self,
        *,
        email: str,
        purpose: str,
        code: str,
        ttl_seconds: int,
    ) -> VerificationCodeRecord:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds)
        record = VerificationCodeRecord(
            verification_id=str(uuid4()),
            email=email.lower(),
            purpose=purpose,
            code_hash=self._hash_code(code),
            status="pending",
            attempt_count=0,
            created_at=now,
            expires_at=expires_at,
            consumed_at=None,
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE email_verification_codes
                SET status = 'replaced'
                WHERE lower(email) = lower(?) AND purpose = ? AND status = 'pending'
                """,
                (record.email, purpose),
            )
            connection.execute(
                """
                INSERT INTO email_verification_codes (
                    verification_id, email, purpose, code_hash, status, attempt_count,
                    created_at, expires_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.verification_id,
                    record.email,
                    record.purpose,
                    record.code_hash,
                    record.status,
                    record.attempt_count,
                    record.created_at.isoformat(),
                    record.expires_at.isoformat(),
                    record.consumed_at,
                ),
            )
        return record

    def get_latest_pending_verification_code(self, email: str, purpose: str) -> VerificationCodeRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM email_verification_codes
                WHERE lower(email) = lower(?) AND purpose = ? AND status = 'pending'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (email, purpose),
            ).fetchone()
        return self._row_to_verification_code(row)

    def consume_verification_code(
        self,
        *,
        email: str,
        purpose: str,
        code: str,
        max_attempts: int,
    ) -> VerificationConsumeResult:
        record = self.get_latest_pending_verification_code(email, purpose)
        if record is None:
            return VerificationConsumeResult(valid=False, reason="请先获取验证码。")

        now = datetime.now(timezone.utc)
        if record.expires_at <= now:
            self._update_verification_status(record.verification_id, status="expired", consumed_at=None)
            return VerificationConsumeResult(valid=False, reason="验证码已过期，请重新获取。")

        if record.attempt_count >= max_attempts:
            self._update_verification_status(record.verification_id, status="locked", consumed_at=None)
            return VerificationConsumeResult(valid=False, reason="验证码错误次数过多，请重新获取。")

        if record.code_hash != self._hash_code(code):
            next_attempt_count = record.attempt_count + 1
            status = "locked" if next_attempt_count >= max_attempts else "pending"
            self._increment_verification_attempt(record.verification_id, next_attempt_count, status)
            if status == "locked":
                return VerificationConsumeResult(valid=False, reason="验证码错误次数过多，请重新获取。")
            return VerificationConsumeResult(valid=False, reason="验证码不正确。")

        self._update_verification_status(record.verification_id, status="consumed", consumed_at=now)
        return VerificationConsumeResult(valid=True, reason="验证码校验通过。")

    def get_user_by_id(self, user_id: str) -> UserRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        return self._row_to_user(row)

    def get_user_by_email(self, email: str) -> UserRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE lower(email) = lower(?)",
                (email,),
            ).fetchone()
        return self._row_to_user(row)

    def list_users_by_ids(self, user_ids: list[str]) -> list[UserRecord]:
        ordered_user_ids = list(dict.fromkeys(user_ids))
        if not ordered_user_ids:
            return []
        placeholders = ", ".join("?" for _ in ordered_user_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM users
                WHERE user_id IN ({placeholders})
                """,
                ordered_user_ids,
            ).fetchall()
        users_by_id = {
            user.user_id: user
            for row in rows
            if (user := self._row_to_user(row)) is not None
        }
        return [users_by_id[user_id] for user_id in ordered_user_ids if user_id in users_by_id]

    def search_users(self, query: str, *, limit: int = 20) -> list[UserRecord]:
        normalized_query = " ".join(query.strip().split())
        params: list[object] = []
        where_clause = "status = 'active'"
        if normalized_query:
            like_query = f"%{normalized_query.lower()}%"
            where_clause = """
                status = 'active'
                AND (
                    lower(email) LIKE ?
                    OR lower(COALESCE(name, '')) LIKE ?
                )
            """
            params.extend([like_query, like_query])
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM users
                WHERE {where_clause}
                ORDER BY last_login_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [user for row in rows if (user := self._row_to_user(row)) is not None]

    def get_external_identity(self, provider: str, provider_user_id: str) -> ExternalIdentityRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM external_identities
                WHERE provider = ? AND (
                    provider_user_id = ? OR lower(provider_email) = lower(?)
                )
                ORDER BY last_verified_at DESC
                LIMIT 1
                """,
                (provider, provider_user_id, provider_user_id),
            ).fetchone()
        if row is None:
            return None
        return ExternalIdentityRecord(
            identity_id=row["identity_id"],
            user_id=row["user_id"],
            provider=row["provider"],
            provider_user_id=row["provider_user_id"],
            provider_email=row["provider_email"],
            hosted_domain=row["hosted_domain"],
            email_verified=bool(row["email_verified"]),
            linked_at=datetime.fromisoformat(row["linked_at"]),
            last_verified_at=datetime.fromisoformat(row["last_verified_at"]),
        )

    def append_decision_log(
        self,
        *,
        user_id: str | None,
        email: str,
        provider: str,
        decision: AccessDecision,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO access_decision_logs (
                    decision_id, user_id, email, provider, result, reason,
                    matched_department, access_group, decided_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    user_id,
                    email,
                    provider,
                    decision.result,
                    decision.reason,
                    decision.matched_department,
                    decision.access_group,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def _upsert_external_identity(self, connection: sqlite3.Connection, identity: ExternalIdentityRecord) -> None:
        connection.execute(
            """
            INSERT INTO external_identities (
                identity_id, user_id, provider, provider_user_id, provider_email,
                hosted_domain, email_verified, linked_at, last_verified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(provider, provider_email) DO UPDATE SET
                user_id = excluded.user_id,
                provider_user_id = excluded.provider_user_id,
                hosted_domain = excluded.hosted_domain,
                email_verified = excluded.email_verified,
                last_verified_at = excluded.last_verified_at
            """,
            (
                identity.identity_id,
                identity.user_id,
                identity.provider,
                identity.provider_user_id,
                identity.provider_email,
                identity.hosted_domain,
                int(identity.email_verified),
                identity.linked_at.isoformat(),
                identity.last_verified_at.isoformat(),
            ),
        )

    def _increment_verification_attempt(self, verification_id: str, attempt_count: int, status: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE email_verification_codes
                SET attempt_count = ?, status = ?
                WHERE verification_id = ?
                """,
                (attempt_count, status, verification_id),
            )

    def _update_verification_status(
        self,
        verification_id: str,
        *,
        status: str,
        consumed_at: datetime | None,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE email_verification_codes
                SET status = ?, consumed_at = ?
                WHERE verification_id = ?
                """,
                (
                    status,
                    consumed_at.isoformat() if consumed_at else None,
                    verification_id,
                ),
            )

    def _row_to_user(self, row: sqlite3.Row | None) -> UserRecord | None:
        if row is None:
            return None
        keys = row.keys()
        return UserRecord(
            user_id=row["user_id"],
            email=row["email"],
            name=row["name"],
            avatar_url=row["avatar_url"],
            status=row["status"],
            role=row["role"] if "role" in keys else "member",
            default_role=row["default_role"],
            agent_access=row["agent_access"] if "agent_access" in keys else "none",
            auth_provider=row["auth_provider"],
            hosted_domain=row["hosted_domain"],
            email_verified=bool(row["email_verified"]),
            department_name=row["department_name"],
            access_group=row["access_group"],
            created_at=datetime.fromisoformat(row["created_at"]),
            first_login_at=datetime.fromisoformat(row["first_login_at"]),
            last_login_at=datetime.fromisoformat(row["last_login_at"]),
        )

    def _user_params(self, user: UserRecord) -> tuple[object, ...]:
        return (
            user.user_id,
            user.email,
            user.name,
            user.avatar_url,
            user.status,
            user.role,
            user.default_role,
            user.agent_access,
            user.auth_provider,
            user.hosted_domain,
            int(user.email_verified),
            user.department_name,
            user.access_group,
            user.created_at.isoformat(),
            user.first_login_at.isoformat(),
            user.last_login_at.isoformat(),
        )

    def _row_to_agent_access_request(self, row: sqlite3.Row | None) -> AgentAccessRequestRecord | None:
        if row is None:
            return None
        return AgentAccessRequestRecord(
            request_id=row["request_id"],
            user_id=row["user_id"],
            reason=row["reason"],
            status=row["status"],
            reviewed_by_user_id=row["reviewed_by_user_id"],
            reviewed_at=datetime.fromisoformat(row["reviewed_at"]) if row["reviewed_at"] else None,
            review_comment=row["review_comment"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _row_to_verification_code(self, row: sqlite3.Row | None) -> VerificationCodeRecord | None:
        if row is None:
            return None
        return VerificationCodeRecord(
            verification_id=row["verification_id"],
            email=row["email"],
            purpose=row["purpose"],
            code_hash=row["code_hash"],
            status=row["status"],
            attempt_count=row["attempt_count"],
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            consumed_at=datetime.fromisoformat(row["consumed_at"]) if row["consumed_at"] else None,
        )

    def _hash_code(self, code: str) -> str:
        return sha256(code.encode("utf-8")).hexdigest()

    def _connect(self) -> sqlite3.Connection:
        connection = getattr(self._thread_local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.db_path, timeout=self.connection_timeout_seconds)
            connection.row_factory = sqlite3.Row
            self._configure_connection(connection)
            self._thread_local.connection = connection
        return connection

    def _configure_connection(self, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA busy_timeout = {int(self.connection_timeout_seconds * 1000)}")
        connection.execute("PRAGMA synchronous = NORMAL")
