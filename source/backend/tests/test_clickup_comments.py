from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.business.requirement_reviews.service import RequirementReviewRecord, RequirementReviewService, RequirementReviewStore
from app.utils.clickup.comments import ClickUpCommentClient, markdown_to_clickup_comment_blocks


def test_markdown_to_clickup_comment_blocks_renders_common_markdown() -> None:
    blocks = markdown_to_clickup_comment_blocks(
        "# 技术评审\n\n"
        "- **风险**：高\n"
        "- 参考 [需求](https://app.clickup.com/t/86abc)\n"
        "1. 使用 `feature_flag`\n\n"
        "```python\n"
        "print('ok')\n"
        "```"
    )

    assert {"text": "技术评审", "attributes": {"bold": True}} in blocks
    assert {"text": "风险", "attributes": {"bold": True}} in blocks
    assert {"text": "\n", "attributes": {"list": {"list": "bullet"}}} in blocks
    assert {"text": "需求", "attributes": {"link": "https://app.clickup.com/t/86abc"}} in blocks
    assert {"text": "feature_flag", "attributes": {"code": True}} in blocks
    assert {"text": "\n", "attributes": {"list": {"list": "ordered"}}} in blocks
    assert {"text": "\n", "attributes": {"code-block": {"code-block": "python"}}} in blocks


def test_clickup_comment_client_posts_structured_comment(monkeypatch) -> None:
    captured = []

    class Response:
        status_code = 200
        text = "{}"

        @staticmethod
        def json() -> dict:
            return {"id": "comment-1"}

    def fake_post(url, headers, json, timeout):
        captured.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        return Response()

    monkeypatch.setattr("app.utils.clickup.comments.requests.post", fake_post)

    client = ClickUpCommentClient(api_token="token", base_url="https://api.example.test")
    response = client.create_task_comment(task_id="86abc", markdown="# 评审\n\n- 通过")

    assert response == {"id": "comment-1"}
    assert captured[0]["url"] == "https://api.example.test/task/86abc/comment"
    assert captured[0]["headers"]["Authorization"] == "token"
    assert captured[0]["json"]["notify_all"] is False
    assert captured[0]["json"]["comment"][0] == {"text": "评审", "attributes": {"bold": True}}


def test_clickup_comment_client_reads_and_writes_task_threads(monkeypatch) -> None:
    calls = []

    class Response:
        status_code = 200
        text = "{}"

        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def json(self) -> dict:
            return self.payload

    def fake_get(url, headers, timeout):
        calls.append({"method": "GET", "url": url, "headers": headers, "timeout": timeout})
        if url.endswith("/reply"):
            return Response({"comments": [{"id": "reply-1", "comment_text": "收到"}]})
        return Response({"comments": [{"id": "comment-1", "comment_text": "请补充"}]})

    def fake_post(url, headers, json, timeout):
        calls.append({"method": "POST", "url": url, "headers": headers, "json": json, "timeout": timeout})
        return Response({"id": "created-1"})

    monkeypatch.setattr("app.utils.clickup.comments.requests.get", fake_get)
    monkeypatch.setattr("app.utils.clickup.comments.requests.post", fake_post)

    client = ClickUpCommentClient(api_token="token", base_url="https://api.example.test")

    assert client.list_task_comments(task_id="86abc")[0]["id"] == "comment-1"
    assert client.list_threaded_comments(comment_id="comment-1")[0]["id"] == "reply-1"
    assert client.create_task_text_comment(task_id="86abc", content="新增评论") == {"id": "created-1"}
    assert client.create_threaded_comment(comment_id="comment-1", content="回复评论") == {"id": "created-1"}
    assert calls[0]["url"] == "https://api.example.test/task/86abc/comment"
    assert calls[1]["url"] == "https://api.example.test/comment/comment-1/reply"
    assert calls[2]["json"] == {"comment_text": "新增评论", "notify_all": False}
    assert calls[3]["json"] == {"comment_text": "回复评论", "notify_all": False}


