"""终端设备注册表：设备信息存数据库，能力来自设备 MCP Server 的 tools/list。"""

import asyncio
import logging
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from device_agent.database import utcnow
from device_agent.models import Device
from device_agent.schemas import DeviceRegister, DeviceUpdate
from device_agent.services.mcp_gateway import MCPConnectionError, MCPGateway, ToolCallResult

logger = logging.getLogger(__name__)


class DeviceNotFoundError(LookupError):
    pass


class DeviceValidationError(ValueError):
    pass


class DeviceService:
    def __init__(self, sessions: async_sessionmaker, mcp: MCPGateway, offline_after_seconds: int) -> None:
        self._sessions = sessions
        self._mcp = mcp
        self._offline_after = timedelta(seconds=offline_after_seconds)

    async def _fetch_tools(self, mcp_url: str) -> list[dict[str, Any]]:
        try:
            tools = await self._mcp.list_tools(mcp_url)
        except MCPConnectionError as exc:
            raise DeviceValidationError(f"MCP 服务验证失败: {exc}") from exc
        return [t.to_dict() for t in tools]

    async def register(self, data: DeviceRegister) -> Device:
        """注册或覆盖更新设备。注册前会连接设备 MCP Server 拉取真实工具列表。"""
        mcp_url = str(data.mcp_url)
        tools = await self._fetch_tools(mcp_url)
        now = utcnow()
        fields = data.model_dump(exclude={"device_id", "mcp_url"})
        async with self._sessions() as session, session.begin():
            device = await session.get(Device, data.device_id)
            if device is None:
                device = Device(device_id=data.device_id, mcp_url=mcp_url, **fields)
                session.add(device)
            else:
                for key, value in fields.items():
                    setattr(device, key, value)
                device.mcp_url = mcp_url
            device.tools = tools
            device.tools_refreshed_at = now
            device.is_online = True
            device.last_seen = now
        logger.info("设备已注册: %s (%d 个工具)", data.device_id, len(tools))
        return device

    async def update(self, device_id: str, patch: DeviceUpdate) -> Device:
        changes = patch.model_dump(exclude_unset=True)
        tools = None
        if changes.get("mcp_url") is not None:
            changes["mcp_url"] = str(changes["mcp_url"])
            tools = await self._fetch_tools(changes["mcp_url"])
        async with self._sessions() as session, session.begin():
            device = await session.get(Device, device_id)
            if device is None:
                raise DeviceNotFoundError(device_id)
            for key, value in changes.items():
                if value is not None or key in ("location", "system_prompt"):
                    setattr(device, key, value)
            if tools is not None:
                device.tools = tools
                device.tools_refreshed_at = utcnow()
        return device

    async def get(self, device_id: str) -> Device:
        async with self._sessions() as session:
            device = await session.get(Device, device_id)
        if device is None:
            raise DeviceNotFoundError(device_id)
        return device

    async def list_devices(
        self, *, online_only: bool = False, device_type: str | None = None, tool: str | None = None
    ) -> list[Device]:
        stmt = select(Device).order_by(Device.created_at)
        if online_only:
            stmt = stmt.where(Device.is_online.is_(True))
        if device_type:
            stmt = stmt.where(Device.device_type == device_type)
        async with self._sessions() as session:
            devices = list((await session.scalars(stmt)).all())
        if tool:
            devices = [d for d in devices if any(t["name"] == tool for t in d.tools)]
        return devices

    async def delete(self, device_id: str) -> None:
        async with self._sessions() as session, session.begin():
            device = await session.get(Device, device_id)
            if device is None:
                raise DeviceNotFoundError(device_id)
            await session.delete(device)

    async def set_status(self, device_id: str, *, online: bool) -> bool:
        """心跳 / 数据上报 / WebSocket 连接状态变化时调用。返回设备是否存在。"""
        async with self._sessions() as session, session.begin():
            device = await session.get(Device, device_id)
            if device is None:
                return False
            device.is_online = online
            if online:
                device.last_seen = utcnow()
        return True

    async def refresh_tools(self, device_id: str) -> Device:
        device = await self.get(device_id)
        tools = await self._fetch_tools(device.mcp_url)
        async with self._sessions() as session, session.begin():
            device = await session.get(Device, device_id)
            if device is None:
                raise DeviceNotFoundError(device_id)
            device.tools = tools
            device.tools_refreshed_at = device.last_seen = utcnow()
            device.is_online = True
        return device

    async def call_tool(self, device_id: str, tool_name: str, arguments: dict[str, Any]) -> ToolCallResult:
        device = await self.get(device_id)
        if not device.is_online:
            return ToolCallResult(False, None, 0, f"设备离线: {device_id}")
        names = [t["name"] for t in device.tools]
        if tool_name not in names:
            return ToolCallResult(
                False, None, 0, f"设备 {device_id} 没有工具 '{tool_name}'，可用工具: {names}"
            )
        result = await self._mcp.call_tool(device.mcp_url, tool_name, arguments)
        if result.ok:
            await self.set_status(device_id, online=True)
        return result

    async def sweep(self, ws_connected: Iterable[str] = ()) -> dict[str, bool]:
        """维护任务：对长时间无活动的设备探测其 MCP Server，更新在线状态与工具缓存。"""
        connected = set(ws_connected)
        cutoff = utcnow() - self._offline_after
        stale = [
            d
            for d in await self.list_devices()
            if d.device_id not in connected and not (d.is_online and d.last_seen and d.last_seen > cutoff)
        ]

        async def probe(device: Device) -> tuple[str, bool]:
            try:
                tools = [t.to_dict() for t in await self._mcp.list_tools(device.mcp_url)]
            except MCPConnectionError:
                tools = None
            async with self._sessions() as session, session.begin():
                current = await session.get(Device, device.device_id)
                if current is None:
                    return device.device_id, False
                current.is_online = tools is not None
                if tools is not None:
                    current.tools, current.last_seen = tools, utcnow()
                    current.tools_refreshed_at = current.last_seen
            return device.device_id, tools is not None

        results = dict(await asyncio.gather(*(probe(d) for d in stale)))
        for device_id, online in results.items():
            logger.log(logging.DEBUG if online else logging.INFO, "设备探测 %s: online=%s", device_id, online)
        return results
