from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import socket
import stat
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from app.business.security_scan.prompts import SCAN_OUTPUT_SCHEMA
from app.business.security_scan.service import SecurityScanService
from app.business.security_scan.store import SecurityScanStore
from app.core import dependencies
from app.core.config import settings
from app.utils.archive import extract_zip_safely
from app.utils.hmac_auth import verify_sha256_hmac

SECRET = "test-secret"


def _signature(payload: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode("utf-8"), payload, hashlib.sha256).hexdigest()


@pytest.fixture
def scan_targets(tmp_path: Path) -> Path:
    targets = tmp_path / "targets"
    targets.mkdir()
    return targets


@pytest.fixture
def scan_service(tmp_path: Path, scan_targets: Path, monkeypatch: pytest.MonkeyPatch):
    dependencies.get_security_scan_store.cache_clear()
    dependencies.get_security_scan_runtime_client.cache_clear()
    dependencies.get_security_scan_service.cache_clear()
    monkeypatch.setattr(settings, "assistant_db_path", str(tmp_path / "assistant.sqlite3"))
    monkeypatch.setattr(settings, "security_scan_shared_secret", SECRET)
    monkeypatch.setattr(settings, "security_scan_allowed_roots", [str(scan_targets)])
    monkeypatch.setattr(settings, "security_scan_root", str(tmp_path / "scan-root"))
    service = dependencies.get_security_scan_service()
    assert isinstance(service, SecurityScanService)
    yield service
    dependencies.get_security_scan_store.cache_clear()
    dependencies.get_security_scan_runtime_client.cache_clear()
    dependencies.get_security_scan_service.cache_clear()


def _write_zip(path: Path, entries: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)


def _mark_zip_encrypted(path: Path) -> None:
    """zipfile.writestr 会重置 flag_bits，这里直接在二进制层面置加密位（本地头偏移 6、中央目录偏移 8）。"""
    data = bytearray(path.read_bytes())
    pos = 0
    while True:
        local = data.find(b"PK\x03\x04", pos)
        central = data.find(b"PK\x01\x02", pos)
        candidates = [offset for offset in (local, central) if offset != -1]
        if not candidates:
            break
        idx = min(candidates)
        flag_offset = idx + (6 if data[idx:idx + 4] == b"PK\x03\x04" else 8)
        data[flag_offset] |= 0x1
        pos = idx + 4
    path.write_bytes(bytes(data))


# ------------------------------------------------------------------ HMAC 工具


def test_verify_sha256_hmac() -> None:
    payload = b'{"path": "/tmp/x"}'
    assert verify_sha256_hmac(secret=SECRET, payload=payload, signature_header=_signature(payload)) is True
    assert verify_sha256_hmac(secret=SECRET, payload=payload, signature_header="sha256=bad") is False
    assert verify_sha256_hmac(secret=SECRET, payload=payload, signature_header=None) is False
    assert verify_sha256_hmac(secret=SECRET, payload=payload, signature_header="md5=abc") is False


# ------------------------------------------------------------------ API 鉴权与入队


@pytest.mark.anyio
async def test_scan_endpoints_require_shared_secret(
    client: httpx.AsyncClient,
    scan_service: SecurityScanService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "security_scan_shared_secret", "")
    body = json.dumps({"url": "https://example.com"}).encode("utf-8")

    post_response = await client.post(
        "/api/security/scan",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _signature(body)},
    )
    assert post_response.status_code == 503

    get_response = await client.get(
        "/api/security/scan/any-job",
        headers={"X-Hub-Signature-256": _signature(b"any-job")},
    )
    assert get_response.status_code == 503


@pytest.mark.anyio
async def test_create_scan_rejects_invalid_signature(
    client: httpx.AsyncClient,
    scan_service: SecurityScanService,
) -> None:
    body = json.dumps({"url": "https://example.com"}).encode("utf-8")

    response = await client.post(
        "/api/security/scan",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": "sha256=bad"},
    )
    assert response.status_code == 401