def test_clickup_client_reads_and_updates_task_and_doc_markdown(monkeypatch) -> None:
    calls = []

    class Response:
        status_code = 200
        text = "{}"

        def __init__(self, payload: dict | None = None) -> None:
            self.payload = payload or {}

        def json(self) -> dict:
            return self.payload

    def fake_get(url, headers, params, timeout):
        calls.append({"method": "GET", "url": url, "headers": headers, "params": params, "timeout": timeout})
        if "/task/" in url:
            return Response({"name": "Web 下单优化", "markdown_description": "# 原 Task"})
        return Response({"name": "优惠券规则", "content": "# 原需求"})

    def fake_put(url, headers, json, timeout):
        calls.append({"method": "PUT", "url": url, "headers": headers, "json": json, "timeout": timeout})
        return Response()

    monkeypatch.setattr("app.utils.clickup.comments.requests.get", fake_get)
    monkeypatch.setattr("app.utils.clickup.comments.requests.put", fake_put)

    client = ClickUpCommentClient(
        api_token="token",
        base_url="https://api-v2.example.test",
        v3_base_url="https://api-v3.example.test",
    )

    assert client.get_task_markdown(task_id="86abc") == {"title": "Web 下单优化", "content": "# 原 Task"}
    client.update_task_markdown(task_id="86abc", content="# 新 Task")
    assert client.get_doc_page_markdown(
        workspace_id="9000000001",
        doc_id="doc-1",
        page_id="page-1",
    ) == {"title": "优惠券规则", "content": "# 原需求"}
    client.update_doc_page_markdown(
        workspace_id="9000000001",
        doc_id="doc-1",
        page_id="page-1",
        content="# 新需求",
    )

    assert calls[0]["params"] == {"include_markdown_description": "true"}
    assert calls[1]["json"] == {"markdown_content": "# 新 Task"}
    assert calls[2]["params"] == {"content_format": "text/md"}
    assert calls[3]["json"] == {
        "content": "# 新需求",
        "content_edit_mode": "replace",
        "content_format": "text/md",
    }


def test_clickup_client_reads_doc_creator_email(monkeypatch) -> None:
    calls = []

    class Response:
        status_code = 200
        text = "{}"

        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def json(self) -> dict:
            return self.payload

    def fake_get(url, headers, timeout):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        if url.endswith("/docs/doc-1"):
            return Response({"creator": 86})
        return Response(
            {
                "teams": [
                    {
                        "id": "9000000001",
                        "members": [{"user": {"id": 86, "email": "Creator@corp.test"}}],
                    }
                ]
            }
        )

    monkeypatch.setattr("app.utils.clickup.comments.requests.get", fake_get)
    client = ClickUpCommentClient(
        api_token="token",
        base_url="https://api-v2.example.test",
        v3_base_url="https://api-v3.example.test",
    )

    assert client.get_doc_creator_email(workspace_id="9000000001", doc_id="doc-1") == "creator@corp.test"
    assert calls[0]["url"] == "https://api-v3.example.test/workspaces/9000000001/docs/doc-1"
    assert calls[1]["url"] == "https://api-v2.example.test/team"


def test_clickup_client_reads_task_creator_email(monkeypatch) -> None:
    class Response:
        status_code = 200
        text = "{}"

        @staticmethod
        def json() -> dict:
            return {"creator": {"email": "Creator@corp.test"}}

    def fake_get(url, headers, timeout):
        assert url == "https://api-v2.example.test/task/86abc"
        assert headers["Authorization"] == "token"
        assert timeout == 30
        return Response()

    monkeypatch.setattr("app.utils.clickup.comments.requests.get", fake_get)
    client = ClickUpCommentClient(api_token="token", base_url="https://api-v2.example.test")

    assert client.get_task_creator_email(task_id="86abc") == "creator@corp.test"


