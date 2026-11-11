import base64

import pytest
from fastapi.testclient import TestClient

from device_agent.services.intent_monitor import IntentAnalysis
from tests.conftest import call_tool_ending_with, text_reply


@pytest.fixture(params=["memory", pytest.param("redis", marks=pytest.mark.redis)])
def event_backend(request) -> str:
    """覆盖 conftest：意图识别对两种事件流后端各跑一遍。"""
    return request.param


def analysis(**overrides) -> IntentAnalysis:
    base = {
        "intent_detected": True,
        "category": "anomaly",
        "intent_type": "overheating",
        "confidence": 0.9,
        "reasoning": "温度 41.5°C 超过阈值 35°C",
        "action_required": True,
        "instruction": "办公室温度过高，请打开补光灯提示并读取传感器确认",
        "priority": "high",
    }
    return IntentAnalysis(**(base | overrides))


def test_anomaly_triggers_a2a_task_and_notifies_device(client: TestClient, llm, camera):
    llm.json_replies = [analysis()]
    llm.replies = [
        call_tool_ending_with("__set_light", {"on": True}),
        text_reply("已打开补光灯提示温度异常。"),
    ]

    with client.websocket_connect("/api/devices/camera_001/ws") as ws:
        ws.receive_json()  # welcome
        ws.send_json({"type": "data", "data": {"temperature": 41.5}})
        ws.receive_json()  # ack
        image = base64.b64encode(b"\xff\xd8fakejpeg").decode()
        ws.send_json({"type": "media", "kind": "image", "media_type": "image/jpeg", "data_b64": image})
        ws.receive_json()

        [log] = client.post("/api/intents/scan", params={"wait_for_tasks": True}).json()
        pushed = ws.receive_json()

    assert log["intent_detected"] and log["priority"] == "high"
    assert log["task_state"] == "completed" and log["result_text"] == "已打开补光灯提示温度异常。"
    assert pushed["type"] == "task_result" and pushed["task_id"] == log["task_id"]

    # 图片以内联二进制交给模型做多模态分析
    parts = llm.calls[0]["contents"][0].parts
    assert any(p.inline_data and p.inline_data.mime_type == "image/jpeg" for p in parts)
    assert "41.5" in "".join(p.text or "" for p in parts)
    # 自动任务的提示里带有设备上下文
    assert "camera_001" in llm.calls[1]["contents"][-1].parts[0].text

    # 自动任务与普通 A2A 任务一致，可以通过 GetTask 查询
    got = client.post(
        "/api/a2a",
        json={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": log["task_id"]}},
        headers={"A2A-Version": "1.0"},
    ).json()
    assert got["result"]["status"]["state"] == "TASK_STATE_COMPLETED"

    stats = client.get("/api/intents/stats").json()
    assert (stats["total_analyses"], stats["intents_detected"], stats["tasks_created"]) == (1, 1, 1)


def test_low_confidence_or_routine_data_does_not_trigger(client: TestClient, llm, camera):
    client.post("/api/devices/camera_001/events", json={"type": "data", "data": {"temperature": 24}})
    llm.json_replies = [analysis(confidence=0.3)]
    [log] = client.post("/api/intents/scan", params={"wait_for_tasks": True}).json()
    assert log["task_id"] is None

    # 已分析过的数据不会被重复分析
    assert client.post("/api/intents/scan").json() == []
    assert client.get("/api/intents", params={"device_id": "camera_001"}).json()[0]["id"] == log["id"]


def test_llm_error_is_logged(client: TestClient, llm, camera):
    from device_agent.services.llm import LLMError

    async def boom(*args, **kwargs):
        raise LLMError("quota exceeded")

    llm.generate_json = boom
    client.post("/api/devices/camera_001/events", json={"type": "text", "text": "hi"})
    [log] = client.post("/api/intents/scan").json()
    assert log["error"] == "quota exceeded" and log["intent_detected"] is False
