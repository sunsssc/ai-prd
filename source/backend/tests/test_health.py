import httpx
import pytest


@pytest.mark.anyio
async def test_healthcheck(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
