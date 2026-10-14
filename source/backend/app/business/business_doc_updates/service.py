from __future__ import annotations

import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PullRequestFile:
    filename: str
    status: str
    patch: str


@dataclass(frozen=True)
class PullRequestReviewRequestEvent:
    event_id: str
    actor_login: str
    reviewer_login: str
    team_slug: str
    created_at: str


@dataclass(frozen=True)
class PullRequestCommit:
    author_login: str
    author_email: str


@dataclass(frozen=True)
class PullRequestSnapshot:
    repo_full_name: str
    pr_number: int
    title: str
    body: str
    base_ref: str
    base_sha: str
    head_sha: str
    draft: bool
    files: tuple[PullRequestFile, ...]
    state: str = "open"
    author_login: str = ""
    html_url: str = ""
    requested_reviewer_logins: tuple[str, ...] = ()
    requested_team_slugs: tuple[str, ...] = ()
    updated_at: str = ""
    merged_at: str = ""

    @property
    def diff_text(self) -> str:
        sections: list[str] = []
        for file in self.files:
            sections.append(f"diff -- {file.filename} ({file.status})")
            if file.patch:
                sections.append(file.patch)
        return "\n".join(sections)


class GitHubPullRequestClient:
    def __init__(self, *, token: str = "", api_base_url: str = "https://api.github.com") -> None:
        self.token = token.strip()
        self.api_base_url = api_base_url.rstrip("/")

    def list_open_pull_requests(self, *, repo_full_name: str) -> tuple[PullRequestSnapshot, ...]:
        snapshots: list[PullRequestSnapshot] = []
        page = 1
        while True:
            payload = self._request_json(f"/repos/{repo_full_name}/pulls?state=open&per_page=100&page={page}")
            if not isinstance(payload, list) or not payload:
                break
            snapshots.extend(
                _snapshot_from_payload(repo_full_name, item, ())
                for item in payload
                if isinstance(item, dict) and int(item.get("number") or 0) > 0
            )
            if len(payload) < 100:
                break
            page += 1
        return tuple(snapshots)

    def fetch_pull_request(self, *, repo_full_name: str, pr_number: int) -> PullRequestSnapshot:
        payload = self._request_json(f"/repos/{repo_full_name}/pulls/{pr_number}")
        if not isinstance(payload, dict):
            raise RuntimeError("GitHub PR 响应格式无效。")
        files = self.fetch_pull_request_files(repo_full_name=repo_full_name, pr_number=pr_number)
        return _snapshot_from_payload(repo_full_name, payload, files)

    def fetch_pull_request_files(self, *, repo_full_name: str, pr_number: int) -> tuple[PullRequestFile, ...]:
        return self._paged_files(lambda page: self._request_json(f"/repos/{repo_full_name}/pulls/{pr_number}/files?per_page=100&page={page}"))

    def fetch_pull_request_commits(self, *, repo_full_name: str, pr_number: int) -> tuple[PullRequestCommit, ...]:
        return self._paged_commits(lambda page: self._request_json(f"/repos/{repo_full_name}/pulls/{pr_number}/commits?per_page=100&page={page}"))

    def fetch_pull_request_timeline(self, *, repo_full_name: str, pr_number: int) -> tuple[PullRequestReviewRequestEvent, ...]:
        return self._paged_timeline(lambda page: self._request_json(f"/repos/{repo_full_name}/issues/{pr_number}/timeline?per_page=100&page={page}"))

    def _request_json(self, path: str) -> object:
        request = urllib.request.Request(f"{self.api_base_url}{path}")
        request.add_header("Accept", "application/vnd.github+json")
        request.add_header("X-GitHub-Api-Version", "2022-11-28")
        request.add_header("User-Agent", "ai-prd-code-review")
        if self.token:
            request.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"GitHub API 请求失败：HTTP {exc.code} {detail[:500]}") from exc

    @staticmethod
    def _paged_files(fetch_page) -> tuple[PullRequestFile, ...]:
        files: list[PullRequestFile] = []
        page = 1
        while True:
            payload = fetch_page(page)
            if not isinstance(payload, list) or not payload:
                break
            files.extend(_files_from_payload(payload))
            if len(payload) < 100:
                break
            page += 1
        return tuple(files)

    @staticmethod
    def _paged_commits(fetch_page) -> tuple[PullRequestCommit, ...]:
        commits: list[PullRequestCommit] = []
        page = 1
        while True:
            payload = fetch_page(page)
            if not isinstance(payload, list) or not payload:
                break
            commits.extend(_commits_from_payload(payload))
            if len(payload) < 100:
                break
            page += 1
        return tuple(commits)

    @staticmethod
    def _paged_timeline(fetch_page) -> tuple[PullRequestReviewRequestEvent, ...]:
        events: list[PullRequestReviewRequestEvent] = []
        page = 1
        while True:
            payload = fetch_page(page)
            if not isinstance(payload, list) or not payload:
                break
            events.extend(_timeline_from_payload(payload))
            if len(payload) < 100:
                break
            page += 1
        return tuple(events)


