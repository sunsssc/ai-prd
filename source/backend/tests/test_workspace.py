from __future__ import annotations

import asyncio
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

from conftest import reset_dependency_overrides
from app.business.assistant.service import AssistantService
from app.business.assistant.store import SQLiteAssistantStore
from app.business.requirement_reviews import RequirementReviewService, RequirementReviewStore
from app.business.requirement_reviews.service import _detect_risk_level
from app.business.workspace import WorkspaceBrowserService
from app.core.config import settings
from app.core.dependencies import (
    get_access_policy_service,
    get_auth_service,
    get_auth_store,
    get_email_sender,
    get_assistant_runtime_client,
    get_assistant_service,
    get_assistant_store,
    get_business_doc_update_service,
    get_business_doc_update_store,
    get_code_review_service,
    get_code_review_runtime_client,
    get_code_review_store,
    get_clickup_comment_client,
    get_github_pull_request_client,
    get_requirement_review_service,
    get_requirement_review_store,
    get_skill_ownership_store,
    get_google_oauth_service,
    get_my_task_service,
    get_notification_service,
    get_oauth_state_store,
    get_workspace_access_service,
    get_workspace_access_store,
    get_workspace_browser_service,
)
from app.integrations.agent_runtime.models import RuntimeEvent, RuntimeSession
from app.jobs.clickup_sync import (
    _assess_review_change,
    _collect_changed_files,
    _count_changed_characters,
    _snapshot_markdown_files,
)
from app.jobs.sync_state import write_sync_state
from app.main import app


def test_workspace_browser_keeps_logical_paths_for_symlinked_knowledge(tmp_path: Path) -> None:
    base_dir = tmp_path / "worktree"
    shared_knowledge = tmp_path / "shared/knowledge"
    requirements_root = shared_knowledge / "requirements"
    business_root = shared_knowledge / "business-docs"
    code_root = shared_knowledge / "code"
    for root in (requirements_root, business_root, code_root):
        root.mkdir(parents=True)
    (requirements_root / "docs").mkdir()
    (requirements_root / "docs/规则.md").write_text("# 规则\n", encoding="utf-8")
    knowledge_mount = base_dir / "workspace/coinex/knowledge"
    knowledge_mount.parent.mkdir(parents=True)
    knowledge_mount.symlink_to(shared_knowledge, target_is_directory=True)
    skills_root = base_dir / "workspace/.claude/skills"
    skills_root.mkdir(parents=True)

    browser = WorkspaceBrowserService(
        base_dir=base_dir,
        knowledge_roots={
            "requirements": knowledge_mount / "requirements",
            "business": knowledge_mount / "business-docs",
            "code": knowledge_mount / "code",
        },
        skills_root=skills_root,
    )

    tree = browser.list_knowledge_children("requirements")

    assert tree.root.path == "/coinex/knowledge/requirements"
    assert tree.root.children[0].path == "/coinex/knowledge/requirements/docs"


