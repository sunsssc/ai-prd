from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request, status

from app.core.config import BASE_DIR, settings
from app.utils.hmac_auth import verify_sha256_hmac

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/deploy", tags=["deploy"])


@router.post("/github/webhook")
async def receive_deploy_github_webhook(
    request: Request,
    x_github_event: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict[str, object]:
    _ensure_deploy_webhook_configured()
    body = await request.body()
    if not verify_sha256_hmac(secret=settings.deploy_webhook_secret, payload=body, signature_header=x_hub_signature_256):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="GitHub Webhook 签名无效。")
    if x_github_event != "push":
        return {"accepted": False, "reason": "ignored_event"}
    try:
        payload = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook JSON 无效。") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook JSON 必须是对象。")

    expected_ref = f"refs/heads/{settings.deploy_webhook_branch}"
    if payload.get("ref") != expected_ref:
        return {"accepted": False, "reason": "ignored_ref"}

    repository = payload.get("repository") if isinstance(payload.get("repository"), dict) else {}
    repo_full_name = str(repository.get("full_name") or "")
    if repo_full_name != settings.deploy_webhook_repository:
        return {"accepted": False, "reason": "ignored_repository"}

    pid = _start_deployment()
    return {
        "accepted": True,
        "pid": pid,
        "branch": settings.deploy_webhook_branch,
        "repository": settings.deploy_webhook_repository,
    }


def _ensure_deploy_webhook_configured() -> None:
    if not settings.deploy_webhook_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="DEPLOY_WEBHOOK_SECRET 未配置。")
    if not settings.deploy_webhook_repository:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="DEPLOY_WEBHOOK_REPOSITORY 未配置。")
    if not settings.deploy_webhook_branch:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="DEPLOY_WEBHOOK_BRANCH 未配置。")
    script_path = Path(settings.deploy_script_path)
    if not script_path.is_absolute():
        script_path = BASE_DIR / script_path
    if not script_path.is_file():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="部署脚本不存在。")


def _start_deployment() -> int:
    script_path = Path(settings.deploy_script_path)
    if not script_path.is_absolute():
        script_path = BASE_DIR / script_path
    log_path = Path(settings.deploy_log_path)
    if not log_path.is_absolute():
        log_path = BASE_DIR / log_path
    log_path.parent.mkdir(parents=True, exist_ok=True)

    args = ["bash", str(script_path)]
    if settings.deploy_skip_frontend:
        args.append("--skip-frontend")

    env = os.environ.copy()
    env["DEPLOY_BRANCH"] = settings.deploy_webhook_branch

    log_file = log_path.open("ab")
    try:
        process = subprocess.Popen(
            args,
            cwd=str(BASE_DIR),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except Exception:
        log_file.close()
        logger.exception("启动部署脚本失败: %s", script_path)
        raise
    log_file.close()
    return int(process.pid)
