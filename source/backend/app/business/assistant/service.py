from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import re
import shutil
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote
from uuid import uuid4

from app.business.assistant.citations import build_answer_citations
from app.business.assistant.clickup_link_resolver import resolve_clickup_doc_links
from app.business.assistant.code_guard import CodeRepoSnapshot, CodeWorkspaceGuard
from app.business.assistant.git_context import (
    AssistantGitContextResolver,
    GitContextRequest,
    PreparedTurnGitContexts,
)
from app.business.assistant.knowledge_scope import (
    build_scope_context_inputs,
    build_knowledge_scope_instruction,
    extract_explicit_knowledge_scopes,
    infer_knowledge_scopes_from_message,
)
from app.business.assistant.models import (
    AnswerCitationRecord,
    AssistantContextInput,
    AssistantFileArtifactRecord,
    AssistantMessageRecord,
    AssistantSessionRecord,
    AssistantTurnRecord,
    SessionCommentRecord,
    SessionContextMountRecord,
    SessionShareRecord,
    SharedSessionFileArtifactRecord,
    SharedSessionFollowUpRecord,
    SharedSessionSummaryRecord,
    SHARE_VISIBILITY_ANYONE,
    TestDataPlanRecord,
    TurnGitContextSnapshotRecord,
    UploadedFileRecord,
    UploadedImageFileRecord,
    UploadedImageRecord,
)
from app.business.assistant.prompts import build_assistant_system_prompt
from app.business.assistant.mcp_approvals import McpApprovalConflict, McpApprovals
from app.business.assistant.runtime_mapper import (
    dedupe_context_inputs,
    extract_context_usage_from_event,
    map_runtime_event_to_sse,
)
from app.business.assistant.store import AssistantTurnAlreadyRunningError, SQLiteAssistantStore
from app.business.assistant.test_data_tools import AssistantTestDataTools
from app.business.assistant.doco_tools import AssistantDocoTools
from app.business.workspace import WorkspaceAccessError, WorkspaceAccessService
from app.integrations.agent_runtime import (
    AgentRuntimeClient,
    AssistantRuntimeError,
    RuntimeDynamicTool,
    RuntimeDynamicToolResult,
)
from app.integrations.agent_runtime.base import discover_project_skills
from app.integrations.agent_runtime.models import (
    RuntimeEvent,
    RuntimeSession,
    RuntimeSkill,
    RuntimeWorkspaceMount,
    RuntimeWorkspacePlan,
)
from app.integrations.llm.client import AssistantLLMClient, LLMMessage
from app.services.auth_models import UserRecord


logger = logging.getLogger("uvicorn.error")


@dataclass(slots=True)
class ChatResult:
    session: AssistantSessionRecord
    turn: AssistantTurnRecord
    user_message: AssistantMessageRecord
    assistant_message: AssistantMessageRecord
    citations: list[AnswerCitationRecord]
    session_created: bool


@dataclass(slots=True)
class TurnTrace:
    turn: AssistantTurnRecord
    runtime_events: list[dict[str, object]]
    context_requests: list[AssistantContextInput]
    context_usage: list[AssistantContextInput]
    citations: list[AnswerCitationRecord]
    git_scopes: list[TurnGitContextSnapshotRecord]


@dataclass(slots=True)
class TurnExecutionState:
    session: AssistantSessionRecord
    session_created: bool
    user_message: AssistantMessageRecord
    turn: AssistantTurnRecord
    session_mounts: list[SessionContextMountRecord]
    requested_contexts: list[AssistantContextInput]
    git_context_mounts: list[RuntimeWorkspaceMount] = field(default_factory=list)
    attachment_mounts: list[RuntimeWorkspaceMount] = field(default_factory=list)
    git_context_update_messages: list[str] = field(default_factory=list)
    pending_git_scope_events: list[dict[str, object]] = field(default_factory=list)
    runtime_generated_title: str | None = None
    git_context_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    isolated_runtime_session: bool = False
    rehydrate_runtime_session: bool = False
    visualization_request: bool = False
    retry_source_message: str | None = None
    runtime_reply_chunks: list[str] = field(default_factory=list)
    runtime_final_message: str | None = None
    runtime_completed: bool = False
    runtime_image_blocks: list[str] = field(default_factory=list)
    runtime_images_accepted: int = 0
    runtime_images_blocked: int = 0
    runtime_image_quota_status: ImageGenerationQuotaStatus | None = None
    runtime_image_quota_event_persisted: bool = False
    runtime_usage_events: list[dict[str, object]] = field(default_factory=list)
    context_usage_inputs: list[AssistantContextInput] = field(default_factory=list)
    code_guard_snapshots: list[CodeRepoSnapshot] = field(default_factory=list)
    assistant_message: AssistantMessageRecord | None = None
    citations: list[AnswerCitationRecord] = field(default_factory=list)


@dataclass(slots=True)
class ActiveTurnStream:
    subscribers: set[asyncio.Queue[tuple[int, dict[str, object] | None]]] = field(default_factory=set)
    task: asyncio.Task[None] | None = None
    next_sequence: int = 0


@dataclass(slots=True)
class PersistedTurnResult:
    turn: AssistantTurnRecord
    assistant_message: AssistantMessageRecord
    citations: list[AnswerCitationRecord]


@dataclass(slots=True)
class FileArtifactDownload:
    file_path: Path
    filename: str
    mime_type: str


@dataclass(slots=True)
class RuntimeWorkspaceMountForSanitize:
    workspace_key: str
    host_path: str
    sandbox_path: str


@dataclass(slots=True)
class RuntimeWorkspacePlanForSanitize:
    host_shadow_root: str
    mounts: list[RuntimeWorkspaceMountForSanitize]


@dataclass(slots=True)
class ImageGenerationQuotaStatus:
    window: str
    used: int
    limit: int
    resets_at: datetime


_VISUALIZATION_REQUEST_MARKER = re.compile(
    r"^\s*<!-- ai-prd-visualization-request:(?P<payload>.*?) -->",
    re.DOTALL,
)
_MANUAL_RETRY_MESSAGE = re.compile(
    r"(再试|重试|重新生成|重新来|再来一次|再跑|重跑|刚才失败|上次失败|失败了)",
    re.IGNORECASE,
)
_OUTPUT_TOKEN_LIMIT_FAILURE = re.compile(
    r"(output\s+tokens?|max[_ -]?tokens?|token\s+maximum|exceeded.*tokens?|超过.*token)",
    re.IGNORECASE,
)
_MISSING_CODEX_ROLLOUT_FAILURE = re.compile(
    r"no rollout found for thread id\s+\S+",
    re.IGNORECASE,
)
_IMAGE_GENERATION_REQUEST = re.compile(
    r"(?:生图|图像生成|image\s+generation|"
    r"(?:generate|draw|create|make|design)[^。！？\n]{0,16}"
    r"(?:image|picture|photo|poster|illustration|avatar|logo|icon|cover|wallpaper)|"
    r"(?:生成|画|绘制|创作|制作|设计|做|改|修)[^。！？\n]{0,16}"
    r"(?:图片|图像|照片|海报|插画|头像|logo|标志|图标|封面|壁纸|配图))"
    r"|(?:生成|画|绘制|创作|制作|做)\s*(?:[一两二三四五六七八九十百千万\d]+\s*)?张",
    re.IGNORECASE,
)
_MARKDOWN_LINK_TARGET = re.compile(r"\[[^\]]*\]\((?P<target>[^)]+)\)")
_INLINE_CODE_TEXT = re.compile(r"`(?P<target>[^`\n]+)`")
_PERSONAL_WORKSPACE_PATH = re.compile(
    r"(?P<target>(?:sandbox:)?/?me/[^\s`'\"<>\]\)]+)",
)


def _has_agent_access(user: UserRecord | None) -> bool:
    return user is not None and user.agent_access == "active"


def sanitize_shared_turn_stream_event(event: dict[str, object]) -> dict[str, object] | None:
    event_type = str(event.get("type") or "")
    if event_type in {"delta", "citations", "message", "complete", "error"}:
        return event
    if event_type == "tool_use":
        return {
            "type": "tool_use",
            "tool_name": str(event.get("tool_name") or "工具")[:80],
        }
    if event_type == "skill_use":
        return {
            "type": "skill_use",
            "skill_name": str(event.get("skill_name") or "Skill")[:80],
        }
    if event_type == "activity":
        activity_kind = str(event.get("activity_kind") or "status")
        message = str(event.get("message") or "正在处理本轮请求").strip()
        if activity_kind == "tool_progress":
            if message.startswith("工具执行失败："):
                message = message.split("（", 1)[0]
            elif not message.startswith(("工具执行完成：", "Skill 执行完成：", "Skill 执行失败：")):
                message = "工具正在执行"
        else:
            message = "正在处理本轮请求"
        return {
            "type": "activity",
            "message": message[:160],
            "activity_kind": activity_kind,
        }
    return None


def _parse_visualization_request_scope(content: str) -> str | None:
    match = _VISUALIZATION_REQUEST_MARKER.match(content)
    if match is None:
        return None

    try:
        payload = json.loads(unquote(match.group("payload")))
    except (json.JSONDecodeError, TypeError, ValueError):
        return None

    scope = payload.get("scope")
    if scope == "session":
        return "session"
    if scope == "turn" and (payload.get("targetTurnId") or payload.get("targetMessageId")):
        return "turn"
    return None


def _is_turn_visualization_request(content: str) -> bool:
    return _parse_visualization_request_scope(content) == "turn"


def _is_visualization_request(content: str) -> bool:
    return _parse_visualization_request_scope(content) is not None


def _is_manual_retry_message(content: str) -> bool:
    return bool(_MANUAL_RETRY_MESSAGE.search(content or ""))


def _is_output_token_limit_failure(content: str | None) -> bool:
    return bool(content and _OUTPUT_TOKEN_LIMIT_FAILURE.search(content))


def requires_runtime_rebuild(error_message: str | None) -> bool:
    return bool(error_message and _MISSING_CODEX_ROLLOUT_FAILURE.search(error_message))


class AssistantSessionBusyError(RuntimeError):
    """当前应用会话已有运行中的对话轮次。"""


