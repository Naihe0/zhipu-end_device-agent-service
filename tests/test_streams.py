import base64
import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from device_agent.database import utcnow
from device_agent.services.event_store import (
    BlobStore,
    EventKind,
    EventStore,
    MemoryEventStore,
    RedisEventStore,
)
from tests.conftest import TEST_REDIS_URL, require_redis

BACKENDS = ["memory", pytest.param("redis", marks=pytest.mark.redis)]


@pytest.fixture(params=BACKENDS)
def event_backend(request) -> str:
    """覆盖 conftest：本模块中基于 API 的用例对两种事件流后端各跑一遍。"""
    return request.param


@pytest.fixture(params=BACKENDS)
async def store(request, tmp_path):
    blobs = BlobStore(tmp_path / "blobs")
    if request.param == "memory":
        store: EventStore = MemoryEventStore(blobs, inline_max_bytes=8, max_len=3)
    else:
        require_redis()
        store = RedisEventStore(
            TEST_REDIS_URL, blobs, inline_max_bytes=8, max_len=3, prefix=f"test:{uuid.uuid4().hex[:12]}:"
        )
    await store.start()
    yield store
    for device_id in await store.device_ids():
        await store.delete_stream(device_id)
    await store.close()


async def test_consumer_group_cursor(store: EventStore):
    for i in range(2):
        await store.append("d1", EventKind.TEXT, text=f"m{i}")
    assert [e.text for e in await store.read_new("d1", "g", 10)] == ["m0", "m1"]
    assert await store.read_new("d1", "g", 10) == []
    await store.append("d1", EventKind.TEXT, text="m2")
    assert [e.text for e in await store.read_new("d1", "g", 10)] == ["m2"]
    # 另一个消费组独立
    assert len(await store.read_new("d1", "other", 10)) == 3


async def test_inline_vs_blob_and_eviction(store: EventStore):
    small = await store.append("d1", EventKind.IMAGE, payload=b"tiny", media_type="image/png")
    big = await store.append("d1", EventKind.AUDIO, payload=b"x" * 100, filename="a.wav")
    assert small.stored_as == "inline" and store.load_payload(small) == b"tiny"
    assert big.stored_as == "blob" and big.media_type in ("audio/wav", "audio/x-wav")
    assert store.load_payload(big) == b"x" * 100
    blob_file = store.blobs.root / big.blob_path

    for i in range(3):  # max_len=3 → 最早的事件被淘汰，其 blob 也被删除
        await store.append("d1", EventKind.TEXT, text=str(i))
    assert await store.length("d1") == 3
    assert not blob_file.exists()


async def test_trim_older_than(store: EventStore):
    old = await store.append("d1", EventKind.AUDIO, payload=b"y" * 100, filename="old.wav")
    # Redis 的事件 ID 来自 Redis 服务器时钟，留出余量避免两边时钟的微小偏差
    assert await store.trim_older_than(utcnow() - timedelta(minutes=5)) == 0
    assert await store.trim_older_than(utcnow() + timedelta(minutes=5)) == 1
    assert await store.device_ids() == []
    assert not (store.blobs.root / old.blob_path).exists()


async def test_delete_stream_then_reuse(store: EventStore):
    await store.append("d1", EventKind.TEXT, text="a")
    assert len(await store.read_new("d1", "g", 10)) == 1
    await store.delete_stream("d1")
    await store.append("d1", EventKind.TEXT, text="b")
    # 流被删除重建后，消费组也要能自动恢复
    assert [e.text for e in await store.read_new("d1", "g", 10)] == ["b"]


def test_http_ingest_and_recent(client: TestClient, camera):
    assert (
        client.post("/api/devices/camera_001/events", json={"type": "text", "text": "你好"}).status_code
        == 201
    )
    data = client.post(
        "/api/devices/camera_001/events", json={"type": "sensor_data", "data": {"temperature": 25}}
    ).json()
    assert data["kind"] == "data" and data["stored_as"] == "none"
    image = base64.b64encode(b"\x89PNG" + b"0" * 2000).decode()
    media = client.post(
        "/api/devices/camera_001/events",
        json={"type": "media", "kind": "image", "media_type": "image/png", "data_b64": image},
    ).json()
    assert media["stored_as"] == "blob" and media["size"] == 2004

    recent = client.get("/api/devices/camera_001/events").json()
    assert [e["kind"] for e in recent] == ["text", "data", "image"]
    assert client.get("/api/devices/camera_001/stream").json()["length"] == 3

    bad = client.post("/api/devices/camera_001/events", json={"type": "media", "data_b64": "@@@"})
    assert bad.status_code == 422
    assert client.post("/api/devices/ghost/events", json={"type": "text", "text": "x"}).status_code == 404


def test_websocket_protocol(client: TestClient, camera):
    with client.websocket_connect("/api/devices/camera_001/ws") as ws:
        assert ws.receive_json()["type"] == "welcome"
        ws.send_text("纯文本消息")
        assert ws.receive_json()["type"] == "ack"
        ws.send_json({"type": "data", "data": {"temperature": 41.5}})
        assert ws.receive_json()["kind"] == "data"
        ws.send_bytes(b"MEDIA:audio:hello.wav:5\nRIFF!")
        ack = ws.receive_json()
        assert ack["kind"] == "audio"
        ws.send_json({"type": "heartbeat"})
        assert ws.receive_json()["type"] == "heartbeat_ack"
        ws.send_json({"type": "text"})  # 缺少 text
        assert ws.receive_json()["type"] == "error"

        assert client.get("/api/connections").json()["total_connections"] == 1
        assert client.get("/api/devices/camera_001").json()["ws_connected"] is True
        assert client.post("/api/devices/camera_001/messages", json={"type": "command", "x": 1}).json() == {
            "delivered": True
        }
        assert ws.receive_json() == {"type": "command", "x": 1}

    events = client.get("/api/devices/camera_001/events").json()
    assert [(e["kind"], e["filename"]) for e in events] == [
        ("text", None),
        ("data", None),
        ("audio", "hello.wav"),
    ]


def test_websocket_rejects_unknown_device(client: TestClient):
    with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/api/devices/ghost/ws") as ws:
        ws.receive_json()
    assert exc.value.code == 4404
