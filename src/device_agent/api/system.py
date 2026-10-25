from typing import Annotated, Any

from a2a.types import Task, TaskState
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text

from device_agent.a2a.executor import tool_calls_of
from device_agent.container import Container, get_container
from device_agent.database import utcnow
from device_agent.schemas import ChatRequest, ChatResponse
from device_agent.services.llm import GeminiLLM
from device_agent.services.remote_agents import task_text

router = APIRouter(tags=["系统"])
Deps = Annotated[Container, Depends(get_container)]


@router.get("/", summary="服务信息")
async def root(c: Deps) -> dict[str, Any]:
    s = c.settings
    return {
        "name": s.app_name,
        "version": s.app_version,
        "a2a_endpoint": s.a2a_rpc_url,
        "agent_card": s.public_base_url.rstrip("/") + "/.well-known/agent-card.json",
        "docs": s.public_base_url.rstrip("/") + "/docs",
    }


@router.get("/health", summary="健康检查")
async def health(c: Deps) -> JSONResponse:
    try:
        async with c.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        db_ok = False
    llm = c.llm
    checks = {
        "database": "ok" if db_ok else "error",
        "event_store": f"{c.store.backend}:{'ok' if await c.store.ping() else 'error'}",
        "llm": f"gemini:{llm.model}" if isinstance(llm, GeminiLLM) else "not_configured",
    }
    healthy = db_ok and checks["event_store"].endswith("ok")
    return JSONResponse(
        {
            "status": "healthy" if healthy else "unhealthy",
            "timestamp": utcnow().isoformat(),
            "checks": checks,
        },
        status_code=200 if healthy else 503,
    )


@router.get("/api/status", summary="运行状态概览")
async def service_status(c: Deps) -> dict[str, Any]:
    devices = await c.devices.list_devices()
    agents = await c.agents.list_agents()
    return {
        "devices": {"total": len(devices), "online": sum(d.is_online for d in devices)},
        "remote_agents": {"total": len(agents), "usable": sum(a.enabled and a.available for a in agents)},
        "connections": c.hub.status(),
        "event_store": c.store.backend,
        "intent_monitor": await c.monitor.stats(),
    }


@router.get("/api/connections", summary="设备 WebSocket 连接状态")
async def connections(c: Deps) -> dict[str, Any]:
    return c.hub.status()


@router.post("/api/chat", summary="便捷对话接口（内部走完整的 A2A 任务链路）")
async def chat(body: ChatRequest, c: Deps) -> ChatResponse:
    result = await c.local_client.send(body.message, context_id=body.context_id)
    if not isinstance(result, Task):
        text_ = " ".join(p.text for p in result.parts if p.text)
        return ChatResponse(task_id="", context_id=result.context_id, state="message", text=text_)
    return ChatResponse(
        task_id=result.id,
        context_id=result.context_id,
        state=TaskState.Name(result.status.state).removeprefix("TASK_STATE_").lower(),
        text=task_text(result),
        tool_calls=tool_calls_of(result),
    )
