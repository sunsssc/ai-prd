from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import httpx
import pytest

from app.api import deploy
from app.core.config import BASE_DIR, settings


def _signature(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _push_payload(*, ref: str = "refs/heads/main", repository: str = "example-org/ai-prd") -> bytes:
    return json.dumps(
        {
            "ref": ref,
            "repository": {
                "full_name": repository,
            },
        }
    ).encode("utf-8")


@pytest.fixture
def deploy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "deploy_webhook_secret", "test-secret")
    monkeypatch.setattr(settings, "deploy_webhook_repository", "example-org/ai-prd")
    monkeypatch.setattr(settings, "deploy_webhook_branch", "main")
    monkeypatch.setattr(settings, "deploy_script_path", str(BASE_DIR / "deployment/deploy-from-github.sh"))
    monkeypatch.setattr(settings, "deploy_log_path", str(BASE_DIR / "workspace/logs/test-deploy-webhook.log"))
    monkeypatch.setattr(settings, "deploy_skip_frontend", False)


@pytest.mark.anyio
async def test_deploy_webhook_starts_deploy_for_valid_push(
    client: httpx.AsyncClient,
    deploy_settings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class FakeProcess:
        pid = 12345

    def fake_popen(args: list[str], **_: object) -> FakeProcess:
        calls.append(args)
        return FakeProcess()

    monkeypatch.setattr(deploy.subprocess, "Popen", fake_popen)
    body = _push_payload()

    response = await client.post(
        "/api/deploy/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": _signature("test-secret", body),
        },
    )

    assert response.status_code == 200
    assert response.json()["accepted"] is True
    assert response.json()["pid"] == 12345
    assert calls == [["bash", str(Path(settings.deploy_script_path))]]


@pytest.mark.anyio
async def test_deploy_webhook_rejects_invalid_signature(
    client: httpx.AsyncClient,
    deploy_settings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(deploy.subprocess, "Popen", lambda *args, **kwargs: None)
    body = _push_payload()

    response = await client.post(
        "/api/deploy/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": "sha256=bad",
        },
    )

    assert response.status_code == 401


@pytest.mark.anyio
async def test_deploy_webhook_ignores_other_branch(
    client: httpx.AsyncClient,
    deploy_settings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(deploy.subprocess, "Popen", lambda *args, **kwargs: calls.append(args))
    body = _push_payload(ref="refs/heads/feature")

    response = await client.post(
        "/api/deploy/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": _signature("test-secret", body),
        },
    )

    assert response.status_code == 200
    assert response.json() == {"accepted": False, "reason": "ignored_ref"}
    assert calls == []


@pytest.mark.anyio
async def test_deploy_webhook_requires_secret(
    client: httpx.AsyncClient,
    deploy_settings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "deploy_webhook_secret", "")
    body = _push_payload()

    response = await client.post(
        "/api/deploy/github/webhook",
        content=body,
        headers={
            "X-GitHub-Event": "push",
            "X-Hub-Signature-256": _signature("test-secret", body),
        },
    )

    assert response.status_code == 503
