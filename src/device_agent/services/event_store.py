"""设备事件流。

- `RedisEventStore`（默认）：Redis Streams（XADD / XREADGROUP 消费组），支持多进程部署与重启后保留数据。
- `MemoryEventStore`：进程内实现，供测试或临时调试使用（EVENT_STORE_BACKEND=memory）。
小的二进制数据以 base64 内联在事件中，大文件写入 `BlobStore`（本地文件系统），事件只保存引用。
"""

import base64
import itertools
import logging
import mimetypes
import uuid
from abc import ABC, abstractmethod
from collections import deque
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from device_agent.database import utcnow

logger = logging.getLogger(__name__)


class EventKind(StrEnum):
    TEXT = "text"
    DATA = "data"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    BINARY = "binary"


MEDIA_KINDS = {EventKind.IMAGE, EventKind.AUDIO, EventKind.VIDEO, EventKind.BINARY}


class DeviceEvent(BaseModel):
    id: str = ""
    device_id: str
    kind: EventKind
    text: str | None = None
    data: Any = None
    media_type: str | None = None
    filename: str | None = None
    size: int = 0
    inline_b64: str | None = None
    blob_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def stored_as(self) -> Literal["none", "inline", "blob"]:
        if self.blob_path:
            return "blob"
        return "inline" if self.inline_b64 else "none"

    def public(self) -> dict[str, Any]:
        """对外展示（不包含二进制内容）。"""
        data = self.model_dump(exclude={"inline_b64", "blob_path"})
        data["stored_as"] = self.stored_as
        return data


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def save(self, device_id: str, payload: bytes, media_type: str | None, filename: str | None) -> str:
        suffix = Path(filename).suffix if filename else ""
        if not suffix and media_type:
            suffix = mimetypes.guess_extension(media_type) or ""
        rel = Path(device_id) / f"{utcnow():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:8]}{suffix or '.bin'}"
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        return rel.as_posix()

    def _resolve(self, rel: str) -> Path:
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"非法的 blob 路径: {rel}")
        return path

    def load(self, rel: str) -> bytes | None:
        path = self._resolve(rel)
        return path.read_bytes() if path.exists() else None

    def delete(self, rel: str) -> None:
        self._resolve(rel).unlink(missing_ok=True)

    def purge_older_than(self, cutoff: datetime) -> int:
        if not self.root.exists():
            return 0
        removed = 0
        for path in self.root.rglob("*"):
            if path.is_file() and path.stat().st_mtime < cutoff.timestamp():
                path.unlink(missing_ok=True)
                removed += 1
        return removed


