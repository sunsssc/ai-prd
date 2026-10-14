from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.core import dependencies as deps
from app.core.config import Settings, TaskRuntimeConfig, settings
from app.integrations.agent_runtime import RuntimeWorkspacePlan
from app.api.workspace import router


@pytest.fixture(autouse=True)
def isolated_runtime_settings(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "codex")
    monkeypatch.setattr(settings, "ai_model", "global-codex-model")
    monkeypatch.setattr(settings, "ai_api_key", "global-test-key")
    monkeypatch.setattr(settings, "ai_base_url", "https://global.example.invalid")
    monkeypatch.setattr(settings, "ai_cli_path", "/global/codex")
    monkeypatch.setattr(settings, "ai_task_runtimes", {})
    cached = [deps.get_assistant_runtime_client, deps.get_code_review_runtime_client, deps.get_security_scan_runtime_client]
    for getter in cached:
        getter.cache_clear()
    yield
    for getter in cached:
        getter.cache_clear()


@pytest.mark.parametrize("getter", [
    deps.get_assistant_runtime_client, deps.get_code_review_runtime_client,
    deps.get_requirement_review_runtime_client, deps.get_business_doc_update_runtime_client,
    deps.get_security_scan_runtime_client, deps.get_skill_read_runtime_client,
])
def test_unconfigured_task_inherits_global_runtime(getter):
    runtime = getter()
    assert runtime.provider == "codex"
    assert runtime.model == "global-codex-model"
    assert runtime.api_key == "global-test-key"
    assert runtime.cli_path == "/global/codex"


@pytest.mark.parametrize("task,getter", [
    ("assistant", deps.get_assistant_runtime_client), ("code_review", deps.get_code_review_runtime_client),
    ("requirement_review", deps.get_requirement_review_runtime_client),
    ("business_doc_update", deps.get_business_doc_update_runtime_client),
    ("security_scan", deps.get_security_scan_runtime_client), ("skill_read", deps.get_skill_read_runtime_client),
])
def test_each_task_uses_its_profile_without_cross_provider_defaults(monkeypatch, task, getter):
    monkeypatch.setattr(settings, "ai_task_runtimes", {task: TaskRuntimeConfig(provider="claude", model="task-claude-model")})
    runtime = getter()
    assert runtime.provider == "claude_code"
    assert runtime.model == "task-claude-model"
    assert runtime.api_key == ""
    assert runtime.cli_path == ""
    assert runtime.base_url == "https://api.anthropic.com"


def test_same_provider_override_keeps_other_global_settings(monkeypatch):
    monkeypatch.setattr(settings, "ai_task_runtimes", {"code_review": TaskRuntimeConfig(model="review-model", api_key="")})
    runtime = deps.get_code_review_runtime_client()
    assert runtime.model == "review-model"
    assert runtime.api_key == ""  # An explicit empty string disables global key inheritance.
    assert runtime.cli_path == "/global/codex"
    assert deps.get_assistant_runtime_client().model == "global-codex-model"


def test_explicit_claude_connection_profile(monkeypatch):
    monkeypatch.setattr(settings, "ai_task_runtimes", {"security_scan": TaskRuntimeConfig(
        provider="claude", model="scan-model", api_key="scan-test-key",
        base_url="https://scan.example.invalid", cli_path="/tools/claude",
    )})
    runtime = deps.get_security_scan_runtime_client()
    assert (runtime.model, runtime.api_key, runtime.base_url, runtime.cli_path) == (
        "scan-model", "scan-test-key", "https://scan.example.invalid", "/tools/claude",
    )
    assert runtime.max_turns == settings.security_scan_ai_max_turns


def test_cross_provider_requires_model(monkeypatch):
    monkeypatch.setattr(settings, "ai_task_runtimes", {"security_scan": TaskRuntimeConfig(provider="claude")})
    with pytest.raises(ValueError, match="must specify a model"):
        deps.get_security_scan_runtime_client()


def test_provider_alias_inherits_claude_defaults(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "anthropic")
    monkeypatch.setattr(settings, "ai_task_runtimes", {"assistant": TaskRuntimeConfig(provider="claude_code")})
    runtime = deps.get_assistant_runtime_client()
    assert runtime.api_key == "global-test-key"
    assert runtime.model == "global-codex-model"  # Factory does not guess vendor model names.


