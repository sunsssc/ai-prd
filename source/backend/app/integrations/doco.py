from __future__ import annotations

import asyncio
from typing import Any

import httpx


class DocoError(RuntimeError):
    """Doco API 调用失败。"""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details
        self.request_id = request_id


class DocoClient:
    """Doco 知识库 REST 客户端（幂等创建 + 带版本写回）。

    写回遵循 Doco 黄金循环：先读最新 ETag，再带 If-Match 写；遇到 409 冲突
    重读最新版重试（最多 4 次写尝试，200/400/600ms 递增退避），绝不盲写覆盖。
    """

    _max_write_attempts = 4

    def __init__(
        self,
        *,
        base_url: str,
        api_token: str,
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token.strip()
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def list_knowledge_bases(self) -> object:
        return await self._request_json("GET", "/knowledge-bases")

    async def create_document(
        self,
        *,
        title: str,
        knowledge_base_id: int,
        content_markdown: str,
        idempotency_key: str,
    ) -> object:
        return await self._request_json(
            "POST",
            "/documents",
            json_body={
                "title": title,
                "knowledge_base_id": knowledge_base_id,
                "content": {"format": "markdown", "content": content_markdown},
            },
            headers={"Idempotency-Key": idempotency_key},
            write_request=True,
        )

    async def save_document(
        self,
        *,
        document_id: str,
        content_markdown: str,
    ) -> object:
        """整篇写回已有文档：读最新 ETag → If-Match 写，409 时重读重试。"""
        for attempt in range(self._max_write_attempts):
            etag = await self._current_etag(document_id)
            try:
                return await self._request_json(
                    "PUT",
                    f"/documents/{document_id}/content",
                    json_body={"format": "markdown", "content": content_markdown},
                    headers={"If-Match": etag},
                    write_request=True,
                )
            except DocoError as exc:
                if exc.status != 409 or attempt >= self._max_write_attempts - 1:
                    raise
                await asyncio.sleep(0.2 * (attempt + 1))
        raise DocoError("Doco 文档写回重试次数耗尽。")

    async def _current_etag(self, document_id: str) -> str:
        response = await self._request("GET", f"/documents/{document_id}/content?format=markdown")
        etag = response.headers.get("etag")
        if not etag:
            raise DocoError("Doco 文档读取响应缺少 ETag。", status=response.status_code)
        return etag

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        write_request: bool = False,
    ) -> object:
        response = await self._request(
            method,
            path,
            json_body=json_body,
            headers=headers,
            write_request=write_request,
        )
        try:
            return response.json()
        except ValueError as exc:
            raise DocoError(
                "Doco 返回了非 JSON 响应。",
                status=response.status_code,
                request_id=response.headers.get("x-request-id"),
            ) from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        write_request: bool = False,
    ) -> httpx.Response:
        if not self.api_token:
            raise DocoError("Doco API Token 未配置。")
        request_headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
            **(headers or {}),
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=request_headers,
                    json=json_body,
                )
        except httpx.RequestError as exc:
            raise DocoError(
                f"Doco 连接失败：{exc}",
                status=0 if write_request else None,
            ) from exc
        if response.status_code >= 400:
            self._raise_for_status(response)
        return response

    def _raise_for_status(self, response: httpx.Response) -> None:
        payload: object = None
        try:
            payload = response.json()
        except ValueError:
            pass
        error = payload.get("error") if isinstance(payload, dict) else None
        message = (
            error.get("message")
            if isinstance(error, dict) and error.get("message")
            else response.text.strip()[:1000]
        )
        raise DocoError(
            f"Doco 请求失败：HTTP {response.status_code} {message}",
            status=response.status_code,
            code=error.get("code") if isinstance(error, dict) else None,
            details=error.get("details") if isinstance(error, dict) else None,
            request_id=response.headers.get("x-request-id"),
        )
