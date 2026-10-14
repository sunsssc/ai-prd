"""Explicit, bounded PR associations; links never grant repository access."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.business.code_reviews.review_context import CodeReviewPreparedContext

MAX_RELATED_PRS = 8
_HEADINGS = {"related prs", "related pull requests", "关联 pr", "关联 prs", "关联pr", "关联prs", "关联拉取请求"}
_HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_URL = re.compile(r"https?://[^\s<>\[\]()`]+", re.IGNORECASE)
_PR_PATH = re.compile(r"/([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9][A-Za-z0-9_.-]*)/pull/([1-9][0-9]{0,9})(?:/(?:files|commits))?/?")


@dataclass(frozen=True)
class RelatedPullRequest:
    repo_full_name: str
    pr_number: int


def extract_related_pull_requests(body: str) -> tuple[RelatedPullRequest, ...]:
    """Read only a Related PRs / 关联 PR Markdown section, never recurse."""
    body = re.sub(r"<!--.*?(?:-->|\Z)", "", body, flags=re.DOTALL)
    section_level: int | None = None
    fence: str | None = None
    found: dict[tuple[str, int], RelatedPullRequest] = {}
    for line in body.splitlines():
        fence_match = _FENCE.match(line)
        if fence is not None:
            if fence_match and fence_match[1][0] == fence[0] and len(fence_match[1]) >= len(fence) and not fence_match[2].strip():
                fence = None
            continue
        if fence_match:
            fence = fence_match[1]
            continue
        heading = _HEADING.match(line)
        if heading:
            if " ".join(heading[2].casefold().split()) in _HEADINGS:
                section_level = len(heading[1])
            elif section_level is not None and len(heading[1]) <= section_level:
                section_level = None
            continue
        if section_level is None or not line.strip():
            continue
        urls = _URL.findall(line)
        if not urls:
            if line.strip().lstrip("-* ").casefold() in {"none", "n/a", "无"}:
                continue
            raise ValueError("关联 PR 小节的每项必须包含完整 https://github.com/owner/repo/pull/123 链接。")
        for url in urls:
            parsed = urlsplit(url.rstrip(".,;，。；"))
            match = _PR_PATH.fullmatch(parsed.path)
            if parsed.scheme.casefold() != "https" or parsed.netloc.casefold() != "github.com" or not match:
                raise ValueError("关联 PR 只支持 https://github.com/owner/repo/pull/123 链接。")
            repo = f"{match[1]}/{match[2]}"
            number = int(match[3])
            found.setdefault((repo.casefold(), number), RelatedPullRequest(repo, number))
            if len(found) > MAX_RELATED_PRS:
                raise ValueError(f"关联 PR 不能超过 {MAX_RELATED_PRS} 个；请缩小联合审查范围。")
    return tuple(found.values())


def review_scope(contexts: tuple[CodeReviewPreparedContext, ...]) -> list[dict[str, object]]:
    return [
        {"repo_full_name": c.repo_full_name, "pr_number": c.pr_number,
         "base_sha": c.base_sha, "head_sha": c.head_sha,
         "base_worktree_path": c.base_worktree_path, "worktree_path": c.worktree_path,
         "context_dir_path": c.context_dir_path}
        for c in contexts
    ]


def render_review_scope(contexts: tuple[CodeReviewPreparedContext, ...]) -> str:
    lines = ["## 联合审查范围（服务端固定版本）", "",
             "以下版本由服务端记录；结论仅覆盖这些快照，不代表它们已同时部署。", "",
             "| 范围 | 仓库 / PR | Base SHA | Head SHA |", "| --- | --- | --- | --- |"]
    for index, c in enumerate(contexts):
        role = "主 PR" if index == 0 else "关联 PR"
        lines.append(f"| {role} | `{c.repo_full_name}#{c.pr_number}` | `{c.base_sha}` | `{c.head_sha}` |")
    return "\n".join(lines)
