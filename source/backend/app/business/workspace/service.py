from __future__ import annotations

import io
import json
import logging
import re
import secrets
import shutil
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from mimetypes import guess_type
from pathlib import Path

from app.utils.archive import normalize_zip_archive_entries

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkspaceTreeNode:
    id: str
    kind: str
    name: str
    path: str
    children: list["WorkspaceTreeNode"]
    has_children: bool = False
    children_loaded: bool = True
    child_count: int | None = None
    updated_at: str | None = None
    content_type: str | None = None
    content_path: str | None = None


@dataclass(frozen=True)
class WorkspaceFileRecord:
    id: str
    name: str
    path: str
    updated_at: str | None
    content_type: str
    content: str


@dataclass(frozen=True)
class WorkspaceSyncSummaryRecord:
    synced_at: str | None
    changed_count: int
    sample_files: list[str]
    errors: int
    last_changed_at: str | None = None
    last_changed_count: int = 0
    last_sample_files: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class WorkspaceTreeRecord:
    root: WorkspaceTreeNode
    default_file_path: str | None = None
    sync_summary: WorkspaceSyncSummaryRecord | None = None


@dataclass(frozen=True)
class WorkspaceKnowledgeIndexRecord:
    prefixes: list[str]
    directories: list[tuple[int, str, int]]
    files: list[tuple[int, str]]


@dataclass(frozen=True)
class WorkspaceAssetRecord:
    path: str
    file_path: Path
    media_type: str


@dataclass
class _CacheEntry:
    node_map: dict[str, WorkspaceTreeNode]
    file_paths: list[str]
    expires_at: float
    sync_state_signature: tuple[float, str | None] | None


