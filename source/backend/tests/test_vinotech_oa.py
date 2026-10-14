from __future__ import annotations

import hashlib
import hmac
import json

import httpx
import pytest

from app.integrations.vinotech_oa import (
    OaDirectorySnapshot,
    OaEmployeeDirectory,
    OaPerson,
    VinotechOaClient,
    VinotechOaError,
)

TEAMS_PAYLOAD = {
    "code": 0,
    "data": [
        {"id": 9, "name": "总裁办 CEO Office", "superiorId": None, "companyId": 1},
        {"id": 77, "name": "研发中心 R&D Center", "superiorId": None, "companyId": 1},
        {"id": 100, "name": "业务C组 Backend Team C", "superiorId": 77, "companyId": 1},
    ],
}

EMPLOYEES_PAYLOAD = {
    "code": 0,
    "data": [
        {
            "slackName": "Pat.Yang",
            "email": "pat.yang@corp.test",
            "id": 1,
            "teamEmployees": [{"teamId": 9, "id": 1}],
            "detail": {"employmentForm": 1},
        },
        {
            "slackName": "Casey.Lin",
            "email": "Casey.Lin@Corp.Test",
            "id": 14,
            "teamEmployees": [{"teamId": 100, "id": 14}],
            "detail": {"employmentForm": 3},
        },
        {
            "slackName": "NoForm",
            "email": "no.form@corp.test",
            "id": 20,
            "teamEmployees": [{"teamId": 77, "id": 20}],
            "detail": {},
        },
        {
            "slackName": "NoEmail",
            "email": None,
            "id": 456,
            "teamEmployees": [{"teamId": 9, "id": 456}],
            "detail": {"employmentForm": 2},
        },
        {
            "slackName": "UnknownTeam",
            "email": "unknown.team@corp.test",
            "id": 457,
            "teamEmployees": [{"teamId": 9999, "id": 457}],
            "detail": {"employmentForm": 1},
        },
    ],
}

COMPANY_DOMAINS = {"corp.test"}


def _build_client(handler) -> VinotechOaClient:
    return VinotechOaClient(
        base_url="https://oa.example.com",
        access_id="access-id",
        secret_key="secret-key",
        transport=httpx.MockTransport(handler),
    )


def _directory_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/res/team/list":
        return httpx.Response(200, json=TEAMS_PAYLOAD)
    if request.url.path == "/res/employee/all":
        return httpx.Response(200, json=EMPLOYEES_PAYLOAD)
    return httpx.Response(404, json={"code": 4704, "message": "接口不存在"})


