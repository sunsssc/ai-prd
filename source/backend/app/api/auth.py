from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse

from app.core.config import settings
from app.core.dependencies import get_auth_service, get_google_oauth_service, get_oauth_state_store
from app.schemas.auth import (
    AuthConfigResponse,
    AuthResultResponse,
    CurrentUserResponse,
    EmailCodeDispatchResponse,
    EmailCodeRevealRequest,
    EmailCodeRevealResponse,
    EmailCodeRequest,
    EmailCodeVerifyRequest,
    GoogleAccessTokenLoginRequest,
    GoogleAuthorizationUrlResponse,
    SessionResponse,
    UserProfileResponse,
)
from app.services.auth_models import UserRecord
from app.services.auth_service import AuthService, InMemoryOAuthStateStore
from app.services.google_oauth import GoogleOAuthError, GoogleOAuthService

router = APIRouter(prefix="/auth", tags=["auth"])
admin_auth_router = APIRouter(prefix="/admin/auth", tags=["admin-auth"])


@router.get("/config", response_model=AuthConfigResponse)
def get_auth_config() -> AuthConfigResponse:
    return AuthConfigResponse(email_auth_enabled=settings.email_auth_enabled)


@router.get("/google/url", response_model=GoogleAuthorizationUrlResponse)
def get_google_authorization_url(
    oauth_state_store: InMemoryOAuthStateStore = Depends(get_oauth_state_store),
    google_oauth_service: GoogleOAuthService = Depends(get_google_oauth_service),
) -> JSONResponse:
    state = oauth_state_store.issue()
    try:
        authorization_url = google_oauth_service.build_authorization_url(state)
    except GoogleOAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    response = JSONResponse({"authorization_url": authorization_url})
    _set_oauth_state_cookie(response, state)
    return response


@router.get("/google/login")
def google_login(
    oauth_state_store: InMemoryOAuthStateStore = Depends(get_oauth_state_store),
    google_oauth_service: GoogleOAuthService = Depends(get_google_oauth_service),
) -> RedirectResponse:
    state = oauth_state_store.issue()
    try:
        authorization_url = google_oauth_service.build_authorization_url(state)
    except GoogleOAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    response = RedirectResponse(url=authorization_url, status_code=307)
    _set_oauth_state_cookie(response, state)
    return response


@router.get("/google/callback", response_model=AuthResultResponse)
def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    auth_service: AuthService = Depends(get_auth_service),
    oauth_state_store: InMemoryOAuthStateStore = Depends(get_oauth_state_store),
    google_oauth_service: GoogleOAuthService = Depends(get_google_oauth_service),
    oauth_state_cookie: str | None = Cookie(default=None, alias=settings.oauth_state_cookie_name),
) -> JSONResponse | RedirectResponse:
    if error:
        return _google_callback_error_response(
            request,
            "你已取消 Google 授权。" if error == "access_denied" else f"Google 授权失败：{error}",
        )

    if not code or not state:
        return _google_callback_error_response(request, "Google OAuth 回调参数不完整。")

    if not oauth_state_store.validate_and_consume(state, oauth_state_cookie):
        return _google_callback_error_response(request, "Google OAuth state 校验失败。")

    try:
        identity = google_oauth_service.exchange_code_for_identity(code)
    except GoogleOAuthError as exc:
        if _wants_browser_redirect(request):
            return _google_callback_redirect_response(exc.message)
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    user, decision, session = auth_service.finalize_google_login(identity)
    payload = {
        "result": decision.result,
        "reason": decision.reason,
        "user": _serialize_user(user).model_dump(mode="json"),
        "session": None,
    }

    status_code = 200 if decision.allowed and session else 403
    if session:
        payload["session"] = SessionResponse(
            token=session.token,
            expires_at=session.expires_at,
        ).model_dump(mode="json")

    response: JSONResponse | RedirectResponse
    if _wants_browser_redirect(request):
        response = _google_callback_redirect_response("" if session else decision.reason)
    else:
        response = JSONResponse(payload, status_code=status_code)

    response.delete_cookie(settings.oauth_state_cookie_name)
    if session:
        _set_session_cookie(response, session.token)
    else:
        response.delete_cookie(settings.session_cookie_name)
    return response


