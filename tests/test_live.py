"""真实 Gemini 端到端测试（默认跳过）。运行: uv run pytest -m live

设备与外部 Agent 仍是进程内模拟，只有 LLM 走真实 API。需要 .env 或环境变量里的 GEMINI_API_KEY。
"""

import base64
import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from device_agent.app import create_app
from device_agent.config import Settings
from device_agent.services.llm import create_llm
from tests.conftest import CAMERA, WEATHER_CARD_URL, build_container

pytestmark = pytest.mark.live


def solid_png(rgb: tuple[int, int, int], size: int = 32) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    row = b"\x00" + bytes(rgb) * size
    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * size))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def live_client(settings: Settings, mcp_servers):
    key = Settings().gemini_api_key  # 从 .env / 环境变量读取
    if key is None:
        pytest.skip("未配置 GEMINI_API_KEY")
    settings = settings.model_copy(update={"gemini_api_key": key})
    container = build_container(settings, create_llm(settings), mcp_servers)
    with TestClient(create_app(settings, container=container)) as client:
        client.post("/api/devices", json=CAMERA).raise_for_status()
        client.post("/api/agents", json={"card_url": WEATHER_CARD_URL}).raise_for_status()
        yield client


def test_routes_to_device_tool(live_client: TestClient):
    body = live_client.post("/api/chat", json={"message": "把办公室摄像头的补光灯打开，亮度调到 40"}).json()
    assert body["state"] == "completed", body
    calls = [c for c in body["tool_calls"] if c["target"] == "camera_001.set_light"]
    assert calls and calls[0]["arguments"].get("brightness") == 40


def test_routes_to_remote_agent(live_client: TestClient):
    body = live_client.post("/api/chat", json={"message": "成都今天天气怎么样？"}).json()
    assert body["state"] == "completed", body
    assert any(c["kind"] == "agent" for c in body["tool_calls"])
    assert "成都" in body["text"]


def test_multimodal_image_over_a2a(live_client: TestClient):
    message = {
        "messageId": "img-1",
        "role": "ROLE_USER",
        "parts": [
            {"text": "这张图片主要是什么颜色？只回答颜色。"},
            {"raw": base64.b64encode(solid_png((220, 20, 20))).decode(), "mediaType": "image/png"},
        ],
    }
    response = live_client.post(
        "/api/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": message}},
        headers={"A2A-Version": "1.0"},
    ).json()
    task = response["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "红" in task["artifacts"][0]["parts"][0]["text"]
