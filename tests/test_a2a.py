from fastapi.testclient import TestClient

from tests.conftest import WEATHER_CARD_URL, call_tool_ending_with, text_reply

V1 = {"A2A-Version": "1.0"}


def send_v1(client: TestClient, text: str, **message_fields):
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "SendMessage",
        "params": {
            "message": {"messageId": "m-1", "role": "ROLE_USER", "parts": [{"text": text}], **message_fields}
        },
    }
    response = client.post("/api/a2a", json=body, headers=V1)
    assert response.status_code == 200, response.text
    return response.json()


def artifact_texts(task: dict) -> dict[str, object]:
    out = {}
    for artifact in task.get("artifacts", []):
        part = artifact["parts"][0]
        out[artifact["name"]] = part.get("text", part.get("data"))
    return out


def test_agent_card_reflects_devices_and_agents(client: TestClient, camera):
    card = client.get("/.well-known/agent-card.json").json()
    assert card["supportedInterfaces"][0]["url"] == "http://localhost:8000/api/a2a"
    assert card["supportedInterfaces"][0]["protocolVersion"] == "1.0"
    skill_ids = [s["id"] for s in card["skills"]]
    assert "device.camera_001" in skill_ids and "agent_delegation" not in skill_ids
    # v0.3 兼容字段同时存在
    assert card["protocolVersion"] and card["url"]

    client.post("/api/agents", json={"card_url": WEATHER_CARD_URL})
    card = client.get("/.well-known/agent-card.json").json()
    assert "agent_delegation" in [s["id"] for s in card["skills"]]


def test_plain_chat(client: TestClient, llm):
    llm.replies = [text_reply("你好！我是终端设备助手。")]
    task = send_v1(client, "你好")["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert artifact_texts(task) == {"response": "你好！我是终端设备助手。"}
    # 没有设备/外部 Agent 时不应给模型传工具
    assert llm.calls[0]["tools"] == []


def test_device_tool_call_loop(client: TestClient, llm, camera):
    llm.replies = [
        call_tool_ending_with("__capture_image", {"resolution": "1280x720"}),
        text_reply("已拍摄 1280x720 的照片。"),
    ]
    task = send_v1(client, "帮我拍张 1280x720 的照片")["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    artifacts = artifact_texts(task)
    assert artifacts["response"] == "已拍摄 1280x720 的照片。"
    [call] = artifacts["tool_calls"]
    assert call["kind"] == "device" and call["target"] == "camera_001.capture_image" and call["ok"] is True
    assert call["output"]["resolution"] == "1280x720"

    # 第二轮模型调用里应包含函数调用结果
    function_response = llm.calls[1]["contents"][-1].parts[0].function_response
    assert function_response.response["output"]["resolution"] == "1280x720"

    # 任务已持久化，可通过 GetTask 查询
    got = client.post(
        "/api/a2a",
        json={"jsonrpc": "2.0", "id": 2, "method": "GetTask", "params": {"id": task["id"]}},
        headers=V1,
    ).json()["result"]
    assert got["status"]["state"] == "TASK_STATE_COMPLETED"


def test_delegation_to_remote_agent(client: TestClient, llm):
    agent = client.post("/api/agents", json={"card_url": WEATHER_CARD_URL}).json()
    assert agent["agent_id"] == "weather_agent" and agent["skills"][0]["id"] == "weather"
    llm.replies = [
        call_tool_ending_with("agent__weather_agent", {"message": "查询上海今天的天气"}),
        lambda contents, tools: text_reply(
            contents[-1].parts[0].function_response.response["output"]["text"]
        ),
    ]
    task = send_v1(client, "上海天气怎么样")["result"]["task"]
    artifacts = artifact_texts(task)
    assert artifacts["response"].startswith("上海今日天气")
    assert artifacts["tool_calls"][0]["output"]["state"] == "completed"


def test_disabled_agent_not_offered(client: TestClient, llm):
    client.post("/api/agents", json={"card_url": WEATHER_CARD_URL})
    client.post("/api/agents/weather_agent/disable")
    llm.replies = [text_reply("ok")]
    send_v1(client, "hi")
    assert llm.calls[0]["tools"] == []


def test_v03_compat_message_send(client: TestClient, llm):
    llm.replies = [text_reply("兼容旧版客户端")]
    body = {
        "jsonrpc": "2.0",
        "id": "r1",
        "method": "message/send",
        "params": {
            "message": {
                "messageId": "m",
                "role": "user",
                "kind": "message",
                "parts": [{"kind": "text", "text": "hi"}],
            }
        },
    }
    result = client.post("/api/a2a", json=body).json()["result"]
    assert result["kind"] == "task" and result["status"]["state"] == "completed"
    assert result["artifacts"][0]["parts"][0]["text"] == "兼容旧版客户端"


def test_conversation_memory_by_context(client: TestClient, llm):
    llm.replies = [text_reply("记住了，你叫小明。"), text_reply("你叫小明。")]
    first = send_v1(client, "我叫小明")["result"]["task"]
    send_v1(client, "我叫什么？", contextId=first["contextId"])
    second_call_contents = llm.calls[1]["contents"]
    assert [c.role for c in second_call_contents] == ["user", "model", "user"]
    assert second_call_contents[0].parts[0].text == "我叫小明"


def test_llm_not_configured_fails_task_cleanly(settings, mcp_servers):
    from device_agent.app import create_app
    from device_agent.services.llm import UnconfiguredLLM
    from tests.conftest import build_container

    container = build_container(settings, UnconfiguredLLM(), mcp_servers)
    with TestClient(create_app(settings, container=container)) as client:
        task = send_v1(client, "你好")["result"]["task"]
    assert task["status"]["state"] == "TASK_STATE_FAILED"
    assert "GEMINI_API_KEY" in task["status"]["message"]["parts"][0]["text"]


def test_chat_endpoint(client: TestClient, llm, camera):
    llm.replies = [call_tool_ending_with("__set_light", {"on": True}), text_reply("灯已打开。")]
    body = client.post("/api/chat", json={"message": "开灯"}).json()
    assert body["state"] == "completed" and body["text"] == "灯已打开。"
    assert body["tool_calls"][0]["output"] == {"light_on": True, "brightness": 80}