@router.post("/google/login", response_model=AuthResultResponse)
def login_with_google_access_token(
    body: GoogleAccessTokenLoginRequest,
    auth_service: AuthService = Depends(get_auth_service),
    google_oauth_service: GoogleOAuthService = Depends(get_google_oauth_service),
) -> JSONResponse:
    try:
        identity = google_oauth_service.verify_access_token(body.access_token)
    except GoogleOAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    user, decision, session = auth_service.finalize_google_access_token_login(identity)
    payload = {
        "result": decision.result,
        "reason": decision.reason,
        "user": _serialize_user(user).model_dump(mode="json"),
        "session": None,
    }

    status_code = 200 if decision.allowed and session else 403
    if session:
        payload["session"] = SessionResponse(
            token=session.token,
            expires_at=session.expires_at,
        ).model_dump(mode="json")

    response = JSONResponse(payload, status_code=status_code)
    if session:
        _set_session_cookie(response, session.token)
    else:
        response.delete_cookie(settings.session_cookie_name)
    return response


@router.post("/email/register", response_model=AuthResultResponse, status_code=201)
def register_with_email(
    body: EmailCodeVerifyRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> JSONResponse:
    _ensure_email_auth_enabled()
    user, decision, session = auth_service.register_with_email(body.email, body.name, body.code)
    payload = {
        "result": decision.result,
        "reason": decision.reason,
        "user": _serialize_user(user).model_dump(mode="json") if user else None,
        "session": None,
    }
    status_code = 201 if session and user else 400
    if session:
        payload["session"] = SessionResponse(
            token=session.token,
            expires_at=session.expires_at,
        ).model_dump(mode="json")

    response = JSONResponse(payload, status_code=status_code)
    if session:
        _set_session_cookie(response, session.token)
    else:
        response.delete_cookie(settings.session_cookie_name)
    return response


@router.post("/email/login", response_model=AuthResultResponse)
def login_with_email(
    body: EmailCodeVerifyRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> JSONResponse:
    _ensure_email_auth_enabled()
    user, decision, session = auth_service.login_with_email(body.email, body.code)
    payload = {
        "result": decision.result,
        "reason": decision.reason,
        "user": _serialize_user(user).model_dump(mode="json") if user else None,
        "session": None,
    }
    status_code = 200 if session and user else 403
    if session:
        payload["session"] = SessionResponse(
            token=session.token,
            expires_at=session.expires_at,
        ).model_dump(mode="json")

    response = JSONResponse(payload, status_code=status_code)
    if session:
        _set_session_cookie(response, session.token)
    else:
        response.delete_cookie(settings.session_cookie_name)
    return response


@router.post("/email/request-code", response_model=EmailCodeDispatchResponse)
def request_email_code(
    body: EmailCodeRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> JSONResponse:
    _ensure_email_auth_enabled()
    result = auth_service.request_email_code(body.email, body.purpose)
    status_code = 200
    if result.result == "reject":
        status_code = 400
    elif result.result == "cooldown":
        status_code = 429
    elif result.result == "error":
        status_code = 502

    return JSONResponse(
        {
            "result": result.result,
            "reason": result.reason,
            "purpose": result.purpose,
            "expires_in_seconds": result.expires_in_seconds,
            "cooldown_seconds": result.cooldown_seconds,
            "debug_code": result.debug_code,
        },
        status_code=status_code,
    )


@router.post("/email/reveal-code", response_model=EmailCodeRevealResponse)
def reveal_email_code(
    body: EmailCodeRevealRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> JSONResponse:
    _ensure_email_auth_enabled()
    result = auth_service.reveal_latest_email_code(body.email, body.purpose)
    status_code = 200
    if result.result in {"disabled", "reject"}:
        status_code = 400
    elif result.result in {"not_found", "expired"}:
        status_code = 404

    return JSONResponse(
        {
            "result": result.result,
            "reason": result.reason,
            "code": result.code,
            "expires_in_seconds": result.expires_in_seconds,
        },
        status_code=status_code,
    )


def _ensure_email_auth_enabled() -> None:
    if not settings.email_auth_enabled:
        raise HTTPException(status_code=403, detail="当前环境未启用邮箱验证码认证。")


@router.get("/me", response_model=CurrentUserResponse)
def get_current_user(
    auth_service: AuthService = Depends(get_auth_service),
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> CurrentUserResponse:
    token = _extract_session_token(authorization, session_cookie)
    if token is None:
        raise HTTPException(status_code=401, detail="缺少应用会话。")

    user = auth_service.get_current_user(token)
    if user is None:
        raise HTTPException(status_code=401, detail="应用会话无效或已过期。")
    if user.status == "blocked":
        raise HTTPException(status_code=403, detail="账号已被封禁。")

    return CurrentUserResponse(user=_serialize_user(user))


@router.post("/logout", status_code=204, response_class=Response, response_model=None)
def logout(
    auth_service: AuthService = Depends(get_auth_service),
    authorization: str | None = Header(default=None),
    session_cookie: str | None = Cookie(default=None, alias=settings.session_cookie_name),
) -> Response:
    token = _extract_session_token(authorization, session_cookie)
    if token:
        auth_service.logout(token)

    response = Response(status_code=204)
    response.delete_cookie(settings.session_cookie_name)
    return response


def _extract_session_token(authorization: str | None, session_cookie: str | None) -> str | None:
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return session_cookie


def _serialize_user(user: UserRecord) -> UserProfileResponse:
    return UserProfileResponse(
        user_id=user.user_id,
        email=user.email,
        name=user.name,
        avatar_url=user.avatar_url,
        status=user.status,
        role=user.role,
        default_role=user.default_role,
        agent_access=user.agent_access,
        auth_provider=user.auth_provider,
        hosted_domain=user.hosted_domain,
        department_name=user.department_name,
        access_group=user.access_group,
        created_at=user.created_at,
        first_login_at=user.first_login_at,
        last_login_at=user.last_login_at,
    )


def _google_callback_error_response(request: Request, message: str) -> JSONResponse | RedirectResponse:
    if _wants_browser_redirect(request):
        response = _google_callback_redirect_response(message)
        response.delete_cookie(settings.oauth_state_cookie_name)
        response.delete_cookie(settings.session_cookie_name)
        return response
    raise HTTPException(status_code=400, detail=message)


def _google_callback_redirect_response(error_message: str = "") -> RedirectResponse:
    return RedirectResponse(url=_frontend_redirect_url(error_message), status_code=303)


def _wants_browser_redirect(request: Request) -> bool:
    accept = request.headers.get("accept", "")
    return "text/html" in accept


def _frontend_redirect_url(error_message: str = "") -> str:
    target = settings.frontend_app_url or "/"
    parts = urlsplit(target)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if error_message:
        query["auth_error"] = error_message
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), parts.fragment))


def _set_oauth_state_cookie(response: Response, state: str) -> None:
    response.set_cookie(
        key=settings.oauth_state_cookie_name,
        value=state,
        httponly=True,
        max_age=settings.oauth_state_ttl_seconds,
        samesite="lax",
        secure=settings.auth_cookie_secure,
    )


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.session_cookie_name,
        value=token,
        httponly=True,
        max_age=settings.session_ttl_seconds,
        samesite="lax",
        secure=settings.auth_cookie_secure,
    )


def _set_admin_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=settings.admin_session_cookie_name,
        value=token,
        httponly=True,
        max_age=settings.session_ttl_seconds,
        samesite="lax",
        secure=settings.auth_cookie_secure,
    )


