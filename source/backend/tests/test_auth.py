from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from conftest import reset_dependency_overrides
from app.core.config import settings
from app.core.dependencies import (
    get_access_policy_service,
    get_auth_service,
    get_email_sender,
    get_auth_store,
    get_google_oauth_service,
    get_notification_service,
    get_oauth_state_store,
    get_workspace_access_service,
    get_workspace_browser_service,
)
from app.main import app
from app.services.auth_models import GoogleIdentity


class FakeGoogleOAuthService:
    def __init__(self, identity: GoogleIdentity) -> None:
        self.identity = identity

    def build_authorization_url(self, state: str) -> str:
        return f"https://accounts.google.com/o/oauth2/v2/auth?state={state}"

    def exchange_code_for_identity(self, code: str) -> GoogleIdentity:
        return self.identity

    def verify_access_token(self, access_token: str) -> GoogleIdentity:
        return self.identity


class FakeNotificationService:
    def __init__(self) -> None:
        self.requested_users: list[str] = []
        self.approved_users: list[str] = []

    def notify_agent_access_requested(self, user) -> None:
        self.requested_users.append(user.email)

    def notify_agent_access_approved(self, user) -> None:
        self.approved_users.append(user.email)


@pytest.fixture(autouse=True)
def reset_state(tmp_path: Path) -> None:
    get_google_oauth_service.cache_clear()
    get_access_policy_service.cache_clear()
    get_auth_service.cache_clear()
    get_auth_store.cache_clear()
    get_oauth_state_store.cache_clear()
    get_email_sender.cache_clear()
    get_notification_service.cache_clear()
    get_workspace_access_service.cache_clear()
    get_workspace_browser_service.cache_clear()
    reset_dependency_overrides()

    original_client_id = settings.google_client_id
    original_client_secret = settings.google_client_secret
    original_redirect_uri = settings.google_redirect_uri
    original_domains = list(settings.google_allowed_domains)
    original_departments = list(settings.google_allowed_departments)
    original_email_domain = settings.email_login_allowed_domain
    original_email_auth_enabled = settings.email_auth_enabled
    original_db_path = settings.auth_db_path
    original_delivery_mode = settings.email_delivery_mode
    original_debug_response = settings.email_verification_debug_response
    original_dev_shortcut = settings.email_verification_dev_shortcut_enabled
    original_app_env = settings.app_env
    original_frontend_app_url = settings.frontend_app_url
    original_admin_frontend_url = settings.admin_frontend_url
    original_admin_accounts = list(settings.auth_admin_accounts)
    original_auto_approved_accounts = list(settings.auth_auto_approved_accounts)
    original_notification_admin_accounts = list(settings.notification_admin_accounts)
    original_slack_bot_token = settings.slack_bot_token

    settings.google_client_id = "test-client-id"
    settings.google_client_secret = "test-client-secret"
    settings.google_redirect_uri = "http://testserver/api/auth/google/callback"
    settings.google_allowed_domains = ["company.com"]
    settings.google_allowed_departments = ["Product", "Engineering", "R&D", "RD"]
    settings.email_login_allowed_domain = "corp.test"
    settings.email_auth_enabled = True
    settings.auth_db_path = str(tmp_path / "auth.sqlite3")
    settings.email_delivery_mode = "debug"
    settings.email_verification_debug_response = True
    settings.email_verification_dev_shortcut_enabled = True
    settings.app_env = "development"
    settings.frontend_app_url = "http://frontend.test/"
    settings.admin_frontend_url = "http://frontend.test/admin/"
    settings.auth_admin_accounts = []
    settings.auth_auto_approved_accounts = []
    settings.notification_admin_accounts = ["admin@corp.test"]
    settings.slack_bot_token = ""

    yield

    settings.google_client_id = original_client_id
    settings.google_client_secret = original_client_secret
    settings.google_redirect_uri = original_redirect_uri
    settings.google_allowed_domains = original_domains
    settings.google_allowed_departments = original_departments
    settings.email_login_allowed_domain = original_email_domain
    settings.email_auth_enabled = original_email_auth_enabled
    settings.auth_db_path = original_db_path
    settings.email_delivery_mode = original_delivery_mode
    settings.email_verification_debug_response = original_debug_response
    settings.email_verification_dev_shortcut_enabled = original_dev_shortcut
    settings.app_env = original_app_env
    settings.frontend_app_url = original_frontend_app_url
    settings.admin_frontend_url = original_admin_frontend_url
    settings.auth_admin_accounts = original_admin_accounts
    settings.auth_auto_approved_accounts = original_auto_approved_accounts
    settings.notification_admin_accounts = original_notification_admin_accounts
    settings.slack_bot_token = original_slack_bot_token
    reset_dependency_overrides()


