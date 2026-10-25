"""设备数据接入：HTTP 上报与 WebSocket 长连接共用同一套解析/入库逻辑。

WebSocket 协议（设备 → 服务）：
  JSON 文本帧，格式见 `IngestMessage`；另有控制消息 {"type": "heartbeat"} / {"type": "pong"}。
  纯文本（非 JSON）帧视为一条文本数据。
  二进制帧：可选头部 `MEDIA:<kind>:<filename>:<size>\\n` + 原始字节；无头部则视为 binary 数据。
服务 → 设备：welcome / ack / error / ping / heartbeat_ack，以及服务主动推送的 task_result 等消息。
"""

import asyncio
import base64
import binascii
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from pydantic import ValidationError

from device_agent.database import utcnow
from device_agent.schemas import IngestMessage
from device_agent.services.devices import DeviceNotFoundError, DeviceService
from device_agent.services.event_store import DeviceEvent, EventKind, EventStore

logger = logging.getLogger(__name__)

CONTROL_TYPES = {"heartbeat", "ping", "pong"}
TOUCH_INTERVAL_SECONDS = 10.0
WS_PING_INTERVAL_SECONDS = 30.0


class IngestError(ValueError):
    pass


@dataclass
class Connection:
    websocket: WebSocket
    device_id: str
    connected_at: datetime = field(default_factory=utcnow)
    last_activity: datetime = field(default_factory=utcnow)
    messages: int = 0
    bytes_received: int = 0

    async def send(self, message: dict[str, Any]) -> None:
        await self.websocket.send_text(json.dumps(message, ensure_ascii=False, default=str))


