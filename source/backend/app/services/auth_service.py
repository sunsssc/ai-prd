from __future__ import annotations

import base64
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from threading import Lock
from typing import Any
from uuid import uuid4

from app.services.auth_models import (
    AccessDecision,
    GoogleIdentity,
    SessionRecord,
    UserRecord,
    VerificationDispatchResult,
    VerificationCodeRevealResult,
)
from app.services.email_service import EmailDeliveryError, EmailSender
from app.services.auth_store import SQLiteAuthStore


class AccessPolicyService:
    def __init__(self, settings: Any) -> None:
        self.allowed_domains = {domain.lower() for domain in settings.google_allowed_domains}
        self.allowed_departments = [department.lower() for department in settings.google_allowed_departments]
        self.allowed_accounts = {
            email.strip().lower()
            for email in [*settings.auth_admin_accounts, *settings.auth_auto_approved_accounts]
            if email.strip()
        }

    def evaluate_google_identity(self, identity: GoogleIdentity) -> AccessDecision:
        if not identity.email_verified:
            return AccessDecision(
                result="reject_identity",
                reason="Google 账号邮箱尚未验证。",
                allowed=False,
            )

        normalized_email = identity.email.strip().lower()
        if normalized_email in self.allowed_accounts:
            return AccessDecision(
                result="allow",
                reason="账号在明确允许名单中，允许登录。",
                allowed=True,
                access_group="member",
            )

        email_domain = identity.email.rsplit("@", 1)[-1].lower()
        hosted_domain = (identity.hosted_domain or "").lower()
        if self.allowed_domains and email_domain not in self.allowed_domains and hosted_domain not in self.allowed_domains:
            return AccessDecision(
                result="reject_identity",
                reason="当前仅允许企业受管域名账号登录。",
                allowed=False,
            )

        matched_department = self._match_department(identity.department_candidates)
        if self.allowed_departments and matched_department is None:
            return AccessDecision(
                result="reject_department",
                reason="当前版本仅对产品和研发部门开放。",
                allowed=False,
            )

        return AccessDecision(
            result="allow",
            reason="认证通过，允许登录。",
            allowed=True,
            matched_department=matched_department,
            access_group=self._normalize_access_group(matched_department) if matched_department else "member",
        )

    def evaluate_email_domain(self, email: str, allowed_domain: str) -> AccessDecision:
        normalized_email = email.strip().lower()
        if "@" not in normalized_email:
            return AccessDecision(
                result="reject_identity",
                reason="邮箱格式不正确。",
                allowed=False,
            )

        if normalized_email in self.allowed_accounts:
            return AccessDecision(
                result="allow",
                reason="账号在明确允许名单中，允许登录。",
                allowed=True,
                access_group="member",
            )

        _, email_domain = normalized_email.rsplit("@", 1)
        if email_domain != allowed_domain.lower():
            return AccessDecision(
                result="reject_identity",
                reason=f"当前仅允许 {allowed_domain} 企业邮箱登录。",
                allowed=False,
            )

        return AccessDecision(
            result="allow",
            reason="邮箱域名校验通过。",
            allowed=True,
            access_group="member",
        )

    def _match_department(self, candidates: list[str]) -> str | None:
        normalized_candidates = [candidate.strip() for candidate in candidates if candidate and candidate.strip()]
        lowered_candidates = [candidate.lower() for candidate in normalized_candidates]
        for allowed_department in self.allowed_departments:
            for index, candidate in enumerate(lowered_candidates):
                if allowed_department in candidate:
                    return normalized_candidates[index]
        return None

    def _normalize_access_group(self, department_name: str) -> str:
        lowered_name = department_name.lower()
        if "product" in lowered_name or lowered_name == "pm" or "产品" in department_name:
            return "product"
        if "engineering" in lowered_name or "r&d" in lowered_name or lowered_name == "rd" or "研发" in department_name:
            return "engineering"
        return "member"


class InMemoryOAuthStateStore:
    def __init__(self, ttl_seconds: int) -> None:
        self.ttl_seconds = ttl_seconds
        self._states: dict[str, datetime] = {}

    def issue(self) -> str:
        from datetime import timedelta, timezone
        from uuid import uuid4

        state = str(uuid4())
        self._states[state] = datetime.now(timezone.utc) + timedelta(seconds=self.ttl_seconds)
        return state

    def validate_and_consume(self, state: str, cookie_state: str | None) -> bool:
        from datetime import timezone

        if not cookie_state or cookie_state != state:
            return False
        expires_at = self._states.pop(state, None)
        if expires_at is None:
            return False
        return expires_at > datetime.now(timezone.utc)


