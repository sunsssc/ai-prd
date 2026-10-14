from __future__ import annotations

import subprocess
from pathlib import Path

from app.business.assistant.code_guard import CodeWorkspaceGuard


def _run_git(repo_dir: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _init_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True)
    _run_git(repo_dir, "init")
    (repo_dir / "tracked.txt").write_text("original\n", encoding="utf-8")
    _run_git(repo_dir, "add", "tracked.txt")
    _run_git(repo_dir, "-c", "user.email=test@example.com", "-c", "user.name=Test User", "commit", "-m", "init")


def test_code_workspace_guard_rolls_back_clean_repo_changes(tmp_path: Path) -> None:
    code_root = tmp_path / "workspace/knowledge/code"
    repo_dir = code_root / "repo-a"
    _init_repo(repo_dir)
    guard = CodeWorkspaceGuard(base_dir=tmp_path, code_root=code_root)

    snapshots = guard.snapshot()
    (repo_dir / "tracked.txt").write_text("modified\n", encoding="utf-8")
    (repo_dir / "generated.txt").write_text("temporary\n", encoding="utf-8")

    report = guard.rollback_changes(snapshots=snapshots, turn_id="turn-1")

    assert report.changed_count == 1
    assert report.rolled_back_count == 1
    assert (repo_dir / "tracked.txt").read_text(encoding="utf-8") == "original\n"
    assert (repo_dir / "generated.txt").read_text(encoding="utf-8") == "temporary\n"
    assert report.repos[0].rolled_back_files == ["tracked.txt"]
    assert report.repos[0].untracked_files == ["generated.txt"]
    assert _run_git(repo_dir, "status", "--porcelain") == "?? generated.txt\n"


def test_code_workspace_guard_skips_repo_dirty_before_turn(tmp_path: Path) -> None:
    code_root = tmp_path / "workspace/knowledge/code"
    repo_dir = code_root / "repo-a"
    _init_repo(repo_dir)
    (repo_dir / "tracked.txt").write_text("dirty before\n", encoding="utf-8")
    guard = CodeWorkspaceGuard(base_dir=tmp_path, code_root=code_root)

    snapshots = guard.snapshot()
    (repo_dir / "tracked.txt").write_text("dirty during turn\n", encoding="utf-8")

    report = guard.rollback_changes(snapshots=snapshots, turn_id="turn-1")

    assert report.changed_count == 1
    assert report.skipped_count == 1
    assert (repo_dir / "tracked.txt").read_text(encoding="utf-8") == "dirty during turn\n"


def test_code_workspace_guard_reports_untracked_without_cleaning(tmp_path: Path) -> None:
    code_root = tmp_path / "workspace/knowledge/code"
    repo_dir = code_root / "repo-a"
    _init_repo(repo_dir)
    guard = CodeWorkspaceGuard(base_dir=tmp_path, code_root=code_root)

    snapshots = guard.snapshot()
    (repo_dir / "generated.txt").write_text("temporary\n", encoding="utf-8")

    report = guard.rollback_changes(snapshots=snapshots, turn_id="turn-1")

    assert report.changed_count == 1
    assert report.rolled_back_count == 0
    assert report.repos[0].untracked_files == ["generated.txt"]
    assert (repo_dir / "generated.txt").exists()
