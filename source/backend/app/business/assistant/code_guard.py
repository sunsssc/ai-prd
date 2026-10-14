from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CodeRepoSnapshot:
    name: str
    path: Path
    head: str
    status: bytes
    fingerprint: bytes

    @property
    def was_clean(self) -> bool:
        return not self.status


@dataclass(frozen=True, slots=True)
class CodeGuardRepoReport:
    name: str
    path: str
    changed: bool
    rolled_back: bool
    skipped: bool = False
    rolled_back_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    reason: str | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class CodeGuardReport:
    repos: list[CodeGuardRepoReport]

    @property
    def changed_count(self) -> int:
        return sum(1 for repo in self.repos if repo.changed)

    @property
    def rolled_back_count(self) -> int:
        return sum(1 for repo in self.repos if repo.rolled_back)

    @property
    def skipped_count(self) -> int:
        return sum(1 for repo in self.repos if repo.skipped)

    @property
    def error_count(self) -> int:
        return sum(1 for repo in self.repos if repo.error)

    @property
    def should_notify(self) -> bool:
        return self.changed_count > 0 or self.error_count > 0

    def to_event_data(self) -> dict[str, object]:
        return {
            "message": self.message,
            "level": "warning",
            "kind": "workspace_guard",
            "changed_count": self.changed_count,
            "rolled_back_count": self.rolled_back_count,
            "skipped_count": self.skipped_count,
            "error_count": self.error_count,
            "repos": [
                {
                    "name": repo.name,
                    "path": repo.path,
                    "changed": repo.changed,
                    "rolled_back": repo.rolled_back,
                    "skipped": repo.skipped,
                    "rolled_back_files": repo.rolled_back_files,
                    "untracked_files": repo.untracked_files,
                    "reason": repo.reason,
                    "error": repo.error,
                }
                for repo in self.repos
                if repo.changed or repo.error
            ],
        }

    @property
    def message(self) -> str:
        parts: list[str] = []
        if self.rolled_back_count:
            rolled_back_files = sum(len(repo.rolled_back_files) for repo in self.repos)
            parts.append(f"检测到代码知识库被异常修改，已自动回滚 {rolled_back_files} 个 tracked 文件")
        untracked_files = sum(len(repo.untracked_files) for repo in self.repos)
        if untracked_files:
            parts.append(f"发现 {untracked_files} 个 untracked 文件，第一阶段仅提示未自动清理")
        if self.skipped_count:
            parts.append(f"{self.skipped_count} 个代码仓在本轮开始前已有未提交改动，已跳过自动回滚")
        if self.error_count:
            parts.append(f"{self.error_count} 个代码仓回滚检查失败")
        return "；".join(parts) + "。" if parts else "代码知识库未发现本轮改动。"


