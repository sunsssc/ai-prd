from __future__ import annotations

import logging
from typing import Protocol

import httpx

from app.services.auth_models import UserRecord

logger = logging.getLogger(__name__)


class NotificationDeliveryError(Exception):
    pass


class SlackMessenger(Protocol):
    def send_direct_message_by_email(self, *, email: str, text: str) -> None: ...


class SlackWebApiMessenger:
    def __init__(self, *, bot_token: str, timeout_seconds: float = 5.0) -> None:
        self.bot_token = bot_token.strip()
        self.timeout_seconds = timeout_seconds

    def send_direct_message_by_email(self, *, email: str, text: str) -> None:
        if not self.bot_token:
            raise NotificationDeliveryError("SLACK_BOT_TOKEN 未配置，无法发送 Slack 通知。")

        user_id = self._lookup_user_id_by_email(email)
        self._post_api("chat.postMessage", {"channel": user_id, "text": text})

    def _lookup_user_id_by_email(self, email: str) -> str:
        payload = self._post_api("users.lookupByEmail", {"email": email})
        user = payload.get("user")
        if not isinstance(user, dict) or not isinstance(user.get("id"), str):
            raise NotificationDeliveryError(f"Slack 未返回用户 ID：{email}")
        return user["id"]

    def _post_api(self, method: str, payload: dict[str, str]) -> dict[str, object]:
        try:
            with httpx.Client(timeout=self.timeout_seconds) as client:
                response = client.post(
                    f"https://slack.com/api/{method}",
                    headers={
                        "Authorization": f"Bearer {self.bot_token}",
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                    data=payload,
                )
            data = response.json()
        except httpx.HTTPError as exc:  # pragma: no cover
            raise NotificationDeliveryError("Slack API 请求失败。") from exc
        except ValueError as exc:  # pragma: no cover
            raise NotificationDeliveryError("Slack API 返回了非 JSON 响应。") from exc

        if response.status_code >= 400:
            raise NotificationDeliveryError(f"Slack API HTTP {response.status_code}。")
        if not data.get("ok"):
            error = data.get("error")
            message = error if isinstance(error, str) else "unknown_error"
            details = []
            for key in ("needed", "provided"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    details.append(f"{key}={value}")
            if details:
                message = f"{message} ({', '.join(details)})"
            raise NotificationDeliveryError(f"Slack API 返回错误：{message}")
        return data


class AuthNotificationService:
    def __init__(
        self,
        *,
        messenger: SlackMessenger | None,
        admin_accounts: list[str],
        app_name: str,
        admin_frontend_url: str,
        frontend_app_url: str,
    ) -> None:
        self.messenger = messenger
        self.admin_accounts = [account.strip().lower() for account in admin_accounts if account.strip()]
        self.app_name = app_name
        self.admin_frontend_url = admin_frontend_url
        self.frontend_app_url = frontend_app_url

    def notify_agent_access_requested(self, user: UserRecord) -> None:
        if not self.admin_accounts:
            logger.info("NOTIFICATION_ADMIN_ACCOUNTS 未配置，跳过管理员审核通知。")
            return

        text = "\n".join(
            [
                f"{self.app_name} 收到新的 Agent 使用权限申请，等待审核。",
                f"用户：{self._display_user(user)}",
                f"审核入口：{self.admin_frontend_url}",
            ]
        )
        for account in self.admin_accounts:
            self._send_direct_message(account, text)

    def notify_agent_access_approved(self, user: UserRecord) -> None:
        text = "\n".join(
            [
                f"你的 {self.app_name} Agent 使用权限申请已通过。",
                f"登录入口：{self.frontend_app_url}",
            ]
        )
        self._send_direct_message(user.email, text)

    def notify_session_shared(self, *, owner: UserRecord, recipient: UserRecord, share_url: str) -> None:
        text = "\n".join(
            [
                f"{self._display_user(owner)} 向你共享了一个 {self.app_name} 会话。",
                f"打开会话：{share_url}",
            ]
        )
        self._send_direct_message(recipient.email, text)

    def _send_direct_message(self, email: str, text: str) -> None:
        if self.messenger is None:
            logger.info("SLACK_BOT_TOKEN 未配置，跳过 Slack 通知。")
            return

        try:
            self.messenger.send_direct_message_by_email(email=email, text=text)
        except NotificationDeliveryError:
            logger.exception("Slack 通知发送失败。recipient=%s", email)

    def _display_user(self, user: UserRecord) -> str:
        if user.name:
            return f"{user.name} <{user.email}>"
        return user.email
