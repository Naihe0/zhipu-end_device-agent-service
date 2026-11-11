"""会话记忆从 A2A 任务表重建：重启后仍在、包含工具调用摘要、受轮数与时间窗口限制。"""

from fastapi.testclient import TestClient

from device_agent.app import create_app
from tests.conftest import (
    ScriptedLLM,
    build_container,
    call_tool_ending_with,
    send_message,
    text_of_parts,
    text_reply,
)


def roles(call) -> list[str]:
    return [c.role for c in call["contents"]]


def test_history_survives_restart(settings, mcp_servers):
    first_llm = ScriptedLLM(replies=[text_reply("记住了，你叫小明。")])
    with TestClient(
        create_app(settings, container=build_container(settings, first_llm, mcp_servers))
    ) as client:
        task = send_message(client, "我叫小明")

    # 新的容器 = 重启后的服务进程：内存中的状态全部丢失，只剩数据库
    second_llm = ScriptedLLM(replies=[text_reply("你叫小明。")])
    with TestClient(
        create_app(settings, container=build_container(settings, second_llm, mcp_servers))
    ) as client:
        send_message(client, "我叫什么？", context_id=task["contextId"])

    [call] = second_llm.chat_calls
    assert roles(call) == ["user", "model", "user"]
    assert text_of_parts(call["contents"][0]) == "我叫小明"
    assert text_of_parts(call["contents"][1]) == "记住了，你叫小明。"


def test_history_includes_tool_call_summary(client: TestClient, llm, camera):
    llm.replies = [
        call_tool_ending_with("__set_light", {"on": True, "brightness": 30}),
        text_reply("灯已打开。"),
    ]
    first = send_message(client, "开灯，亮度 30")
    llm.replies = [text_reply("亮度是 30。")]
    send_message(client, "刚才亮度调到多少？", context_id=first["contextId"])

    model_turn = text_of_parts(llm.chat_calls[-1]["contents"][1])
    assert model_turn.startswith("灯已打开。")
    assert "camera_001.set_light" in model_turn and '"brightness": 30' in model_turn


def test_contexts_are_isolated(client: TestClient, llm):
    llm.replies = [text_reply("好的"), text_reply("你好")]
    send_message(client, "我叫小明")
    send_message(client, "我叫什么？")  # 新的 contextId
    assert roles(llm.chat_calls[-1]) == ["user"]


def test_max_turns(settings, llm, mcp_servers):
    settings = settings.model_copy(update={"conversation_max_turns": 2})
    llm.replies = [text_reply(f"回答{i}") for i in range(4)]
    with TestClient(create_app(settings, container=build_container(settings, llm, mcp_servers))) as client:
        context_id = send_message(client, "问题0")["contextId"]
        for i in range(1, 4):
            send_message(client, f"问题{i}", context_id=context_id)

    last = llm.chat_calls[-1]["contents"]
    assert [text_of_parts(c) for c in last] == ["问题1", "回答1", "问题2", "回答2", "问题3"]


def test_window_excludes_old_tasks(settings, llm, mcp_servers):
    settings = settings.model_copy(update={"conversation_window_hours": 0})
    llm.replies = [text_reply("好的"), text_reply("不记得")]
    with TestClient(create_app(settings, container=build_container(settings, llm, mcp_servers))) as client:
        context_id = send_message(client, "我叫小明")["contextId"]
        send_message(client, "我叫什么？", context_id=context_id)
    assert roles(llm.chat_calls[-1]) == ["user"]