def test_clickup_comment_client_creates_doc_in_folder_and_page(monkeypatch) -> None:
    captured = []

    class Response:
        status_code = 201
        text = "{}"

        def __init__(self, payload: dict) -> None:
            self.payload = payload

        def json(self) -> dict:
            return self.payload

    def fake_post(url, headers, json, timeout):
        captured.append({"url": url, "headers": headers, "json": json, "timeout": timeout})
        if url.endswith("/docs"):
            return Response({"id": "doc-review"})
        return Response({"id": "page-review"})

    monkeypatch.setattr("app.utils.clickup.comments.requests.post", fake_post)

    client = ClickUpCommentClient(
        api_token="token",
        base_url="https://api-v2.example.test",
        v3_base_url="https://api-v3.example.test",
    )
    doc = client.create_review_doc_in_folder(
        workspace_id="9000000001",
        folder_id="90100000001",
        name="技术评审",
    )
    page = client.create_doc_page(
        workspace_id="9000000001",
        doc_id="doc-review",
        name="技术评审",
        content="# 技术评审",
    )

    assert doc == {"id": "doc-review"}
    assert page == {"id": "page-review"}
    assert captured[0]["url"] == "https://api-v3.example.test/workspaces/9000000001/docs"
    assert captured[0]["json"]["parent"] == {"id": "90100000001", "type": 5}
    assert captured[0]["json"]["create_page"] is False
    assert captured[1]["url"] == "https://api-v3.example.test/workspaces/9000000001/docs/doc-review/pages"
    assert captured[1]["json"]["content_format"] == "text/md"


