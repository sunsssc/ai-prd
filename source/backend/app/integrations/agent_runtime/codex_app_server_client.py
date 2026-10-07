from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

from app.integrations.agent_runtime.models import AssistantRuntimeError

_STREAM_CLOSED = object()
_MAX_STDERR_CHARS = 4000
_SUBPROCESS_STREAM_LIMIT = 64 * 1024 * 1024


class CodexAppServerClient:
    """通过 JSONL stdio 管理单个 ``codex app-server`` 进程。"""

    def __init__(
        self,
        *,
        cli_path: str,
        env: dict[str, str],
        client_name: str = "ai_prd",
        client_title: str = "AI PRD",
        client_version: str = "0.1.0",
    ) -> None:
        self.cli_path = cli_path
        self.env = env
        self.client_info = {
            "name": client_name,
            "title": client_title,
            "version": client_version,
        }
        self.process: asyncio.subprocess.Process | None = None
        self._next_request_id = 1
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._messages: asyncio.Queue[dict[str, Any] | BaseException | object] = asyncio.Queue()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_parts: list[str] = []
        self._closed = False

    async def initialize(self) -> dict[str, Any]:
        await self.start()
        result = await self.request(
            "initialize",
            {
                "clientInfo": self.client_info,
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.notify("initialized", {})
        return result

    async def start(self) -> None:
        if self.process is not None:
            return
        self.process = await asyncio.create_subprocess_exec(
            self.cli_path,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self.env,
            limit=_SUBPROCESS_STREAM_LIMIT,
        )
        self._reader_task = asyncio.create_task(self._read_stdout())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        await self.start()
        request_id = self._next_request_id
        self._next_request_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._write({"method": method, "id": request_id, "params": params})
            return await future
        finally:
            self._pending.pop(request_id, None)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self.start()
        await self._write({"method": method, "params": params})

    async def respond_error(self, request_id: object, message: str) -> None:
        await self._write(
            {
                "id": request_id,
                "error": {
                    "code": -32000,
                    "message": message,
                },
            }
        )

    async def respond_result(self, request_id: object, result: dict[str, Any]) -> None:
        await self._write({"id": request_id, "result": result})

    async def messages(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self._messages.get()
            if item is _STREAM_CLOSED:
                return
            if isinstance(item, BaseException):
                raise item
            yield item

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self.process
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self._fail_pending(AssistantRuntimeError("codex app-server client 已关闭。"))

    async def _write(self, message: dict[str, Any]) -> None:
        process = self.process
        if self._closed or process is None or process.stdin is None:
            raise AssistantRuntimeError("codex app-server client 未运行。")
        process.stdin.write(f"{json.dumps(message, ensure_ascii=False)}\n".encode())
        await process.stdin.drain()

    async def _read_stdout(self) -> None:
        process = self.process
        if process is None or process.stdout is None:
            return
        try:
            async for raw_line in process.stdout:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AssistantRuntimeError(f"codex app-server 输出不是合法 JSON：{exc}") from exc
                if not isinstance(message, dict):
                    continue
                request_id = message.get("id")
                if request_id is not None and not message.get("method"):
                    future = self._pending.get(request_id)
                    if future is None or future.done():
                        continue
                    if message.get("error"):
                        error = message["error"]
                        future.set_exception(
                            AssistantRuntimeError(
                                str(error.get("message") or "codex app-server 请求失败")
                            )
                        )
                    else:
                        result = message.get("result")
                        future.set_result(result if isinstance(result, dict) else {})
                    continue
                if message.get("method"):
                    await self._messages.put(message)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._fail_pending(exc)
            await self._messages.put(exc)
        finally:
            return_code = await process.wait()
            if not self._closed and return_code != 0:
                error = AssistantRuntimeError(self._process_error(return_code))
                self._fail_pending(error)
                await self._messages.put(error)
            await self._messages.put(_STREAM_CLOSED)

    async def _read_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        try:
            async for raw_line in process.stderr:
                text = raw_line.decode("utf-8", errors="replace").strip()
                if text:
                    self._stderr_parts.append(text)
                    joined = "\n".join(self._stderr_parts)
                    if len(joined) > _MAX_STDERR_CHARS:
                        self._stderr_parts = [joined[-_MAX_STDERR_CHARS:]]
        except asyncio.CancelledError:
            raise

    def _fail_pending(self, error: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    def _process_error(self, return_code: int) -> str:
        message = f"codex app-server 已退出（code={return_code}）"
        stderr = "\n".join(self._stderr_parts).strip()
        return f"{message}\nError output: {stderr}" if stderr else message
