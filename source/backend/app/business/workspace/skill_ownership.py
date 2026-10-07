from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


@dataclass(frozen=True, slots=True)
class SkillOwnershipRecord:
    skill_key: str
    creator_user_id: str
    creator_name: str | None
    creator_email: str
    created_at: datetime


class SQLiteSkillOwnershipStore:
    def __init__(self, db_path: str) -> None:
        self.db_path = Path(db_path)
        self._lock = Lock()
        self._initialize()

    def _initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS skill_ownership (
                    skill_key TEXT PRIMARY KEY,
                    creator_user_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(creator_user_id) REFERENCES users(user_id)
                )
                """
            )

    def create(self, *, skill_key: str, creator_user_id: str) -> SkillOwnershipRecord:
        created_at = datetime.now(timezone.utc)
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO skill_ownership (skill_key, creator_user_id, created_at) VALUES (?, ?, ?)",
                (skill_key, creator_user_id, created_at.isoformat()),
            )
        record = self.get(skill_key)
        if record is None:
            raise RuntimeError("Skill 所有权记录创建失败。")
        return record

    def get(self, skill_key: str) -> SkillOwnershipRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT ownership.skill_key, ownership.creator_user_id, ownership.created_at,
                       users.name AS creator_name, users.email AS creator_email
                FROM skill_ownership AS ownership
                JOIN users ON users.user_id = ownership.creator_user_id
                WHERE ownership.skill_key = ?
                """,
                (skill_key,),
            ).fetchone()
        return self._row_to_record(row)

    def list_all(self) -> dict[str, SkillOwnershipRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT ownership.skill_key, ownership.creator_user_id, ownership.created_at,
                       users.name AS creator_name, users.email AS creator_email
                FROM skill_ownership AS ownership
                JOIN users ON users.user_id = ownership.creator_user_id
                ORDER BY ownership.created_at ASC
                """
            ).fetchall()
        return {
            record.skill_key: record
            for row in rows
            if (record := self._row_to_record(row)) is not None
        }

    def delete(self, skill_key: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM skill_ownership WHERE skill_key = ?", (skill_key,))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _row_to_record(row: sqlite3.Row | None) -> SkillOwnershipRecord | None:
        if row is None:
            return None
        return SkillOwnershipRecord(
            skill_key=str(row["skill_key"]),
            creator_user_id=str(row["creator_user_id"]),
            creator_name=str(row["creator_name"]) if row["creator_name"] else None,
            creator_email=str(row["creator_email"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )
