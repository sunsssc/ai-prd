from __future__ import annotations

from pydantic import BaseModel, Field


class SecurityScanContext(BaseModel):
    name: str = ""
    description: str = ""
    tags: list[str] = Field(default_factory=list)


class SecurityScanRequest(BaseModel):
    path: str | None = None
    url: str | None = None
    context: SecurityScanContext = Field(default_factory=SecurityScanContext)


class SecurityScanAcceptedResponse(BaseModel):
    jobId: str


class SecurityScanResult(BaseModel):
    verdict: str
    reason: str
    findings: list[str] = Field(default_factory=list)


class SecurityScanStatusResponse(BaseModel):
    status: str
    result: SecurityScanResult | None = None
    error: str | None = None
