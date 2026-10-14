from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock, local
from uuid import uuid4

from app.business.assistant.knowledge_scope import resolve_knowledge_scope_id_from_path
from app.business.assistant.models import (
    AnswerCitationRecord,
    AssistantFileArtifactRecord,
    AssistantContextInput,
    AssistantMessageFeedbackRecord,
    AssistantMessageRecord,
    AssistantRuntimeEventRecord,
    AssistantSessionRecord,
    AssistantTurnRecord,
    SessionCommentRecord,
    SessionContextMountRecord,
    SessionShareRecord,
    SharedSessionFileArtifactRecord,
    SharedSessionFollowUpRecord,
    SharedSessionSummaryRecord,
    SessionGitContextDefaultRecord,
    SHARE_TYPE_MEMBERS,
    SHARE_TYPE_PUBLIC,
    SHARE_VISIBILITY_AGENT,
    SHARE_VISIBILITY_ANYONE,
    SHARE_VISIBILITY_REGISTERED,
    SHARE_VISIBILITY_SCOPES,
    TestDataPlanRecord,
    DocoCredentialRecord,
    DocoDocumentMappingRecord,
    TurnGitContextRequestRecord,
    TurnGitContextSnapshotRecord,
    TurnContextRecord,
)


class AssistantTurnAlreadyRunningError(RuntimeError):
    """同一应用会话已有运行中的对话轮次。"""


