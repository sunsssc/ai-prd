from __future__ import annotations

import httpx
import pytest

from app.integrations.llm.client import (
    AssistantLLMError,
    LLMMessage,
    OpenAICompatibleAssistantLLMClient,
)


@pytest.mark.anyio
async def test_openai_compatible_client_sends_vllm_thinking_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            del exc_type, exc, traceback

        async def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]):
            captured.update(url=url, headers=headers, json=json)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "优惠券规则梳理"}}]},
            )

    monkeypatch.setattr("app.integrations.llm.client.httpx.AsyncClient", FakeAsyncClient)
    client = OpenAICompatibleAssistantLLMClient(
        api_key="test-key",
        model="qwen3.6-27b-fp8",
        base_url="http://127.0.0.1:8003/",
        max_tokens=2048,
        timeout_seconds=300,
    )

    reply = await client.generate_reply(
        "只输出标题",
        [LLMMessage(role="user", content="请帮我梳理优惠券规则")],
    )

    assert reply == "优惠券规则梳理"
    assert captured["url"] == "http://127.0.0.1:8003/v1/chat/completions"
    assert captured["timeout"] == 300
    assert captured["headers"] == {
        "authorization": "Bearer test-key",
        "content-type": "application/json",
    }
    assert captured["json"] == {
        "model": "qwen3.6-27b-fp8",
        "messages": [
            {"role": "system", "content": "只输出标题"},
            {"role": "user", "content": "请帮我梳理优惠券规则"},
        ],
        "max_tokens": 2048,
        "chat_template_kwargs": {"enable_thinking": False},
    }


@pytest.mark.anyio
async def test_openai_compatible_client_rejects_malformed_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAsyncClient:
        def __init__(self, *, timeout: float) -> None:
            del timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback) -> None:
            del exc_type, exc, traceback

        async def post(self, url: str, *, headers: dict[str, str], json: dict[str, object]):
            del url, headers, json
            return httpx.Response(200, json={"choices": []})

    monkeypatch.setattr("app.integrations.llm.client.httpx.AsyncClient", FakeAsyncClient)
    client = OpenAICompatibleAssistantLLMClient(
        api_key="test-key",
        model="qwen3.6-27b-fp8",
        base_url="http://127.0.0.1:8003",
        max_tokens=2048,
        timeout_seconds=300,
    )

    with pytest.raises(AssistantLLMError, match="返回格式不正确"):
        await client.generate_reply("system", [LLMMessage(role="user", content="message")])
