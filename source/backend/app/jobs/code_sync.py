"""
代码仓库增量同步后台任务。

扫描 knowledge_code_root 下的所有子目录，对每个含有 .git 的目录执行 git pull。
在 FastAPI lifespan 启动时通过 asyncio.create_task 运行。
"""

import asyncio
import logging
import subprocess
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter

from app.business.git_context.repository_lock import repository_lock
from app.jobs.sync_state import write_sync_state

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepoSyncResult:
    repo_name: str
    repo_path: str
    old_head: str | None
    new_head: str | None
    changed_files: list[str]
    errors: int


def _git_head(repo_dir: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.strip()


def _git_branch(repo_dir: Path) -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout.strip()


def _git_pull(repo_dir: Path, *, repository_lock_root: Path | None = None) -> str:
    lock = (
        repository_lock(lock_root=repository_lock_root, repository_path=repo_dir)
        if repository_lock_root is not None
        else nullcontext()
    )
    with lock:
        result = subprocess.run(
            ["git", "pull"],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            timeout=120,
            check=True,
        )
    return result.stdout.strip() or result.stderr.strip()


def _git_changed_files(repo_dir: Path, old_head: str, new_head: str) -> list[str]:
    if old_head == new_head:
        return []

    result = subprocess.run(
        ["git", "diff", "--name-only", f"{old_head}..{new_head}"],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    paths = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return [f"{repo_dir.name}/{path}" for path in paths]


def _sync_repo(
    repo_dir: Path,
    *,
    default_branch: str | None = None,
    repository_lock_root: Path | None = None,
) -> RepoSyncResult:
    try:
        if default_branch:
            current_branch = _git_branch(repo_dir)
            if current_branch != default_branch:
                logger.error(
                    "代码同步跳过 [%s]：当前分支 %s 与配置默认分支 %s 不一致",
                    repo_dir.name,
                    current_branch or "(detached)",
                    default_branch,
                )
                return RepoSyncResult(
                    repo_name=repo_dir.name,
                    repo_path=str(repo_dir.resolve()),
                    old_head=None,
                    new_head=None,
                    changed_files=[],
                    errors=1,
                )
        old_head = _git_head(repo_dir)
        output = _git_pull(repo_dir, repository_lock_root=repository_lock_root)
        new_head = _git_head(repo_dir)
        changed_files = _git_changed_files(repo_dir, old_head, new_head)
        logger.info("代码同步成功 [%s]：%s", repo_dir.name, output)
        return RepoSyncResult(
            repo_name=repo_dir.name,
            repo_path=str(repo_dir.resolve()),
            old_head=old_head,
            new_head=new_head,
            changed_files=changed_files,
            errors=0,
        )
    except subprocess.TimeoutExpired:
        logger.error("代码同步超时 [%s]，已跳过", repo_dir.name)
        return RepoSyncResult(repo_name=repo_dir.name, repo_path=str(repo_dir.resolve()), old_head=None, new_head=None, changed_files=[], errors=1)
    except subprocess.CalledProcessError as exc:
        output = exc.stdout.strip() or exc.stderr.strip()
        logger.warning("代码同步失败 [%s]：%s", repo_dir.name, output)
        return RepoSyncResult(repo_name=repo_dir.name, repo_path=str(repo_dir.resolve()), old_head=None, new_head=None, changed_files=[], errors=1)
    except Exception:
        logger.exception("代码同步异常 [%s]", repo_dir.name)
        return RepoSyncResult(repo_name=repo_dir.name, repo_path=str(repo_dir.resolve()), old_head=None, new_head=None, changed_files=[], errors=1)


def _sync_all_repos(
    code_root: Path,
    *,
    default_branches: dict[str, str] | None = None,
    repository_paths: dict[str, Path] | None = None,
    repository_lock_root: Path | None = None,
) -> tuple[list[RepoSyncResult], int]:
    """同步 code_root 下的仓库，以及配置中位于该目录之外的显式仓库。"""
    if not code_root.is_dir() and not repository_paths:
        logger.warning("代码同步目录不存在，跳过：%s", code_root)
        return [], 1

    repos_by_path: dict[Path, tuple[str, Path]] = {}
    if code_root.is_dir():
        for repo in code_root.iterdir():
            if repo.is_dir() and (repo / ".git").exists():
                repos_by_path[repo.resolve()] = (repo.name, repo)
    for repo_key, repo_path in (repository_paths or {}).items():
        resolved = repo_path.resolve()
        repos_by_path[resolved] = (repo_key, resolved)
    repos = list(repos_by_path.values())
    if not repos:
        logger.info("未找到任何 git 仓库，跳过：%s", code_root)
        return [], 0

    results: list[RepoSyncResult] = []
    errors = 0
    for repo_key, repo in repos:
        result = _sync_repo(
            repo,
            default_branch=(default_branches or {}).get(repo_key),
            repository_lock_root=repository_lock_root,
        )
        if result.repo_name != repo_key:
            result = RepoSyncResult(
                repo_name=repo_key,
                repo_path=result.repo_path,
                old_head=result.old_head,
                new_head=result.new_head,
                changed_files=result.changed_files,
                errors=result.errors,
            )
        results.append(result)
        errors += result.errors

    return results, errors


async def code_sync_loop(
    code_root: Path,
    runtime_base_dir: Path,
    interval_seconds: int,
    *,
    default_branches: dict[str, str] | None = None,
    repository_paths: dict[str, Path] | None = None,
    on_repo_synced=None,
) -> None:
    """
    后台定时同步循环。在 lifespan 中通过 asyncio.create_task 启动。
    首次启动时立即执行一次，之后每隔 interval_seconds 秒执行一次。
    """
    logger.info("代码同步任务已启动，root=%s，间隔=%ds", code_root, interval_seconds)
    repository_lock_root = runtime_base_dir / "workspace/runtime/code-review-worktrees/.locks"
    while True:
        started_at = perf_counter()
        try:
            results, errors = await asyncio.to_thread(
                _sync_all_repos,
                code_root,
                default_branches=default_branches,
                repository_paths=repository_paths,
                repository_lock_root=repository_lock_root,
            )
            changed_files = sorted(path for result in results for path in result.changed_files)
            if on_repo_synced is not None:
                for result in results:
                    if result.errors == 0 and result.old_head != result.new_head and result.new_head:
                        on_repo_synced(result)
            write_sync_state(
                base_dir=runtime_base_dir,
                scope="code",
                added=0,
                updated=len(changed_files),
                changed_files=changed_files,
                errors=errors,
            )
            elapsed_ms = (perf_counter() - started_at) * 1000
            logger.info(
                "代码同步完成：耗时 %.1fms，变更 %d 个，失败 %d 个",
                elapsed_ms,
                len(changed_files),
                errors,
            )
        except Exception:
            logger.exception("代码同步出错，下次将继续重试")
        await asyncio.sleep(interval_seconds)