class AssistantService:
    allowed_image_mime_types = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
    }
    allowed_text_upload_extensions = {
        ".txt": "text/plain",
        ".md": "text/markdown",
    }
    runtime_idle_heartbeat_seconds = 10.0
    runtime_event_persist_batch_size = 8
    runtime_event_persist_types = {
        "session",
        "message",
        "usage",
        "error",
        "complete",
        "workspace_guard",
        "tool_use",
        "tool_result",
        "search",
        "read",
        "skill_use",
    }
    orphaned_turn_error_message = (
        "当前轮次已与运行中的 Agent 断开连接，可能由于后端服务重启或执行进程异常中断。"
        "已将该轮次标记为失败，请重新发送消息继续。"
    )
    stopped_turn_error_message = "用户已停止本轮生成。"

    def __init__(
        self,
        *,
        store: SQLiteAssistantStore,
        runtime_client: AgentRuntimeClient,
        system_prompt: str,
        runtime_working_directory: str,
        uploaded_files_root: str | None = None,
        workspace_access: WorkspaceAccessService | None = None,
        protected_code_root: str | None = None,
        code_guard: CodeWorkspaceGuard | None = None,
        image_generation_daily_limit: int = 0,
        image_generation_weekly_limit: int = 0,
        git_context_resolver: AssistantGitContextResolver | None = None,
        lightweight_llm_client: AssistantLLMClient | None = None,
        test_data_tools: AssistantTestDataTools | None = None,
        doco_tools: AssistantDocoTools | None = None,
    ) -> None:
        self.store = store
        self.runtime_client = runtime_client
        self.system_prompt = system_prompt
        self.runtime_working_directory = runtime_working_directory
        self.uploaded_files_root = Path(
            uploaded_files_root
            or Path(runtime_working_directory) / "workspace/runtime/uploads/assistant-images"
        ).resolve()
        self.workspace_access = workspace_access
        self.code_guard = code_guard or CodeWorkspaceGuard(
            base_dir=Path(runtime_working_directory),
            code_root=Path(protected_code_root)
            if protected_code_root is not None
            else Path(runtime_working_directory) / "workspace/coinex/knowledge/code",
        )
        self.image_generation_daily_limit = image_generation_daily_limit
        self.image_generation_weekly_limit = image_generation_weekly_limit
        self.git_context_resolver = git_context_resolver
        self.lightweight_llm_client = lightweight_llm_client
        self.test_data_tools = test_data_tools
        self.doco_tools = doco_tools
        self.mcp_approvals = McpApprovals(store)
        self._turn_streams: dict[str, ActiveTurnStream] = {}
        self._shared_follow_up_tasks: dict[str, asyncio.Task[None]] = {}

    def save_uploaded_image(
        self,
        *,
        user: UserRecord,
        original_filename: str,
        content_type: str,
        data: bytes,
        upload_root: str,
        max_bytes: int,
    ) -> UploadedImageRecord:
        if not data:
            raise ValueError("图片内容不能为空。")
        if len(data) > max_bytes:
            max_mb = max_bytes / 1024 / 1024
            raise ValueError(f"图片不能超过 {max_mb:.0f} MB。")

        detected_content_type = self._detect_image_content_type(data)
        if detected_content_type is None:
            raise ValueError("仅支持 PNG、JPEG、GIF 或 WebP 图片。")

        declared_content_type = content_type.split(";", 1)[0].strip().lower()
        if (
            declared_content_type
            and declared_content_type != "application/octet-stream"
            and declared_content_type != detected_content_type
        ):
            raise ValueError("图片类型与文件内容不一致。")

        image_id = str(uuid4())
        extension = self.allowed_image_mime_types[detected_content_type]
        display_filename = self._display_upload_filename(original_filename) or f"image{extension}"
        stored_filename = f"{image_id}{extension}"
        base_path = Path(upload_root).resolve()
        user_dir = base_path / self._safe_path_segment(user.user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        image_path = user_dir / stored_filename
        image_path.write_bytes(data)

        working_directory = Path(self.runtime_working_directory).resolve()
        try:
            source_uri = image_path.relative_to(working_directory).as_posix()
        except ValueError:
            raise ValueError("图片上传目录必须位于项目根目录内。") from None

        label = f"图片：{display_filename}"
        context_item = AssistantContextInput(
            context_key=f"uploaded-image:{image_id}",
            label=label,
            content=(
                "用户本轮上传的图片文件。请在回答前直接读取或查看该图片，"
                "不要只根据文件名推断图片内容。"
            ),
            source_type="image",
            source_uri=source_uri,
            metadata={
                "image_id": image_id,
                "mime_type": detected_content_type,
                "size_bytes": len(data),
                "original_filename": display_filename,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return UploadedImageRecord(
            image_id=image_id,
            label=label,
            file_name=display_filename,
            source_uri=source_uri,
            mime_type=detected_content_type,
            size_bytes=len(data),
            context_item=context_item,
        )

    def save_uploaded_file(
        self,
        *,
        user: UserRecord,
        original_filename: str,
        content_type: str,
        data: bytes,
        upload_root: str,
        max_bytes: int,
    ) -> UploadedFileRecord:
        display_filename = self._display_upload_filename(original_filename)
        suffix = Path(display_filename or "").suffix.lower()

        if suffix in self.allowed_text_upload_extensions:
            return self._save_uploaded_text_file(
                user=user,
                original_filename=display_filename,
                content_type=content_type,
                data=data,
                upload_root=upload_root,
                max_bytes=max_bytes,
            )

        image = self.save_uploaded_image(
            user=user,
            original_filename=original_filename,
            content_type=content_type,
            data=data,
            upload_root=upload_root,
            max_bytes=max_bytes,
        )
        return UploadedFileRecord(
            file_id=image.image_id,
            label=image.label,
            file_name=image.file_name,
            source_uri=image.source_uri,
            mime_type=image.mime_type,
            size_bytes=image.size_bytes,
            source_type="image",
            context_item=image.context_item,
        )

    def _save_uploaded_text_file(
        self,
        *,
        user: UserRecord,
        original_filename: str,
        content_type: str,
        data: bytes,
        upload_root: str,
        max_bytes: int,
    ) -> UploadedFileRecord:
        if not data:
            raise ValueError("文件内容不能为空。")
        if len(data) > max_bytes:
            max_mb = max_bytes / 1024 / 1024
            raise ValueError(f"文件不能超过 {max_mb:.0f} MB。")

        display_filename = self._display_upload_filename(original_filename)
        suffix = Path(display_filename or "").suffix.lower()
        mime_type = self.allowed_text_upload_extensions.get(suffix)
        if mime_type is None:
            raise ValueError("仅支持 PNG、JPEG、GIF、WebP 图片以及 TXT、MD 文件。")

        declared_content_type = content_type.split(";", 1)[0].strip().lower()
        allowed_declared_types = {mime_type, "text/plain", "application/octet-stream", ""}
        if suffix == ".md":
            allowed_declared_types.update({"text/markdown", "text/x-markdown"})
        if declared_content_type not in allowed_declared_types:
            raise ValueError("文件类型与扩展名不一致。")

        try:
            data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("TXT 和 MD 文件必须使用 UTF-8 编码。") from exc

        file_id = str(uuid4())
        stored_filename = f"{file_id}{suffix}"
        base_path = Path(upload_root).resolve()
        user_dir = base_path / self._safe_path_segment(user.user_id)
        user_dir.mkdir(parents=True, exist_ok=True)
        file_path = user_dir / stored_filename
        file_path.write_bytes(data)

        working_directory = Path(self.runtime_working_directory).resolve()
        try:
            source_uri = file_path.relative_to(working_directory).as_posix()
        except ValueError:
            raise ValueError("文件上传目录必须位于项目根目录内。") from None

        label = f"文件：{display_filename or stored_filename}"
        context_item = AssistantContextInput(
            context_key=f"uploaded-file:{file_id}",
            label=label,
            content=(
                "用户本轮上传的文本文件。请在回答前直接读取该文件内容，"
                "不要只根据文件名推断文件内容。"
            ),
            source_type="file",
            source_uri=source_uri,
            metadata={
                "file_id": file_id,
                "mime_type": mime_type,
                "size_bytes": len(data),
                "original_filename": display_filename or stored_filename,
                "uploaded_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return UploadedFileRecord(
            file_id=file_id,
            label=label,
            file_name=display_filename or stored_filename,
            source_uri=source_uri,
            mime_type=mime_type,
            size_bytes=len(data),
            source_type="file",
            context_item=context_item,
        )

    def get_uploaded_image_file(
        self,
        *,
        user: UserRecord,
        image_id: str,
        upload_root: str,
    ) -> UploadedImageFileRecord | None:
        image_context = self.store.get_uploaded_image_context_for_user(image_id, user.user_id)
        if image_context is None or not image_context.source_uri:
            return None

        return self._uploaded_image_file_from_context(
            image_id=image_id,
            image_context=image_context,
            upload_root=upload_root,
        )

    def get_uploaded_image_file_for_admin(
        self,
        *,
        image_id: str,
        upload_root: str,
    ) -> UploadedImageFileRecord | None:
        image_context = self.store.get_uploaded_image_context(image_id)
        if image_context is None or not image_context.source_uri:
            return None

        return self._uploaded_image_file_from_context(
            image_id=image_id,
            image_context=image_context,
            upload_root=upload_root,
        )

    def _uploaded_image_file_from_context(
        self,
        *,
        image_id: str,
        image_context,
        upload_root: str,
    ) -> UploadedImageFileRecord:
        file_path = (Path(self.runtime_working_directory).resolve() / image_context.source_uri).resolve()
        upload_root_path = Path(upload_root).resolve()
        try:
            file_path.relative_to(upload_root_path)
        except ValueError:
            raise ValueError("图片路径不在允许的上传目录内。") from None

        if not file_path.is_file():
            raise FileNotFoundError("图片文件不存在。")

        mime_type = str(image_context.metadata.get("mime_type") or self._detect_image_content_type(file_path.read_bytes()) or "")
        if mime_type not in self.allowed_image_mime_types:
            raise ValueError("图片类型不受支持。")

        original_filename = str(image_context.metadata.get("original_filename") or file_path.name)
        return UploadedImageFileRecord(
            image_id=image_id,
            file_path=str(file_path),
            file_name=self._display_upload_filename(original_filename) or file_path.name,
            mime_type=mime_type,
        )

    def create_session(
        self,
        user: UserRecord,
        title: str | None = None,
        *,
        organization_key: str | None = None,
    ) -> AssistantSessionRecord:
        session_title = self._normalize_title(title or "新会话")
        active_organization_key = self._resolve_active_organization_key(user, organization_key, session=None)
        return self._attach_session_scopes(
            user,
            self.store.create_session(
                user.user_id,
                session_title,
                runtime_provider=self.runtime_client.provider,
                runtime_working_directory=self.runtime_working_directory,
                active_organization_key=active_organization_key,
            ),
        )

    def create_pull_request_session(
        self,
        user: UserRecord,
        *,
        task_id: str,
        repo_full_name: str,
        pr_number: int,
        resolved_sha: str | None,
        organization_key: str,
    ) -> AssistantSessionRecord:
        active_organization_key = self._resolve_active_organization_key(
            user,
            organization_key,
            session=None,
        )
        if active_organization_key != organization_key:
            raise ValueError("仓库不属于当前用户可访问的组织。")
        repo_name = repo_full_name.split("/", 1)[-1]
        return self._attach_session_scopes(
            user,
            self.store.create_session(
                user.user_id,
                self._normalize_title(f"{repo_name} PR #{pr_number}"),
                runtime_provider=self.runtime_client.provider,
                runtime_working_directory=self.runtime_working_directory,
                active_organization_key=organization_key,
                git_context_default={
                    "repo_full_name": repo_full_name,
                    "default_strategy": "follow",
                    "selector_type": "pull_request",
                    "selector_value": str(pr_number),
                    "last_resolved_sha": resolved_sha,
                    "logical_path": f"/repos/{repo_name}/code",
                    "authorization_source": "task_pr",
                    "authorization_key": task_id,
                    "last_checked_at": datetime.now(timezone.utc).isoformat() if resolved_sha else None,
                },
            ),
        )

    def list_sessions(
        self,
        user: UserRecord,
        *,
        query: str | None = None,
        favorited_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AssistantSessionRecord]:
        sessions = self.store.list_sessions(
            user.user_id,
            query=query,
            favorited_only=favorited_only,
            limit=limit,
            offset=offset,
        )
        scope_ids_by_session = self.store.list_sessions_knowledge_scope_ids(
            [session.session_id for session in sessions],
            user.user_id,
        )
        git_defaults_by_session = self.store.list_sessions_git_context_defaults(
            [session.session_id for session in sessions],
            user.user_id,
        )
        running_session_ids = self.store.list_running_turn_session_ids(
            [session.session_id for session in sessions]
        )
        favorite_session_ids = self.store.list_favorite_session_ids(
            user.user_id,
            [session.session_id for session in sessions],
        )
        for session in sessions:
            session.knowledge_scope_ids = scope_ids_by_session.get(session.session_id, [])
            session.git_context_defaults = git_defaults_by_session.get(session.session_id, [])
            session.has_active_turn = session.session_id in running_session_ids
            session.is_favorited = session.session_id in favorite_session_ids
        return sessions

    def get_session(self, user: UserRecord, session_id: str) -> AssistantSessionRecord | None:
        session = self.store.get_session(session_id, user.user_id)
        if session is None:
            return None
        session.is_favorited = session.session_id in self.store.list_favorite_session_ids(
            user.user_id,
            [session.session_id],
        )
        return self._attach_session_scopes(user, session)

    def set_session_favorite(self, user: UserRecord, session_id: str, *, is_favorited: bool) -> bool:
        return self.store.set_session_favorite(
            session_id,
            user.user_id,
            is_favorited=is_favorited,
            has_agent_access=_has_agent_access(user),
        )

    def get_session_messages(self, user: UserRecord, session_id: str) -> list[AssistantMessageRecord]:
        self.ensure_session_file_artifacts(user, session_id)
        return self.store.list_messages(session_id, user.user_id)

    def list_session_test_data_plans(
        self,
        user: UserRecord,
        session_id: str,
    ) -> list[TestDataPlanRecord]:
        if self.get_session(user, session_id) is None:
            raise ValueError("会话不存在或无权限访问。")
        return self.store.list_test_data_plans(
            user_id=user.user_id,
            session_id=session_id,
        )

    def consume_session_test_data_plan_secret(
        self,
        user: UserRecord,
        session_id: str,
        plan_id: str,
    ) -> dict[str, object]:
        if self.get_session(user, session_id) is None:
            raise ValueError("会话不存在或无权限访问。")
        secret = self.store.consume_test_data_plan_secret(
            plan_id=plan_id,
            user_id=user.user_id,
            session_id=session_id,
        )
        if secret is None:
            raise ValueError("一次性凭据不存在、尚未生成或已经领取。")
        return secret

    def set_message_feedback(
        self,
        user: UserRecord,
        message_id: str,
        feedback: str | None,
    ) -> AssistantMessageRecord:
        self.store.set_message_feedback(message_id, user.user_id, feedback)
        message = self.store.get_message(message_id, user.user_id)
        if message is None:
            raise ValueError("消息不存在或无权限访问。")
        return message

    def get_active_session_turn(self, user: UserRecord, session_id: str) -> AssistantTurnRecord | None:
        active_turn = self.store.get_active_turn(session_id, user.user_id)
        if active_turn is None:
            return None
        reconciled_turn = self._reconcile_running_turn(active_turn)
        if reconciled_turn.status == "running":
            return reconciled_turn
        return None

    def ensure_session_can_start_turn(self, user: UserRecord, session_id: str) -> None:
        if self.store.get_session(session_id, user.user_id) is None:
            raise ValueError("会话不存在或无权限访问。")
        active_turn = self.get_active_session_turn(user, session_id)
        if active_turn is not None:
            raise AssistantSessionBusyError("当前会话仍有回复正在生成，请等待上一轮完成后再发送。")

    def get_latest_session_turn_trace(self, user: UserRecord, session_id: str) -> TurnTrace | None:
        latest_turn = self.get_latest_session_turn(user, session_id)
        if latest_turn is None:
            return None
        if latest_turn.status == "completed":
            return None
        return self.get_turn_trace(user, session_id, latest_turn.turn_id)

    def get_latest_session_turn(self, user: UserRecord, session_id: str) -> AssistantTurnRecord | None:
        latest_turn = self.store.get_latest_turn(session_id, user.user_id)
        if latest_turn is None:
            return None
        return self._reconcile_running_turn(latest_turn)

    def list_session_mounts(self, user: UserRecord, session_id: str) -> list[SessionContextMountRecord]:
        return self.store.list_session_mounts(session_id, user.user_id)

    def replace_session_mounts(
        self,
        user: UserRecord,
        session_id: str,
        mounts: list[AssistantContextInput],
    ) -> list[SessionContextMountRecord]:
        return self.store.replace_session_mounts(session_id, user.user_id, mounts)

    async def list_available_skills(self) -> list[dict[str, str]]:
        skills = await self.runtime_client.list_available_skills(working_directory=self.runtime_working_directory)
        return [
            {
                "skill_id": skill.skill_id,
                "name": skill.name,
                "description": skill.description,
                "path": skill.path,
            }
            for skill in skills
        ]

    def get_turn_trace(self, user: UserRecord, session_id: str, turn_id: str) -> TurnTrace | None:
        session = self.store.get_session(session_id, user.user_id)
        if session is None:
            return None

        turn = self.store.get_turn(turn_id)
        if turn is None or turn.session_id != session_id:
            return None

        return TurnTrace(
            turn=turn,
            runtime_events=[
                {
                    "event_id": event.event_id,
                    "type": event.event_type,
                    "payload": event.payload,
                    "created_at": event.created_at.isoformat(),
                }
                for event in self.store.list_runtime_events(turn_id)
            ],
            context_requests=[self._to_context_input(item) for item in self.store.list_turn_context_requests(turn_id)],
            context_usage=[self._to_context_input(item) for item in self.store.list_turn_context_usage(turn_id)],
            citations=self.store.list_answer_citations(turn_id),
            git_scopes=self.store.list_turn_git_context_snapshots(turn_id),
        )

    def rename_session(self, user: UserRecord, session_id: str, title: str) -> AssistantSessionRecord | None:
        session = self.store.rename_session(session_id, user.user_id, self._normalize_title(title))
        if session is None:
            return None
        return self._attach_session_scopes(user, session)

    def delete_session(self, user: UserRecord, session_id: str) -> bool:
        return self.store.delete_session(session_id, user.user_id)

    def upsert_session_share(
        self,
        user: UserRecord,
        session_id: str,
        *,
        share_type: str,
        member_user_ids: list[str],
        visibility: str = SHARE_VISIBILITY_ANYONE,
    ) -> SessionShareRecord:
        self.ensure_session_file_artifacts(user, session_id)
        return self.store.upsert_session_share(
            session_id,
            user.user_id,
            share_type=share_type,
            member_user_ids=member_user_ids,
            visibility=visibility,
        )

    def get_session_share(self, user: UserRecord, session_id: str) -> SessionShareRecord | None:
        if self.store.get_session(session_id, user.user_id) is None:
            raise ValueError("会话不存在或无权限访问。")
        return self.store.get_active_session_share_for_owner(session_id, user.user_id)

    def revoke_session_share(self, user: UserRecord, session_id: str) -> bool:
        if self.store.get_session(session_id, user.user_id) is None:
            raise ValueError("会话不存在或无权限访问。")
        return self.store.revoke_session_share(session_id, user.user_id)

    def get_shared_session(
        self,
        user: UserRecord | None,
        share_token: str,
    ) -> tuple[SharedSessionSummaryRecord, list[AssistantMessageRecord]] | None:
        user_id = user.user_id if user is not None else None
        shared = self.store.get_shared_session_by_token(
            share_token,
            user_id,
            has_agent_access=_has_agent_access(user),
        )
        if shared is None:
            return None
        if user is not None:
            shared.session.is_favorited = shared.session.session_id in self.store.list_favorite_session_ids(
                user.user_id,
                [shared.session.session_id],
            )
        shared.session.knowledge_scope_ids = self.store.list_session_knowledge_scope_ids(
            shared.session.session_id,
            shared.session.user_id,
        )
        shared.session.git_context_defaults = self.store.get_session_git_context_defaults(
            shared.session.session_id,
            shared.session.user_id,
        )
        self.ensure_session_file_artifacts_by_session(shared.session)
        messages = self.store.list_messages_by_session_id(shared.session.session_id, user_id)
        return shared, messages

    def enqueue_shared_follow_up(
        self,
        user: UserRecord,
        share_token: str,
        message: str,
    ) -> tuple[SharedSessionFollowUpRecord, AssistantSessionRecord]:
        shared = self.store.get_shared_session_by_token(
            share_token,
            user.user_id,
            has_agent_access=_has_agent_access(user),
        )
        if shared is None:
            raise ValueError("共享会话不存在或无权限访问。")
        request = self.store.enqueue_shared_follow_up(
            share_id=shared.share.share_id,
            session_id=shared.session.session_id,
            requested_by_user_id=user.user_id,
            content=message.strip(),
        )
        return request, shared.session

    def rebuild_shared_follow_up_runtime(
        self,
        user: UserRecord,
        owner: UserRecord,
        share_token: str,
        request_id: str,
    ) -> tuple[SharedSessionFollowUpRecord, AssistantSessionRecord]:
        shared = self.store.get_shared_session_by_token(
            share_token,
            user.user_id,
            has_agent_access=_has_agent_access(user),
        )
        if shared is None:
            raise ValueError("共享会话不存在或无权限访问。")
        request = self.store.get_shared_follow_up(request_id)
        if (
            request is None
            or request.share_id != shared.share.share_id
            or request.session_id != shared.session.session_id
        ):
            raise ValueError("共享追问不存在。")
        if user.user_id not in {request.requested_by_user_id, shared.session.user_id}:
            raise PermissionError("只有追问者或会话分享者可以确认重建。")
        if (
            request.status != "failed"
            or not request.turn_id
            or not requires_runtime_rebuild(request.error_message)
        ):
            raise ValueError("该追问不需要重建 Agent Runtime。")

        current = self._shared_follow_up_tasks.get(shared.session.session_id)
        if current is not None and not current.done():
            raise AssistantSessionBusyError("当前会话仍有追问正在执行，请稍后再重建。")
        try:
            prepared = self.store.prepare_shared_follow_up_runtime_rebuild(
                request_id=request.request_id,
                session_id=shared.session.session_id,
                owner_id=shared.session.user_id,
            )
        except AssistantTurnAlreadyRunningError as exc:
            raise AssistantSessionBusyError(str(exc)) from exc
        if prepared is None:
            raise ValueError("共享追问状态已变化，请刷新后重试。")

        self.start_shared_follow_up_rebuild_runner(
            owner,
            prepared,
            source_turn_id=request.turn_id,
        )
        refreshed_session = self.store.get_session(shared.session.session_id, shared.session.user_id)
        return prepared, refreshed_session or shared.session

    def start_shared_follow_up_rebuild_runner(
        self,
        owner: UserRecord,
        request: SharedSessionFollowUpRecord,
        *,
        source_turn_id: str,
    ) -> None:
        session_id = request.session_id
        current = self._shared_follow_up_tasks.get(session_id)
        if current is not None and not current.done():
            raise AssistantSessionBusyError("当前会话仍有追问正在执行，请稍后再重建。")
        task = asyncio.create_task(
            self._rerun_shared_follow_up_after_runtime_rebuild(
                owner,
                request.request_id,
                source_turn_id=source_turn_id,
            )
        )
        self._shared_follow_up_tasks[session_id] = task

        def cleanup(completed_task: asyncio.Task[None]) -> None:
            if self._shared_follow_up_tasks.get(session_id) is completed_task:
                self._shared_follow_up_tasks.pop(session_id, None)
                refreshed_request = self.store.get_shared_follow_up(request.request_id)
                if (
                    refreshed_request is not None
                    and refreshed_request.status == "completed"
                    and self.store.list_pending_shared_follow_ups(session_id)
                ):
                    self.start_shared_follow_up_runner(owner, session_id)

        task.add_done_callback(cleanup)

    async def _rerun_shared_follow_up_after_runtime_rebuild(
        self,
        owner: UserRecord,
        request_id: str,
        *,
        source_turn_id: str,
    ) -> None:
        error_message: str | None = None
        try:
            async for event in self.stream_rerun_turn(
                owner,
                source_turn_id,
                rehydrate_runtime_session=True,
            ):
                if event.get("type") == "turn_start" and isinstance(event.get("turn_id"), str):
                    self.store.set_shared_follow_up_turn(request_id, str(event["turn_id"]))
                elif event.get("type") == "error":
                    error_message = str(event.get("message") or "共享追问执行失败。")
        except Exception as exc:
            error_message = f"Runtime 重建失败: {exc}"
        self.store.complete_shared_follow_up(
            request_id,
            status="failed" if error_message else "completed",
            error_message=error_message,
        )

    def start_shared_follow_up_runner(self, owner: UserRecord, session_id: str) -> None:
        current = self._shared_follow_up_tasks.get(session_id)
        if current is not None and not current.done():
            return
        task = asyncio.create_task(self._drain_shared_follow_ups(owner, session_id))
        self._shared_follow_up_tasks[session_id] = task

        def cleanup(completed_task: asyncio.Task[None]) -> None:
            if self._shared_follow_up_tasks.get(session_id) is completed_task:
                self._shared_follow_up_tasks.pop(session_id, None)

        task.add_done_callback(cleanup)

    async def _drain_shared_follow_ups(self, owner: UserRecord, session_id: str) -> None:
        while True:
            self.get_active_session_turn(owner, session_id)
            self.store.reconcile_processing_shared_follow_ups(session_id)
            request = self.store.claim_next_shared_follow_up(session_id)
            if request is None:
                if not self.store.list_pending_shared_follow_ups(session_id):
                    return
                await asyncio.sleep(0.2)
                continue

            turn_id: str | None = None
            error_message: str | None = None
            async for event in self.stream_chat(
                owner,
                session_id,
                request.content,
                context_items=[],
                organization_key=None,
            ):
                if event.get("type") == "turn_start" and isinstance(event.get("turn_id"), str):
                    turn_id = str(event["turn_id"])
                    self.store.set_shared_follow_up_turn(request.request_id, turn_id)
                elif event.get("type") == "error":
                    error_message = str(event.get("message") or "共享追问执行失败。")

            if error_message is not None and turn_id is None:
                active_turn = self.store.get_active_turn(session_id, owner.user_id)
                if active_turn is not None:
                    self.store.requeue_shared_follow_up(request.request_id)
                    await asyncio.sleep(0.2)
                    continue

            self.store.complete_shared_follow_up(
                request.request_id,
                status="failed" if error_message else "completed",
                error_message=error_message,
            )
            if requires_runtime_rebuild(error_message):
                return

    def list_shared_sessions(self, user: UserRecord) -> list[SharedSessionSummaryRecord]:
        sessions = self.store.list_shared_sessions(user.user_id)
        running_session_ids = self.store.list_running_turn_session_ids(
            [shared.session.session_id for shared in sessions]
        )
        favorite_session_ids = self.store.list_favorite_session_ids(
            user.user_id,
            [shared.session.session_id for shared in sessions],
        )
        for shared in sessions:
            shared.session.knowledge_scope_ids = self.store.list_session_knowledge_scope_ids(
                shared.session.session_id,
                shared.session.user_id,
            )
            shared.session.git_context_defaults = self.store.get_session_git_context_defaults(
                shared.session.session_id,
                shared.session.user_id,
            )
            shared.session.has_active_turn = shared.session.session_id in running_session_ids
            shared.session.is_favorited = shared.session.session_id in favorite_session_ids
        return sessions

    def ensure_session_file_artifacts(
        self,
        user: UserRecord,
        session_id: str,
    ) -> list[AssistantFileArtifactRecord]:
        session = self.store.get_session(session_id, user.user_id)
        if session is None:
            raise ValueError("会话不存在或无权限访问。")
        return self.ensure_session_file_artifacts_by_session(session)

    def ensure_session_file_artifacts_by_session(
        self,
        session: AssistantSessionRecord,
    ) -> list[AssistantFileArtifactRecord]:
        messages = self.store.list_messages_by_session_id(session.session_id, session.user_id)
        turns = self.store.list_turns(session.session_id, session.user_id)
        turn_id_by_message_id: dict[str, str] = {}
        for turn in turns:
            turn_id_by_message_id[turn.user_message_id] = turn.turn_id
            if turn.assistant_message_id:
                turn_id_by_message_id[turn.assistant_message_id] = turn.turn_id

        artifacts: list[AssistantFileArtifactRecord] = []
        seen_keys: set[tuple[str | None, str | None, str]] = set()

        def register(display_path: str, *, message_id: str | None, turn_id: str | None) -> None:
            resolved = self._resolve_owner_file_artifact_source(session.user_id, display_path)
            if resolved is None:
                return
            normalized_display_path, source_path, file_path = resolved
            key = (message_id, turn_id, source_path)
            if key in seen_keys:
                return
            seen_keys.add(key)
            artifacts.append(
                self._upsert_file_artifact_from_path(
                    session=session,
                    message_id=message_id,
                    turn_id=turn_id,
                    display_path=normalized_display_path,
                    source_path=source_path,
                    file_path=file_path,
                )
            )

        for message in messages:
            turn_id = turn_id_by_message_id.get(message.message_id)
            for display_path in self._extract_personal_workspace_paths(message.content):
                register(display_path, message_id=message.message_id, turn_id=turn_id)

        citations_by_message = self.store.list_citations_by_session_id(session.session_id)
        for message_id, citations in citations_by_message.items():
            turn_id = turn_id_by_message_id.get(message_id)
            for citation in citations:
                for display_path in self._extract_personal_workspace_paths(citation.path or ""):
                    register(display_path, message_id=message_id, turn_id=turn_id)

        for event in self.store.list_runtime_events_by_session_id(session.session_id):
            for text in self._iter_payload_strings(event.payload):
                for display_path in self._extract_personal_workspace_paths(text):
                    register(display_path, message_id=None, turn_id=event.turn_id)

        return self.store.list_file_artifacts_by_session_id(session.session_id)

    def list_session_file_artifacts(self, session_id: str) -> list[AssistantFileArtifactRecord]:
        return self.store.list_file_artifacts_by_session_id(session_id)

    def get_owner_file_artifact_download(
        self,
        user: UserRecord,
        session_id: str,
        artifact_id: str,
    ) -> FileArtifactDownload | None:
        session = self.store.get_session(session_id, user.user_id)
        if session is None:
            return None
        self.ensure_session_file_artifacts_by_session(session)
        artifact = self.store.get_file_artifact_for_owner(session_id, artifact_id, user.user_id)
        if artifact is None:
            return None
        file_path = self._source_file_path_for_artifact(artifact)
        if file_path is None:
            return None
        return FileArtifactDownload(file_path=file_path, filename=artifact.filename, mime_type=artifact.mime_type)

    def get_admin_file_artifact_download(
        self,
        session_id: str,
        artifact_id: str,
    ) -> FileArtifactDownload | None:
        session = self.store.get_session_by_id(session_id)
        if session is None:
            return None
        self.ensure_session_file_artifacts_by_session(session)
        artifact = self.store.get_file_artifact_for_owner(session_id, artifact_id, session.user_id)
        if artifact is None:
            return None
        file_path = self._source_file_path_for_artifact(artifact)
        if file_path is None:
            return None
        return FileArtifactDownload(file_path=file_path, filename=artifact.filename, mime_type=artifact.mime_type)

    def get_shared_file_artifact_download(
        self,
        user: UserRecord | None,
        share_token: str,
        artifact_id: str,
    ) -> FileArtifactDownload | None:
        shared = self.store.get_shared_session_by_token(
            share_token,
            user.user_id if user is not None else None,
            has_agent_access=_has_agent_access(user),
        )
        if shared is None:
            return None
        self.ensure_session_file_artifacts_by_session(shared.session)
        artifact = self.store.get_file_artifact_for_owner(
            shared.session.session_id,
            artifact_id,
            shared.session.user_id,
        )
        if artifact is None:
            return None
        snapshot = self.store.get_shared_file_artifact(shared.share.share_id, artifact.artifact_id)
        if snapshot is None:
            snapshot = self._create_shared_file_snapshot(shared.share.share_id, artifact)
        if snapshot is None:
            return None
        snapshot_path = (Path(self.runtime_working_directory).resolve() / snapshot.snapshot_path).resolve()
        if not snapshot_path.is_file():
            snapshot = self._create_shared_file_snapshot(shared.share.share_id, artifact)
            if snapshot is None:
                return None
            snapshot_path = (Path(self.runtime_working_directory).resolve() / snapshot.snapshot_path).resolve()
            if not snapshot_path.is_file():
                return None
        return FileArtifactDownload(
            file_path=snapshot_path,
            filename=snapshot.filename,
            mime_type=snapshot.mime_type,
        )

    def list_session_comments(
        self,
        user: UserRecord | None,
        session_id: str,
        *,
        message_id: str | None = None,
    ) -> list[SessionCommentRecord]:
        if not self.store.user_can_access_shared_session(
            session_id,
            user.user_id if user is not None else None,
            has_agent_access=_has_agent_access(user),
        ):
            raise ValueError("会话不存在或无权限访问。")
        return self.store.list_session_comments(session_id, message_id=message_id)

    def create_session_comment(
        self,
        user: UserRecord,
        session_id: str,
        *,
        content: str,
        message_id: str | None = None,
    ) -> SessionCommentRecord:
        return self.store.create_session_comment(
            session_id,
            user.user_id,
            content=content,
            message_id=message_id,
            has_agent_access=_has_agent_access(user),
        )

    def update_session_comment(self, user: UserRecord, comment_id: str, content: str) -> SessionCommentRecord | None:
        return self.store.update_session_comment(comment_id, user.user_id, content)

    def delete_session_comment(self, user: UserRecord, comment_id: str) -> bool:
        return self.store.delete_session_comment(comment_id, user.user_id)

    async def chat(
        self,
        user: UserRecord,
        session_id: str | None,
        message: str,
        context_items: list[AssistantContextInput] | None = None,
        organization_key: str | None = None,
    ) -> ChatResult:
        state: TurnExecutionState | None = None
        execute_kwargs = dict(
            user=user,
            session_id=session_id,
            message=message,
            context_items=context_items or [],
            organization_key=organization_key,
        )
        async for _event, current_state in self._execute_turn(**execute_kwargs):
            state = current_state

        if state is None or state.assistant_message is None:
            raise AssistantRuntimeError("Runtime 未生成有效回复。")

        refreshed_session = self.store.get_session(state.session.session_id, user.user_id)
        assert refreshed_session is not None
        refreshed_session = self._attach_session_scopes(user, refreshed_session)
        return ChatResult(
            session=refreshed_session,
            turn=self.store.get_turn(state.turn.turn_id) or state.turn,
            user_message=state.user_message,
            assistant_message=state.assistant_message,
            citations=state.citations,
            session_created=state.session_created,
        )

    async def stream_chat(
        self,
        user: UserRecord,
        session_id: str | None,
        message: str,
        context_items: list[AssistantContextInput] | None = None,
        organization_key: str | None = None,
    ) -> AsyncIterator[dict[str, object]]:
        queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()

        async def run_turn() -> None:
            active_turn_id: str | None = None
            try:
                execute_kwargs = dict(
                    user=user,
                    session_id=session_id,
                    message=message,
                    context_items=context_items or [],
                    organization_key=organization_key,
                )
                async for event, _state in self._execute_turn(**execute_kwargs):
                    await queue.put(event)
                    if event.get("type") == "turn_start" and isinstance(event.get("turn_id"), str):
                        active_turn_id = str(event["turn_id"])
                        self._register_turn_stream(active_turn_id, task=run_task)
                    if active_turn_id is not None:
                        await self._publish_turn_stream_event(active_turn_id, event)
            except Exception as exc:
                error_event = {"type": "error", "message": str(exc)}
                await queue.put(error_event)
                if active_turn_id is not None:
                    await self._publish_turn_stream_event(active_turn_id, error_event)
            finally:
                await queue.put(None)
                if active_turn_id is not None:
                    await self._close_turn_stream(active_turn_id)

        run_task = asyncio.create_task(run_turn())

        while True:
            event = await queue.get()
            if event is None:
                break
            yield event

    async def stream_rerun_turn(
        self,
        user: UserRecord,
        turn_id: str,
        *,
        rehydrate_runtime_session: bool = False,
    ) -> AsyncIterator[dict[str, object]]:
        queue: asyncio.Queue[dict[str, object] | None] = asyncio.Queue()

        async def run_turn() -> None:
            active_turn_id: str | None = None
            try:
                async for event, _state in self._execute_turn(
                    user=user,
                    session_id=None,
                    message="",
                    context_items=[],
                    rerun_turn_id=turn_id,
                    rehydrate_runtime_session=rehydrate_runtime_session,
                ):
                    await queue.put(event)
                    if event.get("type") == "turn_start" and isinstance(event.get("turn_id"), str):
                        active_turn_id = str(event["turn_id"])
                        self._register_turn_stream(active_turn_id, task=run_task)
                    if active_turn_id is not None:
                        await self._publish_turn_stream_event(active_turn_id, event)
            except Exception as exc:
                error_event = {"type": "error", "message": str(exc)}
                await queue.put(error_event)
                if active_turn_id is not None:
                    await self._publish_turn_stream_event(active_turn_id, error_event)
            finally:
                await queue.put(None)
                if active_turn_id is not None:
                    await self._close_turn_stream(active_turn_id)

        run_task = asyncio.create_task(run_turn())

        while True:
            event = await queue.get()
            if event is None:
                break
            yield event

    async def resume_turn_stream(
        self,
        user: UserRecord,
        turn_id: str,
    ) -> AsyncIterator[dict[str, object]]:
        turn = self._reconcile_running_turn(self._require_turn_access(user, turn_id))
        async for event in self._resume_accessible_turn_stream(turn, user.user_id):
            yield event

    async def resume_shared_turn_stream(
        self,
        user: UserRecord | None,
        share_token: str,
        turn_id: str,
    ) -> AsyncIterator[dict[str, object]]:
        user_id = user.user_id if user is not None else None
        shared = self.store.get_shared_session_by_token(
            share_token,
            user_id,
            has_agent_access=_has_agent_access(user),
        )
        turn = self.store.get_turn(turn_id)
        if shared is None or turn is None or turn.session_id != shared.session.session_id:
            raise ValueError("轮次不存在或无权限访问。")
        turn = self._reconcile_running_turn(turn)
        async for event in self._resume_accessible_turn_stream(turn, shared.session.user_id):
            shared_event = sanitize_shared_turn_stream_event(event)
            if shared_event is not None:
                yield shared_event

    async def _resume_accessible_turn_stream(
        self,
        turn: AssistantTurnRecord,
        owner_user_id: str,
    ) -> AsyncIterator[dict[str, object]]:
        active_stream = self._turn_streams.get(turn.turn_id)
        subscriber_queue: asyncio.Queue[tuple[int, dict[str, object] | None]] | None = None
        snapshot_sequence = None
        if active_stream is not None:
            snapshot_sequence = active_stream.next_sequence
            subscriber_queue = asyncio.Queue()
            active_stream.subscribers.add(subscriber_queue)

        try:
            for event in self._build_resume_events(owner_user_id, turn, include_terminal=active_stream is None):
                yield event

            if active_stream is None or subscriber_queue is None:
                return

            while True:
                sequence, event = await subscriber_queue.get()
                if snapshot_sequence is not None and sequence <= snapshot_sequence:
                    continue
                if event is None:
                    break
                yield event
        finally:
            if active_stream is not None and subscriber_queue is not None:
                active_stream.subscribers.discard(subscriber_queue)

    async def stop_turn(self, user: UserRecord, turn_id: str) -> AssistantTurnRecord:
        turn = self._require_turn_access(user, turn_id)
        if turn.status != "running":
            return turn

        self.mcp_approvals.expire_turn(turn_id)
        stream = self._turn_streams.get(turn_id)
        if stream is not None and stream.task is not None and not stream.task.done():
            stream.task.cancel()

        self.store.append_runtime_event(
            turn.session_id,
            turn.turn_id,
            "activity",
            {
                "message": self.stopped_turn_error_message,
                "kind": "user_stop",
            },
        )
        stopped_turn = self.store.fail_turn(turn.turn_id, self.stopped_turn_error_message)
        await self._publish_turn_stream_event(
            turn.turn_id,
            {
                "type": "activity",
                "message": self.stopped_turn_error_message,
                "activity_kind": "user_stop",
            },
        )
        await self._close_turn_stream(turn.turn_id)
        return stopped_turn or turn

    def decide_mcp_approval(
        self, user: UserRecord, turn_id: str, approval_id: str, decision: str
    ) -> dict[str, object]:
        turn = self._require_turn_access(user, turn_id)
        if turn.status != "running":
            self.mcp_approvals.expire_turn(turn_id)
            raise McpApprovalConflict("本轮执行已结束，审批请求已失效。")
        return self.mcp_approvals.decide(turn.session_id, turn_id, approval_id, decision, user.user_id)

    async def _prepare_git_contexts(self, **kwargs) -> PreparedTurnGitContexts:
        assert self.git_context_resolver is not None
        try:
            return await asyncio.to_thread(self.git_context_resolver.prepare, **kwargs)
        except ValueError:
            raise
        except Exception as exc:
            raise AssistantRuntimeError(f"Git Context 准备失败：{exc}") from exc

    async def _execute_turn(
        self,
        *,
        user: UserRecord,
        session_id: str | None,
        message: str,
        context_items: list[AssistantContextInput],
        organization_key: str | None = None,
        rerun_turn_id: str | None = None,
        rehydrate_runtime_session: bool = False,
    ) -> AsyncIterator[tuple[dict[str, object], TurnExecutionState]]:
        prepared_git_contexts = PreparedTurnGitContexts(
            requests=[],
            snapshots=[],
            mounts=[],
            update_messages=[],
        )
        attachment_mounts: list[RuntimeWorkspaceMount] = []
        attachment_sandbox_paths: dict[str, str] = {}
        if rerun_turn_id:
            source_turn = self._require_turn_access(user, rerun_turn_id)
            if source_turn.status != "failed":
                raise ValueError("只能重跑失败的轮次。")
            session = self.store.get_session(source_turn.session_id, user.user_id)
            if session is None:
                raise ValueError("会话不存在或无权限访问。")
            user_message = self.store.get_message(source_turn.user_message_id, user.user_id)
            if user_message is None or user_message.role != "user":
                raise ValueError("原始用户消息不存在，无法重跑。")
            message = user_message.content
            context_items = [
                self._to_context_input(item)
                for item in self.store.list_turn_context_requests(source_turn.turn_id)
            ]
            attachment_mounts, attachment_sandbox_paths = self._build_turn_attachment_mounts(
                user=user,
                context_items=context_items,
            )
            self._ensure_runtime_supports_local_images(context_items, attachment_sandbox_paths)
            session_created = False
            if self.get_active_session_turn(user, session.session_id) is not None:
                raise AssistantSessionBusyError("当前会话仍有回复正在生成，请等待上一轮完成后再发送。")
            session_mounts = self.store.list_session_mounts(session.session_id, user.user_id)
            source_git_snapshots = self.store.list_turn_git_context_snapshots(source_turn.turn_id)
            if self.git_context_resolver is not None:
                prepared_git_contexts = await self._prepare_git_contexts(
                    user=user,
                    session=session,
                    explicit_requests=[],
                    rerun_snapshots=source_git_snapshots,
                )
            try:
                turn = self.store.create_turn(
                    session.session_id,
                    user.user_id,
                    user_message.message_id,
                    git_context_requests=prepared_git_contexts.requests,
                    git_context_snapshots=prepared_git_contexts.snapshots,
                )
            except AssistantTurnAlreadyRunningError as exc:
                raise AssistantSessionBusyError(str(exc)) from exc
            self.store.record_turn_context_requests(turn.turn_id, context_items)
        else:
            attachment_mounts, attachment_sandbox_paths = self._build_turn_attachment_mounts(
                user=user,
                context_items=context_items,
            )
            self._ensure_runtime_supports_local_images(context_items, attachment_sandbox_paths)
            session, session_created = self._resolve_session(user, session_id, message, organization_key=organization_key)
            if self.get_active_session_turn(user, session.session_id) is not None:
                raise AssistantSessionBusyError("当前会话仍有回复正在生成，请等待上一轮完成后再发送。")

            session_mounts = self.store.list_session_mounts(session.session_id, user.user_id)
            try:
                user_message, turn = self.store.start_turn(
                    session.session_id,
                    user.user_id,
                    message.strip(),
                    git_context_requests=prepared_git_contexts.requests,
                    git_context_snapshots=prepared_git_contexts.snapshots,
                )
            except AssistantTurnAlreadyRunningError as exc:
                raise AssistantSessionBusyError(str(exc)) from exc
            self.store.record_turn_context_requests(turn.turn_id, context_items)

        retry_source_message = self._find_failed_turn_visualization_retry_source(user, session, message)
        visualization_request = _is_visualization_request(message) or retry_source_message is not None
        isolated_runtime_session = _is_turn_visualization_request(message) or retry_source_message is not None
        state = TurnExecutionState(
            session=session,
            session_created=session_created,
            user_message=user_message,
            turn=turn,
            session_mounts=session_mounts,
            requested_contexts=context_items,
            isolated_runtime_session=isolated_runtime_session,
            rehydrate_runtime_session=rehydrate_runtime_session,
            visualization_request=visualization_request,
            retry_source_message=retry_source_message,
            git_context_mounts=list(prepared_git_contexts.mounts),
            attachment_mounts=attachment_mounts,
            git_context_update_messages=list(prepared_git_contexts.update_messages),
        )
        for snapshot in prepared_git_contexts.snapshots:
            if snapshot.get("source") != "session_default" or not snapshot.get("resolved_sha"):
                continue
            self.store.update_session_git_context_resolution(
                session_id=session.session_id,
                user_id=user.user_id,
                repo_full_name=str(snapshot["repo_full_name"]),
                resolved_sha=str(snapshot["resolved_sha"]),
                checked_at=datetime.now(timezone.utc),
            )
        for update_message in prepared_git_contexts.update_messages:
            self.store.append_runtime_event(
                session.session_id,
                turn.turn_id,
                "activity",
                {
                    "kind": "git_context_updated",
                    "message": update_message,
                },
            )
        self._record_resolved_knowledge_scopes(state, message)
        state.code_guard_snapshots = await asyncio.to_thread(self.code_guard.snapshot)

        yield (
            {
                "type": "session",
                "session_id": session.session_id,
                "title": session.title,
                "created": session_created,
                "runtime_provider": session.runtime_provider or self.runtime_client.provider,
                "active_organization_key": session.active_organization_key,
            },
            state,
        )
        yield (
            {
                "type": "turn_start",
                "turn_id": turn.turn_id,
                "session_id": session.session_id,
                "started_at": turn.started_at.isoformat(),
                "git_scopes": prepared_git_contexts.snapshots,
            },
            state,
        )

        if state.visualization_request:
            progress_event = RuntimeEvent(
                "activity",
                {
                    "message": "已接收可视化请求，正在准备上下文。",
                    "kind": "visualization_progress",
                },
            )
            for event in map_runtime_event_to_sse(progress_event):
                yield event, state

        workspace_plan = self._resolve_runtime_workspace_plan(
            user=user,
            session=session,
            extra_readonly_mounts=[*prepared_git_contexts.mounts, *attachment_mounts],
        )
        dynamic_git_tools_enabled = (
            rerun_turn_id is None
            and self.git_context_resolver is not None
            and self.workspace_access is not None
        )
        organization_key = session.active_organization_key or ""
        test_data_tools_enabled = (
            rerun_turn_id is None
            and self.test_data_tools is not None
            and await self.test_data_tools.is_available_async(
                user=user,
                organization_key=organization_key,
                session_id=session.session_id,
                turn_id=turn.turn_id,
            )
        )
        doco_tools_enabled = (
            rerun_turn_id is None
            and self.doco_tools is not None
            and self.doco_tools.is_available(user=user, organization_key=organization_key)
        )
        system_prompt = self._build_system_prompt(
            session_mounts,
            workspace_plan=workspace_plan,
            git_dynamic_tool_enabled=dynamic_git_tools_enabled,
        )
        if test_data_tools_enabled and self.test_data_tools is not None:
            system_prompt = f"{system_prompt}\n\n{self.test_data_tools.system_prompt}"
        if doco_tools_enabled and self.doco_tools is not None:
            system_prompt = f"{system_prompt}\n\n{self.doco_tools.system_prompt}"
        runtime_session_ref: dict[str, RuntimeSession] = {}
        dynamic_tools: list[RuntimeDynamicTool] = []
        if dynamic_git_tools_enabled:
            dynamic_tools.extend(
                self._build_git_dynamic_tools(
                    user=user,
                    state=state,
                    runtime_session_ref=runtime_session_ref,
                )
            )
        if test_data_tools_enabled and self.test_data_tools is not None:
            dynamic_tools.extend(
                self.test_data_tools.build_tools(
                    user=user,
                    session_id=session.session_id,
                    turn_id=turn.turn_id,
                    organization_key=organization_key,
                    user_message=state.user_message.content,
                )
            )
        if doco_tools_enabled and self.doco_tools is not None:
            dynamic_tools.extend(
                self.doco_tools.build_tools(
                    user=user,
                    session_id=session.session_id,
                    turn_id=turn.turn_id,
                    organization_key=organization_key,
                    user_message=state.user_message.content,
                    workspace_plan=workspace_plan,
                )
            )
        runtime_session_kwargs = {
            "runtime_session_id": (
                None
                if state.isolated_runtime_session or state.rehydrate_runtime_session
                else session.runtime_session_id
            ),
            "working_directory": session.runtime_working_directory or self.runtime_working_directory,
            "system_prompt": system_prompt,
        }
        if dynamic_tools:
            runtime_session_kwargs["dynamic_tools"] = dynamic_tools
        if dynamic_git_tools_enabled and self.git_context_resolver is not None:
            runtime_session_kwargs["dynamic_read_roots"] = [
                str(self.git_context_resolver.worktree_manager.root)
            ]
        if workspace_plan is not None:
            runtime_session_kwargs["workspace_plan"] = workspace_plan
        runtime_session = await self.runtime_client.create_or_resume_session(**runtime_session_kwargs)
        runtime_session_ref["session"] = runtime_session
        if not state.isolated_runtime_session:
            updated_session = self.store.update_session_runtime(
                session.session_id,
                user.user_id,
                runtime_provider=runtime_session.provider,
                runtime_session_id=runtime_session.session_id,
                runtime_working_directory=runtime_session.working_directory,
                active_organization_key=workspace_plan.organization_key if workspace_plan else session.active_organization_key,
                runtime_workspace_profile=workspace_plan.to_dict() if workspace_plan else session.runtime_workspace_profile,
                status="active",
            )
            if updated_session is not None:
                state.session = updated_session

        if state.visualization_request:
            progress_event = RuntimeEvent(
                "activity",
                {
                    "message": "已准备运行环境，正在整理可视化输入。",
                    "kind": "visualization_progress",
                },
            )
            for event in map_runtime_event_to_sse(progress_event):
                yield event, state

        runtime_skills = None
        if any(item.source_type == "skill" for item in context_items):
            runtime_skills = await asyncio.to_thread(
                discover_project_skills,
                str(self._agent_working_directory() / ".claude" / "skills"),
            )

        runtime_message = self._build_runtime_message(
            message=message,
            session_mounts=session_mounts,
            context_items=context_items,
            runtime_skills=runtime_skills,
            retry_source_message=state.retry_source_message,
            workspace_plan=workspace_plan,
            attachment_sandbox_paths=attachment_sandbox_paths,
            image_generation_quota_instruction=self._image_generation_quota_instruction(
                state.session.user_id,
                message=state.retry_source_message or message,
            ),
            git_context_update_messages=prepared_git_contexts.update_messages,
            git_context_defaults=self.store.get_session_git_context_defaults(
                state.session.session_id,
                user.user_id,
            ),
            rehydration_history=(
                self._build_runtime_rehydration_history(
                    state.session,
                    before_message_id=state.user_message.message_id,
                )
                if state.rehydrate_runtime_session
                else None
            ),
        )
        local_image_paths = self._build_runtime_local_image_paths(
            context_items,
            attachment_sandbox_paths,
            workspace_plan,
        )
        initial_session_title = state.session.title
        pending_title_task = (
            asyncio.create_task(self._generate_session_title(message))
            if state.session_created and self.lightweight_llm_client is not None
            else None
        )

        pending_persisted_runtime_events: list[tuple[str, str, str, dict[str, object]]] = []

        try:
            if state.visualization_request:
                progress_event = RuntimeEvent(
                    "activity",
                    {
                        "message": "正在等待模型规划图形结构并生成可视化 Artifact。",
                        "kind": "visualization_progress",
                    },
                )
                for event in map_runtime_event_to_sse(progress_event):
                    yield event, state

            runtime_event_iterator = self.runtime_client.send_message_stream(
                session=runtime_session,
                message=runtime_message,
                metadata={
                    "system_prompt": system_prompt,
                    "isolated_runtime_session": state.isolated_runtime_session,
                    "session_mount_count": len(session_mounts),
                    "requested_context_count": len(context_items),
                    "organization_key": workspace_plan.organization_key if workspace_plan else session.active_organization_key,
                    "workspace_mount_count": len(workspace_plan.mounts) if workspace_plan else 0,
                    "local_image_paths": local_image_paths,
                    "mcp_approval_register": lambda details: self.mcp_approvals.request(
                        session.session_id, turn.turn_id,
                        self._sanitize_runtime_event_paths(state, RuntimeEvent("mcp_approval", details)).data,
                    ),
                    "mcp_approval_expire": self.mcp_approvals.expire,
                },
            ).__aiter__()
            pending_runtime_event = asyncio.create_task(runtime_event_iterator.__anext__())

            while pending_runtime_event is not None or pending_title_task is not None:
                pending_tasks = {
                    task
                    for task in (pending_runtime_event, pending_title_task)
                    if task is not None
                }
                done, _pending = await asyncio.wait(
                    pending_tasks,
                    timeout=self.runtime_idle_heartbeat_seconds,
                )
                if not done:
                    heartbeat_event = RuntimeEvent(
                        "activity",
                        {
                            "message": self._build_idle_heartbeat_message(state),
                            "kind": "idle_heartbeat",
                        },
                    )
                    for event in map_runtime_event_to_sse(heartbeat_event):
                        yield event, state
                    continue

                if pending_title_task is not None and pending_title_task in done:
                    generated_title = pending_title_task.result()
                    pending_title_task = None
                    refreshed_session = self._apply_generated_session_title(
                        user,
                        state,
                        generated_title,
                        initial_title=initial_session_title,
                    )
                    if refreshed_session is not None:
                        yield (
                            {
                                "type": "session",
                                "session_id": state.session.session_id,
                                "title": state.session.title,
                                "created": state.session_created,
                                "runtime_provider": state.session.runtime_provider or self.runtime_client.provider,
                                "active_organization_key": state.session.active_organization_key,
                            },
                            state,
                        )

                if pending_runtime_event is None or pending_runtime_event not in done:
                    continue
                try:
                    runtime_event = pending_runtime_event.result()
                except StopAsyncIteration:
                    pending_runtime_event = None
                    continue

                pending_runtime_event = asyncio.create_task(runtime_event_iterator.__anext__())
                runtime_event = self._sanitize_runtime_event_paths(state, runtime_event)
                self._queue_runtime_event_for_persistence(
                    pending_persisted_runtime_events,
                    session_id=state.session.session_id,
                    turn_id=turn.turn_id,
                    runtime_event=runtime_event,
                )
                for synthetic_event in self._consume_runtime_event(state, runtime_event):
                    yield synthetic_event, state
                while state.pending_git_scope_events:
                    yield state.pending_git_scope_events.pop(0), state
                previous_session_title = state.session.title
                refreshed_session = self._maybe_refresh_runtime_session(user, state, runtime_event)
                if refreshed_session is not None and refreshed_session.title != previous_session_title:
                    state.runtime_generated_title = state.session.title
                    yield (
                        {
                            "type": "session",
                            "session_id": state.session.session_id,
                            "title": state.session.title,
                            "created": state.session_created,
                            "runtime_provider": state.session.runtime_provider or self.runtime_client.provider,
                            "active_organization_key": state.session.active_organization_key,
                        },
                        state,
                    )
                for event in map_runtime_event_to_sse(runtime_event):
                    yield event, state
        except Exception as exc:
            self._queue_image_quota_event_for_persistence(
                state,
                pending_persisted_runtime_events,
            )
            self._queue_runtime_event_for_persistence(
                pending_persisted_runtime_events,
                session_id=state.session.session_id,
                turn_id=turn.turn_id,
                runtime_event=RuntimeEvent("error", {"message": str(exc)}),
            )
            self._flush_runtime_event_batch(pending_persisted_runtime_events)
            async for event, state in self._run_code_guard(state, pending_persisted_runtime_events):
                yield event, state
            turn_status = "completed" if self._has_completed_runtime_answer(state) else "failed"
            persisted_result = self._persist_turn_result(
                user,
                state,
                turn.turn_id,
                status=turn_status,
                error_message=str(exc) if turn_status == "failed" else f"Runtime 尾部异常：{exc}",
            )
            if persisted_result is not None:
                state.turn = persisted_result.turn
                state.assistant_message = persisted_result.assistant_message
                state.citations = persisted_result.citations

                if persisted_result.citations:
                    yield (
                        {
                            "type": "citations",
                            "citations": [
                                self._serialize_citation(citation) for citation in persisted_result.citations
                            ],
                        },
                        state,
                    )

                yield (
                    {
                        "type": "message",
                        "message": self._serialize_message_event(
                            persisted_result.assistant_message,
                            turn_id=turn.turn_id,
                        ),
                    },
                    state,
                )
                if turn_status == "completed":
                    yield (
                        {
                            "type": "complete",
                            "session_id": state.session.session_id,
                            "turn_id": turn.turn_id,
                        },
                        state,
                    )
                    return
            else:
                self.store.fail_turn(turn.turn_id, str(exc))
            raise AssistantRuntimeError(f"Runtime 执行失败: {exc}") from exc
        finally:
            self.mcp_approvals.expire_turn(turn.turn_id)
            if (
                "pending_runtime_event" in locals()
                and pending_runtime_event is not None
                and not pending_runtime_event.done()
            ):
                pending_runtime_event.cancel()
                with suppress(asyncio.CancelledError):
                    await pending_runtime_event
            if pending_title_task is not None and not pending_title_task.done():
                pending_title_task.cancel()
                with suppress(asyncio.CancelledError):
                    await pending_title_task
            self._queue_image_quota_event_for_persistence(
                state,
                pending_persisted_runtime_events,
            )
            if state.runtime_image_quota_event_persisted:
                self._flush_runtime_event_batch(pending_persisted_runtime_events)

        self._flush_runtime_event_batch(pending_persisted_runtime_events)
        async for event, state in self._run_code_guard(state, pending_persisted_runtime_events):
            yield event, state
        self._flush_runtime_event_batch(pending_persisted_runtime_events)
        persisted_result = self._persist_turn_result(user, state, turn.turn_id)
        assert persisted_result is not None

        state.turn = persisted_result.turn
        state.assistant_message = persisted_result.assistant_message
        state.citations = persisted_result.citations

        if persisted_result.citations:
            yield (
                {
                    "type": "citations",
                    "citations": [self._serialize_citation(citation) for citation in persisted_result.citations],
                },
                state,
            )

        yield (
            {
                "type": "message",
                "message": self._serialize_message_event(
                    persisted_result.assistant_message,
                    turn_id=turn.turn_id,
                ),
            },
            state,
        )
        yield (
            {
                "type": "complete",
                "session_id": state.session.session_id,
                "turn_id": turn.turn_id,
            },
            state,
        )

    def _attach_session_scopes(
        self,
        user: UserRecord,
        session: AssistantSessionRecord,
    ) -> AssistantSessionRecord:
        session.knowledge_scope_ids = self.store.list_session_knowledge_scope_ids(session.session_id, user.user_id)
        session.git_context_defaults = self.store.get_session_git_context_defaults(session.session_id, user.user_id)
        return session

    def _record_resolved_knowledge_scopes(self, state: TurnExecutionState, message: str) -> None:
        explicit_scopes = extract_explicit_knowledge_scopes(state.requested_contexts)
        scope_message = state.retry_source_message or message
        resolved_scope_inputs = build_scope_context_inputs(
            explicit_scopes or infer_knowledge_scopes_from_message(scope_message.strip()),
            working_directory=(
                state.session.active_organization_key
                if state.session.active_organization_key
                else self.runtime_working_directory
            ),
            explicit=bool(explicit_scopes),
        )
        if resolved_scope_inputs:
            state.context_usage_inputs.extend(resolved_scope_inputs)

    def _find_failed_turn_visualization_retry_source(
        self,
        user: UserRecord,
        session: AssistantSessionRecord,
        message: str,
    ) -> str | None:
        if not _is_manual_retry_message(message):
            return None

        for turn in reversed(self.store.list_turns(session.session_id, user.user_id)):
            if turn.status != "failed" or not self._turn_failed_from_output_token_limit(user, turn):
                continue
            user_message = self.store.get_message(turn.user_message_id, user.user_id)
            if user_message is not None and _is_turn_visualization_request(user_message.content):
                return user_message.content
            return None

        return None

    def _turn_failed_from_output_token_limit(
        self,
        user: UserRecord,
        turn: AssistantTurnRecord,
    ) -> bool:
        if _is_output_token_limit_failure(turn.error_message):
            return True

        if not turn.assistant_message_id:
            return False

        assistant_message = self.store.get_message(turn.assistant_message_id, user.user_id)
        return _is_output_token_limit_failure(assistant_message.content if assistant_message else None)

    def _resolve_session(
        self,
        user: UserRecord,
        session_id: str | None,
        first_message: str,
        *,
        organization_key: str | None = None,
    ) -> tuple[AssistantSessionRecord, bool]:
        if session_id:
            session = self.store.get_session(session_id, user.user_id)
            if session is None:
                raise ValueError("会话不存在或无权限访问。")
            active_organization_key = self._resolve_active_organization_key(user, organization_key, session=session)
            if session.active_organization_key is None and active_organization_key:
                updated_session = self.store.update_session_runtime(
                    session.session_id,
                    user.user_id,
                    runtime_provider=session.runtime_provider,
                    runtime_session_id=session.runtime_session_id,
                    runtime_working_directory=session.runtime_working_directory,
                    status=session.status,
                    active_organization_key=active_organization_key,
                )
                session = updated_session or session
            return session, False

        title = self._build_session_title(first_message)
        active_organization_key = self._resolve_active_organization_key(user, organization_key, session=None)
        return (
            self.store.create_session(
                user.user_id,
                title,
                runtime_provider=self.runtime_client.provider,
                runtime_working_directory=self.runtime_working_directory,
                active_organization_key=active_organization_key,
            ),
            True,
        )

    def _resolve_active_organization_key(
        self,
        user: UserRecord,
        organization_key: str | None,
        *,
        session: AssistantSessionRecord | None,
    ) -> str | None:
        requested_key = organization_key.strip().lower() if organization_key else None
        if session is not None and session.active_organization_key:
            if requested_key and requested_key != session.active_organization_key:
                raise ValueError("当前会话已绑定其他组织，不能在同一会话中切换组织。")
            return session.active_organization_key
        if self.workspace_access is None:
            return requested_key
        try:
            return self.workspace_access.resolve_active_organization(user, requested_key).organization_key
        except WorkspaceAccessError as exc:
            raise ValueError(str(exc)) from exc

    def _build_git_dynamic_tools(
        self,
        *,
        user: UserRecord,
        state: TurnExecutionState,
        runtime_session_ref: dict[str, RuntimeSession],
    ) -> list[RuntimeDynamicTool]:
        assert self.git_context_resolver is not None
        organization_key = state.session.active_organization_key or ""
        repositories = self.git_context_resolver.catalog.list_for_organization(organization_key)
        repository_names = "、".join(repository.repo_full_name for repository in repositories)
        description = (
            "获取并只读挂载当前 Turn 需要分析的 GitHub PR 或远程分支。"
            "仅在用户问题需要默认分支之外的精确代码版本时调用；可对多个仓库分别调用，"
            "同一仓库再次调用会切换到新的 PR/分支。"
        )
        if repository_names:
            description += f" 当前组织可挂载仓库：{repository_names}。"

        async def mount_git_ref(arguments: dict[str, object]) -> RuntimeDynamicToolResult:
            try:
                return await self._mount_git_ref_for_turn(
                    user=user,
                    state=state,
                    runtime_session_ref=runtime_session_ref,
                    repository_name=str(arguments.get("repository") or ""),
                    reference_type=str(arguments.get("reference_type") or ""),
                    reference=str(arguments.get("reference") or ""),
                )
            except Exception as exc:
                return RuntimeDynamicToolResult(
                    content=str(exc) or "Git reference 挂载失败。",
                    is_error=True,
                )

        return [
            RuntimeDynamicTool(
                name="mount_git_ref",
                description=description,
                input_schema={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["repository", "reference_type", "reference"],
                    "properties": {
                        "repository": {
                            "type": "string",
                            "description": "受控仓库的 owner/repo 或唯一仓库短名称。",
                        },
                        "reference_type": {
                            "type": "string",
                            "enum": ["pull_request", "branch"],
                            "description": "要挂载的是 GitHub PR 还是远程分支。",
                        },
                        "reference": {
                            "type": "string",
                            "description": "PR number（只填数字）或完整分支名。",
                        },
                    },
                },
                handler=mount_git_ref,
            )
        ]

    async def _mount_git_ref_for_turn(
        self,
        *,
        user: UserRecord,
        state: TurnExecutionState,
        runtime_session_ref: dict[str, RuntimeSession],
        repository_name: str,
        reference_type: str,
        reference: str,
    ) -> RuntimeDynamicToolResult:
        assert self.git_context_resolver is not None
        if self.workspace_access is None:
            raise ValueError("当前环境未配置 Runtime workspace，无法挂载 Git reference。")
        normalized_type = reference_type.strip()
        if normalized_type not in {"pull_request", "branch"}:
            raise ValueError("reference_type 只能是 pull_request 或 branch。")
        normalized_reference = reference.strip()
        if normalized_type == "pull_request":
            normalized_reference = normalized_reference.removeprefix("#").strip()
        if not normalized_reference:
            raise ValueError("Git reference 不能为空。")

        async with state.git_context_lock:
            repository = self.git_context_resolver.catalog.resolve_for_organization(
                organization_key=state.session.active_organization_key or "",
                repository=repository_name,
            )
            prepared = await self._prepare_git_contexts(
                user=user,
                session=state.session,
                explicit_requests=[
                    GitContextRequest(
                        repo_full_name=repository.repo_full_name,
                        strategy="follow",
                        selector_type=normalized_type,
                        selector_value=normalized_reference,
                        authorization_source="agent_tool",
                    )
                ],
                apply_session_defaults=False,
            )
            if len(prepared.requests) != 1 or len(prepared.snapshots) != 1:
                raise RuntimeError("Git Context resolver 未返回唯一挂载结果。")

            workspace_key_prefix = f"git:{repository.repo_full_name}:"
            previous_mounts = list(state.git_context_mounts)
            next_mounts = [
                mount
                for mount in previous_mounts
                if not mount.workspace_key.startswith(workspace_key_prefix)
            ]
            next_mounts.extend(prepared.mounts)
            next_plan = self._resolve_runtime_workspace_plan(
                user=user,
                session=state.session,
                extra_readonly_mounts=[*next_mounts, *state.attachment_mounts],
            )
            try:
                snapshots = self.store.update_running_turn_git_context(
                    turn_id=state.turn.turn_id,
                    user_id=user.user_id,
                    request=prepared.requests[0],
                    snapshot=prepared.snapshots[0],
                )
            except Exception:
                self._resolve_runtime_workspace_plan(
                    user=user,
                    session=state.session,
                    extra_readonly_mounts=[*previous_mounts, *state.attachment_mounts],
                )
                raise

            state.git_context_mounts = next_mounts
            runtime_session = runtime_session_ref.get("session")
            if runtime_session is not None:
                runtime_session.workspace_plan = next_plan
                updated_session = self.store.update_session_runtime(
                    state.session.session_id,
                    user.user_id,
                    runtime_provider=runtime_session.provider,
                    runtime_session_id=runtime_session.session_id,
                    runtime_working_directory=runtime_session.working_directory,
                    active_organization_key=next_plan.organization_key if next_plan else state.session.active_organization_key,
                    runtime_workspace_profile=next_plan.to_dict() if next_plan else state.session.runtime_workspace_profile,
                    status="active",
                )
                if updated_session is not None:
                    state.session = updated_session
            snapshot = prepared.snapshots[0]
            resolved_sha = str(snapshot.get("resolved_sha") or "")
            self.store.update_session_git_context_resolution(
                session_id=state.session.session_id,
                user_id=user.user_id,
                repo_full_name=repository.repo_full_name,
                resolved_sha=resolved_sha,
                checked_at=datetime.now(timezone.utc),
            )
            for update_message in prepared.update_messages:
                self.store.append_runtime_event(
                    state.session.session_id,
                    state.turn.turn_id,
                    "activity",
                    {
                        "kind": "git_context_updated",
                        "message": update_message,
                    },
                )
                state.pending_git_scope_events.append(
                    {
                        "type": "activity",
                        "message": update_message,
                        "activity_kind": "git_context_updated",
                    }
                )
            serialized_scopes = [self._serialize_git_scope(item) for item in snapshots]
            state.pending_git_scope_events.append(
                {
                    "type": "git_scope",
                    "turn_id": state.turn.turn_id,
                    "session_id": state.session.session_id,
                    "git_scopes": serialized_scopes,
                }
            )

            selector_label = (
                f"PR #{normalized_reference}"
                if normalized_type == "pull_request"
                else f"分支 {normalized_reference}"
            )
            lines = [
                f"已挂载 {repository.repo_full_name} {selector_label}。",
                f"- 代码：repos/{repository.repo_name}/code",
                f"- revision：{resolved_sha}",
                "当前 Turn Git Scope 已更新；后续分析请优先读取上述精确代码。",
            ]
            if snapshot.get("logical_context_path"):
                lines.insert(2, f"- PR context：repos/{repository.repo_name}/context")
            lines.extend(prepared.update_messages)
            return RuntimeDynamicToolResult(content="\n".join(lines))

    @staticmethod
    def _serialize_git_scope(snapshot: TurnGitContextSnapshotRecord) -> dict[str, object]:
        return {
            "repo_full_name": snapshot.repo_full_name,
            "strategy": snapshot.strategy,
            "selector_type": snapshot.selector_type,
            "selector_value": snapshot.selector_value,
            "resolved_sha": snapshot.resolved_sha,
            "logical_code_path": snapshot.logical_code_path,
            "logical_context_path": snapshot.logical_context_path,
            "source": snapshot.source,
            "resolved_at": snapshot.resolved_at.isoformat(),
        }

    def _build_turn_attachment_mounts(
        self,
        *,
        user: UserRecord,
        context_items: list[AssistantContextInput],
    ) -> tuple[list[RuntimeWorkspaceMount], dict[str, str]]:
        if self.workspace_access is None:
            return [], {}
        mounts_by_context_key: dict[str, RuntimeWorkspaceMount] = {}
        sandbox_paths: dict[str, str] = {}
        user_upload_root = self.uploaded_files_root / self._safe_path_segment(user.user_id)
        working_directory = Path(self.runtime_working_directory).resolve()

        for item in context_items:
            if item.source_type == "image" and item.context_key.startswith("uploaded-image:"):
                attachment_id = item.context_key.removeprefix("uploaded-image:")
                metadata_id = str(item.metadata.get("image_id") or "")
            elif item.source_type == "file" and item.context_key.startswith("uploaded-file:"):
                attachment_id = item.context_key.removeprefix("uploaded-file:")
                metadata_id = str(item.metadata.get("file_id") or "")
            else:
                continue

            if not attachment_id or metadata_id != attachment_id or not item.source_uri:
                raise ValueError("上传附件上下文无效，请重新上传后再试。")
            source_uri = Path(item.source_uri)
            if source_uri.is_absolute():
                raise ValueError("上传附件路径无效，请重新上传后再试。")
            host_path = (working_directory / source_uri).resolve()
            try:
                relative_path = host_path.relative_to(user_upload_root.resolve())
            except ValueError:
                raise ValueError("上传附件不属于当前用户或不在允许的上传目录内。") from None
            if (
                len(relative_path.parts) != 1
                or host_path.stem != attachment_id
                or not host_path.is_file()
            ):
                raise ValueError("上传附件不存在或路径无效，请重新上传后再试。")

            sandbox_path = f"/attachments/{host_path.name}"
            existing = mounts_by_context_key.get(item.context_key)
            if existing is not None and existing.host_path != str(host_path):
                raise ValueError("同一上传附件对应了多个文件路径。")
            mount = RuntimeWorkspaceMount(
                workspace_id=f"attachment_{attachment_id}",
                workspace_key=f"attachment:{attachment_id}",
                host_path=str(host_path),
                sandbox_path=sandbox_path,
                permission="read",
            )
            mounts_by_context_key[item.context_key] = mount
            sandbox_paths[item.context_key] = sandbox_path

        return list(mounts_by_context_key.values()), sandbox_paths

    def _ensure_runtime_supports_local_images(
        self,
        context_items: list[AssistantContextInput],
        attachment_sandbox_paths: dict[str, str],
    ) -> None:
        uploaded_images = [
            item
            for item in context_items
            if item.source_type == "image" and item.context_key.startswith("uploaded-image:")
        ]
        if not uploaded_images:
            return
        if any(item.context_key not in attachment_sandbox_paths for item in uploaded_images):
            raise AssistantRuntimeError("上传图片未进入 Runtime 工作区，无法作为原生视觉输入。")
        if not getattr(self.runtime_client, "supports_local_images", False):
            provider = str(getattr(self.runtime_client, "provider", "当前"))
            raise AssistantRuntimeError(
                f"{provider} Runtime 不支持原生图片输入，已拒绝仅传图片路径的降级处理。"
            )

    @staticmethod
    def _build_runtime_local_image_paths(
        context_items: list[AssistantContextInput],
        attachment_sandbox_paths: dict[str, str],
        workspace_plan: RuntimeWorkspacePlan | None,
    ) -> list[str]:
        sandbox_paths = [
            attachment_sandbox_paths[item.context_key]
            for item in context_items
            if item.source_type == "image" and item.context_key in attachment_sandbox_paths
        ]
        if not sandbox_paths:
            return []
        if workspace_plan is None:
            raise AssistantRuntimeError("上传图片未进入 Runtime 工作区，无法作为原生视觉输入。")

        shadow_root = Path(workspace_plan.host_shadow_root)
        if not shadow_root.is_absolute():
            raise AssistantRuntimeError("Runtime 工作区路径不是绝对路径，无法传入图片。")

        image_paths: list[str] = []
        for sandbox_path in dict.fromkeys(sandbox_paths):
            image_path = shadow_root / sandbox_path.removeprefix("/")
            if not image_path.is_file():
                raise AssistantRuntimeError(f"上传图片未正确挂载到 Runtime 工作区：{sandbox_path}")
            image_paths.append(str(image_path))
        return image_paths

    def _resolve_runtime_workspace_plan(
        self,
        *,
        user: UserRecord,
        session: AssistantSessionRecord,
        extra_readonly_mounts: list[RuntimeWorkspaceMount] | None = None,
    ) -> RuntimeWorkspacePlan | None:
        if self.workspace_access is None:
            return None
        try:
            return self.workspace_access.resolve_runtime_plan(
                user=user,
                organization_key=session.active_organization_key,
                session_id=session.session_id,
                extra_readonly_mounts=extra_readonly_mounts,
            )
        except WorkspaceAccessError as exc:
            raise ValueError(str(exc)) from exc

    def _build_session_title(self, message: str) -> str:
        stripped = " ".join(message.strip().split())
        if not stripped:
            return "新会话"
        return self._normalize_title(stripped[:24])

    async def _generate_session_title(self, first_message: str) -> str | None:
        assert self.lightweight_llm_client is not None
        try:
            reply = await self.lightweight_llm_client.generate_reply(
                "你是会话标题生成器。根据用户的首条消息生成简洁、准确的中文标题。"
                "只输出标题，不加引号、前缀或句末标点，不超过 20 个字。",
                [LLMMessage(role="user", content=first_message)],
            )
        except Exception as exc:
            logger.warning("轻量模型生成会话标题失败: %s", exc)
            return None

        first_line = next((line.strip() for line in reply.splitlines() if line.strip()), "")
        normalized = self._normalize_title(first_line.strip("\"'“”‘’`*# "))
        return normalized if normalized != "新会话" else None

    def _apply_generated_session_title(
        self,
        user: UserRecord,
        state: TurnExecutionState,
        generated_title: str | None,
        *,
        initial_title: str,
    ) -> AssistantSessionRecord | None:
        if not generated_title:
            return None
        current_session = self.store.get_session(state.session.session_id, user.user_id)
        if current_session is None or current_session.title not in {
            initial_title,
            state.runtime_generated_title,
        }:
            return None
        if current_session.title == generated_title:
            return None
        renamed_session = self.store.rename_session(
            current_session.session_id,
            user.user_id,
            generated_title,
        )
        if renamed_session is not None:
            state.session = self._attach_session_scopes(user, renamed_session)
        return renamed_session

    def _normalize_title(self, title: str) -> str:
        normalized = " ".join(title.strip().split())
        return normalized[:40] or "新会话"

    def _build_system_prompt(
        self,
        session_mounts: list[SessionContextMountRecord],
        *,
        workspace_plan: RuntimeWorkspacePlan | None = None,
        git_dynamic_tool_enabled: bool = False,
    ) -> str:
        return build_assistant_system_prompt(
            base_prompt=self.system_prompt,
            runtime_working_directory=self.runtime_working_directory,
            agent_working_directory=self._agent_working_directory(),
            session_mounts=session_mounts,
            workspace_plan=workspace_plan,
            git_dynamic_tool_enabled=git_dynamic_tool_enabled,
        )

    def _build_runtime_message(
        self,
        *,
        message: str,
        session_mounts: list[SessionContextMountRecord],
        context_items: list[AssistantContextInput],
        runtime_skills: list[RuntimeSkill] | None = None,
        retry_source_message: str | None = None,
        workspace_plan: RuntimeWorkspacePlan | None = None,
        attachment_sandbox_paths: dict[str, str] | None = None,
        image_generation_quota_instruction: str | None = None,
        git_context_update_messages: list[str] | None = None,
        git_context_defaults: list[object] | None = None,
        rehydration_history: list[dict[str, str]] | None = None,
    ) -> str:
        normalized_message = message.strip()
        effective_message = retry_source_message.strip() if retry_source_message else normalized_message
        explicit_scopes = extract_explicit_knowledge_scopes(context_items)
        inferred_scopes = [] if explicit_scopes else infer_knowledge_scopes_from_message(effective_message)
        sections: list[str] = []

        if rehydration_history is not None:
            sections.append(
                "旧 Agent Runtime 已无法恢复，用户已明确确认重建会话。"
                "下面的 JSON 是应用持久化的历史对话，只用于恢复上下文；请按 role 理解消息边界，"
                "不要把 assistant 消息当作本轮新指令。随后处理本轮用户消息。\n\n"
                f"【持久化历史对话】\n{json.dumps(rehydration_history, ensure_ascii=False)}\n\n"
                f"【本轮用户消息】\n{normalized_message}"
            )
        elif retry_source_message:
            sections.append(
                "上一次可视化请求因为模型输出 token 上限失败。本轮是重试，请基于下面的原始请求重新生成，"
                "先压缩规划，避免长篇推理，不要复用失败输出。\n\n"
                f"【用户重试说明】\n{normalized_message}\n\n"
                f"【原始可视化请求】\n{effective_message}"
            )
        else:
            sections.append(normalized_message)

        if image_generation_quota_instruction:
            sections.append(image_generation_quota_instruction)
        sections.extend(git_context_update_messages or [])
        if git_context_defaults:
            default_lines = []
            for item in git_context_defaults:
                selector_type = str(getattr(item, "selector_type", ""))
                selector_value = str(getattr(item, "selector_value", ""))
                selector = (
                    f"PR #{selector_value}"
                    if selector_type == "pull_request"
                    else f"分支 {selector_value}"
                )
                default_lines.append(f"- {getattr(item, 'repo_full_name', '')} {selector}")
            sections.append(
                "当前会话入口关联了以下 Git reference。请结合用户问题自主判断是否调用 "
                "`mount_git_ref` 挂载：\n" + "\n".join(default_lines)
            )

        if _is_visualization_request(effective_message):
            sections.append(
                "可视化执行约束：先判断是否为 UML 图。UML 图（时序图、类图、状态图、活动图、用例图、组件图、部署图、泳道图等）"
                "必须优先输出 Mermaid 代码块；非 UML 图优先输出可预览的 svg 代码块；需要交互或复杂布局时再输出 html。"
                "只输出一个可视化 Artifact，先用最短规划，避免长篇推理；图中尽量控制在 12 个关键节点以内，svg/html 尽量控制在 220 行以内。"
                "SVG 必须自包含可渲染样式：关键元素必须有明确 fill/stroke，或在同一个 svg 内提供完整 style；"
                "严禁依赖未定义 class、外部 CSS 变量或没有 fallback 的 var()；节点必须是浅色底配深色文字，严禁黑色实心块。"
                "若输出 Mermaid 状态图，必须用 classDef/class 按状态语义区分颜色：普通/处理中优先 purple、teal、coral、pink，"
                "开始/结束用 gray，成功用 green，风险/错误用 amber 或 red；不要按顺序彩虹轮换。"
            )
        direct_reference_lines: list[str] = []

        if session_mounts:
            mount_lines = [
                f"- {mount.label}" + (f"：{mount.content}" if mount.content else "")
                for mount in session_mounts
            ]
            sections.append("当前会话范围：\n" + "\n".join(mount_lines))

        if context_items:
            skill_items = [item for item in context_items if item.source_type == "skill"]
            general_items = [
                item
                for item in context_items
                if item.source_type not in {"skill", "knowledge_scope"}
            ]
            if general_items:
                request_lines = [
                    self._format_runtime_context_item(
                        item,
                        attachment_sandbox_paths=attachment_sandbox_paths,
                    )
                    for item in general_items
                ]
                sections.append("本轮指定上下文：\n" + "\n".join(request_lines))
            skill_lines = self._build_enabled_skill_lines(skill_items, runtime_skills)
            if skill_lines:
                sections.append("本轮指定启用 Skill：\n" + "\n".join(skill_lines))
            direct_reference_lines = [
                f"- {item.label}：`{self._runtime_context_uri(item, attachment_sandbox_paths)}`"
                for item in general_items
                if item.source_type != "knowledge_scope"
                and self._runtime_context_uri(item, attachment_sandbox_paths)
            ]
            image_lines = [
                f"- {item.label}：`{self._runtime_context_uri(item, attachment_sandbox_paths)}`"
                for item in general_items
                if item.source_type == "image"
                and self._runtime_context_uri(item, attachment_sandbox_paths)
            ]
            if image_lines:
                sections.append(
                    "本轮上传图片：\n"
                    + "\n".join(image_lines)
                    + "\n这些图片已保存到项目工作区内。请优先读取或查看图片文件本身，再结合用户问题回答。"
                )
            uploaded_file_lines = [
                f"- {item.label}：`{self._runtime_context_uri(item, attachment_sandbox_paths)}`"
                for item in general_items
                if item.source_type == "file"
                and str(item.context_key).startswith("uploaded-file:")
                and self._runtime_context_uri(item, attachment_sandbox_paths)
            ]
            if uploaded_file_lines:
                sections.append(
                    "本轮上传文件：\n"
                    + "\n".join(uploaded_file_lines)
                    + "\n这些文件已保存到项目工作区内。请优先直接读取文件内容，再结合用户问题回答。"
                )

        instruction_working_directory = (
            workspace_plan.organization_key
            if workspace_plan is not None
            else str(self._agent_working_directory())
        )
        host_agent_dir = (
            Path(workspace_plan.host_shadow_root).resolve()
            if workspace_plan is not None
            else self._agent_working_directory()
        )
        docs_dir = (
            host_agent_dir / workspace_plan.organization_key / "knowledge" / "requirements" / "docs"
            if workspace_plan is not None
            else host_agent_dir / "knowledge" / "requirements" / "docs"
        )
        resolved_links = resolve_clickup_doc_links(effective_message, docs_dir, host_agent_dir)
        if resolved_links:
            link_lines = [f"- {link.url} → `{link.local_path}`" for link in resolved_links]
            sections.append(
                "ClickUp 链接已解析为本地文件：\n" + "\n".join(link_lines)
            )
            direct_reference_lines.extend(
                f"- ClickUp 文档：`{link.local_path}`" for link in resolved_links
            )

        sections.append(
            build_knowledge_scope_instruction(
                working_directory=instruction_working_directory,
                explicit_scopes=explicit_scopes,
                inferred_scopes=inferred_scopes,
            )
        )
        if direct_reference_lines:
            sections.append(
                "本轮已明确指定具体引用对象：\n"
                + "\n".join(direct_reference_lines)
                + "\n对这些对象必须优先直接读取对应路径，不要为了定位这些已指定对象再搜索；只有需要补充相关材料时才搜索。"
            )

        return "\n\n".join(section for section in sections if section.strip())

    def _build_runtime_rehydration_history(
        self,
        session: AssistantSessionRecord,
        *,
        before_message_id: str,
    ) -> list[dict[str, str]]:
        history: list[dict[str, str]] = []
        for message in self.store.list_messages_by_session_id(session.session_id, session.user_id):
            if message.message_id == before_message_id:
                break
            history.append({"role": message.role, "content": message.content})
        return history

    def _build_enabled_skill_lines(
        self,
        skill_items: list[AssistantContextInput],
        runtime_skills: list[RuntimeSkill] | None,
    ) -> list[str]:
        lines: list[str] = []
        seen_skill_ids: set[str] = set()
        for item in skill_items:
            resolved = self._resolve_runtime_skill(item, runtime_skills or [])
            if resolved is None:
                logger.info("本轮指定的 Skill 未找到，已跳过：%s", item.context_key)
                continue
            if resolved.skill_id in seen_skill_ids:
                continue
            seen_skill_ids.add(resolved.skill_id)
            lines.append(
                f"- {resolved.name}：本轮任务必须按该 Skill 执行。先完整阅读 "
                f"`.agents/skills/{resolved.name}/SKILL.md`，"
                "再严格按其中的流程与输出结构完成本轮任务。"
            )
        return lines

    @staticmethod
    def _resolve_runtime_skill(
        item: AssistantContextInput,
        skills: list[RuntimeSkill],
    ) -> RuntimeSkill | None:
        candidates: list[str] = []
        metadata_skill_id = str(item.metadata.get("skill_id") or "").strip()
        if metadata_skill_id:
            candidates.append(metadata_skill_id)
        context_key = str(item.context_key or "").strip()
        if context_key.startswith("at-skill:"):
            candidates.append(context_key.removeprefix("at-skill:").strip())
        label = str(item.label or "").strip().lstrip("@").strip()
        if label:
            candidates.append(label)
        candidates = [candidate for candidate in candidates if candidate]
        if not candidates or not skills:
            return None
        for candidate in candidates:
            for skill in skills:
                if skill.skill_id == candidate:
                    return skill
        for candidate in candidates:
            for skill in skills:
                if skill.name == candidate:
                    return skill
        return None

    def _format_runtime_context_item(
        self,
        item: AssistantContextInput,
        *,
        attachment_sandbox_paths: dict[str, str] | None = None,
    ) -> str:
        segments = [f"- {item.label}"]
        uri = self._runtime_context_uri(item, attachment_sandbox_paths)
        if uri:
            segments.append(f"路径：`{uri}`")
        if item.source_type in {"image", "file"}:
            mime_type = item.metadata.get("mime_type")
            size_bytes = item.metadata.get("size_bytes")
            if mime_type:
                segments.append(f"类型：{mime_type}")
            if isinstance(size_bytes, int):
                segments.append(f"大小：{size_bytes} bytes")
        if item.content:
            segments.append(f"说明：{item.content}")
        return "；".join(segments)

    def _runtime_context_uri(
        self,
        item: AssistantContextInput,
        attachment_sandbox_paths: dict[str, str] | None = None,
    ) -> str | None:
        attachment_path = (attachment_sandbox_paths or {}).get(item.context_key)
        if attachment_path:
            return attachment_path
        if not item.source_uri:
            return None

        source_uri = item.source_uri.strip()
        if not source_uri:
            return None

        path = Path(source_uri)
        if path.is_absolute():
            workspace_path = self._agent_working_directory()
            try:
                return path.resolve().relative_to(workspace_path).as_posix()
            except ValueError:
                return source_uri
        if source_uri.startswith("workspace/"):
            return self._workspace_uri_to_sandbox_uri(source_uri)
        return source_uri

    def _workspace_uri_to_sandbox_uri(self, source_uri: str) -> str:
        normalized = source_uri.strip().replace("\\", "/")
        if normalized.startswith("workspace/"):
            normalized = normalized.removeprefix("workspace/")
        if normalized.startswith("users/"):
            user_relative = normalized.removeprefix("users/")
            return "/me" if "/" not in user_relative else f"/me/{user_relative.split('/', 1)[1]}"
        if normalized.startswith("runtime/containers/"):
            parts = normalized.split("/", 3)
            return "/tmp" if len(parts) < 4 else f"/tmp/{parts[3].removeprefix('tmp/').lstrip('/')}"
        if "/" in normalized:
            first, rest = normalized.split("/", 1)
            if rest.startswith("knowledge/"):
                return f"/{first}/{rest}"
        return source_uri

    def _agent_working_directory(self) -> Path:
        base_path = Path(self.runtime_working_directory).resolve()
        if base_path.name == "workspace":
            return base_path
        return base_path / "workspace"

    def _consume_runtime_event(
        self,
        state: TurnExecutionState,
        runtime_event: RuntimeEvent,
    ) -> list[dict[str, object]]:
        if runtime_event.type == "delta":
            text = str(runtime_event.data.get("text", ""))
            if text:
                state.runtime_reply_chunks.append(text)
            return []

        if runtime_event.type == "message":
            content = str(runtime_event.data.get("content", "")).strip()
            if content:
                state.runtime_final_message = content
            return []

        if runtime_event.type == "complete":
            state.runtime_completed = True
            result = str(runtime_event.data.get("result") or "").strip()
            if result:
                state.runtime_final_message = result
            return []

        if runtime_event.type == "image":
            quota_status = self._image_generation_quota_status(state.session.user_id)
            if quota_status is not None:
                state.runtime_images_blocked += 1
                state.runtime_image_quota_status = quota_status
                return [
                    {
                        "type": "activity",
                        "message": self._image_generation_quota_message(state),
                        "activity_kind": "image_quota",
                    }
                ]
            image_block = self._persist_runtime_generated_image(state, runtime_event.data)
            if not image_block:
                return []
            self.store.record_image_generation_usage(
                user_id=state.session.user_id,
                turn_id=state.turn.turn_id,
                item_id=str(runtime_event.data.get("item_id") or ""),
            )
            state.runtime_image_blocks.append(image_block)
            state.runtime_images_accepted += 1
            events: list[dict[str, object]] = [
                {"type": "delta", "delta": f"\n\n{image_block}"}
            ]
            quota_status = self._image_generation_quota_status(state.session.user_id)
            if quota_status is not None:
                state.runtime_image_quota_status = quota_status
                events.append(
                    {
                        "type": "activity",
                        "message": self._image_generation_quota_message(state),
                        "activity_kind": "image_quota",
                    }
                )
            return events

        if runtime_event.type == "usage":
            state.runtime_usage_events.append(runtime_event.data)

        usage_inputs = extract_context_usage_from_event(runtime_event)
        if usage_inputs:
            state.context_usage_inputs.extend(usage_inputs)
        return []

    def _sanitize_runtime_event_paths(self, state: TurnExecutionState, runtime_event: RuntimeEvent) -> RuntimeEvent:
        profile = state.session.runtime_workspace_profile
        if not profile:
            return runtime_event
        workspace_plan = self._runtime_event_workspace_plan(state)
        if workspace_plan is None:
            return runtime_event
        return RuntimeEvent(runtime_event.type, self._sanitize_runtime_event_value(runtime_event.data, workspace_plan))

    def _runtime_event_workspace_plan(self, state: TurnExecutionState) -> RuntimeWorkspacePlanForSanitize | None:
        profile = state.session.runtime_workspace_profile
        if not profile:
            return None
        session_id = str(profile.get("session_id") or state.session.session_id)
        host_shadow_root = self._host_shadow_root_for_session(session_id)
        mounts: list[object] = []
        for raw_mount in profile.get("mounts", []) if isinstance(profile.get("mounts"), list) else []:
            if isinstance(raw_mount, dict):
                mounts.append(raw_mount)
        if not mounts:
            return None
        user_id = str(profile.get("user_id") or "")
        runtime_mounts = []
        for mount in mounts:
            sandbox_path = str(mount.get("sandbox_path") or "")
            workspace_key = str(mount.get("workspace_key") or "")
            host_path = self._host_path_for_sandbox_mount(session_id, sandbox_path, user_id=user_id)
            if not sandbox_path or host_path is None:
                continue
            runtime_mounts.append(
                RuntimeWorkspaceMountForSanitize(
                    workspace_key=workspace_key,
                    host_path=host_path,
                    sandbox_path=sandbox_path,
                )
            )
        if not runtime_mounts:
            return None
        return RuntimeWorkspacePlanForSanitize(
            host_shadow_root=host_shadow_root,
            mounts=runtime_mounts,
        )

    def _host_shadow_root_for_session(self, session_id: str) -> str:
        return str(
            (Path(self.runtime_working_directory).resolve() / "workspace/runtime/sessions" / session_id / "root").resolve()
        )

    def _host_path_for_sandbox_mount(self, session_id: str, sandbox_path: str, *, user_id: str) -> str | None:
        base_dir = Path(self.runtime_working_directory).resolve()
        clean_path = sandbox_path.strip("/")
        if not clean_path:
            return None
        if clean_path == "me" and user_id:
            return str((base_dir / "workspace/users" / user_id).resolve())
        if clean_path == "tmp":
            return str((base_dir / "workspace/runtime/containers" / session_id / "tmp").resolve())
        parts = clean_path.split("/")
        if len(parts) >= 2 and parts[1] == "knowledge":
            return str((base_dir / "workspace" / parts[0] / "knowledge").resolve())
        return None

    def _sanitize_runtime_event_value(self, value: object, workspace_plan: object) -> object:
        if isinstance(value, str):
            return self._sandbox_path_from_runtime_string(value, workspace_plan)
        if isinstance(value, dict):
            return {key: self._sanitize_runtime_event_value(item, workspace_plan) for key, item in value.items()}
        if isinstance(value, list):
            return [self._sanitize_runtime_event_value(item, workspace_plan) for item in value]
        return value

    def _sandbox_path_from_runtime_string(self, value: str, workspace_plan: object) -> str:
        result = value
        host_shadow_root = Path(workspace_plan.host_shadow_root).resolve()
        replacements: list[tuple[str, str]] = [(str(host_shadow_root), "/")]
        for mount in workspace_plan.mounts:
            replacements.append((str(Path(mount.host_path).resolve()), mount.sandbox_path))
        for host_path, sandbox_path in sorted(replacements, key=lambda item: len(item[0]), reverse=True):
            if host_path in result:
                result = result.replace(host_path, sandbox_path.rstrip("/") or "/")
        return result

    def _maybe_refresh_runtime_session(
        self,
        user: UserRecord,
        state: TurnExecutionState,
        runtime_event: RuntimeEvent,
    ) -> AssistantSessionRecord | None:
        if state.isolated_runtime_session:
            return None

        runtime_session_id = runtime_event.data.get("session_id")
        normalized_runtime_session_id = (
            str(runtime_session_id) if runtime_session_id is not None else state.session.runtime_session_id
        )
        runtime_session_title = self._runtime_session_title_from_event(runtime_event)
        if not self._should_apply_runtime_session_title(state, runtime_session_title):
            runtime_session_title = None

        if normalized_runtime_session_id == state.session.runtime_session_id and runtime_session_title is None:
            return None

        updated_session = self.store.update_session_runtime(
            state.session.session_id,
            user.user_id,
            runtime_provider=state.session.runtime_provider or self.runtime_client.provider,
            runtime_session_id=normalized_runtime_session_id,
            runtime_working_directory=state.session.runtime_working_directory or self.runtime_working_directory,
            title=runtime_session_title,
            active_organization_key=state.session.active_organization_key,
            runtime_workspace_profile=state.session.runtime_workspace_profile,
            status=state.session.status,
        )
        if updated_session is not None:
            state.session = updated_session
        return updated_session

    def _runtime_session_title_from_event(self, runtime_event: RuntimeEvent) -> str | None:
        if runtime_event.type != "session":
            return None
        raw_title = runtime_event.data.get("title")
        if raw_title is None:
            return None
        normalized = " ".join(str(raw_title).strip().split())
        if not normalized:
            return None
        return self._normalize_title(normalized)

    def _should_apply_runtime_session_title(self, state: TurnExecutionState, title: str | None) -> bool:
        if not title or title == state.session.title:
            return False
        default_title = self._build_session_title(state.user_message.content)
        return state.session.title in {"新会话", default_title}

    def _build_idle_heartbeat_message(self, state: TurnExecutionState) -> str:
        if any(item.record["turn_id"] == state.turn.turn_id for item in self.mcp_approvals.pending.values()):
            return "正在等待你确认工具操作，请选择批准本次调用或拒绝。"
        if state.visualization_request:
            if state.runtime_reply_chunks:
                return "正在生成可视化内容，请稍候。"
            if state.context_usage_inputs:
                return "已读取上下文，正在规划可视化结构。"
            return "正在规划可视化结构，请稍候。"
        if state.runtime_reply_chunks:
            return "正在继续生成回答，请稍候。"
        if state.context_usage_inputs:
            return "已读取上下文，正在整理回答。"
        return "正在处理请求，请稍候。"

    async def _run_code_guard(
        self,
        state: TurnExecutionState,
        pending_events: list[tuple[str, str, str, dict[str, object]]],
    ) -> AsyncIterator[tuple[dict[str, object], TurnExecutionState]]:
        if not state.code_guard_snapshots:
            return

        report = await asyncio.to_thread(
            self.code_guard.rollback_changes,
            snapshots=state.code_guard_snapshots,
            turn_id=state.turn.turn_id,
        )
        if not report.should_notify:
            return

        runtime_event = RuntimeEvent("workspace_guard", report.to_event_data())
        self._queue_runtime_event_for_persistence(
            pending_events,
            session_id=state.session.session_id,
            turn_id=state.turn.turn_id,
            runtime_event=runtime_event,
        )
        for event in map_runtime_event_to_sse(runtime_event):
            yield event, state

    def _queue_runtime_event_for_persistence(
        self,
        pending_events: list[tuple[str, str, str, dict[str, object]]],
        *,
        session_id: str,
        turn_id: str,
        runtime_event: RuntimeEvent,
    ) -> None:
        if not self._should_persist_runtime_event(runtime_event):
            return
        pending_events.append((session_id, turn_id, runtime_event.type, runtime_event.data))
        if len(pending_events) >= self.runtime_event_persist_batch_size:
            self._flush_runtime_event_batch(pending_events)

    def _flush_runtime_event_batch(
        self,
        pending_events: list[tuple[str, str, str, dict[str, object]]],
    ) -> None:
        if not pending_events:
            return
        self.store.append_runtime_events(list(pending_events))
        pending_events.clear()

    def _should_persist_runtime_event(self, runtime_event: RuntimeEvent) -> bool:
        if runtime_event.type == "delta":
            return False
        if runtime_event.type == "activity":
            return runtime_event.data.get("kind") != "idle_heartbeat"
        return runtime_event.type in self.runtime_event_persist_types

    def _finalize_runtime_reply(self, state: TurnExecutionState) -> str:
        text = (state.runtime_final_message or "").strip()
        if not text:
            text = "".join(state.runtime_reply_chunks).strip()
        if not text and state.runtime_images_blocked:
            text = self._image_generation_quota_message(state)
        image_blocks = [block.strip() for block in state.runtime_image_blocks if block.strip()]
        parts = [part for part in [text, *image_blocks] if part]
        if parts:
            return "\n\n".join(parts)
        raise AssistantRuntimeError("Runtime 未返回最终文本内容。")

    def _has_completed_runtime_answer(self, state: TurnExecutionState) -> bool:
        if not state.runtime_completed:
            return False
        try:
            self._finalize_runtime_reply(state)
        except AssistantRuntimeError:
            return False
        return True

    def _persist_turn_result(
        self,
        user: UserRecord,
        state: TurnExecutionState,
        turn_id: str,
        *,
        status: str = "completed",
        error_message: str | None = None,
    ) -> PersistedTurnResult | None:
        try:
            reply = self._finalize_runtime_reply(state)
        except AssistantRuntimeError:
            return None

        assistant_message = self.store.append_message(
            state.session.session_id,
            user.user_id,
            "assistant",
            reply,
        )
        completed_turn = self.store.complete_turn(
            turn_id,
            assistant_message_id=assistant_message.message_id,
            status=status,
            error_message=error_message,
        )
        usage_records = self.store.record_turn_context_usage(
            turn_id,
            dedupe_context_inputs(state.context_usage_inputs),
        )
        citations = build_answer_citations(
            turn_id=turn_id,
            assistant_message_id=assistant_message.message_id,
            usage_records=usage_records,
        )
        stored_citations = self.store.replace_answer_citations(turn_id, assistant_message.message_id, citations)
        self.ensure_session_file_artifacts_by_session(state.session)
        return PersistedTurnResult(
            turn=completed_turn,
            assistant_message=assistant_message,
            citations=stored_citations,
        )

    def _extract_personal_workspace_paths(self, text: str) -> list[str]:
        if not text:
            return []
        candidates: list[str] = []
        for pattern in (_MARKDOWN_LINK_TARGET, _INLINE_CODE_TEXT, _PERSONAL_WORKSPACE_PATH):
            for match in pattern.finditer(text):
                candidates.append(match.group("target").strip())

        result: list[str] = []
        seen: set[str] = set()
        for candidate in candidates:
            normalized = self._normalize_personal_workspace_display_path(candidate)
            if normalized is None or normalized in seen:
                continue
            seen.add(normalized)
            result.append(normalized)
        return result

    def _normalize_personal_workspace_display_path(self, value: str) -> str | None:
        normalized = unquote(str(value or "").strip()).replace("\\", "/")
        if not normalized:
            return None
        if normalized.startswith("sandbox:"):
            normalized = normalized.removeprefix("sandbox:")
        query_index = min([index for index in [normalized.find("?"), normalized.find("#")] if index >= 0] or [-1])
        if query_index >= 0:
            normalized = normalized[:query_index]
        normalized = normalized.strip().strip("/")
        if not normalized or ".." in normalized.split("/"):
            return None
        if normalized.startswith("me/"):
            return normalized
        if normalized.startswith("workspace/users/"):
            return normalized
        return None

    def _image_generation_quota_status(self, user_id: str) -> ImageGenerationQuotaStatus | None:
        """返回当前已耗尽的额度窗口；额度未耗尽时返回 None。"""
        daily_limit = self.image_generation_daily_limit
        weekly_limit = self.image_generation_weekly_limit
        if daily_limit <= 0 and weekly_limit <= 0:
            return None
        now = datetime.now(timezone.utc)
        if daily_limit > 0:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            daily_used = self.store.count_image_generation_usage(user_id=user_id, since=day_start)
            if daily_used >= daily_limit:
                return ImageGenerationQuotaStatus(
                    window="daily",
                    used=daily_used,
                    limit=daily_limit,
                    resets_at=day_start + timedelta(days=1),
                )
        if weekly_limit > 0:
            week_start = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            weekly_used = self.store.count_image_generation_usage(user_id=user_id, since=week_start)
            if weekly_used >= weekly_limit:
                return ImageGenerationQuotaStatus(
                    window="weekly",
                    used=weekly_used,
                    limit=weekly_limit,
                    resets_at=week_start + timedelta(days=7),
                )
        return None

    def _image_generation_quota_instruction(self, user_id: str, *, message: str) -> str | None:
        """仅在当前问题涉及生图时，把剩余硬额度告知模型。"""
        if not _IMAGE_GENERATION_REQUEST.search(message):
            return None

        now = datetime.now(timezone.utc)
        remaining: list[int] = []
        if self.image_generation_daily_limit > 0:
            day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
            daily_used = self.store.count_image_generation_usage(user_id=user_id, since=day_start)
            remaining.append(max(0, self.image_generation_daily_limit - daily_used))
        if self.image_generation_weekly_limit > 0:
            week_start = (now - timedelta(days=now.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            weekly_used = self.store.count_image_generation_usage(user_id=user_id, since=week_start)
            remaining.append(max(0, self.image_generation_weekly_limit - weekly_used))
        if not remaining:
            return None
        available = min(remaining)
        return (
            "平台生图额度约束：当前最多还能接收 "
            f"{available} 张新生成图片。不得调用生图工具超过 {available} 次；"
            "如果用户要求的数量更多，只生成额度允许的数量，并在最终回答中如实说明实际生成数量。"
        )

    def _image_generation_quota_message(self, state: TurnExecutionState) -> str:
        status = state.runtime_image_quota_status
        assert status is not None
        window_label = "今日" if status.window == "daily" else "本周"
        if state.runtime_images_accepted:
            result = f"本轮已保留 {state.runtime_images_accepted} 张"
        else:
            result = "本轮未新增图片"
        blocked = (
            f"；另 {state.runtime_images_blocked} 张因达到{window_label}额度上限未加入会话"
            if state.runtime_images_blocked
            else ""
        )
        return (
            f"生图额度提示：{result}{blocked}。"
            f"{window_label}生图额度已用完（{status.used}/{status.limit} 张）。"
            + ("本轮无法继续生成更多图片。" if not state.runtime_images_blocked else "")
        )

    def _queue_image_quota_event_for_persistence(
        self,
        state: TurnExecutionState,
        pending_events: list[tuple[str, str, str, dict[str, object]]],
    ) -> None:
        status = state.runtime_image_quota_status
        if (
            status is None
            or (state.runtime_images_accepted <= 0 and state.runtime_images_blocked <= 0)
            or state.runtime_image_quota_event_persisted
        ):
            return
        state.runtime_image_quota_event_persisted = True
        self._queue_runtime_event_for_persistence(
            pending_events,
            session_id=state.session.session_id,
            turn_id=state.turn.turn_id,
            runtime_event=RuntimeEvent(
                "activity",
                {
                    "message": self._image_generation_quota_message(state),
                    "kind": "image_quota",
                    "accepted": state.runtime_images_accepted,
                    "blocked": state.runtime_images_blocked,
                    "window": status.window,
                    "used": status.used,
                    "limit": status.limit,
                    "resets_at": status.resets_at.isoformat(),
                },
            ),
        )

    def _persist_runtime_generated_image(
        self,
        state: TurnExecutionState,
        data: dict[str, object],
    ) -> str | None:
        image_bytes = self._load_runtime_generated_image_bytes(data)
        if not image_bytes:
            return None
        content_type = self._detect_image_content_type(image_bytes)
        if content_type is None:
            return None
        extension = self.allowed_image_mime_types[content_type]
        filename = f"{uuid4().hex}{extension}"
        target_dir = self._personal_workspace_root(state.session.user_id) / "generated-images"
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            (target_dir / filename).write_bytes(image_bytes)
        except OSError:
            return None
        return f"![{self._generated_image_alt_text(data)}](me/generated-images/{filename})"

    def _load_runtime_generated_image_bytes(self, data: dict[str, object]) -> bytes | None:
        source_path = str(data.get("source_path") or "").strip()
        if source_path:
            candidate = Path(source_path).expanduser()
            try:
                if candidate.is_file():
                    return candidate.read_bytes()
            except OSError:
                pass
        encoded = str(data.get("data") or "").strip()
        if not encoded:
            return None
        if encoded.startswith("data:"):
            encoded = encoded.split(",", 1)[-1]
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except ValueError:
            return None
        return decoded or None

    @staticmethod
    def _generated_image_alt_text(data: dict[str, object]) -> str:
        prompt = " ".join(str(data.get("revised_prompt") or "").split())
        if not prompt:
            return "AI 生成图像"
        prompt = prompt.replace("[", "(").replace("]", ")")
        if len(prompt) > 80:
            prompt = prompt[:80].rstrip() + "…"
        return prompt

    def _resolve_owner_file_artifact_source(
        self,
        owner_user_id: str,
        display_path: str,
    ) -> tuple[str, str, Path] | None:
        normalized = self._normalize_personal_workspace_display_path(display_path)
        if normalized is None:
            return None

        if normalized.startswith("me/"):
            source_path = normalized.removeprefix("me/")
        elif normalized.startswith("workspace/users/"):
            parts = normalized.split("/")
            if len(parts) < 4 or parts[2] != owner_user_id:
                return None
            source_path = "/".join(parts[3:])
        else:
            return None

        if not source_path or ".." in source_path.split("/"):
            return None

        user_root = self._personal_workspace_root(owner_user_id)
        file_path = (user_root / source_path).resolve()
        try:
            file_path.relative_to(user_root)
        except ValueError:
            return None

        return f"me/{source_path}", source_path, file_path

    def _upsert_file_artifact_from_path(
        self,
        *,
        session: AssistantSessionRecord,
        message_id: str | None,
        turn_id: str | None,
        display_path: str,
        source_path: str,
        file_path: Path,
    ) -> AssistantFileArtifactRecord:
        filename = file_path.name or Path(source_path).name
        if file_path.is_file():
            size_bytes = file_path.stat().st_size
            sha256 = self._sha256_file(file_path)
            mime_type = self._detect_file_mime_type(file_path)
            storage_status = "source"
        else:
            size_bytes = 0
            sha256 = ""
            mime_type = self._guess_file_mime_type(filename)
            storage_status = "missing"

        return self.store.upsert_file_artifact(
            session_id=session.session_id,
            message_id=message_id,
            turn_id=turn_id,
            owner_user_id=session.user_id,
            display_path=display_path,
            source_path=source_path,
            filename=filename,
            mime_type=mime_type,
            size_bytes=size_bytes,
            sha256=sha256,
            storage_status=storage_status,
        )

    def _source_file_path_for_artifact(self, artifact: AssistantFileArtifactRecord) -> Path | None:
        if artifact.storage_status == "missing" or ".." in artifact.source_path.split("/"):
            return None
        user_root = self._personal_workspace_root(artifact.owner_user_id)
        file_path = (user_root / artifact.source_path).resolve()
        try:
            file_path.relative_to(user_root)
        except ValueError:
            return None
        if not file_path.is_file():
            return None
        return file_path

    def _create_shared_file_snapshot(
        self,
        share_id: str,
        artifact: AssistantFileArtifactRecord,
    ) -> SharedSessionFileArtifactRecord | None:
        source_path = self._source_file_path_for_artifact(artifact)
        if source_path is None:
            return None

        relative_snapshot_path = Path("workspace/runtime/shared-session-files") / share_id / artifact.artifact_id / "file"
        snapshot_path = (Path(self.runtime_working_directory).resolve() / relative_snapshot_path).resolve()
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source_path, snapshot_path)
        size_bytes = snapshot_path.stat().st_size
        sha256 = self._sha256_file(snapshot_path)

        metadata_path = snapshot_path.parent / "metadata.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "artifact_id": artifact.artifact_id,
                    "share_id": share_id,
                    "session_id": artifact.session_id,
                    "message_id": artifact.message_id,
                    "turn_id": artifact.turn_id,
                    "display_path": artifact.display_path,
                    "source_path": artifact.source_path,
                    "filename": artifact.filename,
                    "mime_type": artifact.mime_type,
                    "size_bytes": size_bytes,
                    "sha256": sha256,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        self.store.upsert_file_artifact(
            session_id=artifact.session_id,
            message_id=artifact.message_id,
            turn_id=artifact.turn_id,
            owner_user_id=artifact.owner_user_id,
            display_path=artifact.display_path,
            source_path=artifact.source_path,
            filename=artifact.filename,
            mime_type=artifact.mime_type,
            size_bytes=artifact.size_bytes,
            sha256=artifact.sha256,
            storage_status="snapshotted",
        )
        return self.store.upsert_shared_file_artifact(
            share_id=share_id,
            artifact_id=artifact.artifact_id,
            snapshot_path=relative_snapshot_path.as_posix(),
            filename=artifact.filename,
            mime_type=artifact.mime_type,
            size_bytes=size_bytes,
            sha256=sha256,
        )

    def _personal_workspace_root(self, user_id: str) -> Path:
        return (Path(self.runtime_working_directory).resolve() / "workspace/users" / user_id).resolve()

    def _sha256_file(self, file_path: Path) -> str:
        digest = hashlib.sha256()
        with file_path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _detect_file_mime_type(self, file_path: Path) -> str:
        with file_path.open("rb") as handle:
            header = handle.read(512)
        if header.startswith(b"%PDF-"):
            return "application/pdf"
        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if header.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
            return "image/gif"
        if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return "image/webp"
        return self._guess_file_mime_type(file_path.name)

    def _guess_file_mime_type(self, filename: str) -> str:
        suffix = Path(filename).suffix.lower()
        if suffix in {".md", ".mdx"}:
            return "text/markdown; charset=utf-8"
        if suffix in {".txt", ".log"}:
            return "text/plain; charset=utf-8"
        return mimetypes.guess_type(filename)[0] or "application/octet-stream"

    def _iter_payload_strings(self, value: object) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, dict):
            result: list[str] = []
            for item in value.values():
                result.extend(self._iter_payload_strings(item))
            return result
        if isinstance(value, list):
            result = []
            for item in value:
                result.extend(self._iter_payload_strings(item))
            return result
        return []

    def _serialize_citation(self, citation: AnswerCitationRecord) -> dict[str, object]:
        return {
            "citation_id": citation.citation_id,
            "citation_type": citation.citation_type,
            "label": citation.label,
            "path": citation.path,
            "line_start": citation.line_start,
            "line_end": citation.line_end,
            "snippet": citation.snippet,
            "metadata": citation.metadata,
            "created_at": citation.created_at.isoformat(),
        }

    def _serialize_message_event(
        self,
        message: AssistantMessageRecord,
        *,
        turn_id: str | None = None,
    ) -> dict[str, object]:
        git_scopes = self.store.list_turn_git_context_snapshots(turn_id) if turn_id else []
        return {
            "message_id": message.message_id,
            "session_id": message.session_id,
            "turn_id": turn_id,
            "role": message.role,
            "content": message.content,
            "created_at": message.created_at.isoformat(),
            "feedback": message.feedback,
            "git_scopes": [
                {
                    "repo_full_name": item.repo_full_name,
                    "strategy": item.strategy,
                    "selector_type": item.selector_type,
                    "selector_value": item.selector_value,
                    "resolved_sha": item.resolved_sha,
                }
                for item in git_scopes
            ],
        }

    def _to_context_input(self, record: object) -> AssistantContextInput:
        return AssistantContextInput(
            context_key=str(getattr(record, "context_key")),
            label=str(getattr(record, "label")),
            content=getattr(record, "content"),
            source_type=str(getattr(record, "source_type")),
            source_uri=getattr(record, "source_uri"),
            metadata=dict(getattr(record, "metadata")),
        )

    def _require_turn_access(self, user: UserRecord, turn_id: str) -> AssistantTurnRecord:
        turn = self.store.get_turn(turn_id)
        if turn is None:
            raise ValueError("轮次不存在。")
        if self.store.get_session(turn.session_id, user.user_id) is None:
            raise ValueError("轮次不存在或无权限访问。")
        return turn

    def _register_turn_stream(self, turn_id: str, task: asyncio.Task[None] | None = None) -> None:
        self._turn_streams[turn_id] = ActiveTurnStream(task=task)

    async def _publish_turn_stream_event(self, turn_id: str, event: dict[str, object]) -> None:
        stream = self._turn_streams.get(turn_id)
        if stream is None:
            return
        stream.next_sequence += 1
        for subscriber in tuple(stream.subscribers):
            await subscriber.put((stream.next_sequence, event))

    async def _close_turn_stream(self, turn_id: str) -> None:
        stream = self._turn_streams.pop(turn_id, None)
        if stream is None:
            return
        stream.next_sequence += 1
        for subscriber in tuple(stream.subscribers):
            await subscriber.put((stream.next_sequence, None))

    def _reconcile_running_turn(self, turn: AssistantTurnRecord) -> AssistantTurnRecord:
        shared_follow_up_task = self._shared_follow_up_tasks.get(turn.session_id)
        if (
            turn.status != "running"
            or turn.turn_id in self._turn_streams
            or (shared_follow_up_task is not None and not shared_follow_up_task.done())
        ):
            return turn

        self.store.append_runtime_event(
            turn.session_id,
            turn.turn_id,
            "activity",
            {
                "message": "检测到当前执行状态已丢失，正在结束已中断的轮次。",
                "kind": "orphaned_turn",
            },
        )
        updated_turn = self.store.fail_turn(turn.turn_id, self.orphaned_turn_error_message)
        return updated_turn or turn

    def _build_resume_events(
        self,
        owner_user_id: str,
        turn: AssistantTurnRecord,
        *,
        include_terminal: bool,
    ) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []

        for stored_event in self.store.list_runtime_events(turn.turn_id):
            if stored_event.event_type == "mcp_approval":
                continue
            if stored_event.event_type == "activity" and stored_event.payload.get("kind") == "idle_heartbeat":
                continue
            runtime_event = RuntimeEvent(stored_event.event_type, stored_event.payload)
            events.extend(map_runtime_event_to_sse(runtime_event))

        events.extend(
            {"type": "mcp_approval", "approval": record}
            for record in self.mcp_approvals.list_for_session(turn.session_id)
            if record["turn_id"] == turn.turn_id
        )
        if include_terminal:
            events.extend(self._build_turn_terminal_events(owner_user_id, turn))

        return events

    def _build_turn_terminal_events(
        self,
        owner_user_id: str,
        turn: AssistantTurnRecord,
    ) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []
        citations = self.store.list_answer_citations(turn.turn_id)
        if citations:
            events.append(
                {
                    "type": "citations",
                    "citations": [self._serialize_citation(citation) for citation in citations],
                }
            )

        assistant_message = self._find_turn_assistant_message(owner_user_id, turn)
        if assistant_message is not None:
            events.append(
                {
                    "type": "message",
                    "message": self._serialize_message_event(assistant_message, turn_id=turn.turn_id),
                }
            )

        if turn.status == "completed":
            events.append(
                {
                    "type": "complete",
                    "session_id": turn.session_id,
                    "turn_id": turn.turn_id,
                }
            )
        elif turn.status == "failed":
            events.append(
                {
                    "type": "error",
                    "message": turn.error_message or "Runtime 执行失败。",
                }
            )

        return events

    def _find_turn_assistant_message(
        self,
        owner_user_id: str,
        turn: AssistantTurnRecord,
    ) -> AssistantMessageRecord | None:
        if not turn.assistant_message_id:
            return None
        for message in self.store.list_messages(turn.session_id, owner_user_id):
            if message.message_id == turn.assistant_message_id:
                return message
        return None

    def _display_upload_filename(self, filename: str) -> str:
        name = Path(filename or "").name.strip()
        if not name:
            return ""
        sanitized = re.sub(r"[\x00-\x1f\x7f]+", "", name)
        sanitized = re.sub(r"\s+", " ", sanitized).strip(" .")
        return sanitized[:108]

    def _safe_path_segment(self, value: str) -> str:
        sanitized = re.sub(r"[^0-9A-Za-z._-]+", "_", value.strip())
        return sanitized[:80] or "user"

    def _detect_image_content_type(self, data: bytes) -> str | None:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if data.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if data.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            return "image/webp"
        return None
