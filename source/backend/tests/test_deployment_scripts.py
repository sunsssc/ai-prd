from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]
DEPLOY_SCRIPT = REPO_ROOT / "deployment/deploy-from-github.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_deploy_from_github_skips_when_local_head_is_ahead(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    remote = tmp_path / "remote.git"
    deployment_dir = repo / "deployment"
    (repo / ".venv/bin").mkdir(parents=True)
    deployment_dir.mkdir(parents=True)
    (repo / ".venv/bin/activate").touch()
    (deployment_dir / "deploy.sh").write_text(
        "#!/usr/bin/env bash\ntouch deploy-was-called\n",
        encoding="utf-8",
    )
    (deployment_dir / "deploy.sh").chmod(0o755)
    (deployment_dir / "deploy-from-github.sh").write_bytes(DEPLOY_SCRIPT.read_bytes())
    (deployment_dir / "deploy-from-github.sh").chmod(0o755)

    _git(repo, "init", "--initial-branch=main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "version.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "version.txt")
    _git(repo, "commit", "-m", "base")
    _git(repo, "init", "--bare", "--initial-branch=main", str(remote))
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")

    (repo / "version.txt").write_text("local-only\n", encoding="utf-8")
    _git(repo, "commit", "-am", "local commit")

    result = subprocess.run(
        ["bash", str(deployment_dir / "deploy-from-github.sh")],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )

    assert "本地版本可能领先或已分叉" in result.stdout
    assert not (repo / "deploy-was-called").exists()
