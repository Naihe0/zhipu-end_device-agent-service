from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from device_agent.container import Container, get_container
from device_agent.models import IntentLog
from device_agent.schemas import IntentLogOut

router = APIRouter(prefix="/api/intents", tags=["多模态意图识别"])
Deps = Annotated[Container, Depends(get_container)]


@router.get("", summary="意图识别记录")
async def list_intents(
    c: Deps,
    device_id: str | None = None,
    detected_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[IntentLogOut]:
    stmt = select(IntentLog).order_by(IntentLog.id.desc()).limit(limit)
    if device_id:
        stmt = stmt.where(IntentLog.device_id == device_id)
    if detected_only:
        stmt = stmt.where(IntentLog.intent_detected.is_(True))
    async with c.sessions() as session:
        return [IntentLogOut.model_validate(log) for log in (await session.scalars(stmt)).all()]


@router.get("/stats", summary="意图识别统计")
async def intent_stats(c: Deps) -> dict[str, Any]:
    return await c.monitor.stats()


@router.post("/scan", summary="立即扫描一次所有设备的新数据")
async def scan_now(
    c: Deps,
    wait_for_tasks: Annotated[bool, Query(description="等待触发的自动任务执行完毕再返回")] = False,
) -> list[IntentLogOut]:
    logs = await c.monitor.scan_once()
    if wait_for_tasks:
        await c.monitor.wait_for_dispatches()
        async with c.sessions() as session:
            logs = [await session.get(IntentLog, log.id) or log for log in logs]
    return [IntentLogOut.model_validate(log) for log in logs]
