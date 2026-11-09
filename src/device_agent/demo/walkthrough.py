"""一键端到端演示：注册模拟设备与外部 Agent，然后走一遍三种路由与设备自动触发。

前置（三个终端分别运行）:
  uv run python -m device_agent.demo.mock_device     # :9001 模拟摄像头 MCP Server
  uv run python -m device_agent.demo.mock_agent      # :9002 模拟天气 A2A Agent
  uv run device-agent                                # :8000 本服务
然后: uv run python -m device_agent.demo.walkthrough
"""

import argparse
import asyncio
import json

import httpx
from a2a.client import ClientConfig, create_client

from device_agent.demo.a2a_chat import ask

QUESTIONS = [
    "你好，简单介绍一下你能做什么",
    "帮我用办公室摄像头拍一张照片，然后告诉我现在办公室的温湿度",
    "我下午要去上海出差，那边天气怎么样？",
]


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--device-mcp", default="http://localhost:9001/mcp")
    parser.add_argument("--agent-card", default="http://localhost:9002/.well-known/agent-card.json")
    args = parser.parse_args()

    async with httpx.AsyncClient(base_url=args.url, timeout=180) as http:
        print("健康检查:", (await http.get("/health")).json()["checks"])

        device = await http.post(
            "/api/devices",
            json={
                "device_id": "camera_001",
                "name": "办公室摄像头",
                "device_type": "smart_camera",
                "mcp_url": args.device_mcp,
                "location": "办公室",
                "system_prompt": "关注温度异常（高于 35°C 视为异常）、画面中的安全隐患以及现场人员的语音指令",
            },
        )
        device.raise_for_status()
        print("设备已注册:", [t["name"] for t in device.json()["tools"]])

        agent = await http.post("/api/agents", json={"card_url": args.agent_card})
        if agent.status_code == 409:
            print("外部 Agent 已存在")
        else:
            agent.raise_for_status()
            print("外部 Agent 已注册:", agent.json()["agent_id"])

        print("\n===== 通过 A2A 协议对话（流式） =====")
        a2a_http = httpx.AsyncClient(timeout=180)
        async with await create_client(
            args.url, client_config=ClientConfig(httpx_client=a2a_http, streaming=True)
        ) as client:
            for question in QUESTIONS:
                print(f"\nYou> {question}")
                await ask(client, question, None)

        print("\n===== 设备数据 → 多模态意图识别 → 自动任务 =====")
        previous = (await http.get("/api/intents", params={"limit": 1})).json()
        last_id = previous[0]["id"] if previous else 0
        for event in (
            {"type": "sensor_data", "data": {"temperature": 46.3, "humidity": 28, "unit": "°C"}},
            {"type": "text", "text": "（语音转写）这里怎么这么热，帮我把灯打开看看现场情况"},
        ):
            (await http.post("/api/devices/camera_001/events", json=event)).raise_for_status()
        # 立即触发一次扫描；若后台监控已抢先处理，则等待其结果写入日志
        await http.post("/api/intents/scan", params={"wait_for_tasks": True})
        log = await wait_for_intent(http, last_id)
        keys = ("intent_type", "confidence", "reasoning", "instruction", "task_state", "result_text", "error")
        print(
            json.dumps({k: log[k] for k in keys}, ensure_ascii=False, indent=2) if log else "未产生识别记录"
        )


async def wait_for_intent(http: httpx.AsyncClient, after_id: int) -> dict | None:
    async with asyncio.timeout(150):
        while True:
            logs = [
                log
                for log in (await http.get("/api/intents", params={"device_id": "camera_001"})).json()
                if log["id"] > after_id
            ]
            if logs:
                log = logs[-1]
                pending = (
                    log["intent_detected"]
                    and log["action_required"]
                    and log["confidence"] >= 0.6
                    and not log["error"]
                )
                if not pending or log["task_state"]:
                    return log
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(main())