@pytest.mark.anyio
async def test_requirement_review_clickup_publish_job_creates_doc_and_comments_with_link(tmp_path: Path) -> None:
    base_dir = tmp_path
    requirements_root = base_dir / "workspace/knowledge/requirements"
    task_file = requirements_root / "tasks/实现中/Web 下单优化.md"
    task_file.parent.mkdir(parents=True)
    task_file.write_text(
        "# Web 下单优化\n\n"
        "## 任务元信息\n\n"
        "- Task ID: `86abc`\n"
        "- 标题: Web 下单优化\n"
        "- 链接: https://app.clickup.com/t/86abc\n",
        encoding="utf-8",
    )
    skills_root = base_dir / "workspace/.claude/skills"
    skills_root.mkdir(parents=True)

    class FakeClickUpPublisher:
        def __init__(self) -> None:
            self.calls = []

        def create_review_doc_in_folder(self, *, workspace_id: str, folder_id: str, name: str, visibility: str) -> dict:
            self.calls.append({"fn": "create_review_doc_in_folder", "workspace_id": workspace_id, "folder_id": folder_id, "name": name, "visibility": visibility})
            return {"id": "doc-review"}

        def list_docs_in_folder(self, *, workspace_id: str, folder_id: str) -> list[dict]:
            self.calls.append({"fn": "list_docs_in_folder", "workspace_id": workspace_id, "folder_id": folder_id})
            return []

        def list_doc_pages(self, *, doc_id: str) -> list[dict]:
            self.calls.append({"fn": "list_doc_pages", "doc_id": doc_id})
            return []

        def create_doc_page(self, *, workspace_id: str, doc_id: str, name: str, content: str, parent_page_id=None, sub_title: str = "", content_format: str = "text/md") -> dict:
            self.calls.append({"fn": "create_doc_page", "workspace_id": workspace_id, "doc_id": doc_id, "name": name, "content": content, "parent_page_id": parent_page_id, "sub_title": sub_title, "content_format": content_format})
            if parent_page_id is None:
                return {"id": "month-page"}
            return {"id": "page-review"}

        def create_task_comment(self, *, task_id: str, markdown: str, notify_all: bool) -> dict:
            self.calls.append({"fn": "create_task_comment", "task_id": task_id, "markdown": markdown, "notify_all": notify_all})
            return {"id": "comment-1"}

    clickup_client = FakeClickUpPublisher()
    store = RequirementReviewStore(str(tmp_path / "assistant.sqlite3"))
    service = RequirementReviewService(
        base_dir=base_dir,
        requirements_root=requirements_root,
        skills_root=skills_root,
        store=store,
        runtime_client=object(),
        clickup_comment_client=clickup_client,
        clickup_review_publish_enabled=True,
        clickup_workspace_id="9000000001",
        clickup_review_doc_folder_id="90100000001",
        frontend_app_url="https://agent.corp.test/",
    )
    review_path = base_dir / "workspace/knowledge/__reviews__/tech-review/2026/05/review_20260516_103200_ab12cd.md"
    review_path.parent.mkdir(parents=True)
    review_path.write_text(
        "---\n"
        "review_id: review_20260516_103200_ab12cd\n"
        "review_type: tech_review\n"
        "source_path: workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md\n"
        "source_paths: workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md\n"
        "source_hash: sha256:abc\n"
        "status: completed\n"
        "risk_level: high\n"
        "created_at: 2026-05-16T10:32:00+00:00\n"
        "completed_at: 2026-05-16T10:32:00+00:00\n"
        "---\n\n"
        "# 技术评审报告\n\n"
        "## 关联来源\n\n"
        "- [Task: Web 下单优化.md](<workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md>)\n\n"
        "风险等级：高\n"
        "结论：需要补充异常回滚规则。\n",
        encoding="utf-8",
    )
    review = RequirementReviewRecord(
        review_id="review_20260516_103200_ab12cd",
        review_type="tech_review",
        path="workspace/knowledge/__reviews__/tech-review/2026/05/review_20260516_103200_ab12cd.md",
        source_path="workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md",
        source_paths=("workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md",),
        source_hash="sha256:abc",
        status="completed",
        risk_level="high",
        created_at="2026-05-16T10:32:00+00:00",
        completed_at="2026-05-16T10:32:00+00:00",
    )
    job = store.create_clickup_publish_job(
        review=review,
        task_id="86abc",
        workspace_id="9000000001",
        folder_id="90100000001",
    )

    processed = await service.run_clickup_publish_job(job)

    assert processed is not None
    assert [call["fn"] for call in clickup_client.calls] == [
        "list_docs_in_folder",
        "create_review_doc_in_folder",
        "list_doc_pages",
        "create_doc_page",
        "create_doc_page",
        "create_task_comment",
    ]
    assert clickup_client.calls[1]["name"] == "需求评审归档"
    assert clickup_client.calls[3]["name"] == "2026-05"
    assert clickup_client.calls[4]["name"] == "review_Web 下单优化_20260516_103200"
    assert clickup_client.calls[4]["parent_page_id"] == "month-page"
    assert clickup_client.calls[4]["sub_title"] == ""
    doc_content = clickup_client.calls[4]["content"]
    assert "# AI自动化需求评审结果" not in doc_content
    assert "## 元信息" in doc_content
    assert "- Review ID: [review_20260516_103200_ab12cd](https://agent.corp.test/#/knowledge?type=requirements&nodeId=workspace%2Fknowledge%2Frequirements%2Ftasks%2F" in doc_content
    assert "reviewId=review_20260516_103200_ab12cd)" in doc_content
    assert "- 风险等级: 高" in doc_content
    assert "- 来源需求: [Web 下单优化](https://app.clickup.com/t/86abc)" in doc_content
    assert "本地评审文件" not in doc_content
    assert "内容 Hash" not in doc_content
    assert "## 关联来源" not in doc_content
    assert "| A | B |" not in clickup_client.calls[5]["markdown"]
    comment_markdown = clickup_client.calls[5]["markdown"]
    assert "# AI自动化需求评审已完成" in comment_markdown
    assert "- Review ID: [review_20260516_103200_ab12cd](https://agent.corp.test/#/knowledge?type=requirements&nodeId=workspace%2Fknowledge%2Frequirements%2Ftasks%2F" in comment_markdown
    assert "- 风险等级: 高" in comment_markdown
    assert "[查看完整AI自动化需求评审](https://app.clickup.com/9000000001/docs/doc-review/page-review)" in comment_markdown
    assert "完整评审内容已发布到共享 ClickUp 文件夹" not in comment_markdown
    assert "评审意见：需要补充异常回滚规则。" in comment_markdown
    assert store.list_pending_clickup_publish_jobs() == []
    with store._connect() as connection:
        row = connection.execute("SELECT status, doc_id, page_id, doc_url FROM requirement_review_clickup_publish_jobs").fetchone()
    assert row["status"] == "completed"
    assert row["doc_id"] == "doc-review"
    assert row["page_id"] == "page-review"