@pytest.fixture(autouse=True)
def reset_state(tmp_path: Path) -> None:
    get_google_oauth_service.cache_clear()
    get_access_policy_service.cache_clear()
    get_auth_service.cache_clear()
    get_auth_store.cache_clear()
    get_oauth_state_store.cache_clear()
    get_email_sender.cache_clear()
    get_notification_service.cache_clear()
    get_workspace_access_service.cache_clear()
    get_workspace_access_store.cache_clear()
    get_assistant_runtime_client.cache_clear()
    get_code_review_runtime_client.cache_clear()
    get_workspace_browser_service.cache_clear()
    get_assistant_service.cache_clear()
    get_assistant_store.cache_clear()
    get_business_doc_update_service.cache_clear()
    get_business_doc_update_store.cache_clear()
    get_code_review_service.cache_clear()
    get_code_review_store.cache_clear()
    get_clickup_comment_client.cache_clear()
    get_github_pull_request_client.cache_clear()
    get_requirement_review_service.cache_clear()
    get_requirement_review_store.cache_clear()
    get_skill_ownership_store.cache_clear()
    reset_dependency_overrides()

    original_auth_db_path = settings.auth_db_path
    original_assistant_db_path = settings.assistant_db_path
    original_email_domain = settings.email_login_allowed_domain
    original_delivery_mode = settings.email_delivery_mode
    original_debug_response = settings.email_verification_debug_response
    original_requirements_root = settings.knowledge_requirements_root
    original_business_root = settings.knowledge_business_root
    original_code_root = settings.knowledge_code_root
    original_skills_root = settings.skills_root
    original_working_directory = settings.ai_working_directory
    original_ai_provider = settings.ai_provider
    original_ai_model = settings.ai_model
    original_ai_max_turns = settings.ai_max_turns
    original_ai_effort = settings.ai_effort
    original_ai_cli_path = settings.ai_cli_path
    original_code_review_ai_max_turns = settings.code_review_ai_max_turns
    original_code_review_ai_effort = settings.code_review_ai_effort

    workspace_root = tmp_path / "workspace-data"
    requirements_root = workspace_root / "workspace/knowledge/requirements"
    business_root = workspace_root / "workspace/knowledge/business-docs"
    code_root = workspace_root / "workspace/knowledge/code"
    skills_root = workspace_root / "workspace/.claude/skills"

    requirements_root.mkdir(parents=True)
    business_root.mkdir(parents=True)
    code_root.mkdir(parents=True)
    skills_root.mkdir(parents=True)

    (requirements_root / "docs" / "营销").mkdir(parents=True)
    (requirements_root / "docs" / "images").mkdir()
    (requirements_root / "docs" / "_linked").mkdir()
    (requirements_root / "tasks" / "实现中").mkdir(parents=True)
    (requirements_root / "tasks" / "_deleted").mkdir(parents=True)
    (requirements_root / "docs" / "营销" / "优惠券规则.md").write_text("# 优惠券规则\n\n这里是需求正文。", encoding="utf-8")
    (requirements_root / "docs" / "images" / "coupon.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (requirements_root / "docs" / "_linked" / "关联文档.md").write_text("# 关联文档", encoding="utf-8")
    (requirements_root / "tasks" / "实现中" / "Web 下单优化.md").write_text("# ClickUp 需求上下文", encoding="utf-8")
    (requirements_root / "tasks" / "_deleted" / "废弃需求.md").write_text("# 废弃需求", encoding="utf-8")
    (business_root / "README.md").write_text("# 业务文档", encoding="utf-8")
    (code_root / "service.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    sync_state_dir = workspace_root / "workspace/runtime/sync-state"
    sync_state_dir.mkdir(parents=True)
    (sync_state_dir / "requirements.json").write_text(
        '{\n  "synced_at": "2026-04-25T03:26:38Z",\n  "added": 1,\n  "updated": 2,\n  "changed_files": ["tasks/_deleted/废弃需求.md", "docs/_linked/关联文档.md", "docs/营销/优惠券规则.md", "docs/营销/活动规则.md", "tasks/实现中/Web 下单优化.md"],\n  "errors": 0\n}',
        encoding="utf-8",
    )
    (skills_root / "coupon-review").mkdir()
    (skills_root / "coupon-review" / "SKILL.md").write_text("# 优惠券评审 Skill\n\n用于评审优惠券需求。", encoding="utf-8")

    settings.auth_db_path = str(tmp_path / "auth.sqlite3")
    settings.assistant_db_path = str(tmp_path / "assistant.sqlite3")
    settings.email_login_allowed_domain = "corp.test"
    settings.email_delivery_mode = "debug"
    settings.email_verification_debug_response = True
    settings.knowledge_requirements_root = str(requirements_root)
    settings.knowledge_business_root = str(business_root)
    settings.knowledge_code_root = str(code_root)
    settings.skills_root = str(skills_root)
    settings.ai_working_directory = str(workspace_root)

    yield

    settings.auth_db_path = original_auth_db_path
    settings.assistant_db_path = original_assistant_db_path
    settings.email_login_allowed_domain = original_email_domain
    settings.email_delivery_mode = original_delivery_mode
    settings.email_verification_debug_response = original_debug_response
    settings.knowledge_requirements_root = original_requirements_root
    settings.knowledge_business_root = original_business_root
    settings.knowledge_code_root = original_code_root
    settings.skills_root = original_skills_root
    settings.ai_working_directory = original_working_directory
    settings.ai_provider = original_ai_provider
    settings.ai_model = original_ai_model
    settings.ai_max_turns = original_ai_max_turns
    settings.ai_effort = original_ai_effort
    settings.ai_cli_path = original_ai_cli_path
    settings.code_review_ai_max_turns = original_code_review_ai_max_turns
    settings.code_review_ai_effort = original_code_review_ai_effort
    get_assistant_runtime_client.cache_clear()
    get_code_review_runtime_client.cache_clear()
    get_workspace_access_service.cache_clear()
    get_workspace_access_store.cache_clear()
    get_skill_ownership_store.cache_clear()
    get_clickup_comment_client.cache_clear()
    reset_dependency_overrides()


def test_sync_state_keeps_last_real_change_when_latest_sync_has_no_updates(tmp_path: Path) -> None:
    write_sync_state(
        base_dir=tmp_path,
        scope="requirements",
        added=0,
        updated=2,
        changed_files=["docs/优惠券规则.md", "tasks/Web 下单优化.md"],
        errors=0,
        synced_at=datetime(2026, 4, 25, 3, 26, 38, tzinfo=timezone.utc),
    )
    write_sync_state(
        base_dir=tmp_path,
        scope="requirements",
        added=0,
        updated=0,
        changed_files=[],
        errors=0,
        synced_at=datetime(2026, 4, 25, 3, 56, 38, tzinfo=timezone.utc),
    )

    payload = json.loads((tmp_path / "workspace/runtime/sync-state/requirements.json").read_text(encoding="utf-8"))
    assert payload["last_run"] == {
        "synced_at": "2026-04-25T03:56:38Z",
        "added": 0,
        "updated": 0,
        "changed_count": 0,
        "errors": 0,
    }
    assert payload["last_change"] == {
        "changed_at": "2026-04-25T03:26:38Z",
        "added": 0,
        "updated": 2,
        "changed_count": 2,
        "changed_files": ["docs/优惠券规则.md", "tasks/Web 下单优化.md"],
    }


def test_requirement_review_risk_prefers_overall_problem_level_over_item_level() -> None:
    markdown = (
        "# 评审结论\n\n"
        "- **整体问题级别**: 低\n"
        "- 概要：需求整体清晰。\n\n"
        "# 主要问题\n"
        "## 1. 验收边界补充\n"
        "- 严重级别：中\n"
        "- 依据：异常路径需要补充。\n\n"
        "> 严重级别：高 / 中 / 低\n"
    )

    assert _detect_risk_level(markdown) == "low"


def test_requirement_review_risk_uses_highest_issue_level_when_overall_missing() -> None:
    markdown = (
        "# 主要问题\n\n"
        "## 1. 验收边界补充\n"
        "- 严重级别：低\n\n"
        "## 2. 状态流转冲突\n"
        "- 严重级别：高\n"
    )

    assert _detect_risk_level(markdown) == "high"


def test_code_review_runtime_uses_independent_turns_and_effort() -> None:
    settings.ai_provider = "claude_code"
    settings.ai_model = "claude-sonnet-4-6"
    settings.ai_max_turns = 12
    settings.ai_effort = "low"
    settings.code_review_ai_max_turns = 100
    settings.code_review_ai_effort = "medium"
    settings.ai_cli_path = None
    get_assistant_runtime_client.cache_clear()
    get_code_review_runtime_client.cache_clear()

    assistant_runtime = get_assistant_runtime_client()
    code_review_runtime = get_code_review_runtime_client()

    assert assistant_runtime.max_turns == 12
    assert assistant_runtime.effort == "low"
    assert code_review_runtime.max_turns == 100
    assert code_review_runtime.effort == "medium"


class _FakeSkillCreateRuntime:
    provider = "fake"

    async def create_or_resume_session(
        self,
        *,
        runtime_session_id,
        working_directory=None,
        workspace_plan=None,
        tool_allowlist=None,
        system_prompt,
    ):
        del system_prompt
        runtime_working_directory = workspace_plan.host_shadow_root if workspace_plan else working_directory
        return RuntimeSession(
            provider=self.provider,
            working_directory=runtime_working_directory,
            session_id=runtime_session_id or "fake-skill-session",
            workspace_plan=workspace_plan,
            tool_allowlist=tool_allowlist,
        )

    async def send_message_stream(self, *, session, message, metadata):
        del session, message, metadata
        content = {
            "folder_name": "requirement-completeness-check",
            "name": "需求完整性检查",
            "description": "用于检查需求文档是否完整，输出缺失信息、风险和建议补充项。",
            "skill_markdown": (
                "---\n"
                "name: 需求完整性检查\n"
                "description: 用于检查需求文档是否完整，输出缺失信息、风险和建议补充项。\n"
                "creator: Workspace User\n"
                "created_at: 2026-04-28\n"
                "updated_at: 2026-04-28\n"
                "edit_log:\n"
                "  - 2026-04-28 Workspace User: 创建 Skill\n"
                "---\n\n"
                "# 需求完整性检查\n\n## 工作流程\n1. 阅读需求文档。\n2. 识别缺失信息。\n3. 输出风险和补充建议。\n"
            ),
            "scripts": [],
        }
        yield RuntimeEvent("activity", {"message": "已生成 Skill 草案"})
        yield RuntimeEvent(
            "message",
            {
                "content": (
                    "Skill 草案已生成：需求完整性检查。\n"
                    "<!--AI_PRD_SKILL_DRAFT "
                    + json.dumps(content, ensure_ascii=False)
                    + " -->"
                )
            },
        )

    async def list_available_skills(self, *, working_directory):
        del working_directory
        return []


class _VerboseSkillCreateRuntime(_FakeSkillCreateRuntime):
    async def send_message_stream(self, *, session, message, metadata):
        del session, message, metadata
        content = {
            "folder_name": "pr-business-doc-update",
            "name": "PR 业务文档更新",
            "description": "根据指定 PR 分析代码变更并更新业务文档时使用。",
            "skill_markdown": (
                "---\n"
                "name: PR 业务文档更新\n"
                "description: 根据指定 PR 分析代码变更并更新业务文档时使用。\n"
                "creator: Workspace User\n"
                "created_at: 2026-04-28\n"
                "updated_at: 2026-04-28\n"
                "edit_log:\n"
                "  - 2026-04-28 Workspace User: 创建 Skill\n"
                "---\n\n"
                "# PR 业务文档更新\n\n## 工作流程\n1. 读取 PR 变更。\n2. 识别业务影响。\n3. 更新业务文档。\n"
            ),
            "scripts": [],
        }
        yield RuntimeEvent("activity", {"message": "开始创建 Skill"})
        yield RuntimeEvent("message", {
            "content": (
                "已完成 PR 业务文档更新 Skill 草案。\n"
                "<!--AI_PRD_SKILL_DRAFT "
                + json.dumps(content, ensure_ascii=False)
                + " -->"
            )
        })


class _DraftOnlySkillCreateRuntime(_FakeSkillCreateRuntime):
    async def send_message_stream(self, *, session, message, metadata):
        del session, message, metadata
        content = {
            "folder_name": "prd-pr-test-cases",
            "name": "PRD PR 测试用例",
            "description": "根据 PRD 和 PR 变更生成测试用例时使用。",
            "skill_markdown": (
                "---\n"
                "name: PRD PR 测试用例\n"
                "description: 根据 PRD 和 PR 变更生成测试用例时使用。\n"
                "creator: Workspace User\n"
                "created_at: 2026-04-28\n"
                "updated_at: 2026-04-28\n"
                "edit_log:\n"
                "  - 2026-04-28 Workspace User: 创建 Skill\n"
                "---\n\n"
                "# PRD PR 测试用例\n\n## 工作流程\n1. 阅读 PRD。\n2. 阅读 PR 变更。\n3. 输出测试用例。\n"
            ),
            "scripts": [],
        }
        yield RuntimeEvent("activity", {"message": "已生成 Skill 草案"})
        yield RuntimeEvent(
            "message",
            {
                "content": (
                    "已完成 Skill 草案设计，后端将写入 `workspace/.claude/skills/prd-pr-test-cases/SKILL.md`。\n"
                    "<!--AI_PRD_SKILL_DRAFT "
                    + json.dumps(content, ensure_ascii=False)
                    + " -->"
                )
            },
        )


class _EscapedTemplateSkillCreateRuntime(_FakeSkillCreateRuntime):
    async def send_message_stream(self, *, session, message, metadata):
        del session, message, metadata
        content = {
            "skill_markdown": (
                "\\n# 业务测试用例 — [功能名称]\\n\\n"
                "## 使用场景\\n根据需求文档和 PR 变更生成业务测试用例。\\n\\n"
                "## 工作流程\\n1. 阅读需求。\\n2. 阅读 PR。\\n3. 生成测试用例。\\n\\n"
                "## 输出格式\\n- 测试用例表格\\n"
            ),
            "scripts": [],
        }
        yield RuntimeEvent(
            "message",
            {
                "content": (
                    "已完成 Skill 草案设计，后端将写入 `workspace/.claude/skills/business-test-case-generator/SKILL.md`。\n"
                    "<!--AI_PRD_SKILL_DRAFT "
                    + json.dumps(content, ensure_ascii=False)
                    + " -->"
                )
            },
        )


class _ImportedSkillMarkdownRuntime(_FakeSkillCreateRuntime):
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.working_directories: list[str] = []
        self.workspace_plans = []
        self.tool_allowlists: list[tuple[str, ...] | None] = []

    async def create_or_resume_session(self, **kwargs):
        session = await super().create_or_resume_session(**kwargs)
        self.workspace_plans.append(session.workspace_plan)
        self.tool_allowlists.append(session.tool_allowlist)
        return session

    async def send_message_stream(self, *, session, message, metadata):
        del metadata
        self.working_directories.append(session.working_directory)
        self.messages.append(message)
        skill_markdown = (
            "---\n"
            "name: automation-kit\n"
            "description: Audit automation package rules and run bundled scripts when users request release automation checks.\n"
            "---\n\n"
            "# Automation Kit\n\n"
            "## Workflow\n"
            "1. Read `references/rules.md`.\n"
            "2. Run `scripts/run.py` when deterministic validation is required.\n"
            "3. Return findings and evidence.\n"
        )
        yield RuntimeEvent(
            "message",
            {
                "content": (
                    "已读取上传包并生成 Skill 草案。\n"
                    "<!--AI_PRD_IMPORTED_SKILL_MD_START-->\n"
                    f"{skill_markdown}"
                    "<!--AI_PRD_IMPORTED_SKILL_MD_END-->"
                )
            },
        )


class _FailingImportedSkillMarkdownRuntime(_FakeSkillCreateRuntime):
    async def send_message_stream(self, *, session, message, metadata):
        del session, message, metadata
        if False:
            yield RuntimeEvent("message", {"content": ""})
        raise RuntimeError("生成服务异常")


class _TechReviewRuntime:
    provider = "test-review"

    async def create_or_resume_session(self, *, runtime_session_id, working_directory, system_prompt):
        del runtime_session_id, system_prompt
        return RuntimeSession(provider=self.provider, working_directory=working_directory, session_id="review-session")

    async def send_message_stream(self, *, session, message, metadata):
        del session, message, metadata
        yield RuntimeEvent(
            "message",
            {
                "content": (
                    "# 评审结论\n\n"
                    "- 整体问题级别：高\n"
                    "- 概要：优惠券规则需要补充状态流转与验收边界。\n\n"
                    "# 主要问题\n\n"
                    "## 1. 状态流转不完整\n"
                    "- 严重级别：高\n"
                    "- 依据：需求只描述领取动作，未说明使用和过期状态。\n"
                    "- 影响：会影响研发拆分状态机和验收用例。\n"
                    "- 建议：明确领取、使用、过期的边界条件。\n\n"
                    "# 分维度评审\n\n"
                    "| 维度 | 结论 | 说明 |\n"
                    "| --- | --- | --- |\n"
                    "| 状态迁移描述 | ⚠️ 不足 | 缺少完整状态迁移。|\n"
                )
            },
        )

    async def list_available_skills(self, *, working_directory):
        del working_directory
        return []


class _EmptyTechReviewRuntime(_TechReviewRuntime):
    async def send_message_stream(self, *, session, message, metadata):
        del session, message, metadata
        yield RuntimeEvent("complete", {"result": ""})


class _CapturingTechReviewRuntime(_TechReviewRuntime):
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_message_stream(self, *, session, message, metadata):
        self.messages.append(message)
        async for event in super().send_message_stream(session=session, message=message, metadata=metadata):
            yield event


async def _register_user(client: httpx.AsyncClient, email: str = "workspace@corp.test") -> str:
    send_code_response = await client.post(
        "/api/auth/email/request-code",
        json={"email": email, "purpose": "register"},
    )
    code = send_code_response.json()["debug_code"]
    register_response = await client.post(
        "/api/auth/email/register",
        json={"email": email, "name": "Workspace User", "code": code},
    )
    payload = register_response.json()
    user_id = payload["user"]["user_id"]
    get_auth_store().update_user_status(user_id, "active")
    get_auth_store().update_user_agent_access(user_id, "active")
    get_workspace_access_store().upsert_user_membership(
        user_id=user_id,
        organization_key="coinex",
        source="test",
        is_default=True,
    )
    return payload["session"]["token"]


def _assign_skill_owner(skill_key: str, email: str) -> None:
    user = get_auth_store().get_user_by_email(email)
    assert user is not None
    get_skill_ownership_store().create(skill_key=skill_key, creator_user_id=user.user_id)


@pytest.mark.anyio
async def test_requirement_review_rebuild_read_and_tree_badge(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="review@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    skill_dir = Path(settings.skills_root) / "requirement-check"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 需求评审 Skill\n\n输出问题级别与需求完整性建议。", encoding="utf-8")
    service = RequirementReviewService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        skills_root=Path(settings.skills_root),
        store=RequirementReviewStore(settings.assistant_db_path),
        runtime_client=_TechReviewRuntime(),
    )
    app.dependency_overrides[get_requirement_review_service] = lambda: service

    source_path = "workspace/knowledge/requirements/docs/营销/优惠券规则.md"
    rebuild_response = await client.post(
        "/api/requirements/reviews/rebuild",
        json={"source_path": source_path, "review_type": "tech_review"},
    )

    assert rebuild_response.status_code == 200
    review = rebuild_response.json()
    assert review["review_type"] == "tech_review"
    assert review["risk_level"] == "high"
    assert review["is_read"] is False
    assert (Path(settings.ai_working_directory) / review["path"]).is_file()

    meta_path = Path(settings.knowledge_requirements_root) / "__meta__/docs/营销/优惠券规则.md.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["latest_reviews"]["tech_review"] == review["review_id"]
    assert meta["reviews"][0]["path"] == review["path"]

    list_response = await client.get("/api/requirements/reviews", params={"source_path": source_path})
    assert list_response.status_code == 200
    assert list_response.json()["reviews"][0]["review_id"] == review["review_id"]

    tree_response = await client.get(
        "/api/knowledge/requirements/children",
        params={"path": "workspace/knowledge/requirements/docs/营销"},
    )
    assert tree_response.status_code == 200
    tree = tree_response.json()
    file_node = next(child for child in tree["root"]["children"] if child["path"] == source_path)
    assert tree["root"]["review_badge"]["unread_count"] == 1
    assert file_node["review_badge"]["latest_review_id"] == review["review_id"]

    detail_response = await client.get(f"/api/requirements/reviews/{review['review_id']}")
    assert detail_response.status_code == 200
    assert "优惠券规则需要补充" in detail_response.json()["content"]

    read_response = await client.post(f"/api/requirements/reviews/{review['review_id']}/read")
    assert read_response.status_code == 200
    assert read_response.json()["is_read"] is True

    refreshed_tree_response = await client.get(
        "/api/knowledge/requirements/children",
        params={"path": "workspace/knowledge/requirements/docs/营销"},
    )
    assert refreshed_tree_response.status_code == 200
    refreshed_tree = refreshed_tree_response.json()
    refreshed_file_node = next(child for child in refreshed_tree["root"]["children"] if child["path"] == source_path)
    assert refreshed_tree["root"]["review_badge"] is None
    assert refreshed_file_node["review_badge"] is None


@pytest.mark.anyio
async def test_requirement_review_job_fails_when_runtime_returns_empty_report() -> None:
    skill_dir = Path(settings.skills_root) / "requirement-check"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 需求评审 Skill", encoding="utf-8")
    service = RequirementReviewService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        skills_root=Path(settings.skills_root),
        store=RequirementReviewStore(settings.assistant_db_path),
        runtime_client=_EmptyTechReviewRuntime(),
    )
    source_path = "workspace/knowledge/requirements/docs/营销/优惠券规则.md"
    job = service.enqueue_review(source_path=source_path)

    review = await service.run_job(job)

    assert review is None
    failed = service.get_job(job_id=job.job_id)
    assert failed.status == "failed"
    assert failed.review_id is None
    assert "未返回有效评审正文" in (failed.error_message or "")


