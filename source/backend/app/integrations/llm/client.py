from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import httpx


class AssistantLLMError(RuntimeError):
    """AI 助手 LLM 调用异常。"""


@dataclass(slots=True)
class LLMMessage:
    role: str
    content: str


class AssistantLLMClient(Protocol):
    async def generate_reply(self, system_prompt: str, messages: list[LLMMessage]) -> str: ...


class MockAssistantLLMClient:
    async def generate_reply(self, system_prompt: str, messages: list[LLMMessage]) -> str:
        del system_prompt
        latest_user_message = next((message.content for message in reversed(messages) if message.role == "user"), "")
        return (
            "已收到你的问题。\n\n"
            f"你的输入是：{latest_user_message}\n\n"
            "当前后端已经接入基础对话链路，但还没有配置真实的模型服务，所以这是本地 mock 回复。"
            "后续只要补充 LLM 配置，就可以无缝切换到真实模型。"
        )


class AnthropicAssistantLLMClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        max_tokens: int,
        timeout_seconds: float,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds

    async def generate_reply(self, system_prompt: str, messages: list[LLMMessage]) -> str:
        if not self.api_key:
            raise AssistantLLMError("尚未配置 Anthropic API Key。")

        filtered = [m for m in messages if m.content.strip()]
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system_prompt,
            "messages": [
                {
                    "role": message.role,
                    "content": [{"type": "text", "text": message.content}],
                }
                for message in filtered
            ],
        }

        headers = {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(f"{self.base_url}/v1/messages", headers=headers, json=payload)

        if response.status_code >= 400:
            raise AssistantLLMError(f"Anthropic 请求失败: {response.status_code} {response.text}")

        data = response.json()
        content_blocks = data.get("content", [])
        text_parts = [block.get("text", "") for block in content_blocks if block.get("type") == "text"]
        reply = "".join(text_parts).strip()
        if not reply:
            raise AssistantLLMError("模型未返回文本内容。")
        return reply


class OpenAICompatibleAssistantLLMClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str,
        max_tokens: int,
        timeout_seconds: float,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.max_tokens = max_tokens
        self.timeout_seconds = timeout_seconds

    async def generate_reply(self, system_prompt: str, messages: list[LLMMessage]) -> str:
        if not self.api_key:
            raise AssistantLLMError("尚未配置轻量模型 API Key。")

        filtered = [message for message in messages if message.content.strip()]
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                *[
                    {"role": message.role, "content": message.content}
                    for message in filtered
                ],
            ],
            "max_tokens": self.max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        headers = {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
        }

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                f"{self.base_url}/v1/chat/completions",
                headers=headers,
                json=payload,
            )

        if response.status_code >= 400:
            raise AssistantLLMError(f"轻量模型请求失败: {response.status_code} {response.text}")

        try:
            reply = response.json()["choices"][0]["message"]["content"].strip()
        except (IndexError, KeyError, TypeError, AttributeError, ValueError) as exc:
            raise AssistantLLMError("轻量模型返回格式不正确。") from exc
        if not reply:
            raise AssistantLLMError("轻量模型未返回文本内容。")
        return reply