@pytest.mark.anyio
async def test_requirement_review_clickup_publish_job_reuses_archive_doc_and_month_page(tmp_path: Path) -> None:
    base_dir = tmp_path
    requirements_root = base_dir / "workspace/knowledge/requirements"
    task_file = requirements_root / "tasks/实现中/Web 下单优化.md"
    task_file.parent.mkdir(parents=True)
    task_file.write_text("- Task ID: `86abc`\n", encoding="utf-8")
    review_path = base_dir / "workspace/knowledge/__reviews__/tech-review/2026/05/review_20260516_103200_ab12cd.md"
    review_path.parent.mkdir(parents=True)
    review_path.write_text(
        "---\n"
        "review_id: review_20260516_103200_ab12cd\n"
        "review_type: tech_review\n"
        "source_path: workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md\n"
        "source_paths: workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md\n"
        "source_hash: sha256:abc\n"
        "status: completed\n"
        "risk_level: high\n"
        "created_at: 2026-05-16T10:32:00+00:00\n"
        "---\n\n"
        "# 技术评审报告\n",
        encoding="utf-8",
    )

    class FakeClickUpPublisher:
        def __init__(self) -> None:
            self.calls = []

        def list_docs_in_folder(self, *, workspace_id: str, folder_id: str) -> list[dict]:
            self.calls.append({"fn": "list_docs_in_folder", "workspace_id": workspace_id, "folder_id": folder_id})
            return [{"id": "doc-existing", "name": "需求评审归档"}]

        def list_doc_pages(self, *, doc_id: str) -> list[dict]:
            self.calls.append({"fn": "list_doc_pages", "doc_id": doc_id})
            return [{"id": "month-existing", "name": "2026-05", "pages": []}]

        def create_review_doc_in_folder(self, **kwargs) -> dict:
            self.calls.append({"fn": "create_review_doc_in_folder", **kwargs})
            return {"id": "should-not-create"}

        def create_doc_page(self, *, workspace_id: str, doc_id: str, name: str, content: str, parent_page_id=None, sub_title: str = "", content_format: str = "text/md") -> dict:
            self.calls.append({"fn": "create_doc_page", "workspace_id": workspace_id, "doc_id": doc_id, "name": name, "parent_page_id": parent_page_id})
            return {"id": "review-page"}

        def create_task_comment(self, *, task_id: str, markdown: str, notify_all: bool) -> dict:
            self.calls.append({"fn": "create_task_comment", "task_id": task_id, "markdown": markdown, "notify_all": notify_all})
            return {"id": "comment-1"}

    clickup_client = FakeClickUpPublisher()
    store = RequirementReviewStore(str(tmp_path / "assistant.sqlite3"))
    service = RequirementReviewService(
        base_dir=base_dir,
        requirements_root=requirements_root,
        skills_root=base_dir / "workspace/.claude/skills",
        store=store,
        runtime_client=object(),
        clickup_comment_client=clickup_client,
    )
    review = RequirementReviewRecord(
        review_id="review_20260516_103200_ab12cd",
        review_type="tech_review",
        path="workspace/knowledge/__reviews__/tech-review/2026/05/review_20260516_103200_ab12cd.md",
        source_path="workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md",
        source_paths=("workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md",),
        source_hash="sha256:abc",
        status="completed",
        risk_level="high",
        created_at="2026-05-16T10:32:00+00:00",
        completed_at=None,
    )
    job = store.create_clickup_publish_job(review=review, task_id="86abc", workspace_id="9000000001", folder_id="90100000001")

    await service.run_clickup_publish_job(job)

    assert [call["fn"] for call in clickup_client.calls] == [
        "list_docs_in_folder",
        "list_doc_pages",
        "create_doc_page",
        "create_task_comment",
    ]
    assert clickup_client.calls[2]["doc_id"] == "doc-existing"
    assert clickup_client.calls[2]["parent_page_id"] == "month-existing"


