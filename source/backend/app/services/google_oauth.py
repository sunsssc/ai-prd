from __future__ import annotations

import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from app.services.auth_models import GoogleIdentity


class GoogleOAuthError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class GoogleOAuthService:
    authorization_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    token_endpoint = "https://oauth2.googleapis.com/token"
    tokeninfo_endpoint = "https://oauth2.googleapis.com/tokeninfo"
    userinfo_endpoint = "https://openidconnect.googleapis.com/v1/userinfo"
    people_endpoint = "https://people.googleapis.com/v1/people/me?personFields=organizations"

    def __init__(self, settings: Any) -> None:
        self.settings = settings

    def build_authorization_url(self, state: str, redirect_uri: str | None = None) -> str:
        self._ensure_basic_config()
        params = {
            "client_id": self.settings.google_client_id,
            "redirect_uri": redirect_uri or self.settings.google_redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.settings.google_scopes),
            "state": state,
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "select_account",
        }
        if len(self.settings.google_allowed_domains) == 1:
            params["hd"] = self.settings.google_allowed_domains[0]
        return f"{self.authorization_endpoint}?{urlencode(params)}"

    def exchange_code_for_identity(self, code: str, redirect_uri: str | None = None) -> GoogleIdentity:
        self._ensure_full_config()

        token_payload = self._post_form(
            self.token_endpoint,
            {
                "code": code,
                "client_id": self.settings.google_client_id,
                "client_secret": self.settings.google_client_secret,
                "redirect_uri": redirect_uri or self.settings.google_redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        access_token = token_payload.get("access_token")
        if not access_token:
            raise GoogleOAuthError("Google 未返回 access token。", status_code=502)

        userinfo = self._get_json(
            self.userinfo_endpoint,
            headers={"Authorization": f"Bearer {access_token}"},
        )

        people_profile: dict[str, Any] | None = None
        if self.settings.google_fetch_people_profile:
            try:
                people_profile = self._get_json(
                    self.people_endpoint,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
            except GoogleOAuthError:
                people_profile = None

        return self._build_identity(userinfo, people_profile)

    def verify_access_token(self, access_token: str) -> GoogleIdentity:
        self._ensure_basic_config()
        normalized_token = access_token.strip()
        if not normalized_token:
            raise GoogleOAuthError("缺少 Google access token。", status_code=400)

        token_info = self._get_json(
            f"{self.tokeninfo_endpoint}?{urlencode({'access_token': normalized_token})}",
            error_status_code=401,
        )
        audience = token_info.get("aud") or token_info.get("audience") or token_info.get("issued_to")
        if audience and audience != self.settings.google_client_id:
            raise GoogleOAuthError("Google access token 不属于当前应用。", status_code=401)

        expires_in = token_info.get("expires_in")
        if expires_in is not None:
            try:
                if int(expires_in) <= 0:
                    raise GoogleOAuthError("Google access token 已过期。", status_code=401)
            except ValueError as exc:
                raise GoogleOAuthError("Google access token 校验结果无效。", status_code=502) from exc

        userinfo = self._get_json(
            self.userinfo_endpoint,
            headers={"Authorization": f"Bearer {normalized_token}"},
            error_status_code=401,
        )

        people_profile: dict[str, Any] | None = None
        if self.settings.google_fetch_people_profile:
            try:
                people_profile = self._get_json(
                    self.people_endpoint,
                    headers={"Authorization": f"Bearer {normalized_token}"},
                )
            except GoogleOAuthError:
                people_profile = None

        return self._build_identity(userinfo, people_profile)

    def _build_identity(
        self,
        userinfo: dict[str, Any],
        people_profile: dict[str, Any] | None,
    ) -> GoogleIdentity:
        google_user_id = userinfo.get("sub")
        email = userinfo.get("email")
        if not google_user_id or not email:
            raise GoogleOAuthError("Google 返回的身份信息不完整。", status_code=502)

        department_candidates = self._extract_department_candidates(userinfo, people_profile)
        hosted_domain = userinfo.get("hd") or email.rsplit("@", 1)[-1]

        return GoogleIdentity(
            google_user_id=google_user_id,
            email=email,
            email_verified=bool(userinfo.get("email_verified")),
            name=userinfo.get("name"),
            picture=userinfo.get("picture"),
            hosted_domain=hosted_domain,
            department_candidates=department_candidates,
            raw_profile={
                "userinfo": userinfo,
                "people_profile": people_profile,
            },
        )

    def _extract_department_candidates(
        self,
        userinfo: dict[str, Any],
        people_profile: dict[str, Any] | None,
    ) -> list[str]:
        candidates: list[str] = []

        direct_department = userinfo.get("department")
        if isinstance(direct_department, str) and direct_department.strip():
            candidates.append(direct_department.strip())

        organizations = []
        if people_profile:
            organizations = people_profile.get("organizations") or []

        current_organizations = [
            organization
            for organization in organizations
            if isinstance(organization, dict) and organization.get("metadata", {}).get("current", True)
        ]
        ordered_organizations = current_organizations or organizations

        for organization in ordered_organizations:
            if not isinstance(organization, dict):
                continue
            for field in ("department", "name", "title"):
                value = organization.get(field)
                if isinstance(value, str) and value.strip():
                    candidates.append(value.strip())

        seen: set[str] = set()
        deduplicated: list[str] = []
        for candidate in candidates:
            lowered = candidate.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            deduplicated.append(candidate)
        return deduplicated

    def _post_form(self, url: str, payload: dict[str, str]) -> dict[str, Any]:
        data = urlencode(payload).encode("utf-8")
        request = Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        return self._read_json(request)

    def _get_json(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        error_status_code: int = 502,
    ) -> dict[str, Any]:
        request = Request(url, headers=headers or {})
        return self._read_json(request, error_status_code=error_status_code)

    def _read_json(self, request: Request, error_status_code: int = 502) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=10) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="ignore")
            raise GoogleOAuthError(
                f"请求 Google 认证接口失败：{response_body or exc.reason}",
                status_code=error_status_code,
            ) from exc
        except URLError as exc:
            raise GoogleOAuthError("无法连接 Google 认证服务。", status_code=502) from exc

    def _ensure_basic_config(self) -> None:
        if not self.settings.google_client_id:
            raise GoogleOAuthError("缺少 Google OAuth Client ID 配置。", status_code=503)
        if not self.settings.google_redirect_uri:
            raise GoogleOAuthError("缺少 Google OAuth 回调地址配置。", status_code=503)

    def _ensure_full_config(self) -> None:
        self._ensure_basic_config()
        if not self.settings.google_client_secret:
            raise GoogleOAuthError("缺少 Google OAuth Client Secret 配置。", status_code=503)
