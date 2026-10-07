from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

from app.integrations.agent_runtime.base import discover_project_skills
from app.integrations.agent_runtime.codex_app_server_client import CodexAppServerClient
from app.integrations.agent_runtime.mcp_approvals import mcp_approval_policy, messages_with_mcp_approvals
from app.integrations.agent_runtime.models import (
    AssistantRuntimeError,
    RuntimeDynamicTool,
    RuntimeEvent,
    RuntimeSession,
    RuntimeSkill,
    RuntimeWorkspacePlan,
)

_CODEX_RESPONSE_STYLE_INSTRUCTIONS = """
回答呈现规范：
- 最终回答只保留面向用户的结果，不复述执行计划。
- 先用一两句概览，再按问题逻辑展开，不按检索或文件顺序罗列。
- 每个主要分区用 `##` 且只回答一个问题；对象、流程、规则和实现分开说明，`###` 只作节内细分。
- 先用易懂语言说明含义，再给示例、公式或技术依据；路径和引用置于相关结论之后。
- 按关系选择形式：过程用步骤，比较或映射用表格；普通说明和流程不用代码块。
- 每段或每条只讲一个重点；信息多时先讲主干，再补规则、例外和不确定项。

命令输出约束：
- 搜索知识库或代码时必须先缩小目录和文件范围，再读取具体文件；禁止对整个知识库执行无上限的全文输出。
- `rg`、`find`、`git` 等可能产生大量输出的命令，必须使用明确的数量限制，例如 `head -n 120`、`sed -n`、限定文件或限定目录。
- 单次命令输出应控制在 120 行以内；如果结果较多，分批读取并在每批后归纳，不要一次返回全部匹配正文。
- 不要使用会把大量上下文行一起展开的全库 `rg -C`；需要上下文时，先定位少量命中文件，再按具体行号读取。
""".strip()


