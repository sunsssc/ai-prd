from __future__ import annotations

import subprocess
from pathlib import Path

from app.jobs.code_sync import _sync_all_repos, _sync_repo


def test_code_sync_skips_repository_on_wrong_default_branch(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "checkout", "-b", "feature")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    (repository / "README.md").write_text("test", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "init")

    result = _sync_repo(repository, default_branch="main")

    assert result.errors == 1
    assert result.repo_path == str(repository.resolve())
    assert result.old_head is None
    assert result.new_head is None
    assert result.changed_files == []
    assert _git(repository, "branch", "--show-current") == "feature"


def test_code_sync_includes_explicit_repository_outside_code_root(tmp_path: Path) -> None:
    code_root = tmp_path / "knowledge/code"
    code_root.mkdir(parents=True)
    repository = tmp_path / "server-checkouts/exchange"
    repository.mkdir(parents=True)
    _git(repository, "init")
    _git(repository, "checkout", "-b", "main")
    _git(repository, "config", "user.email", "test@example.com")
    _git(repository, "config", "user.name", "Test")
    (repository / "README.md").write_text("test", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "init")

    results, errors = _sync_all_repos(
        code_root,
        default_branches={"coinex_exchange_server": "main"},
        repository_paths={"coinex_exchange_server": repository},
    )

    assert errors == 1  # 没有 remote，证明已实际尝试同步显式路径。
    assert [result.repo_name for result in results] == ["coinex_exchange_server"]
    assert results[0].repo_path == str(repository.resolve())


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError((result.stderr or result.stdout).strip())
    return result.stdout.strip()