def test_unknown_global_provider_is_not_silently_mocked(monkeypatch):
    monkeypatch.setattr(settings, "ai_provider", "codxe")
    with pytest.raises(ValueError, match="Unsupported AI provider"):
        deps.get_assistant_runtime_client()


def test_codex_profile_rejects_unsupported_endpoint(monkeypatch):
    monkeypatch.setattr(settings, "ai_task_runtimes", {"assistant": TaskRuntimeConfig(base_url="https://unsupported.invalid")})
    with pytest.raises(ValueError, match="only by the Claude adapter"):
        deps.get_assistant_runtime_client()


@pytest.mark.parametrize("profile", [
    {"security_sacn": {"provider": "claude"}},
    {"security_scan": {"provider": "cluade"}},
    {"security_scan": {"modle": "typo"}},
])
def test_bad_environment_profiles_are_rejected(monkeypatch, profile):
    monkeypatch.setenv("AI_TASK_RUNTIMES", json.dumps(profile))
    with pytest.raises(ValidationError):
        Settings()


def test_environment_profile_is_parsed_and_key_hidden(monkeypatch):
    monkeypatch.setenv("AI_TASK_RUNTIMES", json.dumps({"security_scan": {
        "provider": "claude", "model": "scan-model", "api_key": "profile-test-secret",
    }}))
    configured = Settings()
    assert configured.ai_task_runtimes["security_scan"].model == "scan-model"
    assert "profile-test-secret" not in repr(configured)
    assert "profile-test-secret" not in repr(configured.ai_task_runtimes["security_scan"])


@pytest.mark.anyio
async def test_codex_assistant_and_claude_restricted_task_can_create_sessions(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(settings, "ai_task_runtimes", {
        task: TaskRuntimeConfig(provider="claude", model="scan-model")
        for task in ["security_scan", "skill_read"]
    })
    chat = await deps.get_assistant_runtime_client().create_or_resume_session(
        runtime_session_id=None, working_directory=str(tmp_path), system_prompt="chat",
    )
    assert chat.provider == "codex"
    for getter in [deps.get_security_scan_runtime_client, deps.get_skill_read_runtime_client]:
        session = await getter().create_or_resume_session(
            runtime_session_id=None, system_prompt="restricted", tool_allowlist=("Read", "Glob", "Grep"),
            workspace_plan=RuntimeWorkspacePlan(
                user_id="test", session_id="test", organization_key="test", sandbox_cwd="/",
                host_shadow_root=str(tmp_path), mounts=[],
            ),
        )
        assert session.provider == "claude_code"
        assert session.tool_allowlist == ("Read", "Glob", "Grep")


def test_skill_import_route_uses_restricted_task_dependency():
    route = next(route for route in router.routes if route.path.endswith("/skills/import/generate"))
    calls = [dependency.call for dependency in route.dependant.dependencies]
    assert deps.get_skill_read_runtime_client in calls
    assert deps.get_assistant_runtime_client not in calls


@pytest.mark.parametrize("service_name,runtime_getter", [
    ("get_requirement_review_service", "get_requirement_review_runtime_client"),
    ("get_business_doc_update_service", "get_business_doc_update_runtime_client"),
])
def test_background_services_use_their_task_runtime(monkeypatch, tmp_path, service_name, runtime_getter):
    # Constructor capture verifies dependency wiring without touching real databases or integrations.
    sentinel = object()
    monkeypatch.setattr(deps, runtime_getter, lambda: sentinel)
    for getter in ["get_requirement_review_store", "get_business_doc_update_store", "get_clickup_comment_client"]:
        monkeypatch.setattr(deps, getter, lambda: None)
    constructor = "RequirementReviewService" if "requirement" in service_name else "BusinessDocUpdateService"
    monkeypatch.setattr(deps, constructor, lambda **kwargs: kwargs)
    service = getattr(deps, service_name)
    service.cache_clear()
    try:
        assert service()["runtime_client"] is sentinel
    finally:
        service.cache_clear()