@pytest.mark.anyio
async def test_requirement_review_accepts_clickup_index_markdown(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="review-index@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    skill_dir = Path(settings.skills_root) / "requirement-check"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 需求评审 Skill\n\n输出问题级别与需求完整性建议。", encoding="utf-8")
    index_dir = Path(settings.knowledge_requirements_root) / "docs/营销/活动"
    index_dir.mkdir(parents=True)
    (index_dir / "_index.md").write_text("# 活动需求\n\n这里是 ClickUp 文档页正文。", encoding="utf-8")
    service = RequirementReviewService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        skills_root=Path(settings.skills_root),
        store=RequirementReviewStore(settings.assistant_db_path),
        runtime_client=_TechReviewRuntime(),
    )
    app.dependency_overrides[get_requirement_review_service] = lambda: service

    source_path = "workspace/knowledge/requirements/docs/营销/活动/_index.md"
    rebuild_response = await client.post(
        "/api/requirements/reviews/rebuild",
        json={"source_path": source_path, "review_type": "tech_review"},
    )

    assert rebuild_response.status_code == 200
    review = rebuild_response.json()
    list_response = await client.get("/api/requirements/reviews", params={"source_path": source_path})
    assert list_response.status_code == 200
    assert list_response.json()["reviews"][0]["review_id"] == review["review_id"]


@pytest.mark.anyio
async def test_requirement_review_async_job_status(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="review-job@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    skill_dir = Path(settings.skills_root) / "requirement-check"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 需求评审 Skill\n\n输出问题级别与需求完整性建议。", encoding="utf-8")
    service = RequirementReviewService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        skills_root=Path(settings.skills_root),
        store=RequirementReviewStore(settings.assistant_db_path),
        runtime_client=_TechReviewRuntime(),
    )
    app.dependency_overrides[get_requirement_review_service] = lambda: service

    source_path = "workspace/knowledge/requirements/docs/营销/优惠券规则.md"
    create_response = await client.post(
        "/api/requirements/reviews/jobs",
        json={"source_path": source_path, "review_type": "tech_review"},
    )

    assert create_response.status_code == 200
    job = create_response.json()
    assert job["source_path"] == source_path
    assert job["status"] in {"pending", "running", "completed"}

    for _ in range(20):
        status_response = await client.get(f"/api/requirements/reviews/jobs/{job['job_id']}")
        assert status_response.status_code == 200
        job = status_response.json()
        if job["status"] == "completed":
            break
        await asyncio.sleep(0.01)

    assert job["status"] == "completed"
    assert job["review"]["risk_level"] == "high"
    assert job["review"]["review_id"] == job["review_id"]


@pytest.mark.anyio
async def test_business_doc_pr_rebuild_endpoint_is_removed(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/api/business-docs/updates/rebuild",
        json={"repo_full_name": "example-org/example_backend", "pr_number": 1234},
    )

    assert response.status_code == 405


@pytest.mark.anyio
async def test_requirement_review_task_and_linked_doc_share_same_record() -> None:
    skill_dir = Path(settings.skills_root) / "requirement-check"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 需求评审 Skill\n\n输出问题级别与需求完整性建议。", encoding="utf-8")
    runtime = _CapturingTechReviewRuntime()
    service = RequirementReviewService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        skills_root=Path(settings.skills_root),
        store=RequirementReviewStore(settings.assistant_db_path),
        runtime_client=runtime,
    )

    task_relative_path = "tasks/实现中/Web 下单优化.md"
    doc_relative_path = "docs/营销/优惠券规则.md"
    task_source_path = f"workspace/knowledge/requirements/{task_relative_path}"
    doc_source_path = f"workspace/knowledge/requirements/{doc_relative_path}"
    task_file = Path(settings.knowledge_requirements_root) / task_relative_path
    task_file.write_text(
        "# ClickUp 需求上下文\n\n## 关联文档\n\n- [优惠券规则](../../docs/营销/优惠券规则.md)\n",
        encoding="utf-8",
    )

    review = await service.rebuild_review(source_path=task_source_path)

    assert review.source_paths == (task_source_path, doc_source_path)
    assert "ClickUp 需求上下文" in runtime.messages[0]
    assert "这里是需求正文" in runtime.messages[0]
    review_content = (Path(settings.ai_working_directory) / review.path).read_text(encoding="utf-8")
    assert "## 关联来源" in review_content
    assert f"[Task: Web 下单优化.md](<{task_source_path}>)" in review_content
    assert f"[Doc: 优惠券规则.md](<{doc_source_path}>)" in review_content

    task_meta_path = Path(settings.knowledge_requirements_root) / f"__meta__/{task_relative_path}.json"
    doc_meta_path = Path(settings.knowledge_requirements_root) / f"__meta__/{doc_relative_path}.json"
    task_meta = json.loads(task_meta_path.read_text(encoding="utf-8"))
    doc_meta = json.loads(doc_meta_path.read_text(encoding="utf-8"))

    assert task_meta["latest_reviews"]["tech_review"] == review.review_id
    assert doc_meta["latest_reviews"]["tech_review"] == review.review_id
    assert task_meta["reviews"][0]["source_paths"] == [task_source_path, doc_source_path]
    assert doc_meta["reviews"][0]["source_paths"] == [task_source_path, doc_source_path]
    assert service.list_reviews(source_path=doc_source_path, user_id="user-1")[0].review_id == review.review_id


def test_requirement_review_auto_enqueue_skips_tiny_completed_review_change() -> None:
    skill_dir = Path(settings.skills_root) / "requirement-check"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 需求评审 Skill\n\n输出问题级别与需求完整性建议。", encoding="utf-8")
    service = RequirementReviewService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        skills_root=Path(settings.skills_root),
        store=RequirementReviewStore(settings.assistant_db_path),
        runtime_client=_TechReviewRuntime(),
    )

    relative_path = "tasks/待确认/Web 下单优化.md"
    source_path = f"workspace/knowledge/requirements/{relative_path}"
    source_file = Path(settings.knowledge_requirements_root) / relative_path
    source_file.parent.mkdir(parents=True, exist_ok=True)
    before_text = "# Web 下单优化\n\n## 任务元信息\n\n- 状态: 待确认\n\n这里是需求正文。"
    source_file.write_text(before_text, encoding="utf-8")
    source_hash = service.compute_source_hash(source_path)
    job = service.store.create_job(source_path=source_path, source_hash=source_hash)
    review = service._write_review(job=job, generated_markdown="# 评审结论\n\n- 整体问题级别：低")
    service.store.complete_job(job_id=job.job_id, review_id=review.review_id, review_path=review.path)

    after_text = "# Web 下单优化\n\n## 任务元信息\n\n- 状态: 待确认\n\n这里是需求正文。小改"
    source_file.write_text(after_text, encoding="utf-8")

    jobs = service.enqueue_changed_files(
        [relative_path],
        change_assessments={relative_path: _assess_review_change(before_text, after_text)},
    )

    assert jobs == []


def test_requirement_review_source_hash_ignores_comments_and_review_linked_docs() -> None:
    skill_dir = Path(settings.skills_root) / "requirement-check"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 需求评审 Skill\n\n输出问题级别与需求完整性建议。", encoding="utf-8")
    service = RequirementReviewService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        skills_root=Path(settings.skills_root),
        store=RequirementReviewStore(settings.assistant_db_path),
        runtime_client=_TechReviewRuntime(),
    )

    relative_path = "tasks/待确认/Web 下单优化.md"
    source_path = f"workspace/knowledge/requirements/{relative_path}"
    source_file = Path(settings.knowledge_requirements_root) / relative_path
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(
        "# Web 下单优化\n\n"
        "## 任务元信息\n\n"
        "- 状态: 待确认\n\n"
        "## 关联文档\n\n"
        "- [优惠券规则](../../docs/营销/优惠券规则.md)\n\n"
        "## 核心需求描述\n\n"
        "这里是需求正文。",
        encoding="utf-8",
    )
    source_hash = service.compute_source_hash(source_path)

    source_file.write_text(
        "# Web 下单优化\n\n"
        "## 任务元信息\n\n"
        "- 状态: 待确认\n\n"
        "## 关联文档\n\n"
        "- [优惠券规则](../../docs/营销/优惠券规则.md)\n"
        "- [review_Web 下单优化_20260629_110000]"
        "(linked_docs/review_Web 下单优化_20260629_110000__doc_page/_index.md)\n\n"
        "## 核心需求描述\n\n"
        "这里是需求正文。\n\n"
        "## 评论\n\n"
        "> **AI PRD** · 2026-06-29 11:00:00 UTC\n\n"
        "自动需求评审完成：[review_20260629_110000_ab12cd]"
        "(https://agent.example/reviews/review_20260629_110000_ab12cd)",
        encoding="utf-8",
    )

    assert service.compute_source_hash(source_path) == source_hash


def test_requirement_review_change_assessment_ignores_comments_and_review_linked_docs() -> None:
    before_text = (
        "# Web 下单优化\n\n"
        "## 任务元信息\n\n"
        "- 状态: 待确认\n\n"
        "## 关联文档\n\n"
        "- [优惠券规则](../../docs/营销/优惠券规则.md)\n\n"
        "## 核心需求描述\n\n"
        "这里是需求正文。"
    )
    after_text = (
        "# Web 下单优化\n\n"
        "## 任务元信息\n\n"
        "- 状态: 待确认\n\n"
        "## 关联文档\n\n"
        "- [优惠券规则](../../docs/营销/优惠券规则.md)\n"
        "- [review_Web 下单优化_20260629_110000]"
        "(linked_docs/review_Web 下单优化_20260629_110000__doc_page/_index.md)\n\n"
        "## 核心需求描述\n\n"
        "这里是需求正文。\n\n"
        "## 评论\n\n"
        "> **AI PRD** · 2026-06-29 11:00:00 UTC\n\n"
        "自动需求评审完成：[review_20260629_110000_ab12cd]"
        "(https://agent.example/reviews/review_20260629_110000_ab12cd)"
    )

    assessment = _assess_review_change(before_text, after_text)

    assert assessment.changed_characters == 0
    assert assessment.should_auto_review is False


