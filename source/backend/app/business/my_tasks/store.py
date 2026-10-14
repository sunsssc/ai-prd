from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from app.utils.figma import extract_figma_references


DEFAULT_RELATIONS = ("负责人", "前端", "后端", "产品", "测试")
CC_RELATION = "抄送人"
SUPPORTED_RELATIONS = (*DEFAULT_RELATIONS, CC_RELATION)
COMPLETED_LISTS = ("历史迭代",)
TERMINAL_STATUSES = ("closed", "已上线", "已拒绝")
ENVIRONMENT_PATTERN = re.compile(r"(?<![A-Za-z0-9_])(testadmin\d+|test\d+|cbi)(?![A-Za-z0-9_])", re.IGNORECASE)
CANONICAL_ENVIRONMENT_PATTERN = re.compile(r"(test\d+|cbi)", re.IGNORECASE)
_COMPLETED_SQL = """
(
    task.list_name = '历史迭代'
    OR task.status_type = 'closed'
    OR lower(trim(task.status)) IN ('closed', '已上线', '已拒绝')
)
"""


@dataclass(frozen=True)
class MyTaskRecord:
    task_id: str
    title: str
    status: str
    status_type: str
    list_name: str
    clickup_url: str
    priority: str
    updated_at: str
    updated_at_ms: int
    due_date: str
    source_path: str
    designers: tuple[str, ...]
    figma_url: str
    detected_environments: tuple[str, ...]
    indexed_at: str


@dataclass(frozen=True)
class TaskPersonRelation:
    task_id: str
    relation: str
    email: str
    display_name: str


@dataclass(frozen=True)
class MyTaskRecordPage:
    tasks: tuple[MyTaskRecord, ...]
    total: int
    page: int
    page_size: int


