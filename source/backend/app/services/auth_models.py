from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class GoogleIdentity:
    google_user_id: str
    email: str
    email_verified: bool
    name: str | None
    picture: str | None
    hosted_domain: str | None
    department_candidates: list[str] = field(default_factory=list)
    raw_profile: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AccessDecision:
    result: str
    reason: str
    allowed: bool
    matched_department: str | None = None
    access_group: str | None = None


@dataclass(slots=True)
class UserRecord:
    user_id: str
    email: str
    name: str | None
    avatar_url: str | None
    status: str        # active | blocked（注册即 active，仅封禁会改变）
    role: str          # member | admin
    default_role: str
    agent_access: str  # none | pending | active | rejected（Agent 使用权限）
    auth_provider: str
    hosted_domain: str | None
    email_verified: bool
    department_name: str | None
    access_group: str | None
    created_at: datetime
    first_login_at: datetime
    last_login_at: datetime


@dataclass(slots=True)
class AgentAccessRequestRecord:
    request_id: str
    user_id: str
    reason: str
    status: str  # pending | approved | rejected
    reviewed_by_user_id: str | None
    reviewed_at: datetime | None
    review_comment: str | None
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class SessionRecord:
    token: str
    user_id: str
    expires_at: datetime
    created_at: datetime


@dataclass(slots=True)
class VerificationCodeRecord:
    verification_id: str
    email: str
    purpose: str
    code_hash: str
    status: str
    attempt_count: int
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None


@dataclass(slots=True)
class VerificationDispatchResult:
    result: str
    reason: str
    purpose: str | None = None
    expires_in_seconds: int | None = None
    cooldown_seconds: int | None = None
    debug_code: str | None = None


@dataclass(slots=True)
class VerificationCodeRevealResult:
    result: str
    reason: str
    code: str | None = None
    expires_in_seconds: int | None = None


@dataclass(slots=True)
class VerificationConsumeResult:
    valid: bool
    reason: str
