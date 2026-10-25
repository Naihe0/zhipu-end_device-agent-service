import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from device_agent.app import create_app
from tests.conftest import build_container


@pytest.fixture
def secured(settings, llm, mcp_servers):
    settings = settings.model_copy(update={"api_token": SecretStr("s3cret")})
    with TestClient(create_app(settings, container=build_container(settings, llm, mcp_servers))) as client:
        yield client


def test_public_endpoints_stay_open(secured: TestClient):
    assert secured.get("/health").status_code == 200
    assert secured.get("/.well-known/agent-card.json").status_code == 200


def test_api_requires_token(secured: TestClient):
    assert secured.get("/api/devices").status_code == 401
    assert secured.post("/api/a2a", json={}).status_code == 401
    ok = secured.get("/api/devices", headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_websocket_requires_token(secured: TestClient):
    with pytest.raises(WebSocketDisconnect) as exc, secured.websocket_connect("/api/devices/x/ws") as ws:
        ws.receive_json()
    assert exc.value.code == 4401
    # 带 token 后通过鉴权（设备不存在 → 4404）
    with (
        pytest.raises(WebSocketDisconnect) as exc,
        secured.websocket_connect("/api/devices/x/ws?token=s3cret") as ws,
    ):
        ws.receive_json()
    assert exc.value.code == 4404