class MyTaskStore:
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
                CREATE TABLE IF NOT EXISTS my_task_records (
                    task_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL,
                    status_type TEXT NOT NULL,
                    list_name TEXT NOT NULL,
                    clickup_url TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_at_ms INTEGER NOT NULL,
                    due_date TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    designers TEXT NOT NULL,
                    figma_url TEXT NOT NULL,
                    detected_environments TEXT NOT NULL,
                    indexed_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS my_task_people (
                    task_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    email TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    PRIMARY KEY (task_id, relation, email),
                    FOREIGN KEY(task_id) REFERENCES my_task_records(task_id) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_my_task_people_email_relation
                ON my_task_people(email, relation);

                CREATE TABLE IF NOT EXISTS my_task_environment_overrides (
                    task_id TEXT PRIMARY KEY,
                    environment TEXT NOT NULL,
                    updated_by_email TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(task_id) REFERENCES my_task_records(task_id) ON DELETE CASCADE
                );
                """
            )
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(my_task_records)").fetchall()
            }
            if "status_type" not in columns:
                connection.execute(
                    "ALTER TABLE my_task_records ADD COLUMN status_type TEXT NOT NULL DEFAULT ''"
                )
            if "designers" not in columns:
                connection.execute(
                    "ALTER TABLE my_task_records ADD COLUMN designers TEXT NOT NULL DEFAULT '[]'"
                )
            if "figma_url" not in columns:
                connection.execute(
                    "ALTER TABLE my_task_records ADD COLUMN figma_url TEXT NOT NULL DEFAULT ''"
                )
            connection.execute(
                """
                UPDATE my_task_environment_overrides
                SET environment = 'test' || substr(environment, 10)
                WHERE environment GLOB 'testadmin[0-9]*'
                """
            )

    def upsert_clickup_task(self, *, raw_task: dict[str, Any], source_path: str) -> bool:
        task_id = str(raw_task.get("id") or "").strip()
        if not task_id:
            return False
        updated_at_ms = _integer(raw_task.get("date_updated"))
        people = _task_people(task_id, raw_task)
        designers, figma_url = _design_details(raw_task)
        detected_environments = _detected_environments(raw_task.get("comments"))
        record = MyTaskRecord(
            task_id=task_id,
            title=str(raw_task.get("name") or raw_task.get("title") or f"ClickUp Task {task_id}").strip(),
            status=_status_text(raw_task.get("status")) or "未知",
            status_type=_status_type(raw_task.get("status")),
            list_name=str(raw_task.get("_list_name") or "").strip(),
            clickup_url=str(raw_task.get("url") or f"https://app.clickup.com/t/{task_id}").strip(),
            priority=_priority_text(raw_task.get("priority")) or "未设置",
            updated_at=_format_timestamp(updated_at_ms) or "未知",
            updated_at_ms=updated_at_ms,
            due_date=_format_timestamp(_integer(raw_task.get("due_date"))) or "未设置",
            source_path=source_path,
            designers=designers,
            figma_url=figma_url,
            detected_environments=detected_environments,
            indexed_at=_utc_now(),
        )
        with self._lock, self._connect() as connection:
            existing = connection.execute(
                "SELECT updated_at_ms FROM my_task_records WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if existing is not None and int(existing["updated_at_ms"]) > updated_at_ms:
                return False
            connection.execute(
                """
                INSERT INTO my_task_records (
                    task_id, title, status, status_type, list_name, clickup_url, priority,
                    updated_at, updated_at_ms, due_date, source_path,
                    designers, figma_url, detected_environments, indexed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    title = excluded.title,
                    status = excluded.status,
                    status_type = excluded.status_type,
                    list_name = excluded.list_name,
                    clickup_url = excluded.clickup_url,
                    priority = excluded.priority,
                    updated_at = excluded.updated_at,
                    updated_at_ms = excluded.updated_at_ms,
                    due_date = excluded.due_date,
                    source_path = excluded.source_path,
                    designers = excluded.designers,
                    figma_url = excluded.figma_url,
                    detected_environments = excluded.detected_environments,
                    indexed_at = excluded.indexed_at
                """,
                (
                    record.task_id,
                    record.title,
                    record.status,
                    record.status_type,
                    record.list_name,
                    record.clickup_url,
                    record.priority,
                    record.updated_at,
                    record.updated_at_ms,
                    record.due_date,
                    record.source_path,
                    json.dumps(record.designers, ensure_ascii=False),
                    record.figma_url,
                    json.dumps(record.detected_environments, ensure_ascii=False),
                    record.indexed_at,
                ),
            )
            connection.execute("DELETE FROM my_task_people WHERE task_id = ?", (task_id,))
            connection.executemany(
                """
                INSERT INTO my_task_people (task_id, relation, email, display_name)
                VALUES (?, ?, ?, ?)
                """,
                [(item.task_id, item.relation, item.email, item.display_name) for item in people],
            )
        return True

    def list_for_email(self, *, email: str, include_cc: bool = False) -> tuple[MyTaskRecord, ...]:
        normalized_email = email.strip().lower()
        relations = SUPPORTED_RELATIONS if include_cc else DEFAULT_RELATIONS
        placeholders = ",".join("?" for _ in relations)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT task.*
                FROM my_task_records AS task
                JOIN my_task_people AS person ON person.task_id = task.task_id
                WHERE person.email = ?
                  AND person.relation IN ({placeholders})
                ORDER BY task.updated_at_ms DESC, task.task_id ASC
                """,
                (normalized_email, *relations),
            ).fetchall()
        return tuple(self._row_to_task(row) for row in rows)

    def list_page_for_email(
        self,
        *,
        email: str,
        assignment_scope: str,
        scope: str,
        include_cc: bool,
        roles: tuple[str, ...] | None,
        statuses: tuple[str, ...] | None,
        query: str,
        page: int,
        page_size: int,
    ) -> MyTaskRecordPage:
        where_sql, parameters = self._task_filter(
            email=email,
            assignment_scope=assignment_scope,
            include_cc=include_cc,
            roles=roles,
            statuses=statuses,
            query=query,
            scope=scope,
        )
        with self._connect() as connection:
            total = int(
                connection.execute(
                    f"SELECT COUNT(*) AS total FROM my_task_records AS task WHERE {where_sql}",
                    parameters,
                ).fetchone()["total"]
            )
            total_pages = (total + page_size - 1) // page_size
            resolved_page = min(page, max(total_pages, 1))
            rows = connection.execute(
                f"""
                SELECT task.*
                FROM my_task_records AS task
                WHERE {where_sql}
                ORDER BY task.updated_at_ms DESC, task.task_id ASC
                LIMIT ? OFFSET ?
                """,
                (*parameters, page_size, (resolved_page - 1) * page_size),
            ).fetchall()
        return MyTaskRecordPage(
            tasks=tuple(self._row_to_task(row) for row in rows),
            total=total,
            page=resolved_page,
            page_size=page_size,
        )

    def count_scopes_for_email(
        self,
        *,
        email: str,
        assignment_scope: str,
        include_cc: bool,
        roles: tuple[str, ...] | None,
        query: str,
    ) -> dict[str, int]:
        where_sql, parameters = self._task_filter(
            email=email,
            assignment_scope=assignment_scope,
            include_cc=include_cc,
            roles=roles,
            statuses=None,
            query=query,
            scope="all",
        )
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN {_COMPLETED_SQL} THEN 1 ELSE 0 END), 0) AS completed
                FROM my_task_records AS task
                WHERE {where_sql}
                """,
                parameters,
            ).fetchone()
        total = int(row["total"])
        completed = int(row["completed"])
        return {
            "active": total - completed,
            "completed": completed,
            "all": total,
        }

    def list_status_options_for_email(
        self,
        *,
        email: str,
        assignment_scope: str,
    ) -> tuple[tuple[str, bool], ...]:
        with self._connect() as connection:
            person_filter = ""
            parameters: tuple[object, ...] = ()
            if assignment_scope == "mine":
                person_filter = """
                WHERE EXISTS (
                    SELECT 1
                    FROM my_task_people AS person
                    WHERE person.task_id = task.task_id
                      AND person.email = ?
                )
                """
                parameters = (email.strip().lower(),)
            rows = connection.execute(
                f"""
                SELECT DISTINCT
                    task.status,
                    CASE WHEN {_COMPLETED_SQL} THEN 1 ELSE 0 END AS is_completed
                FROM my_task_records AS task
                {person_filter}
                ORDER BY task.status ASC, is_completed ASC
                """,
                parameters,
            ).fetchall()
        return tuple((str(row["status"]), bool(row["is_completed"])) for row in rows)

    def list_role_options_for_email(self, *, email: str) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT relation
                FROM my_task_people
                WHERE email = ?
                """,
                (email.strip().lower(),),
            ).fetchall()
        found = {str(row["relation"]) for row in rows}
        return tuple(role for role in SUPPORTED_RELATIONS if role in found)

    def list_relations(self, *, task_id: str, email: str) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT relation
                FROM my_task_people
                WHERE task_id = ? AND email = ?
                ORDER BY CASE relation
                    WHEN '负责人' THEN 0
                    WHEN '前端' THEN 1
                    WHEN '后端' THEN 2
                    WHEN '产品' THEN 3
                    WHEN '测试' THEN 4
                    ELSE 5
                END
                """,
                (task_id, email.strip().lower()),
            ).fetchall()
        return tuple(str(row["relation"]) for row in rows)

    def set_environment_override(self, *, task_id: str, environment: str | None, updated_by_email: str) -> None:
        with self._lock, self._connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM my_task_records WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if exists is None:
                raise FileNotFoundError("任务不存在。")
            normalized = (environment or "").strip().lower()
            if not normalized:
                connection.execute("DELETE FROM my_task_environment_overrides WHERE task_id = ?", (task_id,))
                return
            connection.execute(
                """
                INSERT INTO my_task_environment_overrides (
                    task_id, environment, updated_by_email, updated_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    environment = excluded.environment,
                    updated_by_email = excluded.updated_by_email,
                    updated_at = excluded.updated_at
                """,
                (task_id, normalized, updated_by_email.strip().lower(), _utc_now()),
            )

    def get_environment_override(self, *, task_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT environment FROM my_task_environment_overrides WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        return str(row["environment"]) if row is not None else None

    def list_detected_environments(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute("SELECT detected_environments FROM my_task_records").fetchall()
        environments: set[str] = set()
        for row in rows:
            environments.update(_parse_environments(row["detected_environments"]))
        return tuple(sorted(environments, key=_environment_sort_key))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @staticmethod
    def _task_filter(
        *,
        email: str,
        assignment_scope: str,
        include_cc: bool,
        roles: tuple[str, ...] | None,
        statuses: tuple[str, ...] | None,
        query: str,
        scope: str,
    ) -> tuple[str, tuple[object, ...]]:
        conditions: list[str] = []
        parameters: list[object] = []
        if assignment_scope == "mine":
            selected_roles = roles or (SUPPORTED_RELATIONS if include_cc else DEFAULT_RELATIONS)
            role_placeholders = ",".join("?" for _ in selected_roles)
            conditions.append(
                f"""
                EXISTS (
                    SELECT 1
                    FROM my_task_people AS person
                    WHERE person.task_id = task.task_id
                      AND person.email = ?
                      AND person.relation IN ({role_placeholders})
                )
                """
            )
            parameters.extend((email.strip().lower(), *selected_roles))

        if scope == "active":
            conditions.append(f"NOT {_COMPLETED_SQL}")
        elif scope == "completed":
            conditions.append(_COMPLETED_SQL)

        if statuses is not None:
            status_placeholders = ",".join("?" for _ in statuses)
            conditions.append(f"task.status IN ({status_placeholders})")
            parameters.extend(statuses)

        normalized_query = query.strip().lower()
        if normalized_query:
            pattern = f"%{normalized_query}%"
            conditions.append(
                """
                (
                    lower(task.title) LIKE ?
                    OR lower(task.task_id) LIKE ?
                    OR lower(task.status) LIKE ?
                    OR lower(task.list_name) LIKE ?
                    OR EXISTS (
                        SELECT 1
                        FROM requirement_pr_links AS pr
                        WHERE pr.task_id = task.task_id
                          AND (
                              lower(pr.repo_full_name) LIKE ?
                              OR CAST(pr.pr_number AS TEXT) LIKE ?
                              OR lower(pr.title) LIKE ?
                          )
                    )
                )
                """
            )
            parameters.extend((pattern,) * 7)

        return " AND ".join(conditions) or "1 = 1", tuple(parameters)

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> MyTaskRecord:
        return MyTaskRecord(
            task_id=row["task_id"],
            title=row["title"],
            status=row["status"],
            status_type=row["status_type"],
            list_name=row["list_name"],
            clickup_url=row["clickup_url"],
            priority=row["priority"],
            updated_at=row["updated_at"],
            updated_at_ms=int(row["updated_at_ms"]),
            due_date=row["due_date"],
            source_path=row["source_path"],
            designers=_parse_string_list(row["designers"]),
            figma_url=row["figma_url"],
            detected_environments=_parse_environments(row["detected_environments"]),
            indexed_at=row["indexed_at"],
        )


def is_supported_environment(value: str) -> bool:
    return CANONICAL_ENVIRONMENT_PATTERN.fullmatch(value.strip()) is not None


def normalize_environment(value: str) -> str:
    normalized = value.strip().lower()
    if normalized.startswith("testadmin"):
        return "test" + normalized.removeprefix("testadmin")
    return normalized


def is_completed_task(task: MyTaskRecord) -> bool:
    return (
        task.list_name.strip() in COMPLETED_LISTS
        or task.status_type == "closed"
        or task.status.strip().lower() in TERMINAL_STATUSES
    )


def environment_options(detected: tuple[str, ...] = ()) -> tuple[str, ...]:
    configured = {
        *(f"test{number}" for number in range(1, 11)),
        "cbi",
        *(normalize_environment(item) for item in detected),
    }
    return tuple(sorted(configured, key=_environment_sort_key))


def _task_people(task_id: str, raw_task: dict[str, Any]) -> tuple[TaskPersonRelation, ...]:
    people: dict[tuple[str, str], TaskPersonRelation] = {}
    _collect_people(people, task_id=task_id, relation="负责人", users=raw_task.get("assignees"))
    for field in raw_task.get("custom_fields") or []:
        if not isinstance(field, dict):
            continue
        relation = str(field.get("name") or "").strip()
        if relation not in SUPPORTED_RELATIONS:
            continue
        _collect_people(people, task_id=task_id, relation=relation, users=field.get("value"))
    return tuple(people.values())


def _design_details(raw_task: dict[str, Any]) -> tuple[tuple[str, ...], str]:
    designers: list[str] = []
    figma_sources: list[object] = []
    for field in raw_task.get("custom_fields") or []:
        if not isinstance(field, dict):
            continue
        name = str(field.get("name") or "").strip()
        if name == "设计":
            designers.extend(_display_names(field.get("value")))
        if "设计稿" in name:
            figma_sources.append(field.get("value"))

    figma_sources.extend(
        raw_task.get(key)
        for key in (
            "markdown_description",
            "markdownDescription",
            "description",
            "text_content",
            "textContent",
        )
    )
    for source in figma_sources:
        content = source if isinstance(source, str) else json.dumps(source, ensure_ascii=False)
        references = extract_figma_references(content)
        if references:
            return tuple(dict.fromkeys(designers)), references[0].url
    return tuple(dict.fromkeys(designers)), ""


def _display_names(value: object) -> tuple[str, ...]:
    values = value if isinstance(value, list) else [value]
    names: list[str] = []
    for item in values:
        if isinstance(item, dict):
            name = str(item.get("username") or item.get("name") or item.get("email") or "").strip()
        else:
            name = str(item or "").strip()
        if name:
            names.append(name)
    return tuple(names)


def _collect_people(
    target: dict[tuple[str, str], TaskPersonRelation],
    *,
    task_id: str,
    relation: str,
    users: object,
) -> None:
    if not isinstance(users, list):
        return
    for user in users:
        if not isinstance(user, dict):
            continue
        email = str(user.get("email") or "").strip().lower()
        if not email:
            continue
        display_name = str(user.get("username") or user.get("name") or email).strip()
        target[(relation, email)] = TaskPersonRelation(
            task_id=task_id,
            relation=relation,
            email=email,
            display_name=display_name,
        )


def _detected_environments(comments: object) -> tuple[str, ...]:
    text = json.dumps(comments or [], ensure_ascii=False)
    found = {normalize_environment(match.group(1)) for match in ENVIRONMENT_PATTERN.finditer(text)}
    return tuple(sorted(found, key=_environment_sort_key))


def _parse_environments(value: object) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    environments = {
        normalize_environment(str(item))
        for item in parsed
        if str(item).strip()
    }
    return tuple(sorted(environments, key=_environment_sort_key))


def _parse_string_list(value: object) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(str(item).strip() for item in parsed if str(item).strip())


def _environment_sort_key(value: str) -> tuple[int, int, str]:
    normalized = value.lower()
    if normalized.startswith("testadmin"):
        return 1, _integer(normalized.removeprefix("testadmin")), normalized
    if normalized.startswith("test"):
        return 0, _integer(normalized.removeprefix("test")), normalized
    return 2, 0, normalized


def _status_text(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("status") or value.get("type") or "").strip()
    return str(value or "").strip()


def _status_type(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get("type") or "").strip().lower()


def _priority_text(value: object) -> str:
    if isinstance(value, dict):
        return str(value.get("priority") or value.get("id") or "").strip()
    return str(value or "").strip()


def _integer(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _format_timestamp(milliseconds: int) -> str:
    if milliseconds <= 0:
        return ""
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
