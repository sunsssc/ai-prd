from __future__ import annotations

import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from app.business.code_reviews.store import REVIEW_TYPE


SOURCE_TYPE = "github_pr"
DEFAULT_REVIEW_POLICY_PATH = "source/backend/app/business/code_reviews/default_review_policy.md"


class CodeReviewArtifactJob(Protocol):
    repo_full_name: str
    pr_number: int
    pr_url: str
    base_ref: str
    base_sha: str
    head_sha: str
    diff_hash: str
    requested_event_id: str
    requested_by_login: str
    requested_reviewer_logins: str
    requested_team_slugs: str
    notification_emails: str
    review_policy_path: str
    status: str
    review_id: str | None


class CodeReviewArtifactSnapshot(Protocol):
    author_login: str


@dataclass(frozen=True)
class CodeReviewRecord:
    review_id: str
    review_type: str
    path: str
    repo_full_name: str
    pr_number: int
    pr_url: str
    base_ref: str
    base_sha: str
    head_sha: str
    diff_hash: str
    status: str
    risk_level: str
    created_at: str
    completed_at: str | None


@dataclass(frozen=True)
class CodeReviewDetail(CodeReviewRecord):
    requested_event_id: str = ""
    requested_by_login: str = ""
    author_login: str = ""
    requested_reviewer_logins: tuple[str, ...] = ()
    requested_team_slugs: tuple[str, ...] = ()
    notification_emails: tuple[str, ...] = ()
    review_policy: str = DEFAULT_REVIEW_POLICY_PATH
    content: str = ""
    frontmatter: dict[str, str] | None = None


