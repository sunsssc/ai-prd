from __future__ import annotations

import smtplib
from email.message import EmailMessage
from typing import Protocol


class EmailDeliveryError(Exception):
    pass


class EmailSender(Protocol):
    def send_verification_code(self, *, email: str, code: str, purpose: str) -> None: ...
    def send_message(
        self,
        *,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        cc: tuple[str, ...] = (),
    ) -> None: ...


class DebugEmailSender:
    def send_verification_code(self, *, email: str, code: str, purpose: str) -> None:
        print(f"[email-debug] purpose={purpose} email={email} verification_code={code}")

    def send_message(
        self,
        *,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        cc: tuple[str, ...] = (),
    ) -> None:
        cc_text = f" cc={','.join(cc)}" if cc else ""
        print(f"[email-debug] to={to}{cc_text} subject={subject}\n{text}")
        if html:
            print(f"[email-debug-html]\n{html}")


class SMTPEmailSender:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool = False,
        use_tls: bool = True,
        sender_name: str,
        sender_address: str,
    ) -> None:
        if not host.strip():
            raise EmailDeliveryError("SMTP_HOST 未配置，无法发送验证码邮件。")
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.use_tls = use_tls
        self.sender_name = sender_name
        self.sender_address = sender_address

    def send_verification_code(self, *, email: str, code: str, purpose: str) -> None:
        self.send_message(
            to=email,
            subject=f"ai-prd 邮箱验证码 - {self._subject_suffix(purpose)}",
            text="\n".join(
                [
                    f"你好，",
                    "",
                    f"你的 ai-prd {self._body_purpose(purpose)}验证码是：{code}",
                    "验证码 5 分钟内有效，且只能使用一次。",
                    "如果这不是你的操作，请忽略这封邮件。",
                ]
            ),
        )

    def send_message(
        self,
        *,
        to: str,
        subject: str,
        text: str,
        html: str | None = None,
        cc: tuple[str, ...] = (),
    ) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = f"{self.sender_name} <{self.sender_address}>"
        message["To"] = to
        cc = tuple(dict.fromkeys(email.strip() for email in cc if email.strip()))
        if cc:
            message["Cc"] = ", ".join(cc)
        message.set_content(text)
        if html:
            message.add_alternative(html, subtype="html")

        try:
            if self.use_ssl:
                ctx = smtplib.SMTP_SSL(self.host, self.port, timeout=10)
            else:
                ctx = smtplib.SMTP(self.host, self.port, timeout=10)
            with ctx as smtp:
                if not self.use_ssl and self.use_tls:
                    smtp.starttls()
                if self.username:
                    smtp.login(self.username, self.password)
                smtp.send_message(message)
        except Exception as exc:  # pragma: no cover
            raise EmailDeliveryError("邮件发送失败。") from exc

    def _subject_suffix(self, purpose: str) -> str:
        return "注册" if purpose == "register" else "登录"

    def _body_purpose(self, purpose: str) -> str:
        return "注册" if purpose == "register" else "登录"
