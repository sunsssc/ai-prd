from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[3]


def _table_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'",
    ).fetchall()
    return {str(row[0]) for row in rows}


def test_migrate_db_moves_legacy_knowledge_and_rebuilds_workspace_foreign_keys(tmp_path: Path) -> None:
    if not shutil.which("sqlite3"):
        pytest.skip("sqlite3 CLI is required by deployment/migrate.sh")

    target_root = tmp_path / "target"
    db_path = target_root / "workspace/runtime/db/auth.sqlite3"
    db_path.parent.mkdir(parents=True)

    legacy_requirements_doc = target_root / "workspace/knowledge/requirements/docs/spec.md"
    legacy_business_doc = target_root / "workspace/knowledge/business-docs/rules/policy.md"
    legacy_code_doc = target_root / "workspace/knowledge/code/README.md"
    code_review_config = target_root / "workspace/config/code-auto-review.json"
    legacy_requirements_doc.parent.mkdir(parents=True)
    legacy_business_doc.parent.mkdir(parents=True)
    legacy_code_doc.parent.mkdir(parents=True)
    code_review_config.parent.mkdir(parents=True)
    legacy_requirements_doc.write_text("requirements", encoding="utf-8")
    legacy_business_doc.write_text("business", encoding="utf-8")
    legacy_code_doc.write_text("code", encoding="utf-8")
    code_review_config.write_text(
        '{"repositories":[{"workspace_repo_path":"workspace/knowledge/code/example_backend"}]}',
        encoding="utf-8",
    )

    for scope in ("requirements", "business-docs", "code"):
        (target_root / "workspace/coinex/knowledge" / scope).mkdir(parents=True)

    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                role TEXT NOT NULL
            );
            CREATE TABLE organizations (
                organization_id TEXT PRIMARY KEY,
                organization_key TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE user_organization_memberships (
                membership_id TEXT PRIMARY KEY
            );
            CREATE TABLE department_memberships (
                membership_id TEXT PRIMARY KEY
            );
            CREATE TABLE organization_access_requests (
                request_id TEXT PRIMARY KEY
            );
            CREATE TABLE user_workspace_profiles (
                profile_id TEXT PRIMARY KEY,
                organization_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                personal_workspace_id TEXT NOT NULL,
                personal_workspace_status TEXT NOT NULL,
                storage_quota_bytes INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(organization_id, user_id),
                FOREIGN KEY(organization_id) REFERENCES organizations(organization_id),
                FOREIGN KEY(personal_workspace_id) REFERENCES workspaces(workspace_id)
            );
            CREATE TABLE workspaces (
                workspace_id TEXT PRIMARY KEY,
                organization_id TEXT,
                workspace_key TEXT NOT NULL UNIQUE,
                workspace_type TEXT NOT NULL,
                display_name TEXT NOT NULL,
                host_path TEXT NOT NULL,
                default_mount_path TEXT NOT NULL,
                default_permission TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(organization_id) REFERENCES organizations(organization_id)
            );
            CREATE TABLE workspace_grants (
                grant_id TEXT PRIMARY KEY,
                organization_id TEXT,
                subject_type TEXT NOT NULL,
                subject_key TEXT NOT NULL,
                workspace_id TEXT NOT NULL,
                permission TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(subject_type, subject_key, workspace_id),
                FOREIGN KEY(organization_id) REFERENCES organizations(organization_id),
                FOREIGN KEY(workspace_id) REFERENCES workspaces(workspace_id)
            );
            INSERT INTO users (user_id, role) VALUES ('user_1', 'admin');
            INSERT INTO organizations (
                organization_id, organization_key, display_name, status, created_at, updated_at
            ) VALUES (
                'legacy_org', 'coinex', 'CoinEx', 'active', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
            );
            INSERT INTO workspaces (
                workspace_id, organization_id, workspace_key, workspace_type, display_name,
                host_path, default_mount_path, default_permission, status, created_at, updated_at
            ) VALUES (
                'ws_legacy', 'legacy_org', 'organization:coinex:knowledge-requirements',
                'organization_knowledge', '旧需求知识库', '/legacy/requirements',
                '/workspace/knowledge/requirements', 'read', 'active',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
            );
            INSERT INTO workspace_grants (
                grant_id, organization_id, subject_type, subject_key, workspace_id,
                permission, status, created_at, updated_at
            ) VALUES (
                'grant_legacy', 'legacy_org', 'organization', 'organization:coinex',
                'ws_legacy', 'read', 'active',
                '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00'
            );
            """
        )

    result = subprocess.run(
        ["bash", str(REPO_ROOT / "deployment/migrate.sh"), "migrate-db", "--target-root", str(target_root)],
        cwd=REPO_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr + result.stdout

    assert not (target_root / "workspace/knowledge/requirements").exists()
    assert (target_root / "workspace/coinex/knowledge/requirements/docs/spec.md").read_text(encoding="utf-8") == "requirements"
    assert (target_root / "workspace/coinex/knowledge/business-docs/rules/policy.md").read_text(encoding="utf-8") == "business"
    assert (target_root / "workspace/coinex/knowledge/code/README.md").read_text(encoding="utf-8") == "code"
    assert "workspace/coinex/knowledge/code/example_backend" in code_review_config.read_text(encoding="utf-8")

    with sqlite3.connect(db_path) as connection:
        tables = _table_names(connection)
        assert "orgs" in tables
        assert "organizations" not in tables
        assert "user_organization_memberships" not in tables
        assert "department_memberships" not in tables
        assert "organization_access_requests" not in tables
        assert "user_workspace_profiles" not in tables

        workspace_foreign_keys = connection.execute("PRAGMA foreign_key_list(workspaces)").fetchall()
        grant_foreign_keys = connection.execute("PRAGMA foreign_key_list(workspace_grants)").fetchall()
        assert any(row[2] == "orgs" and row[3] == "org_id" and row[4] == "org_id" for row in workspace_foreign_keys)
        assert any(row[2] == "orgs" and row[3] == "org_id" and row[4] == "org_id" for row in grant_foreign_keys)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

        workspaces = connection.execute(
            "SELECT workspace_key, org_id, host_path, default_sandbox_path, status FROM workspaces ORDER BY workspace_key",
        ).fetchall()
        assert workspaces == [
            (
                "org:coinex:knowledge",
                "org_coinex",
                str(target_root / "workspace/coinex/knowledge"),
                "/coinex/knowledge",
                "active",
            ),
        ]

    with sqlite3.connect(target_root / "workspace/runtime/db/assistant.sqlite3") as connection:
        tables = _table_names(connection)
        assert "session_git_context_defaults" in tables
        assert "turn_git_context_requests" in tables
        assert "turn_git_context_snapshots" in tables
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