@pytest.mark.anyio
async def test_create_scan_accepts_valid_path_request(
    client: httpx.AsyncClient,
    scan_service: SecurityScanService,
    scan_targets: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_job(job: object) -> None:
        return None

    monkeypatch.setattr(scan_service, "run_job", fake_run_job)
    target_file = scan_targets / "app.zip"
    _write_zip(target_file, {"readme.txt": b"hello"})
    body = json.dumps({"path": str(target_file), "context": {"name": "小工具"}}).encode("utf-8")

    response = await client.post(
        "/api/security/scan",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _signature(body)},
    )

    assert response.status_code == 202
    job_id = response.json()["jobId"]
    job = scan_service.get_job(job_id=job_id)
    assert job.status == "pending"
    assert job.target_type == "path"
    assert job.target_value == str(target_file)
    await asyncio.sleep(0)


@pytest.mark.anyio
async def test_create_scan_validates_request(
    client: httpx.AsyncClient,
    scan_service: SecurityScanService,
    scan_targets: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_run_job(job: object) -> None:
        return None

    monkeypatch.setattr(scan_service, "run_job", fake_run_job)
    cases = [
        {"path": str(scan_targets), "url": "https://example.com"},
        {},
        {"path": "/tmp/not-in-allowed-roots"},
        {"path": str(scan_targets / "missing.zip")},
        {"url": "ftp://example.com"},
    ]
    for payload in cases:
        body = json.dumps(payload).encode("utf-8")
        response = await client.post(
            "/api/security/scan",
            content=body,
            headers={"Content-Type": "application/json", "X-Hub-Signature-256": _signature(body)},
        )
        assert response.status_code == 400, payload


@pytest.mark.anyio
async def test_get_scan_status_flow(
    client: httpx.AsyncClient,
    scan_service: SecurityScanService,
) -> None:
    job = scan_service.enqueue(path=None, url="https://example.com", context_json=None)

    pending_response = await client.get(
        f"/api/security/scan/{job.job_id}",
        headers={"X-Hub-Signature-256": _signature(job.job_id.encode("utf-8"))},
    )
    assert pending_response.status_code == 200
    assert pending_response.json() == {"status": "running"}

    assert scan_service.store.mark_job_running(job.job_id) is True
    scan_service.store.complete_job(
        job_id=job.job_id,
        verdict="unsafe",
        reason="README 明确声明窃取凭证",
        findings_json=json.dumps(["README.md 第 3 行"], ensure_ascii=False),
    )

    done_response = await client.get(
        f"/api/security/scan/{job.job_id}",
        headers={"X-Hub-Signature-256": _signature(job.job_id.encode("utf-8"))},
    )
    assert done_response.status_code == 200
    assert done_response.json() == {
        "status": "done",
        "result": {
            "verdict": "unsafe",
            "reason": "README 明确声明窃取凭证",
            "findings": ["README.md 第 3 行"],
        },
    }


@pytest.mark.anyio
async def test_get_scan_failed_shows_error(
    client: httpx.AsyncClient,
    scan_service: SecurityScanService,
) -> None:
    job = scan_service.enqueue(path=None, url="https://example.com", context_json=None)
    assert scan_service.store.mark_job_running(job.job_id) is True
    scan_service.store.fail_job(job_id=job.job_id, error_message="Agent 未返回最终 JSON。")

    response = await client.get(
        f"/api/security/scan/{job.job_id}",
        headers={"X-Hub-Signature-256": _signature(job.job_id.encode("utf-8"))},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "failed", "error": "Agent 未返回最终 JSON。"}


@pytest.mark.anyio
async def test_get_scan_requires_signature_and_exists(
    client: httpx.AsyncClient,
    scan_service: SecurityScanService,
) -> None:
    bad_signature = await client.get(
        "/api/security/scan/unknown-job",
        headers={"X-Hub-Signature-256": "sha256=bad"},
    )
    assert bad_signature.status_code == 401

    not_found = await client.get(
        "/api/security/scan/unknown-job",
        headers={"X-Hub-Signature-256": _signature(b"unknown-job")},
    )
    assert not_found.status_code == 404


# ------------------------------------------------------------------ enqueue 校验


def test_enqueue_validates_targets(scan_service: SecurityScanService, scan_targets: Path) -> None:
    with pytest.raises(ValueError):
        scan_service.enqueue(path=str(scan_targets), url="https://example.com", context_json=None)
    with pytest.raises(ValueError):
        scan_service.enqueue(path=None, url=None, context_json=None)
    with pytest.raises(ValueError):
        scan_service.enqueue(path="/tmp/not-in-allowed-roots", url=None, context_json=None)
    with pytest.raises(ValueError):
        scan_service.enqueue(path=None, url="javascript:alert(1)", context_json=None)

    job = scan_service.enqueue(path=None, url="https://example.com/", context_json=None)
    assert job.target_type == "url"
    assert job.target_value == "https://example.com/"


def test_enqueue_rejects_all_local_paths_when_roots_empty(
    scan_service: SecurityScanService,
    scan_targets: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scan_service, "_allowed_roots", [])
    with pytest.raises(ValueError):
        scan_service.enqueue(path=str(scan_targets), url=None, context_json=None)


# ------------------------------------------------------------------ store 状态机


def test_store_state_machine(tmp_path: Path) -> None:
    store = SecurityScanStore(db_path=str(tmp_path / "scan.sqlite3"))
    job = store.create_job(job_id="job-1", target_type="path", target_value="/tmp/x", context_json=None)
    assert job.status == "pending"
    assert [pending.job_id for pending in store.list_pending_jobs()] == ["job-1"]

    assert store.mark_job_running("job-1") is True
    assert store.mark_job_running("job-1") is False
    assert store.list_pending_jobs() == []

    store.complete_job(job_id="job-1", verdict="safe", reason="未见异常", findings_json="[]")
    done = store.get_job(job_id="job-1")
    assert done is not None
    assert done.status == "done"
    assert done.verdict == "safe"

    store.create_job(job_id="job-2", target_type="url", target_value="https://example.com", context_json=None)
    store.mark_job_running("job-2")
    future = (datetime.now(timezone.utc) + timedelta(seconds=10)).isoformat()
    stale = store.fail_stale_running_jobs(cutoff_started_at=future, error_message="超时")
    assert [stale_job.job_id for stale_job in stale] == ["job-2"]
    failed = store.get_job(job_id="job-2")
    assert failed is not None
    assert failed.status == "failed"

    purged = store.purge_jobs_older_than(cutoff_created_at=future)
    assert {purged_job.job_id for purged_job in purged} == {"job-1", "job-2"}
    assert store.get_job(job_id="job-1") is None


# ------------------------------------------------------------------ 安全解压


def test_extract_zip_safely_strips_wrapper(tmp_path: Path) -> None:
    archive = tmp_path / "app.zip"
    _write_zip(archive, {"wrapper/readme.txt": b"hello", "wrapper/sub/tool.exe": b"MZ"})
    dest = tmp_path / "out"

    count = extract_zip_safely(archive_path=archive, dest_dir=dest, max_files=10, max_total_bytes=1024)

    assert count == 2
    assert (dest / "readme.txt").read_bytes() == b"hello"
    assert (dest / "sub" / "tool.exe").read_bytes() == b"MZ"


def test_extract_zip_safely_rejects_malicious_entries(tmp_path: Path) -> None:
    traversal = tmp_path / "traversal.zip"
    with zipfile.ZipFile(traversal, "w") as archive:
        archive.writestr("../evil.txt", "x")
    with pytest.raises(FileNotFoundError):
        extract_zip_safely(archive_path=traversal, dest_dir=tmp_path / "out1", max_files=10, max_total_bytes=1024)

    symlink = tmp_path / "symlink.zip"
    with zipfile.ZipFile(symlink, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "/etc/passwd")
    with pytest.raises(FileNotFoundError):
        extract_zip_safely(archive_path=symlink, dest_dir=tmp_path / "out2", max_files=10, max_total_bytes=1024)

    encrypted = tmp_path / "encrypted.zip"
    _write_zip(encrypted, {"secret.txt": b"x"})
    _mark_zip_encrypted(encrypted)
    with pytest.raises(FileNotFoundError):
        extract_zip_safely(archive_path=encrypted, dest_dir=tmp_path / "out3", max_files=10, max_total_bytes=1024)

    duplicate = tmp_path / "duplicate.zip"
    with zipfile.ZipFile(duplicate, "w") as archive:
        archive.writestr("a.txt", "1")
        archive.writestr("a.txt", "2")
    with pytest.raises(FileNotFoundError):
        extract_zip_safely(archive_path=duplicate, dest_dir=tmp_path / "out4", max_files=10, max_total_bytes=1024)


def test_extract_zip_safely_enforces_limits(tmp_path: Path) -> None:
    archive = tmp_path / "big.zip"
    _write_zip(archive, {"a.txt": b"x" * 1000, "b.txt": b"y", "c.txt": b"z"})

    with pytest.raises(ValueError):
        extract_zip_safely(archive_path=archive, dest_dir=tmp_path / "out-files", max_files=2, max_total_bytes=10_000)

    dest = tmp_path / "out-bytes"
    with pytest.raises(ValueError):
        extract_zip_safely(archive_path=archive, dest_dir=dest, max_files=10, max_total_bytes=100)
    assert not dest.exists()

    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip")
    with pytest.raises(ValueError):
        extract_zip_safely(archive_path=broken, dest_dir=tmp_path / "out-broken", max_files=10, max_total_bytes=1024)


# ------------------------------------------------------------------ 判定载荷校验


def test_validate_scan_payload(scan_service: SecurityScanService) -> None:
    validated = scan_service._validate_scan_payload(
        {"verdict": "safe", "reason": " 未见恶意 ", "findings": ["证据一", "", "  证据二  "]}
    )
    assert validated == {"verdict": "safe", "reason": "未见恶意", "findings": ["证据一", "证据二"]}

    with pytest.raises(ValueError):
        scan_service._validate_scan_payload({"verdict": "maybe", "reason": "x"})
    with pytest.raises(ValueError):
        scan_service._validate_scan_payload({"verdict": "safe", "reason": "  "})
    with pytest.raises(ValueError):
        scan_service._validate_scan_payload({"verdict": "safe", "reason": "x", "findings": "not-a-list"})

    long_reason = scan_service._validate_scan_payload({"verdict": "safe", "reason": "长" * 800})
    assert len(str(long_reason["reason"])) == 500
    truncated = scan_service._validate_scan_payload(
        {"verdict": "unsafe", "reason": "x", "findings": [f"证据{i}" for i in range(30)]}
    )
    assert len(truncated["findings"]) == 20


def test_scan_output_schema_is_strict() -> None:
    """codex 结构化输出要求：每个 object 节点显式 additionalProperties=False，
    且 required 必须覆盖全部属性（不允许可选字段）。"""

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node.get("additionalProperties") is False
                properties = node.get("properties")
                if isinstance(properties, dict):
                    assert sorted(node.get("required", [])) == sorted(properties)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(SCAN_OUTPUT_SCHEMA)


# ------------------------------------------------------------------ SSRF 防护


@pytest.mark.anyio
async def test_assert_public_host_filters_private_addresses(
    scan_service: SecurityScanService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(host: str, port: object, *args: object, **kwargs: object) -> list:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError):
        await scan_service._assert_public_host("internal.example.com")


@pytest.mark.anyio
async def test_assert_public_host_accepts_public_addresses(
    scan_service: SecurityScanService,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_getaddrinfo(host: str, port: object, *args: object, **kwargs: object) -> list:
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    await scan_service._assert_public_host("example.com")
