from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

import requests


class ClickUpCommentClient:
    folder_parent_type = 5

    def __init__(
        self,
        *,
        api_token: str,
        base_url: str = "https://api.clickup.com/api/v2",
        v3_base_url: str = "https://api.clickup.com/api/v3",
    ) -> None:
        if not api_token:
            raise ValueError("缺少 CLICKUP_API_TOKEN，无法回填 ClickUp 评论。")
        self.api_token = api_token
        self.base_url = base_url.rstrip("/")
        self.v3_base_url = v3_base_url.rstrip("/")

    def create_task_comment(self, *, task_id: str, markdown: str, notify_all: bool = False) -> dict[str, Any]:
        payload = {
            "comment": markdown_to_clickup_comment_blocks(markdown),
            "notify_all": notify_all,
        }
        response = requests.post(
            f"{self.base_url}/task/{task_id}/comment",
            headers={
                "Authorization": self.api_token,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ClickUp 评论回填失败: HTTP {response.status_code} {response.text[:500]}")
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def get_task_markdown(self, *, task_id: str) -> dict[str, str]:
        response = requests.get(
            f"{self.base_url}/task/{task_id}",
            headers={"Authorization": self.api_token},
            params={"include_markdown_description": "true"},
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ClickUp Task 读取失败: HTTP {response.status_code} {response.text[:500]}")
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("ClickUp Task 读取失败: 返回格式错误")
        content = data.get("markdown_description")
        if content is None:
            content = data.get("description") or ""
        return {
            "title": str(data.get("name") or ""),
            "content": str(content),
        }

    def get_task_creator_email(self, *, task_id: str) -> str:
        response = requests.get(
            f"{self.base_url}/task/{task_id}",
            headers={"Authorization": self.api_token},
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ClickUp Task 创建人读取失败: HTTP {response.status_code}")
        data = response.json()
        creator = data.get("creator") if isinstance(data, dict) else None
        email = str(creator.get("email") or "").strip().lower() if isinstance(creator, dict) else ""
        if not email:
            raise RuntimeError("ClickUp Task 创建人未关联邮箱")
        return email

    def update_task_markdown(self, *, task_id: str, content: str) -> None:
        payload = {"markdown_content": content} if content else {"description": " "}
        response = requests.put(
            f"{self.base_url}/task/{task_id}",
            headers={
                "Authorization": self.api_token,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ClickUp Task 保存失败: HTTP {response.status_code} {response.text[:500]}")

    def get_doc_page_markdown(self, *, workspace_id: str, doc_id: str, page_id: str) -> dict[str, str]:
        response = requests.get(
            f"{self.v3_base_url}/workspaces/{workspace_id}/docs/{doc_id}/pages/{page_id}",
            headers={"Authorization": self.api_token},
            params={"content_format": "text/md"},
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ClickUp 需求文档读取失败: HTTP {response.status_code} {response.text[:500]}")
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError("ClickUp 需求文档读取失败: 返回格式错误")
        return {
            "title": str(data.get("name") or data.get("title") or ""),
            "content": str(data.get("content") or ""),
        }

    def get_doc_creator_email(self, *, workspace_id: str, doc_id: str) -> str:
        doc_response = requests.get(
            f"{self.v3_base_url}/workspaces/{workspace_id}/docs/{doc_id}",
            headers={"Authorization": self.api_token},
            timeout=30,
        )
        if doc_response.status_code >= 400:
            raise RuntimeError(f"ClickUp 需求文档创建人读取失败: HTTP {doc_response.status_code}")
        doc = doc_response.json()
        if not isinstance(doc, dict):
            raise RuntimeError("ClickUp 需求文档创建人读取失败: 返回格式错误")
        creator_id = str(doc.get("creator") or "").strip()
        if not creator_id:
            raise RuntimeError("ClickUp 需求文档缺少创建人信息")

        teams_response = requests.get(
            f"{self.base_url}/team",
            headers={"Authorization": self.api_token},
            timeout=30,
        )
        if teams_response.status_code >= 400:
            raise RuntimeError(f"ClickUp 工作区成员读取失败: HTTP {teams_response.status_code}")
        teams_data = teams_response.json()
        if not isinstance(teams_data, dict):
            raise RuntimeError("ClickUp 工作区成员读取失败: 返回格式错误")
        team = next(
            (
                item
                for item in teams_data.get("teams", [])
                if isinstance(item, dict) and str(item.get("id") or "") == workspace_id
            ),
            None,
        )
        if not isinstance(team, dict):
            raise RuntimeError("ClickUp 工作区成员读取失败: 未找到工作区")
        for member in team.get("members", []):
            user = member.get("user") if isinstance(member, dict) else None
            if not isinstance(user, dict) or str(user.get("id") or "") != creator_id:
                continue
            email = str(user.get("email") or "").strip().lower()
            if email:
                return email
            break
        raise RuntimeError("ClickUp 需求文档创建人未关联邮箱")

    def update_doc_page_markdown(
        self,
        *,
        workspace_id: str,
        doc_id: str,
        page_id: str,
        content: str,
    ) -> None:
        response = requests.put(
            f"{self.v3_base_url}/workspaces/{workspace_id}/docs/{doc_id}/pages/{page_id}",
            headers={
                "Authorization": self.api_token,
                "Content-Type": "application/json",
            },
            json={
                "content": content,
                "content_edit_mode": "replace",
                "content_format": "text/md",
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ClickUp 需求文档保存失败: HTTP {response.status_code} {response.text[:500]}")

    def list_task_comments(self, *, task_id: str) -> list[dict[str, Any]]:
        response = requests.get(
            f"{self.base_url}/task/{task_id}/comment",
            headers={"Authorization": self.api_token},
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ClickUp 评论查询失败: HTTP {response.status_code} {response.text[:500]}")
        data = response.json()
        comments = data.get("comments") if isinstance(data, dict) else None
        return comments if isinstance(comments, list) else []

    def list_threaded_comments(self, *, comment_id: str) -> list[dict[str, Any]]:
        response = requests.get(
            f"{self.base_url}/comment/{comment_id}/reply",
            headers={"Authorization": self.api_token},
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ClickUp 评论回复查询失败: HTTP {response.status_code} {response.text[:500]}")
        data = response.json()
        comments = data.get("comments") if isinstance(data, dict) else None
        return comments if isinstance(comments, list) else []

    def create_task_text_comment(
        self,
        *,
        task_id: str,
        content: str,
        notify_all: bool = False,
    ) -> dict[str, Any]:
        return self._create_text_comment(
            url=f"{self.base_url}/task/{task_id}/comment",
            content=content,
            notify_all=notify_all,
            error_label="ClickUp 评论添加失败",
        )

    def create_threaded_comment(
        self,
        *,
        comment_id: str,
        content: str,
        notify_all: bool = False,
    ) -> dict[str, Any]:
        return self._create_text_comment(
            url=f"{self.base_url}/comment/{comment_id}/reply",
            content=content,
            notify_all=notify_all,
            error_label="ClickUp 评论回复失败",
        )

    def _create_text_comment(
        self,
        *,
        url: str,
        content: str,
        notify_all: bool,
        error_label: str,
    ) -> dict[str, Any]:
        response = requests.post(
            url,
            headers={
                "Authorization": self.api_token,
                "Content-Type": "application/json",
            },
            json={"comment_text": content, "notify_all": notify_all},
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"{error_label}: HTTP {response.status_code} {response.text[:500]}")
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def create_review_doc_in_folder(
        self,
        *,
        workspace_id: str,
        folder_id: str,
        name: str,
        visibility: str = "PUBLIC",
    ) -> dict[str, Any]:
        response = requests.post(
            f"{self.v3_base_url}/workspaces/{workspace_id}/docs",
            headers={
                "Authorization": self.api_token,
                "Content-Type": "application/json",
            },
            json={
                "name": name,
                "parent": {
                    "id": folder_id,
                    "type": self.folder_parent_type,
                },
                "visibility": visibility,
                "create_page": False,
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ClickUp 评审文档创建失败: HTTP {response.status_code} {response.text[:500]}")
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def list_docs_in_folder(self, *, workspace_id: str, folder_id: str) -> list[dict[str, Any]]:
        response = requests.get(
            f"{self.v3_base_url}/workspaces/{workspace_id}/docs",
            headers={
                "Authorization": self.api_token,
                "Content-Type": "application/json",
            },
            params={
                "parent_id": folder_id,
                "parent_type": self.folder_parent_type,
                "limit": 100,
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ClickUp 评审文档查询失败: HTTP {response.status_code} {response.text[:500]}")
        data = response.json()
        docs = data.get("docs") if isinstance(data, dict) else None
        return docs if isinstance(docs, list) else []

    def list_doc_pages(self, *, doc_id: str) -> list[dict[str, Any]]:
        response = requests.get(
            f"{self.base_url}/doc/{doc_id}/page",
            headers={
                "Authorization": self.api_token,
                "Content-Type": "application/json",
            },
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ClickUp 评审文档页面查询失败: HTTP {response.status_code} {response.text[:500]}")
        data = response.json()
        pages = data.get("pages") if isinstance(data, dict) else None
        return pages if isinstance(pages, list) else []

    def create_doc_page(
        self,
        *,
        workspace_id: str,
        doc_id: str,
        name: str,
        content: str,
        parent_page_id: str | None = None,
        sub_title: str = "",
        content_format: str = "text/md",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": name,
            "sub_title": sub_title,
            "content": content,
            "content_format": content_format,
        }
        if parent_page_id:
            payload["parent_page_id"] = parent_page_id
        response = requests.post(
            f"{self.v3_base_url}/workspaces/{workspace_id}/docs/{doc_id}/pages",
            headers={
                "Authorization": self.api_token,
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=30,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"ClickUp 评审文档页面创建失败: HTTP {response.status_code} {response.text[:500]}")
        try:
            data = response.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}


def markdown_to_clickup_comment_blocks(markdown: str) -> list[dict[str, Any]]:
    """把常用 Markdown 转为 ClickUp 评论 rich text blocks。

    ClickUp 评论 API 不直接把 comment_text 当 Markdown 渲染；结构化 comment blocks
    才能稳定表达加粗、链接、列表和代码块。
    """
    text = markdown.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return [{"text": "", "attributes": {}}]

    blocks: list[dict[str, Any]] = []
    in_code_block = False
    code_language = "plain"
    code_lines: list[str] = []

    for line in text.split("\n"):
        fence_match = re.match(r"^```([A-Za-z0-9_-]+)?\s*$", line.strip())
        if fence_match:
            if in_code_block:
                _append_code_block(blocks, "\n".join(code_lines), code_language)
                in_code_block = False
                code_language = "plain"
                code_lines = []
            else:
                in_code_block = True
                code_language = fence_match.group(1) or "plain"
            continue

        if in_code_block:
            code_lines.append(line)
            continue

        if not line.strip():
            blocks.append({"text": "\n", "attributes": {}})
            continue

        checklist_match = re.match(r"^\s*[-*+]\s+\[([ xX])\]\s+(.+)$", line)
        if checklist_match:
            list_type = "checked" if checklist_match.group(1).lower() == "x" else "unchecked"
            _append_inline(blocks, checklist_match.group(2))
            blocks.append({"text": "\n", "attributes": {"list": {"list": list_type}}})
            continue

        bullet_match = re.match(r"^\s*[-*+]\s+(.+)$", line)
        if bullet_match:
            _append_inline(blocks, bullet_match.group(1))
            blocks.append({"text": "\n", "attributes": {"list": {"list": "bullet"}}})
            continue

        ordered_match = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        if ordered_match:
            _append_inline(blocks, ordered_match.group(1))
            blocks.append({"text": "\n", "attributes": {"list": {"list": "ordered"}}})
            continue

        heading_match = re.match(r"^#{1,6}\s+(.+)$", line)
        if heading_match:
            _append_inline(blocks, heading_match.group(1), base_attributes={"bold": True})
            blocks.append({"text": "\n", "attributes": {}})
            continue

        quote_match = re.match(r"^>\s?(.*)$", line)
        if quote_match:
            _append_inline(blocks, f"> {quote_match.group(1)}")
            blocks.append({"text": "\n", "attributes": {}})
            continue

        _append_inline(blocks, line)
        blocks.append({"text": "\n", "attributes": {}})

    if in_code_block:
        _append_code_block(blocks, "\n".join(code_lines), code_language)

    while blocks and blocks[-1].get("text") == "\n" and blocks[-1].get("attributes") == {}:
        blocks.pop()
    return blocks or [{"text": "", "attributes": {}}]


def _append_code_block(blocks: list[dict[str, Any]], text: str, language: str) -> None:
    blocks.append({"text": text, "attributes": {}})
    blocks.append({"text": "\n", "attributes": {"code-block": {"code-block": language or "plain"}}})


_INLINE_PATTERN = re.compile(
    r"(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^)]+\)|_[^_\n]+_|\*[^*\n]+\*)"
)


def _append_inline(blocks: list[dict[str, Any]], text: str, base_attributes: dict[str, Any] | None = None) -> None:
    base = base_attributes or {}
    cursor = 0
    for match in _INLINE_PATTERN.finditer(text):
        if match.start() > cursor:
            _append_text(blocks, text[cursor:match.start()], base)
        token = match.group(0)
        _append_inline_token(blocks, token, base)
        cursor = match.end()
    if cursor < len(text):
        _append_text(blocks, text[cursor:], base)


def _append_inline_token(blocks: list[dict[str, Any]], token: str, base_attributes: dict[str, Any]) -> None:
    link_match = re.match(r"^\[([^\]\n]+)\]\(([^)]+)\)$", token)
    if link_match:
        attributes = _merged_attributes(base_attributes, {"link": link_match.group(2)})
        _append_text(blocks, link_match.group(1), attributes)
        return

    if token.startswith("`") and token.endswith("`"):
        _append_text(blocks, token[1:-1], _merged_attributes(base_attributes, {"code": True}))
        return

    if token.startswith("**") and token.endswith("**"):
        _append_text(blocks, token[2:-2], _merged_attributes(base_attributes, {"bold": True}))
        return

    if (token.startswith("_") and token.endswith("_")) or (token.startswith("*") and token.endswith("*")):
        _append_text(blocks, token[1:-1], _merged_attributes(base_attributes, {"italic": True}))
        return

    _append_text(blocks, token, base_attributes)


def _append_text(blocks: list[dict[str, Any]], text: str, attributes: dict[str, Any]) -> None:
    if not text:
        return
    blocks.append({"text": text, "attributes": deepcopy(attributes)})


def _merged_attributes(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    merged.update(extra)
    return merged
