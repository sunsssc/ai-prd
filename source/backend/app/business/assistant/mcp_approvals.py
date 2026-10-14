from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.business.assistant.store import SQLiteAssistantStore

logger = logging.getLogger(__name__)


class McpApprovalConflict(ValueError):
    pass


@dataclass
class PendingMcpApproval:
    record: dict[str, object]
    future: asyncio.Future[dict[str, object]]
    timer: asyncio.TimerHandle


class McpApprovals:
    """审批只关联当前存活的 Runtime 请求；事件用于审计和刷新页面，不用于重放执行。"""

    timeout_seconds = 600

    def __init__(self, store: SQLiteAssistantStore) -> None:
        self.store = store
        self.pending: dict[str, PendingMcpApproval] = {}

    def request(
        self, session_id: str, turn_id: str, details: dict[str, object]
    ) -> tuple[dict[str, object], asyncio.Future[dict[str, object]]]:
        now = datetime.now(timezone.utc)
        approval_id = str(uuid4())
        record = {
            **details,
            "approval_id": approval_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "status": "pending",
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(seconds=self.timeout_seconds)).isoformat(),
        }
        self._persist(record)
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        timer = loop.call_later(self.timeout_seconds, self._settle, approval_id, "expired")
        self.pending[approval_id] = PendingMcpApproval(record, future, timer)
        return record, future

    def list_for_session(self, session_id: str) -> list[dict[str, object]]:
        records: dict[str, dict[str, object]] = {}
        for event in self.store.list_runtime_events_by_session_id(session_id, event_type="mcp_approval"):
            record = dict(event.payload)
            approval_id = str(record["approval_id"])
            if record["status"] == "pending" and approval_id not in self.pending:
                record["status"] = "expired"
            records[approval_id] = record
        return list(records.values())

    def decide(
        self, session_id: str, turn_id: str, approval_id: str, decision: str, user_id: str
    ) -> dict[str, object]:
        if decision not in {"approved", "declined"}:
            raise ValueError("不支持的审批决定。")
        pending = self.pending.get(approval_id)
        if pending is None:
            record = next(
                (item for item in self.list_for_session(session_id) if item["approval_id"] == approval_id),
                None,
            )
            if record is None or record["turn_id"] != turn_id:
                raise ValueError("审批请求不存在。")
            if record["status"] == decision:
                return record
            raise McpApprovalConflict("审批已处理或失效，请刷新页面。")
        if pending.record["session_id"] != session_id or pending.record["turn_id"] != turn_id:
            raise ValueError("审批请求不存在。")
        if pending.future.done() or datetime.now(timezone.utc) >= datetime.fromisoformat(str(pending.record["expires_at"])):
            self._settle(approval_id, "expired")
            raise McpApprovalConflict("审批已失效，请重新发起操作。")
        return self._settle(approval_id, decision, user_id)

    def expire_turn(self, turn_id: str) -> None:
        for approval_id, pending in list(self.pending.items()):
            if pending.record["turn_id"] == turn_id:
                self._settle(approval_id, "expired")

    def expire(self, approval_id: str) -> dict[str, object] | None:
        if approval_id in self.pending:
            return self._settle(approval_id, "expired")
        return None

    def _settle(self, approval_id: str, status: str, user_id: str | None = None) -> dict[str, object]:
        pending = self.pending[approval_id]
        record = {
            **pending.record,
            "status": status,
            "resolved_at": datetime.now(timezone.utc).isoformat(),
            "resolved_by": user_id,
        }
        # 先记录用户决定，再允许 Runtime 继续；持久化失败不会放行。
        try:
            self._persist(record)
        except Exception:
            if status != "expired":
                raise
            # 存储故障也必须取消等待；重启后的历史 pending 同样按失效展示。
            logger.exception("MCP 审批失效记录写入失败：%s", approval_id)
        self.pending.pop(approval_id)
        pending.timer.cancel()
        if not pending.future.done():
            pending.future.set_result(record)
        return record

    def _persist(self, record: dict[str, object]) -> None:
        self.store.append_runtime_event(
            str(record["session_id"]), str(record["turn_id"]), "mcp_approval", record
        )