class DeviceConnectionHub:
    def __init__(self, devices: DeviceService, store: EventStore, max_upload_bytes: int) -> None:
        self._devices = devices
        self._store = store
        self._max_bytes = max_upload_bytes
        self._connections: dict[str, Connection] = {}
        self._last_touch: dict[str, float] = {}

    # ---------- 入库 ----------

    async def _touch(self, device_id: str) -> None:
        now = time.monotonic()
        if now - self._last_touch.get(device_id, 0.0) >= TOUCH_INTERVAL_SECONDS:
            self._last_touch[device_id] = now
            await self._devices.set_status(device_id, online=True)

    async def ingest(self, device_id: str, msg: IngestMessage) -> DeviceEvent:
        common = {"metadata": msg.metadata}
        if msg.type == "text":
            if msg.text is None:
                raise IngestError("type=text 需要 text 字段")
            event = await self._store.append(device_id, EventKind.TEXT, text=msg.text, **common)
        elif msg.type in ("data", "sensor_data", "json_data"):
            if msg.data is None:
                raise IngestError(f"type={msg.type} 需要 data 字段")
            event = await self._store.append(device_id, EventKind.DATA, data=msg.data, **common)
        else:
            if not msg.data_b64:
                raise IngestError("type=media 需要 data_b64 字段")
            try:
                payload = base64.b64decode(msg.data_b64, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise IngestError(f"data_b64 不是合法的 base64: {exc}") from exc
            event = await self._store_media(
                device_id,
                EventKind(msg.kind or "binary"),
                payload,
                msg.media_type,
                msg.filename,
                msg.metadata,
            )
        await self._touch(device_id)
        return event

    async def _store_media(
        self,
        device_id: str,
        kind: EventKind,
        payload: bytes,
        media_type: str | None,
        filename: str | None,
        metadata: dict[str, Any],
    ) -> DeviceEvent:
        if len(payload) > self._max_bytes:
            raise IngestError(f"数据过大: {len(payload)} 字节 (上限 {self._max_bytes})")
        return await self._store.append(
            device_id, kind, payload=payload, media_type=media_type, filename=filename, metadata=metadata
        )

    async def ingest_binary(self, device_id: str, raw: bytes) -> DeviceEvent:
        kind, filename, payload = EventKind.BINARY, None, raw
        if raw.startswith(b"MEDIA:"):
            header, sep, body = raw.partition(b"\n")
            parts = header.decode("utf-8", errors="replace").split(":", 3)
            if not sep or len(parts) < 4:
                raise IngestError("二进制帧头格式应为 MEDIA:<kind>:<filename>:<size>\\n")
            kind_name, filename = parts[1].lower(), parts[2] or None
            kind = EventKind(kind_name) if kind_name in {k.value for k in EventKind} else EventKind.BINARY
            payload = body
        event = await self._store_media(device_id, kind, payload, None, filename, {"transport": "ws-binary"})
        await self._touch(device_id)
        return event

    # ---------- WebSocket ----------

    async def serve(self, websocket: WebSocket, device_id: str) -> None:
        await websocket.accept()
        try:
            device = await self._devices.get(device_id)
        except DeviceNotFoundError:
            await websocket.close(code=4404, reason="device not registered")
            return

        if (old := self._connections.get(device_id)) is not None:
            await old.websocket.close(code=4409, reason="replaced by a new connection")
        conn = self._connections[device_id] = Connection(websocket, device_id)
        await self._devices.set_status(device_id, online=True)
        logger.info("设备 WebSocket 已连接: %s", device_id)
        await conn.send(
            {
                "type": "welcome",
                "device_id": device_id,
                "server_time": utcnow().isoformat(),
                "supported_data_types": device.supported_data_types,
                "max_upload_bytes": self._max_bytes,
            }
        )
        try:
            while True:
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=WS_PING_INTERVAL_SECONDS)
                except TimeoutError:
                    await conn.send({"type": "ping", "server_time": utcnow().isoformat()})
                    continue
                if message["type"] == "websocket.disconnect":
                    break
                conn.messages += 1
                conn.last_activity = utcnow()
                if (text := message.get("text")) is not None:
                    conn.bytes_received += len(text.encode())
                    await self._on_text(conn, text)
                elif (data := message.get("bytes")) is not None:
                    conn.bytes_received += len(data)
                    await self._reply(conn, self.ingest_binary(device_id, data))
        except WebSocketDisconnect:
            pass
        finally:
            if self._connections.get(device_id) is conn:
                del self._connections[device_id]
            logger.info("设备 WebSocket 已断开: %s", device_id)

    async def _on_text(self, conn: Connection, text: str) -> None:
        try:
            obj = json.loads(text)
        except ValueError:
            obj = {"type": "text", "text": text}
        if isinstance(obj, dict) and obj.get("type") in CONTROL_TYPES:
            if obj["type"] == "heartbeat":
                await self._devices.set_status(conn.device_id, online=True)
                await conn.send({"type": "heartbeat_ack", "server_time": utcnow().isoformat()})
            elif obj["type"] == "ping":
                await conn.send({"type": "pong", "server_time": utcnow().isoformat()})
            return
        try:
            msg = IngestMessage.model_validate(obj)
        except ValidationError as exc:
            await conn.send({"type": "error", "error": exc.errors(include_url=False, include_context=False)})
            return
        await self._reply(conn, self.ingest(conn.device_id, msg))

    async def _reply(self, conn: Connection, pending) -> None:
        try:
            event = await pending
        except IngestError as exc:
            await conn.send({"type": "error", "error": str(exc)})
        else:
            await conn.send({"type": "ack", "event_id": event.id, "kind": event.kind})

    async def send(self, device_id: str, message: dict[str, Any]) -> bool:
        conn = self._connections.get(device_id)
        if conn is None:
            return False
        try:
            await conn.send(message)
        except Exception as exc:
            logger.warning("推送消息到设备 %s 失败: %s", device_id, exc)
            return False
        return True

    def is_connected(self, device_id: str) -> bool:
        return device_id in self._connections

    def connected_ids(self) -> set[str]:
        return set(self._connections)

    def status(self) -> dict[str, Any]:
        return {
            "total_connections": len(self._connections),
            "connections": [
                {
                    "device_id": c.device_id,
                    "connected_at": c.connected_at,
                    "last_activity": c.last_activity,
                    "messages": c.messages,
                    "bytes_received": c.bytes_received,
                }
                for c in self._connections.values()
            ],
        }

    async def close_all(self) -> None:
        for conn in list(self._connections.values()):
            with contextlib.suppress(Exception):
                await conn.websocket.close(code=1001, reason="server shutdown")
        self._connections.clear()