@pytest.mark.anyio
async def test_requirement_review_schedules_clickup_publish_without_blocking_review_completion(tmp_path: Path) -> None:
    base_dir = tmp_path
    requirements_root = base_dir / "workspace/knowledge/requirements"
    task_file = requirements_root / "tasks/实现中/Web 下单优化.md"
    task_file.parent.mkdir(parents=True)
    task_file.write_text("- Task ID: `86abc`\n", encoding="utf-8")
    review_path = base_dir / "workspace/knowledge/__reviews__/tech-review/2026/05/review_20260516_103200_ab12cd.md"
    review_path.parent.mkdir(parents=True)
    review_path.write_text(
        "---\n"
        "review_id: review_20260516_103200_ab12cd\n"
        "review_type: tech_review\n"
        "source_path: workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md\n"
        "source_paths: workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md\n"
        "source_hash: sha256:abc\n"
        "status: completed\n"
        "risk_level: high\n"
        "---\n\n"
        "# 技术评审报告\n\n"
        "风险等级：高\n",
        encoding="utf-8",
    )

    class FailingClickUpPublisher:
        def list_docs_in_folder(self, *, workspace_id: str, folder_id: str) -> list[dict]:
            del workspace_id, folder_id
            return []

        def create_review_doc_in_folder(self, *, workspace_id: str, folder_id: str, name: str, visibility: str) -> dict:
            del workspace_id, folder_id, name, visibility
            raise RuntimeError("ClickUp 暂时不可用")

        def create_task_comment(self, *, task_id: str, markdown: str, notify_all: bool) -> dict:
            del task_id, markdown, notify_all
            return {}

    store = RequirementReviewStore(str(tmp_path / "assistant.sqlite3"))
    service = RequirementReviewService(
        base_dir=base_dir,
        requirements_root=requirements_root,
        skills_root=base_dir / "workspace/.claude/skills",
        store=store,
        runtime_client=object(),
        clickup_comment_client=FailingClickUpPublisher(),
        clickup_review_publish_enabled=True,
        clickup_workspace_id="9000000001",
        clickup_review_doc_folder_id="90100000001",
    )
    review = RequirementReviewRecord(
        review_id="review_20260516_103200_ab12cd",
        review_type="tech_review",
        path="workspace/knowledge/__reviews__/tech-review/2026/05/review_20260516_103200_ab12cd.md",
        source_path="workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md",
        source_paths=("workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md",),
        source_hash="sha256:abc",
        status="completed",
        risk_level="high",
        created_at="2026-05-16T10:32:00+00:00",
        completed_at="2026-05-16T10:32:00+00:00",
    )

    service._schedule_clickup_comment_job(review)
    await asyncio.sleep(0.05)

    with store._connect() as connection:
        row = connection.execute("SELECT status, error_message FROM requirement_review_clickup_publish_jobs").fetchone()
    assert row["status"] == "failed"
    assert "ClickUp 暂时不可用" in row["error_message"]


def test_requirement_review_publish_is_disabled_by_default(tmp_path: Path) -> None:
    base_dir = tmp_path
    requirements_root = base_dir / "workspace/knowledge/requirements"
    task_file = requirements_root / "tasks/实现中/Web 下单优化.md"
    task_file.parent.mkdir(parents=True)
    task_file.write_text("- Task ID: `86abc`\n", encoding="utf-8")

    class FakeClickUpPublisher:
        pass

    store = RequirementReviewStore(str(tmp_path / "assistant.sqlite3"))
    service = RequirementReviewService(
        base_dir=base_dir,
        requirements_root=requirements_root,
        skills_root=base_dir / "workspace/.claude/skills",
        store=store,
        runtime_client=object(),
        clickup_comment_client=FakeClickUpPublisher(),
        clickup_workspace_id="9000000001",
        clickup_review_doc_folder_id="90100000001",
    )
    review = RequirementReviewRecord(
        review_id="review_20260516_103200_ab12cd",
        review_type="tech_review",
        path="workspace/knowledge/__reviews__/tech-review/2026/05/review_20260516_103200_ab12cd.md",
        source_path="workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md",
        source_paths=("workspace/knowledge/requirements/tasks/实现中/Web 下单优化.md",),
        source_hash="sha256:abc",
        status="completed",
        risk_level="high",
        created_at="2026-05-16T10:32:00+00:00",
        completed_at="2026-05-16T10:32:00+00:00",
    )

    service._schedule_clickup_comment_job(review)

    assert store.list_pending_clickup_publish_jobs() == []