class CodeReviewArtifacts:
    def __init__(self, *, base_dir: Path, reviews_root: Path) -> None:
        self.base_dir = base_dir.resolve()
        self.reviews_root = reviews_root.resolve()

    def get_job_review(self, *, job: CodeReviewArtifactJob) -> CodeReviewRecord | None:
        if job.status != "completed" or not job.review_id:
            return None
        detail = self.get_review_detail(review_id=job.review_id)
        return CodeReviewRecord(
            review_id=detail.review_id,
            review_type=detail.review_type,
            path=detail.path,
            repo_full_name=detail.repo_full_name,
            pr_number=detail.pr_number,
            pr_url=detail.pr_url,
            base_ref=detail.base_ref,
            base_sha=detail.base_sha,
            head_sha=detail.head_sha,
            diff_hash=detail.diff_hash,
            status=detail.status,
            risk_level=detail.risk_level,
            created_at=detail.created_at,
            completed_at=detail.completed_at,
        )

    def get_review_detail(self, *, review_id: str) -> CodeReviewDetail:
        review_file = self._find_review_file(review_id)
        raw = review_file.read_text(encoding="utf-8", errors="replace")
        frontmatter, body = _split_frontmatter(raw)
        return CodeReviewDetail(
            review_id=frontmatter.get("review_id", review_id),
            review_type=frontmatter.get("review_type", REVIEW_TYPE),
            path=self._display_path(review_file),
            repo_full_name=frontmatter.get("repo_full_name", ""),
            pr_number=int(frontmatter.get("pr_number") or 0),
            pr_url=frontmatter.get("pr_url", ""),
            base_ref=frontmatter.get("base_ref", ""),
            base_sha=frontmatter.get("base_sha", ""),
            head_sha=frontmatter.get("head_sha", ""),
            diff_hash=frontmatter.get("diff_hash", ""),
            status=frontmatter.get("status", "completed"),
            risk_level=frontmatter.get("risk_level", "medium"),
            created_at=frontmatter.get("created_at", ""),
            completed_at=frontmatter.get("completed_at") or None,
            requested_event_id=frontmatter.get("requested_event_id", ""),
            requested_by_login=frontmatter.get("requested_by_login", ""),
            author_login=frontmatter.get("author_login", ""),
            requested_reviewer_logins=_parse_csv(frontmatter.get("requested_reviewer_logins", "")),
            requested_team_slugs=_parse_csv(frontmatter.get("requested_team_slugs", "")),
            notification_emails=_parse_csv(frontmatter.get("notification_emails", "")),
            review_policy=frontmatter.get("review_policy", DEFAULT_REVIEW_POLICY_PATH),
            content=body.strip(),
            frontmatter=frontmatter,
        )

    def write_review(
        self,
        *,
        job: CodeReviewArtifactJob,
        snapshot: CodeReviewArtifactSnapshot,
        generated_markdown: str,
    ) -> CodeReviewRecord:
        now = datetime.now(timezone.utc)
        created_at = now.isoformat()
        review_id = f"code_review_{now.strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(3)}"
        review_dir = self.reviews_root / now.strftime("%Y") / now.strftime("%m")
        review_dir.mkdir(parents=True, exist_ok=True)
        review_file = review_dir / f"{review_id}.md"
        body = generated_markdown.strip() or "未生成有效代码 Review 报告。"
        if not body.startswith("#"):
            body = f"# 代码 Review 报告\n\n{body}"
        risk_level = _detect_risk_level(body)
        frontmatter = {
            "review_id": review_id,
            "review_type": REVIEW_TYPE,
            "source_type": SOURCE_TYPE,
            "repo_full_name": job.repo_full_name,
            "pr_number": str(job.pr_number),
            "pr_url": job.pr_url,
            "base_ref": job.base_ref,
            "base_sha": job.base_sha,
            "head_sha": job.head_sha,
            "diff_hash": job.diff_hash,
            "requested_event_id": job.requested_event_id,
            "requested_by_login": job.requested_by_login,
            "author_login": snapshot.author_login,
            "requested_reviewer_logins": job.requested_reviewer_logins,
            "requested_team_slugs": job.requested_team_slugs,
            "notification_emails": job.notification_emails,
            "review_policy": job.review_policy_path,
            "status": "completed",
            "risk_level": risk_level,
            "created_at": created_at,
            "completed_at": created_at,
        }
        review_file.write_text(f"{_format_frontmatter(frontmatter)}\n\n{body}\n", encoding="utf-8")
        return CodeReviewRecord(
            review_id=review_id,
            review_type=REVIEW_TYPE,
            path=self._display_path(review_file),
            repo_full_name=job.repo_full_name,
            pr_number=job.pr_number,
            pr_url=job.pr_url,
            base_ref=job.base_ref,
            base_sha=job.base_sha,
            head_sha=job.head_sha,
            diff_hash=job.diff_hash,
            status="completed",
            risk_level=risk_level,
            created_at=created_at,
            completed_at=created_at,
        )

    def _find_review_file(self, review_id: str) -> Path:
        normalized = review_id.strip()
        if not re.match(r"^code_review_[0-9]{8}_[0-9]{6}_[a-f0-9]{6}$", normalized):
            raise FileNotFoundError("代码 Review 报告不存在。")
        matches = list(self.reviews_root.rglob(f"{normalized}.md"))
        if not matches:
            raise FileNotFoundError("代码 Review 报告不存在。")
        return matches[0]

    def _display_path(self, path: Path) -> str:
        return path.resolve().relative_to(self.base_dir).as_posix()


def _format_frontmatter(metadata: dict[str, str]) -> str:
    lines = ["---"]
    lines.extend(f"{key}: {value}" for key, value in metadata.items())
    lines.append("---")
    return "\n".join(lines)


def _split_frontmatter(markdown: str) -> tuple[dict[str, str], str]:
    normalized = markdown.replace("\r\n", "\n")
    match = re.match(r"^---\n([\s\S]*?)\n---(?:\n+|$)", normalized)
    if not match:
        return {}, normalized
    metadata: dict[str, str] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata, normalized[match.end():]


def _detect_risk_level(markdown: str) -> str:
    text = markdown.lower()
    high_markers = ("风险等级：high", "risk_level: high", "严重级别：高", "整体问题级别：高", "[p0]", "[p1]")
    medium_markers = ("风险等级：medium", "risk_level: medium", "严重级别：中", "整体问题级别：中", "[p2]")
    if any(marker in text for marker in high_markers):
        return "high"
    if any(marker in text for marker in medium_markers):
        return "medium"
    if "风险等级：low" in text or "risk_level: low" in text or "严重级别：低" in text:
        return "low"
    return "medium"


def _parse_csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())
