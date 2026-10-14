from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

from app.business.workspace import WorkspaceAccessError, WorkspaceAccessService
from app.integrations.llm.client import AssistantLLMClient, LLMMessage
from app.integrations.vinotech_oa import OaDirectorySnapshot, OaEmployeeDirectory
from app.services.auth_models import UserRecord
from app.services.permission_store import (
    PermissionPolicyDepartmentRecord,
    PermissionPolicyRecord,
    SQLitePermissionStore,
)

TEST_DATA_CAPABILITY = "test_data"
UAT_ENVIRONMENT_CONTROL_TASK = "uat_environment_control"
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PermissionDecision:
    allowed: bool
    reason: str
    matched_policy_id: str | None = None
    matched_policy_name: str | None = None
    department_snapshot: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "matched_policy_id": self.matched_policy_id,
            "matched_policy_name": self.matched_policy_name,
            "department_snapshot": self.department_snapshot,
        }


class BusinessPermissionService:
    def __init__(
        self,
        *,
        store: SQLitePermissionStore,
        oa_directory: OaEmployeeDirectory,
        workspace_access: WorkspaceAccessService,
        allowed_organizations: list[str],
        allowed_tasks: list[str],
        blocked_tasks: list[str],
        name_llm_client: AssistantLLMClient | None = None,
    ) -> None:
        self.store = store
        self.oa_directory = oa_directory
        self.workspace_access = workspace_access
        self.allowed_organizations = {item.strip().lower() for item in allowed_organizations if item.strip()}
        self.allowed_tasks = {item.strip() for item in allowed_tasks if item.strip()}
        self.blocked_tasks = {item.strip() for item in blocked_tasks if item.strip()}
        self.name_llm_client = name_llm_client

    async def check_capability_access(
        self,
        user: UserRecord,
        organization_key: str,
        capability_key: str,
        task_name: str | None = None,
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        request_id: str | None = None,
    ) -> PermissionDecision:
        normalized_org = organization_key.strip().lower()
        normalized_capability = capability_key.strip()
        normalized_task = task_name.strip() if task_name else None
        snapshot = await self.oa_directory.snapshot()
        department_snapshot = self._department_snapshot(user, snapshot)

        decision = self._check_without_log(
            user=user,
            organization_key=normalized_org,
            capability_key=normalized_capability,
            task_name=normalized_task,
            snapshot=snapshot,
        )
        self.store.append_decision_log(
            user_id=user.user_id,
            email=user.email,
            organization_key=normalized_org,
            capability_key=normalized_capability,
            task_name=normalized_task,
            allowed=decision.allowed,
            reason=decision.reason,
            matched_policy_id=decision.matched_policy_id,
            matched_policy_name=decision.matched_policy_name,
            department_snapshot=department_snapshot,
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
        )
        decision.department_snapshot = department_snapshot
        return decision

    async def list_allowed_tasks(
        self,
        *,
        user: UserRecord,
        organization_key: str,
        task_names: list[str],
        session_id: str | None = None,
        turn_id: str | None = None,
        request_id: str | None = None,
    ) -> tuple[PermissionDecision, set[str]]:
        snapshot = await self.oa_directory.snapshot()
        capability_decision = self._check_without_log(
            user=user,
            organization_key=organization_key.strip().lower(),
            capability_key=TEST_DATA_CAPABILITY,
            task_name=None,
            snapshot=snapshot,
        )
        if capability_decision.allowed:
            policies = self._matched_policies(
                user=user,
                organization_key=organization_key.strip().lower(),
                capability_key=TEST_DATA_CAPABILITY,
                snapshot=snapshot,
            )
            allowed = {
                name
                for name in task_names
                if self._task_allowed_by_system(name)
                and any(policy.task_scope_type == "all" or name in policy.tasks for policy in policies)
            }
        else:
            allowed = set()
        department_snapshot = self._department_snapshot(user, snapshot)
        capability_decision.department_snapshot = department_snapshot
        self.store.append_decision_log(
            user_id=user.user_id,
            email=user.email,
            organization_key=organization_key.strip().lower(),
            capability_key=TEST_DATA_CAPABILITY,
            task_name=None,
            allowed=capability_decision.allowed,
            reason=capability_decision.reason,
            matched_policy_id=capability_decision.matched_policy_id,
            matched_policy_name=capability_decision.matched_policy_name,
            department_snapshot=department_snapshot,
            session_id=session_id,
            turn_id=turn_id,
            request_id=request_id,
        )
        return capability_decision, allowed

    async def validate_policy_departments(self, department_keys: list[str]) -> dict[str, str]:
        snapshot = await self.oa_directory.snapshot()
        if not snapshot.departments:
            raise ValueError("OA 部门目录暂时无法确认，不能创建权限策略。")
        normalized = {str(key).strip() for key in department_keys if str(key).strip()}
        unknown = sorted(normalized - set(snapshot.departments))
        if unknown:
            raise ValueError(f"部门不存在或已不在 OA 目录中：{', '.join(unknown)}。")
        return {key: snapshot.departments[key].path for key in normalized}

    async def build_policy(
        self,
        *,
        policy_id: str | None,
        policy_name: str | None,
        organization_key: str,
        capability_key: str,
        department_keys: list[str],
        include_children: bool,
        task_scope_type: str,
        tasks: list[str],
        status: str,
        starts_at: datetime | None,
        expires_at: datetime | None,
        description: str | None,
        actor_user_id: str,
    ) -> PermissionPolicyRecord:
        normalized_org = organization_key.strip().lower()
        if normalized_org not in self.allowed_organizations:
            raise ValueError("该组织未开放测试造数能力。")
        if capability_key != TEST_DATA_CAPABILITY:
            raise ValueError("当前仅支持测试造数能力。")
        if task_scope_type not in {"all", "selected"}:
            raise ValueError("任务范围只能是 all 或 selected。")
        normalized_tasks = sorted({task.strip() for task in tasks if task.strip()})
        if task_scope_type == "selected" and not normalized_tasks:
            raise ValueError("指定任务范围至少选择一个任务。")
        if task_scope_type == "all":
            normalized_tasks = []
        if any(not self._task_allowed_by_system(task) for task in normalized_tasks):
            raise ValueError("任务未被系统级造数范围开放，不能通过部门策略重新开放。")
        if not department_keys:
            raise ValueError("至少选择一个 OA 部门。")
        starts_at = self._normalize_datetime(starts_at)
        expires_at = self._normalize_datetime(expires_at)
        if expires_at and starts_at and expires_at <= starts_at:
            raise ValueError("有效期结束时间必须晚于开始时间。")
        department_paths = await self.validate_policy_departments(department_keys)
        normalized_policy_name = (policy_name or "").strip()
        if not normalized_policy_name:
            normalized_policy_name = await self._generate_policy_name(
                organization_key=normalized_org,
                department_paths=list(department_paths.values()),
                task_scope_type=task_scope_type,
                tasks=normalized_tasks,
            )
        now = datetime.now(timezone.utc)
        existing = self.store.get_policy(policy_id) if policy_id else None
        policy = PermissionPolicyRecord(
            policy_id=policy_id or f"pol_{uuid4().hex[:12]}",
            policy_name=normalized_policy_name,
            organization_key=normalized_org,
            capability_key=capability_key,
            task_scope_type=task_scope_type,
            status=status,
            starts_at=starts_at,
            expires_at=expires_at,
            description=description.strip() if description and description.strip() else None,
            created_by=existing.created_by if existing else actor_user_id,
            updated_by=actor_user_id,
            created_at=existing.created_at if existing else now,
            updated_at=now,
            departments=[
                PermissionPolicyDepartmentRecord(key, department_paths[key], include_children)
                for key in sorted(set(department_keys))
            ],
            tasks=normalized_tasks,
        )
        action = "created" if existing is None else "updated"
        return self.store.save_policy(
            policy,
            actor_user_id=actor_user_id,
            action=action,
            changes=self._policy_dict(policy),
        )

    async def inspect_user(
        self,
        *,
        user: UserRecord,
        organization_key: str,
        task_name: str | None = None,
    ) -> dict[str, object]:
        snapshot = await self.oa_directory.snapshot()
        decision = self._check_without_log(
            user=user,
            organization_key=organization_key.strip().lower(),
            capability_key=TEST_DATA_CAPABILITY,
            task_name=task_name,
            snapshot=snapshot,
        )
        decision.department_snapshot = self._department_snapshot(user, snapshot)
        self.store.append_decision_log(
            user_id=user.user_id,
            email=user.email,
            organization_key=organization_key.strip().lower(),
            capability_key=TEST_DATA_CAPABILITY,
            task_name=task_name.strip() if task_name else None,
            allowed=decision.allowed,
            reason=decision.reason,
            matched_policy_id=decision.matched_policy_id,
            matched_policy_name=decision.matched_policy_name,
            department_snapshot=decision.department_snapshot,
        )
        policies = self._matched_policies(
            user=user,
            organization_key=organization_key.strip().lower(),
            capability_key=TEST_DATA_CAPABILITY,
            snapshot=snapshot,
        )
        return {
            "user": user,
            "agent_access": user.agent_access,
            "organization_key": organization_key.strip().lower(),
            "oa_directory_loaded": snapshot.loaded,
            "department_snapshot": decision.department_snapshot,
            "matched_policies": policies,
            "decision": decision,
        }

    async def count_policy_matches(
        self,
        *,
        policy_id: str,
        users: list[UserRecord],
    ) -> int:
        policy = self.store.get_policy(policy_id)
        if policy is None:
            return 0
        snapshot = await self.oa_directory.snapshot()
        if not snapshot.loaded:
            return 0
        now = datetime.now(timezone.utc)
        if policy.status != "enabled" or (policy.starts_at and policy.starts_at > now) or (policy.expires_at and policy.expires_at <= now):
            return 0
        count = 0
        for user in users:
            if user.status != "active" or user.agent_access != "active":
                continue
            try:
                self.workspace_access.resolve_active_organization(user, policy.organization_key)
            except WorkspaceAccessError:
                continue
            if any(item.policy_id == policy.policy_id for item in self._matched_policies(
                user=user,
                organization_key=policy.organization_key,
                capability_key=policy.capability_key,
                snapshot=snapshot,
            )):
                count += 1
        return count

    def _check_without_log(
        self,
        *,
        user: UserRecord,
        organization_key: str,
        capability_key: str,
        task_name: str | None,
        snapshot: OaDirectorySnapshot,
    ) -> PermissionDecision:
        if organization_key not in self.allowed_organizations:
            return PermissionDecision(False, "当前组织未被系统开放测试造数能力。")
        if capability_key != TEST_DATA_CAPABILITY:
            return PermissionDecision(False, "当前业务能力未注册。")
        if user.status != "active":
            return PermissionDecision(False, "当前账号不可用。")
        if user.agent_access != "active":
            return PermissionDecision(False, "当前账号尚未开通 Agent 权限。")
        try:
            self.workspace_access.resolve_active_organization(user, organization_key)
        except WorkspaceAccessError:
            return PermissionDecision(False, "当前账号不是该组织的有效成员。")
        if not snapshot.loaded:
            return PermissionDecision(False, "部门信息暂时无法确认。")
        person = snapshot.people.get(user.email.strip().lower())
        if person is None or not person.department_keys:
            return PermissionDecision(False, "当前用户没有可确认的 OA 部门归属。")
        policies = self._matched_policies(
            user=user,
            organization_key=organization_key,
            capability_key=capability_key,
            snapshot=snapshot,
        )
        if not policies:
            return PermissionDecision(False, "当前部门未命中任何启用的测试造数策略。")
        matched = policies[0]
        if task_name is not None:
            if not self._task_allowed_by_system(task_name):
                return PermissionDecision(
                    False,
                    "该任务被系统级造数范围阻断，部门策略不能覆盖。",
                    matched.policy_id,
                    matched.policy_name,
                )
            if not any(policy.task_scope_type == "all" or task_name in policy.tasks for policy in policies):
                return PermissionDecision(
                    False,
                    f"当前部门策略未开放任务 {task_name}。",
                    matched.policy_id,
                    matched.policy_name,
                )
        return PermissionDecision(True, "已命中启用的测试造数部门策略。", matched.policy_id, matched.policy_name)

    def _matched_policies(
        self,
        *,
        user: UserRecord,
        organization_key: str,
        capability_key: str,
        snapshot: OaDirectorySnapshot,
    ) -> list[PermissionPolicyRecord]:
        person = snapshot.people.get(user.email.strip().lower())
        if person is None:
            return []
        policies = self.store.list_policies(
            organization_key=organization_key,
            capability_key=capability_key,
            status="enabled",
        )
        now = datetime.now(timezone.utc)
        return [
            policy
            for policy in policies
            if (policy.starts_at is None or policy.starts_at <= now)
            and (policy.expires_at is None or policy.expires_at > now)
            and any(
                self._department_matches(
                    current_key=current_key,
                    policy_department=department,
                    snapshot=snapshot,
                )
                for current_key in person.department_keys
                for department in policy.departments
            )
        ]

    def _department_matches(
        self,
        *,
        current_key: str,
        policy_department: PermissionPolicyDepartmentRecord,
        snapshot: OaDirectorySnapshot,
    ) -> bool:
        if current_key == policy_department.department_key:
            return True
        if not policy_department.include_children:
            return False
        current = snapshot.departments.get(current_key)
        seen: set[str] = set()
        while current is not None and current.parent_key and current.key not in seen:
            seen.add(current.key)
            if current.parent_key == policy_department.department_key:
                return True
            current = snapshot.departments.get(current.parent_key)
        return False

    def _task_allowed_by_system(self, task_name: str) -> bool:
        return task_name not in self.blocked_tasks and (
            not self.allowed_tasks or task_name in self.allowed_tasks
        )

    async def _generate_policy_name(
        self,
        *,
        organization_key: str,
        department_paths: list[str],
        task_scope_type: str,
        tasks: list[str],
    ) -> str:
        department_names = [path.split(" / ", 1)[0] for path in department_paths if path]
        fallback_department = "、".join(dict.fromkeys(department_names)) or organization_key
        fallback = f"{fallback_department}-测试造数"
        if self.name_llm_client is None:
            return fallback[:120]

        prompt = (
            "你负责为测试造数部门权限策略生成名称。只输出一个简短中文名称，不要解释、不要引号、不要 Markdown。"
            "名称应包含部门或组织和测试造数语义，长度不超过 30 个汉字。"
        )
        request = json.dumps(
            {
                "organization": organization_key,
                "departments": department_paths,
                "task_scope_type": task_scope_type,
                "tasks": tasks,
            },
            ensure_ascii=False,
        )
        try:
            reply = await self.name_llm_client.generate_reply(
                prompt,
                [LLMMessage(role="user", content=request)],
            )
        except Exception as exc:
            logger.warning("light 模型生成权限策略名称失败，使用兜底名称：%s", exc)
            return fallback[:120]
        candidate = next((line.strip() for line in reply.splitlines() if line.strip()), "")
        candidate = candidate.strip("`\"'“”‘’「」『』：: ").strip()
        return candidate[:120] or fallback[:120]

    @staticmethod
    def _normalize_datetime(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    @staticmethod
    def _department_snapshot(user: UserRecord, snapshot: OaDirectorySnapshot) -> dict[str, object]:
        person = snapshot.people.get(user.email.strip().lower())
        return {
            "loaded": snapshot.loaded,
            "department_keys": list(person.department_keys) if person else [],
            "department_paths": list(person.department_paths) if person else [],
            "department_name": person.department_name if person else user.department_name,
        }

    @staticmethod
    def _policy_dict(policy: PermissionPolicyRecord) -> dict[str, object]:
        return {
            "policy_id": policy.policy_id,
            "policy_name": policy.policy_name,
            "organization_key": policy.organization_key,
            "capability_key": policy.capability_key,
            "task_scope_type": policy.task_scope_type,
            "status": policy.status,
            "starts_at": policy.starts_at.isoformat() if policy.starts_at else None,
            "expires_at": policy.expires_at.isoformat() if policy.expires_at else None,
            "description": policy.description,
            "departments": [
                {
                    "department_key": item.department_key,
                    "department_path_snapshot": item.department_path_snapshot,
                    "include_children": item.include_children,
                }
                for item in policy.departments
            ],
            "tasks": policy.tasks,
        }