class AuthService:
    def __init__(
        self,
        access_policy: AccessPolicyService,
        auth_store: SQLiteAuthStore,
        email_allowed_domain: str,
        email_sender: EmailSender,
        verification_code_length: int,
        verification_ttl_seconds: int,
        verification_max_attempts: int,
        verification_cooldown_seconds: int,
        verification_debug_response: bool,
        verification_dev_shortcut_enabled: bool,
        app_env: str,
        session_jwt_secret: str,
        session_jwt_issuer: str,
        admin_session_jwt_issuer: str,
        admin_accounts: list[str],
        auto_approved_accounts: list[str],
    ) -> None:
        self.access_policy = access_policy
        self.auth_store = auth_store
        self.email_allowed_domain = email_allowed_domain.lower()
        self.email_sender = email_sender
        self.verification_code_length = verification_code_length
        self.verification_ttl_seconds = verification_ttl_seconds
        self.verification_max_attempts = verification_max_attempts
        self.verification_cooldown_seconds = verification_cooldown_seconds
        self.verification_debug_response = verification_debug_response
        self.verification_dev_shortcut_enabled = verification_dev_shortcut_enabled
        self.app_env = app_env.strip().lower()
        self.session_jwt_secret = session_jwt_secret
        self.session_jwt_issuer = session_jwt_issuer
        self.admin_session_jwt_issuer = admin_session_jwt_issuer
        self.admin_accounts = {email.strip().lower() for email in admin_accounts}
        self.auto_approved_accounts = {email.strip().lower() for email in auto_approved_accounts}
        self._latest_plaintext_codes: dict[tuple[str, str], tuple[str, datetime]] = {}
        self._latest_plaintext_codes_lock = Lock()

    def finalize_google_login(self, identity: GoogleIdentity) -> tuple[UserRecord, AccessDecision, SessionRecord | None]:
        decision = self.access_policy.evaluate_google_identity(identity)
        normalized_email = identity.email.strip().lower()

        # 注册即激活账号；管理员与自动放行账号同时直接获得 Agent 权限。
        initial_role = "admin" if normalized_email in self.admin_accounts else "member"
        initial_agent_access = (
            "active"
            if normalized_email in self.admin_accounts or normalized_email in self.auto_approved_accounts
            else "none"
        )

        user = self.auth_store.upsert_google_user(
            email=identity.email,
            name=identity.name,
            avatar_url=identity.picture,
            hosted_domain=identity.hosted_domain,
            email_verified=identity.email_verified,
            google_user_id=identity.google_user_id,
            decision=decision,
            initial_status="active",
            initial_role=initial_role,
            initial_agent_access=initial_agent_access,
        )

        if normalized_email in self.admin_accounts:
            if user.role != "admin":
                user = self.auth_store.update_user_role(user.user_id, "admin") or user
            if user.status != "active":
                user = self.auth_store.update_user_status(user.user_id, "active") or user
            if user.agent_access != "active":
                user = self.auth_store.update_user_agent_access(user.user_id, "active") or user
        elif normalized_email in self.auto_approved_accounts and user.agent_access == "none":
            user = self.auth_store.update_user_agent_access(user.user_id, "active") or user

        self.auth_store.append_decision_log(
            user_id=user.user_id,
            email=user.email,
            provider="google",
            decision=decision,
        )

        session = self._create_session(user.user_id) if decision.allowed else None
        return user, decision, session

    def finalize_admin_google_login(self, identity: GoogleIdentity) -> tuple[UserRecord | None, str]:
        normalized_email = identity.email.strip().lower()

        if normalized_email not in self.admin_accounts:
            user = self.auth_store.get_user_by_email(normalized_email)
            if user is None or user.role != "admin":
                return None, "该账号没有管理员权限。"
            if user.status == "blocked":
                return None, "该账号已被封禁。"
            session = self._create_admin_session(user.user_id)
            return user, session.token

        # 管理员账号：如果用户尚不存在，先通过常规流程创建
        user = self.auth_store.get_user_by_email(normalized_email)
        if user is None:
            user, _, _ = self.finalize_google_login(identity)
        elif user.role != "admin":
            user = self.auth_store.update_user_role(user.user_id, "admin") or user
        if user.status == "blocked":
            return None, "该账号已被封禁。"
        session = self._create_admin_session(user.user_id)
        return user, session.token

    def finalize_google_access_token_login(
        self,
        identity: GoogleIdentity,
    ) -> tuple[UserRecord, AccessDecision, SessionRecord | None]:
        return self.finalize_google_login(identity)

    def request_email_code(self, email: str, purpose: str) -> VerificationDispatchResult:
        normalized_email = email.strip().lower()
        purpose = purpose.strip().lower()
        if purpose not in {"register", "login", "auto"}:
            return VerificationDispatchResult(
                result="reject",
                reason="验证码用途不支持。",
            )

        decision = self.access_policy.evaluate_email_domain(normalized_email, self.email_allowed_domain)
        if not decision.allowed:
            return VerificationDispatchResult(
                result="reject",
                reason=decision.reason,
            )

        existing_user = self.auth_store.get_user_by_email(normalized_email)
        if purpose == "auto":
            purpose = "login" if existing_user is not None else "register"

        if purpose == "register" and existing_user is not None:
            return VerificationDispatchResult(
                result="reject",
                reason="该邮箱已经注册，请直接获取登录验证码。",
            )
        if purpose == "login" and existing_user is None:
            return VerificationDispatchResult(
                result="reject",
                reason="该邮箱尚未注册，请先注册。",
            )

        latest_code = self.auth_store.get_latest_pending_verification_code(normalized_email, purpose)
        if latest_code is not None:
            now = datetime.now(latest_code.created_at.tzinfo)
            cooldown_deadline = latest_code.created_at.timestamp() + self.verification_cooldown_seconds
            remaining = int(cooldown_deadline - now.timestamp())
            if latest_code.status == "pending" and remaining > 0:
                return VerificationDispatchResult(
                    result="cooldown",
                    reason="验证码发送过于频繁，请稍后再试。",
                    purpose=purpose,
                    cooldown_seconds=remaining,
                )

        code = self._generate_verification_code()
        self.auth_store.issue_verification_code(
            email=normalized_email,
            purpose=purpose,
            code=code,
            ttl_seconds=self.verification_ttl_seconds,
        )
        self._remember_latest_verification_code(
            email=normalized_email,
            purpose=purpose,
            code=code,
        )
        try:
            self.email_sender.send_verification_code(
                email=normalized_email,
                code=code,
                purpose=purpose,
            )
        except EmailDeliveryError as exc:
            return VerificationDispatchResult(
                result="error",
                reason=str(exc),
                purpose=purpose,
            )

        return VerificationDispatchResult(
            result="sent",
            reason="验证码已发送，请检查邮箱。",
            purpose=purpose,
            expires_in_seconds=self.verification_ttl_seconds,
            debug_code=code if self.verification_debug_response else None,
        )

    def reveal_latest_email_code(self, email: str, purpose: str) -> VerificationCodeRevealResult:
        if not self._is_dev_shortcut_enabled():
            return VerificationCodeRevealResult(
                result="disabled",
                reason="当前环境未开启开发验证码快捷查看。",
            )

        normalized_email = email.strip().lower()
        normalized_purpose = purpose.strip().lower()
        if normalized_purpose not in {"register", "login"}:
            return VerificationCodeRevealResult(
                result="reject",
                reason="验证码用途不支持。",
            )

        latest_code = self.auth_store.get_latest_pending_verification_code(normalized_email, normalized_purpose)
        if latest_code is None:
            return VerificationCodeRevealResult(
                result="not_found",
                reason="当前没有可用的待验证验证码。",
            )

        now = datetime.now(latest_code.expires_at.tzinfo)
        if latest_code.expires_at <= now:
            return VerificationCodeRevealResult(
                result="expired",
                reason="验证码已过期，请重新获取。",
            )

        with self._latest_plaintext_codes_lock:
            cached = self._latest_plaintext_codes.get((normalized_email, normalized_purpose))

        if cached is None:
            return VerificationCodeRevealResult(
                result="not_found",
                reason="当前验证码未缓存明文，请重新发送一次验证码。",
            )

        code, expires_at = cached
        if expires_at <= now:
            with self._latest_plaintext_codes_lock:
                self._latest_plaintext_codes.pop((normalized_email, normalized_purpose), None)
            return VerificationCodeRevealResult(
                result="expired",
                reason="验证码已过期，请重新获取。",
            )

        return VerificationCodeRevealResult(
            result="ok",
            reason="已返回开发环境验证码。",
            code=code,
            expires_in_seconds=max(int(expires_at.timestamp() - now.timestamp()), 0),
        )

    def register_with_email(self, email: str, name: str | None, code: str) -> tuple[UserRecord | None, AccessDecision, SessionRecord | None]:
        normalized_email = email.strip().lower()
        decision = self.access_policy.evaluate_email_domain(normalized_email, self.email_allowed_domain)
        if not decision.allowed:
            self.auth_store.append_decision_log(
                user_id=None,
                email=normalized_email,
                provider="email",
                decision=decision,
            )
            return None, decision, None

        verification_result = self.auth_store.consume_verification_code(
            email=normalized_email,
            purpose="register",
            code=code.strip(),
            max_attempts=self.verification_max_attempts,
        )
        if not verification_result.valid:
            decision = AccessDecision(
                result="reject_identity",
                reason=verification_result.reason,
                allowed=False,
            )
            self.auth_store.append_decision_log(
                user_id=None,
                email=normalized_email,
                provider="email",
                decision=decision,
            )
            return None, decision, None

        # 注册即激活账号；管理员与自动放行账号同时直接获得 Agent 权限。
        initial_role = "admin" if normalized_email in self.admin_accounts else "member"
        initial_agent_access = (
            "active"
            if normalized_email in self.admin_accounts or normalized_email in self.auto_approved_accounts
            else "none"
        )

        try:
            user = self.auth_store.create_email_user(
                normalized_email,
                name.strip() if name else None,
                initial_status="active",
                initial_role=initial_role,
                initial_agent_access=initial_agent_access,
            )
        except ValueError:
            decision = AccessDecision(
                result="reject_identity",
                reason="该邮箱已经注册，请直接登录。",
                allowed=False,
            )
            existing_user = self.auth_store.get_user_by_email(normalized_email)
            self.auth_store.append_decision_log(
                user_id=existing_user.user_id if existing_user else None,
                email=normalized_email,
                provider="email",
                decision=decision,
            )
            return None, decision, None

        self.auth_store.append_decision_log(
            user_id=user.user_id,
            email=user.email,
            provider="email",
            decision=decision,
        )
        session = self._create_session(user.user_id)
        return user, decision, session

    def login_with_email(self, email: str, code: str) -> tuple[UserRecord | None, AccessDecision, SessionRecord | None]:
        normalized_email = email.strip().lower()
        decision = self.access_policy.evaluate_email_domain(normalized_email, self.email_allowed_domain)
        if not decision.allowed:
            self.auth_store.append_decision_log(
                user_id=None,
                email=normalized_email,
                provider="email",
                decision=decision,
            )
            return None, decision, None

        verification_result = self.auth_store.consume_verification_code(
            email=normalized_email,
            purpose="login",
            code=code.strip(),
            max_attempts=self.verification_max_attempts,
        )
        if not verification_result.valid:
            decision = AccessDecision(
                result="reject_identity",
                reason=verification_result.reason,
                allowed=False,
            )
            self.auth_store.append_decision_log(
                user_id=None,
                email=normalized_email,
                provider="email",
                decision=decision,
            )
            return None, decision, None

        user = self.auth_store.touch_email_login(normalized_email)
        if user is None:
            decision = AccessDecision(
                result="reject_identity",
                reason="该邮箱尚未注册，请先注册。",
                allowed=False,
            )
            self.auth_store.append_decision_log(
                user_id=None,
                email=normalized_email,
                provider="email",
                decision=decision,
            )
            return None, decision, None

        self.auth_store.append_decision_log(
            user_id=user.user_id,
            email=user.email,
            provider="email",
            decision=decision,
        )
        session = self._create_session(user.user_id)
        return user, decision, session

    def request_agent_access(self, user: UserRecord, reason: str) -> tuple[object, UserRecord]:
        if user.status != "active":
            raise ValueError("账号当前不可用，无法申请 Agent 权限。")
        if user.agent_access == "pending":
            raise ValueError("已有 Agent 权限申请正在审核中。")
        if user.agent_access == "active":
            raise ValueError("已拥有 Agent 使用权限，无需重复申请。")
        if not reason or not reason.strip():
            raise ValueError("申请原因不能为空。")

        request = self.auth_store.create_agent_access_request(
            user_id=user.user_id,
            reason=reason,
        )
        updated_user = self.auth_store.update_user_agent_access(user.user_id, "pending") or user
        return request, updated_user

    def review_agent_access_request(
        self,
        *,
        request_id: str,
        reviewer_user_id: str,
        approved: bool,
        review_comment: str | None,
    ) -> tuple[object, UserRecord]:
        try:
            request = self.auth_store.review_agent_access_request(
                request_id=request_id,
                status="approved" if approved else "rejected",
                reviewer_user_id=reviewer_user_id,
                review_comment=review_comment,
            )
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        if request is None:
            raise ValueError("申请不存在。")

        user = self.auth_store.get_user_by_id(request.user_id)
        if user is None:
            raise ValueError("申请对应的用户不存在。")
        updated_user = (
            self.auth_store.update_user_agent_access(user.user_id, "active" if approved else "rejected")
            or user
        )
        return request, updated_user

    def set_user_agent_access(self, user_id: str, agent_access: str) -> UserRecord:
        if agent_access not in {"active", "none"}:
            raise ValueError("只能直接开通或收回 Agent 权限。")
        user = self.auth_store.get_user_by_id(user_id)
        if user is None:
            raise ValueError("用户不存在。")
        if user.status == "blocked":
            raise ValueError("账号已被封禁，不能调整 Agent 权限。")
        return self.auth_store.update_user_agent_access(user_id, agent_access) or user

    def get_current_user(self, token: str) -> UserRecord | None:
        payload = self._decode_session_jwt(token, self.session_jwt_issuer)
        if payload is None:
            return None
        session = self.auth_store.get_session(token)
        if session is None:
            return None
        if payload.get("sub") != session.user_id:
            return None
        return self.auth_store.get_user_by_id(session.user_id)

    def get_current_admin(self, token: str) -> UserRecord | None:
        payload = self._decode_session_jwt(token, self.admin_session_jwt_issuer)
        if payload is None:
            return None
        session = self.auth_store.get_session(token)
        if session is None:
            return None
        if payload.get("sub") != session.user_id:
            return None
        user = self.auth_store.get_user_by_id(session.user_id)
        if user is None or user.role != "admin":
            return None
        return user

    def logout(self, token: str) -> None:
        self.auth_store.delete_session(token)

    def _generate_verification_code(self) -> str:
        digits = "0123456789"
        return "".join(secrets.choice(digits) for _ in range(self.verification_code_length))

    def _remember_latest_verification_code(self, *, email: str, purpose: str, code: str) -> None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.verification_ttl_seconds)
        with self._latest_plaintext_codes_lock:
            self._latest_plaintext_codes[(email, purpose)] = (code, expires_at)

    def _is_dev_shortcut_enabled(self) -> bool:
        return self.verification_dev_shortcut_enabled and self.app_env != "production"

    def _create_session(self, user_id: str) -> SessionRecord:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.auth_store.session_ttl_seconds)
        token = self._encode_session_jwt(
            {
                "iss": self.session_jwt_issuer,
                "sub": user_id,
                "jti": str(uuid4()),
                "iat": int(datetime.now(timezone.utc).timestamp()),
                "exp": int(expires_at.timestamp()),
            }
        )
        return self.auth_store.create_session(user_id, token)

    def _create_admin_session(self, user_id: str) -> SessionRecord:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=self.auth_store.session_ttl_seconds)
        token = self._encode_session_jwt(
            {
                "iss": self.admin_session_jwt_issuer,
                "sub": user_id,
                "jti": str(uuid4()),
                "iat": int(datetime.now(timezone.utc).timestamp()),
                "exp": int(expires_at.timestamp()),
            }
        )
        return self.auth_store.create_session(user_id, token)

    def _encode_session_jwt(self, payload: dict[str, object]) -> str:
        header = {"alg": "HS256", "typ": "JWT"}
        signing_input = ".".join(
            [
                self._base64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8")),
                self._base64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")),
            ]
        )
        signature = hmac.new(
            self.session_jwt_secret.encode("utf-8"),
            signing_input.encode("ascii"),
            sha256,
        ).digest()
        return f"{signing_input}.{self._base64url_encode(signature)}"

    def _decode_session_jwt(self, token: str, expected_issuer: str | None = None) -> dict[str, object] | None:
        parts = token.split(".")
        if len(parts) != 3:
            return None

        signing_input = ".".join(parts[:2])
        expected_signature = hmac.new(
            self.session_jwt_secret.encode("utf-8"),
            signing_input.encode("ascii"),
            sha256,
        ).digest()
        try:
            actual_signature = self._base64url_decode(parts[2])
            header = json.loads(self._base64url_decode(parts[0]).decode("utf-8"))
            payload = json.loads(self._base64url_decode(parts[1]).decode("utf-8"))
        except (ValueError, json.JSONDecodeError):
            return None

        if not hmac.compare_digest(actual_signature, expected_signature):
            return None
        if header.get("alg") != "HS256" or header.get("typ") != "JWT":
            return None
        issuer = expected_issuer or self.session_jwt_issuer
        if payload.get("iss") != issuer:
            return None

        exp = payload.get("exp")
        if not isinstance(exp, int) or exp <= int(datetime.now(timezone.utc).timestamp()):
            return None
        return payload

    def _base64url_encode(self, value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    def _base64url_decode(self, value: str) -> bytes:
        padded = value + "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(padded.encode("ascii"))
