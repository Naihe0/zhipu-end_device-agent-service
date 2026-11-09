from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, WebSocket, status

from device_agent.container import Container, get_container
from device_agent.models import Device, DeviceType
from device_agent.schemas import (
    DeviceOut,
    DeviceRegister,
    DeviceUpdate,
    EventOut,
    IngestMessage,
    StreamStatsOut,
    ToolCallOut,
    ToolCallRequest,
)

router = APIRouter(prefix="/api/devices", tags=["终端设备"])
Deps = Annotated[Container, Depends(get_container)]


def _out(c: Container, device: Device) -> DeviceOut:
    out = DeviceOut.model_validate(device)
    out.ws_connected = c.hub.is_connected(device.device_id)
    return out


@router.post("", status_code=status.HTTP_201_CREATED, summary="注册设备（会先校验其 MCP Server）")
async def register_device(body: DeviceRegister, c: Deps) -> DeviceOut:
    return _out(c, await c.devices.register(body))


@router.get("", summary="设备列表")
async def list_devices(
    c: Deps,
    online_only: bool = False,
    device_type: DeviceType | None = None,
    tool: Annotated[str | None, Query(description="只返回拥有该 MCP 工具的设备")] = None,
) -> list[DeviceOut]:
    devices = await c.devices.list_devices(online_only=online_only, device_type=device_type, tool=tool)
    return [_out(c, d) for d in devices]


@router.get("/{device_id}", summary="设备详情")
async def get_device(device_id: str, c: Deps) -> DeviceOut:
    return _out(c, await c.devices.get(device_id))


@router.patch("/{device_id}", summary="更新设备（修改 mcp_url 时会重新校验）")
async def update_device(device_id: str, body: DeviceUpdate, c: Deps) -> DeviceOut:
    return _out(c, await c.devices.update(device_id, body))


@router.delete("/{device_id}", status_code=status.HTTP_204_NO_CONTENT, summary="注销设备并删除其数据流")
async def delete_device(device_id: str, c: Deps) -> None:
    await c.devices.delete(device_id)
    await c.store.delete_stream(device_id)


@router.post("/{device_id}/heartbeat", summary="设备心跳")
async def heartbeat(device_id: str, c: Deps) -> DeviceOut:
    await c.devices.get(device_id)
    await c.devices.set_status(device_id, online=True)
    return _out(c, await c.devices.get(device_id))


@router.post("/{device_id}/tools/refresh", summary="重新拉取设备 MCP 工具列表")
async def refresh_tools(device_id: str, c: Deps) -> DeviceOut:
    return _out(c, await c.devices.refresh_tools(device_id))


@router.post("/{device_id}/tools/{tool_name}", summary="直接调用设备的 MCP 工具")
async def call_tool(device_id: str, tool_name: str, body: ToolCallRequest, c: Deps) -> ToolCallOut:
    result = await c.devices.call_tool(device_id, tool_name, body.arguments)
    return ToolCallOut(ok=result.ok, output=result.output, error=result.error, elapsed_ms=result.elapsed_ms)


@router.post("/{device_id}/events", status_code=status.HTTP_201_CREATED, summary="上报设备数据（HTTP 方式）")
async def ingest_event(device_id: str, body: IngestMessage, c: Deps) -> EventOut:
    await c.devices.get(device_id)
    return EventOut(**(await c.hub.ingest(device_id, body)).public())


@router.get("/{device_id}/events", summary="设备最近上报的数据")
async def recent_events(
    device_id: str, c: Deps, limit: Annotated[int, Query(ge=1, le=500)] = 50
) -> list[EventOut]:
    return [EventOut(**e.public()) for e in await c.store.recent(device_id, limit)]


@router.get("/{device_id}/stream", summary="设备数据流状态")
async def stream_stats(device_id: str, c: Deps) -> StreamStatsOut:
    last = await c.store.recent(device_id, 1)
    return StreamStatsOut(
        device_id=device_id,
        backend=c.store.backend,
        length=await c.store.length(device_id),
        last_event_id=last[-1].id if last else None,
        last_event_at=last[-1].created_at if last else None,
    )


@router.post("/{device_id}/messages", summary="通过 WebSocket 向设备推送消息")
async def push_to_device(device_id: str, body: dict[str, Any], c: Deps) -> dict[str, bool]:
    await c.devices.get(device_id)
    return {"delivered": await c.hub.send(device_id, body)}


@router.websocket("/{device_id}/ws")
async def device_ws(websocket: WebSocket, device_id: str) -> None:
    """设备数据长连接，协议见 services/connections.py。"""
    container: Container = websocket.app.state.container
    await container.hub.serve(websocket, device_id)
