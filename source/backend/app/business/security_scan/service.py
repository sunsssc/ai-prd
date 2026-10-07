from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import socket
import uuid
from datetime import datetime, timedelta, timezone
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from app.business.security_scan.prompts import (
    SCAN_OUTPUT_SCHEMA,
    SECURITY_SCAN_SYSTEM_PROMPT,
    build_scan_prompt,
)
from app.business.security_scan.store import SecurityScanJob, SecurityScanStore
from app.integrations.agent_runtime import (
    AgentRuntimeClient,
    RuntimeDynamicTool,
    RuntimeDynamicToolResult,
    RuntimeSession,
    RuntimeWorkspacePlan,
)
from app.utils.archive import extract_zip_safely

logger = logging.getLogger(__name__)


class SecurityScanService:
    def __init__(
        self,
        *,
        store: SecurityScanStore,
        runtime_client: AgentRuntimeClient,
        scan_root: Path,
        allowed_roots: list[str],
        running_timeout_seconds: int,
        retention_days: int,
        max_concurrent: int,
        max_archive_bytes: int,
        max_extracted_bytes: int,
        max_files: int,
        url_max_fetches: int,
        url_max_response_bytes: int,
        url_fetch_timeout_seconds: float,
        url_max_redirects: int,
        url_max_text_chars: int,
    ) -> None:
        self.store = store
        self.runtime_client = runtime_client
        self._scan_root = scan_root
        self._allowed_roots = allowed_roots
        self._running_timeout_seconds = running_timeout_seconds
        self._retention_days = retention_days
        self._max_archive_bytes = max_archive_bytes
        self._max_extracted_bytes = max_extracted_bytes
        self._max_files = max_files
        self._url_max_fetches = url_max_fetches
        self._url_max_response_bytes = url_max_response_bytes
        self._url_fetch_timeout_seconds = url_fetch_timeout_seconds
        self._url_max_redirects = url_max_redirects
        self._url_max_text_chars = url_max_text_chars
        self._semaphore = asyncio.Semaphore(max_concurrent)

    # ------------------------------------------------------------------ 入队

    def enqueue(self, *, path: str | None, url: str | None, context_json: str | None) -> SecurityScanJob:
        if (path is None) == (url is None):
            raise ValueError("path 与 url 必须且只能提供其中一个。")
        if path is not None:
            target_type, target_value = "path", self._validate_path(path)
        else:
            assert url is not None
            target_type, target_value = "url", self._validate_url(url)
        job_id = uuid.uuid4().hex
        return self.store.create_job(
            job_id=job_id,
            target_type=target_type,
            target_value=target_value,
            context_json=context_json,
        )

    def _validate_path(self, raw_path: str) -> str:
        candidate = Path(raw_path).expanduser().resolve()
        if not candidate.exists():
            raise ValueError(f"目标路径不存在：{raw_path}")
        if not self._allowed_roots:
            raise ValueError("未配置 SECURITY_SCAN_ALLOWED_ROOTS，无法扫描本地路径。")
        for root in self._allowed_roots:
            root_path = Path(root).expanduser().resolve()
            if candidate == root_path or root_path in candidate.parents:
                break
        else:
            raise ValueError("目标路径不在允许扫描的目录白名单内。")
        if candidate.is_file() and candidate.stat().st_size > self._max_archive_bytes:
            raise ValueError(f"目标文件超过大小上限（{self._max_archive_bytes} 字节）。")
        return str(candidate)

    def _validate_url(self, raw_url: str) -> str:
        candidate = raw_url.strip()
        parts = urlsplit(candidate)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("仅支持 http/https 网址。")
        return candidate

    def get_job(self, *, job_id: str) -> SecurityScanJob:
        job = self.store.get_job(job_id=job_id)
        if job is None:
            raise FileNotFoundError("安全扫描任务不存在。")
        return job

    # ------------------------------------------------------------------ 任务循环

    async def job_loop(self, *, interval_seconds: int = 30, limit: int = 5) -> None:
        logger.info("安全扫描 worker 已启动，间隔=%ds", interval_seconds)
        while True:
            try:
                self.recover_stale_running_jobs()
                await self.process_pending_jobs(limit=limit)
                self.cleanup()
            except Exception:
                logger.exception("安全扫描 worker 出错，下次将继续重试")
            await asyncio.sleep(interval_seconds)

    async def process_pending_jobs(self, *, limit: int = 5) -> None:
        for job in self.store.list_pending_jobs(limit=limit):
            await self.run_job(job)

    def recover_stale_running_jobs(self) -> list[SecurityScanJob]:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._running_timeout_seconds)
        stale_jobs = self.store.fail_stale_running_jobs(
            cutoff_started_at=cutoff.isoformat(),
            error_message=f"安全扫描任务运行超过 {self._running_timeout_seconds} 秒，已标记为失败。",
        )
        for job in stale_jobs:
            logger.warning("安全扫描 running 任务超时回收: job_id=%s started_at=%s", job.job_id, job.started_at)
        return stale_jobs

    def cleanup(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._retention_days)
        purged = self.store.purge_jobs_older_than(cutoff_created_at=cutoff.isoformat())
        if purged:
            logger.info("安全扫描任务清理：删除 %d 条超过 %d 天的记录", len(purged), self._retention_days)
        if not self._scan_root.is_dir():
            return
        for entry in self._scan_root.iterdir():
            if not entry.is_dir():
                continue
            if self.store.get_job(job_id=entry.name) is None:
                shutil.rmtree(entry, ignore_errors=True)
                logger.info("安全扫描清理孤儿暂存目录: %s", entry)

    # ------------------------------------------------------------------ 任务执行

    async def run_job(self, job: SecurityScanJob) -> SecurityScanJob | None:
        if not self.store.mark_job_running(job.job_id):
            return None
        stage_dir: Path | None = None
        try:
            async with self._semaphore:
                shadow_root, stage_dir = self._stage_target(job)
                if stage_dir is not None:
                    self.store.update_stage_dir(job_id=job.job_id, stage_dir=str(stage_dir))
                dynamic_tools = (
                    [self._build_fetch_url_tool(job_id=job.job_id, fetch_dir=stage_dir / "fetches")]
                    if job.target_type == "url" and stage_dir is not None
                    else None
                )
                session = await self.runtime_client.create_or_resume_session(
                    runtime_session_id=None,
                    workspace_plan=RuntimeWorkspacePlan(
                        user_id="security-scan",
                        session_id=f"security-scan-{job.job_id}",
                        organization_key="security-scan",
                        sandbox_cwd="/",
                        host_shadow_root=str(shadow_root),
                        mounts=[],
                    ),
                    tool_allowlist=("Read", "Glob", "Grep", "LS", "Bash"),
                    dynamic_tools=dynamic_tools,
                    system_prompt=SECURITY_SCAN_SYSTEM_PROMPT,
                )
                context = json.loads(job.context_json) if job.context_json else {}
                payload = await self._send_json(
                    session=session,
                    prompt=build_scan_prompt(target_type=job.target_type, target_value=job.target_value, context=context),
                    metadata={
                        "task": "security_scan",
                        "job_id": job.job_id,
                        "output_schema": SCAN_OUTPUT_SCHEMA,
                    },
                )
                validated = self._validate_scan_payload(payload)
                self.store.complete_job(
                    job_id=job.job_id,
                    verdict=str(validated["verdict"]),
                    reason=str(validated["reason"]),
                    findings_json=json.dumps(validated["findings"], ensure_ascii=False),
                )
                logger.info("安全扫描任务完成: job_id=%s verdict=%s", job.job_id, validated["verdict"])
                return self.get_job(job_id=job.job_id)
        except Exception as exc:
            self.store.fail_job(job_id=job.job_id, error_message=str(exc))
            logger.exception("安全扫描任务失败: job_id=%s", job.job_id)
            return self.get_job(job_id=job.job_id)
        finally:
            if stage_dir is not None:
                shutil.rmtree(stage_dir, ignore_errors=True)

    def _stage_target(self, job: SecurityScanJob) -> tuple[Path, Path | None]:
        """准备扫描内容，返回 (shadow_root, stage_dir)。目录目标原地扫描，无需暂存。"""
        if job.target_type == "url":
            stage_dir = self._scan_root / job.job_id
            (stage_dir / "fetches").mkdir(parents=True)
            return stage_dir, stage_dir
        target = Path(job.target_value)
        if not target.exists():
            raise ValueError(f"目标路径不存在：{job.target_value}")
        if target.is_dir():
            return target, None
        stage_dir = self._scan_root / job.job_id
        stage_dir.mkdir(parents=True)
        if target.suffix.lower() == ".zip":
            extract_zip_safely(
                archive_path=target,
                dest_dir=stage_dir,
                max_files=self._max_files,
                max_total_bytes=self._max_extracted_bytes,
            )
        else:
            shutil.copy2(target, stage_dir / target.name)
        return stage_dir, stage_dir

    # ------------------------------------------------------------------ agent 交互

    async def _send_json(self, *, session: RuntimeSession, prompt: str, metadata: dict[str, object]) -> dict[str, Any]:
        chunks: list[str] = []
        final_message = ""
        async for event in self.runtime_client.send_message_stream(session=session, message=prompt, metadata=metadata):
            if event.type == "session" and str(event.data.get("session_id") or "").strip():
                session.session_id = str(event.data["session_id"])
            elif event.type == "delta" and isinstance(event.data.get("text"), str):
                chunks.append(str(event.data["text"]))
            elif event.type == "message" and isinstance(event.data.get("content"), str):
                final_message = str(event.data["content"])
            elif event.type == "complete" and not final_message and isinstance(event.data.get("result"), str):
                final_message = str(event.data["result"])
        raw = (final_message or "".join(chunks)).strip()
        if not raw:
            raise ValueError("Agent 未返回最终 JSON。")
        match = re.fullmatch(r"(?:```json\s*)?([\s\S]*?)(?:\s*```)?", raw)
        if match is None:
            raise ValueError("Agent 未返回合法 JSON。")
        payload = json.loads(match.group(1))
        if not isinstance(payload, dict):
            raise ValueError("Agent JSON 必须是 object。")
        return payload

    def _validate_scan_payload(self, payload: dict[str, object]) -> dict[str, object]:
        verdict = payload.get("verdict")
        if verdict not in {"safe", "unsafe"}:
            raise ValueError("verdict 必须是 safe 或 unsafe。")
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("reason 必须是非空字符串。")
        raw_findings = payload.get("findings") or []
        if not isinstance(raw_findings, list):
            raise ValueError("findings 必须是字符串数组。")
        findings = [str(item).strip()[:300] for item in raw_findings[:20] if str(item).strip()]
        return {"verdict": verdict, "reason": reason.strip()[:500], "findings": findings}

    # ------------------------------------------------------------------ url 抓取工具

    def _build_fetch_url_tool(self, *, job_id: str, fetch_dir: Path) -> RuntimeDynamicTool:
        state = {"fetches": 0}

        async def handler(arguments: dict[str, Any]) -> RuntimeDynamicToolResult:
            raw_url = str(arguments.get("url") or "").strip()
            try:
                if state["fetches"] >= self._url_max_fetches:
                    raise ValueError(f"抓取次数已达上限（{self._url_max_fetches} 次）。")
                state["fetches"] += 1
                text, status_code, content_type, saved_path = await self._fetch_url(raw_url, fetch_dir=fetch_dir, slug=state["fetches"])
                return RuntimeDynamicToolResult(
                    content=(
                        f"HTTP {status_code}，Content-Type: {content_type or '未知'}，完整内容已保存到 {saved_path}。\n"
                        f"内容预览（超过 {self._url_max_text_chars} 字符已截断）：\n{text}"
                    )
                )
            except Exception as exc:
                return RuntimeDynamicToolResult(content=f"抓取失败：{exc}", is_error=True)

        return RuntimeDynamicTool(
            name="fetch_url",
            description="抓取一个 http/https 网址的内容用于安全检测。参数：{\"url\": \"要抓取的网址\"}。有次数限制，只抓取与判定相关的链接。",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "要抓取的 http/https 网址"}},
                "required": ["url"],
            },
            handler=handler,
        )

    async def _fetch_url(self, raw_url: str, *, fetch_dir: Path, slug: int) -> tuple[str, int, str, Path]:
        current_url = raw_url
        final_status = 0
        final_content_type = ""
        body = b""
        for hop in range(self._url_max_redirects + 1):
            parts = urlsplit(current_url)
            if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
                raise ValueError("仅支持不带凭证的 http/https 网址。")
            await self._assert_public_host(parts.hostname)
            async with httpx.AsyncClient(follow_redirects=False, timeout=self._url_fetch_timeout_seconds) as client:
                async with client.stream("GET", current_url, headers={"User-Agent": "ai-prd-security-scan/1.0"}) as response:
                    final_status = response.status_code
                    final_content_type = response.headers.get("content-type", "")
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        if not location:
                            raise ValueError("重定向缺少 Location 头。")
                        if hop >= self._url_max_redirects:
                            raise ValueError(f"重定向次数超过上限（{self._url_max_redirects} 次）。")
                        current_url = urljoin(current_url, location)
                        continue
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        chunks.append(chunk)
                        total += len(chunk)
                        if total > self._url_max_response_bytes:
                            break
                    body = b"".join(chunks)[: self._url_max_response_bytes]
            break
        fetch_dir.mkdir(parents=True, exist_ok=True)
        host_name = urlsplit(current_url).hostname or "page"
        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", host_name)[:80] or "page"
        saved_path = fetch_dir / f"{slug:02d}-{safe_name}"
        saved_path.write_bytes(body)
        text = self._decode_body(body, final_content_type)[: self._url_max_text_chars]
        return text, final_status, final_content_type, saved_path

    async def _assert_public_host(self, hostname: str) -> None:
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, hostname, None)
        except socket.gaierror as exc:
            raise ValueError(f"域名解析失败：{hostname}") from exc
        if not infos:
            raise ValueError(f"域名解析失败：{hostname}")
        for info in infos:
            address = ip_address(info[4][0])
            if (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
                or address.is_unspecified
            ):
                raise ValueError(f"禁止访问内网地址：{hostname} -> {address}")

    @staticmethod
    def _decode_body(body: bytes, content_type: str) -> str:
        charset = ""
        lowered = content_type.lower()
        if "charset=" in lowered:
            charset = lowered.split("charset=", 1)[1].split(";")[0].strip()
        encodings = [charset] if charset else []
        encodings.append("utf-8")
        for encoding in encodings:
            try:
                return body.decode(encoding)
            except (LookupError, UnicodeDecodeError):
                continue
        return body.decode("utf-8", errors="replace")