class CodeWorkspaceGuard:
    """保护组织代码知识库不被 Agent 持久修改。"""

    def __init__(
        self,
        *,
        base_dir: Path,
        code_root: Path,
    ) -> None:
        self._base_dir = base_dir.resolve()
        self._code_root = code_root.resolve()

    def snapshot(self) -> list[CodeRepoSnapshot]:
        snapshots: list[CodeRepoSnapshot] = []
        for repo_dir in self._list_git_repos():
            try:
                snapshots.append(
                    CodeRepoSnapshot(
                        name=repo_dir.name,
                        path=repo_dir,
                        head=self._git_stdout(repo_dir, "rev-parse", "HEAD").decode("utf-8").strip(),
                        status=self._git_stdout(repo_dir, "status", "--porcelain=v1", "-z"),
                        fingerprint=self._repo_fingerprint(repo_dir),
                    )
                )
            except Exception:
                logger.exception("代码保护快照失败：%s", repo_dir)
        return snapshots

    def rollback_changes(self, *, snapshots: list[CodeRepoSnapshot], turn_id: str) -> CodeGuardReport:
        reports: list[CodeGuardRepoReport] = []
        for snapshot in snapshots:
            reports.append(self._rollback_repo(snapshot, turn_id=turn_id))
        return CodeGuardReport(repos=reports)

    def _list_git_repos(self) -> list[Path]:
        if not self._code_root.is_dir():
            return []
        return sorted(
            [
                child.resolve()
                for child in self._code_root.iterdir()
                if child.is_dir() and (child / ".git").exists()
            ],
            key=lambda path: path.name,
        )

    def _rollback_repo(self, snapshot: CodeRepoSnapshot, *, turn_id: str) -> CodeGuardRepoReport:
        repo_dir = snapshot.path
        path = self._display_path(repo_dir)
        if not repo_dir.is_dir() or not (repo_dir / ".git").exists():
            return CodeGuardRepoReport(
                name=snapshot.name,
                path=path,
                changed=True,
                rolled_back=False,
                error="代码仓目录或 .git 已不存在，无法自动回滚。",
            )

        try:
            current_head = self._git_stdout(repo_dir, "rev-parse", "HEAD").decode("utf-8").strip()
            current_status = self._git_stdout(repo_dir, "status", "--porcelain=v1", "-z")
            current_fingerprint = self._repo_fingerprint(repo_dir)
        except Exception as exc:
            return CodeGuardRepoReport(
                name=snapshot.name,
                path=path,
                changed=True,
                rolled_back=False,
                error=f"读取代码仓状态失败：{exc}",
            )

        changed = current_head != snapshot.head or current_status != snapshot.status or current_fingerprint != snapshot.fingerprint
        if not changed:
            return CodeGuardRepoReport(name=snapshot.name, path=path, changed=False, rolled_back=False)

        untracked_files = self._untracked_files(repo_dir)
        tracked_dirty_files = self._tracked_dirty_files(repo_dir)

        if not snapshot.was_clean:
            return CodeGuardRepoReport(
                name=snapshot.name,
                path=path,
                changed=True,
                rolled_back=False,
                skipped=True,
                untracked_files=untracked_files,
                reason="本轮开始前已有未提交改动，为避免误删未自动回滚。",
            )

        if current_head != snapshot.head:
            return CodeGuardRepoReport(
                name=snapshot.name,
                path=path,
                changed=True,
                rolled_back=False,
                untracked_files=untracked_files,
                reason="本轮执行后 HEAD 发生变化，已记录高风险事件，请人工确认代码知识库状态。",
            )

        if not tracked_dirty_files:
            return CodeGuardRepoReport(
                name=snapshot.name,
                path=path,
                changed=True,
                rolled_back=False,
                untracked_files=untracked_files,
                reason="发现新增 untracked 文件，第一阶段仅提示，不自动清理。",
            )

        try:
            del turn_id
            self._git_stdout(repo_dir, "reset", "--hard", snapshot.head)
            return CodeGuardRepoReport(
                name=snapshot.name,
                path=path,
                changed=True,
                rolled_back=True,
                rolled_back_files=tracked_dirty_files,
                untracked_files=untracked_files,
            )
        except Exception as exc:
            logger.exception("代码仓自动回滚失败：%s", repo_dir)
            return CodeGuardRepoReport(
                name=snapshot.name,
                path=path,
                changed=True,
                rolled_back=False,
                error=f"自动回滚失败：{exc}",
            )

    def _untracked_files(self, repo_dir: Path) -> list[str]:
        output = self._git_stdout(repo_dir, "ls-files", "--others", "--exclude-standard", "-z")
        return sorted(part.decode("utf-8", errors="replace") for part in output.split(b"\0") if part)

    def _tracked_dirty_files(self, repo_dir: Path) -> list[str]:
        output = self._git_stdout(repo_dir, "diff", "--name-only", "-z", "HEAD")
        return sorted(part.decode("utf-8", errors="replace") for part in output.split(b"\0") if part)

    def _git_stdout(self, repo_dir: Path, *args: str) -> bytes:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_dir,
            capture_output=True,
            timeout=30,
            check=True,
        )
        return result.stdout

    def _repo_fingerprint(self, repo_dir: Path) -> bytes:
        untracked = self._git_stdout(repo_dir, "ls-files", "--others", "--exclude-standard", "-z")
        diff = self._git_stdout(repo_dir, "diff", "--binary", "HEAD")
        return b"\0".join([self._git_stdout(repo_dir, "status", "--porcelain=v1", "-z"), untracked, diff])

    def _display_path(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self._base_dir).as_posix()
        except ValueError:
            return str(path.resolve())
