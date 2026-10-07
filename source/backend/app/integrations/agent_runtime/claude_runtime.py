from __future__ import annotations

import importlib
import importlib.metadata
import json
import logging
import os
import shutil
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from app.integrations.agent_runtime.base import discover_project_skills
from app.integrations.agent_runtime.models import (
    AssistantRuntimeError,
    RuntimeDynamicTool,
    RuntimeEvent,
    RuntimeSession,
    RuntimeSkill,
    RuntimeWorkspacePlan,
)

logger = logging.getLogger(__name__)
_STDERR_PLACEHOLDER = "Check stderr output for details"
_MAX_ERROR_LINES = 20
_MAX_ERROR_CHARS = 4000


class ClaudeCodeAgentRuntimeClient:
    provider = "claude_code"
    supports_local_images = False

    def __init__(
        self,
        *,
        model: str,
        max_turns: int,
        permission_mode: str,
        allowed_tools: list[str],
        effort: str | None = None,
        skills_root: str | None = None,
        cli_path: str | None = None,
        api_key: str = "",
        base_url: str = "",
        max_output_tokens: int | None = None,
        runtime_python_venv_path: str | None = None,
    ) -> None:
        self.model = model
        self.max_turns = max(max_turns, 1)
        self.effort = effort
        self.permission_mode = permission_mode
        self.allowed_tools = allowed_tools
        self.skills_root = skills_root
        self.cli_path = cli_path
        self.api_key = api_key
        self.base_url = base_url
        self.max_output_tokens = max_output_tokens
        self.runtime_python_venv_path = runtime_python_venv_path
        self._tool_names_by_use_id: dict[str, str] = {}

    async def create_or_resume_session(
        self,
        *,
        runtime_session_id: str | None,
        working_directory: str | None = None,
        workspace_plan: RuntimeWorkspacePlan | None = None,
        tool_allowlist: tuple[str, ...] | None = None,
        dynamic_tools: list[RuntimeDynamicTool] | None = None,
        dynamic_read_roots: list[str] | None = None,
        system_prompt: str,
    ) -> RuntimeSession:
        if workspace_plan is not None:
            agent_cwd = Path(workspace_plan.host_shadow_root).resolve()
            agent_cwd.mkdir(parents=True, exist_ok=True)
        elif working_directory is not None:
            agent_cwd = self._resolve_agent_cwd(working_directory)
        else:
            raise AssistantRuntimeError("working_directory 或 workspace_plan 至少需要提供一个。")
        return RuntimeSession(
            provider=self.provider,
            session_id=runtime_session_id,
            working_directory=str(agent_cwd),
            system_prompt=system_prompt,
            workspace_plan=workspace_plan,
            tool_allowlist=tool_allowlist,
            dynamic_tools=list(dynamic_tools or []),
            dynamic_read_roots=list(dynamic_read_roots or []),
        )

    async def send_message_stream(
        self,
        *,
        session: RuntimeSession,
        message: str,
        metadata: dict[str, Any],
    ) -> AsyncIterator[RuntimeEvent]:
        sdk = self._load_sdk()
        stderr_lines: list[str] = []

        def handle_stderr(line: str) -> None:
            normalized = line.strip()
            if not normalized:
                return
            stderr_lines.append(normalized)
            if len(stderr_lines) > _MAX_ERROR_LINES:
                del stderr_lines[:-_MAX_ERROR_LINES]

        subprocess_env = self._subprocess_env(session)

        options = sdk["ClaudeAgentOptions"](
            system_prompt=str(metadata.get("system_prompt") or session.system_prompt or ""),
            cwd=Path(session.working_directory),
            model=self.model,
            max_turns=self.max_turns,
            effort=self.effort,
            permission_mode=self.permission_mode,
            allowed_tools=self._allowed_tools(session),
            disallowed_tools=self._disallowed_tools(session),
            mcp_servers=self._mcp_servers(sdk, session),
            resume=session.session_id,
            continue_conversation=False,
            cli_path=self.cli_path,
            setting_sources=["project"],
            settings=self._settings_for_session(session),
            sandbox={
                "enabled": True,
                "failIfUnavailable": True,
                "autoAllowBashIfSandboxed": self._auto_allow_bash_if_sandboxed(session),
                "allowUnsandboxedCommands": False,
                "filesystem": self._sandbox_filesystem(session),
                "network": {
                    "allowAllUnixSockets": False,
                    "allowLocalBinding": False,
                },
            },
            stderr=handle_stderr,
            env=subprocess_env,
        )

        yield RuntimeEvent(
            "session",
            {
                "provider": self.provider,
                "session_id": session.session_id,
                "working_directory": session.working_directory,
                "organization_key": session.workspace_plan.organization_key if session.workspace_plan else None,
            },
        )

        try:
            async for raw_message in sdk["query"](prompt=message, options=options):
                for event in self._convert_raw_message(raw_message):
                    self._raise_if_permission_approval_required(event)
                    yield event
        except Exception as exc:
            raise self._normalize_runtime_exception(exc, stderr_lines) from exc

    async def list_available_skills(
        self,
        *,
        working_directory: str,
    ) -> list[RuntimeSkill]:
        fallback_root = Path(working_directory)
        if fallback_root.name == "workspace":
            fallback_root = fallback_root / ".claude/skills"
        else:
            fallback_root = fallback_root / "workspace/.claude/skills"
        return discover_project_skills(self.skills_root or str(fallback_root))

    def _resolve_agent_cwd(self, working_directory: str) -> Path:
        base_path = Path(working_directory).resolve()
        if base_path.name == "workspace":
            return base_path
        workspace_root = base_path / "workspace"
        workspace_root.mkdir(parents=True, exist_ok=True)
        return workspace_root

    def _sandbox_allow_read(self, session: RuntimeSession) -> list[str]:
        if session.workspace_plan is None:
            return sorted(set([".", *self._runtime_python_read_paths()]))
        shadow_root = Path(session.working_directory).resolve()
        allow_read = [str(shadow_root)]
        for mount in session.workspace_plan.mounts:
            allow_read.append(str(Path(mount.host_path).resolve()))
        allow_read.extend(str(Path(path).resolve()) for path in session.dynamic_read_roots)
        allow_read.extend(self._runtime_python_read_paths())
        return sorted(set(allow_read))

    def _sandbox_allow_write(self, session: RuntimeSession) -> list[str]:
        if session.workspace_plan is None:
            return []
        allow_write: list[str] = []
        for mount in session.workspace_plan.mounts:
            if mount.permission != "write":
                continue
            allow_write.append(str(Path(mount.host_path).resolve()))
        return sorted(set(allow_write))

    def _sandbox_filesystem(self, session: RuntimeSession) -> dict[str, object]:
        filesystem: dict[str, object] = {
            "denyWrite": ["."],
            "denyRead": ["~/"],
            "allowRead": self._sandbox_allow_read(session),
        }
        allow_write = self._sandbox_allow_write(session)
        if allow_write:
            filesystem["allowWrite"] = allow_write
        return filesystem

    def _auto_allow_bash_if_sandboxed(self, session: RuntimeSession) -> bool:
        return session.workspace_plan is not None and self._tool_is_allowed(session, "Bash")

    def _subprocess_env(self, session: RuntimeSession) -> dict[str, str]:
        subprocess_env: dict[str, str] = {}
        if self.api_key:
            subprocess_env["ANTHROPIC_API_KEY"] = self.api_key
        if self.base_url:
            subprocess_env["ANTHROPIC_BASE_URL"] = self.base_url
        if self.max_output_tokens is not None and self.max_output_tokens > 0:
            subprocess_env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(self.max_output_tokens)
        subprocess_env.update(self._runtime_python_env())
        subprocess_env.update(self._workspace_temp_env(session))
        return subprocess_env

    def _runtime_python_env(self) -> dict[str, str]:
        if not self.runtime_python_venv_path:
            return {}
        venv_root = Path(self.runtime_python_venv_path).expanduser().resolve()
        bin_dir = venv_root / "bin"
        python_bin = bin_dir / "python"
        if not python_bin.exists():
            logger.warning("Assistant Runtime Python venv 不存在或不完整：%s", venv_root)
            return {}
        current_path = os.environ.get("PATH", "")
        return {
            "PATH": f"{bin_dir}{os.pathsep}{current_path}" if current_path else str(bin_dir),
            "VIRTUAL_ENV": str(venv_root),
            "PYTHONNOUSERSITE": "1",
        }

    def _runtime_python_read_paths(self) -> list[str]:
        if not self.runtime_python_venv_path:
            return []
        venv_root = Path(self.runtime_python_venv_path).expanduser().resolve()
        if not (venv_root / "bin" / "python").exists():
            return []
        return [str(venv_root)]

    def _workspace_temp_env(self, session: RuntimeSession) -> dict[str, str]:
        if session.workspace_plan is None:
            return {}
        for mount in session.workspace_plan.mounts:
            if mount.permission != "write" or mount.sandbox_path.strip("/") != "tmp":
                continue
            tmp_root = Path(mount.host_path).resolve()
            tmp_root.mkdir(parents=True, exist_ok=True)
            cache_root = tmp_root / ".cache"
            texmf_var_root = cache_root / "texmf-var"
            texmf_config_root = cache_root / "texmf-config"
            texmf_var_root.mkdir(parents=True, exist_ok=True)
            texmf_config_root.mkdir(parents=True, exist_ok=True)
            return {
                "XDG_CACHE_HOME": str(cache_root),
                "TEXMFVAR": str(texmf_var_root),
                "TEXMFCONFIG": str(texmf_config_root),
            }
        return {}

    def _allowed_tools(self, session: RuntimeSession) -> list[str]:
        tools = list(self.allowed_tools)
        if session.tool_allowlist is not None:
            tools = [tool for tool in tools if self._tool_is_allowed(session, tool)]
            for tool in session.tool_allowlist:
                if tool not in tools:
                    tools.append(tool)
        if session.workspace_plan is not None:
            for tool in ("Bash", "Edit", "Write"):
                if not self._tool_is_allowed(session, tool):
                    continue
                if tool not in tools:
                    tools.append(tool)
        for dynamic_tool in session.dynamic_tools:
            tool_name = f"mcp__ai_prd__{dynamic_tool.name}"
            if tool_name not in tools:
                tools.append(tool_name)
        return tools

    @staticmethod
    def _mcp_servers(sdk: dict[str, Any], session: RuntimeSession) -> dict[str, object]:
        if not session.dynamic_tools:
            return {}
        sdk_tools = []
        for dynamic_tool in session.dynamic_tools:
            async def invoke(arguments, tool=dynamic_tool):
                result = await tool.handler(arguments if isinstance(arguments, dict) else {})
                return {
                    "content": [{"type": "text", "text": result.content}],
                    "isError": result.is_error,
                }

            sdk_tools.append(
                sdk["tool"](
                    dynamic_tool.name,
                    dynamic_tool.description,
                    dynamic_tool.input_schema,
                )(invoke)
            )
        return {
            "ai_prd": sdk["create_sdk_mcp_server"](
                name="ai_prd",
                version="1.0.0",
                tools=sdk_tools,
            )
        }

    def _disallowed_tools(self, session: RuntimeSession) -> list[str]:
        has_write_access = bool(self._sandbox_allow_write(session))
        tools: list[str] = []
        for tool in ("Bash", "Edit", "Write"):
            lacks_permission = tool in {"Edit", "Write"} and not has_write_access
            if lacks_permission or not self._tool_is_allowed(session, tool):
                tools.append(tool)
        return tools

    @staticmethod
    def _tool_is_allowed(session: RuntimeSession, tool: str) -> bool:
        if session.tool_allowlist is None:
            return True
        tool_name = tool.split("(", 1)[0]
        return tool in session.tool_allowlist or tool_name in session.tool_allowlist

    def _settings_for_session(self, session: RuntimeSession) -> str | None:
        if session.workspace_plan is None:
            return None
        read_patterns = [
            self._permission_path_pattern(path)
            for path in self._sandbox_allow_read(session)
        ]
        read_patterns.extend(self._workspace_permission_patterns(session, writable_only=False))
        allow_rules = [f"Read({pattern})" for pattern in read_patterns]
        write_patterns = [
            self._permission_path_pattern(path)
            for path in self._sandbox_allow_write(session)
        ]
        write_patterns.extend(self._workspace_permission_patterns(session, writable_only=True))
        for pattern in write_patterns:
            allow_rules.append(f"Edit({pattern})")
            allow_rules.append(f"Write({pattern})")
        return json.dumps(
            {
                "permissions": {
                    "allow": sorted(set(allow_rules)),
                }
            },
            ensure_ascii=False,
        )

    def _permission_path_pattern(self, value: str) -> str:
        path = Path(value).resolve().as_posix().rstrip("/")
        return f"{path}/**"

    def _workspace_permission_patterns(self, session: RuntimeSession, *, writable_only: bool) -> list[str]:
        if session.workspace_plan is None:
            return []
        patterns: list[str] = []
        for mount in session.workspace_plan.mounts:
            if writable_only and mount.permission != "write":
                continue
            clean_path = mount.sandbox_path.strip("/")
            if not clean_path:
                continue
            patterns.append(f"{clean_path}/**")
            patterns.append(f"/{clean_path}/**")
        return sorted(set(patterns))

    def _raise_if_permission_approval_required(self, event: RuntimeEvent) -> None:
        if event.type != "tool_result" or not event.data.get("is_error"):
            return
        content = str(event.data.get("content") or "")
        normalized = " ".join(content.lower().split())
        approval_markers = (
            "requires approval",
            "requested permissions",
            "haven't granted",
            "permission required",
        )
        if not any(marker in normalized for marker in approval_markers):
            return
        tool_name = str(event.data.get("tool_name") or "工具")
        raise AssistantRuntimeError(f"{tool_name} 自动审批未通过，已直接终止本轮执行。")

    def _load_sdk(self) -> dict[str, Any]:
        resolved_cli = ensure_claude_cli_available(self.cli_path)
        logger.debug("Using Claude Code CLI at %s", resolved_cli)

        try:
            module = importlib.import_module("claude_agent_sdk")
        except ImportError as exc:
            raise AssistantRuntimeError(
                "未安装 Claude Agent SDK。请先安装 Python 包 `claude-agent-sdk`，"
                "并安装 Claude Code CLI：`npm install -g @anthropic-ai/claude-code`。"
            ) from exc

        query = getattr(module, "query", None)
        options = getattr(module, "ClaudeAgentOptions", None)
        create_sdk_mcp_server = getattr(module, "create_sdk_mcp_server", None)
        tool = getattr(module, "tool", None)
        if all(value is not None for value in (query, options, create_sdk_mcp_server, tool)):
            return {
                "query": query,
                "ClaudeAgentOptions": options,
                "create_sdk_mcp_server": create_sdk_mcp_server,
                "tool": tool,
            }

        raise AssistantRuntimeError(
            "`claude_agent_sdk` 已安装，但缺少 Runtime 或动态工具所需导出。"
            "请检查当前安装版本是否与项目依赖一致。"
        )

    def _convert_raw_message(self, message: object) -> list[RuntimeEvent]:
        events: list[RuntimeEvent] = []
        session_id = getattr(message, "session_id", None)
        type_name = type(message).__name__

        if type_name == "ResultMessage":
            result = str(getattr(message, "result", "") or "").strip()
            if result:
                events.append(RuntimeEvent("message", {"content": result, "session_id": session_id}))
            usage = {
                "duration_ms": getattr(message, "duration_ms", None),
                "duration_api_ms": getattr(message, "duration_api_ms", None),
                "total_cost_usd": getattr(message, "total_cost_usd", None),
                "num_turns": getattr(message, "num_turns", None),
                "usage": getattr(message, "usage", None),
                "model_usage": getattr(message, "model_usage", None),
            }
            usage = {key: value for key, value in usage.items() if value is not None}
            if usage:
                events.append(RuntimeEvent("usage", usage))
            events.append(
                RuntimeEvent(
                    "complete",
                    {
                        "session_id": session_id,
                        "result": result,
                    },
                )
            )
            return events

        if type_name == "RateLimitEvent":
            rate_limit_info = self._normalize_data(getattr(message, "rate_limit_info", None))
            status = rate_limit_info.get("status")
            events.append(
                RuntimeEvent(
                    "usage",
                    {
                        "rate_limit": rate_limit_info,
                        "session_id": session_id,
                    },
                )
            )
            if status:
                events.append(
                    RuntimeEvent(
                        "activity",
                        {
                            "message": f"Claude 速率状态：{status}",
                            "session_id": session_id,
                        },
                    )
                )
            return events

        if type_name == "StreamEvent":
            event_payload = self._normalize_data(getattr(message, "event", None))
            event_type = str(event_payload.get("type") or event_payload.get("subtype") or "stream_event")

            if event_type == "content_block_delta":
                delta = event_payload.get("delta") or {}
                if isinstance(delta, dict) and delta.get("type") == "text_delta":
                    text = str(delta.get("text") or "")
                    if text:
                        return [RuntimeEvent("delta", {"text": text, "session_id": session_id})]
                return []

            return [
                RuntimeEvent(
                    "activity",
                    {
                        "message": f"Runtime 事件：{event_type}",
                        "event": event_payload,
                        "session_id": session_id,
                    },
                )
            ]

        content = getattr(message, "content", None)
        if not content:
            return events

        if type_name == "UserMessage":
            if isinstance(content, list):
                for block in content:
                    if type(block).__name__ == "ToolResultBlock" or hasattr(block, "tool_use_id"):
                        content_text = self._extract_block_text(block)
                        tool_name = self._tool_result_name(block)
                        events.append(
                            RuntimeEvent(
                                "tool_result",
                                {
                                    "tool_name": tool_name,
                                    "tool_use_id": getattr(block, "tool_use_id", None),
                                    "content": content_text,
                                    "is_error": bool(getattr(block, "is_error", False)),
                                },
                            )
                        )
            return events

        if type_name != "AssistantMessage":
            return events

        for block in content:
            block_type = type(block).__name__
            if block_type == "ThinkingBlock" or hasattr(block, "thinking"):
                thinking = str(getattr(block, "thinking", "") or "")
                events.append(
                    RuntimeEvent(
                        "activity",
                        {
                            "message": "Claude 正在规划回答内容。",
                            "kind": "thinking",
                            "session_id": session_id,
                            "thinking_chars": len(thinking),
                        },
                    )
                )
                continue

            if hasattr(block, "text"):
                text = str(getattr(block, "text", "") or "")
                if text:
                    events.append(RuntimeEvent("delta", {"text": text, "session_id": session_id}))
                continue

            if block_type == "ToolUseBlock" or hasattr(block, "name"):
                tool_name = str(getattr(block, "name", "tool"))
                tool_input = self._normalize_data(getattr(block, "input", None))
                self._record_tool_use_name(getattr(block, "id", None), tool_name)
                events.append(
                    RuntimeEvent(
                        "tool_use",
                        {
                            "tool_name": tool_name,
                            "tool_input": tool_input,
                            "tool_use_id": getattr(block, "id", None),
                        },
                    )
                )
                events.extend(self._derive_tool_side_events(tool_name, tool_input))
                continue

            if block_type == "ToolResultBlock" or hasattr(block, "tool_use_id"):
                content_text = self._extract_block_text(block)
                tool_name = self._tool_result_name(block)
                events.append(
                    RuntimeEvent(
                        "tool_result",
                        {
                            "tool_name": tool_name,
                            "tool_use_id": getattr(block, "tool_use_id", None),
                            "content": content_text,
                            "is_error": bool(getattr(block, "is_error", False)),
                        },
                    )
                )

        return events

    def _record_tool_use_name(self, tool_use_id: object, tool_name: str) -> None:
        if tool_use_id is None:
            return
        normalized_id = str(tool_use_id).strip()
        if normalized_id:
            self._tool_names_by_use_id[normalized_id] = tool_name

    def _tool_result_name(self, block: object) -> str | None:
        raw_name = getattr(block, "name", None)
        if raw_name:
            return str(raw_name)
        tool_use_id = getattr(block, "tool_use_id", None)
        if tool_use_id is None:
            return None
        return self._tool_names_by_use_id.get(str(tool_use_id).strip())

    def _derive_tool_side_events(self, tool_name: str, tool_input: dict[str, Any]) -> list[RuntimeEvent]:
        normalized_name = tool_name.lower()
        events: list[RuntimeEvent] = []

        if normalized_name in {"read", "view"}:
            path = str(
                tool_input.get("file_path")
                or tool_input.get("path")
                or tool_input.get("target_file")
                or ""
            ).strip()
            if path:
                read_event = RuntimeEvent("read", {"path": path})
                events.append(read_event)
                if "/.claude/skills/" in path or path.endswith("/SKILL.md"):
                    events.append(
                        RuntimeEvent(
                            "skill_use",
                            {
                                "skill_id": path,
                                "skill_name": Path(path).parent.name,
                                "path": path,
                            },
                        )
                    )

        if normalized_name in {"glob", "grep", "websearch", "ls"}:
            query = str(
                tool_input.get("pattern")
                or tool_input.get("query")
                or tool_input.get("path")
                or tool_input.get("dir")
                or ""
            ).strip()
            if query:
                events.append(RuntimeEvent("search", {"query": query, "tool_name": tool_name}))

        if normalized_name == "skill":
            skill_name = str(tool_input.get("skill") or tool_input.get("name") or "Skill").strip()
            events.append(
                RuntimeEvent(
                    "skill_use",
                    {
                        "skill_id": skill_name or "Skill",
                        "skill_name": skill_name or "Skill",
                    },
                )
            )

        return events

    def _extract_block_text(self, block: object) -> str | None:
        raw_content = getattr(block, "content", None)
        if raw_content is None:
            return None
        if isinstance(raw_content, str):
            return raw_content
        if isinstance(raw_content, list):
            parts: list[str] = []
            for item in raw_content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
            combined = "\n".join(part for part in parts if part)
            return combined or None
        return str(raw_content)

    def _normalize_data(self, value: object) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        if value is None:
            return {}
        if hasattr(value, "model_dump"):
            dumped = value.model_dump()
            if isinstance(dumped, dict):
                return dumped
        if hasattr(value, "__dict__"):
            return dict(vars(value))
        return {"value": value}

    def _normalize_runtime_exception(
        self,
        exc: Exception,
        stderr_lines: list[str] | None = None,
    ) -> AssistantRuntimeError:
        message = str(exc).strip() or exc.__class__.__name__
        stderr_output = self._extract_runtime_stderr(exc, stderr_lines or [])
        if stderr_output:
            base_message = self._strip_error_output(message)
            message = f"{base_message}\nError output: {stderr_output}"
        else:
            message = self._strip_error_output(message)
        return AssistantRuntimeError(message)

    def _extract_runtime_stderr(self, exc: Exception, stderr_lines: list[str]) -> str | None:
        candidates: list[str] = []
        seen_objects: set[int] = set()
        current: BaseException | None = exc

        while current is not None and id(current) not in seen_objects:
            seen_objects.add(id(current))
            stderr_value = getattr(current, "stderr", None)
            if isinstance(stderr_value, str):
                normalized = stderr_value.strip()
                if normalized and normalized != _STDERR_PLACEHOLDER:
                    candidates.append(normalized)
            current = getattr(current, "__cause__", None) or getattr(current, "__context__", None)

        if stderr_lines:
            candidates.append("\n".join(stderr_lines))

        for candidate in candidates:
            normalized = candidate.strip()
            if normalized:
                if len(normalized) > _MAX_ERROR_CHARS:
                    return normalized[-_MAX_ERROR_CHARS:]
                return normalized
        return None

    def _strip_error_output(self, message: str) -> str:
        normalized = message.strip()
        if "\nError output:" in normalized:
            normalized = normalized.split("\nError output:", 1)[0].strip()
        return normalized