class CodexAgentRuntimeClient:
    """基于 ``codex app-server`` 的 Codex runtime adapter。

    每次 turn 启动一个本地 app-server stdio 进程，通过 ``thread/start`` 或
    ``thread/resume`` 获取会话，再用 ``turn/start`` 执行并消费流式通知。
    """

    provider = "codex"
    supports_local_images = True

    def __init__(
        self,
        *,
        model: str = "gpt-5.6-terra",
        effort: str = "medium",
        sandbox: str = "auto",
        writable_roots: list[str] | None = None,
        skills_root: str | None = None,
        cli_path: str | None = None,
        api_key: str = "",
        client_factory: Callable[[str, dict[str, str]], CodexAppServerClient] | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        if sandbox not in {"auto", "read-only", "workspace-write", "danger-full-access"}:
            raise AssistantRuntimeError(f"不支持的 Codex sandbox：{sandbox}")
        self.sandbox = sandbox
        self.writable_roots = list(writable_roots or [])
        self.skills_root = skills_root
        self.cli_path = cli_path
        self.api_key = api_key
        self.client_factory = client_factory

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
            agent_cwd, _writable_roots = self._resolve_write_policy(working_directory)
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
        cli = resolve_codex_cli_path(self.cli_path)
        client = self._create_client(cli)
        command_cwd, writable_roots = self._resolve_session_write_policy(session)
        sandbox = self._effective_sandbox(writable_roots)
        system_prompt = str(metadata.get("system_prompt") or session.system_prompt or "").strip()
        reply_parts: list[str] = []
        session_id = session.session_id or ""
        delta_item_ids: set[str] = set()
        message_phases: dict[str, str] = {}
        pending_message_deltas: dict[str, list[str]] = {}
        runtime_title = ""
        app_messages = None

        try:
            await client.initialize()
            if session.session_id:
                thread_result = await client.request(
                    "thread/resume",
                    self._thread_resume_params(
                        session_id=session.session_id,
                        cwd=command_cwd,
                        sandbox=sandbox,
                        system_prompt=system_prompt,
                    ),
                )
            else:
                thread_result = await client.request(
                    "thread/start",
                    self._thread_start_params(
                        cwd=command_cwd,
                        sandbox=sandbox,
                        system_prompt=system_prompt,
                        dynamic_tools=session.dynamic_tools,
                    ),
                )

            thread = thread_result.get("thread") or {}
            session_id = str(thread.get("id") or thread_result.get("threadId") or "")
            if not session_id:
                raise AssistantRuntimeError("codex app-server 未返回 thread id。")
            runtime_title = str(thread.get("name") or "")

            yield RuntimeEvent(
                "session",
                {
                    "provider": self.provider,
                    "session_id": session_id,
                    "working_directory": str(thread.get("cwd") or command_cwd),
                    "title": runtime_title or None,
                    "organization_key": session.workspace_plan.organization_key if session.workspace_plan else None,
                },
            )

            turn_result = await client.request(
                "turn/start",
                self._turn_start_params(
                    thread_id=session_id,
                    cwd=command_cwd,
                    writable_roots=writable_roots,
                    message=message,
                    metadata=metadata,
                ),
            )
            turn = turn_result.get("turn") or {}
            turn_id = str(turn.get("id") or turn_result.get("turnId") or "")
            if not turn_id:
                raise AssistantRuntimeError("codex app-server 未返回 turn id。")

            app_messages = messages_with_mcp_approvals(
                client,
                thread_id=session_id,
                turn_id=turn_id,
                register=metadata.get("mcp_approval_register"),
                expire=metadata.get("mcp_approval_expire"),
            )
            async for app_server_message in app_messages:
                if isinstance(app_server_message, RuntimeEvent):
                    yield app_server_message
                    continue
                if app_server_message.get("id") is not None:
                    request_method = str(app_server_message.get("method") or "")
                    if request_method == "item/tool/call":
                        await self._handle_dynamic_tool_call(
                            client=client,
                            request_id=app_server_message.get("id"),
                            params=app_server_message.get("params") or {},
                            dynamic_tools=session.dynamic_tools,
                        )
                        continue
                    await client.respond_error(
                        app_server_message.get("id"),
                        f"ai-prd 当前不支持 app-server 交互请求：{request_method}",
                    )
                    raise AssistantRuntimeError(
                        f"Codex 请求了当前 runtime 不支持的交互：{request_method}"
                    )

                method = str(app_server_message.get("method") or "")
                params = app_server_message.get("params") or {}
                if method == "thread/started":
                    continue
                if method == "thread/name/updated":
                    title = str(params.get("threadName") or "")
                    if title:
                        runtime_title = title
                        yield RuntimeEvent(
                            "session",
                            {
                                "provider": self.provider,
                                "session_id": str(params.get("threadId") or session_id),
                                "working_directory": command_cwd,
                                "title": title,
                            },
                        )
                    continue
                if method == "turn/started":
                    yield RuntimeEvent(
                        "activity",
                        {
                            "message": "Codex app-server 开始处理任务",
                            "session_id": session_id,
                        },
                    )
                    continue
                if method == "item/agentMessage/delta":
                    item_id = str(params.get("itemId") or "")
                    if item_id:
                        delta_item_ids.add(item_id)
                    text = str(params.get("delta") or "")
                    if text:
                        phase = message_phases.get(item_id, "")
                        if phase == "final_answer":
                            reply_parts.append(text)
                            yield RuntimeEvent("delta", {"text": text, "session_id": session_id})
                        elif phase != "commentary":
                            pending_message_deltas.setdefault(item_id, []).append(text)
                    continue
                if method in {"item/started", "item/completed"}:
                    lifecycle = "started" if method == "item/started" else "completed"
                    item = params.get("item") or {}
                    if item.get("type") == "agentMessage":
                        item_id = str(item.get("id") or "")
                        phase = str(item.get("phase") or message_phases.get(item_id) or "")
                        if item_id and phase:
                            message_phases[item_id] = phase
                        if lifecycle == "started":
                            if phase == "commentary":
                                pending_message_deltas.pop(item_id, None)
                            elif phase == "final_answer":
                                pending_parts = pending_message_deltas.pop(item_id, [])
                                for text in pending_parts:
                                    reply_parts.append(text)
                                    yield RuntimeEvent("delta", {"text": text, "session_id": session_id})
                            continue
                        pending_parts = pending_message_deltas.pop(item_id, [])
                        if phase != "commentary":
                            if pending_parts:
                                for text in pending_parts:
                                    reply_parts.append(text)
                                    yield RuntimeEvent("delta", {"text": text, "session_id": session_id})
                            elif item_id not in delta_item_ids:
                                text = str(item.get("text") or "")
                                if text:
                                    reply_parts.append(text)
                                    yield RuntimeEvent("delta", {"text": text, "session_id": session_id})
                        continue
                    for event in self._convert_item(item, lifecycle, session_id):
                        if event.type == "delta":
                            reply_parts.append(str(event.data.get("text") or ""))
                        yield event
                    continue
                if method in {"item/commandExecution/outputDelta", "item/fileChange/outputDelta"}:
                    continue
                if method == "thread/tokenUsage/updated":
                    yield RuntimeEvent(
                        "usage",
                        {
                            "usage": params.get("tokenUsage") or params.get("usage") or params,
                            "session_id": session_id,
                        },
                    )
                    continue
                if method == "turn/completed":
                    status = str((params.get("turn") or {}).get("status") or params.get("status") or "completed")
                    if status == "failed":
                        raise AssistantRuntimeError(self._turn_error(params))
                    for pending_parts in pending_message_deltas.values():
                        for text in pending_parts:
                            reply_parts.append(text)
                            yield RuntimeEvent("delta", {"text": text, "session_id": session_id})
                    pending_message_deltas.clear()
                    usage = params.get("usage") or (params.get("turn") or {}).get("usage")
                    if usage:
                        yield RuntimeEvent("usage", {"usage": usage, "session_id": session_id})
                    break
                if method in {"turn/failed", "error"}:
                    raise AssistantRuntimeError(self._turn_error(params))

            try:
                thread_read = await client.request(
                    "thread/read",
                    {"threadId": session_id, "includeTurns": False},
                )
                settled_thread = thread_read.get("thread") or {}
                settled_title = str(settled_thread.get("name") or "")
                if settled_title and settled_title != runtime_title:
                    yield RuntimeEvent(
                        "session",
                        {
                            "provider": self.provider,
                            "session_id": session_id,
                            "working_directory": str(settled_thread.get("cwd") or command_cwd),
                            "title": settled_title,
                        },
                    )
            except Exception:
                pass

            full_reply = "".join(reply_parts).strip()
            if full_reply:
                yield RuntimeEvent("message", {"content": full_reply, "session_id": session_id})
            yield RuntimeEvent("complete", {"session_id": session_id, "result": full_reply})
        except AssistantRuntimeError:
            raise
        except Exception as exc:
            raise AssistantRuntimeError(str(exc) or "Codex app-server 执行失败") from exc
        finally:
            try:
                if app_messages is not None:
                    await app_messages.aclose()
            finally:
                await client.close()

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

    def _create_client(self, cli: str) -> CodexAppServerClient:
        env = self._build_env()
        if self.client_factory is not None:
            return self.client_factory(cli, env)
        return CodexAppServerClient(cli_path=cli, env=env)

    def _thread_start_params(
        self,
        *,
        cwd: str,
        sandbox: str,
        system_prompt: str,
        dynamic_tools: list[RuntimeDynamicTool] | None = None,
    ) -> dict[str, Any]:
        params = {
            "model": self.model,
            "cwd": cwd,
            "approvalPolicy": mcp_approval_policy(),
            "approvalsReviewer": "user",
            "sandbox": sandbox,
            "developerInstructions": self._developer_instructions(system_prompt),
            "serviceName": "ai_prd",
        }
        if dynamic_tools:
            params["dynamicTools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "inputSchema": tool.input_schema,
                }
                for tool in dynamic_tools
            ]
        return params

    @staticmethod
    async def _handle_dynamic_tool_call(
        *,
        client: CodexAppServerClient,
        request_id: object,
        params: dict[str, Any],
        dynamic_tools: list[RuntimeDynamicTool],
    ) -> None:
        tool_name = str(params.get("tool") or "")
        dynamic_tool = next((tool for tool in dynamic_tools if tool.name == tool_name), None)
        if dynamic_tool is None:
            await client.respond_result(
                request_id,
                {
                    "success": False,
                    "contentItems": [{"type": "inputText", "text": f"未知动态工具：{tool_name}"}],
                },
            )
            return
        try:
            arguments = params.get("arguments")
            result = await dynamic_tool.handler(arguments if isinstance(arguments, dict) else {})
        except Exception as exc:
            await client.respond_result(
                request_id,
                {
                    "success": False,
                    "contentItems": [{"type": "inputText", "text": str(exc) or "动态工具执行失败。"}],
                },
            )
            return
        await client.respond_result(
            request_id,
            {
                "success": not result.is_error,
                "contentItems": [{"type": "inputText", "text": result.content}],
            },
        )

    def _thread_resume_params(
        self,
        *,
        session_id: str,
        cwd: str,
        sandbox: str,
        system_prompt: str,
    ) -> dict[str, Any]:
        return {
            "threadId": session_id,
            "model": self.model,
            "cwd": cwd,
            "approvalPolicy": mcp_approval_policy(),
            "approvalsReviewer": "user",
            "sandbox": sandbox,
            "developerInstructions": self._developer_instructions(system_prompt),
            # resume 响应不回传 turns 历史：含图历史会使单行 JSONL 超过子进程流读取上限。
            "excludeTurns": True,
        }

    @staticmethod
    def _developer_instructions(system_prompt: str) -> str:
        parts = [system_prompt.strip(), _CODEX_RESPONSE_STYLE_INSTRUCTIONS]
        return "\n\n".join(part for part in parts if part)

    def _turn_start_params(
        self,
        *,
        thread_id: str,
        cwd: str,
        writable_roots: list[str],
        message: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        effort = str(
            metadata.get("reasoning_effort")
            or metadata.get("effort")
            or self.effort
            or ""
        ).strip()
        turn_input: list[dict[str, Any]] = [
            {"type": "text", "text": message, "text_elements": []}
        ]
        local_image_paths = metadata.get("local_image_paths") or []
        if not isinstance(local_image_paths, (list, tuple)):
            raise AssistantRuntimeError("Codex 视觉输入图片路径格式无效。")
        for raw_path in local_image_paths:
            image_path = Path(str(raw_path))
            if not image_path.is_absolute() or not image_path.is_file():
                raise AssistantRuntimeError(f"Codex 视觉输入图片不存在或不是绝对路径：{image_path}")
            turn_input.append(
                {"type": "localImage", "path": str(image_path), "detail": "high"}
            )

        params: dict[str, Any] = {
            "threadId": thread_id,
            "input": turn_input,
            "cwd": cwd,
            "approvalPolicy": mcp_approval_policy(),
            "approvalsReviewer": "user",
            "sandboxPolicy": self._sandbox_policy(writable_roots),
            "model": self.model,
        }
        if effort:
            params["effort"] = effort
        output_schema = metadata.get("output_schema")
        if output_schema is not None:
            if not isinstance(output_schema, dict):
                raise AssistantRuntimeError("Codex 结构化输出 Schema 格式无效。")
            params["outputSchema"] = output_schema
        return params

    def _sandbox_policy(self, writable_roots: list[str]) -> dict[str, Any]:
        effective_sandbox = self._effective_sandbox(writable_roots)
        if effective_sandbox == "danger-full-access":
            return {"type": "dangerFullAccess"}
        if effective_sandbox == "workspace-write":
            return {
                "type": "workspaceWrite",
                "writableRoots": writable_roots,
                "networkAccess": False,
                "excludeTmpdirEnvVar": False,
                "excludeSlashTmp": False,
            }
        return {"type": "readOnly", "networkAccess": False}

    def _effective_sandbox(self, writable_roots: list[str]) -> str:
        if self.sandbox == "danger-full-access":
            return "danger-full-access"
        if writable_roots:
            return "workspace-write"
        return "read-only"

    def _convert_item(
        self,
        item: dict[str, Any],
        lifecycle: str,
        session_id: str,
    ) -> list[RuntimeEvent]:
        item_type = str(item.get("type") or "")
        item_id = item.get("id")
        status = str(item.get("status") or ("completed" if lifecycle == "completed" else "inProgress"))

        if item_type == "reasoning":
            text = self._render_value(item.get("summary") or item.get("content"))
            return (
                [RuntimeEvent("activity", {"message": text, "kind": "agent", "session_id": session_id})]
                if text
                else []
            )

        if item_type == "commandExecution":
            if lifecycle == "started":
                tool_input = {
                    "cmd": str(item.get("command") or ""),
                    "cwd": item.get("cwd"),
                }
                events = [
                    RuntimeEvent(
                        "tool_use",
                        {
                            "tool_name": "exec_command",
                            "tool_input": tool_input,
                            "tool_use_id": item_id,
                            "session_id": session_id,
                        },
                    )
                ]
                events.extend(self._derive_tool_side_events("exec_command", tool_input, session_id))
                return events
            if lifecycle == "completed":
                exit_code = item.get("exitCode")
                return [
                    RuntimeEvent(
                        "tool_result",
                        {
                            "tool_name": "exec_command",
                            "tool_use_id": item_id,
                            "content": str(item.get("aggregatedOutput") or ""),
                            "is_error": status == "failed" or (isinstance(exit_code, int) and exit_code != 0),
                            "session_id": session_id,
                        },
                    )
                ]
            return []

        if item_type in {"mcpToolCall", "dynamicToolCall"}:
            tool_name = str(item.get("tool") or item_type)
            if lifecycle == "started":
                return [
                    RuntimeEvent(
                        "tool_use",
                        {
                            "tool_name": tool_name,
                            "tool_input": item.get("arguments") or {},
                            "tool_use_id": item_id,
                            "session_id": session_id,
                        },
                    )
                ]
            if lifecycle == "completed":
                error = item.get("error") or {}
                error_message = error.get("message") if isinstance(error, dict) else str(error)
                content = self._render_value(
                    item.get("contentItems")
                    if item.get("contentItems") is not None
                    else item.get("result")
                )
                return [
                    RuntimeEvent(
                        "tool_result",
                        {
                            "tool_name": tool_name,
                            "tool_use_id": item_id,
                            "content": str(error_message or content),
                            "is_error": status == "failed" or bool(error) or item.get("success") is False,
                            "session_id": session_id,
                        },
                    )
                ]

        if item_type == "webSearch":
            query = str(item.get("query") or "")
            if lifecycle == "started":
                return [
                    RuntimeEvent(
                        "tool_use",
                        {
                            "tool_name": "web_search",
                            "tool_input": {"query": query},
                            "tool_use_id": item_id,
                            "session_id": session_id,
                        },
                    ),
                    RuntimeEvent("search", {"query": query, "session_id": session_id}),
                ]
            if lifecycle == "completed":
                return [
                    RuntimeEvent(
                        "tool_result",
                        {
                            "tool_name": "web_search",
                            "tool_use_id": item_id,
                            "content": f"搜索完成：{query}",
                            "is_error": False,
                            "session_id": session_id,
                        },
                    )
                ]

        if item_type == "imageGeneration":
            if lifecycle == "started":
                return [
                    RuntimeEvent(
                        "activity",
                        {
                            "message": "正在生成图像…",
                            "kind": "tool_progress",
                            "session_id": session_id,
                        },
                    )
                ]
            image_data = str(item.get("result") or "")
            source_path = str(item.get("savedPath") or "")
            if not image_data and not source_path:
                return [
                    RuntimeEvent(
                        "activity",
                        {
                            "message": "图像生成未返回可用结果。",
                            "kind": "tool_progress",
                            "session_id": session_id,
                        },
                    )
                ]
            return [
                RuntimeEvent(
                    "image",
                    {
                        "data": image_data,
                        "source_path": source_path,
                        "revised_prompt": str(item.get("revisedPrompt") or ""),
                        "item_id": str(item_id or ""),
                        "session_id": session_id,
                    },
                )
            ]

        if item_type == "fileChange":
            changes = item.get("changes") or []
            summary = ", ".join(
                f"{change.get('type') or 'update'} {change.get('path') or change.get('move_path') or ''}".strip()
                for change in changes
                if isinstance(change, dict)
            )
            action = "失败" if status == "failed" else ("开始" if lifecycle == "started" else "完成")
            return [
                RuntimeEvent(
                    "activity",
                    {
                        "message": f"文件修改{action}{f'：{summary}' if summary else ''}",
                        "kind": "tool_progress",
                        "session_id": session_id,
                    },
                )
            ]

        return []

    def _resolve_write_policy(self, project_root: str) -> tuple[str, list[str]]:
        base_path = Path(project_root).resolve()
        if base_path.name == "workspace":
            workspace_root = base_path
            base_path = base_path.parent
        else:
            workspace_root = base_path / "workspace"
        resolved_roots: list[str] = []
        for raw_root in self.writable_roots:
            root_path = Path(raw_root)
            if not root_path.is_absolute():
                root_path = base_path / root_path
            root_path = root_path.resolve()
            try:
                root_path.relative_to(base_path)
            except ValueError as exc:
                raise AssistantRuntimeError(f"Codex 可写目录必须位于项目根目录内：{root_path}") from exc
            root_path.mkdir(parents=True, exist_ok=True)
            resolved_roots.append(str(root_path))
        workspace_root.mkdir(parents=True, exist_ok=True)
        return str(workspace_root.resolve()), sorted(set(resolved_roots))

    def _resolve_session_write_policy(self, session: RuntimeSession) -> tuple[str, list[str]]:
        if session.workspace_plan is None:
            if self.sandbox == "workspace-write":
                return self._resolve_write_policy(session.working_directory)
            return str(Path(session.working_directory).resolve()), []

        workspace_root = Path(session.working_directory).resolve()
        if self.sandbox == "read-only":
            return str(workspace_root), []

        writable_roots: list[str] = []
        for mount in session.workspace_plan.mounts:
            if mount.permission != "write":
                continue
            logical_path = mount.sandbox_path.removeprefix("/").strip("/")
            if logical_path not in {"me", "tmp"}:
                continue
            writable_path = Path(mount.host_path).resolve()
            writable_path.mkdir(parents=True, exist_ok=True)
            writable_roots.append(str(writable_path))
        return str(workspace_root), sorted(set(writable_roots))

    def _build_env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.api_key:
            env["OPENAI_API_KEY"] = self.api_key
        return env

    def _derive_tool_side_events(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        session_id: str,
    ) -> list[RuntimeEvent]:
        if tool_name.lower() != "exec_command":
            return []
        cmd = str(tool_input.get("cmd") or "").strip()
        search_keywords = ("grep", "rg", "find", "glob", "ls ", "ls\t", "ls\n", "fd ")
        read_keywords = ("cat ", "head ", "tail ", "bat ", "less ", "sed -n ")
        if any(keyword in cmd for keyword in search_keywords):
            return [
                RuntimeEvent(
                    "search",
                    {
                        "query": cmd[:120],
                        "tool_name": tool_name,
                        "session_id": session_id,
                    },
                )
            ]
        if any(keyword in cmd for keyword in read_keywords):
            parts = [part for part in cmd.split() if not part.startswith("-")]
            path = parts[-1] if len(parts) > 1 else cmd
            return [RuntimeEvent("read", {"path": path, "session_id": session_id})]
        return []

    def _render_value(self, value: object) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(filter(None, (self._render_value(item) for item in value)))
        if isinstance(value, dict):
            if isinstance(value.get("text"), str):
                return value["text"]
            if isinstance(value.get("content"), list):
                return self._render_value(value["content"])
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def _turn_error(self, params: dict[str, Any]) -> str:
        turn = params.get("turn") or {}
        error = turn.get("error") or params.get("error") or {}
        if isinstance(error, dict):
            return str(error.get("message") or params.get("message") or "Codex app-server 执行失败")
        return str(error or params.get("message") or "Codex app-server 执行失败")


def perform_codex_runtime_startup_check(cli_path: str | None = None) -> dict[str, object]:
    report: dict[str, object] = {
        "provider": "codex",
        "cli_found": False,
        "cli_path": None,
        "cli_version": None,
    }
    try:
        resolved = resolve_codex_cli_path(cli_path)
        report["cli_found"] = True
        report["cli_path"] = resolved
        report["cli_version"] = _read_codex_cli_version(resolved)
    except AssistantRuntimeError:
        pass
    return report


def resolve_codex_cli_path(cli_path: str | None = None) -> str:
    if cli_path:
        configured = Path(cli_path).expanduser()
        if _is_executable_file(configured):
            return str(configured)
        raise AssistantRuntimeError(f"CODEX_CLI_PATH 指向的 Codex CLI 不可用：{cli_path}")

    candidates: list[Path] = []
    which_path = shutil.which("codex")
    if which_path:
        candidates.append(Path(which_path))
    candidates.extend(
        [
            Path("/opt/homebrew/bin/codex"),
            Path("/usr/local/bin/codex"),
            Path.home() / ".local" / "bin" / "codex",
            Path.home() / "bin" / "codex",
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
        "未找到 Codex CLI。请安装 `@openai/codex` 或 `brew install --cask codex`，"
        "或通过 `CODEX_CLI_PATH` 环境变量显式指定路径。"
    )


def _is_executable_file(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _read_codex_cli_version(cli_path: str) -> str | None:
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