class EventStore(ABC):
    backend: str

    def __init__(self, blobs: BlobStore, *, inline_max_bytes: int, max_len: int) -> None:
        self.blobs = blobs
        self._inline_max = inline_max_bytes
        self._max_len = max_len

    async def append(
        self,
        device_id: str,
        kind: EventKind,
        *,
        text: str | None = None,
        data: Any = None,
        payload: bytes | None = None,
        media_type: str | None = None,
        filename: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> DeviceEvent:
        event = DeviceEvent(
            device_id=device_id,
            kind=kind,
            text=text,
            data=data,
            media_type=media_type or (mimetypes.guess_type(filename)[0] if filename else None),
            filename=filename,
            metadata=metadata or {},
        )
        if payload is not None:
            event.size = len(payload)
            if len(payload) > self._inline_max:
                event.blob_path = self.blobs.save(device_id, payload, event.media_type, filename)
            else:
                event.inline_b64 = base64.b64encode(payload).decode()
        return await self._store(event)

    def load_payload(self, event: DeviceEvent) -> bytes | None:
        if event.inline_b64:
            return base64.b64decode(event.inline_b64)
        if event.blob_path:
            return self.blobs.load(event.blob_path)
        return None

    @abstractmethod
    async def _store(self, event: DeviceEvent) -> DeviceEvent: ...

    @abstractmethod
    async def read_new(self, device_id: str, group: str, count: int) -> list[DeviceEvent]:
        """消费组语义：返回该 group 尚未读取过的事件，并标记为已读。"""

    @abstractmethod
    async def recent(self, device_id: str, limit: int) -> list[DeviceEvent]: ...

    @abstractmethod
    async def length(self, device_id: str) -> int: ...

    @abstractmethod
    async def device_ids(self) -> list[str]: ...

    @abstractmethod
    async def delete_stream(self, device_id: str) -> None: ...

    @abstractmethod
    async def trim_older_than(self, cutoff: datetime) -> int: ...

    async def start(self) -> None:
        self.blobs.root.mkdir(parents=True, exist_ok=True)

    async def ping(self) -> bool:
        return True

    async def close(self) -> None:
        return None


class MemoryEventStore(EventStore):
    backend = "memory"

    def __init__(self, blobs: BlobStore, *, inline_max_bytes: int, max_len: int) -> None:
        super().__init__(blobs, inline_max_bytes=inline_max_bytes, max_len=max_len)
        self._streams: dict[str, deque[DeviceEvent]] = {}
        self._seq = itertools.count(1)
        self._cursors: dict[tuple[str, str], int] = {}

    @staticmethod
    def _seq_of(event: DeviceEvent) -> int:
        return int(event.id.rsplit("-", 1)[1])

    async def _store(self, event: DeviceEvent) -> DeviceEvent:
        stream = self._streams.setdefault(event.device_id, deque())
        event.id = f"{int(event.created_at.timestamp() * 1000)}-{next(self._seq)}"
        if len(stream) >= self._max_len:
            dropped = stream.popleft()
            if dropped.blob_path:
                self.blobs.delete(dropped.blob_path)
        stream.append(event)
        return event

    async def read_new(self, device_id: str, group: str, count: int) -> list[DeviceEvent]:
        cursor = self._cursors.get((device_id, group), 0)
        fresh = [e for e in self._streams.get(device_id, ()) if self._seq_of(e) > cursor][:count]
        if fresh:
            self._cursors[(device_id, group)] = self._seq_of(fresh[-1])
        return fresh

    async def recent(self, device_id: str, limit: int) -> list[DeviceEvent]:
        return list(self._streams.get(device_id, ()))[-limit:]

    async def length(self, device_id: str) -> int:
        return len(self._streams.get(device_id, ()))

    async def device_ids(self) -> list[str]:
        return [d for d, s in self._streams.items() if s]

    async def delete_stream(self, device_id: str) -> None:
        for event in self._streams.pop(device_id, ()):
            if event.blob_path:
                self.blobs.delete(event.blob_path)
        self._cursors = {k: v for k, v in self._cursors.items() if k[0] != device_id}

    async def trim_older_than(self, cutoff: datetime) -> int:
        removed = 0
        for stream in self._streams.values():
            while stream and stream[0].created_at < cutoff:
                event = stream.popleft()
                if event.blob_path:
                    self.blobs.delete(event.blob_path)
                removed += 1
        return removed


class RedisEventStore(EventStore):
    """Redis Streams 实现，语义与 MemoryEventStore 一致（精确的 max_len、淘汰/清理时删除 blob）。"""

    backend = "redis"

    def __init__(
        self,
        url: str,
        blobs: BlobStore,
        *,
        inline_max_bytes: int,
        max_len: int,
        prefix: str = "device_agent:stream:",
    ) -> None:
        super().__init__(blobs, inline_max_bytes=inline_max_bytes, max_len=max_len)
        import redis.asyncio as aioredis

        self._url = url
        self._prefix = prefix
        self._redis = aioredis.from_url(url, decode_responses=True)
        self._consumer = f"agent-{uuid.uuid4().hex[:8]}"
        self._groups: set[tuple[str, str]] = set()

    def _key(self, device_id: str) -> str:
        return f"{self._prefix}{device_id}"

    @staticmethod
    def _decode(entry_id: str, fields: dict[str, str]) -> DeviceEvent:
        event = DeviceEvent.model_validate_json(fields["event"])
        event.id = entry_id
        return event

    def _drop_blobs(self, entries: list[tuple[str, dict[str, str]]]) -> None:
        for entry_id, fields in entries:
            event = self._decode(entry_id, fields)
            if event.blob_path:
                self.blobs.delete(event.blob_path)

    def _forget_groups(self, key: str) -> None:
        self._groups = {g for g in self._groups if g[0] != key}

    async def _store(self, event: DeviceEvent) -> DeviceEvent:
        key = self._key(event.device_id)
        overflow = await self._redis.xlen(key) - self._max_len + 1
        if overflow > 0:  # 即将被淘汰的事件，先删除其 blob
            self._drop_blobs(await self._redis.xrange(key, count=overflow))
        event.id = await self._redis.xadd(
            key, {"event": event.model_dump_json(exclude={"id"})}, maxlen=self._max_len, approximate=False
        )
        return event

    async def _ensure_group(self, key: str, group: str) -> None:
        if (key, group) in self._groups:
            return
        from redis.exceptions import ResponseError

        try:
            await self._redis.xgroup_create(key, group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        self._groups.add((key, group))

    async def read_new(self, device_id: str, group: str, count: int) -> list[DeviceEvent]:
        from redis.exceptions import ResponseError

        key = self._key(device_id)
        for attempt in range(2):
            await self._ensure_group(key, group)
            try:
                response = await self._redis.xreadgroup(group, self._consumer, {key: ">"}, count=count)
                break
            except ResponseError as exc:  # 流被删除后消费组随之消失，重建一次
                if "NOGROUP" not in str(exc) or attempt:
                    raise
                self._forget_groups(key)
        events = [self._decode(i, f) for _, entries in response for i, f in entries]
        if events:
            await self._redis.xack(key, group, *[e.id for e in events])
        return events

    async def recent(self, device_id: str, limit: int) -> list[DeviceEvent]:
        entries = await self._redis.xrevrange(self._key(device_id), count=limit)
        return [self._decode(i, f) for i, f in reversed(entries)]

    async def length(self, device_id: str) -> int:
        return await self._redis.xlen(self._key(device_id))

    async def device_ids(self) -> list[str]:
        ids = []
        async for key in self._redis.scan_iter(match=f"{self._prefix}*", _type="stream"):
            if await self._redis.xlen(key):
                ids.append(key.removeprefix(self._prefix))
        return ids

    async def delete_stream(self, device_id: str) -> None:
        key = self._key(device_id)
        self._drop_blobs(await self._redis.xrange(key))
        await self._redis.delete(key)
        self._forget_groups(key)

    async def trim_older_than(self, cutoff: datetime) -> int:
        cutoff_ms = int(cutoff.timestamp() * 1000)
        removed = 0
        async for key in self._redis.scan_iter(match=f"{self._prefix}*", _type="stream"):
            self._drop_blobs(await self._redis.xrange(key, max=f"({cutoff_ms}-0"))
            removed += await self._redis.xtrim(key, minid=f"{cutoff_ms}-0", approximate=False)
        return removed

    async def start(self) -> None:
        await super().start()
        try:
            await self._redis.ping()
        except Exception as exc:
            raise RuntimeError(
                f"无法连接 Redis ({self._url}): {exc}。请先启动 Redis（Windows 上可运行 "
                "scripts/dev-services.ps1），或设置 EVENT_STORE_BACKEND=memory 临时使用内存事件流。"
            ) from exc

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception:
            return False

    async def close(self) -> None:
        await self._redis.aclose()


def create_event_store(settings) -> EventStore:
    blobs = BlobStore(settings.blob_dir)
    kwargs = {"inline_max_bytes": settings.inline_blob_max_bytes, "max_len": settings.stream_max_len}
    if settings.use_redis:
        logger.info("事件流后端: Redis Streams (%s)", settings.redis_url)
        return RedisEventStore(settings.redis_url, blobs, prefix=settings.redis_stream_prefix, **kwargs)
    logger.info("事件流后端: 内存")
    return MemoryEventStore(blobs, **kwargs)
