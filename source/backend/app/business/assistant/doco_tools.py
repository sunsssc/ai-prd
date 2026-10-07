from __future__ import annotations

import json
import secrets
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol
from uuid import uuid4

from app.business.assistant.models import DocoDocumentMappingRecord
from app.business.assistant.store import SQLiteAssistantStore
from app.integrations.agent_runtime import (
    RuntimeDynamicTool,
    RuntimeDynamicToolResult,
    RuntimeWorkspacePlan,
)
from app.integrations.doco import DocoError
from app.services.auth_models import UserRecord


class AssistantDocoClient(Protocol):
    async def list_knowledge_bases(self) -> object: ...

    async def create_document(
        self,
        *,
        title: str,
        knowledge_base_id: int,
        content_markdown: str,
        idempotency_key: str,
    ) -> object: ...

    async def save_document(
        self,
        *,
        document_id: str,
        content_markdown: str,
    ) -> object: ...


class AssistantDocoTools:
    system_prompt = (
        "当用户要求把本地文档保存到 Doco 知识库时，先调用 list_doco_knowledge_bases "
        "确认目标知识库（未指定时使用当前用户设置的默认知识库），再调用 save_doco_document 保存。"
        "save_doco_document 是幂等的：已保存过的本地文档会更新到同一篇 Doco 文档，不会重复创建；"
        "必须依据工具返回的 document_id 向用户汇报保存结果，不得虚构保存成功。"
        "用户询问某文档的保存状态时，调用 get_doco_document_mapping 查询。"
        "Doco 凭证来自当前用户的个人设置，不要向用户索要、回显或写入对话内容。"
    )

    def __init__(
        self,
        *,
        store: SQLiteAssistantStore,
        client_factory: Callable[[str], AssistantDocoClient],
        allowed_organizations: list[str],
    ) -> None:
        self.store = store
        self.client_factory = client_factory
        self.allowed_organizations = {
            item.strip().lower() for item in allowed_organizations if item.strip()
        }

    def is_available(self, *, user: UserRecord, organization_key: str) -> bool:
        normalized_org = organization_key.strip().lower()
        if not normalized_org or normalized_org not in self.allowed_organizations:
            return False
        return self.store.get_doco_credentials(user_id=user.user_id) is not None

    def build_tools(
        self,
        *,
        user: UserRecord,
        session_id: str,
        turn_id: str,
        organization_key: str,
        user_message: str,
        workspace_plan: RuntimeWorkspacePlan | None = None,
    ) -> list[RuntimeDynamicTool]:
        if not self.is_available(user=user, organization_key=organization_key):
            return []
        credentials = self.store.get_doco_credentials(user_id=user.user_id)
        if credentials is None:
            return []
        client = self.client_factory(credentials.api_token)
        default_knowledge_base_id = credentials.default_knowledge_base_id

        async def list_knowledge_bases(arguments: dict[str, Any]) -> RuntimeDynamicToolResult:
            del arguments
            try:
                payload = await client.list_knowledge_bases()
                return self._ok(
                    {
                        "knowledge_bases": self._knowledge_base_list(payload),
                        "default_knowledge_base_id": default_knowledge_base_id,
                    }
                )
            except Exception as exc:
                return self._error(str(exc) or "读取 Doco 知识库失败。")

        async def save_document(arguments: dict[str, Any]) -> RuntimeDynamicToolResult:
            try:
                sandbox_path = str(arguments.get("path") or "").strip()
                if not sandbox_path:
                    raise ValueError("path 不能为空。")
                local_path = self._normalize_local_path(sandbox_path)
                source_path = self._resolve_local_file(workspace_plan, local_path)
                content = source_path.read_text(encoding="utf-8")
                title = str(arguments.get("title") or "").strip() or Path(local_path).stem
                knowledge_base_id = self._resolve_knowledge_base_id(
                    arguments,
                    default_knowledge_base_id=default_knowledge_base_id,
                )

                existing = self.store.get_doco_document_mapping(
                    user_id=user.user_id,
                    local_path=local_path,
                )
                if existing is not None:
                    await client.save_document(
                        document_id=existing.document_id,
                        content_markdown=content,
                    )
                    now = datetime.now(timezone.utc)
                    self.store.upsert_doco_document_mapping(
                        DocoDocumentMappingRecord(
                            mapping_id=existing.mapping_id,
                            user_id=existing.user_id,
                            local_path=existing.local_path,
                            document_id=existing.document_id,
                            title=title,
                            knowledge_base_id=knowledge_base_id,
                            created_at=existing.created_at,
                            updated_at=now,
                        )
                    )
                    self.store.append_runtime_event(
                        session_id,
                        turn_id,
                        "activity",
                        {
                            "kind": "doco_document_updated",
                            "message": f"已更新 Doco 文档 {existing.document_id}",
                            "local_path": local_path,
                            "document_id": existing.document_id,
                            "title": title,
                            "knowledge_base_id": knowledge_base_id,
                        },
                    )
                    return self._ok(
                        {
                            "action": "updated",
                            "local_path": local_path,
                            "document_id": existing.document_id,
                            "title": title,
                            "knowledge_base_id": knowledge_base_id,
                            "document_uri": f"doco://doc/{existing.document_id}",
                        }
                    )

                created = await client.create_document(
                    title=title,
                    knowledge_base_id=knowledge_base_id,
                    content_markdown=content,
                    idempotency_key=f"doco_save_{secrets.token_hex(8)}",
                )
                document_id = self._document_id(created)
                now = datetime.now(timezone.utc)
                self.store.upsert_doco_document_mapping(
                    DocoDocumentMappingRecord(
                        mapping_id=str(uuid4()),
                        user_id=user.user_id,
                        local_path=local_path,
                        document_id=document_id,
                        title=title,
                        knowledge_base_id=knowledge_base_id,
                        created_at=now,
                        updated_at=now,
                    )
                )
                self.store.append_runtime_event(
                    session_id,
                    turn_id,
                    "activity",
                    {
                        "kind": "doco_document_created",
                        "message": f"已创建 Doco 文档 {document_id}",
                        "local_path": local_path,
                        "document_id": document_id,
                        "title": title,
                        "knowledge_base_id": knowledge_base_id,
                    },
                )
                return self._ok(
                    {
                        "action": "created",
                        "local_path": local_path,
                        "document_id": document_id,
                        "title": title,
                        "knowledge_base_id": knowledge_base_id,
                        "document_uri": f"doco://doc/{document_id}",
                    }
                )
            except DocoError as exc:
                return self._error(str(exc))
            except Exception as exc:
                return self._error(str(exc) or "保存文档到 Doco 失败。")

        async def get_mapping(arguments: dict[str, Any]) -> RuntimeDynamicToolResult:
            try:
                sandbox_path = str(arguments.get("path") or "").strip()
                if not sandbox_path:
                    raise ValueError("path 不能为空。")
                local_path = self._normalize_local_path(sandbox_path)
                mapping = self.store.get_doco_document_mapping(
                    user_id=user.user_id,
                    local_path=local_path,
                )
                if mapping is None:
                    return self._ok({"mapped": False, "local_path": local_path})
                return self._ok(
                    {
                        "mapped": True,
                        "local_path": mapping.local_path,
                        "document_id": mapping.document_id,
                        "title": mapping.title,
                        "knowledge_base_id": mapping.knowledge_base_id,
                        "updated_at": mapping.updated_at.isoformat(),
                        "document_uri": f"doco://doc/{mapping.document_id}",
                    }
                )
            except Exception as exc:
                return self._error(str(exc) or "查询 Doco 保存映射失败。")

        return [
            RuntimeDynamicTool(
                name="list_doco_knowledge_bases",
                description=(
                    "读取当前账号可用的 Doco 知识库列表（含默认知识库）。"
                    "用户准备保存文档但不确定目标知识库时调用；只读，不产生任何写入。"
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {},
                },
                handler=list_knowledge_bases,
            ),
            RuntimeDynamicTool(
                name="save_doco_document",
                description=(
                    "把工作区内的本地文档保存到 Doco 知识库（幂等）：该文档若已保存过，"
                    "则更新到同一篇 Doco 文档；否则新建。path 是工作区相对路径"
                    "（如 me/docs/xxx.md 或 coinex/knowledge/requirements/docs/xxx.md），"
                    "title 缺省取文件名，knowledge_base_id 缺省用默认知识库。"
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path"],
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "工作区内的本地文档相对路径。",
                        },
                        "title": {
                            "type": "string",
                            "description": "Doco 文档标题；缺省取文件名。",
                        },
                        "knowledge_base_id": {
                            "type": "integer",
                            "description": "目标知识库 ID；缺省用默认知识库。",
                        },
                    },
                },
                handler=save_document,
            ),
            RuntimeDynamicTool(
                name="get_doco_document_mapping",
                description=(
                    "查询某个本地文档是否已保存到 Doco 以及对应的 Doco 文档信息；只读。"
                ),
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["path"],
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "工作区内的本地文档相对路径。",
                        },
                    },
                },
                handler=get_mapping,
            ),
        ]

    def _resolve_local_file(
        self,
        workspace_plan: RuntimeWorkspacePlan | None,
        local_path: str,
    ) -> Path:
        if workspace_plan is None:
            raise ValueError("当前会话没有可用的工作区，无法定位本地文档。")
        shadow_root = Path(workspace_plan.host_shadow_root)
        if not shadow_root.is_absolute():
            raise ValueError("Runtime 工作区路径不是绝对路径。")
        candidate = (shadow_root / local_path).resolve()
        for mount in workspace_plan.mounts:
            host_root = Path(mount.host_path).resolve()
            if candidate.is_relative_to(host_root):
                if not candidate.is_file():
                    raise ValueError(f"本地文档不存在：{local_path}")
                return candidate
        raise ValueError(f"本地文档 {local_path} 超出工作区挂载范围。")

    def _resolve_knowledge_base_id(
        self,
        arguments: dict[str, Any],
        *,
        default_knowledge_base_id: int | None,
    ) -> int:
        raw = arguments.get("knowledge_base_id")
        if raw in (None, ""):
            if default_knowledge_base_id is None:
                raise ValueError("未指定目标知识库，且当前用户未配置默认知识库。")
            return default_knowledge_base_id
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise ValueError("knowledge_base_id 必须是整数。") from None

    @staticmethod
    def _normalize_local_path(sandbox_path: str) -> str:
        return sandbox_path.strip().removeprefix("/")

    @staticmethod
    def _knowledge_base_list(payload: object) -> list[dict[str, object]]:
        if not isinstance(payload, Mapping):
            return []
        items = payload.get("data")
        if not isinstance(items, list):
            return []
        result: list[dict[str, object]] = []
        for item in items:
            if not isinstance(item, Mapping):
                continue
            kb_id = item.get("id")
            name = str(item.get("name") or "").strip()
            if kb_id is None or not name:
                continue
            result.append({"id": kb_id, "name": name})
        return result

    @staticmethod
    def _document_id(payload: object) -> str:
        if isinstance(payload, Mapping):
            data = payload.get("data")
            if isinstance(data, Mapping):
                document_id = data.get("id")
                if document_id:
                    return str(document_id)
            document_id = payload.get("document_id")
            if document_id:
                return str(document_id)
        raise DocoError("Doco 创建文档响应缺少 document_id。")

    @staticmethod
    def _ok(payload: dict[str, object]) -> RuntimeDynamicToolResult:
        return RuntimeDynamicToolResult(
            content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )

    @staticmethod
    def _error(
        message: str,
        *,
        extra: dict[str, object] | None = None,
    ) -> RuntimeDynamicToolResult:
        return RuntimeDynamicToolResult(
            content=json.dumps(
                {"error": message[:1000], **(extra or {})},
                ensure_ascii=False,
                sort_keys=True,
            ),
            is_error=True,
        )
