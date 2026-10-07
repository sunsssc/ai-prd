from __future__ import annotations

import json

import httpx
import pytest

from app.integrations.test_data_platform import (
    TestDataPlatformClient as PlatformClient,
    TestDataPlatformError as PlatformError,
)


@pytest.mark.anyio
async def test_client_uses_bearer_token_and_new_api_payloads() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/makedata/tasks":
            return httpx.Response(200, json={"tasks": [{"name": "adjust_user_asset"}]})
        if request.url.path == "/api/v1/users/assets/update":
            return httpx.Response(200, json={"run_id": 17, "status": "submitted"})
        if request.url.path == "/api/v1/users/assets/query":
            return httpx.Response(200, json={"status": "success", "assets": []})
        return httpx.Response(200, json={"run_id": 17, "status": "completed"})

    client = PlatformClient(
        base_url="https://testhelp.example/",
        api_token="secret-token",
        transport=httpx.MockTransport(handler),
    )

    task_catalog = await client.list_tasks()
    await client.execute(
        task="adjust_user_asset",
        environment="2",
        parameters={"identifier": "123", "asset": "USDT", "amount": "100"},
    )
    await client.query(
        operation="query_user_assets",
        environment="2",
        parameters={"identifier": "123", "account": "all"},
    )
    await client.get_request("17")

    assert [request.url.path for request in requests] == [
        "/api/v1/makedata/tasks",
        "/api/v1/users/assets/update",
        "/api/v1/users/assets/query",
        "/api/v1/makedata/requests/17",
    ]
    assert all(request.headers["authorization"] == "Bearer secret-token" for request in requests)
    task_names = {item["name"] for item in task_catalog["tasks"]}
    assert {"query_kyc_status", "query_user_assets", "adjust_user_asset"} <= task_names
    assert json.loads(requests[1].content) == {
        "identifier": "123",
        "asset": "USDT",
        "amount": "100",
        "env": "2",
    }
    assert json.loads(requests[2].content) == {
        "identifier": "123",
        "account": "all",
        "env": "2",
    }


@pytest.mark.anyio
async def test_execute_network_failure_is_ambiguous_and_not_safe_to_retry() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = PlatformClient(
        base_url="https://testhelp.example",
        api_token="secret-token",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(PlatformError) as exc_info:
        await client.execute(task="adjust_user_asset", environment="1", parameters={})

    assert exc_info.value.execution_state_unknown is True


@pytest.mark.anyio
async def test_list_tasks_http_error_is_not_ambiguous() -> None:
    client = PlatformClient(
        base_url="https://testhelp.example",
        api_token="secret-token",
        transport=httpx.MockTransport(lambda _request: httpx.Response(401, json={"detail": "invalid"})),
    )

    with pytest.raises(PlatformError) as exc_info:
        await client.list_tasks()

    assert exc_info.value.execution_state_unknown is False
    assert "HTTP 401" in str(exc_info.value)
