"""事件（incident）：同一设备的相关触发归并、冷却合并、自动任务之间的上下文连续性。"""

from fastapi.testclient import TestClient

from device_agent.app import create_app
from tests.conftest import (
    build_container,
    call_tool_ending_with,
    get_task,
    intent_analysis,
    report,
    scan,
    text_of_parts,
    text_reply,
)


def triage_text(llm) -> str:
    return text_of_parts(llm.triage_calls[-1]["contents"][0])


def test_repeated_anomaly_is_merged(client: TestClient, llm, camera):
    llm.json_replies = [intent_analysis(), intent_analysis()]
    llm.replies = [text_reply("已处理高温告警。")]  # 只应运行一次编排

    report(client, {"type": "data", "data": {"temperature": 41}})
    [first] = scan(client)
    report(client, {"type": "data", "data": {"temperature": 42}})
    [second] = scan(client)

    assert first["task_state"] == "completed" and first["incident_id"]
    assert second["incident_id"] == first["incident_id"]
    assert second["task_state"] == "suppressed" and "合并" in second["result_text"]
    assert len(llm.chat_calls) == 1

    # 第二次分析时，模型看到了进行中的事件和之前的处理结果
    assert "进行中的事件" in triage_text(llm) and "已处理高温告警" in triage_text(llm)

    detail = client.get(f"/api/incidents/{first['incident_id']}").json()
    assert detail["status"] == "open" and detail["trigger_count"] == 2
    assert [i["id"] for i in detail["intents"]] == [first["id"], second["id"]]
    assert client.get("/api/intents/stats").json()["triggers_suppressed"] == 1


def test_user_command_is_not_merged_and_sees_previous_handling(client: TestClient, llm, camera):
    llm.json_replies = [
        intent_analysis(),
        intent_analysis(category="user_command", intent_type="turn_off_light", instruction="关闭补光灯"),
    ]
    llm.replies = [
        call_tool_ending_with("__set_light", {"on": True}),
        text_reply("已开灯提示高温。"),
        text_reply("已关灯。"),
    ]
    report(client, {"type": "data", "data": {"temperature": 41}})
    [first] = scan(client)
    report(client, {"type": "text", "text": "（语音转写）把灯关了"})
    [second] = scan(client)

    assert second["task_state"] == "completed" and second["incident_id"] == first["incident_id"]
    # 两个自动任务在同一个 A2A contextId（即事件 ID）中
    for log in (first, second):
        assert get_task(client, log["task_id"])["contextId"] == first["incident_id"]
    # 第二个任务的历史里有第一个任务做过的事
    history = llm.chat_calls[-1]["contents"]
    assert "[自动任务]" in text_of_parts(history[0])
    assert "camera_001.set_light" in text_of_parts(history[1])


def test_after_cooldown_same_incident_acts_again(settings, llm, mcp_servers, camera_body):
    settings = settings.model_copy(update={"intent_action_cooldown_seconds": 0})
    llm.json_replies = [intent_analysis(), intent_analysis(reasoning="温度继续升高")]
    llm.replies = [text_reply("第一次处理"), text_reply("第二次处理")]
    with TestClient(create_app(settings, container=build_container(settings, llm, mcp_servers))) as client:
        client.post("/api/devices", json=camera_body).raise_for_status()
        report(client, {"type": "data", "data": {"temperature": 41}})
        [first] = scan(client)
        report(client, {"type": "data", "data": {"temperature": 48}})
        [second] = scan(client)

    assert first["incident_id"] == second["incident_id"]
    assert second["task_state"] == "completed"
    assert text_of_parts(llm.chat_calls[-1]["contents"][1]).startswith("第一次处理")


def test_quiet_period_starts_new_incident(settings, llm, mcp_servers, camera_body):
    settings = settings.model_copy(update={"incident_quiet_minutes": 0})
    llm.json_replies = [intent_analysis(), intent_analysis()]
    llm.replies = [text_reply("处理 1"), text_reply("处理 2")]
    with TestClient(create_app(settings, container=build_container(settings, llm, mcp_servers))) as client:
        client.post("/api/devices", json=camera_body).raise_for_status()
        report(client, {"type": "data", "data": {"temperature": 41}})
        [first] = scan(client)
        report(client, {"type": "data", "data": {"temperature": 41}})
        [second] = scan(client)
        incidents = client.get("/api/incidents").json()

    assert first["incident_id"] != second["incident_id"]
    status = {i["incident_id"]: i["status"] for i in incidents}
    assert status == {first["incident_id"]: "closed", second["incident_id"]: "open"}
    # 新事件的自动任务没有旧事件的历史
    assert [c.role for c in llm.chat_calls[-1]["contents"]] == ["user"]


def test_close_incident(client: TestClient, llm, container, camera):
    llm.json_replies = [intent_analysis(), intent_analysis()]
    llm.replies = [text_reply("处理 1"), text_reply("处理 2")]
    report(client, {"type": "data", "data": {"temperature": 41}})
    [first] = scan(client)
    closed = client.post(f"/api/incidents/{first['incident_id']}/close").json()
    assert closed["status"] == "closed"

    report(client, {"type": "data", "data": {"temperature": 41}})
    [second] = scan(client)
    assert second["incident_id"] != first["incident_id"] and second["task_state"] == "completed"
    assert (
        client.get("/api/incidents", params={"status": "open"}).json()[0]["incident_id"]
        == second["incident_id"]
    )
    assert client.get("/api/incidents/nope").status_code == 404


def test_maintenance_closes_stale_incidents(settings, llm, mcp_servers, camera_body):
    settings = settings.model_copy(update={"incident_quiet_minutes": 0})
    llm.json_replies = [intent_analysis()]
    llm.replies = [text_reply("处理")]
    container = build_container(settings, llm, mcp_servers)
    with TestClient(create_app(settings, container=container)) as client:
        client.post("/api/devices", json=camera_body).raise_for_status()
        report(client, {"type": "data", "data": {"temperature": 41}})
        scan(client)
        stats = client.portal.call(container.maintenance_once)
        assert stats["incidents_closed"] == 1
        assert client.get("/api/incidents", params={"status": "open"}).json() == []
