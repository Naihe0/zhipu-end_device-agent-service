from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from device_agent.container import Container, get_container
from device_agent.models import IntentLog
from device_agent.schemas import ApprovalOut, IncidentDetailOut, IncidentOut, IntentLogOut

router = APIRouter(prefix="/api/incidents", tags=["事件"])
Deps = Annotated[Container, Depends(get_container)]


@router.get("", summary="事件列表")
async def list_incidents(
    c: Deps,
    status: Literal["open", "closed"] | None = None,
    device_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[IncidentOut]:
    incidents = await c.incidents.list_incidents(status=status, device_id=device_id, limit=limit)
    return [IncidentOut.model_validate(i) for i in incidents]


@router.get("/{incident_id}", summary="事件详情：包含其中的意图识别记录与审批")
async def get_incident(incident_id: str, c: Deps) -> IncidentDetailOut:
    incident = await c.incidents.get(incident_id)
    async with c.sessions() as session:
        logs = (
            await session.scalars(
                select(IntentLog).where(IntentLog.incident_id == incident_id).order_by(IntentLog.id)
            )
        ).all()
    approvals = await c.approvals.list_approvals(context_id=incident_id, limit=200)
    detail = IncidentDetailOut.model_validate(incident)
    detail.intents = [IntentLogOut.model_validate(log) for log in logs]
    detail.approvals = [ApprovalOut.model_validate(a) for a in approvals]
    return detail


@router.post("/{incident_id}/close", summary="手动关闭事件（之后的触发会开启新事件）")
async def close_incident(incident_id: str, c: Deps) -> IncidentOut:
    return IncidentOut.model_validate(await c.incidents.close(incident_id))
