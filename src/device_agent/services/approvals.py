"""自动任务中需要人工确认的工具调用。

编排器遇到需要确认的调用时不执行，只登记一条待审批记录并告知模型；操作员通过
`/api/approvals/{id}/approve` 批准后，服务按登记时的原始参数直接执行该工具（不再经过模型），
结果写回记录、推送给设备，并出现在同一 contextId 后续任务的历史中。
"""

import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from device_agent.database import utcnow
from device_agent.models import Approval
from device_agent.services.devices import DeviceService

logger = logging.getLogger(__name__)

Notify = Callable[[str, dict[str, Any]], Awaitable[bool]]

PENDING, APPROVED, EXECUTED, FAILED, REJECTED, EXPIRED = (
    "pending",
    "approved",
    "executed",
    "failed",
    "rejected",
    "expired",
)
STATUS_TEXT = {
    PENDING: "待审批",
    APPROVED: "已批准，执行中",
    EXECUTED: "已批准并执行",
    FAILED: "已批准但执行失败",
    REJECTED: "已拒绝",
    EXPIRED: "已过期未处理",
}


class ApprovalNotFoundError(LookupError):
    pass


class ApprovalStateError(ValueError):
    pass


class ApprovalService:
    def __init__(
        self,
        sessions: async_sessionmaker,
        devices: DeviceService,
        ttl_minutes: float,
        notify: Notify | None = None,
    ) -> None:
        self._sessions = sessions
        self._devices = devices
        self._ttl = timedelta(minutes=ttl_minutes)
        self._notify = notify

    async def request(
        self,
        *,
        device_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        reason: str,
        task_id: str | None,
        context_id: str | None,
    ) -> Approval:
        """登记待审批的调用；同一 contextId 中相同的待审批调用直接复用，避免重复审批。"""
        now = utcnow()
        async with self._sessions() as session, session.begin():
            pending = await session.scalars(
                select(Approval).where(
                    Approval.status == PENDING,
                    Approval.device_id == device_id,
                    Approval.tool_name == tool_name,
                    Approval.context_id == context_id,
                    Approval.expires_at > now,
                )
            )
            for existing in pending:
                if existing.arguments == arguments:
                    return existing
            approval = Approval(
                approval_id=uuid.uuid4().hex,
                status=PENDING,
                device_id=device_id,
                tool_name=tool_name,
                arguments=arguments,
                reason=reason,
                task_id=task_id,
                context_id=context_id,
                created_at=now,
                expires_at=now + self._ttl,
            )
            session.add(approval)
        logger.info("待审批: %s %s.%s %s", approval.approval_id, device_id, tool_name, arguments)
        return approval

    async def get(self, approval_id: str) -> Approval:
        async with self._sessions() as session:
            approval = await session.get(Approval, approval_id)
        if approval is None:
            raise ApprovalNotFoundError(approval_id)
        return approval

    async def list_approvals(
        self,
        *,
        status: str | None = None,
        device_id: str | None = None,
        context_id: str | None = None,
        limit: int = 50,
    ) -> list[Approval]:
        stmt = select(Approval).order_by(Approval.created_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(Approval.status == status)
        if device_id:
            stmt = stmt.where(Approval.device_id == device_id)
        if context_id:
            stmt = stmt.where(Approval.context_id == context_id)
        async with self._sessions() as session:
            return list((await session.scalars(stmt)).all())

    async def _claim(self, approval_id: str, new_status: str, note: str | None) -> Approval:
        """把待审批记录原子地切换到新状态，保证同一条审批只会被处理一次。"""
        now = utcnow()
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(Approval)
                .where(
                    Approval.approval_id == approval_id,
                    Approval.status == PENDING,
                    Approval.expires_at > now,
                )
                .values(status=new_status, decision_note=note, decided_at=now)
            )
            if result.rowcount != 1:
                current = await session.get(Approval, approval_id)
                if current is None:
                    raise ApprovalNotFoundError(approval_id)
                status = EXPIRED if current.status == PENDING else current.status
                raise ApprovalStateError(
                    f"审批 {approval_id} 当前状态为「{STATUS_TEXT[status]}」，无法再处理"
                )
        return await self.get(approval_id)

    async def approve(self, approval_id: str, note: str | None = None) -> Approval:
        approval = await self._claim(approval_id, APPROVED, note)
        outcome = await self._devices.call_tool(approval.device_id, approval.tool_name, approval.arguments)
        async with self._sessions() as session, session.begin():
            approval = await session.get(Approval, approval_id)
            approval.status = EXECUTED if outcome.ok else FAILED
            approval.result, approval.error = outcome.output, outcome.error
        logger.info("审批 %s 已执行: ok=%s", approval_id, outcome.ok)
        await self._announce(approval)
        return approval

    async def reject(self, approval_id: str, note: str | None = None) -> Approval:
        approval = await self._claim(approval_id, REJECTED, note)
        await self._announce(approval)
        return approval

    async def expire_stale(self, now: datetime | None = None) -> int:
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(Approval)
                .where(Approval.status == PENDING, Approval.expires_at <= (now or utcnow()))
                .values(status=EXPIRED)
            )
        return result.rowcount or 0

    async def _announce(self, approval: Approval) -> None:
        if self._notify is None:
            return
        await self._notify(
            approval.device_id,
            {
                "type": "approval_result",
                "approval_id": approval.approval_id,
                "tool": approval.tool_name,
                "arguments": approval.arguments,
                "status": approval.status,
                "result": approval.result,
                "error": approval.error,
            },
        )


def describe(approval: Approval) -> str:
    """一行文字描述，用于写入模型上下文。"""
    text = f"审批 {approval.approval_id[:8]}: {approval.tool_name}({approval.arguments}) → {STATUS_TEXT[approval.status]}"
    if approval.status in (EXECUTED, FAILED):
        text += f"，结果: {approval.result if approval.status == EXECUTED else approval.error}"
    if approval.decision_note:
        text += f"（操作员备注: {approval.decision_note}）"
    return text
