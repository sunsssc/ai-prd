from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.business.workspace import access as workspace_access_module
from app.business.workspace.access import (
    SQLiteWorkspaceAccessStore,
    WorkspaceAccessError,
    WorkspaceAccessService,
)
from app.integrations.agent_runtime.models import RuntimeWorkspaceMount
from app.services.auth_models import UserRecord


def _user(
    user_id: str = "user_1",
    *,
    status: str = "active",
    role: str = "member",
    department_name: str | None = None,
    access_group: str | None = None,
) -> UserRecord:
    now = datetime.now(timezone.utc)
    return UserRecord(
        user_id=user_id,
        email=f"{user_id}@corp.test",
        name=user_id,
        avatar_url=None,
        status=status,
        role=role,
        default_role="member",
        agent_access="active",
        auth_provider="email",
        hosted_domain="corp.test",
        email_verified=True,
        department_name=department_name,
        access_group=access_group,
        created_at=now,
        first_login_at=now,
        last_login_at=now,
    )


def _service(tmp_path: Path) -> tuple[SQLiteWorkspaceAccessStore, WorkspaceAccessService]:
    store = SQLiteWorkspaceAccessStore(
        str(tmp_path / "workspace/runtime/db/workspace-access.sqlite3"),
        base_dir=tmp_path,
    )
    return store, WorkspaceAccessService(store=store, base_dir=tmp_path)


def _worktree_checkout(tmp_path: Path) -> tuple[Path, Path]:
    main_checkout = tmp_path / "main/ai-prd"
    common_git_dir = main_checkout / ".git"
    worktree_git_dir = common_git_dir / "worktrees/test"
    worktree_git_dir.mkdir(parents=True)
    (worktree_git_dir / "commondir").write_text("../..\n", encoding="utf-8")
    worktree = tmp_path / "worktree/ai-prd"
    worktree.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {worktree_git_dir}\n", encoding="utf-8")
    return worktree, main_checkout


def _mounts_by_path(plan):
    return {mount.sandbox_path: mount for mount in plan.mounts}


def test_workspace_knowledge_root_can_link_to_shared_checkout(tmp_path: Path) -> None:
    worktree, main_checkout = _worktree_checkout(tmp_path)
    shared_knowledge = main_checkout / "workspace/coinex/knowledge"
    shared_knowledge.mkdir(parents=True)
    knowledge_mount = worktree / "workspace/coinex/knowledge"
    knowledge_mount.parent.mkdir(parents=True)
    knowledge_mount.symlink_to(shared_knowledge, target_is_directory=True)

    store = SQLiteWorkspaceAccessStore(
        str(worktree / "workspace/runtime/db/workspace-access.sqlite3"),
        base_dir=worktree,
    )
    service = WorkspaceAccessService(store=store, base_dir=worktree)
    user = _user("linked_workspace_user")
    store.upsert_user_membership(
        user_id=user.user_id,
        organization_key="coinex",
        source="test",
        is_default=True,
    )

    plan = service.resolve_runtime_plan(user=user, organization_key="coinex", session_id="linked-workspace")

    assert _mounts_by_path(plan)["/coinex/knowledge"].host_path == str(shared_knowledge)


def test_workspace_knowledge_root_rejects_unrelated_symlink_target(tmp_path: Path) -> None:
    worktree, _ = _worktree_checkout(tmp_path)
    unrelated = tmp_path / "unrelated/knowledge"
    unrelated.mkdir(parents=True)
    knowledge_mount = worktree / "workspace/coinex/knowledge"
    knowledge_mount.parent.mkdir(parents=True)
    knowledge_mount.symlink_to(unrelated, target_is_directory=True)

    with pytest.raises(WorkspaceAccessError, match="不是主检出目录"):
        SQLiteWorkspaceAccessStore(
            str(worktree / "workspace/runtime/db/workspace-access.sqlite3"),
            base_dir=worktree,
        )