class GitHubCliPullRequestClient(GitHubPullRequestClient):
    hostname = "github.com"

    def __init__(self, *, repo_full_name: str, working_directory: Path, account: str, cli_path: str = "gh") -> None:
        super().__init__()
        self.repo_full_name = repo_full_name.strip()
        self.working_directory = working_directory.resolve()
        self.account = account.strip()
        self.cli_path = cli_path.strip() or "gh"
        self._token: str | None = None

    def list_open_pull_requests(self, *, repo_full_name: str) -> tuple[PullRequestSnapshot, ...]:
        self._ensure_repo(repo_full_name)
        return super().list_open_pull_requests(repo_full_name=repo_full_name)

    def fetch_pull_request(self, *, repo_full_name: str, pr_number: int) -> PullRequestSnapshot:
        self._ensure_repo(repo_full_name)
        return super().fetch_pull_request(repo_full_name=repo_full_name, pr_number=pr_number)

    def fetch_pull_request_files(self, *, repo_full_name: str, pr_number: int) -> tuple[PullRequestFile, ...]:
        self._ensure_repo(repo_full_name)
        return super().fetch_pull_request_files(repo_full_name=repo_full_name, pr_number=pr_number)

    def fetch_pull_request_commits(self, *, repo_full_name: str, pr_number: int) -> tuple[PullRequestCommit, ...]:
        self._ensure_repo(repo_full_name)
        return super().fetch_pull_request_commits(repo_full_name=repo_full_name, pr_number=pr_number)

    def fetch_pull_request_timeline(self, *, repo_full_name: str, pr_number: int) -> tuple[PullRequestReviewRequestEvent, ...]:
        self._ensure_repo(repo_full_name)
        return super().fetch_pull_request_timeline(repo_full_name=repo_full_name, pr_number=pr_number)

    def _request_json(self, path: str) -> object:
        if not self.working_directory.is_dir():
            raise FileNotFoundError(f"GitHub CLI 工作目录不存在：{self.working_directory}")
        env = os.environ.copy()
        env["GH_TOKEN"] = self._resolve_token()
        env["GH_HOST"] = self.hostname
        env["GH_PROMPT_DISABLED"] = "1"
        for variable in ("GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
            env.pop(variable, None)
        result = subprocess.run(
            [self.cli_path, "api", "--hostname", self.hostname, "-H", "Accept: application/vnd.github+json", path],
            cwd=self.working_directory,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=env,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"gh 命令执行失败：{detail[:1000]}")
        return json.loads(result.stdout or "null")

    def _resolve_token(self) -> str:
        if self._token:
            return self._token
        if not self.account:
            raise ValueError("GITHUB_CLI_ACCOUNT 未配置。")
        env = os.environ.copy()
        env["GH_PROMPT_DISABLED"] = "1"
        for variable in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
            env.pop(variable, None)
        result = subprocess.run(
            [self.cli_path, "auth", "token", "--hostname", self.hostname, "--user", self.account],
            cwd=self.working_directory,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
            env=env,
        )
        token = result.stdout.strip()
        if result.returncode != 0 or not token:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(f"无法获取 GitHub CLI 账号 {self.account} 的凭证：{detail[:1000]}")
        self._token = token
        return token

    def _ensure_repo(self, repo_full_name: str) -> None:
        if repo_full_name.strip() != self.repo_full_name:
            raise ValueError(f"GitHub CLI client 仓库不匹配：{repo_full_name} != {self.repo_full_name}")


def _snapshot_from_payload(repo_full_name: str, payload: dict[str, object], files: tuple[PullRequestFile, ...]) -> PullRequestSnapshot:
    base = payload.get("base") if isinstance(payload.get("base"), dict) else {}
    head = payload.get("head") if isinstance(payload.get("head"), dict) else {}
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    requested_reviewers = payload.get("requested_reviewers") if isinstance(payload.get("requested_reviewers"), list) else []
    requested_teams = payload.get("requested_teams") if isinstance(payload.get("requested_teams"), list) else []
    return PullRequestSnapshot(
        repo_full_name=repo_full_name,
        pr_number=int(payload.get("number") or 0),
        title=str(payload.get("title") or ""),
        body=str(payload.get("body") or ""),
        base_ref=str(base.get("ref") or ""),
        base_sha=str(base.get("sha") or ""),
        head_sha=str(head.get("sha") or ""),
        draft=bool(payload.get("draft")),
        files=files,
        state=str(payload.get("state") or "open"),
        author_login=str(user.get("login") or ""),
        html_url=str(payload.get("html_url") or ""),
        requested_reviewer_logins=tuple(str(item.get("login") or "") for item in requested_reviewers if isinstance(item, dict) and item.get("login")),
        requested_team_slugs=tuple(str(item.get("slug") or "") for item in requested_teams if isinstance(item, dict) and item.get("slug")),
        updated_at=str(payload.get("updated_at") or ""),
        merged_at=str(payload.get("merged_at") or ""),
    )


def _files_from_payload(payload: list[object]) -> tuple[PullRequestFile, ...]:
    return tuple(
        PullRequestFile(filename=str(item.get("filename") or ""), status=str(item.get("status") or ""), patch=str(item.get("patch") or ""))
        for item in payload
        if isinstance(item, dict) and item.get("filename")
    )


def _commits_from_payload(payload: list[object]) -> tuple[PullRequestCommit, ...]:
    commits: list[PullRequestCommit] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        author = item.get("author") if isinstance(item.get("author"), dict) else {}
        commit = item.get("commit") if isinstance(item.get("commit"), dict) else {}
        commit_author = commit.get("author") if isinstance(commit.get("author"), dict) else {}
        email = str(commit_author.get("email") or "").strip()
        if email:
            commits.append(PullRequestCommit(author_login=str(author.get("login") or ""), author_email=email))
    return tuple(commits)


def _timeline_from_payload(payload: list[object]) -> tuple[PullRequestReviewRequestEvent, ...]:
    events: list[PullRequestReviewRequestEvent] = []
    for item in payload:
        if not isinstance(item, dict) or item.get("event") != "review_requested":
            continue
        actor = item.get("actor") if isinstance(item.get("actor"), dict) else {}
        reviewer = item.get("requested_reviewer") if isinstance(item.get("requested_reviewer"), dict) else {}
        team = item.get("requested_team") if isinstance(item.get("requested_team"), dict) else {}
        event_id = str(item.get("id") or "")
        if event_id:
            events.append(
                PullRequestReviewRequestEvent(
                    event_id=event_id,
                    actor_login=str(actor.get("login") or ""),
                    reviewer_login=str(reviewer.get("login") or ""),
                    team_slug=str(team.get("slug") or ""),
                    created_at=str(item.get("created_at") or ""),
                )
            )
    return tuple(events)


def _hash_text(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"
