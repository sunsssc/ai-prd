from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.business.security_scan import SecurityScanJob, SecurityScanService
from app.core.config import settings
from app.core.dependencies import get_security_scan_service
from app.schemas.security_scan import (
    SecurityScanAcceptedResponse,
    SecurityScanRequest,
    SecurityScanResult,
    SecurityScanStatusResponse,
)
from app.utils.hmac_auth import verify_sha256_hmac

router = APIRouter(prefix="/security", tags=["security-scan"])


@router.post("/scan", status_code=status.HTTP_202_ACCEPTED, response_model=SecurityScanAcceptedResponse)
async def create_security_scan(
    request: Request,
    body: SecurityScanRequest,
    x_hub_signature_256: str | None = Header(default=None),
    service: SecurityScanService = Depends(get_security_scan_service),
) -> SecurityScanAcceptedResponse:
    _ensure_scan_configured()
    raw_body = await request.body()
    if not verify_sha256_hmac(
        secret=settings.security_scan_shared_secret,
        payload=raw_body,
        signature_header=x_hub_signature_256,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="安全扫描签名无效。")
    try:
        job = service.enqueue(
            path=body.path,
            url=body.url,
            context_json=body.context.model_dump_json(),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    if job.status == "pending":
        asyncio.create_task(service.run_job(job))
    return SecurityScanAcceptedResponse(jobId=job.job_id)


@router.get("/scan/{job_id}", response_model=SecurityScanStatusResponse, response_model_exclude_none=True)
def get_security_scan(
    job_id: str,
    x_hub_signature_256: str | None = Header(default=None),
    service: SecurityScanService = Depends(get_security_scan_service),
) -> SecurityScanStatusResponse:
    _ensure_scan_configured()
    if not verify_sha256_hmac(
        secret=settings.security_scan_shared_secret,
        payload=job_id.encode("utf-8"),
        signature_header=x_hub_signature_256,
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="安全扫描签名无效。")
    try:
        job = service.get_job(job_id=job_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    return _serialize_job(job)


def _ensure_scan_configured() -> None:
    if not settings.security_scan_shared_secret:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="SECURITY_SCAN_SHARED_SECRET 未配置。")


def _serialize_job(job: SecurityScanJob) -> SecurityScanStatusResponse:
    if job.status == "done":
        findings = json.loads(job.findings_json) if job.findings_json else []
        return SecurityScanStatusResponse(
            status="done",
            result=SecurityScanResult(
                verdict=job.verdict or "safe",
                reason=job.reason or "",
                findings=findings,
            ),
        )
    if job.status == "failed":
        return SecurityScanStatusResponse(status="failed", error=job.error_message or "安全扫描任务失败。")
    return SecurityScanStatusResponse(status="running")