def _admin_redirect_url(error_message: str = "") -> str:
    target = settings.admin_frontend_url or "/admin/"
    parts = urlsplit(target)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if error_message:
        query["auth_error"] = error_message
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/admin/", urlencode(query), parts.fragment))


@admin_auth_router.get("/google/login")
def admin_google_login(
    oauth_state_store: InMemoryOAuthStateStore = Depends(get_oauth_state_store),
    google_oauth_service: GoogleOAuthService = Depends(get_google_oauth_service),
) -> RedirectResponse:
    state = oauth_state_store.issue()
    try:
        authorization_url = google_oauth_service.build_authorization_url(
            state, redirect_uri=settings.admin_google_redirect_uri
        )
    except GoogleOAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    response = RedirectResponse(url=authorization_url, status_code=307)
    response.set_cookie(
        key=settings.admin_oauth_state_cookie_name,
        value=state,
        httponly=True,
        max_age=settings.oauth_state_ttl_seconds,
        samesite="lax",
        secure=settings.auth_cookie_secure,
    )
    return response


@admin_auth_router.get("/google/callback")
def admin_google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    auth_service: AuthService = Depends(get_auth_service),
    oauth_state_store: InMemoryOAuthStateStore = Depends(get_oauth_state_store),
    google_oauth_service: GoogleOAuthService = Depends(get_google_oauth_service),
    admin_oauth_state_cookie: str | None = Cookie(default=None, alias=settings.admin_oauth_state_cookie_name),
) -> RedirectResponse:
    def error_redirect(msg: str) -> RedirectResponse:
        resp = RedirectResponse(url=_admin_redirect_url(msg), status_code=303)
        resp.delete_cookie(settings.admin_oauth_state_cookie_name)
        resp.delete_cookie(settings.admin_session_cookie_name)
        return resp

    if error:
        return error_redirect("你已取消 Google 授权。" if error == "access_denied" else f"Google 授权失败：{error}")
    if not code or not state:
        return error_redirect("Google OAuth 回调参数不完整。")
    if not oauth_state_store.validate_and_consume(state, admin_oauth_state_cookie):
        return error_redirect("Google OAuth state 校验失败。")

    try:
        identity = google_oauth_service.exchange_code_for_identity(
            code, redirect_uri=settings.admin_google_redirect_uri
        )
    except GoogleOAuthError as exc:
        return error_redirect(exc.message)

    user, token_or_error = auth_service.finalize_admin_google_login(identity)
    if user is None:
        return error_redirect(token_or_error)

    response = RedirectResponse(url=_admin_redirect_url(), status_code=303)
    response.delete_cookie(settings.admin_oauth_state_cookie_name)
    _set_admin_session_cookie(response, token_or_error)
    return response


