from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# 登录相关测试从接口响应里读取验证码：在加载 app 之前改用调试发信并返回验证码，不依赖真实 SMTP。
os.environ.setdefault("EMAIL_AUTH_ENABLED", "true")
os.environ.setdefault("EMAIL_DELIVERY_MODE", "debug")
os.environ.setdefault("EMAIL_VERIFICATION_DEBUG_RESPONSE", "true")
# 助手对话会读取受控代码仓库配置；测试默认用一份空配置，需要仓库的用例自行覆盖。
os.environ.setdefault("CODE_REVIEW_PROJECT_CONFIG_PATH", str(BACKEND_ROOT / "tests/fixtures/code_review_projects.json"))

from app.core.dependencies import get_oa_employee_directory
from app.integrations.vinotech_oa import OaEmployeeDirectory
from app.main import app


def _isolated_oa_directory() -> OaEmployeeDirectory:
    return OaEmployeeDirectory(client=None, cache_ttl_seconds=300)


def reset_dependency_overrides() -> None:
    """清空测试残留的 overrides，并恢复 OA 目录基线隔离（测试不访问真实 OA）。"""
    app.dependency_overrides.clear()
    app.dependency_overrides[get_oa_employee_directory] = _isolated_oa_directory


# 测试隔离：OA 目录不访问真实环境，loaded=False，离职判定不生效
app.dependency_overrides[get_oa_employee_directory] = _isolated_oa_directory

# 公开快照只包含 source/；仓库根的 deployment/ 与 .claude/skills 不在其中，依赖它们的用例在缺失时跳过。
REPO_ROOT = BACKEND_ROOT.parents[1]
_DEPLOYMENT_TEST_FILES = {"test_deployment_migrate.py", "test_deployment_scripts.py"}
_DEPLOYMENT_TESTS = {
    "test_deploy_webhook_starts_deploy_for_valid_push",
    "test_deploy_webhook_rejects_invalid_signature",
    "test_deploy_webhook_ignores_other_branch",
}
_PROJECT_SKILL_TESTS = {
    "test_assistant_stream_chat_returns_sse_events",
    "test_assistant_skills_mounts_and_trace_endpoints",
    "test_assistant_skills_endpoint_lists_project_skills",
}


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    missing_deployment = not (REPO_ROOT / "deployment").is_dir()
    missing_project_skills = not (REPO_ROOT / ".claude" / "skills").is_dir()
    for item in items:
        if missing_deployment and (item.path.name in _DEPLOYMENT_TEST_FILES or item.name in _DEPLOYMENT_TESTS):
            item.add_marker(pytest.mark.skip(reason="deployment/ is not part of the public snapshot"))
        elif missing_project_skills and item.name in _PROJECT_SKILL_TESTS:
            item.add_marker(pytest.mark.skip(reason=".claude/skills is not part of the public snapshot"))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def client() -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as async_client:
        yield async_client