class WorkspaceBrowserService:
    _SKILL_ARCHIVE_MAX_BYTES = 20 * 1024 * 1024
    _SKILL_ARCHIVE_MAX_EXTRACTED_BYTES = 50 * 1024 * 1024
    _SKILL_ARCHIVE_MAX_FILES = 1000
    _TEXT_CODE_EXTENSIONS = {
        ".css",
        ".go",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".jsx",
        ".py",
        ".rb",
        ".rs",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".xml",
        ".yaml",
        ".yml",
    }
    _TEXT_PLAIN_EXTENSIONS = {
        ".csv",
        ".log",
        ".md",
        ".mdx",
        ".rst",
        ".text",
        ".txt",
    }
    _HIDDEN_NAMES = {
        ".DS_Store",
        ".git",
        ".idea",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "node_modules",
    }
    _MEDIA_DIR_NAMES = {"images", "assets", "attachments"}
    _NON_TEXT_EXTENSIONS = {
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".zip",
        ".pack",
        ".idx",
        ".svg",
        ".webp",
        ".mp4",
        ".mp3",
    }

    def __init__(
        self,
        *,
        base_dir: Path,
        knowledge_roots: dict[str, Path],
        skills_root: Path,
        preview_limit: int = 100000,
        knowledge_cache_ttl_seconds: int = 300,
    ) -> None:
        self._base_dir = base_dir.resolve()
        self._knowledge_mount_roots = {key: path.absolute() for key, path in knowledge_roots.items()}
        self._knowledge_roots = {key: path.resolve() for key, path in knowledge_roots.items()}
        self._skills_root = skills_root.resolve()
        self._preview_limit = preview_limit
        self._knowledge_cache_ttl_seconds = knowledge_cache_ttl_seconds
        self._sync_state_dir = self._base_dir / "workspace/runtime/sync-state"

        self._knowledge_cache: dict[str, _CacheEntry] = {}
        self._knowledge_cache_lock = threading.Lock()
        self._refreshing_scopes: set[str] = set()

    def list_knowledge_children(self, knowledge_type: str, relative_path: str | None = None) -> WorkspaceTreeRecord:
        root_path = self._get_knowledge_root(knowledge_type)
        target_path = root_path if relative_path is None else self._resolve_relative_path(root_path, relative_path)
        cache_entry = self._get_or_schedule_cache_entry(knowledge_type)
        if cache_entry is None:
            empty_node = self._build_empty_folder_node(target_path)
            return WorkspaceTreeRecord(
                root=empty_node,
                default_file_path=None,
                sync_summary=self._read_sync_summary(knowledge_type, target_path),
            )

        relative_display_path = self._to_relative_display_path(target_path)
        node = cache_entry.node_map.get(relative_display_path)
        if node is None:
            raise FileNotFoundError("目录不存在。")

        return WorkspaceTreeRecord(root=node, default_file_path=None, sync_summary=self._read_sync_summary(knowledge_type, target_path))

    def list_knowledge_index(self) -> WorkspaceKnowledgeIndexRecord:
        prefixes: list[str] = []
        directories: list[tuple[int, str, int]] = []
        files: list[tuple[int, str]] = []

        for idx, scope in enumerate(["requirements", "business", "code"]):
            root_path = self._knowledge_roots.get(scope)
            if root_path is None:
                continue

            prefix = self._to_relative_display_path(root_path).rstrip("/") + "/"
            prefixes.append(prefix)
            cache_entry = self._get_or_schedule_cache_entry(scope)
            if cache_entry is None:
                continue

            for absolute_path, node in sorted(cache_entry.node_map.items()):
                if absolute_path.startswith(prefix) and absolute_path != prefix.rstrip("/"):
                    directories.append((idx, absolute_path[len(prefix):], node.child_count or 0))

            for absolute_path in cache_entry.file_paths:
                if absolute_path.startswith(prefix):
                    files.append((idx, absolute_path[len(prefix):]))

        return WorkspaceKnowledgeIndexRecord(prefixes=prefixes, directories=directories, files=files)

    def read_knowledge_file(self, knowledge_type: str, relative_path: str) -> WorkspaceFileRecord:
        root_path = self._get_knowledge_root(knowledge_type)
        return self._read_file(root_path, relative_path)

    def resolve_knowledge_asset(self, knowledge_type: str, relative_path: str) -> WorkspaceAssetRecord:
        root_path = self._get_knowledge_root(knowledge_type)
        return self._resolve_asset(root_path, relative_path)

    def refresh_knowledge_cache(self, knowledge_type: str) -> None:
        self._rebuild_cache_entry(knowledge_type)

    def list_skill_tree(self) -> WorkspaceTreeRecord:
        return self._build_tree(self._skills_root)

    def read_skill_file(self, relative_path: str) -> WorkspaceFileRecord:
        return self._read_file(self._skills_root, relative_path)

    def get_skill_key(self, relative_path: str) -> str:
        target = self._resolve_relative_path(self._skills_root, relative_path)
        return self._resolve_skill_root_for_entry(target).name

    def delete_skill_folder(self, relative_path: str) -> WorkspaceTreeRecord:
        folder_path = self._resolve_relative_path(self._skills_root, relative_path)
        if not folder_path.exists() or not folder_path.is_dir():
            raise FileNotFoundError("Skill 文件夹不存在。")
        if folder_path == self._skills_root:
            raise FileNotFoundError("不能删除 Skill 根目录。")
        if folder_path.parent != self._skills_root:
            raise FileNotFoundError("只能删除 Skill 自身文件夹。")
        if not (folder_path / "SKILL.md").is_file():
            raise FileNotFoundError("只能删除包含 SKILL.md 的 Skill 文件夹。")
        shutil.rmtree(folder_path)
        return self.list_skill_tree()

    def update_skill_file(self, relative_path: str, content: str) -> WorkspaceFileRecord:
        return self.update_skill_file_with_log(relative_path, content, editor_name=None, edit_summary=None)

    def update_skill_file_with_log(
        self,
        relative_path: str,
        content: str,
        *,
        editor_name: str | None,
        edit_summary: str | None,
    ) -> WorkspaceFileRecord:
        file_path = self._resolve_relative_path(self._skills_root, relative_path)
        if not file_path.exists() or not file_path.is_file():
            raise FileNotFoundError("Skill 文件不存在。")
        if file_path.name == "SKILL.md":
            content = self._record_skill_edit(
                content,
                actor_name=editor_name or "未知用户",
                action=edit_summary or "更新 Skill 内容",
            )
        file_path.write_text(content, encoding="utf-8")
        return self._read_file(self._skills_root, relative_path)

    def create_skill(
        self,
        *,
        name: str | None,
        folder_name: str | None = None,
        description: str,
        creator_name: str,
        content: str,
        intent: str,
        files: list[tuple[str, str]],
    ) -> WorkspaceFileRecord:
        resolved_name = (name or self._derive_skill_name(intent, description)).strip()
        if not resolved_name:
            raise FileNotFoundError("Skill 名称不能为空。")
        resolved_description = description.strip() or self._derive_skill_description(intent)
        self._skills_root.mkdir(parents=True, exist_ok=True)
        skill_slug = self._slugify_skill_name(folder_name or resolved_name)
        skill_dir = self._skills_root / skill_slug
        if skill_dir.exists():
            raise FileExistsError("Skill 已存在。")
        file_targets: list[tuple[Path, str]] = []
        for file_path, file_content in files:
            target_path = self._resolve_skill_entry_path(skill_dir, file_path)
            if target_path.name == "SKILL.md":
                raise FileExistsError("文件路径不能覆盖 SKILL.md。")
            file_targets.append((target_path, file_content))

        skill_dir.mkdir(parents=True)
        today = date.today().isoformat()
        skill_content = content.strip()
        if not skill_content:
            skill_content = self._generate_skill_markdown(name=resolved_name, description=resolved_description, intent=intent)
        skill_content = self._ensure_skill_metadata(
            skill_content,
            name=resolved_name,
            description=resolved_description,
            creator_name=creator_name,
            created_at=today,
            action="创建 Skill",
        )

        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(skill_content, encoding="utf-8")

        for target_path, file_content in file_targets:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(file_content, encoding="utf-8")

        return self._read_file(self._skills_root, self._to_relative_display_path(skill_file))

    def preview_skill_archive(self, archive: bytes, *, archive_name: str, owner_id: str) -> dict[str, object]:
        if not archive_name.lower().endswith(".zip"):
            raise FileNotFoundError("只支持上传 ZIP 文件。")
        if not archive or len(archive) > self._SKILL_ARCHIVE_MAX_BYTES:
            raise FileNotFoundError("ZIP 文件不能为空，且大小不能超过 20 MB。")

        try:
            archive_file = zipfile.ZipFile(io.BytesIO(archive))
        except zipfile.BadZipFile as exc:
            raise FileNotFoundError("ZIP 文件损坏或格式不正确。") from exc

        with archive_file:
            entries, wrapper_name = normalize_zip_archive_entries(archive_file.infolist())
            file_entries = [(info, path) for info, path in entries if not info.is_dir()]
            total_size = sum(info.file_size for info, _ in file_entries)
            if len(file_entries) > self._SKILL_ARCHIVE_MAX_FILES:
                raise FileNotFoundError("ZIP 内文件数量不能超过 1000 个。")
            if total_size > self._SKILL_ARCHIVE_MAX_EXTRACTED_BYTES:
                raise FileNotFoundError("ZIP 解压后的总大小不能超过 50 MB。")

            nested_skill_paths = [path for _, path in file_entries if path.name == "SKILL.md" and len(path.parts) > 1]
            if nested_skill_paths:
                raise FileNotFoundError("ZIP 中包含嵌套的 Skill，请拆分为一个 ZIP 一个 Skill 后再上传。")

            token = secrets.token_urlsafe(18)
            stage_root = self._skill_import_root / token
            package_root = stage_root / "workspace"
            package_root.mkdir(parents=True)
            try:
                for info, relative_path in entries:
                    target = package_root.joinpath(*relative_path.parts)
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive_file.open(info) as source, target.open("wb") as destination:
                        shutil.copyfileobj(source, destination)

                suggested_name = self._slugify_skill_name(wrapper_name or Path(archive_name).stem)
                metadata = {
                    "owner_id": owner_id,
                    "archive_name": archive_name,
                    "suggested_folder_name": suggested_name,
                    "created_at": time.time(),
                    "generation_tracking_version": 1,
                }
                (stage_root / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")
                skill_file = package_root / "SKILL.md"
                if skill_file.is_file() and self._detect_content_type(skill_file) == "binary":
                    raise FileNotFoundError("SKILL.md 必须是 UTF-8 文本文件。")
                preview = self._build_skill_import_preview(token, package_root, metadata)
                self._discard_other_skill_imports(owner_id=owner_id, keep_token=token)
                return preview
            except Exception:
                shutil.rmtree(stage_root, ignore_errors=True)
                raise

    def get_latest_skill_import_preview(self, *, owner_id: str) -> dict[str, object] | None:
        if not self._skill_import_root.is_dir():
            return None

        latest: tuple[float, str, Path, dict[str, object]] | None = None
        for stage_root in self._skill_import_root.iterdir():
            metadata_path = stage_root / "metadata.json"
            package_root = stage_root / "workspace"
            if not metadata_path.is_file() or not package_root.is_dir():
                continue
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
                created_at = float(metadata.get("created_at") or 0)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if metadata.get("owner_id") != owner_id:
                continue
            if latest is None or created_at > latest[0]:
                latest = (created_at, stage_root.name, package_root, metadata)

        if latest is None:
            return None
        _, token, package_root, metadata = latest
        return self._build_skill_import_preview(token, package_root, metadata)

    def get_skill_import_preview(self, *, token: str, owner_id: str) -> dict[str, object]:
        _, package_root, metadata = self._load_skill_import(token, owner_id)
        return self._build_skill_import_preview(token, package_root, metadata)

    def discard_skill_import(self, *, token: str, owner_id: str) -> bool:
        try:
            stage_root, _, _ = self._load_skill_import(token, owner_id)
        except FileNotFoundError:
            return False
        shutil.rmtree(stage_root)
        return True

    def update_skill_import_generation(
        self,
        *,
        token: str,
        owner_id: str,
        status: str,
        message: str = "",
        content: str = "",
        error: str = "",
    ) -> None:
        stage_root, _, _ = self._load_skill_import(token, owner_id)
        state_path = stage_root / "generation.json"
        pending_path = stage_root / ".generation.json.tmp"
        pending_path.write_text(
            json.dumps(
                {
                    "status": status,
                    "message": message,
                    "content": content,
                    "error": error,
                    "updated_at": time.time(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        pending_path.replace(state_path)

    def commit_skill_archive(
        self,
        *,
        token: str,
        owner_id: str,
        folder_name: str,
        skill_md_mode: str,
        skill_md_content: str,
    ) -> tuple[WorkspaceTreeRecord, str]:
        stage_root, package_root, metadata = self._load_skill_import(token, owner_id)
        skill_file = package_root / "SKILL.md"
        if skill_md_mode == "existing":
            if not skill_file.is_file():
                raise FileNotFoundError("上传内容缺少 SKILL.md，请手动或自动补充。")
            if skill_md_content.strip():
                skill_file.write_text(skill_md_content, encoding="utf-8")
        else:
            if not skill_md_content.strip():
                raise FileNotFoundError("请填写 SKILL.md 内容。")
            skill_file.write_text(skill_md_content, encoding="utf-8")

        self._skills_root.mkdir(parents=True, exist_ok=True)
        skill_slug = self._slugify_skill_name(folder_name)
        skill_dir = self._skills_root / skill_slug
        if skill_dir.exists():
            raise FileExistsError("Skill 已存在，请修改目录名后重试。")
        package_root.replace(skill_dir)
        shutil.rmtree(stage_root, ignore_errors=True)
        skill_path = self._to_relative_display_path(skill_dir / "SKILL.md")
        return self.list_skill_tree(), skill_path

    def get_skill_import_generation_context(self, *, token: str, owner_id: str) -> dict[str, object]:
        stage_root, package_root, metadata = self._load_skill_import(token, owner_id)
        return {
            "archive_name": str(metadata.get("archive_name") or ""),
            "runtime_working_directory": str(package_root),
            "entries": self._build_skill_import_preview_entries(package_root),
            "generation_status": self._read_skill_import_generation(stage_root, metadata)["status"],
        }

    def create_skill_entry(self, *, parent_path: str, name: str, kind: str, content: str) -> WorkspaceTreeRecord:
        parent = self._resolve_relative_path(self._skills_root, parent_path)
        skill_root = self._resolve_skill_root_for_entry(parent)
        if not parent.is_dir():
            raise FileNotFoundError("父目录不存在。")
        normalized_name = self._normalize_skill_entry_name(name)
        target = self._resolve_skill_entry_path(parent, normalized_name)
        if target.exists():
            raise FileExistsError("同名文件或文件夹已存在。")
        if normalized_name == "SKILL.md" and parent != skill_root:
            raise FileNotFoundError("SKILL.md 只能位于 Skill 根目录。")
        if kind == "folder":
            target.mkdir()
        else:
            target.write_text(content, encoding="utf-8")
        return self.list_skill_tree()

    def move_skill_entry(self, *, relative_path: str, destination_path: str) -> WorkspaceTreeRecord:
        source = self._resolve_relative_path(self._skills_root, relative_path)
        destination = self._resolve_relative_path(self._skills_root, destination_path)
        source_skill_root = self._resolve_skill_root_for_entry(source)
        destination_skill_root = self._resolve_skill_root_for_entry(destination.parent)
        if source_skill_root != destination_skill_root:
            raise FileNotFoundError("不能把文件移动到其他 Skill。")
        if source == source_skill_root or source == source_skill_root / "SKILL.md":
            raise FileNotFoundError("不能重命名 Skill 根目录或 SKILL.md。")
        if not source.exists():
            raise FileNotFoundError("文件或文件夹不存在。")
        if not destination.parent.is_dir():
            raise FileNotFoundError("目标目录不存在。")
        self._normalize_skill_entry_name(destination.name)
        if destination.name == "SKILL.md":
            raise FileNotFoundError("不能创建嵌套的 SKILL.md。")
        if destination.exists():
            raise FileExistsError("目标路径已存在。")
        if source.is_dir() and destination.resolve().is_relative_to(source.resolve()):
            raise FileNotFoundError("不能把文件夹移动到自身内部。")
        destination.parent.mkdir(parents=False, exist_ok=True)
        source.replace(destination)
        return self.list_skill_tree()

    def delete_skill_entry(self, relative_path: str) -> WorkspaceTreeRecord:
        target = self._resolve_relative_path(self._skills_root, relative_path)
        skill_root = self._resolve_skill_root_for_entry(target)
        if target == skill_root or target == skill_root / "SKILL.md":
            raise FileNotFoundError("不能删除 Skill 根目录或 SKILL.md。")
        if not target.exists():
            raise FileNotFoundError("文件或文件夹不存在。")
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return self.list_skill_tree()

    def _get_or_schedule_cache_entry(self, knowledge_type: str) -> _CacheEntry | None:
        now = time.monotonic()
        current_signature = self._read_sync_state_signature(knowledge_type)

        with self._knowledge_cache_lock:
            entry = self._knowledge_cache.get(knowledge_type)
            if entry is not None:
                is_expired = entry.expires_at <= now
                signature_changed = entry.sync_state_signature != current_signature
                if not is_expired and not signature_changed:
                    return entry
                self._schedule_cache_rebuild_locked(knowledge_type)
                return entry

        self._rebuild_cache_entry(knowledge_type)
        with self._knowledge_cache_lock:
            return self._knowledge_cache.get(knowledge_type)

    def _schedule_cache_rebuild_locked(self, knowledge_type: str) -> None:
        if knowledge_type in self._refreshing_scopes:
            return
        self._refreshing_scopes.add(knowledge_type)
        thread = threading.Thread(target=self._rebuild_cache_entry, args=(knowledge_type,), daemon=True)
        thread.start()

    def _rebuild_cache_entry(self, knowledge_type: str) -> None:
        started_at = time.perf_counter()
        try:
            root_path = self._get_knowledge_root(knowledge_type)
            node_map, file_paths = self._build_knowledge_node_map(root_path)
            signature = self._read_sync_state_signature(knowledge_type)
            expires_at = time.monotonic() + self._knowledge_cache_ttl_seconds
            next_entry = _CacheEntry(
                node_map=node_map,
                file_paths=file_paths,
                expires_at=expires_at,
                sync_state_signature=signature,
            )
            with self._knowledge_cache_lock:
                self._knowledge_cache[knowledge_type] = next_entry
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            logger.info(
                "知识库缓存重建完成: type=%s elapsed_ms=%.1f nodes=%d files=%d",
                knowledge_type,
                elapsed_ms,
                len(node_map),
                len(file_paths),
            )
        finally:
            with self._knowledge_cache_lock:
                self._refreshing_scopes.discard(knowledge_type)

    def _build_knowledge_node_map(self, root_path: Path) -> tuple[dict[str, WorkspaceTreeNode], list[str]]:
        if not root_path.exists() or not root_path.is_dir():
            raise FileNotFoundError("目录不存在。")

        node_map: dict[str, WorkspaceTreeNode] = {}
        file_paths: list[str] = []

        def build_directory(path: Path) -> WorkspaceTreeNode:
            # 检测目录自身的 _index.md（文件夹自带内容）
            index_file = path / "_index.md"
            index_content_path: str | None = None
            index_content_type: str | None = None
            if index_file.is_file():
                index_content_path = self._to_relative_display_path(index_file)
                index_content_type = self._detect_content_type(index_file)
                file_paths.append(index_content_path)
                if not self._index_has_body(index_file):
                    index_content_path = None
                    index_content_type = None

            children: list[WorkspaceTreeNode] = []
            for child in sorted(path.iterdir(), key=self._sort_key):
                if self._should_skip_knowledge_path(child):
                    continue
                if child.name == "_index.md":
                    continue

                if child.is_dir():
                    child_children_count = self._count_visible_children(child)
                    child_node = WorkspaceTreeNode(
                        id=self._to_relative_display_path(child),
                        kind="folder",
                        name=child.name,
                        path=self._to_relative_display_path(child),
                        children=[],
                        has_children=child_children_count > 0,
                        children_loaded=False,
                        child_count=None,
                    )
                else:
                    content_type = self._detect_content_type(child)
                    child_node = WorkspaceTreeNode(
                        id=self._to_relative_display_path(child),
                        kind="file",
                        name=child.name,
                        path=self._to_relative_display_path(child),
                        children=[],
                        has_children=False,
                        children_loaded=True,
                        updated_at=self._format_mtime(child),
                        content_type=content_type,
                    )
                    file_paths.append(child_node.path)
                children.append(child_node)

            current_path = self._to_relative_display_path(path)
            expanded_node = WorkspaceTreeNode(
                id=current_path,
                kind="folder",
                name=path.name,
                path=current_path,
                children=children,
                has_children=bool(children),
                children_loaded=True,
                child_count=len(children),
                content_path=index_content_path,
                content_type=index_content_type,
            )
            node_map[current_path] = expanded_node

            for child in sorted(path.iterdir(), key=self._sort_key):
                if child.is_dir() and not self._should_skip_knowledge_path(child):
                    build_directory(child)

            return expanded_node

        build_directory(root_path)
        return node_map, sorted(file_paths)

    def _count_visible_children(self, path: Path) -> int:
        count = 0
        for child in path.iterdir():
            if child.name == "_index.md":
                continue
            if not self._should_skip_knowledge_path(child):
                count += 1
        return count

    def _build_empty_folder_node(self, path: Path) -> WorkspaceTreeNode:
        relative_path = self._to_relative_display_path(path)
        return WorkspaceTreeNode(
            id=relative_path,
            kind="folder",
            name=path.name,
            path=relative_path,
            children=[],
            has_children=False,
            children_loaded=True,
            child_count=0,
        )

    def _read_sync_state_signature(self, knowledge_type: str) -> tuple[float, str | None] | None:
        state_path = self._sync_state_dir / f"{knowledge_type}.json"
        if not state_path.exists() or not state_path.is_file():
            return None

        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        last_run = payload.get("last_run") if isinstance(payload, dict) else None
        if isinstance(last_run, dict):
            synced_at = last_run.get("synced_at")
        else:
            synced_at = payload.get("synced_at") if isinstance(payload, dict) else None
        return (state_path.stat().st_mtime, synced_at if isinstance(synced_at, str) else None)

    def _read_sync_summary(self, knowledge_type: str, target_path: Path | None = None) -> WorkspaceSyncSummaryRecord | None:
        state_path = self._sync_state_dir / f"{knowledge_type}.json"
        if not state_path.exists() or not state_path.is_file():
            return None

        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

        if not isinstance(payload, dict):
            return None

        last_run = payload.get("last_run") if isinstance(payload.get("last_run"), dict) else payload
        last_change = payload.get("last_change") if isinstance(payload.get("last_change"), dict) else None
        if last_change is None:
            last_change = {
                "changed_at": payload.get("last_changed_at") or payload.get("synced_at"),
                "added": payload.get("last_added") if "last_added" in payload else payload.get("added"),
                "updated": payload.get("last_updated") if "last_updated" in payload else payload.get("updated"),
                "changed_count": payload.get("last_changed_count"),
                "changed_files": payload.get("last_changed_files") if "last_changed_files" in payload else payload.get("changed_files"),
            }

        scope_relative_path = self._sync_summary_scope_relative_path(knowledge_type, target_path)
        synced_at = last_run.get("synced_at") if isinstance(last_run.get("synced_at"), str) else None
        last_changed_at = last_change.get("changed_at") if isinstance(last_change.get("changed_at"), str) else None
        run_changed_files = last_run.get("changed_files")
        if not isinstance(run_changed_files, list) and synced_at and synced_at == last_changed_at:
            run_changed_files = last_change.get("changed_files")
        changed_files = self._filter_sync_files(knowledge_type, run_changed_files if isinstance(run_changed_files, list) else [], scope_relative_path)
        changed_count = int(last_run.get("changed_count") or 0)
        if changed_count <= 0:
            changed_count = int(last_run.get("added") or 0) + int(last_run.get("updated") or 0)
        if changed_count <= 0 and changed_files:
            changed_count = len(changed_files)
        if scope_relative_path:
            changed_count = len(changed_files)
        sample_files = self._sync_summary_sample_files(changed_files, scope_relative_path)
        errors = int(last_run.get("errors") or 0)

        last_changed_files = self._filter_sync_files(
            knowledge_type,
            last_change.get("changed_files") if isinstance(last_change.get("changed_files"), list) else [],
            scope_relative_path,
        )
        last_changed_count = int(last_change.get("changed_count") or 0)
        if last_changed_count <= 0:
            last_changed_count = int(last_change.get("added") or 0) + int(last_change.get("updated") or 0)
        if last_changed_count <= 0 and last_changed_files:
            last_changed_count = len(last_changed_files)
        if scope_relative_path:
            last_changed_count = len(last_changed_files)
        if not last_changed_at and changed_count > 0:
            last_changed_at = synced_at
            last_changed_count = changed_count
            last_changed_files = changed_files
        if last_changed_count <= 0:
            last_changed_at = None
        last_sample_files = self._sync_summary_sample_files(last_changed_files, scope_relative_path)

        return WorkspaceSyncSummaryRecord(
            synced_at=synced_at,
            changed_count=changed_count,
            sample_files=sample_files,
            errors=errors,
            last_changed_at=last_changed_at,
            last_changed_count=last_changed_count,
            last_sample_files=last_sample_files,
        )

    def _sync_summary_scope_relative_path(self, knowledge_type: str, target_path: Path | None) -> str:
        if target_path is None:
            return ""

        root_path = self._get_knowledge_root(knowledge_type)
        try:
            relative_path = target_path.resolve().relative_to(root_path)
        except ValueError:
            return ""
        scope_path = relative_path.as_posix()
        return "" if scope_path == "." else scope_path

    def _filter_sync_files(self, knowledge_type: str, file_paths: list[object], scope_relative_path: str) -> list[str]:
        normalized_paths = [
            path
            for path in (self._normalize_sync_file_path(knowledge_type, value) for value in file_paths)
            if path and not self._should_skip_knowledge_relative_path(path)
        ]
        if not scope_relative_path:
            return normalized_paths

        prefix = scope_relative_path.rstrip("/")
        return [path for path in normalized_paths if path == prefix or path.startswith(f"{prefix}/")]

    def _normalize_sync_file_path(self, knowledge_type: str, value: object) -> str:
        path = str(value).strip().replace("\\", "/").strip("/")
        if not path:
            return ""

        root_display_path = self._to_relative_display_path(self._get_knowledge_root(knowledge_type)).strip("/")
        if path == root_display_path:
            return ""
        if path.startswith(f"{root_display_path}/"):
            return path[len(root_display_path) + 1:]
        return path

    @staticmethod
    def _sync_summary_sample_files(file_paths: list[str], scope_relative_path: str) -> list[str]:
        prefix = scope_relative_path.rstrip("/")
        samples: list[str] = []
        for path in file_paths[:3]:
            if prefix and path.startswith(f"{prefix}/"):
                samples.append(path[len(prefix) + 1:])
            else:
                samples.append(path)
        return samples

    def _get_knowledge_root(self, knowledge_type: str) -> Path:
        root_path = self._knowledge_roots.get(knowledge_type)
        if root_path is None:
            raise KeyError(f"未知知识库类型：{knowledge_type}")
        return root_path

    def _build_tree(self, root_path: Path) -> WorkspaceTreeRecord:
        if not root_path.exists() or not root_path.is_dir():
            raise FileNotFoundError("目录不存在。")

        default_file_path: str | None = None

        def build_node(path: Path) -> WorkspaceTreeNode | None:
            nonlocal default_file_path
            if self._should_skip(path):
                return None

            relative_path = self._to_relative_display_path(path)
            if path.is_dir():
                children: list[WorkspaceTreeNode] = []
                for child in sorted(path.iterdir(), key=self._sort_key):
                    child_node = build_node(child)
                    if child_node is not None:
                        children.append(child_node)
                return WorkspaceTreeNode(
                    id=relative_path,
                    kind="folder",
                    name=path.name,
                    path=relative_path,
                    children=children,
                    has_children=bool(children),
                    children_loaded=True,
                    child_count=len(children),
                )

            content_type = self._detect_content_type(path)
            if default_file_path is None:
                default_file_path = relative_path
            return WorkspaceTreeNode(
                id=relative_path,
                kind="file",
                name=path.name,
                path=relative_path,
                children=[],
                has_children=False,
                children_loaded=True,
                updated_at=self._format_mtime(path),
                content_type=content_type,
            )

        root_node = build_node(root_path)
        if root_node is None:
            raise FileNotFoundError("目录不存在。")
        return WorkspaceTreeRecord(root=root_node, default_file_path=default_file_path)

    def _read_file(self, root_path: Path, relative_path: str) -> WorkspaceFileRecord:
        file_path = self._resolve_relative_path(root_path, relative_path)
        if not file_path.exists() or not file_path.is_file():
            raise FileNotFoundError("文件不存在。")

        content_type = self._detect_content_type(file_path)
        content = self._read_file_content(file_path, content_type)
        return WorkspaceFileRecord(
            id=self._to_relative_display_path(file_path),
            name=file_path.name,
            path=self._to_relative_display_path(file_path),
            updated_at=self._format_mtime(file_path),
            content_type=content_type,
            content=content,
        )

    def _resolve_asset(self, root_path: Path, relative_path: str) -> WorkspaceAssetRecord:
        file_path = self._resolve_relative_path(root_path, relative_path)
        if not file_path.exists() or not file_path.is_file():
            raise FileNotFoundError("文件不存在。")

        media_type = guess_type(file_path.name)[0] or "application/octet-stream"
        return WorkspaceAssetRecord(
            path=self._to_relative_display_path(file_path),
            file_path=file_path,
            media_type=media_type,
        )

    @property
    def _skill_import_root(self) -> Path:
        return self._skills_root.parent / ".skill-imports"

    def _build_skill_import_preview_entries(self, package_root: Path) -> list[dict[str, object]]:
        entries: list[dict[str, object]] = []
        for path in sorted(package_root.rglob("*"), key=lambda value: (value.as_posix().lower(), value.is_file())):
            relative_path = path.relative_to(package_root).as_posix()
            entries.append(
                {
                    "path": relative_path,
                    "kind": "folder" if path.is_dir() else "file",
                    "size": path.stat().st_size if path.is_file() else 0,
                    "content_type": None if path.is_dir() else self._detect_content_type(path),
                }
            )
        return entries

    def _build_skill_import_preview(
        self,
        token: str,
        package_root: Path,
        metadata: dict[str, object],
    ) -> dict[str, object]:
        skill_file = package_root / "SKILL.md"
        skill_content = skill_file.read_text(encoding="utf-8", errors="replace") if skill_file.is_file() else ""
        generation_state = self._read_skill_import_generation(package_root.parent, metadata)
        return {
            "token": token,
            "archive_name": str(metadata.get("archive_name") or ""),
            "suggested_folder_name": str(metadata.get("suggested_folder_name") or ""),
            "has_skill_md": skill_file.is_file(),
            "skill_md_content": skill_content,
            "entries": self._build_skill_import_preview_entries(package_root),
            "generation_status": generation_state["status"],
            "generation_message": generation_state["message"],
            "generated_skill_md_content": generation_state["content"],
            "generation_error": generation_state["error"],
            "generation_updated_at": generation_state["updated_at"],
        }

    @staticmethod
    def _read_skill_import_generation(stage_root: Path, metadata: dict[str, object]) -> dict[str, object]:
        state_path = stage_root / "generation.json"
        if state_path.is_file():
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
                if payload.get("status") in {"running", "completed", "failed", "interrupted"}:
                    return {
                        "status": payload["status"],
                        "message": str(payload.get("message") or ""),
                        "content": str(payload.get("content") or ""),
                        "error": str(payload.get("error") or ""),
                        "updated_at": float(payload.get("updated_at") or 0),
                    }
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
        return {
            "status": "not_started" if metadata.get("generation_tracking_version") == 1 else "unknown",
            "message": "",
            "content": "",
            "error": "",
            "updated_at": 0,
        }

    def _load_skill_import(self, token: str, owner_id: str) -> tuple[Path, Path, dict[str, object]]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{16,120}", token):
            raise FileNotFoundError("导入任务不存在。")
        stage_root = self._skill_import_root / token
        package_root = stage_root / "workspace"
        metadata_path = stage_root / "metadata.json"
        if not package_root.is_dir() or not metadata_path.is_file():
            raise FileNotFoundError("导入任务不存在。")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise FileNotFoundError("导入任务已损坏，请重新上传。") from exc
        if metadata.get("owner_id") != owner_id:
            raise FileNotFoundError("导入任务不存在。")
        return stage_root, package_root, metadata

    def _discard_other_skill_imports(self, *, owner_id: str, keep_token: str) -> None:
        root = self._skill_import_root
        if not root.is_dir():
            return
        for stage_root in root.iterdir():
            if stage_root.name == keep_token:
                continue
            metadata_path = stage_root / "metadata.json"
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError):
                continue
            if metadata.get("owner_id") == owner_id:
                shutil.rmtree(stage_root, ignore_errors=True)

    def _resolve_skill_root_for_entry(self, path: Path) -> Path:
        try:
            relative = path.resolve().relative_to(self._skills_root)
        except ValueError as exc:
            raise FileNotFoundError("Skill 路径不合法。") from exc
        if not relative.parts:
            raise FileNotFoundError("不能操作 Skill 根目录。")
        skill_root = self._skills_root / relative.parts[0]
        if not (skill_root / "SKILL.md").is_file():
            raise FileNotFoundError("目标不属于有效的 Skill。")
        return skill_root

    @staticmethod
    def _normalize_skill_entry_name(name: str) -> str:
        normalized = name.strip()
        if not normalized or normalized in {".", ".."} or "/" in normalized or "\\" in normalized:
            raise FileNotFoundError("文件或文件夹名称不合法。")
        if normalized.startswith("."):
            raise FileNotFoundError("不支持创建隐藏文件或文件夹。")
        return normalized

    def _resolve_skill_entry_path(self, skill_dir: Path, relative_path: str) -> Path:
        raw_path = relative_path.strip().replace("\\", "/")
        normalized = raw_path.strip("/")
        if not normalized or raw_path.startswith("/") or normalized.startswith("../") or "/../" in f"/{normalized}/":
            raise FileNotFoundError("文件路径不合法。")

        target_path = (skill_dir / normalized).resolve()
        try:
            target_path.relative_to(skill_dir.resolve())
        except ValueError as exc:
            raise FileNotFoundError("文件路径不合法。") from exc
        return target_path

    def _resolve_relative_path(self, root_path: Path, relative_path: str) -> Path:
        normalized = self._display_path_to_host_relative_path(relative_path)
        if not normalized:
            raise FileNotFoundError("文件不存在。")

        target_path = (self._base_dir / normalized).resolve()
        try:
            target_path.relative_to(root_path)
        except ValueError as exc:
            raise FileNotFoundError("文件不存在。") from exc
        return target_path

    def _to_relative_display_path(self, path: Path) -> str:
        resolved_path = path.resolve()
        relative_path: str | None = None
        for knowledge_type, root_path in self._knowledge_roots.items():
            try:
                knowledge_relative = resolved_path.relative_to(root_path)
            except ValueError:
                continue
            logical_path = self._knowledge_mount_roots[knowledge_type] / knowledge_relative
            relative_path = logical_path.relative_to(self._base_dir).as_posix()
            break
        if relative_path is None:
            relative_path = resolved_path.relative_to(self._base_dir).as_posix()
        parts = relative_path.split("/")
        if len(parts) >= 3 and parts[0] == "workspace" and parts[2] == "knowledge":
            return "/" + "/".join(parts[1:])
        if len(parts) >= 2 and parts[0] == "workspace" and parts[1] == "users":
            return "/me" if len(parts) <= 3 else "/me/" + "/".join(parts[3:])
        if len(parts) >= 4 and parts[:2] == ["workspace", "runtime"] and parts[2] == "containers":
            tmp_index = 4 if len(parts) > 4 and parts[4] == "tmp" else 3
            return "/tmp" if len(parts) <= tmp_index else "/tmp/" + "/".join(parts[tmp_index + 1 :])
        return relative_path

    def _display_path_to_host_relative_path(self, display_path: str) -> str:
        normalized = display_path.strip().replace("\\", "/").strip("/")
        if not normalized:
            return ""
        if normalized.startswith("workspace/"):
            return normalized
        parts = normalized.split("/")
        if len(parts) >= 2 and parts[1] == "knowledge":
            return "workspace/" + normalized
        if parts[0] == "me":
            return "workspace/users/" + "/".join(parts[1:]) if len(parts) > 1 else "workspace/users"
        if parts[0] == "tmp":
            return "workspace/runtime/containers/" + "/".join(parts[1:]) if len(parts) > 1 else "workspace/runtime/containers"
        return normalized

    def _ensure_skill_metadata(
        self,
        content: str,
        *,
        name: str,
        description: str,
        creator_name: str,
        created_at: str,
        action: str,
    ) -> str:
        body = self._strip_frontmatter(content.strip())
        metadata = self._build_skill_frontmatter(
            name=name,
            description=description,
            creator_name=creator_name,
            created_at=created_at,
            updated_at=created_at,
            log_entries=[f"{created_at} {creator_name}: {action}"],
        )
        return f"{metadata}\n\n{body}\n"

    def _record_skill_edit(self, content: str, *, actor_name: str, action: str) -> str:
        today = date.today().isoformat()
        normalized = content.replace("\r\n", "\n").strip()
        match = re.match(r"^---\n([\s\S]*?)\n---(?:\n+|$)", normalized)
        if not match:
            metadata = self._build_skill_frontmatter(
                name="",
                description="",
                creator_name=actor_name,
                created_at=today,
                updated_at=today,
                log_entries=[f"{today} {actor_name}: {action}"],
            )
            return f"{metadata}\n\n{normalized}\n"

        metadata_block = match.group(1)
        body = normalized[match.end():].lstrip("\n")
        lines = metadata_block.split("\n")
        next_lines: list[str] = []
        inserted_log = False
        skipped_updated_at = False
        for line in lines:
            if line.startswith("updated_at:"):
                if not skipped_updated_at:
                    next_lines.append(f"updated_at: {today}")
                    skipped_updated_at = True
                continue
            if line.strip() == "edit_log:":
                next_lines.append(line)
                next_lines.append(f"  - {today} {actor_name}: {action}")
                inserted_log = True
                continue
            next_lines.append(line)

        if not skipped_updated_at:
            next_lines.append(f"updated_at: {today}")
        if not inserted_log:
            next_lines.append("edit_log:")
            next_lines.append(f"  - {today} {actor_name}: {action}")

        return f"---\n{chr(10).join(next_lines)}\n---\n\n{body}\n"

    @staticmethod
    def _build_skill_frontmatter(
        *,
        name: str,
        description: str,
        creator_name: str,
        created_at: str,
        updated_at: str,
        log_entries: list[str],
    ) -> str:
        lines = ["---"]
        if name:
            lines.append(f"name: {name}")
        if description:
            lines.append(f"description: {description}")
        lines.extend(
            [
                f"creator: {creator_name}",
                f"created_at: {created_at}",
                f"updated_at: {updated_at}",
                "edit_log:",
            ]
        )
        lines.extend(f"  - {entry}" for entry in log_entries)
        lines.append("---")
        return "\n".join(lines)

    @staticmethod
    def _strip_frontmatter(content: str) -> str:
        match = re.match(r"^---\n[\s\S]*?\n---(?:\n+|$)", content.replace("\r\n", "\n"))
        if not match:
            return content
        return content[match.end():].lstrip("\n")

    @staticmethod
    def _generate_skill_markdown(*, name: str, description: str, intent: str) -> str:
        intent_text = intent.strip() or description.strip() or "说明这个 Skill 的适用场景、输入要求和输出结构。"
        return "\n".join(
            [
                f"# {name}",
                "",
                "## 使用场景",
                intent_text,
                "",
                "## 输入要求",
                "- 说明用户需要提供哪些文档、代码路径、业务背景或约束条件。",
                "- 如果输入不足，先列出需要澄清的问题。",
                "",
                "## 工作流程",
                "1. 读取并确认用户给出的上下文。",
                "2. 按任务目标拆解分析维度。",
                "3. 输出结论、依据、风险和下一步建议。",
                "",
                "## 输出格式",
                "- 结论",
                "- 关键依据",
                "- 风险与歧义",
                "- 建议行动",
            ]
        )

    @staticmethod
    def _slugify_skill_name(name: str) -> str:
        normalized = name.strip().lower()
        slug = re.sub(r"[^a-z0-9\u4e00-\u9fff_-]+", "-", normalized)
        slug = re.sub(r"-{2,}", "-", slug).strip("-_")
        return slug or "new-skill"

    @staticmethod
    def _derive_skill_name(intent: str, description: str) -> str:
        source = (intent or description).strip()
        if not source:
            return "新 Skill"
        first_line = source.splitlines()[0].strip()
        first_line = re.sub(r"[，。；：,.。;:]+.*$", "", first_line).strip()
        return first_line[:24].strip() or "新 Skill"

    @staticmethod
    def _derive_skill_description(intent: str) -> str:
        source = intent.strip()
        if not source:
            return ""
        return source.replace("\n", " ")[:120].strip()

    def _read_file_content(self, path: Path, content_type: str) -> str:
        if content_type == "binary":
            return "当前文件为二进制内容，暂不支持在线预览。"

        text = path.read_text(encoding="utf-8", errors="replace")
        if len(text) <= self._preview_limit:
            return text
        truncated_text = text[: self._preview_limit]
        return f"{truncated_text}\n\n[内容已截断，仅展示前 {self._preview_limit} 个字符]"

    @staticmethod
    def _index_has_body(path: Path) -> bool:
        """判断 _index.md 在元信息头之后是否有实际内容。"""
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return False
        # 找到 "## 内容" 标记后的部分
        marker = "## 内容"
        idx = text.find(marker)
        if idx >= 0:
            body = text[idx + len(marker):]
        else:
            body = text
        return bool(body.strip())

    def _detect_content_type(self, path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix in {".md", ".mdx"}:
            return "markdown"
        if suffix in self._TEXT_CODE_EXTENSIONS:
            return "code"
        if suffix in self._TEXT_PLAIN_EXTENSIONS:
            return "plain"

        sample = path.read_bytes()[:2048]
        if b"\x00" in sample:
            return "binary"
        if not sample:
            return "plain"
        try:
            sample.decode("utf-8")
        except UnicodeDecodeError:
            return "binary"
        return "plain"

    def _format_mtime(self, path: Path) -> str:
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return modified_at.isoformat()

    def _should_skip_knowledge_path(self, path: Path) -> bool:
        name = path.name
        if self._should_skip(path):
            return True
        if not path.exists():  # 悬挂符号链接
            return True
        if path.is_dir() and name.startswith("_"):
            return True
        if path.is_dir() and name.lower() in self._MEDIA_DIR_NAMES:
            return True
        if path.is_file() and path.suffix.lower() in self._NON_TEXT_EXTENSIONS:
            return True
        return False

    def _should_skip_knowledge_relative_path(self, relative_path: str) -> bool:
        parts = [part for part in relative_path.replace("\\", "/").split("/") if part]
        for part in parts:
            if part in self._HIDDEN_NAMES or part.startswith("."):
                return True
        for part in parts[:-1]:
            if part.startswith("_"):
                return True
            if part.lower() in self._MEDIA_DIR_NAMES:
                return True
        if parts and Path(parts[-1]).suffix.lower() in self._NON_TEXT_EXTENSIONS:
            return True
        return False

    def _should_skip(self, path: Path) -> bool:
        name = path.name
        if name in self._HIDDEN_NAMES:
            return True
        return name.startswith(".")

    @staticmethod
    def _sort_key(path: Path) -> tuple[int, str]:
        return (0 if path.is_dir() else 1, path.name.lower())
