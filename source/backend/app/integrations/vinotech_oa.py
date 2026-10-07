from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)


class VinotechOaError(RuntimeError):
    """VINOTECH OA 开放接口调用失败。"""


class VinotechOaClient:
    """VINOTECH OA 开放接口客户端，签名规则遵循 OA 官方 api-guide。"""

    def __init__(
        self,
        *,
        base_url: str,
        access_id: str,
        secret_key: str,
        timeout_seconds: float = 10.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_id = access_id.strip()
        self.secret_key = secret_key.strip()
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def list_all_employees(self) -> object:
        """GET /res/employee/all：全部在职员工（含团队归属），用于架构显示。"""
        return await self._request_json("GET", "/res/employee/all")

    async def list_teams(self) -> object:
        """GET /res/team/list：团队（部门）列表。"""
        return await self._request_json("GET", "/res/team/list")

    async def _request_json(self, method: str, path: str) -> object:
        if not self.access_id or not self.secret_key:
            raise VinotechOaError("OA API 凭据未配置（OA_ACCESS_ID / OA_SECRET_KEY）。")

        timestamp = str(int(time.time() * 1000))
        prepared = f"{method}{path}{timestamp}"
        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            prepared.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-VINOTECH-OA-KEY": self.access_id,
            "X-VINOTECH-OA-SIGN": signature,
            "X-VINOTECH-OA-TIMESTAMP": timestamp,
            "Accept": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                )
        except httpx.RequestError as exc:
            raise VinotechOaError(f"OA API 连接失败：{exc}") from exc

        if response.status_code >= 400:
            detail = response.text.strip()[:1000]
            raise VinotechOaError(f"OA API 请求失败：HTTP {response.status_code} {detail}")
        try:
            return response.json()
        except ValueError as exc:
            raise VinotechOaError("OA API 返回了非 JSON 响应。") from exc


@dataclass(frozen=True)
class OaPerson:
    """OA 员工目录信息：部门完整路径 + 稳定部门标识 + 用工形式。"""

    department_name: str
    employment_form: int | None
    department_keys: tuple[str, ...] = field(default=(), compare=False)
    department_paths: tuple[str, ...] = field(default=(), compare=False)


@dataclass(frozen=True)
class OaDepartment:
    key: str
    name: str
    parent_key: str | None
    path: str


@dataclass(frozen=True)
class OaDirectorySnapshot:
    """目录一次加载结果；loaded=False 表示 OA 不可用或未配置，不能作为离职判定依据。"""

    people: dict[str, OaPerson]
    loaded: bool
    departments: dict[str, OaDepartment] = field(default_factory=dict)

    def is_left_employee(self, email: str, company_domains: set[str]) -> bool:
        """公司域名邮箱且不在在职目录中，视为离职。"""
        if not self.loaded:
            return False
        normalized = email.strip().lower()
        domain = normalized.rpartition("@")[2]
        return bool(domain) and domain in company_domains and normalized not in self.people