@pytest.mark.anyio
async def test_client_signs_request_with_oa_headers() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"code": 0, "data": []})

    client = _build_client(handler)
    await client.list_all_employees()

    request = captured[0]
    assert request.url == "https://oa.example.com/res/employee/all"
    timestamp = request.headers["X-VINOTECH-OA-TIMESTAMP"]
    expected_sign = hmac.new(
        b"secret-key",
        f"GET/res/employee/all{timestamp}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    assert request.headers["X-VINOTECH-OA-KEY"] == "access-id"
    assert request.headers["X-VINOTECH-OA-SIGN"] == expected_sign


@pytest.mark.anyio
async def test_snapshot_maps_email_to_person_with_department_path_and_form() -> None:
    directory = OaEmployeeDirectory(client=_build_client(_directory_handler), cache_ttl_seconds=300)

    snapshot = await directory.snapshot()

    assert snapshot.loaded is True
    assert snapshot.people == {
        "pat.yang@corp.test": OaPerson("总裁办 CEO Office", 1),
        "casey.lin@corp.test": OaPerson("研发中心 R&D Center / 业务C组 Backend Team C", 3),
        "no.form@corp.test": OaPerson("研发中心 R&D Center", None),
        "unknown.team@corp.test": OaPerson("", 1),
    }


@pytest.mark.anyio
async def test_snapshot_caches_within_ttl() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return _directory_handler(request)

    directory = OaEmployeeDirectory(client=_build_client(handler), cache_ttl_seconds=300)

    await directory.snapshot()
    await directory.snapshot()

    assert calls == ["/res/team/list", "/res/employee/all"]


@pytest.mark.anyio
async def test_snapshot_not_loaded_after_failure_and_negative_cached() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(403, json={"code": 4703, "message": "您没有访问权限"})

    directory = OaEmployeeDirectory(client=_build_client(handler), cache_ttl_seconds=300)

    first = await directory.snapshot()
    second = await directory.snapshot()
    assert first.loaded is False and second.loaded is False
    assert calls == ["/res/team/list"]


@pytest.mark.anyio
async def test_snapshot_without_client_is_not_loaded() -> None:
    directory = OaEmployeeDirectory(client=None, cache_ttl_seconds=300)

    assert (await directory.snapshot()).loaded is False


def test_is_left_employee_only_for_company_domain_when_loaded() -> None:
    loaded = OaDirectorySnapshot({"pat.yang@corp.test": OaPerson("总裁办 CEO Office", 1)}, True)
    not_loaded = OaDirectorySnapshot({}, False)

    assert loaded.is_left_employee("someone@corp.test", COMPANY_DOMAINS) is True
    assert loaded.is_left_employee("pat.yang@corp.test", COMPANY_DOMAINS) is False
    # 非公司域名不参与离职判定（如外部/个人邮箱账号）
    assert loaded.is_left_employee("someone@gmail.com", COMPANY_DOMAINS) is False
    # OA 不可用时不能判定离职
    assert not_loaded.is_left_employee("someone@corp.test", COMPANY_DOMAINS) is False


@pytest.mark.anyio
async def test_client_missing_credentials_raises() -> None:
    client = VinotechOaClient(base_url="https://oa.example.com", access_id="", secret_key="")

    with pytest.raises(VinotechOaError):
        await client.list_teams()


@pytest.mark.anyio
async def test_snapshot_success_writes_cache_file(tmp_path) -> None:
    cache_file = tmp_path / "oa_directory_cache.json"
    directory = OaEmployeeDirectory(
        client=_build_client(_directory_handler),
        cache_ttl_seconds=300,
        cache_file=cache_file,
    )

    await directory.snapshot()

    raw = json.loads(cache_file.read_text(encoding="utf-8"))
    assert raw["pat.yang@corp.test"] == {"department_name": "总裁办 CEO Office", "employment_form": 1}
    assert raw["casey.lin@corp.test"] == {
        "department_name": "研发中心 R&D Center / 业务C组 Backend Team C",
        "employment_form": 3,
    }
    assert raw["no.form@corp.test"] == {"department_name": "研发中心 R&D Center", "employment_form": None}


@pytest.mark.anyio
async def test_snapshot_failure_falls_back_to_cache_file(tmp_path) -> None:
    cache_file = tmp_path / "oa_directory_cache.json"
    # 先成功一次，写入缓存
    directory = OaEmployeeDirectory(
        client=_build_client(_directory_handler),
        cache_ttl_seconds=300,
        cache_file=cache_file,
    )
    await directory.snapshot()

    # 新实例指向同一缓存文件，且 OA 全部失败
    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"code": 4703, "message": "您没有访问权限"})

    directory2 = OaEmployeeDirectory(
        client=_build_client(failing_handler),
        cache_ttl_seconds=300,
        cache_file=cache_file,
    )

    snapshot = await directory2.snapshot()

    assert snapshot.loaded is False
    assert snapshot.people["pat.yang@corp.test"] == OaPerson("总裁办 CEO Office", 1)
    assert snapshot.people["casey.lin@corp.test"] == OaPerson(
        "研发中心 R&D Center / 业务C组 Backend Team C", 3
    )
    # 兜底数据不参与离职判定
    assert snapshot.is_left_employee("new.come@corp.test", COMPANY_DOMAINS) is False


@pytest.mark.anyio
async def test_snapshot_failure_without_cache_file_returns_empty(tmp_path, caplog) -> None:
    cache_file = tmp_path / "oa_directory_cache.json"  # 不存在
    directory = OaEmployeeDirectory(
        client=_build_client(lambda request: httpx.Response(403, json={"code": 4703})),
        cache_ttl_seconds=300,
        cache_file=cache_file,
    )

    snapshot = await directory.snapshot()

    assert snapshot.loaded is False
    assert snapshot.people == {}
    assert any("缓存读取失败" in record.message for record in caplog.records)


@pytest.mark.anyio
async def test_directory_business_code_error_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 4703, "message": "您没有访问权限"})

    directory = OaEmployeeDirectory(client=_build_client(handler), cache_ttl_seconds=300)

    with pytest.raises(VinotechOaError):
        await directory._load()