def perform_claude_runtime_startup_check(cli_path: str | None = None) -> dict[str, object]:
    report: dict[str, object] = {
        "provider": "claude_code",
        "sdk_importable": False,
        "sdk_version": None,
        "cli_found": False,
        "cli_path": None,
        "cli_version": None,
    }

    try:
        importlib.import_module("claude_agent_sdk")
        report["sdk_importable"] = True
        report["sdk_version"] = importlib.metadata.version("claude-agent-sdk")
    except ImportError:
        report["sdk_importable"] = False
    except importlib.metadata.PackageNotFoundError:
        report["sdk_importable"] = True

    try:
        resolved_cli = resolve_claude_cli_path(cli_path)
    except AssistantRuntimeError:
        resolved_cli = None

    if resolved_cli is not None:
        report["cli_found"] = True
        report["cli_path"] = resolved_cli
        report["cli_version"] = read_claude_cli_version(resolved_cli)

    return report


def ensure_claude_cli_available(cli_path: str | None = None) -> str:
    resolved_cli = resolve_claude_cli_path(cli_path)
    _prepend_cli_directory_to_path(resolved_cli)
    return resolved_cli


def resolve_claude_cli_path(cli_path: str | None = None) -> str:
    if cli_path:
        configured_path = Path(cli_path).expanduser()
        if _is_executable_file(configured_path):
            return str(configured_path)
        raise AssistantRuntimeError(f"AI_CLI_PATH 指向的 Claude Code CLI 不可用：{cli_path}")

    candidates: list[Path] = []
    which_path = shutil.which("claude")
    if which_path:
        candidates.append(Path(which_path))

    candidates.extend(
        [
            Path.home() / ".claude" / "local" / "claude",
            Path("/opt/homebrew/bin/claude"),
            Path("/usr/local/bin/claude"),
            Path.home() / ".local" / "bin" / "claude",
            Path.home() / "bin" / "claude",
        ]
    )

    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.expanduser()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        if _is_executable_file(resolved):
            return str(resolved)

    raise AssistantRuntimeError(
        "未找到 Claude Code CLI。请安装 `@anthropic-ai/claude-code`，"
        "或通过 `AI_CLI_PATH` 显式指定 `claude` 可执行文件路径。"
    )


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _prepend_cli_directory_to_path(cli_path: str) -> None:
    cli_dir = str(Path(cli_path).resolve().parent)
    current_path = os.environ.get("PATH", "")
    path_parts = current_path.split(":") if current_path else []
    if path_parts and path_parts[0] == cli_dir:
        return
    filtered = [part for part in path_parts if part and part != cli_dir]
    os.environ["PATH"] = ":".join([cli_dir, *filtered]) if filtered else cli_dir


def read_claude_cli_version(cli_path: str) -> str | None:
    try:
        result = subprocess.run(
            [cli_path, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None

    output = (result.stdout or result.stderr or "").strip()
    return output or None
