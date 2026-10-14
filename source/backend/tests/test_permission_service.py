from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.integrations.vinotech_oa import OaDepartment, OaDirectorySnapshot, OaPerson
from app.services.auth_models import UserRecord
from app.services.permission_service import BusinessPermissionService
from app.services.permission_store import SQLitePermissionStore


class FakeDirectory:
    def __init__(self, snapshot: OaDirectorySnapshot) -> None:
        self._snapshot = snapshot

    async def snapshot(self) -> OaDirectorySnapshot:
        return self._snapshot


class FakeWorkspaceAccess:
    def resolve_active_organization(self, user: UserRecord, organization_key: str) -> object:
        del user, organization_key
        return object()


class FakeNameLLM:
    def __init__(self, reply: str = "研发中心资产调整") -> None:
        self.reply = reply
        self.messages: list[object] = []

    async def generate_reply(self, system_prompt: str, messages: list[object]) -> str:
        self.messages = [system_prompt, *messages]
        return self.reply


def _user(*, email: str = "dev@corp.test", agent_access: str = "active") -> UserRecord:
    now = datetime.now(timezone.utc)
    return UserRecord(
        user_id="user-1",
        email=email,
        name="Dev",
        avatar_url=None,
        status="active",
        role="member",
        default_role="member",
        agent_access=agent_access,
        auth_provider="email",
        hosted_domain="corp.test",
        email_verified=True,
        department_name="研发中心 / 后端组",
        access_group=None,
        created_at=now,
        first_login_at=now,
        last_login_at=now,
    )


def _service(tmp_path, *, loaded: bool = True) -> BusinessPermissionService:
    departments = {
        "rd": OaDepartment("rd", "研发中心", None, "研发中心"),
        "backend": OaDepartment("backend", "后端组", "rd", "研发中心 / 后端组"),
        "vendor": OaDepartment("vendor", "外包组", None, "外包组"),
    }
    directory = FakeDirectory(
        OaDirectorySnapshot(
            people={
                "dev@corp.test": OaPerson(
                    "研发中心 / 后端组",
                    1,
                    department_keys=("backend",),
                    department_paths=("研发中心 / 后端组",),
                )
            },
            loaded=loaded,
            departments=departments,
        )
    )
    return BusinessPermissionService(
        store=SQLitePermissionStore(str(tmp_path / "auth.sqlite3")),
        oa_directory=directory,
        workspace_access=FakeWorkspaceAccess(),
        allowed_organizations=["coinex"],
        allowed_tasks=["adjust_user_asset", "register_user"],
        blocked_tasks=["register_user"],
    )


@pytest.mark.anyio
async def test_department_policy_matches_children_and_unions_tasks(tmp_path) -> None:
    service = _service(tmp_path)
    await service.build_policy(
        policy_id=None,
        policy_name="研发中心造数",
        organization_key="coinex",
        capability_key="test_data",
        department_keys=["rd"],
        include_children=True,
        task_scope_type="selected",
        tasks=["adjust_user_asset"],
        status="enabled",
        starts_at=None,
        expires_at=None,
        description=None,
        actor_user_id="admin-1",
    )

    allowed = await service.check_capability_access(_user(), "coinex", "test_data", "adjust_user_asset")
    blocked = await service.check_capability_access(_user(), "coinex", "test_data", "register_user")

    assert allowed.allowed is True
    assert allowed.matched_policy_name == "研发中心造数"
    assert blocked.allowed is False
    assert "系统级造数范围" in blocked.reason


@pytest.mark.anyio
async def test_missing_oa_snapshot_denies_even_if_cached_person_exists(tmp_path) -> None:
    service = _service(tmp_path, loaded=False)
    decision = await service.check_capability_access(_user(), "coinex", "test_data")

    assert decision.allowed is False
    assert decision.reason == "部门信息暂时无法确认。"


@pytest.mark.anyio
async def test_empty_policy_name_uses_light_model(tmp_path) -> None:
    service = _service(tmp_path)
    llm = FakeNameLLM()
    service.name_llm_client = llm

    policy = await service.build_policy(
        policy_id=None,
        policy_name=None,
        organization_key="coinex",
        capability_key="test_data",
        department_keys=["rd"],
        include_children=True,
        task_scope_type="selected",
        tasks=["adjust_user_asset"],
        status="enabled",
        starts_at=None,
        expires_at=None,
        description=None,
        actor_user_id="admin-1",
    )

    assert policy.policy_name == "研发中心资产调整"
    assert len(llm.messages) == 2


@pytest.mark.anyio
async def test_policy_status_toggle_and_delete_keep_audit_history(tmp_path) -> None:
    service = _service(tmp_path)
    policy = await service.build_policy(
        policy_id=None,
        policy_name="研发中心 UAT",
        organization_key="coinex",
        capability_key="test_data",
        department_keys=["rd"],
        include_children=True,
        task_scope_type="all",
        tasks=[],
        status="enabled",
        starts_at=None,
        expires_at=None,
        description=None,
        actor_user_id="admin-1",
    )

    disabled = service.store.update_policy_status(
        policy_id=policy.policy_id,
        status="disabled",
        actor_user_id="admin-1",
    )
    deleted = service.store.delete_policy(policy_id=policy.policy_id, actor_user_id="admin-1")

    assert disabled is not None and disabled.status == "disabled"
    assert deleted is not None and deleted.status == "deleted"
    assert {item.action for item in service.store.list_policy_audits(policy.policy_id)} == {
        "deleted",
        "status_changed",
        "created",
    }