def test_requirement_review_auto_enqueue_runs_when_change_reaches_threshold() -> None:
    skill_dir = Path(settings.skills_root) / "requirement-check"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 需求评审 Skill\n\n输出问题级别与需求完整性建议。", encoding="utf-8")
    service = RequirementReviewService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        skills_root=Path(settings.skills_root),
        store=RequirementReviewStore(settings.assistant_db_path),
        runtime_client=_TechReviewRuntime(),
    )

    relative_path = "tasks/待确认/Web 下单优化.md"
    source_path = f"workspace/knowledge/requirements/{relative_path}"
    source_file = Path(settings.knowledge_requirements_root) / relative_path
    source_file.parent.mkdir(parents=True, exist_ok=True)
    before_text = "# Web 下单优化\n\n## 任务元信息\n\n- 状态: 待确认\n\n这里是需求正文。"
    source_file.write_text(before_text, encoding="utf-8")
    source_hash = service.compute_source_hash(source_path)
    job = service.store.create_job(source_path=source_path, source_hash=source_hash)
    review = service._write_review(job=job, generated_markdown="# 评审结论\n\n- 整体问题级别：低")
    service.store.complete_job(job_id=job.job_id, review_id=review.review_id, review_path=review.path)

    after_text = "# Web 下单优化\n\n## 任务元信息\n\n- 状态: 待确认\n\n这里是需求正文。新增自动评审阈值覆盖二十个字符以上并补充说明"
    source_file.write_text(after_text, encoding="utf-8")

    jobs = service.enqueue_changed_files(
        [relative_path],
        change_assessments={relative_path: _assess_review_change(before_text, after_text)},
    )

    assert len(jobs) == 1
    assert jobs[0].source_path == source_path


def test_requirement_review_auto_enqueue_respects_auto_review_switch() -> None:
    skill_dir = Path(settings.skills_root) / "requirement-check"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 需求评审 Skill\n\n输出问题级别与需求完整性建议。", encoding="utf-8")
    service = RequirementReviewService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        skills_root=Path(settings.skills_root),
        store=RequirementReviewStore(settings.assistant_db_path),
        runtime_client=_TechReviewRuntime(),
        requirement_review_auto_enabled=False,
    )

    relative_path = "tasks/待确认/Web 下单优化.md"
    before_text = "# Web 下单优化\n\n## 任务元信息\n\n- 状态: 待确认\n\n这里是需求正文。"
    after_text = "# Web 下单优化\n\n## 任务元信息\n\n- 状态: 待确认\n\n这里是需求正文。新增自动评审阈值覆盖二十个字符以上并补充说明"
    source_file = Path(settings.knowledge_requirements_root) / relative_path
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(after_text, encoding="utf-8")

    jobs = service.enqueue_changed_files(
        [relative_path],
        change_assessments={relative_path: _assess_review_change(before_text, after_text)},
    )

    assert jobs == []


def test_requirement_review_auto_enqueue_skips_existing_file_without_history_for_tiny_change() -> None:
    skill_dir = Path(settings.skills_root) / "requirement-check"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 需求评审 Skill\n\n输出问题级别与需求完整性建议。", encoding="utf-8")
    service = RequirementReviewService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        skills_root=Path(settings.skills_root),
        store=RequirementReviewStore(settings.assistant_db_path),
        runtime_client=_TechReviewRuntime(),
    )

    relative_path = "tasks/待确认/Web 下单优化.md"
    before_text = "# Web 下单优化\n\n## 任务元信息\n\n- 状态: 待确认\n\n这里是需求正文。"
    after_text = "# Web 下单优化\n\n## 任务元信息\n\n- 状态: 待确认\n\n这里是需求正文。小改"
    source_file = Path(settings.knowledge_requirements_root) / relative_path
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(after_text, encoding="utf-8")

    jobs = service.enqueue_changed_files(
        [relative_path],
        change_assessments={relative_path: _assess_review_change(before_text, after_text)},
    )

    assert jobs == []


def test_requirement_review_auto_enqueue_runs_for_new_file_without_history() -> None:
    skill_dir = Path(settings.skills_root) / "requirement-check"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 需求评审 Skill\n\n输出问题级别与需求完整性建议。", encoding="utf-8")
    service = RequirementReviewService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        skills_root=Path(settings.skills_root),
        store=RequirementReviewStore(settings.assistant_db_path),
        runtime_client=_TechReviewRuntime(),
    )

    relative_path = "tasks/待确认/新需求.md"
    source_file = Path(settings.knowledge_requirements_root) / relative_path
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text("# 新需求\n\n## 任务元信息\n\n- 状态: 待确认\n\n新增需求正文。", encoding="utf-8")

    jobs = service.enqueue_changed_files(
        [relative_path],
        change_assessments={
            relative_path: _assess_review_change(
                "",
                source_file.read_text(encoding="utf-8"),
                is_new_file=True,
            )
        },
    )

    assert len(jobs) == 1
    assert jobs[0].source_path == f"workspace/knowledge/requirements/{relative_path}"


def test_requirement_review_auto_enqueue_skips_non_pending_confirmation_task() -> None:
    skill_dir = Path(settings.skills_root) / "requirement-check"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 需求评审 Skill\n\n输出问题级别与需求完整性建议。", encoding="utf-8")
    service = RequirementReviewService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        skills_root=Path(settings.skills_root),
        store=RequirementReviewStore(settings.assistant_db_path),
        runtime_client=_TechReviewRuntime(),
    )

    relative_path = "tasks/实现中/Web 下单优化.md"
    before_text = "# Web 下单优化\n\n## 任务元信息\n\n- 状态: 实现中\n\n这里是需求正文。"
    after_text = "# Web 下单优化\n\n## 任务元信息\n\n- 状态: 实现中\n\n这里是需求正文。新增自动评审阈值覆盖二十个字符以上并补充说明"
    source_file = Path(settings.knowledge_requirements_root) / relative_path
    source_file.write_text(after_text, encoding="utf-8")

    jobs = service.enqueue_changed_files(
        [relative_path],
        change_assessments={relative_path: _assess_review_change(before_text, after_text)},
    )

    assert jobs == []


def test_requirement_review_auto_enqueue_skips_pending_confirmation_task_in_discovery_list() -> None:
    skill_dir = Path(settings.skills_root) / "requirement-check"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 需求评审 Skill\n\n输出问题级别与需求完整性建议。", encoding="utf-8")
    service = RequirementReviewService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        skills_root=Path(settings.skills_root),
        store=RequirementReviewStore(settings.assistant_db_path),
        runtime_client=_TechReviewRuntime(),
    )

    relative_path = "tasks/梳理中/Web 下单优化.md"
    before_text = "# Web 下单优化\n\n## 任务元信息\n\n- 状态: 待确认\n- List: 梳理中（产品）\n\n这里是需求正文。"
    after_text = "# Web 下单优化\n\n## 任务元信息\n\n- 状态: 待确认\n- List: 梳理中（产品）\n\n这里是需求正文。新增自动评审阈值覆盖二十个字符以上并补充说明"
    source_file = Path(settings.knowledge_requirements_root) / relative_path
    source_file.parent.mkdir(parents=True, exist_ok=True)
    source_file.write_text(after_text, encoding="utf-8")

    jobs = service.enqueue_changed_files(
        [relative_path],
        change_assessments={relative_path: _assess_review_change(before_text, after_text)},
    )

    assert jobs == []


def test_requirement_review_doc_change_enqueues_referencing_task_scope() -> None:
    skill_dir = Path(settings.skills_root) / "requirement-check"
    skill_dir.mkdir()
    (skill_dir / "SKILL.md").write_text("# 需求评审 Skill\n\n输出问题级别与需求完整性建议。", encoding="utf-8")
    service = RequirementReviewService(
        base_dir=Path(settings.ai_working_directory),
        requirements_root=Path(settings.knowledge_requirements_root),
        skills_root=Path(settings.skills_root),
        store=RequirementReviewStore(settings.assistant_db_path),
        runtime_client=_TechReviewRuntime(),
    )

    task_relative_path = "tasks/待确认/Web 下单优化.md"
    doc_relative_path = "docs/营销/优惠券规则.md"
    task_source_path = f"workspace/knowledge/requirements/{task_relative_path}"
    task_file = Path(settings.knowledge_requirements_root) / task_relative_path
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text(
        "# ClickUp 需求上下文\n\n## 任务元信息\n\n- 状态: 待确认\n\n## 关联文档\n\n- [优惠券规则](../../docs/营销/优惠券规则.md)\n",
        encoding="utf-8",
    )

    before_text = "# 优惠券规则\n\n这里是需求正文。"
    after_text = "# 优惠券规则\n\n这里是需求正文。补充任务引用文档后的联合评审触发说明，确保超过自动评审阈值。"
    doc_file = Path(settings.knowledge_requirements_root) / doc_relative_path
    doc_file.write_text(after_text, encoding="utf-8")

    jobs = service.enqueue_changed_files(
        [doc_relative_path],
        change_assessments={doc_relative_path: _assess_review_change(before_text, after_text)},
    )

    assert len(jobs) == 1
    assert jobs[0].source_path == task_source_path
    assert jobs[0].source_hash == service.compute_source_hash(task_source_path)


def test_clickup_change_collection_ignores_review_archive_linked_docs() -> None:
    requirements_root = Path(settings.knowledge_requirements_root)
    archive_file = (
        requirements_root
        / "tasks/待确认/linked_docs/review_Web 下单优化_20260629_110000__doc_page/_index.md"
    )
    archive_file.parent.mkdir(parents=True)
    archive_file.write_text("# review_Web 下单优化_20260629_110000\n\n旧评审内容", encoding="utf-8")
    before = _snapshot_markdown_files(requirements_root)
    archive_file.write_text("# review_Web 下单优化_20260629_110000\n\n新评审内容", encoding="utf-8")
    after = _snapshot_markdown_files(requirements_root)

    changed_files, change_assessments = _collect_changed_files(before, after, requirements_root)

    assert archive_file.as_posix() not in before
    assert archive_file.as_posix() not in after
    assert changed_files == []
    assert change_assessments == {}


def test_requirement_review_changed_character_counter_matches_review_threshold_unit() -> None:
    assert _count_changed_characters("这里是旧文案", "这里是新文案") == 1
    assert _count_changed_characters("正文", "正文一二三四五六七八九十一二三四五六七八九十") == 20


def test_requirement_review_change_assessment_ignores_section_keywords_below_threshold() -> None:
    before_text = "# 需求\n\n## 验收标准\n\n用户登录后展示余额。"
    after_text = "# 需求\n\n## 验收标准\n\n用户登录后展示余额和体验金入口。"

    assessment = _assess_review_change(before_text, after_text)

    assert assessment.changed_characters < assessment.ordinary_threshold
    assert assessment.should_auto_review is False


def test_requirement_review_change_assessment_ignores_word_and_number_semantics_below_threshold() -> None:
    before_text = "# 需求\n\n## 背景\n\n用户可以提现。"
    after_text = "# 需求\n\n## 背景\n\n用户不可以提现，每日最多 3 次。"

    assessment = _assess_review_change(before_text, after_text)

    assert assessment.changed_characters < assessment.ordinary_threshold
    assert assessment.should_auto_review is False