@admin_auth_router.get("/me", response_model=CurrentUserResponse)
def admin_get_current_user(
    auth_service: AuthService = Depends(get_auth_service),
    admin_session_cookie: str | None = Cookie(default=None, alias=settings.admin_session_cookie_name),
) -> CurrentUserResponse:
    if admin_session_cookie is None:
        raise HTTPException(status_code=401, detail="缺少管理员会话。")
    user = auth_service.get_current_admin(admin_session_cookie)
    if user is None:
        raise HTTPException(status_code=401, detail="管理员会话无效或已过期。")
    return CurrentUserResponse(user=_serialize_user(user))


@admin_auth_router.post("/email/login", response_model=CurrentUserResponse)
def admin_login_with_email(
    body: EmailCodeVerifyRequest,
    auth_service: AuthService = Depends(get_auth_service),
) -> JSONResponse:
    user, decision, _session = auth_service.login_with_email(body.email, body.code)
    if user is None:
        raise HTTPException(status_code=403, detail=decision.reason)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="该账号没有管理员权限。")
    if user.status == "blocked":
        raise HTTPException(status_code=403, detail="该账号已被封禁。")

    admin_session = auth_service._create_admin_session(user.user_id)
    response = JSONResponse({"user": _serialize_user(user).model_dump(mode="json")})
    _set_admin_session_cookie(response, admin_session.token)
    return response


@admin_auth_router.post("/logout", status_code=204, response_class=Response, response_model=None)
def admin_logout(
    auth_service: AuthService = Depends(get_auth_service),
    admin_session_cookie: str | None = Cookie(default=None, alias=settings.admin_session_cookie_name),
) -> Response:
    if admin_session_cookie:
        auth_service.logout(admin_session_cookie)
    response = Response(status_code=204)
    response.delete_cookie(settings.admin_session_cookie_name)
    return response
