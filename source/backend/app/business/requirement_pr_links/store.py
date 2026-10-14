from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from threading import Lock


@dataclass(frozen=True)
class RequirementPrLink:
    task_id: str
    repo_full_name: str
    pr_number: int
    pr_url: str
    title: str
    author_login: str
    state: str
    base_ref: str
    github_updated_at: str
    first_seen_at: str
    last_seen_at: str


class RequirementPrLinkStore:
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
                CREATE TABLE IF NOT EXISTS requirement_pr_links (
                    task_id TEXT NOT NULL,
                    repo_full_name TEXT NOT NULL,
                    pr_number INTEGER NOT NULL,
                    pr_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    author_login TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('draft', 'open', 'merged', 'closed')),
                    base_ref TEXT NOT NULL,
                    github_updated_at TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    PRIMARY KEY (task_id, repo_full_name, pr_number)
                );

                CREATE INDEX IF NOT EXISTS idx_requirement_pr_links_task
                ON requirement_pr_links(task_id);

                CREATE INDEX IF NOT EXISTS idx_requirement_pr_links_repo_state
                ON requirement_pr_links(repo_full_name, state);
                """
            )

    def sync_pr_links(self, *, repo_full_name: str, pr_number: int, links: tuple[RequirementPrLink, ...]) -> None:
        keep_task_ids = tuple(link.task_id for link in links)
        with self._lock, self._connect() as connection:
            if keep_task_ids:
                placeholders = ",".join("?" for _ in keep_task_ids)
                connection.execute(
                    f"DELETE FROM requirement_pr_links "
                    f"WHERE repo_full_name = ? AND pr_number = ? AND task_id NOT IN ({placeholders})",
                    (repo_full_name, pr_number, *keep_task_ids),
                )
            else:
                connection.execute(
                    "DELETE FROM requirement_pr_links WHERE repo_full_name = ? AND pr_number = ?",
                    (repo_full_name, pr_number),
                )
            for link in links:
                connection.execute(
                    """
                    INSERT INTO requirement_pr_links (
                        task_id, repo_full_name, pr_number, pr_url, title, author_login,
                        state, base_ref, github_updated_at, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id, repo_full_name, pr_number) DO UPDATE SET
                        pr_url = excluded.pr_url,
                        title = excluded.title,
                        author_login = excluded.author_login,
                        state = excluded.state,
                        base_ref = excluded.base_ref,
                        github_updated_at = excluded.github_updated_at,
                        last_seen_at = excluded.last_seen_at
                    """,
                    (
                        link.task_id,
                        link.repo_full_name,
                        link.pr_number,
                        link.pr_url,
                        link.title,
                        link.author_login,
                        link.state,
                        link.base_ref,
                        link.github_updated_at,
                        link.first_seen_at,
                        link.last_seen_at,
                    ),
                )

    def list_active_links(self, *, repo_full_name: str) -> tuple[RequirementPrLink, ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM requirement_pr_links WHERE repo_full_name = ? AND state IN ('draft', 'open')",
                (repo_full_name,),
            ).fetchall()
        return tuple(self._row_to_link(row) for row in rows)

    def list_for_task(self, *, task_id: str) -> tuple[RequirementPrLink, ...]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM requirement_pr_links WHERE task_id = ?",
                (task_id,),
            ).fetchall()
        return tuple(self._row_to_link(row) for row in rows)

    def get_for_task_pr(
        self,
        *,
        task_id: str,
        repo_full_name: str,
        pr_number: int,
    ) -> RequirementPrLink | None:
        with self._lock, self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM requirement_pr_links
                WHERE task_id = ? AND repo_full_name = ? AND pr_number = ?
                LIMIT 1
                """,
                (task_id, repo_full_name, pr_number),
            ).fetchone()
        return self._row_to_link(row) if row is not None else None

    def update_state(self, *, repo_full_name: str, pr_number: int, state: str, github_updated_at: str, last_seen_at: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                """
                UPDATE requirement_pr_links
                SET state = ?, github_updated_at = ?, last_seen_at = ?
                WHERE repo_full_name = ? AND pr_number = ?
                """,
                (state, github_updated_at, last_seen_at, repo_full_name, pr_number),
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=self.busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    @staticmethod
    def _row_to_link(row: sqlite3.Row) -> RequirementPrLink:
        return RequirementPrLink(
            task_id=row["task_id"],
            repo_full_name=row["repo_full_name"],
            pr_number=int(row["pr_number"]),
            pr_url=row["pr_url"],
            title=row["title"],
            author_login=row["author_login"],
            state=row["state"],
            base_ref=row["base_ref"],
            github_updated_at=row["github_updated_at"],
            first_seen_at=row["first_seen_at"],
            last_seen_at=row["last_seen_at"],
        )