def test_requirement_review_change_assessment_uses_dynamic_threshold_for_large_docs() -> None:
    before_text = "# 需求\n\n## 背景\n\n" + "背景说明" * 700
    after_text = before_text + "普通描述补充三十个字左右作为背景材料"

    assessment = _assess_review_change(before_text, after_text)

    assert assessment.ordinary_threshold > 20
    assert assessment.changed_characters < assessment.ordinary_threshold
    assert assessment.should_auto_review is False


@pytest.mark.anyio
async def test_workspace_tree_and_file_endpoints_read_real_directories(client: httpx.AsyncClient) -> None:
    token = await _register_user(client)
    client.cookies.set(settings.session_cookie_name, token)

    tree_response = await client.get("/api/knowledge/requirements/children")
    assert tree_response.status_code == 200
    tree_payload = tree_response.json()
    assert tree_payload["root"]["path"] == "workspace/knowledge/requirements"
    assert tree_payload["default_file_path"] is None
    assert tree_payload["sync_summary"]["changed_count"] == 3
    assert tree_payload["sync_summary"]["sample_files"] == [
        "docs/营销/优惠券规则.md",
        "docs/营销/活动规则.md",
        "tasks/实现中/Web 下单优化.md",
    ]
    assert tree_payload["sync_summary"]["last_changed_at"] == "2026-04-25T03:26:38Z"
    assert tree_payload["sync_summary"]["last_changed_count"] == 3
    assert tree_payload["sync_summary"]["last_sample_files"] == [
        "docs/营销/优惠券规则.md",
        "docs/营销/活动规则.md",
        "tasks/实现中/Web 下单优化.md",
    ]
    assert tree_payload["root"]["children_loaded"] is True
    assert tree_payload["root"]["child_count"] == 2

    marketing_node = next(
        child for child in tree_payload["root"]["children"] if child["path"] == "workspace/knowledge/requirements/docs"
    )
    assert marketing_node["kind"] == "folder"
    assert marketing_node["children"] == []
    assert marketing_node["children_loaded"] is False
    assert marketing_node["has_children"] is True
    assert marketing_node["child_count"] is None

    tasks_node = next(
        child for child in tree_payload["root"]["children"] if child["path"] == "workspace/knowledge/requirements/tasks"
    )
    assert tasks_node["kind"] == "folder"
    assert tasks_node["has_children"] is True

    tasks_response = await client.get(
        "/api/knowledge/requirements/children",
        params={"path": "workspace/knowledge/requirements/tasks"},
    )
    assert tasks_response.status_code == 200
    tasks_payload = tasks_response.json()
    assert tasks_payload["root"]["path"] == "workspace/knowledge/requirements/tasks"
    assert tasks_payload["root"]["child_count"] == 1
    assert all(not child["name"].startswith("_") for child in tasks_payload["root"]["children"])
    assert tasks_payload["sync_summary"]["last_changed_count"] == 1
    assert tasks_payload["sync_summary"]["last_sample_files"] == ["实现中/Web 下单优化.md"]

    docs_response = await client.get(
        "/api/knowledge/requirements/children",
        params={"path": "workspace/knowledge/requirements/docs"},
    )
    assert docs_response.status_code == 200
    docs_payload = docs_response.json()
    assert docs_payload["root"]["path"] == "workspace/knowledge/requirements/docs"
    assert all(not child["name"].startswith("_") for child in docs_payload["root"]["children"])
    assert docs_payload["sync_summary"]["last_changed_count"] == 2
    assert docs_payload["sync_summary"]["last_sample_files"] == ["营销/优惠券规则.md", "营销/活动规则.md"]

    expand_response = await client.get(
        "/api/knowledge/requirements/children",
        params={"path": "workspace/knowledge/requirements/docs/营销"},
    )
    assert expand_response.status_code == 200
    expand_payload = expand_response.json()
    assert expand_payload["root"]["path"] == "workspace/knowledge/requirements/docs/营销"
    assert expand_payload["sync_summary"]["last_changed_count"] == 2
    assert expand_payload["sync_summary"]["last_sample_files"] == ["优惠券规则.md", "活动规则.md"]
    assert expand_payload["root"]["children_loaded"] is True
    assert expand_payload["root"]["child_count"] == 1
    assert expand_payload["root"]["children"][0]["path"] == "workspace/knowledge/requirements/docs/营销/优惠券规则.md"

    file_response = await client.get(
        "/api/knowledge/requirements/file",
        params={"path": "workspace/knowledge/requirements/docs/营销/优惠券规则.md"},
    )
    assert file_response.status_code == 200
    file_payload = file_response.json()
    assert file_payload["content_type"] == "markdown"
    assert "优惠券规则" in file_payload["content"]

    index_response = await client.get("/api/knowledge/index")
    assert index_response.status_code == 200
    index_payload = index_response.json()
    assert len(index_payload["prefixes"]) == 3
    assert [0, "docs", 1] in index_payload["directories"]
    assert [0, "docs/营销", 1] in index_payload["directories"]
    assert [0, "docs/营销/优惠券规则.md"] in index_payload["files"]
    assert [0, "tasks/实现中/Web 下单优化.md"] in index_payload["files"]
    assert [0, "tasks/_deleted", 1] not in index_payload["directories"]
    assert [0, "tasks/_deleted/废弃需求.md"] not in index_payload["files"]
    assert [0, "docs/_linked/关联文档.md"] not in index_payload["files"]
    assert [1, "README.md"] in index_payload["files"]
    assert [2, "service.py"] in index_payload["files"]


@pytest.mark.anyio
async def test_workspace_asset_endpoint_serves_knowledge_files(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="asset@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    asset_response = await client.get(
        "/api/knowledge/requirements/asset",
        params={"path": "workspace/knowledge/requirements/docs/images/coupon.png"},
    )
    assert asset_response.status_code == 200
    assert asset_response.headers["content-type"].startswith("image/png")
    assert asset_response.content == b"\x89PNG\r\n\x1a\n"