def test_runtime_plan_mounts_only_the_selected_organization_workspace(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    store.upsert_organization(organization_key="acme", display_name="Acme")
    user = _user("user_product", department_name="产品部门")
    store.upsert_user_membership(
        user_id=user.user_id,
        organization_key="coinex",
        source="test",
        is_default=True,
    )
    store.upsert_user_membership(
        user_id=user.user_id,
        organization_key="acme",
        source="test",
    )

    plan = service.resolve_runtime_plan(user=user, organization_key="acme", session_id="session_1")
    coinex_plan = service.resolve_runtime_plan(user=user, organization_key="coinex", session_id="session_coinex")

    assert plan.organization_key == "acme"
    assert plan.sandbox_cwd == "/"
    assert plan.host_shadow_root.endswith("workspace/runtime/sessions/session_1/root")
    mounts = _mounts_by_path(plan)
    assert mounts["/acme/knowledge"].permission == "read"
    assert mounts["/me"].permission == "write"
    assert mounts["/tmp"].permission == "write"
    assert all("/departments/" not in mount.sandbox_path for mount in plan.mounts)
    assert all(not mount.sandbox_path.startswith("/workspace") for mount in plan.mounts)
    assert Path(mounts["/acme/knowledge"].host_path).is_relative_to(
        tmp_path / "workspace/acme/knowledge"
    )
    assert Path(mounts["/me"].host_path).is_relative_to(
        tmp_path / "workspace/users/user_product"
    )
    assert mounts["/me"].host_path == _mounts_by_path(coinex_plan)["/me"].host_path
    assert all("/workspace/coinex/knowledge" not in mount.host_path for mount in plan.mounts)


def test_runtime_plan_materializes_readonly_turn_attachment_and_removes_stale_view(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    user = _user("attachment_user")
    store.upsert_user_membership(
        user_id=user.user_id,
        organization_key="coinex",
        source="test",
        is_default=True,
    )
    attachment_id = "11111111-2222-3333-4444-555555555555"
    attachment = (
        tmp_path
        / "workspace/runtime/uploads/assistant-images"
        / user.user_id
        / f"{attachment_id}.png"
    )
    attachment.parent.mkdir(parents=True)
    attachment.write_bytes(b"image-bytes")

    plan = service.resolve_runtime_plan(
        user=user,
        organization_key="coinex",
        session_id="session-attachment",
        extra_readonly_mounts=[
            RuntimeWorkspaceMount(
                workspace_id=f"attachment_{attachment_id}",
                workspace_key=f"attachment:{attachment_id}",
                host_path=str(attachment),
                sandbox_path=f"/attachments/{attachment.name}",
                permission="read",
            )
        ],
    )

    shadow_attachment = Path(plan.host_shadow_root) / "attachments" / attachment.name
    assert shadow_attachment.is_file()
    assert shadow_attachment.read_bytes() == b"image-bytes"
    assert _mounts_by_path(plan)[f"/attachments/{attachment.name}"].permission == "read"

    service.resolve_runtime_plan(
        user=user,
        organization_key="coinex",
        session_id="session-attachment",
    )
    assert not Path(plan.host_shadow_root, "attachments").exists()


def test_runtime_plan_materializes_codex_skill_symlink_view_with_user_override(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    user = _user("skill_user")
    store.upsert_user_membership(
        user_id=user.user_id,
        organization_key="coinex",
        source="test",
        is_default=True,
    )
    public_skills = tmp_path / "workspace/.claude/skills"
    user_skills = tmp_path / "workspace/users/skill_user/.claude/skills"
    for root, skill_name, content in [
        (public_skills, "public-only", "public"),
        (public_skills, "shared", "public shared"),
        (user_skills, "user-only", "user"),
        (user_skills, "shared", "user shared"),
    ]:
        skill_dir = root / skill_name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")
    (public_skills / "missing-entry").mkdir(parents=True)

    plan = service.resolve_runtime_plan(
        user=user,
        organization_key="coinex",
        session_id="skill_session",
    )
    skills_view = Path(plan.host_shadow_root) / ".agents/skills"

    assert skills_view.is_dir()
    assert not skills_view.is_symlink()
    assert sorted(path.name for path in skills_view.iterdir()) == ["public-only", "shared", "user-only"]
    assert (skills_view / "public-only").is_symlink()
    assert (skills_view / "public-only").resolve() == (public_skills / "public-only").resolve()
    assert (skills_view / "user-only").resolve() == (user_skills / "user-only").resolve()
    assert (skills_view / "shared").resolve() == (user_skills / "shared").resolve()
    assert (skills_view / "shared/SKILL.md").read_text(encoding="utf-8") == "user shared"

    public_skill = public_skills / "public-only"
    public_skill.rename(public_skills / "renamed")
    service.resolve_runtime_plan(
        user=user,
        organization_key="coinex",
        session_id="skill_session",
    )

    assert not (skills_view / "public-only").exists()
    assert (skills_view / "renamed").resolve() == (public_skills / "renamed").resolve()


def test_multi_organization_user_must_choose_when_no_default_exists(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    store.upsert_organization(organization_key="acme", display_name="Acme")
    user = _user("multi_org")
    store.upsert_user_membership(user_id=user.user_id, organization_key="coinex", source="test")
    store.upsert_user_membership(user_id=user.user_id, organization_key="acme", source="test")

    with pytest.raises(WorkspaceAccessError, match="多个组织"):
        service.resolve_active_organization(user, None)


def test_runtime_plan_can_explicitly_mount_multiple_authorized_organizations(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    store.upsert_organization(organization_key="acme", display_name="Acme")
    user = _user("multi_org_with_default")
    store.upsert_user_membership(
        user_id=user.user_id,
        organization_key="coinex",
        source="test",
        is_default=True,
    )
    store.upsert_user_membership(user_id=user.user_id, organization_key="acme", source="test")

    plan = service.resolve_runtime_plan(
        user=user,
        organization_key="coinex",
        organization_keys=["coinex", "acme"],
        session_id="session_multi",
    )
    mounts = _mounts_by_path(plan)

    assert plan.organization_key == "coinex"
    assert mounts["/coinex/knowledge"].permission == "read"
    assert mounts["/acme/knowledge"].permission == "read"
    assert mounts["/me"].permission == "write"
    assert mounts["/tmp"].permission == "write"


def test_user_without_active_organization_cannot_start_runtime(tmp_path: Path) -> None:
    _, service = _service(tmp_path)

    with pytest.raises(WorkspaceAccessError, match="等待管理员分配组织"):
        service.resolve_runtime_plan(user=_user("unassigned"), organization_key=None, session_id="session_1")


def test_reserved_sandbox_path_names_cannot_be_organization_keys(tmp_path: Path) -> None:
    store, _ = _service(tmp_path)

    with pytest.raises(WorkspaceAccessError, match="保留路径名"):
        store.upsert_organization(organization_key="me", display_name="Me")
    with pytest.raises(WorkspaceAccessError, match="保留路径名"):
        store.upsert_organization(organization_key="tmp", display_name="Tmp")


def test_access_request_approval_creates_target_membership_without_mixed_plan(tmp_path: Path) -> None:
    store, service = _service(tmp_path)
    store.upsert_organization(organization_key="acme", display_name="Acme")
    user = _user("requester")
    store.upsert_user_membership(
        user_id=user.user_id,
        organization_key="coinex",
        source="test",
        is_default=True,
    )

    request = service.create_access_request(
        user=user,
        target_organization_key="acme",
        reason="需要查看 Acme 需求",
        requested_scope="requirements",
        expires_at=None,
    )
    reviewed = store.review_access_request(
        request_id=request.request_id,
        reviewer_user_id="admin_1",
        status="approved",
        review_comment="同意",
    )
    memberships = store.list_active_user_memberships(user.user_id)
    plan = service.resolve_runtime_plan(user=user, organization_key="acme", session_id="session_2")

    assert reviewed.status == "approved"
    assert any(
        membership.organization_key == "acme" and membership.source == "approved_request"
        for membership in memberships
    )
    assert plan.organization_key == "acme"
    assert all("/workspace/coinex/knowledge" not in mount.host_path for mount in plan.mounts)
    assert "/acme/knowledge" in _mounts_by_path(plan)


def test_runtime_plan_switches_backend_git_mount_and_removes_it_for_baseline(
    tmp_path: Path,
) -> None:
    store, service = _service(tmp_path)
    user = _user()
    store.upsert_user_membership(
        user_id=user.user_id,
        organization_key="coinex",
        source="test",
        is_default=True,
    )
    revisions = tmp_path / "workspace/runtime/code-review-worktrees/coinex/repo/revisions"
    revision_a = revisions / ("a" * 40)
    revision_b = revisions / ("b" * 40)
    revision_a.mkdir(parents=True)
    revision_b.mkdir(parents=True)

    plan_a = service.resolve_runtime_plan(
        user=user,
        organization_key="coinex",
        session_id="session-git",
        extra_readonly_mounts=[
            RuntimeWorkspaceMount(
                workspace_id="git-a",
                workspace_key="git:repo:code",
                host_path=str(revision_a),
                sandbox_path="/repos/repo/code",
                permission="read",
            )
        ],
    )
    target = Path(plan_a.host_shadow_root) / "repos/repo/code"
    assert target.is_symlink()
    assert target.resolve() == revision_a.resolve()

    plan_b = service.resolve_runtime_plan(
        user=user,
        organization_key="coinex",
        session_id="session-git",
        extra_readonly_mounts=[
            RuntimeWorkspaceMount(
                workspace_id="git-b",
                workspace_key="git:repo:code",
                host_path=str(revision_b),
                sandbox_path="/repos/repo/code",
                permission="read",
            )
        ],
    )
    assert Path(plan_b.host_shadow_root, "repos/repo/code").resolve() == revision_b.resolve()
    assert next(mount for mount in plan_b.mounts if mount.sandbox_path == "/repos/repo/code").permission == "read"

    baseline = service.resolve_runtime_plan(
        user=user,
        organization_key="coinex",
        session_id="session-git",
    )
    assert not Path(baseline.host_shadow_root, "repos/repo").exists()


def test_runtime_plan_git_mount_switch_failure_keeps_previous_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, service = _service(tmp_path)
    user = _user()
    store.upsert_user_membership(
        user_id=user.user_id,
        organization_key="coinex",
        source="test",
        is_default=True,
    )
    revisions = tmp_path / "workspace/runtime/code-review-worktrees/coinex/repo/revisions"
    revision_a = revisions / ("a" * 40)
    revision_b = revisions / ("b" * 40)
    revision_a.mkdir(parents=True)
    revision_b.mkdir(parents=True)

    def git_mount(revision: Path) -> RuntimeWorkspaceMount:
        return RuntimeWorkspaceMount(
            workspace_id=f"git-{revision.name}",
            workspace_key="git:repo:code",
            host_path=str(revision),
            sandbox_path="/repos/repo/code",
            permission="read",
        )

    plan = service.resolve_runtime_plan(
        user=user,
        organization_key="coinex",
        session_id="session-git-failure",
        extra_readonly_mounts=[git_mount(revision_a)],
    )
    target = Path(plan.host_shadow_root) / "repos/repo/code"
    original_replace = workspace_access_module.os.replace

    def fail_git_view_switch(source, destination):
        if Path(destination).name == "repos":
            raise OSError("simulated atomic switch failure")
        return original_replace(source, destination)

    monkeypatch.setattr(workspace_access_module.os, "replace", fail_git_view_switch)
    with pytest.raises(OSError, match="simulated"):
        service.resolve_runtime_plan(
            user=user,
            organization_key="coinex",
            session_id="session-git-failure",
            extra_readonly_mounts=[git_mount(revision_b)],
        )

    assert target.is_symlink()
    assert target.resolve() == revision_a.resolve()