@pytest.mark.anyio
async def test_google_login_redirect_sets_state_cookie(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/auth/google/login", follow_redirects=False)

    assert response.status_code == 307
    assert settings.oauth_state_cookie_name in response.cookies
    parsed = urlparse(response.headers["location"])
    query = parse_qs(parsed.query)
    assert query["state"][0] == response.cookies[settings.oauth_state_cookie_name]


@pytest.mark.anyio
async def test_google_callback_allows_authorized_user(client: httpx.AsyncClient) -> None:
    app.dependency_overrides[get_google_oauth_service] = lambda: FakeGoogleOAuthService(
        GoogleIdentity(
            google_user_id="google-123",
            email="alice@company.com",
            email_verified=True,
            name="Alice",
            picture="https://example.com/avatar.png",
            hosted_domain="company.com",
            department_candidates=["Product Management"],
        )
    )

    login_response = await client.get("/api/auth/google/url")
    state = login_response.cookies[settings.oauth_state_cookie_name]
    client.cookies.set(settings.oauth_state_cookie_name, state)

    callback_response = await client.get(
        "/api/auth/google/callback",
        params={"code": "auth-code", "state": state},
    )

    assert callback_response.status_code == 200
    payload = callback_response.json()
    assert payload["result"] == "allow"
    assert payload["user"]["email"] == "alice@company.com"
    assert payload["user"]["auth_provider"] == "google"
    assert payload["user"]["access_group"] == "product"
    assert payload["session"]["token"]
    assert payload["session"]["token"].count(".") == 2


@pytest.mark.anyio
async def test_google_callback_redirects_browser_to_frontend(client: httpx.AsyncClient) -> None:
    app.dependency_overrides[get_google_oauth_service] = lambda: FakeGoogleOAuthService(
        GoogleIdentity(
            google_user_id="google-browser",
            email="browser@company.com",
            email_verified=True,
            name="Browser User",
            picture=None,
            hosted_domain="company.com",
            department_candidates=["Product Management"],
        )
    )

    login_response = await client.get("/api/auth/google/url")
    state = login_response.cookies[settings.oauth_state_cookie_name]
    client.cookies.set(settings.oauth_state_cookie_name, state)

    callback_response = await client.get(
        "/api/auth/google/callback",
        params={"code": "auth-code", "state": state},
        headers={"accept": "text/html"},
        follow_redirects=False,
    )

    assert callback_response.status_code == 303
    assert callback_response.headers["location"] == "http://frontend.test/"
    assert settings.session_cookie_name in callback_response.cookies


@pytest.mark.anyio
async def test_google_access_token_login_allows_authorized_user(client: httpx.AsyncClient) -> None:
    app.dependency_overrides[get_google_oauth_service] = lambda: FakeGoogleOAuthService(
        GoogleIdentity(
            google_user_id="google-456",
            email="bob@company.com",
            email_verified=True,
            name="Bob",
            picture=None,
            hosted_domain="company.com",
            department_candidates=["Engineering"],
        )
    )

    response = await client.post("/api/auth/google/login", json={"access_token": "google-access-token"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["result"] == "allow"
    assert payload["user"]["email"] == "bob@company.com"
    assert payload["user"]["access_group"] == "engineering"
    assert payload["session"]["token"].count(".") == 2

    client.cookies.set(settings.session_cookie_name, payload["session"]["token"])
    me_response = await client.get("/api/auth/me")
    assert me_response.status_code == 200
    assert me_response.json()["user"]["email"] == "bob@company.com"


@pytest.mark.anyio
async def test_google_access_token_login_allows_explicit_external_account(client: httpx.AsyncClient) -> None:
    settings.auth_auto_approved_accounts = ["guest@gmail.com"]
    app.dependency_overrides[get_google_oauth_service] = lambda: FakeGoogleOAuthService(
        GoogleIdentity(
            google_user_id="google-external-guest",
            email="guest@gmail.com",
            email_verified=True,
            name="External Guest",
            picture=None,
            hosted_domain=None,
            department_candidates=[],
        )
    )

    response = await client.post("/api/auth/google/login", json={"access_token": "google-access-token"})

    assert response.status_code == 200
    payload = response.json()
    assert payload["result"] == "allow"
    assert payload["user"]["email"] == "guest@gmail.com"
    assert payload["user"]["status"] == "active"
    assert payload["user"]["agent_access"] == "active"
    assert payload["user"]["access_group"] == "member"
    assert payload["session"]["token"].count(".") == 2


@pytest.mark.anyio
async def test_email_register_and_login_persist_user(client: httpx.AsyncClient) -> None:
    send_register_code = await client.post(
        "/api/auth/email/request-code",
        json={"email": "demo@corp.test", "purpose": "auto"},
    )
    assert send_register_code.json()["purpose"] == "register"
    register_code = send_register_code.json()["debug_code"]

    register_response = await client.post(
        "/api/auth/email/register",
        json={"email": "demo@corp.test", "name": "Demo User", "code": register_code},
    )

    assert register_response.status_code == 201
    register_payload = register_response.json()
    assert register_payload["result"] == "allow"
    assert register_payload["user"]["email"] == "demo@corp.test"
    assert register_payload["user"]["auth_provider"] == "email"
    assert register_payload["user"]["status"] == "active"
    assert register_payload["user"]["agent_access"] == "none"

    client.cookies.clear()
    send_login_code = await client.post(
        "/api/auth/email/request-code",
        json={"email": "demo@corp.test", "purpose": "auto"},
    )
    assert send_login_code.json()["purpose"] == "login"
    login_code = send_login_code.json()["debug_code"]
    login_response = await client.post(
        "/api/auth/email/login",
        json={"email": "demo@corp.test", "code": login_code},
    )

    assert login_response.status_code == 200
    login_payload = login_response.json()
    assert login_payload["result"] == "allow"
    assert login_payload["user"]["user_id"] == register_payload["user"]["user_id"]
    assert login_payload["session"]["token"]


@pytest.mark.anyio
async def test_register_does_not_notify_admins(client: httpx.AsyncClient) -> None:
    fake_notifications = FakeNotificationService()
    app.dependency_overrides[get_notification_service] = lambda: fake_notifications

    send_register_code = await client.post(
        "/api/auth/email/request-code",
        json={"email": "notify-register@corp.test", "purpose": "register"},
    )
    register_code = send_register_code.json()["debug_code"]

    register_response = await client.post(
        "/api/auth/email/register",
        json={"email": "notify-register@corp.test", "name": "Notify Register", "code": register_code},
    )

    assert register_response.status_code == 201
    assert fake_notifications.requested_users == []
    assert fake_notifications.approved_users == []


@pytest.mark.anyio
async def test_agent_access_request_notifies_admins(client: httpx.AsyncClient) -> None:
    fake_notifications = FakeNotificationService()
    app.dependency_overrides[get_notification_service] = lambda: fake_notifications

    send_register_code = await client.post(
        "/api/auth/email/request-code",
        json={"email": "notify-request@corp.test", "purpose": "register"},
    )
    register_code = send_register_code.json()["debug_code"]
    register_response = await client.post(
        "/api/auth/email/register",
        json={"email": "notify-request@corp.test", "name": "Notify Request", "code": register_code},
    )
    assert register_response.status_code == 201
    client.cookies.set(settings.session_cookie_name, register_response.json()["session"]["token"])

    request_response = await client.post("/api/agent-access/request", json={"reason": "需要使用 Agent"})

    assert request_response.status_code == 200
    payload = request_response.json()
    assert payload["agent_access"] == "pending"
    assert payload["latest_request"]["status"] == "pending"
    assert fake_notifications.requested_users == ["notify-request@corp.test"]
    assert fake_notifications.approved_users == []


@pytest.mark.anyio
async def test_google_first_login_activates_account_without_agent_access(client: httpx.AsyncClient) -> None:
    fake_notifications = FakeNotificationService()
    app.dependency_overrides[get_notification_service] = lambda: fake_notifications
    app.dependency_overrides[get_google_oauth_service] = lambda: FakeGoogleOAuthService(
        GoogleIdentity(
            google_user_id="google-notify",
            email="notify-google@company.com",
            email_verified=True,
            name="Notify Google",
            picture=None,
            hosted_domain="company.com",
            department_candidates=["Product Management"],
        )
    )

    response = await client.post("/api/auth/google/login", json={"access_token": "google-access-token"})

    assert response.status_code == 200
    assert response.json()["user"]["status"] == "active"
    assert response.json()["user"]["agent_access"] == "none"
    assert fake_notifications.requested_users == []
    assert fake_notifications.approved_users == []


@pytest.mark.anyio
async def test_google_rejected_identity_does_not_notify_admins(client: httpx.AsyncClient) -> None:
    fake_notifications = FakeNotificationService()
    app.dependency_overrides[get_notification_service] = lambda: fake_notifications
    app.dependency_overrides[get_google_oauth_service] = lambda: FakeGoogleOAuthService(
        GoogleIdentity(
            google_user_id="google-rejected",
            email="rejected@company.com",
            email_verified=True,
            name="Rejected Google",
            picture=None,
            hosted_domain="company.com",
            department_candidates=["Sales"],
        )
    )

    response = await client.post("/api/auth/google/login", json={"access_token": "google-access-token"})

    assert response.status_code == 403
    assert fake_notifications.requested_users == []
    assert fake_notifications.approved_users == []


async def _register_and_login(client: httpx.AsyncClient, email: str, name: str) -> str:
    """注册一个新用户并返回其 user_id，同时把会话 cookie 设置到 client 上。"""
    send_code = await client.post(
        "/api/auth/email/request-code",
        json={"email": email, "purpose": "register"},
    )
    code = send_code.json()["debug_code"]
    register_response = await client.post(
        "/api/auth/email/register",
        json={"email": email, "name": name, "code": code},
    )
    assert register_response.status_code == 201
    payload = register_response.json()
    client.cookies.set(settings.session_cookie_name, payload["session"]["token"])
    return payload["user"]["user_id"]


def _login_admin(client: httpx.AsyncClient) -> None:
    auth_store = get_auth_store()
    admin = auth_store.create_email_user(
        "admin@corp.test",
        "Admin",
        initial_role="admin",
    )
    admin_session = get_auth_service()._create_admin_session(admin.user_id)
    client.cookies.set(settings.admin_session_cookie_name, admin_session.token)


@pytest.mark.anyio
async def test_admin_approve_agent_access_grants_permission_and_default_org(client: httpx.AsyncClient) -> None:
    fake_notifications = FakeNotificationService()
    app.dependency_overrides[get_notification_service] = lambda: fake_notifications

    user_id = await _register_and_login(client, "approve-me@corp.test", "Approve Me")
    request_response = await client.post("/api/agent-access/request", json={"reason": "需要使用 Agent"})
    request_id = request_response.json()["latest_request"]["request_id"]

    _login_admin(client)
    pending_response = await client.get("/api/admin/agent-access/requests")
    assert pending_response.status_code == 200
    assert [item["request_id"] for item in pending_response.json()] == [request_id]
    assert pending_response.json()[0]["user_email"] == "approve-me@corp.test"

    approve_response = await client.post(
        f"/api/admin/agent-access/requests/{request_id}/approve",
        json={"review_comment": "欢迎使用"},
    )

    assert approve_response.status_code == 200
    assert approve_response.json()["status"] == "approved"
    assert fake_notifications.requested_users == ["approve-me@corp.test"]
    assert fake_notifications.approved_users == ["approve-me@corp.test"]

    me_response = await client.get("/api/auth/me")
    assert me_response.json()["user"]["agent_access"] == "active"
    memberships_response = await client.get(f"/api/admin/users/{user_id}/organization-memberships")
    assert memberships_response.status_code == 200
    assert [item["organization_key"] for item in memberships_response.json()] == [
        settings.default_organization_key
    ]
    # 开通后 Agent 级接口可用
    workspace_response = await client.get("/api/organizations")
    assert workspace_response.status_code == 200


@pytest.mark.anyio
async def test_admin_review_rejects_handled_request(client: httpx.AsyncClient) -> None:
    fake_notifications = FakeNotificationService()
    app.dependency_overrides[get_notification_service] = lambda: fake_notifications

    await _register_and_login(client, "handled@corp.test", "Handled")
    request_response = await client.post("/api/agent-access/request", json={"reason": "需要使用 Agent"})
    request_id = request_response.json()["latest_request"]["request_id"]

    _login_admin(client)
    approve_response = await client.post(f"/api/admin/agent-access/requests/{request_id}/approve", json={})
    repeat_response = await client.post(f"/api/admin/agent-access/requests/{request_id}/approve", json={})

    assert approve_response.status_code == 200
    assert repeat_response.status_code == 400
    assert repeat_response.json()["detail"] == "该申请已处理。"
    assert fake_notifications.approved_users == ["handled@corp.test"]


@pytest.mark.anyio
async def test_admin_reject_agent_access_keeps_account_usable(client: httpx.AsyncClient) -> None:
    user_id = await _register_and_login(client, "reject-me@corp.test", "Reject Me")
    request_response = await client.post("/api/agent-access/request", json={"reason": "需要使用 Agent"})
    request_id = request_response.json()["latest_request"]["request_id"]

    _login_admin(client)
    reject_response = await client.post(
        f"/api/admin/agent-access/requests/{request_id}/reject",
        json={"review_comment": "岗位不需要使用 Agent"},
    )

    assert reject_response.status_code == 200
    assert reject_response.json()["status"] == "rejected"
    assert reject_response.json()["review_comment"] == "岗位不需要使用 Agent"

    # 账号本身仍然可用：能访问 /api/auth/me，但 Agent 级接口被拒
    me_response = await client.get("/api/auth/me")
    assert me_response.status_code == 200
    assert me_response.json()["user"]["agent_access"] == "rejected"
    workspace_response = await client.get("/api/organizations")
    assert workspace_response.status_code == 403
    assert workspace_response.json()["detail"] == "Agent 使用权限申请未通过。"

    # 被拒绝后可以重新申请
    reapply_response = await client.post("/api/agent-access/request", json={"reason": "岗位调整，需要重新申请"})
    assert reapply_response.status_code == 200
    assert reapply_response.json()["agent_access"] == "pending"


@pytest.mark.anyio
async def test_agent_gate_blocks_unapproved_accounts(client: httpx.AsyncClient) -> None:
    user_id = await _register_and_login(client, "no-access@corp.test", "No Access")

    # 未申请 Agent 权限：账号级接口可用，Agent 级接口被拒
    me_response = await client.get("/api/auth/me")
    assert me_response.status_code == 200
    workspace_response = await client.get("/api/organizations")
    assert workspace_response.status_code == 403
    assert workspace_response.json()["detail"] == "尚未获得 Agent 使用权限，请先提交申请。"

    status_response = await client.get("/api/agent-access/me")
    assert status_response.status_code == 200
    assert status_response.json()["agent_access"] == "none"
    assert status_response.json()["latest_request"] is None

    # 封禁后连账号级接口也被拒
    _login_admin(client)
    block_response = await client.post(f"/api/admin/users/{user_id}/block")
    assert block_response.status_code == 200
    blocked_me_response = await client.get("/api/auth/me")
    assert blocked_me_response.status_code == 403
    assert blocked_me_response.json()["detail"] == "账号已被封禁。"


@pytest.mark.anyio
async def test_admin_set_user_agent_access_grant_and_revoke(client: httpx.AsyncClient) -> None:
    fake_notifications = FakeNotificationService()
    app.dependency_overrides[get_notification_service] = lambda: fake_notifications

    user_id = await _register_and_login(client, "direct-grant@corp.test", "Direct Grant")
    _login_admin(client)

    grant_response = await client.post(
        f"/api/admin/users/{user_id}/agent-access",
        json={"agent_access": "active"},
    )
    assert grant_response.status_code == 200
    assert grant_response.json()["agent_access"] == "active"
    # 直接开通会加入默认组织，但不发用户通知
    assert [item["organization_key"] for item in grant_response.json()["organizations"]] == [
        settings.default_organization_key
    ]
    assert fake_notifications.approved_users == []

    workspace_response = await client.get("/api/organizations")
    assert workspace_response.status_code == 200

    revoke_response = await client.post(
        f"/api/admin/users/{user_id}/agent-access",
        json={"agent_access": "none"},
    )
    assert revoke_response.status_code == 200
    assert revoke_response.json()["agent_access"] == "none"
    revoked_workspace_response = await client.get("/api/organizations")
    assert revoked_workspace_response.status_code == 403


@pytest.mark.anyio
async def test_admin_users_include_active_organizations(client: httpx.AsyncClient) -> None:
    auth_store = get_auth_store()
    user = auth_store.create_email_user(
        "org-user@corp.test",
        "Org User",
        initial_status="active",
    )
    admin = auth_store.create_email_user(
        "admin@corp.test",
        "Admin",
        initial_status="active",
        initial_role="admin",
    )
    workspace_access = get_workspace_access_service()
    workspace_access.store.upsert_user_membership(
        user_id=user.user_id,
        organization_key="coinex",
        organization_role="member",
        source="test",
        is_default=True,
        status="active",
    )
    workspace_access.store.upsert_organization(organization_key="acme", display_name="Acme")
    workspace_access.store.upsert_user_membership(
        user_id=user.user_id,
        organization_key="acme",
        organization_role="member",
        source="test",
        status="disabled",
    )
    admin_session = get_auth_service()._create_admin_session(admin.user_id)
    client.cookies.set(settings.admin_session_cookie_name, admin_session.token)

    response = await client.get("/api/admin/users")

    assert response.status_code == 200
    org_user = next(item for item in response.json() if item["user_id"] == user.user_id)
    assert org_user["organizations"] == [
        {
            "organization_key": "coinex",
            "organization_role": "member",
            "is_default": True,
        }
    ]


@pytest.mark.anyio
async def test_email_register_rejects_non_vinotech_domain(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/auth/email/request-code",
        json={"email": "demo@gmail.com", "purpose": "register"},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["result"] == "reject"
    assert "corp.test" in payload["reason"]


@pytest.mark.anyio
async def test_email_auth_requires_explicit_environment_config(client: httpx.AsyncClient) -> None:
    settings.email_auth_enabled = False

    config_response = await client.get("/api/auth/config")
    request_response = await client.post(
        "/api/auth/email/request-code",
        json={"email": "demo@corp.test", "purpose": "auto"},
    )

    assert config_response.status_code == 200
    assert config_response.json() == {"email_auth_enabled": False}
    assert request_response.status_code == 403
    assert "未启用" in request_response.json()["detail"]


@pytest.mark.anyio
async def test_email_register_allows_explicit_external_account(client: httpx.AsyncClient) -> None:
    settings.auth_auto_approved_accounts = ["guest@gmail.com"]

    response = await client.post(
        "/api/auth/email/request-code",
        json={"email": "guest@gmail.com", "purpose": "register"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["result"] == "sent"
    assert payload["debug_code"]


@pytest.mark.anyio
async def test_email_login_requires_existing_registration(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/auth/email/request-code",
        json={"email": "newuser@corp.test", "purpose": "login"},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["result"] == "reject"
    assert "先注册" in payload["reason"]


@pytest.mark.anyio
async def test_get_current_user_from_session_cookie(client: httpx.AsyncClient) -> None:
    send_code = await client.post(
        "/api/auth/email/request-code",
        json={"email": "carol@corp.test", "purpose": "register"},
    )
    code = send_code.json()["debug_code"]
    register_response = await client.post(
        "/api/auth/email/register",
        json={"email": "carol@corp.test", "name": "Carol", "code": code},
    )

    session_token = register_response.json()["session"]["token"]
    client.cookies.set(settings.session_cookie_name, session_token)
    me_response = await client.get("/api/auth/me")

    assert me_response.status_code == 200
    assert me_response.json()["user"]["email"] == "carol@corp.test"


@pytest.mark.anyio
async def test_email_register_requires_valid_code(client: httpx.AsyncClient) -> None:
    await client.post(
        "/api/auth/email/request-code",
        json={"email": "invalid@corp.test", "purpose": "register"},
    )
    response = await client.post(
        "/api/auth/email/register",
        json={"email": "invalid@corp.test", "name": "Invalid", "code": "000000"},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload["result"] == "reject_identity"
    assert "验证码" in payload["reason"]


@pytest.mark.anyio
async def test_development_shortcut_can_reveal_latest_email_code(client: httpx.AsyncClient) -> None:
    send_code = await client.post(
        "/api/auth/email/request-code",
        json={"email": "shortcut@corp.test", "purpose": "register"},
    )

    issued_code = send_code.json()["debug_code"]
    reveal_response = await client.post(
        "/api/auth/email/reveal-code",
        json={"email": "shortcut@corp.test", "purpose": "register"},
    )

    assert reveal_response.status_code == 200
    payload = reveal_response.json()
    assert payload["result"] == "ok"
    assert payload["code"] == issued_code
    assert payload["expires_in_seconds"] is not None
