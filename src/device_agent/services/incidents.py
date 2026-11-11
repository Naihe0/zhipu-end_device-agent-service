"""事件（incident）：把同一设备在一段时间内的相关触发归并起来。

事件 ID 同时作为自动任务的 A2A contextId，因此同一事件里后续的自动任务能从任务表看到
之前做过什么（见 memory.py）。超过 `incident_quiet_minutes` 没有新触发的事件自动关闭。
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from device_agent.database import utcnow
from device_agent.models import Incident

OPEN, CLOSED = "open", "closed"


class IncidentNotFoundError(LookupError):
    pass


class IncidentService:
    def __init__(self, sessions: async_sessionmaker, quiet_minutes: float) -> None:
        self._sessions = sessions
        self._quiet = timedelta(minutes=quiet_minutes)

    def _is_active(self, incident: Incident, now: datetime) -> bool:
        return incident.status == OPEN and incident.last_triggered_at > now - self._quiet

    async def current(self, device_id: str, now: datetime | None = None) -> Incident | None:
        """设备当前仍在进行中的事件（已打开且未进入静默期）。"""
        now = now or utcnow()
        async with self._sessions() as session:
            incident = await session.scalar(
                select(Incident)
                .where(Incident.device_id == device_id, Incident.status == OPEN)
                .order_by(Incident.last_triggered_at.desc())
                .limit(1)
            )
        return incident if incident is not None and self._is_active(incident, now) else None

    async def trigger(self, device_id: str, title: str, now: datetime | None = None) -> Incident:
        """记录一次触发：归入进行中的事件，没有则关闭过期事件并开启新事件。"""
        now = now or utcnow()
        async with self._sessions() as session, session.begin():
            incident = await session.scalar(
                select(Incident)
                .where(Incident.device_id == device_id, Incident.status == OPEN)
                .order_by(Incident.last_triggered_at.desc())
                .limit(1)
            )
            if incident is not None and not self._is_active(incident, now):
                incident.status, incident.closed_at = CLOSED, now
                incident = None
            if incident is None:
                incident = Incident(
                    # 事件 ID 同时是 A2A contextId；a2a-sdk 的 tasks.context_id 列为 VARCHAR(36)，用标准 UUID
                    incident_id=str(uuid.uuid4()),
                    device_id=device_id,
                    status=OPEN,
                    title=title[:200],
                    trigger_count=0,
                    opened_at=now,
                    last_triggered_at=now,
                )
                session.add(incident)
            incident.trigger_count += 1
            incident.last_triggered_at = now
        return incident

    async def close_stale(self, now: datetime | None = None) -> int:
        now = now or utcnow()
        async with self._sessions() as session, session.begin():
            result = await session.execute(
                update(Incident)
                .where(Incident.status == OPEN, Incident.last_triggered_at <= now - self._quiet)
                .values(status=CLOSED, closed_at=now)
            )
        return result.rowcount or 0

    async def get(self, incident_id: str) -> Incident:
        async with self._sessions() as session:
            incident = await session.get(Incident, incident_id)
        if incident is None:
            raise IncidentNotFoundError(incident_id)
        return incident

    async def list_incidents(
        self, *, status: str | None = None, device_id: str | None = None, limit: int = 50
    ) -> list[Incident]:
        stmt = select(Incident).order_by(Incident.last_triggered_at.desc()).limit(limit)
        if status:
            stmt = stmt.where(Incident.status == status)
        if device_id:
            stmt = stmt.where(Incident.device_id == device_id)
        async with self._sessions() as session:
            return list((await session.scalars(stmt)).all())

    async def close(self, incident_id: str) -> Incident:
        async with self._sessions() as session, session.begin():
            incident = await session.get(Incident, incident_id)
            if incident is None:
                raise IncidentNotFoundError(incident_id)
            if incident.status == OPEN:
                incident.status, incident.closed_at = CLOSED, utcnow()
        return incident