class SQLiteAssistantStore:
    busy_timeout_ms = 5000

    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self._lock = Lock()
        self._thread_local = local()
        self._initialize()

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS assistant_sessions (
                    session_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS assistant_messages (
                    message_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES assistant_sessions(session_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS assistant_turns (
                    turn_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_message_id TEXT NOT NULL,
                    assistant_message_id TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    FOREIGN KEY(session_id) REFERENCES assistant_sessions(session_id) ON DELETE CASCADE,
                    FOREIGN KEY(user_message_id) REFERENCES assistant_messages(message_id) ON DELETE CASCADE,
                    FOREIGN KEY(assistant_message_id) REFERENCES assistant_messages(message_id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS session_git_context_defaults (
                    default_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    repo_full_name TEXT NOT NULL,
                    default_strategy TEXT NOT NULL CHECK(default_strategy IN ('follow', 'pinned')),
                    selector_type TEXT NOT NULL CHECK(selector_type IN ('pull_request', 'branch')),
                    selector_value TEXT NOT NULL,
                    last_resolved_sha TEXT,
                    logical_path TEXT NOT NULL,
                    authorization_source TEXT NOT NULL,
                    authorization_key TEXT NOT NULL,
                    last_checked_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES assistant_sessions(session_id) ON DELETE CASCADE,
                    UNIQUE(session_id, repo_full_name)
                );

                CREATE TABLE IF NOT EXISTS turn_git_context_requests (
                    request_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    repo_full_name TEXT NOT NULL,
                    strategy TEXT NOT NULL CHECK(strategy IN ('baseline', 'follow', 'pinned')),
                    selector_type TEXT CHECK(selector_type IN ('pull_request', 'branch')),
                    selector_value TEXT,
                    authorization_source TEXT NOT NULL,
                    authorization_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(turn_id) REFERENCES assistant_turns(turn_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS turn_git_context_snapshots (
                    snapshot_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    repo_full_name TEXT NOT NULL,
                    strategy TEXT NOT NULL CHECK(strategy IN ('baseline', 'follow', 'pinned')),
                    selector_type TEXT CHECK(selector_type IN ('pull_request', 'branch')),
                    selector_value TEXT,
                    resolved_sha TEXT,
                    logical_code_path TEXT NOT NULL,
                    logical_context_path TEXT,
                    source TEXT NOT NULL CHECK(source IN ('turn_explicit', 'session_default', 'baseline', 'rerun_snapshot')),
                    resolved_at TEXT NOT NULL,
                    FOREIGN KEY(turn_id) REFERENCES assistant_turns(turn_id) ON DELETE CASCADE,
                    UNIQUE(turn_id, repo_full_name)
                );

                CREATE TABLE IF NOT EXISTS session_context_mounts (
                    mount_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    context_key TEXT NOT NULL,
                    label TEXT NOT NULL,
                    content TEXT,
                    source_type TEXT NOT NULL,
                    source_uri TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES assistant_sessions(session_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS turn_context_requests (
                    request_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    context_key TEXT NOT NULL,
                    label TEXT NOT NULL,
                    content TEXT,
                    source_type TEXT NOT NULL,
                    source_uri TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(turn_id) REFERENCES assistant_turns(turn_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS turn_context_usage (
                    usage_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    context_key TEXT NOT NULL,
                    label TEXT NOT NULL,
                    content TEXT,
                    source_type TEXT NOT NULL,
                    source_uri TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(turn_id) REFERENCES assistant_turns(turn_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS image_generation_usage (
                    usage_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    turn_id TEXT,
                    item_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS uq_image_generation_usage_user_item
                    ON image_generation_usage(user_id, item_id)
                    WHERE item_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_image_generation_usage_user_created
                    ON image_generation_usage(user_id, created_at);

                CREATE TABLE IF NOT EXISTS answer_citations (
                    citation_id TEXT PRIMARY KEY,
                    turn_id TEXT NOT NULL,
                    assistant_message_id TEXT,
                    citation_type TEXT NOT NULL,
                    label TEXT NOT NULL,
                    path TEXT,
                    line_start INTEGER,
                    line_end INTEGER,
                    snippet TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(turn_id) REFERENCES assistant_turns(turn_id) ON DELETE CASCADE,
                    FOREIGN KEY(assistant_message_id) REFERENCES assistant_messages(message_id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS assistant_runtime_events (
                    event_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    turn_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES assistant_sessions(session_id) ON DELETE CASCADE,
                    FOREIGN KEY(turn_id) REFERENCES assistant_turns(turn_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS assistant_test_data_plans (
                    plan_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    prepared_turn_id TEXT NOT NULL,
                    executed_turn_id TEXT,
                    organization_key TEXT NOT NULL,
                    task_name TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    parameters_json TEXT NOT NULL,
                    redacted_parameters_json TEXT NOT NULL,
                    risk_level TEXT NOT NULL CHECK(risk_level IN ('medium', 'high')),
                    summary TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('prepared', 'executing', 'submitted', 'completed', 'failed', 'unknown')),
                    upstream_run_id TEXT,
                    upstream_status TEXT,
                    result_json TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    executed_at TEXT,
                    secret_json TEXT,
                    secret_revealed_at TEXT,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES assistant_sessions(session_id) ON DELETE CASCADE,
                    FOREIGN KEY(prepared_turn_id) REFERENCES assistant_turns(turn_id) ON DELETE CASCADE,
                    FOREIGN KEY(executed_turn_id) REFERENCES assistant_turns(turn_id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS assistant_doco_document_mappings (
                    mapping_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    local_path TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    knowledge_base_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(user_id, local_path)
                );

                CREATE INDEX IF NOT EXISTS idx_doco_mappings_user_path
                    ON assistant_doco_document_mappings(user_id, local_path);

                CREATE TABLE IF NOT EXISTS assistant_doco_credentials (
                    user_id TEXT PRIMARY KEY,
                    api_token TEXT NOT NULL,
                    default_knowledge_base_id INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS assistant_file_artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    message_id TEXT,
                    turn_id TEXT,
                    owner_user_id TEXT NOT NULL,
                    display_path TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    storage_status TEXT NOT NULL CHECK(storage_status IN ('source', 'snapshotted', 'missing')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES assistant_sessions(session_id) ON DELETE CASCADE,
                    FOREIGN KEY(message_id) REFERENCES assistant_messages(message_id) ON DELETE SET NULL,
                    FOREIGN KEY(turn_id) REFERENCES assistant_turns(turn_id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS shared_session_file_artifacts (
                    shared_file_id TEXT PRIMARY KEY,
                    share_id TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    snapshot_path TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(share_id) REFERENCES session_shares(share_id) ON DELETE CASCADE,
                    FOREIGN KEY(artifact_id) REFERENCES assistant_file_artifacts(artifact_id) ON DELETE CASCADE,
                    UNIQUE(share_id, artifact_id)
                );

                CREATE TABLE IF NOT EXISTS assistant_message_feedback (
                    feedback_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    feedback TEXT NOT NULL CHECK(feedback IN ('like', 'dislike')),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(message_id) REFERENCES assistant_messages(message_id) ON DELETE CASCADE,
                    FOREIGN KEY(session_id) REFERENCES assistant_sessions(session_id) ON DELETE CASCADE,
                    UNIQUE(message_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS session_shares (
                    share_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    owner_id TEXT NOT NULL,
                    share_type TEXT NOT NULL CHECK(share_type IN ('public', 'members')),
                    visibility TEXT NOT NULL DEFAULT 'anyone'
                        CHECK(visibility IN ('agent', 'registered', 'anyone')),
                    share_token TEXT NOT NULL UNIQUE,
                    is_active INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES assistant_sessions(session_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS session_share_members (
                    member_id TEXT PRIMARY KEY,
                    share_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    added_at TEXT NOT NULL,
                    FOREIGN KEY(share_id) REFERENCES session_shares(share_id) ON DELETE CASCADE,
                    UNIQUE(share_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS shared_session_follow_ups (
                    request_id TEXT PRIMARY KEY,
                    share_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    requested_by_user_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('queued', 'processing', 'completed', 'failed', 'cancelled')),
                    turn_id TEXT,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    FOREIGN KEY(share_id) REFERENCES session_shares(share_id) ON DELETE CASCADE,
                    FOREIGN KEY(session_id) REFERENCES assistant_sessions(session_id) ON DELETE CASCADE,
                    FOREIGN KEY(turn_id) REFERENCES assistant_turns(turn_id) ON DELETE SET NULL
                );

                CREATE TABLE IF NOT EXISTS session_comments (
                    comment_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    message_id TEXT,
                    user_id TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(session_id) REFERENCES assistant_sessions(session_id) ON DELETE CASCADE,
                    FOREIGN KEY(message_id) REFERENCES assistant_messages(message_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS session_favorites (
                    session_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(session_id, user_id),
                    FOREIGN KEY(session_id) REFERENCES assistant_sessions(session_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_assistant_sessions_user_updated
                ON assistant_sessions(user_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_assistant_messages_session_created
                ON assistant_messages(session_id, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_assistant_turns_session_started
                ON assistant_turns(session_id, started_at DESC);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_assistant_turns_one_running_per_session
                ON assistant_turns(session_id)
                WHERE status = 'running';

                CREATE INDEX IF NOT EXISTS idx_session_git_context_defaults_session
                ON session_git_context_defaults(session_id, repo_full_name);

                CREATE INDEX IF NOT EXISTS idx_turn_git_context_requests_turn
                ON turn_git_context_requests(turn_id, repo_full_name);

                CREATE INDEX IF NOT EXISTS idx_turn_git_context_snapshots_turn
                ON turn_git_context_snapshots(turn_id, repo_full_name);

                CREATE INDEX IF NOT EXISTS idx_turn_git_context_snapshots_selector
                ON turn_git_context_snapshots(repo_full_name, selector_type, selector_value, resolved_at DESC);

                CREATE INDEX IF NOT EXISTS idx_session_context_mounts_session_user_created
                ON session_context_mounts(session_id, user_id, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_turn_context_requests_turn_created
                ON turn_context_requests(turn_id, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_turn_context_usage_turn_created
                ON turn_context_usage(turn_id, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_answer_citations_turn_created
                ON answer_citations(turn_id, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_runtime_events_turn_created
                ON assistant_runtime_events(turn_id, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_runtime_events_session_created
                ON assistant_runtime_events(session_id, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_test_data_plans_user_session_created
                ON assistant_test_data_plans(user_id, session_id, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_test_data_plans_upstream_run
                ON assistant_test_data_plans(upstream_run_id);

                CREATE INDEX IF NOT EXISTS idx_file_artifacts_session_message
                ON assistant_file_artifacts(session_id, message_id, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_file_artifacts_session_source
                ON assistant_file_artifacts(session_id, source_path);

                CREATE INDEX IF NOT EXISTS idx_shared_file_artifacts_share
                ON shared_session_file_artifacts(share_id, artifact_id);

                CREATE INDEX IF NOT EXISTS idx_message_feedback_session_user
                ON assistant_message_feedback(session_id, user_id, updated_at DESC);

                CREATE INDEX IF NOT EXISTS idx_session_shares_session_active
                ON session_shares(session_id, is_active);

                CREATE INDEX IF NOT EXISTS idx_session_shares_owner_active
                ON session_shares(owner_id, is_active, created_at DESC);

                CREATE INDEX IF NOT EXISTS idx_session_share_members_user
                ON session_share_members(user_id, share_id);

                CREATE INDEX IF NOT EXISTS idx_shared_follow_ups_session_status_created
                ON shared_session_follow_ups(session_id, status, created_at ASC);

                CREATE UNIQUE INDEX IF NOT EXISTS idx_shared_follow_ups_one_processing_per_session
                ON shared_session_follow_ups(session_id)
                WHERE status = 'processing';

                CREATE INDEX IF NOT EXISTS idx_session_comments_session_created
                ON session_comments(session_id, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_session_comments_message_created
                ON session_comments(message_id, created_at ASC);

                CREATE INDEX IF NOT EXISTS idx_session_favorites_user_created
                ON session_favorites(user_id, created_at DESC);
                """
            )
            self._ensure_session_columns(connection)
            self._ensure_test_data_plan_columns(connection)
            self._ensure_session_share_visibility_column(connection)
            connection.execute(
                """
                UPDATE shared_session_follow_ups
                SET status = 'queued', started_at = NULL
                WHERE status = 'processing' AND turn_id IS NULL
                """
            )
            connection.execute("PRAGMA foreign_keys = ON")

    def create_session(
        self,
        user_id: str,
        title: str,
        *,
        runtime_provider: str | None = None,
        runtime_working_directory: str | None = None,
        active_organization_key: str | None = None,
        runtime_workspace_profile: dict[str, object] | None = None,
        status: str = "active",
        git_context_default: dict[str, object] | None = None,
    ) -> AssistantSessionRecord:
        now = datetime.now(timezone.utc)
        session = AssistantSessionRecord(
            session_id=str(uuid4()),
            user_id=user_id,
            title=title,
            created_at=now,
            updated_at=now,
            message_count=0,
            last_message_preview=None,
            runtime_provider=runtime_provider,
            runtime_working_directory=runtime_working_directory,
            active_organization_key=active_organization_key,
            runtime_workspace_profile=runtime_workspace_profile,
            status=status,
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO assistant_sessions (
                    session_id,
                    user_id,
                    title,
                    created_at,
                    updated_at,
                    runtime_provider,
                    runtime_session_id,
                    runtime_working_directory,
                    active_organization_key,
                    runtime_workspace_profile_json,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session.session_id,
                    session.user_id,
                    session.title,
                    session.created_at.isoformat(),
                    session.updated_at.isoformat(),
                    session.runtime_provider,
                    session.runtime_session_id,
                    session.runtime_working_directory,
                    session.active_organization_key,
                    json.dumps(session.runtime_workspace_profile, ensure_ascii=False, sort_keys=True)
                    if session.runtime_workspace_profile is not None
                    else None,
                    session.status,
                ),
            )
            if git_context_default is not None:
                self._insert_session_git_context_default(
                    connection,
                    session=session,
                    payload=git_context_default,
                )
        return session

    def get_session_git_context_defaults(
        self,
        session_id: str,
        user_id: str,
    ) -> list[SessionGitContextDefaultRecord]:
        return self.list_sessions_git_context_defaults([session_id], user_id).get(session_id, [])

    def list_sessions_git_context_defaults(
        self,
        session_ids: list[str],
        user_id: str,
    ) -> dict[str, list[SessionGitContextDefaultRecord]]:
        if not session_ids:
            return {}
        ordered_session_ids = list(dict.fromkeys(session_ids))
        placeholders = ", ".join("?" for _ in ordered_session_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT defaults.*
                FROM session_git_context_defaults AS defaults
                INNER JOIN assistant_sessions AS session
                  ON session.session_id = defaults.session_id
                WHERE session.user_id = ?
                  AND defaults.session_id IN ({placeholders})
                ORDER BY defaults.created_at ASC
                """,
                [user_id, *ordered_session_ids],
            ).fetchall()
        result = {session_id: [] for session_id in ordered_session_ids}
        for row in rows:
            result[row["session_id"]].append(self._row_to_session_git_context_default(row))
        return result

    def update_session_git_context_resolution(
        self,
        *,
        session_id: str,
        user_id: str,
        repo_full_name: str,
        resolved_sha: str,
        checked_at: datetime,
    ) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE session_git_context_defaults
                SET last_resolved_sha = ?, last_checked_at = ?, updated_at = ?
                WHERE session_id = ? AND user_id = ? AND repo_full_name = ?
                """,
                (
                    resolved_sha,
                    checked_at.isoformat(),
                    checked_at.isoformat(),
                    session_id,
                    user_id,
                    repo_full_name,
                ),
            )

    def get_session(self, session_id: str, user_id: str) -> AssistantSessionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    s.session_id,
                    s.user_id,
                    s.title,
                    s.created_at,
                    s.updated_at,
                    s.runtime_provider,
                    s.runtime_session_id,
                    s.runtime_working_directory,
                    s.active_organization_key,
                    s.runtime_workspace_profile_json,
                    s.status,
                    COUNT(m.message_id) AS message_count,
                    (
                        SELECT content
                        FROM assistant_messages
                        WHERE session_id = s.session_id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS last_message_preview
                FROM assistant_sessions AS s
                LEFT JOIN assistant_messages AS m ON m.session_id = s.session_id
                WHERE s.session_id = ? AND s.user_id = ?
                GROUP BY
                    s.session_id,
                    s.user_id,
                    s.title,
                    s.created_at,
                    s.updated_at,
                    s.runtime_provider,
                    s.runtime_session_id,
                    s.runtime_working_directory,
                    s.active_organization_key,
                    s.runtime_workspace_profile_json,
                    s.status
                """,
                (session_id, user_id),
            ).fetchone()
        return self._row_to_session(row)

    def list_sessions(
        self,
        user_id: str,
        *,
        query: str | None = None,
        favorited_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AssistantSessionRecord]:
        where_clauses = ["s.user_id = ?"]
        params: list[object] = [user_id]

        if favorited_only:
            where_clauses.append(
                "EXISTS (SELECT 1 FROM session_favorites AS favorite WHERE favorite.session_id = s.session_id AND favorite.user_id = ?)"
            )
            params.append(user_id)

        normalized_query = " ".join((query or "").split())
        if normalized_query:
            like_query = f"%{normalized_query}%"
            where_clauses.append(
                """
                (
                    s.title LIKE ?
                    OR EXISTS (
                        SELECT 1
                        FROM assistant_messages AS search_messages
                        WHERE search_messages.session_id = s.session_id
                          AND search_messages.content LIKE ?
                    )
                )
                """
            )
            params.extend([like_query, like_query])

        params.extend([limit, offset])

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    s.session_id,
                    s.user_id,
                    s.title,
                    s.created_at,
                    s.updated_at,
                    s.runtime_provider,
                    s.runtime_session_id,
                    s.runtime_working_directory,
                    s.active_organization_key,
                    s.runtime_workspace_profile_json,
                    s.status,
                    COUNT(m.message_id) AS message_count,
                    (
                        SELECT content
                        FROM assistant_messages
                        WHERE session_id = s.session_id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS last_message_preview
                FROM assistant_sessions AS s
                LEFT JOIN assistant_messages AS m ON m.session_id = s.session_id
                WHERE {" AND ".join(where_clauses)}
                GROUP BY
                    s.session_id,
                    s.user_id,
                    s.title,
                    s.created_at,
                    s.updated_at,
                    s.runtime_provider,
                    s.runtime_session_id,
                    s.runtime_working_directory,
                    s.active_organization_key,
                    s.runtime_workspace_profile_json,
                    s.status
                ORDER BY s.updated_at DESC
                LIMIT ?
                OFFSET ?
                """,
                params,
            ).fetchall()
        return [session for row in rows if (session := self._row_to_session(row)) is not None]

    def list_favorite_session_ids(self, user_id: str, session_ids: list[str]) -> set[str]:
        if not session_ids:
            return set()
        ordered_session_ids = list(dict.fromkeys(session_ids))
        placeholders = ", ".join("?" for _ in ordered_session_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT session_id
                FROM session_favorites
                WHERE user_id = ? AND session_id IN ({placeholders})
                """,
                [user_id, *ordered_session_ids],
            ).fetchall()
        return {str(row["session_id"]) for row in rows}

    def set_session_favorite(
        self,
        session_id: str,
        user_id: str,
        *,
        is_favorited: bool,
        has_agent_access: bool = False,
    ) -> bool:
        if not self.user_can_access_shared_session(session_id, user_id, has_agent_access=has_agent_access):
            raise ValueError("会话不存在或无权限访问。")
        with self._lock, self._connect() as connection:
            if is_favorited:
                connection.execute(
                    """
                    INSERT INTO session_favorites (session_id, user_id, created_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(session_id, user_id) DO NOTHING
                    """,
                    (session_id, user_id, datetime.now(timezone.utc).isoformat()),
                )
            else:
                connection.execute(
                    "DELETE FROM session_favorites WHERE session_id = ? AND user_id = ?",
                    (session_id, user_id),
                )
        return is_favorited

    def list_all_sessions(
        self,
        *,
        query: str | None = None,
        user_ids: list[str] | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AssistantSessionRecord]:
        where_clauses: list[str] = []
        params: list[object] = []

        if user_ids is not None:
            if not user_ids:
                return []
            placeholders = ", ".join("?" for _ in user_ids)
            where_clauses.append(f"s.user_id IN ({placeholders})")
            params.extend(user_ids)

        normalized_query = " ".join((query or "").split())
        if normalized_query:
            like_query = f"%{normalized_query}%"
            where_clauses.append(
                """
                (
                    s.title LIKE ?
                    OR EXISTS (
                        SELECT 1
                        FROM assistant_messages AS search_messages
                        WHERE search_messages.session_id = s.session_id
                          AND search_messages.content LIKE ?
                    )
                )
                """
            )
            params.extend([like_query, like_query])

        params.extend([limit, offset])
        where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    s.session_id,
                    s.user_id,
                    s.title,
                    s.created_at,
                    s.updated_at,
                    s.runtime_provider,
                    s.runtime_session_id,
                    s.runtime_working_directory,
                    s.active_organization_key,
                    s.runtime_workspace_profile_json,
                    s.status,
                    COUNT(m.message_id) AS message_count,
                    (
                        SELECT content
                        FROM assistant_messages
                        WHERE session_id = s.session_id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS last_message_preview
                FROM assistant_sessions AS s
                LEFT JOIN assistant_messages AS m ON m.session_id = s.session_id
                {where_sql}
                GROUP BY
                    s.session_id,
                    s.user_id,
                    s.title,
                    s.created_at,
                    s.updated_at,
                    s.runtime_provider,
                    s.runtime_session_id,
                    s.runtime_working_directory,
                    s.active_organization_key,
                    s.runtime_workspace_profile_json,
                    s.status
                ORDER BY s.updated_at DESC
                LIMIT ?
                OFFSET ?
                """,
                params,
            ).fetchall()
        return [session for row in rows if (session := self._row_to_session(row)) is not None]

    def list_messages(self, session_id: str, user_id: str) -> list[AssistantMessageRecord]:
        if not self._session_exists(session_id, user_id):
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    m.message_id,
                    m.session_id,
                    m.role,
                    m.content,
                    m.created_at,
                    f.feedback
                FROM assistant_messages AS m
                LEFT JOIN assistant_message_feedback AS f
                  ON f.message_id = m.message_id
                 AND f.user_id = ?
                WHERE m.session_id = ?
                ORDER BY m.created_at ASC
                """,
                (user_id, session_id),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def list_messages_by_session_id(self, session_id: str, user_id: str) -> list[AssistantMessageRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    m.message_id,
                    m.session_id,
                    m.role,
                    m.content,
                    m.created_at,
                    f.feedback
                FROM assistant_messages AS m
                LEFT JOIN assistant_message_feedback AS f
                  ON f.message_id = m.message_id
                 AND f.user_id = ?
                WHERE m.session_id = ?
                ORDER BY m.created_at ASC
                """,
                (user_id, session_id),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def get_session_by_id(self, session_id: str) -> AssistantSessionRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    s.session_id,
                    s.user_id,
                    s.title,
                    s.created_at,
                    s.updated_at,
                    s.runtime_provider,
                    s.runtime_session_id,
                    s.runtime_working_directory,
                    s.active_organization_key,
                    s.runtime_workspace_profile_json,
                    s.status,
                    COUNT(m.message_id) AS message_count,
                    (
                        SELECT content
                        FROM assistant_messages
                        WHERE session_id = s.session_id
                        ORDER BY created_at DESC
                        LIMIT 1
                    ) AS last_message_preview
                FROM assistant_sessions AS s
                LEFT JOIN assistant_messages AS m ON m.session_id = s.session_id
                WHERE s.session_id = ?
                GROUP BY
                    s.session_id,
                    s.user_id,
                    s.title,
                    s.created_at,
                    s.updated_at,
                    s.runtime_provider,
                    s.runtime_session_id,
                    s.runtime_working_directory,
                    s.active_organization_key,
                    s.runtime_workspace_profile_json,
                    s.status
                """,
                (session_id,),
            ).fetchone()
        return self._row_to_session(row)

    def get_message(self, message_id: str, user_id: str) -> AssistantMessageRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    m.message_id,
                    m.session_id,
                    m.role,
                    m.content,
                    m.created_at,
                    f.feedback
                FROM assistant_messages AS m
                INNER JOIN assistant_sessions AS s ON s.session_id = m.session_id
                LEFT JOIN assistant_message_feedback AS f
                  ON f.message_id = m.message_id
                 AND f.user_id = ?
                WHERE m.message_id = ? AND s.user_id = ?
                LIMIT 1
                """,
                (user_id, message_id, user_id),
            ).fetchone()
        return self._row_to_message(row) if row is not None else None

    def set_message_feedback(
        self,
        message_id: str,
        user_id: str,
        feedback: str | None,
    ) -> AssistantMessageFeedbackRecord | None:
        if feedback not in {"like", "dislike", None}:
            raise ValueError("评价类型无效。")

        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            message_row = connection.execute(
                """
                SELECT m.message_id, m.session_id, m.role
                FROM assistant_messages AS m
                INNER JOIN assistant_sessions AS s ON s.session_id = m.session_id
                WHERE m.message_id = ? AND s.user_id = ?
                LIMIT 1
                """,
                (message_id, user_id),
            ).fetchone()
            if message_row is None:
                raise ValueError("消息不存在或无权限访问。")
            if message_row["role"] != "assistant":
                raise ValueError("只能评价 AI 回复。")

            if feedback is None:
                connection.execute(
                    """
                    DELETE FROM assistant_message_feedback
                    WHERE message_id = ? AND user_id = ?
                    """,
                    (message_id, user_id),
                )
                return None

            existing_row = connection.execute(
                """
                SELECT feedback_id, created_at
                FROM assistant_message_feedback
                WHERE message_id = ? AND user_id = ?
                LIMIT 1
                """,
                (message_id, user_id),
            ).fetchone()
            feedback_id = existing_row["feedback_id"] if existing_row is not None else str(uuid4())
            created_at = datetime.fromisoformat(existing_row["created_at"]) if existing_row is not None else now

            connection.execute(
                """
                INSERT INTO assistant_message_feedback (
                    feedback_id,
                    message_id,
                    session_id,
                    user_id,
                    feedback,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(message_id, user_id) DO UPDATE SET
                    feedback = excluded.feedback,
                    updated_at = excluded.updated_at
                """,
                (
                    feedback_id,
                    message_id,
                    message_row["session_id"],
                    user_id,
                    feedback,
                    created_at.isoformat(),
                    now.isoformat(),
                ),
            )

        return AssistantMessageFeedbackRecord(
            feedback_id=feedback_id,
            message_id=message_id,
            session_id=message_row["session_id"],
            user_id=user_id,
            feedback=feedback,
            created_at=created_at,
            updated_at=now,
        )

    def append_message(self, session_id: str, user_id: str, role: str, content: str) -> AssistantMessageRecord:
        session = self.get_session(session_id, user_id)
        if session is None:
            raise ValueError("会话不存在或无权限访问。")

        message = AssistantMessageRecord(
            message_id=str(uuid4()),
            session_id=session_id,
            role=role,
            content=content,
            created_at=datetime.now(timezone.utc),
        )

        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO assistant_messages (message_id, session_id, role, content, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    message.message_id,
                    message.session_id,
                    message.role,
                    message.content,
                    message.created_at.isoformat(),
                ),
            )
            connection.execute(
                """
                UPDATE assistant_sessions
                SET updated_at = ?
                WHERE session_id = ? AND user_id = ?
                """,
                (message.created_at.isoformat(), session_id, user_id),
            )

        return message

    def update_message_content(self, message_id: str, user_id: str, content: str) -> AssistantMessageRecord | None:
        existing = self.get_message(message_id, user_id)
        if existing is None:
            return None

        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE assistant_messages
                SET content = ?
                WHERE message_id = ?
                """,
                (content, message_id),
            )

        return AssistantMessageRecord(
            message_id=existing.message_id,
            session_id=existing.session_id,
            role=existing.role,
            content=content,
            created_at=existing.created_at,
            feedback=existing.feedback,
        )

    def start_turn(
        self,
        session_id: str,
        user_id: str,
        user_message_content: str,
        git_context_requests: list[dict[str, object]] | None = None,
        git_context_snapshots: list[dict[str, object]] | None = None,
    ) -> tuple[AssistantMessageRecord, AssistantTurnRecord]:
        now = datetime.now(timezone.utc)
        user_message = AssistantMessageRecord(
            message_id=str(uuid4()),
            session_id=session_id,
            role="user",
            content=user_message_content,
            created_at=now,
        )
        turn = AssistantTurnRecord(
            turn_id=str(uuid4()),
            session_id=session_id,
            user_message_id=user_message.message_id,
            assistant_message_id=None,
            started_at=now,
            completed_at=None,
            status="running",
            error_message=None,
        )

        try:
            with self._lock, self._connect() as connection:
                session_row = connection.execute(
                    """
                    SELECT 1
                    FROM assistant_sessions
                    WHERE session_id = ? AND user_id = ?
                    LIMIT 1
                    """,
                    (session_id, user_id),
                ).fetchone()
                if session_row is None:
                    raise ValueError("会话不存在或无权限访问。")

                running_row = connection.execute(
                    """
                    SELECT 1
                    FROM assistant_turns
                    WHERE session_id = ? AND status = 'running'
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if running_row is not None:
                    raise AssistantTurnAlreadyRunningError("当前会话仍有回复正在生成，请等待上一轮完成后再发送。")

                connection.execute(
                    """
                    INSERT INTO assistant_messages (message_id, session_id, role, content, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        user_message.message_id,
                        user_message.session_id,
                        user_message.role,
                        user_message.content,
                        user_message.created_at.isoformat(),
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO assistant_turns (
                        turn_id,
                        session_id,
                        user_message_id,
                        assistant_message_id,
                        started_at,
                        completed_at,
                        status,
                        error_message
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        turn.turn_id,
                        turn.session_id,
                        turn.user_message_id,
                        turn.assistant_message_id,
                        turn.started_at.isoformat(),
                        None,
                        turn.status,
                        turn.error_message,
                    ),
                )
                self._insert_turn_git_contexts(
                    connection,
                    turn_id=turn.turn_id,
                    requests=git_context_requests or [],
                    snapshots=git_context_snapshots or [],
                )
                connection.execute(
                    """
                    UPDATE assistant_sessions
                    SET updated_at = ?
                    WHERE session_id = ? AND user_id = ?
                    """,
                    (now.isoformat(), session_id, user_id),
                )
        except sqlite3.IntegrityError as exc:
            if _is_running_turn_integrity_error(exc):
                raise AssistantTurnAlreadyRunningError("当前会话仍有回复正在生成，请等待上一轮完成后再发送。") from exc
            raise

        return user_message, turn

    def create_turn(
        self,
        session_id: str,
        user_id: str,
        user_message_id: str,
        *,
        git_context_requests: list[dict[str, object]] | None = None,
        git_context_snapshots: list[dict[str, object]] | None = None,
    ) -> AssistantTurnRecord:
        turn = AssistantTurnRecord(
            turn_id=str(uuid4()),
            session_id=session_id,
            user_message_id=user_message_id,
            assistant_message_id=None,
            started_at=datetime.now(timezone.utc),
            completed_at=None,
            status="running",
            error_message=None,
        )

        try:
            with self._lock, self._connect() as connection:
                session_row = connection.execute(
                    """
                    SELECT 1
                    FROM assistant_sessions
                    WHERE session_id = ? AND user_id = ?
                    LIMIT 1
                    """,
                    (session_id, user_id),
                ).fetchone()
                if session_row is None:
                    raise ValueError("会话不存在或无权限访问。")

                running_row = connection.execute(
                    """
                    SELECT 1
                    FROM assistant_turns
                    WHERE session_id = ? AND status = 'running'
                    LIMIT 1
                    """,
                    (session_id,),
                ).fetchone()
                if running_row is not None:
                    raise AssistantTurnAlreadyRunningError("当前会话仍有回复正在生成，请等待上一轮完成后再发送。")

                connection.execute(
                    """
                    INSERT INTO assistant_turns (
                        turn_id,
                        session_id,
                        user_message_id,
                        assistant_message_id,
                        started_at,
                        completed_at,
                        status,
                        error_message
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        turn.turn_id,
                        turn.session_id,
                        turn.user_message_id,
                        turn.assistant_message_id,
                        turn.started_at.isoformat(),
                        None,
                        turn.status,
                        turn.error_message,
                    ),
                )
                self._insert_turn_git_contexts(
                    connection,
                    turn_id=turn.turn_id,
                    requests=git_context_requests or [],
                    snapshots=git_context_snapshots or [],
                )
                connection.execute(
                    """
                    UPDATE assistant_sessions
                    SET updated_at = ?
                    WHERE session_id = ? AND user_id = ?
                    """,
                    (turn.started_at.isoformat(), session_id, user_id),
                )
        except sqlite3.IntegrityError as exc:
            if _is_running_turn_integrity_error(exc):
                raise AssistantTurnAlreadyRunningError("当前会话仍有回复正在生成，请等待上一轮完成后再发送。") from exc
            raise
        return turn

    def list_turn_git_context_requests(self, turn_id: str) -> list[TurnGitContextRequestRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM turn_git_context_requests
                WHERE turn_id = ?
                ORDER BY created_at ASC
                """,
                (turn_id,),
            ).fetchall()
        return [self._row_to_turn_git_context_request(row) for row in rows]

    def list_turn_git_context_snapshots(self, turn_id: str) -> list[TurnGitContextSnapshotRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM turn_git_context_snapshots
                WHERE turn_id = ?
                ORDER BY resolved_at ASC
                """,
                (turn_id,),
            ).fetchall()
        return [self._row_to_turn_git_context_snapshot(row) for row in rows]

    def update_running_turn_git_context(
        self,
        *,
        turn_id: str,
        user_id: str,
        request: dict[str, object],
        snapshot: dict[str, object],
    ) -> list[TurnGitContextSnapshotRecord]:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            turn_row = connection.execute(
                """
                SELECT turn.status
                FROM assistant_turns AS turn
                INNER JOIN assistant_sessions AS session ON session.session_id = turn.session_id
                WHERE turn.turn_id = ? AND session.user_id = ?
                LIMIT 1
                """,
                (turn_id, user_id),
            ).fetchone()
            if turn_row is None:
                raise ValueError("Turn 不存在或无权限访问。")
            if turn_row["status"] != "running":
                raise ValueError("只能更新运行中的 Turn Git Scope。")

            connection.execute(
                """
                INSERT INTO turn_git_context_requests (
                    request_id, turn_id, repo_full_name, strategy,
                    selector_type, selector_value, authorization_source,
                    authorization_key, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(uuid4()),
                    turn_id,
                    request["repo_full_name"],
                    request["strategy"],
                    request.get("selector_type"),
                    request.get("selector_value"),
                    request["authorization_source"],
                    request["authorization_key"],
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO turn_git_context_snapshots (
                    snapshot_id, turn_id, repo_full_name, strategy,
                    selector_type, selector_value, resolved_sha,
                    logical_code_path, logical_context_path, source, resolved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(turn_id, repo_full_name) DO UPDATE SET
                    strategy = excluded.strategy,
                    selector_type = excluded.selector_type,
                    selector_value = excluded.selector_value,
                    resolved_sha = excluded.resolved_sha,
                    logical_code_path = excluded.logical_code_path,
                    logical_context_path = excluded.logical_context_path,
                    source = excluded.source,
                    resolved_at = excluded.resolved_at
                """,
                (
                    str(uuid4()),
                    turn_id,
                    snapshot["repo_full_name"],
                    snapshot["strategy"],
                    snapshot.get("selector_type"),
                    snapshot.get("selector_value"),
                    snapshot.get("resolved_sha"),
                    snapshot["logical_code_path"],
                    snapshot.get("logical_context_path"),
                    snapshot["source"],
                    snapshot.get("resolved_at") or now,
                ),
            )
        return self.list_turn_git_context_snapshots(turn_id)

    def list_session_turn_git_context_snapshots(
        self,
        session_id: str,
    ) -> dict[str, list[TurnGitContextSnapshotRecord]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT snapshot.*
                FROM turn_git_context_snapshots AS snapshot
                INNER JOIN assistant_turns AS turn ON turn.turn_id = snapshot.turn_id
                WHERE turn.session_id = ?
                ORDER BY snapshot.resolved_at ASC
                """,
                (session_id,),
            ).fetchall()
        result: dict[str, list[TurnGitContextSnapshotRecord]] = {}
        for row in rows:
            result.setdefault(row["turn_id"], []).append(self._row_to_turn_git_context_snapshot(row))
        return result

    def get_previous_git_context_snapshot(
        self,
        *,
        session_id: str,
        repo_full_name: str,
        selector_type: str,
        selector_value: str,
    ) -> TurnGitContextSnapshotRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT snapshot.*
                FROM turn_git_context_snapshots AS snapshot
                JOIN assistant_turns AS turn ON turn.turn_id = snapshot.turn_id
                WHERE turn.session_id = ?
                  AND snapshot.repo_full_name = ?
                  AND snapshot.selector_type = ?
                  AND snapshot.selector_value = ?
                  AND snapshot.resolved_sha IS NOT NULL
                ORDER BY snapshot.resolved_at DESC
                LIMIT 1
                """,
                (session_id, repo_full_name, selector_type, selector_value),
            ).fetchone()
        return self._row_to_turn_git_context_snapshot(row) if row is not None else None

    def complete_turn(
        self,
        turn_id: str,
        *,
        assistant_message_id: str,
        status: str = "completed",
        error_message: str | None = None,
    ) -> AssistantTurnRecord:
        completed_at = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE assistant_turns
                SET assistant_message_id = ?, completed_at = ?, status = ?, error_message = ?
                WHERE turn_id = ?
                """,
                (assistant_message_id, completed_at.isoformat(), status, error_message, turn_id),
            )
        turn = self.get_turn(turn_id)
        assert turn is not None
        return turn

    def fail_turn(self, turn_id: str, error_message: str) -> AssistantTurnRecord | None:
        completed_at = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE assistant_turns
                SET completed_at = ?, status = ?, error_message = ?
                WHERE turn_id = ?
                """,
                (completed_at.isoformat(), "failed", error_message, turn_id),
            )
        return self.get_turn(turn_id)

    def get_turn(self, turn_id: str) -> AssistantTurnRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    turn_id,
                    session_id,
                    user_message_id,
                    assistant_message_id,
                    started_at,
                    completed_at,
                    status,
                    error_message
                FROM assistant_turns
                WHERE turn_id = ?
                """,
                (turn_id,),
            ).fetchone()
        return self._row_to_turn(row)

    def list_turns(self, session_id: str, user_id: str) -> list[AssistantTurnRecord]:
        if not self._session_exists(session_id, user_id):
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    turn_id,
                    session_id,
                    user_message_id,
                    assistant_message_id,
                    started_at,
                    completed_at,
                    status,
                    error_message
                FROM assistant_turns
                WHERE session_id = ?
                ORDER BY started_at ASC
                """,
                (session_id,),
            ).fetchall()
        return [turn for row in rows if (turn := self._row_to_turn(row)) is not None]

    def get_latest_turn(self, session_id: str, user_id: str) -> AssistantTurnRecord | None:
        if not self._session_exists(session_id, user_id):
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    turn_id,
                    session_id,
                    user_message_id,
                    assistant_message_id,
                    started_at,
                    completed_at,
                    status,
                    error_message
                FROM assistant_turns
                WHERE session_id = ?
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return self._row_to_turn(row)

    def get_active_turn(self, session_id: str, user_id: str) -> AssistantTurnRecord | None:
        if not self._session_exists(session_id, user_id):
            return None
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    turn_id,
                    session_id,
                    user_message_id,
                    assistant_message_id,
                    started_at,
                    completed_at,
                    status,
                    error_message
                FROM assistant_turns
                WHERE session_id = ? AND status = 'running'
                ORDER BY started_at DESC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
        return self._row_to_turn(row)

    def list_running_turn_session_ids(self, session_ids: list[str]) -> set[str]:
        if not session_ids:
            return set()
        unique_session_ids = list(dict.fromkeys(session_ids))
        placeholders = ", ".join("?" for _ in unique_session_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT session_id
                FROM assistant_turns
                WHERE status = 'running'
                  AND session_id IN ({placeholders})
                """,
                unique_session_ids,
            ).fetchall()
        return {str(row["session_id"]) for row in rows}

    def update_session_runtime(
        self,
        session_id: str,
        user_id: str,
        *,
        runtime_provider: str | None,
        runtime_session_id: str | None,
        runtime_working_directory: str | None,
        status: str,
        title: str | None = None,
        active_organization_key: str | None = None,
        runtime_workspace_profile: dict[str, object] | None = None,
    ) -> AssistantSessionRecord | None:
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            result = connection.execute(
                """
                UPDATE assistant_sessions
                SET runtime_provider = ?,
                    runtime_session_id = ?,
                    runtime_working_directory = ?,
                    title = COALESCE(?, title),
                    active_organization_key = COALESCE(?, active_organization_key),
                    runtime_workspace_profile_json = COALESCE(?, runtime_workspace_profile_json),
                    status = ?,
                    updated_at = ?
                WHERE session_id = ? AND user_id = ?
                """,
                (
                    runtime_provider,
                    runtime_session_id,
                    runtime_working_directory,
                    title,
                    active_organization_key,
                    json.dumps(runtime_workspace_profile, ensure_ascii=False, sort_keys=True)
                    if runtime_workspace_profile is not None
                    else None,
                    status,
                    now.isoformat(),
                    session_id,
                    user_id,
                ),
            )
        if result.rowcount == 0:
            return None
        return self.get_session(session_id, user_id)

    def delete_session(self, session_id: str, user_id: str) -> bool:
        with self._lock, self._connect() as connection:
            result = connection.execute(
                "DELETE FROM assistant_sessions WHERE session_id = ? AND user_id = ?",
                (session_id, user_id),
            )
        return result.rowcount > 0

    def rename_session(self, session_id: str, user_id: str, title: str) -> AssistantSessionRecord | None:
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            result = connection.execute(
                """
                UPDATE assistant_sessions
                SET title = ?, updated_at = ?
                WHERE session_id = ? AND user_id = ?
                """,
                (title, now.isoformat(), session_id, user_id),
            )
        if result.rowcount == 0:
            return None
        return self.get_session(session_id, user_id)

    def replace_session_mounts(
        self,
        session_id: str,
        user_id: str,
        mounts: list[AssistantContextInput],
    ) -> list[SessionContextMountRecord]:
        if self.get_session(session_id, user_id) is None:
            raise ValueError("会话不存在或无权限访问。")

        now = datetime.now(timezone.utc)
        records = [
            SessionContextMountRecord(
                mount_id=str(uuid4()),
                session_id=session_id,
                user_id=user_id,
                context_key=item.context_key,
                label=item.label,
                content=item.content,
                source_type=item.source_type,
                source_uri=item.source_uri,
                metadata=item.metadata,
                created_at=now,
                updated_at=now,
            )
            for item in mounts
        ]

        with self._lock, self._connect() as connection:
            connection.execute(
                "DELETE FROM session_context_mounts WHERE session_id = ? AND user_id = ?",
                (session_id, user_id),
            )
            for record in records:
                connection.execute(
                    """
                    INSERT INTO session_context_mounts (
                        mount_id,
                        session_id,
                        user_id,
                        context_key,
                        label,
                        content,
                        source_type,
                        source_uri,
                        metadata_json,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.mount_id,
                        record.session_id,
                        record.user_id,
                        record.context_key,
                        record.label,
                        record.content,
                        record.source_type,
                        record.source_uri,
                        json.dumps(record.metadata, ensure_ascii=False),
                        record.created_at.isoformat(),
                        record.updated_at.isoformat(),
                    ),
                )
        return records

    def list_session_mounts(self, session_id: str, user_id: str) -> list[SessionContextMountRecord]:
        if not self._session_exists(session_id, user_id):
            return []
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    mount_id,
                    session_id,
                    user_id,
                    context_key,
                    label,
                    content,
                    source_type,
                    source_uri,
                    metadata_json,
                    created_at,
                    updated_at
                FROM session_context_mounts
                WHERE session_id = ? AND user_id = ?
                ORDER BY created_at ASC
                """,
                (session_id, user_id),
            ).fetchall()
        return [self._row_to_session_mount(row) for row in rows]

    def record_turn_context_requests(self, turn_id: str, items: list[AssistantContextInput]) -> list[TurnContextRecord]:
        return self._replace_turn_context_entries(
            table_name="turn_context_requests",
            id_column="request_id",
            turn_id=turn_id,
            items=items,
        )

    def record_turn_context_usage(self, turn_id: str, items: list[AssistantContextInput]) -> list[TurnContextRecord]:
        return self._replace_turn_context_entries(
            table_name="turn_context_usage",
            id_column="usage_id",
            turn_id=turn_id,
            items=items,
        )

    def record_image_generation_usage(
        self,
        *,
        user_id: str,
        turn_id: str | None,
        item_id: str | None,
    ) -> bool:
        """记录一次成功落盘的 Codex 生图用量，返回是否新增。

        以 (user_id, item_id) 做幂等键：同一 codex itemId 重复落盘（重试/重放）只计一次，
        返回 False；item_id 为空时无法去重，每次都新增并返回 True。
        """
        normalized_item_id = (item_id or "").strip() or None
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO image_generation_usage (usage_id, user_id, turn_id, item_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    uuid4().hex,
                    user_id,
                    turn_id,
                    normalized_item_id,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            return cursor.rowcount > 0

    def count_image_generation_usage(self, *, user_id: str, since: datetime) -> int:
        """统计用户在 since（含）之后成功落盘的生图次数，用于额度判断。"""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS total
                FROM image_generation_usage
                WHERE user_id = ? AND created_at >= ?
                """,
                (user_id, since.isoformat()),
            ).fetchone()
        return int(row["total"]) if row is not None else 0

    def image_generation_usage_summary_by_user(
        self,
        *,
        day_start: datetime,
        week_start: datetime,
    ) -> dict[str, dict[str, int]]:
        """按用户聚合生图用量，返回 {user_id: {total, daily, weekly}}，供管理端全员视图。"""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    user_id,
                    COUNT(*) AS total,
                    SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS daily,
                    SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END) AS weekly
                FROM image_generation_usage
                GROUP BY user_id
                """,
                (day_start.isoformat(), week_start.isoformat()),
            ).fetchall()
        return {
            row["user_id"]: {
                "total": int(row["total"]),
                "daily": int(row["daily"] or 0),
                "weekly": int(row["weekly"] or 0),
            }
            for row in rows
        }

    def list_turn_context_requests(self, turn_id: str) -> list[TurnContextRecord]:
        return self._list_turn_context_entries("turn_context_requests", "request_id", turn_id)

    def list_turn_context_usage(self, turn_id: str) -> list[TurnContextRecord]:
        return self._list_turn_context_entries("turn_context_usage", "usage_id", turn_id)

    def list_context_requests_by_session_id(self, session_id: str) -> dict[str, list[TurnContextRecord]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    r.request_id AS entry_id,
                    r.turn_id,
                    r.context_key,
                    r.label,
                    r.content,
                    r.source_type,
                    r.source_uri,
                    r.metadata_json,
                    r.created_at,
                    t.user_message_id
                FROM turn_context_requests AS r
                INNER JOIN assistant_turns AS t ON t.turn_id = r.turn_id
                WHERE t.session_id = ?
                ORDER BY r.created_at ASC
                """,
                (session_id,),
            ).fetchall()
        result: dict[str, list[TurnContextRecord]] = {}
        for row in rows:
            result.setdefault(row["user_message_id"], []).append(self._row_to_turn_context(row))
        return result

    def get_uploaded_image_context_for_user(self, image_id: str, user_id: str) -> TurnContextRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    r.request_id AS entry_id,
                    r.turn_id,
                    r.context_key,
                    r.label,
                    r.content,
                    r.source_type,
                    r.source_uri,
                    r.metadata_json,
                    r.created_at
                FROM turn_context_requests AS r
                INNER JOIN assistant_turns AS t ON t.turn_id = r.turn_id
                INNER JOIN assistant_sessions AS s ON s.session_id = t.session_id
                LEFT JOIN session_shares AS sh
                  ON sh.session_id = s.session_id
                 AND sh.is_active = 1
                LEFT JOIN session_share_members AS sm
                  ON sm.share_id = sh.share_id
                 AND sm.user_id = ?
                WHERE r.context_key = ?
                  AND r.source_type = 'image'
                  AND (s.user_id = ? OR sh.share_type = 'public' OR sm.user_id IS NOT NULL)
                ORDER BY r.created_at DESC
                LIMIT 1
                """,
                (user_id, f"uploaded-image:{image_id}", user_id),
            ).fetchone()
        return self._row_to_turn_context(row) if row is not None else None

    def get_uploaded_image_context(self, image_id: str) -> TurnContextRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    r.request_id AS entry_id,
                    r.turn_id,
                    r.context_key,
                    r.label,
                    r.content,
                    r.source_type,
                    r.source_uri,
                    r.metadata_json,
                    r.created_at
                FROM turn_context_requests AS r
                WHERE r.context_key = ?
                  AND r.source_type = 'image'
                ORDER BY r.created_at DESC
                LIMIT 1
                """,
                (f"uploaded-image:{image_id}",),
            ).fetchone()
        return self._row_to_turn_context(row) if row is not None else None

    def replace_answer_citations(
        self,
        turn_id: str,
        assistant_message_id: str | None,
        citations: list[AnswerCitationRecord],
    ) -> list[AnswerCitationRecord]:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM answer_citations WHERE turn_id = ?", (turn_id,))
            for citation in citations:
                connection.execute(
                    """
                    INSERT INTO answer_citations (
                        citation_id,
                        turn_id,
                        assistant_message_id,
                        citation_type,
                        label,
                        path,
                        line_start,
                        line_end,
                        snippet,
                        metadata_json,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        citation.citation_id,
                        citation.turn_id,
                        assistant_message_id,
                        citation.citation_type,
                        citation.label,
                        citation.path,
                        citation.line_start,
                        citation.line_end,
                        citation.snippet,
                        json.dumps(citation.metadata, ensure_ascii=False),
                        citation.created_at.isoformat(),
                    ),
                )
        return self.list_answer_citations(turn_id)

    def list_citations_by_session_id(self, session_id: str) -> dict[str, list[AnswerCitationRecord]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    ac.citation_id,
                    ac.turn_id,
                    ac.assistant_message_id,
                    ac.citation_type,
                    ac.label,
                    ac.path,
                    ac.line_start,
                    ac.line_end,
                    ac.snippet,
                    ac.metadata_json,
                    ac.created_at
                FROM answer_citations AS ac
                INNER JOIN assistant_turns AS t ON t.turn_id = ac.turn_id
                WHERE t.session_id = ?
                ORDER BY ac.created_at ASC
                """,
                (session_id,),
            ).fetchall()
        result: dict[str, list[AnswerCitationRecord]] = {}
        for row in rows:
            record = self._row_to_citation(row)
            if record.assistant_message_id:
                result.setdefault(record.assistant_message_id, []).append(record)
        return result

    def list_answer_citations(self, turn_id: str) -> list[AnswerCitationRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    citation_id,
                    turn_id,
                    assistant_message_id,
                    citation_type,
                    label,
                    path,
                    line_start,
                    line_end,
                    snippet,
                    metadata_json,
                    created_at
                FROM answer_citations
                WHERE turn_id = ?
                ORDER BY created_at ASC
                """,
                (turn_id,),
            ).fetchall()
        return [self._row_to_citation(row) for row in rows]

    def append_runtime_event(
        self,
        session_id: str,
        turn_id: str,
        event_type: str,
        payload: dict[str, object],
    ) -> AssistantRuntimeEventRecord:
        return self.append_runtime_events([(session_id, turn_id, event_type, payload)])[0]

    def append_runtime_events(
        self,
        events: list[tuple[str, str, str, dict[str, object]]],
    ) -> list[AssistantRuntimeEventRecord]:
        if not events:
            return []
        now = datetime.now(timezone.utc)
        records = [
            AssistantRuntimeEventRecord(
                event_id=str(uuid4()),
                session_id=session_id,
                turn_id=turn_id,
                event_type=event_type,
                payload=payload,
                created_at=now + timedelta(microseconds=index),
            )
            for index, (session_id, turn_id, event_type, payload) in enumerate(events)
        ]
        with self._lock, self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO assistant_runtime_events (
                    event_id,
                    session_id,
                    turn_id,
                    event_type,
                    payload_json,
                    created_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        item.event_id,
                        item.session_id,
                        item.turn_id,
                        item.event_type,
                        json.dumps(item.payload, ensure_ascii=False),
                        item.created_at.isoformat(),
                    )
                    for item in records
                ],
            )
        return records

    def create_test_data_plan(self, record: TestDataPlanRecord) -> TestDataPlanRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO assistant_test_data_plans (
                    plan_id, user_id, session_id, prepared_turn_id, executed_turn_id,
                    organization_key, task_name, environment, parameters_json,
                    redacted_parameters_json, risk_level, summary, status,
                    upstream_run_id, upstream_status, result_json, error_message,
                    created_at, expires_at, executed_at, secret_json,
                    secret_revealed_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.plan_id,
                    record.user_id,
                    record.session_id,
                    record.prepared_turn_id,
                    record.executed_turn_id,
                    record.organization_key,
                    record.task_name,
                    record.environment,
                    json.dumps(record.parameters, ensure_ascii=False),
                    json.dumps(record.redacted_parameters, ensure_ascii=False),
                    record.risk_level,
                    record.summary,
                    record.status,
                    record.upstream_run_id,
                    record.upstream_status,
                    json.dumps(record.result, ensure_ascii=False) if record.result is not None else None,
                    record.error_message,
                    record.created_at.isoformat(),
                    record.expires_at.isoformat(),
                    record.executed_at.isoformat() if record.executed_at else None,
                    json.dumps(record.secret, ensure_ascii=False) if record.secret is not None else None,
                    record.secret_revealed_at.isoformat() if record.secret_revealed_at else None,
                    record.updated_at.isoformat(),
                ),
            )
        return record

    def get_doco_document_mapping(
        self,
        *,
        user_id: str,
        local_path: str,
    ) -> DocoDocumentMappingRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM assistant_doco_document_mappings
                WHERE user_id = ? AND local_path = ?
                LIMIT 1
                """,
                (user_id, local_path),
            ).fetchone()
        return self._row_to_doco_document_mapping(row) if row is not None else None

    def get_doco_credentials(self, *, user_id: str) -> DocoCredentialRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM assistant_doco_credentials
                WHERE user_id = ?
                LIMIT 1
                """,
                (user_id,),
            ).fetchone()
        return self._row_to_doco_credentials(row) if row is not None else None

    def upsert_doco_credentials(self, record: DocoCredentialRecord) -> DocoCredentialRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO assistant_doco_credentials (
                    user_id, api_token, default_knowledge_base_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    api_token = excluded.api_token,
                    default_knowledge_base_id = excluded.default_knowledge_base_id,
                    updated_at = excluded.updated_at
                """,
                (
                    record.user_id,
                    record.api_token,
                    record.default_knowledge_base_id,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
        return record

    def delete_doco_credentials(self, *, user_id: str) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM assistant_doco_credentials WHERE user_id = ?",
                (user_id,),
            )
        return cursor.rowcount > 0

    def upsert_doco_document_mapping(
        self,
        record: DocoDocumentMappingRecord,
    ) -> DocoDocumentMappingRecord:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO assistant_doco_document_mappings (
                    mapping_id, user_id, local_path, document_id, title,
                    knowledge_base_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id, local_path) DO UPDATE SET
                    document_id = excluded.document_id,
                    title = excluded.title,
                    knowledge_base_id = excluded.knowledge_base_id,
                    updated_at = excluded.updated_at
                """,
                (
                    record.mapping_id,
                    record.user_id,
                    record.local_path,
                    record.document_id,
                    record.title,
                    record.knowledge_base_id,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
        return record

    def get_test_data_plan(
        self,
        *,
        plan_id: str,
        user_id: str,
        session_id: str,
    ) -> TestDataPlanRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM assistant_test_data_plans
                WHERE plan_id = ? AND user_id = ? AND session_id = ?
                LIMIT 1
                """,
                (plan_id, user_id, session_id),
            ).fetchone()
        return self._row_to_test_data_plan(row) if row is not None else None

    def list_test_data_plans(
        self,
        *,
        user_id: str,
        session_id: str,
    ) -> list[TestDataPlanRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM assistant_test_data_plans
                WHERE user_id = ? AND session_id = ?
                ORDER BY created_at ASC
                """,
                (user_id, session_id),
            ).fetchall()
        return [self._row_to_test_data_plan(row) for row in rows]

    def claim_test_data_plan_for_execution(
        self,
        *,
        plan_id: str,
        user_id: str,
        session_id: str,
        executed_turn_id: str,
    ) -> TestDataPlanRecord | None:
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            result = connection.execute(
                """
                UPDATE assistant_test_data_plans
                SET status = 'executing', executed_turn_id = ?, executed_at = ?, updated_at = ?
                WHERE plan_id = ? AND user_id = ? AND session_id = ?
                  AND status = 'prepared' AND expires_at > ?
                """,
                (
                    executed_turn_id,
                    now.isoformat(),
                    now.isoformat(),
                    plan_id,
                    user_id,
                    session_id,
                    now.isoformat(),
                ),
            )
            if result.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM assistant_test_data_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        return self._row_to_test_data_plan(row) if row is not None else None

    def finish_test_data_plan(
        self,
        *,
        plan_id: str,
        status: str,
        upstream_run_id: str | None = None,
        upstream_status: str | None = None,
        result: dict[str, object] | None = None,
        error_message: str | None = None,
    ) -> TestDataPlanRecord:
        if status not in {"submitted", "completed", "failed", "unknown"}:
            raise ValueError("不支持的造数计划状态。")
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            updated = connection.execute(
                """
                UPDATE assistant_test_data_plans
                SET status = ?, upstream_run_id = COALESCE(?, upstream_run_id),
                    upstream_status = COALESCE(?, upstream_status), result_json = ?,
                    error_message = ?, updated_at = ?
                WHERE plan_id = ?
                """,
                (
                    status,
                    upstream_run_id,
                    upstream_status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    error_message,
                    now.isoformat(),
                    plan_id,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("造数计划不存在。")
            row = connection.execute(
                "SELECT * FROM assistant_test_data_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        assert row is not None
        return self._row_to_test_data_plan(row)

    def merge_test_data_plan_secret(
        self,
        *,
        plan_id: str,
        secret: dict[str, object],
    ) -> TestDataPlanRecord:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                "SELECT secret_json, secret_revealed_at FROM assistant_test_data_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            if row is None:
                raise ValueError("造数计划不存在。")
            if row["secret_revealed_at"]:
                raise ValueError("一次性凭据已经领取，不能再次写入。")
            merged = _json_loads(row["secret_json"]) if row["secret_json"] else {}
            merged.update(secret)
            connection.execute(
                """
                UPDATE assistant_test_data_plans
                SET secret_json = ?, updated_at = ?
                WHERE plan_id = ?
                """,
                (
                    json.dumps(merged, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                    plan_id,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM assistant_test_data_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
        assert updated is not None
        return self._row_to_test_data_plan(updated)

    def consume_test_data_plan_secret(
        self,
        *,
        plan_id: str,
        user_id: str,
        session_id: str,
    ) -> dict[str, object] | None:
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT secret_json, secret_revealed_at, status
                FROM assistant_test_data_plans
                WHERE plan_id = ? AND user_id = ? AND session_id = ?
                LIMIT 1
                """,
                (plan_id, user_id, session_id),
            ).fetchone()
            if row is None or row["status"] != "completed" or row["secret_revealed_at"] or not row["secret_json"]:
                return None
            secret = _json_loads(row["secret_json"])
            connection.execute(
                """
                UPDATE assistant_test_data_plans
                SET secret_json = NULL, secret_revealed_at = ?, updated_at = ?
                WHERE plan_id = ? AND secret_revealed_at IS NULL
                """,
                (now.isoformat(), now.isoformat(), plan_id),
            )
        return secret

    def list_runtime_events(self, turn_id: str) -> list[AssistantRuntimeEventRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    event_id,
                    session_id,
                    turn_id,
                    event_type,
                    payload_json,
                    created_at
                FROM assistant_runtime_events
                WHERE turn_id = ?
                ORDER BY created_at ASC
                """,
                (turn_id,),
            ).fetchall()
        return [self._row_to_runtime_event(row) for row in rows]

    def list_runtime_events_by_session_id(
        self, session_id: str, *, event_type: str | None = None
    ) -> list[AssistantRuntimeEventRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    event_id,
                    session_id,
                    turn_id,
                    event_type,
                    payload_json,
                    created_at
                FROM assistant_runtime_events
                WHERE session_id = ? AND (? IS NULL OR event_type = ?)
                ORDER BY created_at ASC
                """,
                (session_id, event_type, event_type),
            ).fetchall()
        return [self._row_to_runtime_event(row) for row in rows]

    def list_runtime_activity_events_by_kind(
        self,
        session_id: str,
        kind: str,
    ) -> list[AssistantRuntimeEventRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    event_id,
                    session_id,
                    turn_id,
                    event_type,
                    payload_json,
                    created_at
                FROM assistant_runtime_events
                WHERE session_id = ?
                  AND event_type = 'activity'
                  AND json_extract(payload_json, '$.kind') = ?
                ORDER BY created_at ASC
                """,
                (session_id, kind),
            ).fetchall()
        return [self._row_to_runtime_event(row) for row in rows]

    def list_runtime_activity_events_by_kinds(
        self,
        session_id: str,
        kinds: tuple[str, ...],
    ) -> list[AssistantRuntimeEventRecord]:
        if not kinds:
            return []
        placeholders = ", ".join("?" for _ in kinds)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    event_id, session_id, turn_id, event_type, payload_json, created_at
                FROM assistant_runtime_events
                WHERE session_id = ?
                  AND event_type = 'activity'
                  AND json_extract(payload_json, '$.kind') IN ({placeholders})
                ORDER BY created_at ASC
                """,
                (session_id, *kinds),
            ).fetchall()
        return [self._row_to_runtime_event(row) for row in rows]

    def upsert_file_artifact(
        self,
        *,
        session_id: str,
        message_id: str | None,
        turn_id: str | None,
        owner_user_id: str,
        display_path: str,
        source_path: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
        storage_status: str,
    ) -> AssistantFileArtifactRecord:
        if storage_status not in {"source", "snapshotted", "missing"}:
            raise ValueError("附件存储状态无效。")

        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM assistant_file_artifacts
                WHERE session_id = ?
                  AND source_path = ?
                  AND COALESCE(message_id, '') = COALESCE(?, '')
                  AND COALESCE(turn_id, '') = COALESCE(?, '')
                LIMIT 1
                """,
                (session_id, source_path, message_id, turn_id),
            ).fetchone()
            if row is None:
                artifact_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO assistant_file_artifacts (
                        artifact_id,
                        session_id,
                        message_id,
                        turn_id,
                        owner_user_id,
                        display_path,
                        source_path,
                        filename,
                        mime_type,
                        size_bytes,
                        sha256,
                        storage_status,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        artifact_id,
                        session_id,
                        message_id,
                        turn_id,
                        owner_user_id,
                        display_path,
                        source_path,
                        filename,
                        mime_type,
                        size_bytes,
                        sha256,
                        storage_status,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            else:
                artifact_id = row["artifact_id"]
                connection.execute(
                    """
                    UPDATE assistant_file_artifacts
                    SET display_path = ?,
                        filename = ?,
                        mime_type = ?,
                        size_bytes = ?,
                        sha256 = ?,
                        storage_status = ?,
                        updated_at = ?
                    WHERE artifact_id = ?
                    """,
                    (
                        display_path,
                        filename,
                        mime_type,
                        size_bytes,
                        sha256,
                        storage_status,
                        now.isoformat(),
                        artifact_id,
                    ),
                )

            artifact_row = connection.execute(
                "SELECT * FROM assistant_file_artifacts WHERE artifact_id = ?",
                (artifact_id,),
            ).fetchone()
        assert artifact_row is not None
        return self._row_to_file_artifact(artifact_row)

    def list_file_artifacts_by_session_id(self, session_id: str) -> list[AssistantFileArtifactRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM assistant_file_artifacts
                WHERE session_id = ?
                ORDER BY created_at ASC
                """,
                (session_id,),
            ).fetchall()
        return [self._row_to_file_artifact(row) for row in rows]

    def get_file_artifact_for_owner(
        self,
        session_id: str,
        artifact_id: str,
        owner_user_id: str,
    ) -> AssistantFileArtifactRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM assistant_file_artifacts
                WHERE session_id = ? AND artifact_id = ? AND owner_user_id = ?
                LIMIT 1
                """,
                (session_id, artifact_id, owner_user_id),
            ).fetchone()
        return self._row_to_file_artifact(row) if row is not None else None

    def get_file_artifact_by_id(self, artifact_id: str) -> AssistantFileArtifactRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM assistant_file_artifacts WHERE artifact_id = ? LIMIT 1",
                (artifact_id,),
            ).fetchone()
        return self._row_to_file_artifact(row) if row is not None else None

    def get_shared_file_artifact(
        self,
        share_id: str,
        artifact_id: str,
    ) -> SharedSessionFileArtifactRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM shared_session_file_artifacts
                WHERE share_id = ? AND artifact_id = ?
                LIMIT 1
                """,
                (share_id, artifact_id),
            ).fetchone()
        return self._row_to_shared_file_artifact(row) if row is not None else None

    def upsert_shared_file_artifact(
        self,
        *,
        share_id: str,
        artifact_id: str,
        snapshot_path: str,
        filename: str,
        mime_type: str,
        size_bytes: int,
        sha256: str,
    ) -> SharedSessionFileArtifactRecord:
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT shared_file_id
                FROM shared_session_file_artifacts
                WHERE share_id = ? AND artifact_id = ?
                LIMIT 1
                """,
                (share_id, artifact_id),
            ).fetchone()
            if row is None:
                shared_file_id = str(uuid4())
                connection.execute(
                    """
                    INSERT INTO shared_session_file_artifacts (
                        shared_file_id,
                        share_id,
                        artifact_id,
                        snapshot_path,
                        filename,
                        mime_type,
                        size_bytes,
                        sha256,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        shared_file_id,
                        share_id,
                        artifact_id,
                        snapshot_path,
                        filename,
                        mime_type,
                        size_bytes,
                        sha256,
                        now.isoformat(),
                    ),
                )
            else:
                shared_file_id = row["shared_file_id"]
                connection.execute(
                    """
                    UPDATE shared_session_file_artifacts
                    SET snapshot_path = ?,
                        filename = ?,
                        mime_type = ?,
                        size_bytes = ?,
                        sha256 = ?
                    WHERE shared_file_id = ?
                    """,
                    (snapshot_path, filename, mime_type, size_bytes, sha256, shared_file_id),
                )

            snapshot_row = connection.execute(
                "SELECT * FROM shared_session_file_artifacts WHERE shared_file_id = ?",
                (shared_file_id,),
            ).fetchone()
        assert snapshot_row is not None
        return self._row_to_shared_file_artifact(snapshot_row)

    def list_session_knowledge_scope_ids(self, session_id: str, user_id: str) -> list[str]:
        return self.list_sessions_knowledge_scope_ids([session_id], user_id).get(session_id, [])

    def list_sessions_knowledge_scope_ids(
        self,
        session_ids: list[str],
        user_id: str,
    ) -> dict[str, list[str]]:
        if not session_ids:
            return {}

        ordered_session_ids = list(dict.fromkeys(session_ids))
        placeholders = ", ".join("?" for _ in ordered_session_ids)

        with self._connect() as connection:
            accessible_rows = connection.execute(
                f"""
                SELECT session_id
                FROM assistant_sessions
                WHERE user_id = ?
                  AND session_id IN ({placeholders})
                """,
                [user_id, *ordered_session_ids],
            ).fetchall()
            accessible_session_ids = [row["session_id"] for row in accessible_rows]
            if not accessible_session_ids:
                return {}

            accessible_placeholders = ", ".join("?" for _ in accessible_session_ids)
            result = {session_id: [] for session_id in accessible_session_ids}
            seen_by_session = {session_id: set() for session_id in accessible_session_ids}

            def append_scope(target_session_id: str, scope_id: str | None) -> None:
                if scope_id is None:
                    return
                seen = seen_by_session[target_session_id]
                if scope_id in seen:
                    return
                seen.add(scope_id)
                result[target_session_id].append(scope_id)

            request_rows = connection.execute(
                f"""
                SELECT t.session_id, r.context_key, r.source_uri, r.metadata_json
                FROM turn_context_requests AS r
                INNER JOIN assistant_turns AS t ON t.turn_id = r.turn_id
                WHERE t.session_id IN ({accessible_placeholders})
                ORDER BY r.created_at ASC
                """,
                accessible_session_ids,
            ).fetchall()

            usage_rows = connection.execute(
                f"""
                SELECT t.session_id, u.source_uri, u.metadata_json
                FROM turn_context_usage AS u
                INNER JOIN assistant_turns AS t ON t.turn_id = u.turn_id
                WHERE t.session_id IN ({accessible_placeholders})
                ORDER BY u.created_at ASC
                """,
                accessible_session_ids,
            ).fetchall()

            citation_rows = connection.execute(
                f"""
                SELECT t.session_id, c.path
                FROM answer_citations AS c
                INNER JOIN assistant_turns AS t ON t.turn_id = c.turn_id
                WHERE t.session_id IN ({accessible_placeholders})
                ORDER BY c.created_at ASC
                """,
                accessible_session_ids,
            ).fetchall()

        for row in request_rows:
            session_id = row["session_id"]
            metadata = _json_loads(row["metadata_json"])
            append_scope(session_id, str(metadata.get("scope") or "").strip().lower() or None)
            append_scope(session_id, resolve_knowledge_scope_id_from_path(row["source_uri"]))
            append_scope(session_id, resolve_knowledge_scope_id_from_path(row["context_key"]))

        for row in usage_rows:
            session_id = row["session_id"]
            metadata = _json_loads(row["metadata_json"])
            append_scope(session_id, str(metadata.get("scope") or "").strip().lower() or None)
            append_scope(session_id, resolve_knowledge_scope_id_from_path(row["source_uri"]))

        for row in citation_rows:
            append_scope(row["session_id"], resolve_knowledge_scope_id_from_path(row["path"]))

        return result

    def upsert_session_share(
        self,
        session_id: str,
        owner_id: str,
        *,
        share_type: str,
        member_user_ids: list[str],
        visibility: str = SHARE_VISIBILITY_ANYONE,
    ) -> SessionShareRecord:
        if share_type not in {SHARE_TYPE_PUBLIC, SHARE_TYPE_MEMBERS}:
            raise ValueError("共享类型无效。")
        if visibility not in SHARE_VISIBILITY_SCOPES:
            raise ValueError("可见范围无效。")
        if self.get_session(session_id, owner_id) is None:
            raise ValueError("会话不存在或无权限访问。")

        # 指定成员模式不区分可见范围，统一记为 anyone
        normalized_visibility = visibility if share_type == SHARE_TYPE_PUBLIC else SHARE_VISIBILITY_ANYONE

        now = datetime.now(timezone.utc)
        normalized_member_ids = [
            member_user_id
            for member_user_id in list(dict.fromkeys(member_user_ids))
            if member_user_id != owner_id
        ] if share_type == SHARE_TYPE_MEMBERS else []

        with self._lock, self._connect() as connection:
            existing_row = connection.execute(
                """
                SELECT share_id, share_token, created_at
                FROM session_shares
                WHERE session_id = ? AND owner_id = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (session_id, owner_id),
            ).fetchone()

            if existing_row is None:
                share_id = str(uuid4())
                share_token = self._generate_share_token(connection)
                created_at = now
                connection.execute(
                    """
                    INSERT INTO session_shares (
                        share_id,
                        session_id,
                        owner_id,
                        share_type,
                        visibility,
                        share_token,
                        is_active,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        share_id,
                        session_id,
                        owner_id,
                        share_type,
                        normalized_visibility,
                        share_token,
                        1,
                        created_at.isoformat(),
                    ),
                )
            else:
                share_id = existing_row["share_id"]
                share_token = existing_row["share_token"]
                created_at = datetime.fromisoformat(existing_row["created_at"])
                connection.execute(
                    """
                    UPDATE session_shares
                    SET share_type = ?, visibility = ?, is_active = 1
                    WHERE share_id = ?
                    """,
                    (share_type, normalized_visibility, share_id),
                )

            connection.execute("DELETE FROM session_share_members WHERE share_id = ?", (share_id,))
            for member_user_id in normalized_member_ids:
                connection.execute(
                    """
                    INSERT INTO session_share_members (member_id, share_id, user_id, added_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (str(uuid4()), share_id, member_user_id, now.isoformat()),
                )

        return SessionShareRecord(
            share_id=share_id,
            session_id=session_id,
            owner_id=owner_id,
            share_type=share_type,
            share_token=share_token,
            is_active=True,
            created_at=created_at,
            member_user_ids=normalized_member_ids,
            visibility=normalized_visibility,
        )

    def get_active_session_share_for_owner(self, session_id: str, owner_id: str) -> SessionShareRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM session_shares
                WHERE session_id = ? AND owner_id = ? AND is_active = 1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (session_id, owner_id),
            ).fetchone()
            return self._row_to_share(row, connection)

    def revoke_session_share(self, session_id: str, owner_id: str) -> bool:
        with self._lock, self._connect() as connection:
            result = connection.execute(
                """
                UPDATE session_shares
                SET is_active = 0
                WHERE session_id = ? AND owner_id = ? AND is_active = 1
                """,
                (session_id, owner_id),
            )
            if result.rowcount > 0:
                now = datetime.now(timezone.utc).isoformat()
                connection.execute(
                    """
                    UPDATE shared_session_follow_ups
                    SET status = 'cancelled', completed_at = ?, error_message = ?
                    WHERE session_id = ? AND status = 'queued'
                    """,
                    (now, "共享已撤销，排队中的追问已取消。", session_id),
                )
        return result.rowcount > 0

    def enqueue_shared_follow_up(
        self,
        *,
        share_id: str,
        session_id: str,
        requested_by_user_id: str,
        content: str,
    ) -> SharedSessionFollowUpRecord:
        now = datetime.now(timezone.utc)
        request = SharedSessionFollowUpRecord(
            request_id=str(uuid4()),
            share_id=share_id,
            session_id=session_id,
            requested_by_user_id=requested_by_user_id,
            content=content,
            status="queued",
            turn_id=None,
            error_message=None,
            created_at=now,
            started_at=None,
            completed_at=None,
        )
        with self._lock, self._connect() as connection:
            share_row = connection.execute(
                """
                SELECT 1
                FROM session_shares
                WHERE share_id = ? AND session_id = ? AND is_active = 1
                LIMIT 1
                """,
                (share_id, session_id),
            ).fetchone()
            if share_row is None:
                raise ValueError("共享会话不存在或已撤销。")
            connection.execute(
                """
                INSERT INTO shared_session_follow_ups (
                    request_id, share_id, session_id, requested_by_user_id,
                    content, status, turn_id, error_message, created_at,
                    started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.request_id,
                    request.share_id,
                    request.session_id,
                    request.requested_by_user_id,
                    request.content,
                    request.status,
                    None,
                    None,
                    request.created_at.isoformat(),
                    None,
                    None,
                ),
            )
        return request

    def list_pending_shared_follow_ups(self, session_id: str) -> list[SharedSessionFollowUpRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM shared_session_follow_ups
                WHERE session_id = ? AND status IN ('queued', 'processing')
                ORDER BY
                    CASE status WHEN 'processing' THEN 0 ELSE 1 END,
                    created_at ASC,
                    request_id ASC
                """,
                (session_id,),
            ).fetchall()
        return [self._row_to_shared_follow_up(row) for row in rows]

    def list_recent_failed_shared_follow_ups(
        self,
        session_id: str,
        *,
        limit: int = 5,
    ) -> list[SharedSessionFollowUpRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM shared_session_follow_ups
                WHERE session_id = ? AND status = 'failed'
                ORDER BY completed_at DESC, created_at DESC
                LIMIT ?
                """,
                (session_id, limit),
            ).fetchall()
        return [self._row_to_shared_follow_up(row) for row in rows]

    def get_shared_follow_up(self, request_id: str) -> SharedSessionFollowUpRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM shared_session_follow_ups WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return self._row_to_shared_follow_up(row) if row is not None else None

    def prepare_shared_follow_up_runtime_rebuild(
        self,
        *,
        request_id: str,
        session_id: str,
        owner_id: str,
    ) -> SharedSessionFollowUpRecord | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            request_row = connection.execute(
                """
                SELECT *
                FROM shared_session_follow_ups
                WHERE request_id = ? AND session_id = ? AND status = 'failed'
                """,
                (request_id, session_id),
            ).fetchone()
            if request_row is None:
                return None

            busy_row = connection.execute(
                """
                SELECT 1
                FROM assistant_turns
                WHERE session_id = ? AND status = 'running'
                UNION ALL
                SELECT 1
                FROM shared_session_follow_ups
                WHERE session_id = ? AND status = 'processing'
                LIMIT 1
                """,
                (session_id, session_id),
            ).fetchone()
            if busy_row is not None:
                raise AssistantTurnAlreadyRunningError("当前会话仍有追问正在执行，请稍后再重建。")

            session_result = connection.execute(
                """
                UPDATE assistant_sessions
                SET runtime_session_id = NULL, updated_at = ?
                WHERE session_id = ? AND user_id = ?
                """,
                (now, session_id, owner_id),
            )
            if session_result.rowcount != 1:
                return None

            result = connection.execute(
                """
                UPDATE shared_session_follow_ups
                SET status = 'processing', turn_id = NULL, error_message = NULL,
                    started_at = ?, completed_at = NULL
                WHERE request_id = ? AND session_id = ? AND status = 'failed'
                """,
                (now, request_id, session_id),
            )
            if result.rowcount != 1:
                return None
            prepared = connection.execute(
                "SELECT * FROM shared_session_follow_ups WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return self._row_to_shared_follow_up(prepared) if prepared is not None else None

    def reconcile_processing_shared_follow_ups(self, session_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE shared_session_follow_ups
                SET
                    status = CASE turn.status WHEN 'completed' THEN 'completed' ELSE 'failed' END,
                    error_message = CASE
                        WHEN turn.status = 'completed' THEN NULL
                        ELSE COALESCE(turn.error_message, '共享追问执行失败。')
                    END,
                    completed_at = COALESCE(turn.completed_at, ?)
                FROM assistant_turns AS turn
                WHERE shared_session_follow_ups.session_id = ?
                  AND shared_session_follow_ups.status = 'processing'
                  AND shared_session_follow_ups.turn_id = turn.turn_id
                  AND turn.status IN ('completed', 'failed')
                """,
                (datetime.now(timezone.utc).isoformat(), session_id),
            )

    def claim_next_shared_follow_up(self, session_id: str) -> SharedSessionFollowUpRecord | None:
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._connect() as connection:
            running_turn = connection.execute(
                """
                SELECT 1 FROM assistant_turns
                WHERE session_id = ? AND status = 'running'
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            processing_request = connection.execute(
                """
                SELECT 1 FROM shared_session_follow_ups
                WHERE session_id = ? AND status = 'processing'
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if running_turn is not None or processing_request is not None:
                return None

            row = connection.execute(
                """
                SELECT follow_up.*
                FROM shared_session_follow_ups AS follow_up
                INNER JOIN session_shares AS share ON share.share_id = follow_up.share_id
                WHERE follow_up.session_id = ?
                  AND follow_up.status = 'queued'
                  AND share.is_active = 1
                ORDER BY follow_up.created_at ASC, follow_up.request_id ASC
                LIMIT 1
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            result = connection.execute(
                """
                UPDATE shared_session_follow_ups
                SET status = 'processing', started_at = ?, error_message = NULL
                WHERE request_id = ? AND status = 'queued'
                """,
                (now, row["request_id"]),
            )
            if result.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM shared_session_follow_ups WHERE request_id = ?",
                (row["request_id"],),
            ).fetchone()
        return self._row_to_shared_follow_up(claimed) if claimed is not None else None

    def set_shared_follow_up_turn(self, request_id: str, turn_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE shared_session_follow_ups
                SET turn_id = ?
                WHERE request_id = ? AND status = 'processing'
                """,
                (turn_id, request_id),
            )

    def complete_shared_follow_up(
        self,
        request_id: str,
        *,
        status: str,
        error_message: str | None = None,
    ) -> None:
        if status not in {"completed", "failed"}:
            raise ValueError("共享追问完成状态无效。")
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE shared_session_follow_ups
                SET status = ?, completed_at = ?, error_message = ?
                WHERE request_id = ? AND status IN ('queued', 'processing')
                """,
                (status, datetime.now(timezone.utc).isoformat(), error_message, request_id),
            )

    def requeue_shared_follow_up(self, request_id: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE shared_session_follow_ups
                SET status = 'queued', started_at = NULL, turn_id = NULL, error_message = NULL
                WHERE request_id = ? AND status = 'processing'
                """,
                (request_id,),
            )

    def list_shared_follow_up_requesters_by_turn(self, session_id: str) -> dict[str, str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT turn_id, requested_by_user_id
                FROM shared_session_follow_ups
                WHERE session_id = ? AND turn_id IS NOT NULL
                """,
                (session_id,),
            ).fetchall()
        return {str(row["turn_id"]): str(row["requested_by_user_id"]) for row in rows}

    def get_share_by_token(self, share_token: str) -> SessionShareRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM session_shares
                WHERE share_token = ? AND is_active = 1
                LIMIT 1
                """,
                (share_token,),
            ).fetchone()
            return self._row_to_share(row, connection)

    def user_can_access_shared_session(
        self,
        session_id: str,
        user_id: str | None,
        *,
        has_agent_access: bool = False,
    ) -> bool:
        session = self.get_session_by_id(session_id)
        if session is None:
            return False
        if user_id is not None and session.user_id == user_id:
            return True
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM session_shares AS sh
                LEFT JOIN session_share_members AS sm
                  ON sm.share_id = sh.share_id
                 AND sm.user_id = ?
                WHERE sh.session_id = ?
                  AND sh.is_active = 1
                  AND (
                    sm.user_id IS NOT NULL
                    OR (
                      sh.share_type = 'public'
                      AND (
                        sh.visibility = 'anyone'
                        OR (sh.visibility = 'registered' AND ?)
                        OR (sh.visibility = 'agent' AND ?)
                      )
                    )
                  )
                LIMIT 1
                """,
                (
                    user_id,
                    session_id,
                    1 if user_id is not None else 0,
                    1 if has_agent_access else 0,
                ),
            ).fetchone()
        return row is not None

    def get_shared_session_by_token(
        self,
        share_token: str,
        user_id: str | None,
        *,
        has_agent_access: bool = False,
    ) -> SharedSessionSummaryRecord | None:
        share = self.get_share_by_token(share_token)
        if share is None or not self.user_can_access_shared_session(
            share.session_id,
            user_id,
            has_agent_access=has_agent_access,
        ):
            return None
        session = self.get_session_by_id(share.session_id)
        if session is None:
            return None
        return SharedSessionSummaryRecord(
            share=share,
            session=session,
            comment_count=self.count_session_comments(session.session_id),
        )

    def list_shared_sessions(self, user_id: str) -> list[SharedSessionSummaryRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT sh.share_id
                FROM session_shares AS sh
                LEFT JOIN session_share_members AS sm
                  ON sm.share_id = sh.share_id
                 AND sm.user_id = ?
                WHERE sh.is_active = 1
                  AND (
                    sh.owner_id = ?
                    OR sh.share_type = 'public'
                    OR sm.user_id IS NOT NULL
                  )
                ORDER BY sh.created_at DESC
                """,
                (user_id, user_id),
            ).fetchall()

        summaries: list[SharedSessionSummaryRecord] = []
        for row in rows:
            with self._connect() as connection:
                share_row = connection.execute(
                    "SELECT * FROM session_shares WHERE share_id = ?",
                    (row["share_id"],),
                ).fetchone()
                share = self._row_to_share(share_row, connection)
            if share is None:
                continue
            session = self.get_session_by_id(share.session_id)
            if session is None:
                continue
            summaries.append(
                SharedSessionSummaryRecord(
                    share=share,
                    session=session,
                    comment_count=self.count_session_comments(session.session_id),
                )
            )
        return summaries

    def list_session_comments(
        self,
        session_id: str,
        *,
        message_id: str | None = None,
    ) -> list[SessionCommentRecord]:
        clauses = ["session_id = ?"]
        params: list[object] = [session_id]
        if message_id is not None:
            clauses.append("message_id = ?")
            params.append(message_id)

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT *
                FROM session_comments
                WHERE {" AND ".join(clauses)}
                ORDER BY created_at ASC
                """,
                params,
            ).fetchall()
        return [self._row_to_comment(row) for row in rows]

    def count_session_comments(self, session_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM session_comments WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return int(row["count"] if row else 0)

    def create_session_comment(
        self,
        session_id: str,
        user_id: str,
        *,
        content: str,
        message_id: str | None = None,
        has_agent_access: bool = False,
    ) -> SessionCommentRecord:
        if not self.user_can_access_shared_session(session_id, user_id, has_agent_access=has_agent_access):
            raise ValueError("会话不存在或无权限访问。")
        if message_id is not None and not self._message_belongs_to_session(message_id, session_id):
            raise ValueError("消息不存在或不属于当前会话。")

        now = datetime.now(timezone.utc)
        comment = SessionCommentRecord(
            comment_id=str(uuid4()),
            session_id=session_id,
            message_id=message_id,
            user_id=user_id,
            content=content,
            created_at=now,
            updated_at=now,
        )
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                INSERT INTO session_comments (
                    comment_id,
                    session_id,
                    message_id,
                    user_id,
                    content,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    comment.comment_id,
                    comment.session_id,
                    comment.message_id,
                    comment.user_id,
                    comment.content,
                    comment.created_at.isoformat(),
                    comment.updated_at.isoformat(),
                ),
            )
        return comment

    def update_session_comment(
        self,
        comment_id: str,
        user_id: str,
        content: str,
    ) -> SessionCommentRecord | None:
        existing = self.get_session_comment(comment_id)
        if existing is None or existing.user_id != user_id:
            return None
        now = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE session_comments
                SET content = ?, updated_at = ?
                WHERE comment_id = ? AND user_id = ?
                """,
                (content, now.isoformat(), comment_id, user_id),
            )
        return self.get_session_comment(comment_id)

    def delete_session_comment(self, comment_id: str, user_id: str) -> bool:
        with self._lock, self._connect() as connection:
            result = connection.execute(
                "DELETE FROM session_comments WHERE comment_id = ? AND user_id = ?",
                (comment_id, user_id),
            )
        return result.rowcount > 0

    def get_session_comment(self, comment_id: str) -> SessionCommentRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM session_comments WHERE comment_id = ?",
                (comment_id,),
            ).fetchone()
        return self._row_to_comment(row) if row is not None else None

    def _replace_turn_context_entries(
        self,
        *,
        table_name: str,
        id_column: str,
        turn_id: str,
        items: list[AssistantContextInput],
    ) -> list[TurnContextRecord]:
        now = datetime.now(timezone.utc)
        records = [
            TurnContextRecord(
                entry_id=str(uuid4()),
                turn_id=turn_id,
                context_key=item.context_key,
                label=item.label,
                content=item.content,
                source_type=item.source_type,
                source_uri=item.source_uri,
                metadata=item.metadata,
                created_at=now,
            )
            for item in items
        ]

        with self._lock, self._connect() as connection:
            connection.execute(f"DELETE FROM {table_name} WHERE turn_id = ?", (turn_id,))
            for record in records:
                connection.execute(
                    f"""
                    INSERT INTO {table_name} (
                        {id_column},
                        turn_id,
                        context_key,
                        label,
                        content,
                        source_type,
                        source_uri,
                        metadata_json,
                        created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.entry_id,
                        record.turn_id,
                        record.context_key,
                        record.label,
                        record.content,
                        record.source_type,
                        record.source_uri,
                        json.dumps(record.metadata, ensure_ascii=False),
                        record.created_at.isoformat(),
                    ),
                )
        return records

    def _list_turn_context_entries(
        self,
        table_name: str,
        id_column: str,
        turn_id: str,
    ) -> list[TurnContextRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    {id_column} AS entry_id,
                    turn_id,
                    context_key,
                    label,
                    content,
                    source_type,
                    source_uri,
                    metadata_json,
                    created_at
                FROM {table_name}
                WHERE turn_id = ?
                ORDER BY created_at ASC
                """,
                (turn_id,),
            ).fetchall()
        return [self._row_to_turn_context(row) for row in rows]

    def _ensure_session_columns(self, connection: sqlite3.Connection) -> None:
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(assistant_sessions)").fetchall()
        }
        column_defs = {
            "runtime_provider": "TEXT",
            "runtime_session_id": "TEXT",
            "runtime_working_directory": "TEXT",
            "active_organization_key": "TEXT",
            "runtime_workspace_profile_json": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'active'",
        }
        for column_name, definition in column_defs.items():
            if column_name in existing_columns:
                continue
            try:
                connection.execute(f"ALTER TABLE assistant_sessions ADD COLUMN {column_name} {definition}")
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise

    def _ensure_test_data_plan_columns(self, connection: sqlite3.Connection) -> None:
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(assistant_test_data_plans)").fetchall()
        }
        for column_name, definition in {
            "secret_json": "TEXT",
            "secret_revealed_at": "TEXT",
        }.items():
            if column_name not in existing_columns:
                connection.execute(
                    f"ALTER TABLE assistant_test_data_plans ADD COLUMN {column_name} {definition}"
                )

    def _ensure_session_share_visibility_column(self, connection: sqlite3.Connection) -> None:
        # 存量库迁移：分享范围拆分前只有 public/members，public 等价于 anyone。
        existing_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(session_shares)").fetchall()
        }
        if "visibility" in existing_columns:
            return
        connection.execute(
            "ALTER TABLE session_shares ADD COLUMN visibility TEXT NOT NULL DEFAULT 'anyone' "
            "CHECK(visibility IN ('agent', 'registered', 'anyone'))"
        )

    def _row_to_session(self, row: sqlite3.Row | None) -> AssistantSessionRecord | None:
        if row is None:
            return None
        keys = row.keys()
        profile_json = row["runtime_workspace_profile_json"] if "runtime_workspace_profile_json" in keys else None
        runtime_workspace_profile = None
        if profile_json:
            try:
                runtime_workspace_profile = json.loads(profile_json)
            except json.JSONDecodeError:
                runtime_workspace_profile = None
        return AssistantSessionRecord(
            session_id=row["session_id"],
            user_id=row["user_id"],
            title=row["title"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            message_count=row["message_count"] or 0,
            last_message_preview=row["last_message_preview"],
            runtime_provider=row["runtime_provider"] if "runtime_provider" in keys else None,
            runtime_session_id=row["runtime_session_id"] if "runtime_session_id" in keys else None,
            runtime_working_directory=row["runtime_working_directory"] if "runtime_working_directory" in keys else None,
            active_organization_key=row["active_organization_key"] if "active_organization_key" in keys else None,
            runtime_workspace_profile=runtime_workspace_profile,
            status=row["status"] or "active",
        )

    def _row_to_message(self, row: sqlite3.Row) -> AssistantMessageRecord:
        return AssistantMessageRecord(
            message_id=row["message_id"],
            session_id=row["session_id"],
            role=row["role"],
            content=row["content"],
            created_at=datetime.fromisoformat(row["created_at"]),
            feedback=row["feedback"] if "feedback" in row.keys() else None,
        )

    def _row_to_turn(self, row: sqlite3.Row | None) -> AssistantTurnRecord | None:
        if row is None:
            return None
        completed_at = row["completed_at"]
        return AssistantTurnRecord(
            turn_id=row["turn_id"],
            session_id=row["session_id"],
            user_message_id=row["user_message_id"],
            assistant_message_id=row["assistant_message_id"],
            started_at=datetime.fromisoformat(row["started_at"]),
            completed_at=datetime.fromisoformat(completed_at) if completed_at else None,
            status=row["status"],
            error_message=row["error_message"],
        )

    def _row_to_test_data_plan(self, row: sqlite3.Row) -> TestDataPlanRecord:
        keys = row.keys()
        return TestDataPlanRecord(
            plan_id=row["plan_id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            prepared_turn_id=row["prepared_turn_id"],
            executed_turn_id=row["executed_turn_id"],
            organization_key=row["organization_key"],
            task_name=row["task_name"],
            environment=row["environment"],
            parameters=_json_loads(row["parameters_json"]),
            redacted_parameters=_json_loads(row["redacted_parameters_json"]),
            risk_level=row["risk_level"],
            summary=row["summary"],
            status=row["status"],
            upstream_run_id=row["upstream_run_id"],
            upstream_status=row["upstream_status"],
            result=_json_loads(row["result_json"]) if row["result_json"] else None,
            error_message=row["error_message"],
            created_at=datetime.fromisoformat(row["created_at"]),
            expires_at=datetime.fromisoformat(row["expires_at"]),
            executed_at=datetime.fromisoformat(row["executed_at"]) if row["executed_at"] else None,
            updated_at=datetime.fromisoformat(row["updated_at"]),
            secret=(
                _json_loads(row["secret_json"])
                if "secret_json" in keys and row["secret_json"]
                else None
            ),
            secret_revealed_at=(
                datetime.fromisoformat(row["secret_revealed_at"])
                if "secret_revealed_at" in keys and row["secret_revealed_at"]
                else None
            ),
        )

    def _row_to_doco_document_mapping(self, row: sqlite3.Row) -> DocoDocumentMappingRecord:
        return DocoDocumentMappingRecord(
            mapping_id=row["mapping_id"],
            user_id=row["user_id"],
            local_path=row["local_path"],
            document_id=row["document_id"],
            title=row["title"],
            knowledge_base_id=row["knowledge_base_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _row_to_doco_credentials(self, row: sqlite3.Row) -> DocoCredentialRecord:
        return DocoCredentialRecord(
            user_id=row["user_id"],
            api_token=row["api_token"],
            default_knowledge_base_id=row["default_knowledge_base_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _insert_session_git_context_default(
        self,
        connection: sqlite3.Connection,
        *,
        session: AssistantSessionRecord,
        payload: dict[str, object],
    ) -> None:
        now = session.created_at.isoformat()
        connection.execute(
            """
            INSERT INTO session_git_context_defaults (
                default_id, session_id, user_id, repo_full_name,
                default_strategy, selector_type, selector_value,
                last_resolved_sha, logical_path, authorization_source,
                authorization_key, last_checked_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                session.session_id,
                session.user_id,
                payload["repo_full_name"],
                payload["default_strategy"],
                payload["selector_type"],
                payload["selector_value"],
                payload.get("last_resolved_sha"),
                payload["logical_path"],
                payload["authorization_source"],
                payload["authorization_key"],
                payload.get("last_checked_at"),
                now,
                now,
            ),
        )

    def _insert_turn_git_contexts(
        self,
        connection: sqlite3.Connection,
        *,
        turn_id: str,
        requests: list[dict[str, object]],
        snapshots: list[dict[str, object]],
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        connection.executemany(
            """
            INSERT INTO turn_git_context_requests (
                request_id, turn_id, repo_full_name, strategy,
                selector_type, selector_value, authorization_source,
                authorization_key, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(uuid4()),
                    turn_id,
                    item["repo_full_name"],
                    item["strategy"],
                    item.get("selector_type"),
                    item.get("selector_value"),
                    item["authorization_source"],
                    item["authorization_key"],
                    now,
                )
                for item in requests
            ],
        )
        connection.executemany(
            """
            INSERT INTO turn_git_context_snapshots (
                snapshot_id, turn_id, repo_full_name, strategy,
                selector_type, selector_value, resolved_sha,
                logical_code_path, logical_context_path, source, resolved_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    str(uuid4()),
                    turn_id,
                    item["repo_full_name"],
                    item["strategy"],
                    item.get("selector_type"),
                    item.get("selector_value"),
                    item.get("resolved_sha"),
                    item["logical_code_path"],
                    item.get("logical_context_path"),
                    item["source"],
                    item.get("resolved_at") or now,
                )
                for item in snapshots
            ],
        )

    def _row_to_session_git_context_default(
        self,
        row: sqlite3.Row,
    ) -> SessionGitContextDefaultRecord:
        return SessionGitContextDefaultRecord(
            default_id=row["default_id"],
            session_id=row["session_id"],
            user_id=row["user_id"],
            repo_full_name=row["repo_full_name"],
            default_strategy=row["default_strategy"],
            selector_type=row["selector_type"],
            selector_value=row["selector_value"],
            last_resolved_sha=row["last_resolved_sha"],
            logical_path=row["logical_path"],
            authorization_source=row["authorization_source"],
            authorization_key=row["authorization_key"],
            last_checked_at=datetime.fromisoformat(row["last_checked_at"]) if row["last_checked_at"] else None,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _row_to_turn_git_context_request(
        self,
        row: sqlite3.Row,
    ) -> TurnGitContextRequestRecord:
        return TurnGitContextRequestRecord(
            request_id=row["request_id"],
            turn_id=row["turn_id"],
            repo_full_name=row["repo_full_name"],
            strategy=row["strategy"],
            selector_type=row["selector_type"],
            selector_value=row["selector_value"],
            authorization_source=row["authorization_source"],
            authorization_key=row["authorization_key"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _row_to_turn_git_context_snapshot(
        self,
        row: sqlite3.Row,
    ) -> TurnGitContextSnapshotRecord:
        return TurnGitContextSnapshotRecord(
            snapshot_id=row["snapshot_id"],
            turn_id=row["turn_id"],
            repo_full_name=row["repo_full_name"],
            strategy=row["strategy"],
            selector_type=row["selector_type"],
            selector_value=row["selector_value"],
            resolved_sha=row["resolved_sha"],
            logical_code_path=row["logical_code_path"],
            logical_context_path=row["logical_context_path"],
            source=row["source"],
            resolved_at=datetime.fromisoformat(row["resolved_at"]),
        )

    def _row_to_session_mount(self, row: sqlite3.Row) -> SessionContextMountRecord:
        return SessionContextMountRecord(
            mount_id=row["mount_id"],
            session_id=row["session_id"],
            user_id=row["user_id"],
            context_key=row["context_key"],
            label=row["label"],
            content=row["content"],
            source_type=row["source_type"],
            source_uri=row["source_uri"],
            metadata=_json_loads(row["metadata_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _row_to_turn_context(self, row: sqlite3.Row) -> TurnContextRecord:
        return TurnContextRecord(
            entry_id=row["entry_id"],
            turn_id=row["turn_id"],
            context_key=row["context_key"],
            label=row["label"],
            content=row["content"],
            source_type=row["source_type"],
            source_uri=row["source_uri"],
            metadata=_json_loads(row["metadata_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _row_to_citation(self, row: sqlite3.Row) -> AnswerCitationRecord:
        return AnswerCitationRecord(
            citation_id=row["citation_id"],
            turn_id=row["turn_id"],
            assistant_message_id=row["assistant_message_id"],
            citation_type=row["citation_type"],
            label=row["label"],
            path=row["path"],
            line_start=row["line_start"],
            line_end=row["line_end"],
            snippet=row["snippet"],
            metadata=_json_loads(row["metadata_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _row_to_file_artifact(self, row: sqlite3.Row) -> AssistantFileArtifactRecord:
        return AssistantFileArtifactRecord(
            artifact_id=row["artifact_id"],
            session_id=row["session_id"],
            message_id=row["message_id"],
            turn_id=row["turn_id"],
            owner_user_id=row["owner_user_id"],
            display_path=row["display_path"],
            source_path=row["source_path"],
            filename=row["filename"],
            mime_type=row["mime_type"],
            size_bytes=int(row["size_bytes"]),
            sha256=row["sha256"],
            storage_status=row["storage_status"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _row_to_shared_file_artifact(self, row: sqlite3.Row) -> SharedSessionFileArtifactRecord:
        return SharedSessionFileArtifactRecord(
            shared_file_id=row["shared_file_id"],
            share_id=row["share_id"],
            artifact_id=row["artifact_id"],
            snapshot_path=row["snapshot_path"],
            filename=row["filename"],
            mime_type=row["mime_type"],
            size_bytes=int(row["size_bytes"]),
            sha256=row["sha256"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _row_to_runtime_event(self, row: sqlite3.Row) -> AssistantRuntimeEventRecord:
        return AssistantRuntimeEventRecord(
            event_id=row["event_id"],
            session_id=row["session_id"],
            turn_id=row["turn_id"],
            event_type=row["event_type"],
            payload=_json_loads(row["payload_json"]),
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    def _row_to_share(
        self,
        row: sqlite3.Row | None,
        connection: sqlite3.Connection,
    ) -> SessionShareRecord | None:
        if row is None:
            return None
        member_rows = connection.execute(
            """
            SELECT user_id
            FROM session_share_members
            WHERE share_id = ?
            ORDER BY added_at ASC
            """,
            (row["share_id"],),
        ).fetchall()
        return SessionShareRecord(
            share_id=row["share_id"],
            session_id=row["session_id"],
            owner_id=row["owner_id"],
            share_type=row["share_type"],
            share_token=row["share_token"],
            is_active=bool(row["is_active"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            member_user_ids=[member_row["user_id"] for member_row in member_rows],
            visibility=row["visibility"] if "visibility" in row.keys() else SHARE_VISIBILITY_ANYONE,
        )

    def _row_to_shared_follow_up(self, row: sqlite3.Row) -> SharedSessionFollowUpRecord:
        return SharedSessionFollowUpRecord(
            request_id=row["request_id"],
            share_id=row["share_id"],
            session_id=row["session_id"],
            requested_by_user_id=row["requested_by_user_id"],
            content=row["content"],
            status=row["status"],
            turn_id=row["turn_id"],
            error_message=row["error_message"],
            created_at=datetime.fromisoformat(row["created_at"]),
            started_at=datetime.fromisoformat(row["started_at"]) if row["started_at"] else None,
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None,
        )

    def _row_to_comment(self, row: sqlite3.Row) -> SessionCommentRecord:
        return SessionCommentRecord(
            comment_id=row["comment_id"],
            session_id=row["session_id"],
            message_id=row["message_id"],
            user_id=row["user_id"],
            content=row["content"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    def _generate_share_token(self, connection: sqlite3.Connection) -> str:
        while True:
            token = secrets.token_urlsafe(18)
            existing = connection.execute(
                "SELECT 1 FROM session_shares WHERE share_token = ? LIMIT 1",
                (token,),
            ).fetchone()
            if existing is None:
                return token

    def _message_belongs_to_session(self, message_id: str, session_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM assistant_messages
                WHERE message_id = ? AND session_id = ?
                LIMIT 1
                """,
                (message_id, session_id),
            ).fetchone()
        return row is not None

    def _connect(self) -> sqlite3.Connection:
        connection = getattr(self._thread_local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
            connection.row_factory = sqlite3.Row
            self._configure_connection(connection)
            self._thread_local.connection = connection
        return connection

    def _configure_connection(self, connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        connection.execute("PRAGMA synchronous = NORMAL")

    def _session_exists(self, session_id: str, user_id: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM assistant_sessions
                WHERE session_id = ? AND user_id = ?
                LIMIT 1
                """,
                (session_id, user_id),
            ).fetchone()
        return row is not None


def _json_loads(value: str) -> dict[str, object]:
    if not value:
        return {}
    loaded = json.loads(value)
    if isinstance(loaded, dict):
        return loaded
    return {"value": loaded}


def _is_running_turn_integrity_error(exc: sqlite3.IntegrityError) -> bool:
    message = str(exc)
    return "assistant_turns.session_id" in message or "idx_assistant_turns_one_running_per_session" in message