class OaEmployeeDirectory:
    """拉取 OA 团队与在职员工数据，构建 邮箱 -> OaPerson 目录，进程内 TTL 缓存。

    配置了 cache_file 时，每次成功拉取后会把目录落盘；OA 接口失败时可从文件兜底
    （仅用于部门展示，loaded 仍为 False，不参与离职判定）。
    """

    NEGATIVE_CACHE_TTL_SECONDS = 30

    def __init__(
        self,
        client: VinotechOaClient | None,
        cache_ttl_seconds: int,
        cache_file: Path | None = None,
    ) -> None:
        self._client = client
        self._cache_ttl_seconds = cache_ttl_seconds
        self._cache_file = cache_file
        self._cache = OaDirectorySnapshot({}, False)
        self._cache_expires_at = 0.0

    async def snapshot(self) -> OaDirectorySnapshot:
        if self._client is None:
            return OaDirectorySnapshot({}, False)
        now = time.monotonic()
        if now < self._cache_expires_at:
            return self._cache
        try:
            mapping, departments = await self._load()
            snapshot = OaDirectorySnapshot(mapping, True, departments)
            ttl = self._cache_ttl_seconds
            self._save_cache_file(mapping, departments)
        except VinotechOaError as exc:
            logger.warning("OA 员工目录加载失败：%s", exc)
            fallback = self._load_cache_file()
            if fallback is not None:
                # 用上次成功的本地缓存兜底；loaded=False 保持「不可作离职判定」的安全语义
                people, departments = fallback
                snapshot = OaDirectorySnapshot(people, False, departments)
                ttl = self._cache_ttl_seconds
            else:
                snapshot = OaDirectorySnapshot({}, False)
                ttl = self.NEGATIVE_CACHE_TTL_SECONDS
        self._cache = snapshot
        self._cache_expires_at = now + ttl
        return snapshot

    def _save_cache_file(self, mapping: dict[str, OaPerson], departments: dict[str, OaDepartment]) -> None:
        """把目录落盘；失败只记日志，不影响接口成功的结果。"""
        if self._cache_file is None:
            return
        try:
            payload = {
                email: {
                    "department_name": person.department_name,
                    "employment_form": person.employment_form,
                }
                for email, person in mapping.items()
            }
            payload["__person_departments__"] = {
                email: {
                    "department_keys": list(person.department_keys),
                    "department_paths": list(person.department_paths),
                }
                for email, person in mapping.items()
            }
            payload["__departments__"] = {
                key: {
                    "name": department.name,
                    "parent_key": department.parent_key,
                    "path": department.path,
                }
                for key, department in departments.items()
            }
            tmp_path = self._cache_file.with_suffix(self._cache_file.suffix + ".tmp")
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp_path, self._cache_file)
        except OSError as exc:
            logger.warning("OA 员工目录缓存写入失败：%s", exc)

    def _load_cache_file(self) -> tuple[dict[str, OaPerson], dict[str, OaDepartment]] | None:
        """从本地缓存文件读取目录；缺失/损坏时返回 None。"""
        if self._cache_file is None:
            return None
        try:
            raw = json.loads(self._cache_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("OA 员工目录缓存读取失败，回退为空目录：%s", exc)
            return None
        if not isinstance(raw, dict):
            logger.warning("OA 员工目录缓存格式异常，回退为空目录。")
            return None
        people: dict[str, OaPerson] = {}
        departments: dict[str, OaDepartment] = {}
        raw_person_departments = raw.get("__person_departments__")
        raw_departments = raw.get("__departments__")
        if isinstance(raw_departments, dict):
            for key, item in raw_departments.items():
                if not isinstance(item, dict):
                    continue
                name = item.get("name")
                path = item.get("path")
                if not isinstance(name, str) or not isinstance(path, str):
                    continue
                parent_key = item.get("parent_key")
                departments[str(key)] = OaDepartment(
                    key=str(key),
                    name=name,
                    parent_key=str(parent_key) if parent_key not in (None, "") else None,
                    path=path,
                )
        for email, item in raw.items():
            if email in {"__departments__", "__person_departments__"}:
                continue
            if not isinstance(item, dict):
                continue
            department_name = item.get("department_name")
            employment_form = item.get("employment_form")
            metadata = raw_person_departments.get(email) if isinstance(raw_person_departments, dict) else None
            department_keys = metadata.get("department_keys") if isinstance(metadata, dict) else None
            department_paths = metadata.get("department_paths") if isinstance(metadata, dict) else None
            people[str(email).strip().lower()] = OaPerson(
                department_name=department_name if isinstance(department_name, str) else "",
                employment_form=employment_form if isinstance(employment_form, int) else None,
                department_keys=tuple(str(value) for value in department_keys if value not in (None, ""))
                if isinstance(department_keys, list)
                else (),
                department_paths=tuple(str(value) for value in department_paths if value not in (None, ""))
                if isinstance(department_paths, list)
                else (),
            )
        return people, departments

    async def _load(self) -> tuple[dict[str, OaPerson], dict[str, OaDepartment]]:
        teams = self._parse_teams(await self._client.list_teams())
        employees = self._parse_employees(await self._client.list_all_employees())
        departments = self._build_departments(teams)
        mapping: dict[str, OaPerson] = {}
        for employee in employees:
            email = str(employee.get("email") or "").strip().lower()
            if not email or email in mapping:
                continue
            department_keys: list[str] = []
            department_paths: list[str] = []
            team_links = employee.get("teamEmployees")
            if isinstance(team_links, list):
                for link in team_links:
                    if not isinstance(link, dict):
                        continue
                    team_key = self._team_key(link.get("teamId"))
                    path = departments.get(team_key).path if team_key in departments else ""
                    if path:
                        department_keys.append(team_key)
                        department_paths.append(path)
            mapping[email] = OaPerson(
                department_name=department_paths[0] if department_paths else "",
                employment_form=self._employment_form(employee),
                department_keys=tuple(dict.fromkeys(department_keys)),
                department_paths=tuple(dict.fromkeys(department_paths)),
            )
        return mapping, departments

    @classmethod
    def _build_departments(
        cls,
        teams: dict[object, tuple[str, object]],
    ) -> dict[str, OaDepartment]:
        departments: dict[str, OaDepartment] = {}
        for team_id, (name, parent_id) in teams.items():
            key = cls._team_key(team_id)
            parent_key = cls._team_key(parent_id) if parent_id is not None else None
            departments[key] = OaDepartment(
                key=key,
                name=name,
                parent_key=parent_key,
                path=cls._team_path(team_id, teams),
            )
        return departments

    @staticmethod
    def _team_key(team_id: object) -> str:
        return str(team_id).strip()

    @staticmethod
    def _employment_form(employee: dict[str, object]) -> int | None:
        detail = employee.get("detail")
        form = detail.get("employmentForm") if isinstance(detail, dict) else None
        return form if isinstance(form, int) else None

    @staticmethod
    def _parse_teams(payload: object) -> dict[object, tuple[str, object]]:
        """响应为 {"code": 0, "data": [{id, name, superiorId, ...}]}; team 即部门。"""
        teams: dict[object, tuple[str, object]] = {}
        for row in OaEmployeeDirectory._parse_data_list(payload, "团队列表"):
            name = str(row.get("name") or "").strip()
            if name:
                teams[row.get("id")] = (name, row.get("superiorId"))
        return teams

    @staticmethod
    def _parse_employees(payload: object) -> list[dict[str, object]]:
        """响应为 {"code": 0, "data": [{email, teamEmployees: [{teamId}], ...}]}。"""
        return OaEmployeeDirectory._parse_data_list(payload, "员工列表")

    @staticmethod
    def _parse_data_list(payload: object, label: str) -> list[dict[str, object]]:
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise VinotechOaError(f"OA {label}响应业务码异常。")
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise VinotechOaError(f"OA {label}响应结构不符合预期。")
        return [row for row in rows if isinstance(row, dict)]

    @staticmethod
    def _team_path(team_id: object, teams: dict[object, tuple[str, object]]) -> str:
        """沿 superiorId 向上拼接完整层级路径，如「研发中心 R&D Center / 业务C组 Backend Team C」。"""
        parts: list[str] = []
        seen: set[object] = set()
        current = team_id
        while current is not None and current in teams and current not in seen:
            seen.add(current)
            name, superior_id = teams[current]
            parts.append(name)
            current = superior_id
        return " / ".join(reversed(parts))
