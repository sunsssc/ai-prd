from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[3]


def _load_module():
    backend_root = REPO_ROOT / "source/backend"
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))
    module_path = backend_root / "app/utils/clickup/export_task.py"
    spec = importlib.util.spec_from_file_location("clickup_export_task", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_collect_doc_links_reads_markdown_description_and_structured_comment_preview() -> None:
    module = _load_module()

    links = module._collect_doc_links(
        {
            "markdown_description": (
                "详细文档参考：[合约底层机制优化PRD]"
                "(https://app.clickup.com/9000000001/docs/8cabcde-202438/8cabcde-384698)"
            ),
            "comments": [
                {
                    "comment": [
                        {
                            "type": "link_preview",
                            "link_preview": {
                                "url": "https://app.clickup.com/9000000001/docs/8cabcde-12558/8cabcde-267098"
                            },
                            "text": "Document preview 8cabcde-267098",
                        }
                    ],
                    "comment_text": "Document preview 8cabcde-267098旧需求文档\n",
                }
            ],
        }
    )

    assert [link["page_id"] for link in links] == ["8cabcde-384698", "8cabcde-267098"]


def test_relative_file_reference_is_url_encoded(tmp_path: Path) -> None:
    module = _load_module()

    target = tmp_path / "linked_docs" / "关键 文档" / "首页.md"

    assert (
        module._build_relative_file_reference(tmp_path, target)
        == "linked_docs/%E5%85%B3%E9%94%AE%20%E6%96%87%E6%A1%A3/%E9%A6%96%E9%A1%B5.md"
    )


def test_default_requirements_paths_point_to_workspace_knowledge() -> None:
    module = _load_module()

    assert module.DEFAULT_REQUIREMENTS_ROOT == REPO_ROOT / "workspace/knowledge/requirements"
    assert module.DEFAULT_REQUIREMENTS_TASKS_DIR == REPO_ROOT / "workspace/knowledge/requirements/tasks"
    assert module.DEFAULT_REQUIREMENTS_DOCS_DIR == REPO_ROOT / "workspace/knowledge/requirements/docs"


def test_export_task_defaults_to_requirements_tasks_dir(monkeypatch, tmp_path: Path) -> None:
    module = _load_module()
    tasks_dir = tmp_path / "workspace" / "knowledge" / "requirements" / "tasks"

    monkeypatch.setattr(module, "DEFAULT_REQUIREMENTS_TASKS_DIR", tasks_dir)
    monkeypatch.setattr(module, "_load_env", lambda: None)
    monkeypatch.setattr(
        module,
        "fetch_task",
        lambda task_id: {
            "id": task_id,
            "name": "默认目录任务",
            "status": "open",
            "custom_fields": [],
            "comments": [],
        },
    )

    output_path, _, _ = module.export_task("86abc")

    assert output_path == tasks_dir / "默认目录任务.md"
    assert output_path.exists()


def test_normalize_task_formats_sections_and_rewrites_linked_doc_links(monkeypatch, tmp_path: Path) -> None:
    module = _load_module()
    doc_url = "https://app.clickup.com/9000000001/docs/8cabcde-202438/8cabcde-384698"

    def fake_fetch_page_subtree_from_v2(doc_id: str, page_id: str) -> dict:
        return {"id": page_id, "name": "关键 文档", "pages": []}

    def fake_fetch_doc_page(workspace_id: str, doc_id: str, page_id: str) -> dict:
        return {"content": "文档正文"}

    monkeypatch.setattr(module, "fetch_page_subtree_from_v2", fake_fetch_page_subtree_from_v2)
    monkeypatch.setattr(module, "fetch_doc_page", fake_fetch_doc_page)

    task = module.normalize_task(
        "86abc",
        {
            "name": "测试任务",
            "markdown_description": f"需求背景\n\n参考文档：{doc_url}",
            "comments": [
                {
                    "user": {"username": "张三"},
                    "date": "1710000000000",
                    "comment_text": f"评论里的文档：[旧链接]({doc_url})",
                }
            ],
            "custom_fields": [
                {"name": "产品", "value": "R"},
                {"name": "需求分类", "value": "opt-1", "type_config": {"options": [{"id": "opt-1", "name": "合约"}]}},
                {"name": "抄送人", "value": ""},
            ],
        },
        linked_docs_dir=tmp_path / "linked_docs",
        markdown_dir=tmp_path,
    )
    markdown = module.render_markdown(task)

    assert "clickup.com" not in task["description"]
    assert "clickup.com" not in task["comments"]
    assert task["description"] == "需求背景"
    assert "参考文档" not in task["description"]
    assert "| 产品 | R |" in task["custom_fields"]
    assert "| 需求分类 | 合约 |" in task["custom_fields"]
    assert "抄送人" not in task["custom_fields"]
    assert "> **张三** · 2024-03-09 16:00:00 UTC" in task["comments"]
    assert "- [关键 文档](linked_docs/" in task["docs_content"]
    assert "ClickUp Doc ID" not in task["docs_content"]
    assert "ClickUp Page ID" not in task["docs_content"]
    assert "已导出页面" not in task["docs_content"]
    assert "## 评论" in markdown
    assert "## 关联文档" in markdown
    assert markdown.startswith("# 测试任务\n\n")
    assert "ClickUp 需求上下文" not in markdown
    assert "## 验收标准" not in markdown
    assert "未提供单独的验收标准字段" not in markdown
    assert markdown.index("## 核心需求描述") < markdown.index("## 关联文档") < markdown.index("## 自定义字段")
    assert "- Task ID: `86abc`" in markdown
    assert "| Task ID |" not in markdown
    assert "| 信息 | 内容 |" not in markdown
