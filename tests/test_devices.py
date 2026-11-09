from datetime import timedelta

from fastapi.testclient import TestClient

from tests.conftest import CAMERA, CAMERA_URL


def test_health_and_root(client: TestClient):
    health = client.get("/health")
    assert health.status_code == 200
    assert health.json()["checks"] == {"database": "ok", "event_store": "memory:ok", "llm": "not_configured"}
    assert client.get("/").json()["a2a_endpoint"].endswith("/api/a2a")


def test_register_validates_mcp_and_caches_tools(client: TestClient, camera):
    assert camera["is_online"] is True
    assert {t["name"] for t in camera["tools"]} == {
        "capture_image",
        "analyze_scene",
        "read_sensor_data",
        "set_light",
    }
    schema = next(t for t in camera["tools"] if t["name"] == "set_light")["input_schema"]
    assert schema["required"] == ["on"]


def test_register_accepts_legacy_field_name(client: TestClient):
    body = {**CAMERA, "device_id": "legacy_cam"}
    body["mcp_server_url"] = body.pop("mcp_url")
    assert client.post("/api/devices", json=body).status_code == 201


def test_register_rejects_unreachable_mcp(client: TestClient):
    response = client.post("/api/devices", json={**CAMERA, "mcp_url": "http://nowhere.test/mcp"})
    assert response.status_code == 422
    assert "MCP" in response.json()["detail"]


def test_list_filter_update_delete(client: TestClient, camera):
    assert [d["device_id"] for d in client.get("/api/devices", params={"tool": "set_light"}).json()] == [
        "camera_001"
    ]
    assert client.get("/api/devices", params={"tool": "fly"}).json() == []

    patched = client.patch(
        "/api/devices/camera_001", json={"location": "会议室", "name": "会议室摄像头"}
    ).json()
    assert (patched["location"], patched["name"]) == ("会议室", "会议室摄像头")

    assert client.delete("/api/devices/camera_001").status_code == 204
    assert client.get("/api/devices/camera_001").status_code == 404


def test_direct_tool_call(client: TestClient, camera):
    ok = client.post(
        "/api/devices/camera_001/tools/set_light", json={"arguments": {"on": True, "brightness": 30}}
    )
    assert ok.json()["ok"] is True
    assert ok.json()["output"] == {"light_on": True, "brightness": 30}

    legacy = client.post(
        "/api/devices/camera_001/tools/read_sensor_data", json={"parameters": {"sensor_type": "battery"}}
    )
    assert set(legacy.json()["output"]["readings"]) == {"battery"}

    missing = client.post("/api/devices/camera_001/tools/fly", json={}).json()
    assert missing["ok"] is False and "fly" in missing["error"]


def test_sweep_marks_unreachable_devices_offline(client: TestClient, container, mcp_servers, camera):
    del mcp_servers[CAMERA_URL]
    container.devices._offline_after = timedelta(seconds=0)
    # 在应用所在的事件循环中执行维护任务
    assert client.portal.call(container.devices.sweep) == {"camera_001": False}
    assert client.get("/api/devices/camera_001").json()["is_online"] is False
    assert client.get("/api/devices", params={"online_only": True}).json() == []