@pytest.mark.anyio
async def test_requirement_comments_read_create_and_reply_in_clickup(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="comments@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    source_path = "workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md"
    task_file = Path(settings.knowledge_requirements_root) / "tasks/实现中/Web 下单优化.md"
    task_file.write_text(
        "# Web 下单优化\n\n## 任务元信息\n\n- Task ID: `86task1`\n",
        encoding="utf-8",
    )

    class FakeClickUpComments:
        def __init__(self) -> None:
            self.created = []

        def list_task_comments(self, *, task_id: str) -> list[dict]:
            assert task_id == "86task1"
            return [
                {
                    "id": "comment-1",
                    "comment_text": [{"text": "请补充异常场景"}],
                    "user": {"username": "张三"},
                    "date": "1785664800000",
                    "reply_count": 1,
                }
            ]

        def list_threaded_comments(self, *, comment_id: str) -> list[dict]:
            assert comment_id == "comment-1"
            return [
                {
                    "id": "reply-1",
                    "comment_text": "已补充",
                    "user": {"username": "李四"},
                    "date": "1785668400000",
                }
            ]

        def create_task_text_comment(self, *, task_id: str, content: str, notify_all: bool) -> dict:
            self.created.append(("task", task_id, content, notify_all))
            return {"id": "comment-new"}

        def create_threaded_comment(self, *, comment_id: str, content: str, notify_all: bool) -> dict:
            self.created.append(("reply", comment_id, content, notify_all))
            return {"id": "reply-new"}

    clickup = FakeClickUpComments()
    app.dependency_overrides[get_clickup_comment_client] = lambda: clickup

    read_response = await client.get(
        "/api/knowledge/requirements/comments",
        params={"path": source_path},
    )
    assert read_response.status_code == 200
    comment = read_response.json()["comments"][0]
    assert comment["comment_id"] == "comment-1"
    assert comment["author"] == "张三"
    assert comment["content"] == "请补充异常场景"
    assert comment["replies"][0]["content"] == "已补充"

    create_response = await client.post(
        "/api/knowledge/requirements/comments",
        json={"path": source_path, "content": "新增评论"},
    )
    reply_response = await client.post(
        "/api/knowledge/requirements/comments",
        json={"path": source_path, "content": "回复内容", "parent_comment_id": "comment-1"},
    )
    assert create_response.status_code == 201
    assert reply_response.status_code == 201
    assert clickup.created == [
        ("task", "86task1", "新增评论", False),
        ("reply", "comment-1", "回复内容", False),
    ]

    invalid_reply_response = await client.post(
        "/api/knowledge/requirements/comments",
        json={"path": source_path, "content": "越界回复", "parent_comment_id": "other-comment"},
    )
    assert invalid_reply_response.status_code == 400
    assert invalid_reply_response.json()["detail"] == "回复目标不属于当前需求 Task。"


@pytest.mark.anyio
async def test_requirement_comments_reject_non_task_document(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="comments-doc@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    app.dependency_overrides[get_clickup_comment_client] = lambda: object()

    response = await client.get(
        "/api/knowledge/requirements/comments",
        params={"path": "workspace/knowledge/requirements/docs/营销/优惠券规则.md"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "只有有效的需求 Task 支持 ClickUp 评论。"


@pytest.mark.anyio
async def test_requirement_clickup_content_reads_and_updates_task_and_doc(
    client: httpx.AsyncClient,
) -> None:
    token = await _register_user(client, email="editor@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    task_path = "workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md"
    doc_path = "workspace/knowledge/requirements/docs/营销/优惠券规则.md"
    task_file = Path(settings.knowledge_requirements_root) / "tasks/实现中/Web 下单优化.md"
    doc_file = Path(settings.knowledge_requirements_root) / "docs/营销/优惠券规则.md"
    task_file.write_text(
        "# Web 下单优化\n\n"
        "## 任务元信息\n\n"
        "- Task ID: `86task1`\n\n"
        "## 核心需求描述\n\n"
        "# 原 Task\n\n"
        "## 验收标准\n\n"
        "- 原验收标准\n",
        encoding="utf-8",
    )
    doc_file.write_text(
        "# 优惠券规则\n\n"
        "- Page ID: `page-1`\n"
        "- Doc ID: `doc-1`\n"
        "- 链接: https://app.clickup.com/9000000001/docs/doc-1/page-1\n\n"
        "## 内容\n\n"
        "# 原需求\n",
        encoding="utf-8",
    )

    class FakeClickUpContent:
        def __init__(self) -> None:
            self.task_content = "# 原 Task"
            self.doc_content = "# 原需求"
            self.updated = []

        def get_task_markdown(self, *, task_id: str) -> dict[str, str]:
            assert task_id == "86task1"
            return {"title": "Web 下单优化", "content": self.task_content}

        def get_task_creator_email(self, *, task_id: str) -> str:
            assert task_id == "86task1"
            return "editor@corp.test"

        def update_task_markdown(self, *, task_id: str, content: str) -> None:
            assert task_id == "86task1"
            self.task_content = content
            self.updated.append(("task", content))

        def get_doc_page_markdown(self, *, workspace_id: str, doc_id: str, page_id: str) -> dict[str, str]:
            assert (workspace_id, doc_id, page_id) == ("9000000001", "doc-1", "page-1")
            return {"title": "优惠券规则", "content": self.doc_content}

        def get_doc_creator_email(self, *, workspace_id: str, doc_id: str) -> str:
            assert (workspace_id, doc_id) == ("9000000001", "doc-1")
            return "editor@corp.test"

        def update_doc_page_markdown(
            self,
            *,
            workspace_id: str,
            doc_id: str,
            page_id: str,
            content: str,
        ) -> None:
            assert (workspace_id, doc_id, page_id) == ("9000000001", "doc-1", "page-1")
            self.doc_content = content
            self.updated.append(("doc", content))

    clickup = FakeClickUpContent()

    app.dependency_overrides[get_clickup_comment_client] = lambda: clickup
    task_read = await client.get("/api/knowledge/requirements/clickup-content", params={"path": task_path})
    task_update = await client.put(
        "/api/knowledge/requirements/clickup-content",
        json={"path": task_path, "content": "# 新 Task", "base_content": "# 原 Task"},
    )
    doc_read = await client.get("/api/knowledge/requirements/clickup-content", params={"path": doc_path})
    doc_update = await client.put(
        "/api/knowledge/requirements/clickup-content",
        json={"path": doc_path, "content": "# 新需求", "base_content": "# 原需求"},
    )

    assert task_read.status_code == 200
    assert task_read.json()["content"] == "# 原 Task"
    assert task_read.json()["source_type"] == "task"
    assert task_update.status_code == 200
    assert task_update.json()["content"] == "# 新 Task"
    assert task_update.json()["local_synced"] is True
    assert doc_read.status_code == 200
    assert doc_read.json()["content"] == "# 原需求"
    assert doc_read.json()["source_type"] == "doc"
    assert doc_update.status_code == 200
    assert doc_update.json()["content"] == "# 新需求"
    assert clickup.updated == [("task", "# 新 Task"), ("doc", "# 新需求")]
    assert "## 核心需求描述\n\n# 新 Task\n\n## 验收标准" in task_file.read_text(encoding="utf-8")
    assert doc_file.read_text(encoding="utf-8").endswith("## 内容\n\n# 新需求")

    stale_update = await client.put(
        "/api/knowledge/requirements/clickup-content",
        json={"path": task_path, "content": "# 覆盖内容", "base_content": "# 原 Task"},
    )
    assert stale_update.status_code == 409
    assert clickup.updated == [("task", "# 新 Task"), ("doc", "# 新需求")]

    history = await client.get(
        "/api/knowledge/requirements/clickup-content/history",
        params={"path": task_path},
    )
    assert history.status_code == 200
    versions = history.json()["versions"]
    assert len(versions) == 1
    assert versions[0]["content_length"] == len("# 原 Task")

    restored = await client.post(
        f"/api/knowledge/requirements/clickup-content/history/{versions[0]['version_id']}/restore",
        json={"path": task_path},
    )
    assert restored.status_code == 200
    assert restored.json()["content"] == "# 原 Task"
    assert restored.json()["local_synced"] is True
    assert clickup.updated == [("task", "# 新 Task"), ("doc", "# 新需求"), ("task", "# 原 Task")]
    assert "## 核心需求描述\n\n# 原 Task\n\n## 验收标准" in task_file.read_text(encoding="utf-8")


def test_sync_requirement_local_markdown_includes_downloaded_figma_assets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.api import workspace as workspace_api
    from app.utils.figma import FigmaAssetSyncResult

    task_file = Path(settings.knowledge_requirements_root) / "tasks/实现中/Figma 同步.md"
    task_file.write_text(
        "# Figma 同步\n\n"
        "## 任务元信息\n\n"
        "- Task ID: `86figma`\n\n"
        "## 核心需求描述\n\n"
        "原需求\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    def fake_sync(content: str, **_: object) -> FigmaAssetSyncResult:
        calls.append(content)
        return FigmaAssetSyncResult(content=f"{content.rstrip()}\n\n## Figma 设计稿（本地同步）\n")

    monkeypatch.setattr(workspace_api, "sync_requirement_figma_assets", fake_sync)

    changed = workspace_api._sync_requirement_local_markdown(
        task_file,
        {"source_type": "task"},
        {
            "content": (
                "新需求\n\n"
                "设计稿：https://www.figma.com/design/FileKey12345/Page?node-id=1-2"
            )
        },
    )

    assert changed is True
    assert len(calls) == 1
    assert "figma.com/design/FileKey12345" in calls[0]
    assert "## Figma 设计稿（本地同步）" in task_file.read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_requirement_clickup_edit_access_returns_readonly_for_non_creator(
    client: httpx.AsyncClient,
) -> None:
    token = await _register_user(client, email="editor-access@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    task_path = "workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md"
    doc_path = "workspace/knowledge/requirements/docs/营销/优惠券规则.md"
    task_file = Path(settings.knowledge_requirements_root) / "tasks/实现中/Web 下单优化.md"
    doc_file = Path(settings.knowledge_requirements_root) / "docs/营销/优惠券规则.md"
    task_file.write_text("# Web 下单优化\n\n- Task ID: `86task1`\n", encoding="utf-8")
    doc_file.write_text(
        "# 优惠券规则\n\n"
        "- Page ID: `page-1`\n"
        "- Doc ID: `doc-1`\n"
        "- 链接: https://app.clickup.com/9000000001/docs/doc-1/page-1\n",
        encoding="utf-8",
    )

    class FakeCreators:
        def get_task_creator_email(self, *, task_id: str) -> str:
            assert task_id == "86task1"
            return "creator@corp.test"

        def get_doc_creator_email(self, *, workspace_id: str, doc_id: str) -> str:
            assert (workspace_id, doc_id) == ("9000000001", "doc-1")
            return "creator@corp.test"

    app.dependency_overrides[get_clickup_comment_client] = lambda: FakeCreators()

    task_response = await client.get(
        "/api/knowledge/requirements/clickup-edit-access",
        params={"path": task_path},
    )
    doc_response = await client.get(
        "/api/knowledge/requirements/clickup-edit-access",
        params={"path": doc_path},
    )

    assert task_response.status_code == 200
    assert task_response.json() == {
        "path": task_path,
        "source_type": "task",
        "can_edit": False,
        "reason": "只有创建人可以编辑该 ClickUp Task。",
    }
    assert doc_response.status_code == 200
    assert doc_response.json() == {
        "path": doc_path,
        "source_type": "doc",
        "can_edit": False,
        "reason": "只有创建人可以编辑该 ClickUp 需求文档。",
    }

    for source_path, expected_detail in (
        (task_path, "只有创建人可以编辑该 ClickUp Task。"),
        (doc_path, "只有创建人可以编辑该 ClickUp 需求文档。"),
    ):
        for method, url, kwargs in (
            ("get", "/api/knowledge/requirements/clickup-content", {"params": {"path": source_path}}),
            (
                "put",
                "/api/knowledge/requirements/clickup-content",
                {"json": {"path": source_path, "content": "# 修改", "base_content": "# 原需求"}},
            ),
            (
                "get",
                "/api/knowledge/requirements/clickup-content/history",
                {"params": {"path": source_path}},
            ),
            (
                "post",
                "/api/knowledge/requirements/clickup-content/history/version-1/restore",
                {"json": {"path": source_path}},
            ),
        ):
            response = await getattr(client, method)(url, **kwargs)
            assert response.status_code == 403
            assert response.json()["detail"] == expected_detail


@pytest.mark.anyio
async def test_personal_workspace_file_can_be_downloaded(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="personal-file@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    me_response = await client.get("/api/auth/me")
    user_id = me_response.json()["user"]["user_id"]
    file_path = Path(settings.ai_working_directory) / "workspace/users" / user_id / "files/report.md"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("# 个人报告\n\n已生成。", encoding="utf-8")
    spaced_file_path = file_path.parent / "report final.md"
    spaced_file_path.write_text("final", encoding="utf-8")

    response = await client.get("/api/workspace/me/file", params={"path": "me/files/report.md"})
    encoded_response = await client.get("/api/workspace/me/file", params={"path": "me/files/report%20final.md"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/markdown")
    assert "report.md" in response.headers["content-disposition"]
    assert response.text == "# 个人报告\n\n已生成。"
    assert encoded_response.status_code == 200
    assert encoded_response.text == "final"


@pytest.mark.anyio
async def test_personal_workspace_file_rejects_other_users_and_traversal(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="personal-file-deny@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    other_file = Path(settings.ai_working_directory) / "workspace/users/other-user/files/secret.md"
    other_file.parent.mkdir(parents=True, exist_ok=True)
    other_file.write_text("secret", encoding="utf-8")

    other_response = await client.get(
        "/api/workspace/me/file",
        params={"path": "workspace/users/other-user/files/secret.md"},
    )
    traversal_response = await client.get("/api/workspace/me/file", params={"path": "me/../other-user/files/secret.md"})

    assert other_response.status_code == 404
    assert traversal_response.status_code == 404


@pytest.mark.anyio
async def test_workspace_skill_file_can_be_updated(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="skill@corp.test")
    _assign_skill_owner("coupon-review", "skill@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    tree_response = await client.get("/api/skills/tree")
    assert tree_response.status_code == 200
    assert tree_response.json()["default_file_path"] == "workspace/.claude/skills/coupon-review/SKILL.md"

    update_response = await client.put(
        "/api/skills/file",
        json={
            "path": "workspace/.claude/skills/coupon-review/SKILL.md",
            "content": "# 新的 Skill 内容\n\n已保存到真实文件。",
        },
    )
    assert update_response.status_code == 200
    payload = update_response.json()
    assert "# 新的 Skill 内容\n\n已保存到真实文件。" in payload["content"]

    file_response = await client.get(
        "/api/skills/file",
        params={"path": "workspace/.claude/skills/coupon-review/SKILL.md"},
    )
    assert file_response.status_code == 200
    assert "已保存到真实文件" in file_response.json()["content"]
    assert "Workspace User: 更新 Skill 内容" in file_response.json()["content"]


@pytest.mark.anyio
async def test_workspace_skill_can_be_created_with_nested_files_and_metadata(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="new-skill@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    create_response = await client.post(
        "/api/skills",
        json={
            "name": "需求拆解",
            "description": "把需求拆成实现和测试关注点。",
            "creator_name": "伪造创建人",
            "intent": "用于把产品需求拆解成实现任务、风险和测试范围。",
            "files": [
                {
                    "path": "scripts/prepare.py",
                    "content": "print('prepare')\n",
                }
            ],
        },
    )

    assert create_response.status_code == 200
    payload = create_response.json()
    assert payload["path"] == "workspace/.claude/skills/需求拆解/SKILL.md"
    assert "creator: Workspace User" in payload["content"]
    assert "created_at:" in payload["content"]
    assert "Workspace User: 创建 Skill" in payload["content"]

    tree_response = await client.get("/api/skills/tree")
    created_node = next(node for node in tree_response.json()["root"]["children"] if node["name"] == "需求拆解")
    assert created_node["creator_email"] == "new-skill@corp.test"
    assert created_node["creator_name"] == "Workspace User"
    assert created_node["ownership_status"] == "owned"
    assert created_node["can_edit"] is True
    assert created_node["requires_admin_override"] is False
    assert "用于把产品需求拆解成实现任务" in payload["content"]

    script_response = await client.get(
        "/api/skills/file",
        params={"path": "workspace/.claude/skills/需求拆解/scripts/prepare.py"},
    )
    assert script_response.status_code == 200
    assert script_response.json()["content"] == "print('prepare')\n"

    duplicate_response = await client.post(
        "/api/skills",
        json={
            "name": "需求拆解",
        },
    )
    assert duplicate_response.status_code == 409


@pytest.mark.anyio
async def test_only_creator_can_edit_skill_and_admin_must_confirm_override(client: httpx.AsyncClient) -> None:
    owner_token = await _register_user(client, email="skill-owner@corp.test")
    client.cookies.set(settings.session_cookie_name, owner_token)
    create_response = await client.post(
        "/api/skills",
        json={"name": "权限校验", "description": "验证 Skill 所有权。", "content": "# 权限校验\n"},
    )
    assert create_response.status_code == 200

    other_token = await _register_user(client, email="skill-other@corp.test")
    client.cookies.set(settings.session_cookie_name, other_token)
    denied_response = await client.put(
        "/api/skills/file",
        json={
            "path": "workspace/.claude/skills/权限校验/SKILL.md",
            "content": "# 不应保存\n",
        },
    )
    assert denied_response.status_code == 403
    assert "只有创建人" in denied_response.json()["detail"]

    other_user = get_auth_store().get_user_by_email("skill-other@corp.test")
    assert other_user is not None
    get_auth_store().update_user_role(other_user.user_id, "admin")

    tree_response = await client.get("/api/skills/tree")
    skill_node = next(node for node in tree_response.json()["root"]["children"] if node["name"] == "权限校验")
    assert skill_node["can_edit"] is True
    assert skill_node["creator_email"] == "skill-owner@corp.test"
    assert skill_node["requires_admin_override"] is True

    warning_response = await client.put(
        "/api/skills/file",
        json={
            "path": "workspace/.claude/skills/权限校验/SKILL.md",
            "content": "# 管理员修改\n",
        },
    )
    assert warning_response.status_code == 409
    assert "请确认后重试" in warning_response.json()["detail"]

    confirmed_response = await client.put(
        "/api/skills/file",
        json={
            "path": "workspace/.claude/skills/权限校验/SKILL.md",
            "content": "# 管理员修改\n",
            "admin_override_confirmed": True,
        },
    )
    assert confirmed_response.status_code == 200
    assert "# 管理员修改" in confirmed_response.json()["content"]


@pytest.mark.anyio
async def test_historical_skill_is_admin_only_and_requires_confirmation(client: httpx.AsyncClient) -> None:
    member_token = await _register_user(client, email="historical-member@corp.test")
    client.cookies.set(settings.session_cookie_name, member_token)
    member_tree = await client.get("/api/skills/tree")
    historical_node = member_tree.json()["root"]["children"][0]
    assert historical_node["ownership_status"] == "unknown"
    assert historical_node["can_edit"] is False
    assert historical_node["can_delete"] is False

    admin_user = get_auth_store().get_user_by_email("historical-member@corp.test")
    assert admin_user is not None
    get_auth_store().update_user_role(admin_user.user_id, "admin")
    admin_tree = await client.get("/api/skills/tree")
    historical_node = admin_tree.json()["root"]["children"][0]
    assert historical_node["can_edit"] is True
    assert historical_node["requires_admin_override"] is True

    warning_response = await client.delete(
        "/api/skills/folder",
        params={"path": historical_node["path"]},
    )
    assert warning_response.status_code == 409

    delete_response = await client.delete(
        "/api/skills/folder",
        params={"path": historical_node["path"], "admin_override_confirmed": "true"},
    )
    assert delete_response.status_code == 200


def _build_skill_zip(files: dict[str, bytes | str]) -> bytes:
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for path, content in files.items():
            zip_file.writestr(path, content.encode("utf-8") if isinstance(content, str) else content)
    return archive.getvalue()


@pytest.mark.anyio
async def test_workspace_skill_zip_can_be_previewed_and_committed(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="zip-skill@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    archive = _build_skill_zip(
        {
            "release-helper/SKILL.md": "# 发布助手\n\n用于生成发布检查清单。\n",
            "release-helper/references/checklist.md": "# 检查项\n",
            "release-helper/assets/icon.png": b"\x89PNG\r\n\x1a\n",
        }
    )

    preview_response = await client.post(
        "/api/skills/import/preview",
        params={"filename": "uploaded.zip"},
        content=archive,
        headers={"Content-Type": "application/zip"},
    )
    assert preview_response.status_code == 200
    preview = preview_response.json()
    assert preview["suggested_folder_name"] == "release-helper"
    assert preview["has_skill_md"] is True
    assert {entry["path"] for entry in preview["entries"]} >= {
        "SKILL.md",
        "references/checklist.md",
        "assets/icon.png",
    }

    commit_response = await client.post(
        "/api/skills/import/commit",
        json={
            "token": preview["token"],
            "folder_name": "release-helper",
            "skill_md_mode": "existing",
            "skill_md_content": preview["skill_md_content"],
        },
    )
    assert commit_response.status_code == 200
    assert commit_response.json()["path"] == "workspace/.claude/skills/release-helper/SKILL.md"

    nested_response = await client.get(
        "/api/skills/file",
        params={"path": "workspace/.claude/skills/release-helper/references/checklist.md"},
    )
    assert nested_response.status_code == 200
    assert nested_response.json()["content"] == "# 检查项\n"


@pytest.mark.anyio
async def test_workspace_skill_zip_latest_preview_is_restored_per_user(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="zip-restore@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    first_response = await client.post(
        "/api/skills/import/preview",
        params={"filename": "first.zip"},
        content=_build_skill_zip({"SKILL.md": "# First\n"}),
        headers={"Content-Type": "application/zip"},
    )
    assert first_response.status_code == 200
    first_preview = first_response.json()

    invalid_response = await client.post(
        "/api/skills/import/preview",
        params={"filename": "invalid.zip"},
        content=b"not-a-zip",
        headers={"Content-Type": "application/zip"},
    )
    assert invalid_response.status_code == 400
    preserved_response = await client.get(
        "/api/skills/import/preview",
        params={"token": first_preview["token"]},
    )
    assert preserved_response.status_code == 200

    second_response = await client.post(
        "/api/skills/import/preview",
        params={"filename": "second.zip"},
        content=_build_skill_zip({"references/rules.md": "# Rules\n"}),
        headers={"Content-Type": "application/zip"},
    )
    assert second_response.status_code == 200
    second_preview = second_response.json()
    assert second_preview["generation_status"] == "not_started"

    replaced_response = await client.get(
        "/api/skills/import/preview",
        params={"token": first_preview["token"]},
    )
    assert replaced_response.status_code == 404
    assert replaced_response.json()["detail"] == "导入任务不存在。"

    second_metadata_path = (
        Path(settings.skills_root).parent / ".skill-imports" / second_preview["token"] / "metadata.json"
    )
    second_metadata = json.loads(second_metadata_path.read_text(encoding="utf-8"))
    second_metadata.pop("generation_tracking_version")
    second_metadata["created_at"] = 0
    second_metadata_path.write_text(json.dumps(second_metadata), encoding="utf-8")

    legacy_response = await client.get(
        "/api/skills/import/preview",
        params={"token": second_preview["token"]},
    )
    assert legacy_response.status_code == 200
    assert legacy_response.json()["generation_status"] == "unknown"

    latest_response = await client.get("/api/skills/import/latest")
    assert latest_response.status_code == 200
    latest = latest_response.json()["preview"]
    assert latest["token"] == second_response.json()["token"]
    assert latest["archive_name"] == "second.zip"
    assert latest["has_skill_md"] is False
    assert latest["entries"][0]["path"] == "rules.md"

    other_token = await _register_user(client, email="zip-restore-other@corp.test")
    client.cookies.set(settings.session_cookie_name, other_token)
    other_response = await client.get("/api/skills/import/latest")
    assert other_response.status_code == 200
    assert other_response.json() == {"preview": None}


@pytest.mark.anyio
async def test_workspace_skill_zip_draft_cancel_is_idempotent_and_owner_scoped(client: httpx.AsyncClient) -> None:
    owner_token = await _register_user(client, email="zip-cancel-owner@corp.test")
    client.cookies.set(settings.session_cookie_name, owner_token)
    preview_response = await client.post(
        "/api/skills/import/preview",
        params={"filename": "cancel.zip"},
        content=_build_skill_zip({"SKILL.md": "# Cancel\n"}),
        headers={"Content-Type": "application/zip"},
    )
    assert preview_response.status_code == 200
    preview_token = preview_response.json()["token"]

    other_token = await _register_user(client, email="zip-cancel-other@corp.test")
    client.cookies.set(settings.session_cookie_name, other_token)
    other_cancel_response = await client.delete(
        "/api/skills/import/draft",
        params={"token": preview_token},
    )
    assert other_cancel_response.status_code == 200
    assert other_cancel_response.json() == {"deleted": False}

    client.cookies.set(settings.session_cookie_name, owner_token)
    preserved_response = await client.get(
        "/api/skills/import/preview",
        params={"token": preview_token},
    )
    assert preserved_response.status_code == 200

    cancel_response = await client.delete(
        "/api/skills/import/draft",
        params={"token": preview_token},
    )
    assert cancel_response.status_code == 200
    assert cancel_response.json() == {"deleted": True}

    repeated_cancel_response = await client.delete(
        "/api/skills/import/draft",
        params={"token": preview_token},
    )
    assert repeated_cancel_response.status_code == 200
    assert repeated_cancel_response.json() == {"deleted": False}

    missing_response = await client.get(
        "/api/skills/import/preview",
        params={"token": preview_token},
    )
    assert missing_response.status_code == 404


@pytest.mark.anyio
async def test_workspace_skill_zip_can_generate_missing_skill_markdown(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="zip-auto-skill@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    archive = _build_skill_zip({"scripts/run.py": "print('ok')\n", "references/rules.md": "# 规则\n"})
    runtime = _ImportedSkillMarkdownRuntime()
    app.dependency_overrides[get_assistant_runtime_client] = lambda: runtime

    preview_response = await client.post(
        "/api/skills/import/preview",
        params={"filename": "automation-kit.zip"},
        content=archive,
        headers={"Content-Type": "application/zip"},
    )
    preview = preview_response.json()
    assert preview_response.status_code == 200
    assert preview["has_skill_md"] is False

    generate_response = await client.post(
        "/api/skills/import/generate",
        json={
            "token": preview["token"],
            "folder_name": "automation-kit",
        },
    )
    assert generate_response.status_code == 200
    assert generate_response.headers["content-type"].startswith("text/event-stream")
    stream_lines = [line for line in generate_response.text.splitlines() if line.startswith("data: ")]
    assert stream_lines[-1] == "data: [DONE]"
    stream_events = [json.loads(line.removeprefix("data: ")) for line in stream_lines[:-1]]
    assert any(event["type"] == "activity" for event in stream_events)
    generated = next(event for event in stream_events if event["type"] == "complete")
    assert "name: automation-kit" in generated["content"]
    assert "references/rules.md" in generated["content"]
    assert runtime.messages
    assert runtime.working_directories
    assert runtime.workspace_plans
    assert runtime.workspace_plans[0] is not None
    assert runtime.workspace_plans[0].user_id
    assert runtime.workspace_plans[0].organization_key == "skill-import"
    assert runtime.workspace_plans[0].mounts == []
    assert runtime.tool_allowlists == [("Read", "Glob", "Grep")]
    runtime_root = Path(runtime.working_directories[0])
    assert runtime_root.name == "workspace"
    assert runtime_root.parent.parent.name == ".skill-imports"
    assert (runtime_root / "scripts/run.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert (runtime_root / "references/rules.md").read_text(encoding="utf-8") == "# 规则\n"
    assert "当前工作目录就是本次上传包的根目录" in runtime.messages[0]
    assert "不得调用 Bash" in runtime.messages[0]
    assert "不得读取或搜索父级目录" in runtime.messages[0]
    assert "不得仅凭文件清单生成草案" in runtime.messages[0]
    assert "逐个读取包内所有可读文本文件的完整内容" in runtime.messages[0]
    assert "scripts/run.py" in runtime.messages[0]
    assert "references/rules.md" in runtime.messages[0]
    assert ".skill-imports" not in runtime.messages[0]

    restored_response = await client.get(
        "/api/skills/import/preview",
        params={"token": preview["token"]},
    )
    assert restored_response.status_code == 200
    restored = restored_response.json()
    assert restored["generation_status"] == "completed"
    assert restored["generation_message"] == "SKILL.md 已生成，可编辑后确认导入。"
    assert restored["generated_skill_md_content"] == generated["content"]
    assert restored["generation_error"] == ""

    commit_response = await client.post(
        "/api/skills/import/commit",
        json={
            "token": preview["token"],
            "folder_name": "automation-kit",
            "skill_md_mode": "generated",
            "skill_md_content": generated["content"],
        },
    )
    assert commit_response.status_code == 200
    file_response = await client.get(
        "/api/skills/file",
        params={"path": "workspace/.claude/skills/automation-kit/SKILL.md"},
    )
    assert file_response.status_code == 200
    assert file_response.json()["content"] == generated["content"]


@pytest.mark.anyio
async def test_workspace_skill_zip_rejects_concurrent_generation(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="zip-concurrent-generation@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    preview_response = await client.post(
        "/api/skills/import/preview",
        params={"filename": "concurrent-generation.zip"},
        content=_build_skill_zip({"references/rules.md": "# Rules\n"}),
        headers={"Content-Type": "application/zip"},
    )
    preview = preview_response.json()
    me_response = await client.get("/api/auth/me")
    user_id = me_response.json()["user"]["user_id"]
    get_workspace_browser_service().update_skill_import_generation(
        token=preview["token"],
        owner_id=user_id,
        status="running",
        message="正在生成。",
    )

    response = await client.post(
        "/api/skills/import/generate",
        json={"token": preview["token"], "folder_name": "concurrent-generation"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "SKILL.md 正在生成，请等待当前任务完成。"


@pytest.mark.anyio
async def test_workspace_skill_zip_persists_generation_failure(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="zip-failed-generation@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    app.dependency_overrides[get_assistant_runtime_client] = lambda: _FailingImportedSkillMarkdownRuntime()

    preview_response = await client.post(
        "/api/skills/import/preview",
        params={"filename": "failed-generation.zip"},
        content=_build_skill_zip({"scripts/run.py": "print('ok')\n"}),
        headers={"Content-Type": "application/zip"},
    )
    preview = preview_response.json()

    generate_response = await client.post(
        "/api/skills/import/generate",
        json={"token": preview["token"], "folder_name": "failed-generation"},
    )
    assert generate_response.status_code == 200
    assert '"type": "error"' in generate_response.text

    restored_response = await client.get(
        "/api/skills/import/preview",
        params={"token": preview["token"]},
    )
    restored = restored_response.json()
    assert restored["generation_status"] == "failed"
    assert restored["generation_message"] == "SKILL.md 生成失败。"
    assert restored["generation_error"] == "生成服务异常"


@pytest.mark.anyio
async def test_workspace_skill_zip_accepts_manual_skill_markdown(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="zip-manual-skill@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    archive = _build_skill_zip({"references/rules.md": "# 规则\n"})

    preview_response = await client.post(
        "/api/skills/import/preview",
        params={"filename": "manual-kit.zip"},
        content=archive,
        headers={"Content-Type": "application/zip"},
    )
    preview = preview_response.json()
    pending_commit_response = await client.post(
        "/api/skills/import/commit",
        json={
            "token": preview["token"],
            "folder_name": "manual-kit",
            "skill_md_mode": "manual",
            "skill_md_content": "",
        },
    )
    assert pending_commit_response.status_code == 400
    assert pending_commit_response.json()["detail"] == "请填写 SKILL.md 内容。"

    commit_response = await client.post(
        "/api/skills/import/commit",
        json={
            "token": preview["token"],
            "folder_name": "manual-kit",
            "skill_md_mode": "manual",
            "skill_md_content": "# 手动补充的 Skill\n\n使用手动说明。\n",
        },
    )
    assert commit_response.status_code == 200
    file_response = await client.get(
        "/api/skills/file",
        params={"path": "workspace/.claude/skills/manual-kit/SKILL.md"},
    )
    assert file_response.status_code == 200
    assert file_response.json()["content"] == "# 手动补充的 Skill\n\n使用手动说明。\n"


@pytest.mark.anyio
async def test_workspace_skill_zip_rejects_unsafe_paths(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="zip-unsafe-skill@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    archive = _build_skill_zip({"../SKILL.md": "# Unsafe\n"})

    response = await client.post(
        "/api/skills/import/preview",
        params={"filename": "unsafe.zip"},
        content=archive,
        headers={"Content-Type": "application/zip"},
    )
    assert response.status_code == 400
    assert "不安全" in response.json()["detail"]


@pytest.mark.anyio
async def test_workspace_skill_entries_can_be_created_moved_and_deleted(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="skill-entry@corp.test")
    _assign_skill_owner("coupon-review", "skill-entry@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    folder_response = await client.post(
        "/api/skills/entry",
        json={
            "parent_path": "workspace/.claude/skills/coupon-review",
            "name": "references",
            "kind": "folder",
        },
    )
    assert folder_response.status_code == 200

    file_response = await client.post(
        "/api/skills/entry",
        json={
            "parent_path": "workspace/.claude/skills/coupon-review/references",
            "name": "rules.md",
            "kind": "file",
            "content": "# 规则\n",
        },
    )
    assert file_response.status_code == 200

    move_response = await client.put(
        "/api/skills/entry",
        json={
            "path": "workspace/.claude/skills/coupon-review/references/rules.md",
            "destination_path": "workspace/.claude/skills/coupon-review/references/policy.md",
        },
    )
    assert move_response.status_code == 200

    delete_response = await client.delete(
        "/api/skills/entry",
        params={"path": "workspace/.claude/skills/coupon-review/references"},
    )
    assert delete_response.status_code == 200
    assert not any(child["name"] == "references" for child in delete_response.json()["root"]["children"][0]["children"])


@pytest.mark.anyio
async def test_workspace_skill_can_be_created_from_intent_only(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="intent-skill@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    app.dependency_overrides[get_assistant_runtime_client] = lambda: _FakeSkillCreateRuntime()

    create_response = await client.post(
        "/api/skills",
        json={
            "intent": "沉淀一个用于检查需求文档完整性的 Skill，输出缺失信息、风险和建议补充项。",
        },
    )

    assert create_response.status_code == 200
    payload = create_response.json()
    assert payload["mode"] == "agent"
    assert "Skill 草案已生成：需求完整性检查" in payload["agent_output"]
    assert payload["tree"]["default_file_path"] == "workspace/.claude/skills/coupon-review/SKILL.md"
    assert any(child["path"] == "workspace/.claude/skills/requirement-completeness-check" for child in payload["tree"]["root"]["children"])

    file_response = await client.get(
        "/api/skills/file",
        params={"path": "workspace/.claude/skills/requirement-completeness-check/SKILL.md"},
    )
    assert file_response.status_code == 200
    assert "name: 需求完整性检查" in file_response.json()["content"]


@pytest.mark.anyio
async def test_workspace_skill_intent_generation_returns_agent_output(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="fenced-skill@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    app.dependency_overrides[get_assistant_runtime_client] = lambda: _VerboseSkillCreateRuntime()

    create_response = await client.post(
        "/api/skills",
        json={
            "intent": "编写一个根据指定 PR 更新业务文档的 skill。",
        },
    )

    assert create_response.status_code == 200
    payload = create_response.json()
    assert payload["mode"] == "agent"
    assert "开始创建 Skill" in payload["agent_output"]
    assert "[tool_use] Write" not in payload["agent_output"]
    assert "已完成 PR 业务文档更新 Skill 草案" in payload["agent_output"]
    assert any(child["path"] == "workspace/.claude/skills/pr-business-doc-update" for child in payload["tree"]["root"]["children"])


@pytest.mark.anyio
async def test_workspace_skill_stream_backend_writes_agent_draft(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="stream-draft-skill@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    service = AssistantService(
        store=SQLiteAssistantStore(settings.assistant_db_path),
        runtime_client=_DraftOnlySkillCreateRuntime(),
        system_prompt=settings.ai_system_prompt,
        runtime_working_directory=settings.ai_working_directory,
    )
    app.dependency_overrides[get_assistant_service] = lambda: service

    create_response = await client.post(
        "/api/skills/stream",
        json={
            "intent": "创建一个根据 PRD 和 PR 变更生成测试用例的 Skill。",
        },
    )

    assert create_response.status_code == 200
    body = create_response.text
    assert "已由后端写入 workspace/.claude/skills/prd-pr-test-cases/SKILL.md" in body
    assert '"created_skill_paths": ["workspace/.claude/skills/prd-pr-test-cases/SKILL.md"]' in body

    file_response = await client.get(
        "/api/skills/file",
        params={"path": "workspace/.claude/skills/prd-pr-test-cases/SKILL.md"},
    )
    assert file_response.status_code == 200
    assert "name: PRD PR 测试用例" in file_response.json()["content"]

    sessions_response = await client.get("/api/assistant/sessions")
    assert sessions_response.status_code == 200
    sessions = sessions_response.json()
    assert any("创建 Skill" in session["title"] for session in sessions)
    detail_response = await client.get(f"/api/assistant/sessions/{sessions[0]['session_id']}")
    assert detail_response.status_code == 200
    assistant_messages = [message for message in detail_response.json()["messages"] if message["role"] == "assistant"]
    assert assistant_messages
    assert "```json" not in assistant_messages[-1]["content"]


@pytest.mark.anyio
async def test_workspace_skill_stream_normalizes_poor_agent_draft(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="escaped-template-skill@corp.test")
    client.cookies.set(settings.session_cookie_name, token)
    service = AssistantService(
        store=SQLiteAssistantStore(settings.assistant_db_path),
        runtime_client=_EscapedTemplateSkillCreateRuntime(),
        system_prompt=settings.ai_system_prompt,
        runtime_working_directory=settings.ai_working_directory,
    )
    app.dependency_overrides[get_assistant_service] = lambda: service

    create_response = await client.post(
        "/api/skills/stream",
        json={
            "intent": "帮我创建一个，根据指定需求文档+代码实现（PR）来生成业务测试用例的 skill 吧",
        },
    )

    assert create_response.status_code == 200
    body = create_response.text
    assert "workspace/.claude/skills/business-test-case-generator/SKILL.md" in body
    assert "workspace/.claude/skills/帮我创建一个/SKILL.md" not in body

    file_response = await client.get(
        "/api/skills/file",
        params={"path": "workspace/.claude/skills/business-test-case-generator/SKILL.md"},
    )
    assert file_response.status_code == 200
    content = file_response.json()["content"]
    assert "name: 业务测试用例生成" in content
    assert "\\n# 业务测试用例" not in content
    assert "# 业务测试用例" in content


@pytest.mark.anyio
async def test_workspace_skill_folder_can_be_deleted(client: httpx.AsyncClient) -> None:
    token = await _register_user(client, email="delete-skill@corp.test")
    _assign_skill_owner("coupon-review", "delete-skill@corp.test")
    client.cookies.set(settings.session_cookie_name, token)

    delete_response = await client.delete(
        "/api/skills/folder",
        params={"path": "workspace/.claude/skills/coupon-review"},
    )

    assert delete_response.status_code == 200
    payload = delete_response.json()
    assert payload["default_file_path"] is None
    assert payload["root"]["children"] == []

    file_response = await client.get(
        "/api/skills/file",
        params={"path": "workspace/.claude/skills/coupon-review/SKILL.md"},
    )
    assert file_response.status_code == 404
