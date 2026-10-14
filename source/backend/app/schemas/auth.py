from datetime import datetime

from pydantic import BaseModel


class UserProfileResponse(BaseModel):
    user_id: str
    email: str
    name: str | None
    avatar_url: str | None
    status: str
    role: str
    default_role: str
    agent_access: str
    auth_provider: str
    hosted_domain: str | None
    department_name: str | None
    access_group: str | None
    created_at: datetime
    first_login_at: datetime
    last_login_at: datetime


class SessionResponse(BaseModel):
    token: str
    expires_at: datetime


class EmailAuthRequest(BaseModel):
    email: str
    name: str | None = None


class EmailCodeRequest(BaseModel):
    email: str
    purpose: str


class EmailCodeVerifyRequest(BaseModel):
    email: str
    code: str
    name: str | None = None


class EmailCodeDispatchResponse(BaseModel):
    result: str
    reason: str
    purpose: str | None = None
    expires_in_seconds: int | None = None
    cooldown_seconds: int | None = None
    debug_code: str | None = None


class EmailCodeRevealRequest(BaseModel):
    email: str
    purpose: str


class EmailCodeRevealResponse(BaseModel):
    result: str
    reason: str
    code: str | None = None
    expires_in_seconds: int | None = None


class GoogleAuthorizationUrlResponse(BaseModel):
    authorization_url: str


class AuthConfigResponse(BaseModel):
    email_auth_enabled: bool


class GoogleAccessTokenLoginRequest(BaseModel):
    access_token: str


class AuthResultResponse(BaseModel):
    result: str
    reason: str
    user: UserProfileResponse | None = None
    session: SessionResponse | None = None


class CurrentUserResponse(BaseModel):
    user: UserProfileResponse
