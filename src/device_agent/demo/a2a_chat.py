"""交互式 A2A 客户端：用官方 a2a-sdk 以流式方式与本服务对话（多轮共享 contextId）。

运行: uv run python -m device_agent.demo.a2a_chat [--url http://localhost:8000]
"""

import argparse
import asyncio
import uuid

import httpx
from a2a.client import ClientConfig, create_client
from a2a.helpers import get_artifact_text, get_data_parts, get_message_text
from a2a.types import Message, Part, Role, SendMessageRequest, TaskState


async def ask(client, text: str, context_id: str | None) -> str | None:
    message = Message(
        message_id=str(uuid.uuid4()),
        role=Role.ROLE_USER,
        parts=[Part(text=text)],
        context_id=context_id or "",
    )
    async for event in client.send_message(SendMessageRequest(message=message)):
        kind = event.WhichOneof("payload")
        if kind == "task":
            context_id = event.task.context_id
        elif kind == "status_update":
            context_id = event.status_update.context_id
            status = event.status_update.status
            if status.state == TaskState.TASK_STATE_WORKING and status.HasField("message"):
                print(f"  … {get_message_text(status.message)}")
            elif status.state == TaskState.TASK_STATE_FAILED:
                print(f"  ✗ 失败: {get_message_text(status.message)}")
        elif kind == "artifact_update":
            artifact = event.artifact_update.artifact
            if artifact.name == "tool_calls":
                for call in get_data_parts(artifact.parts)[0]:
                    mark = "✓" if call["ok"] else "✗"
                    print(f"  {mark} [{call['kind']}] {call['target']} {call['arguments']}")
            else:
                print(f"\nAgent> {get_artifact_text(artifact)}\n")
        elif kind == "message":
            print(f"\nAgent> {get_message_text(event.message)}\n")
    return context_id


async def main() -> None:
    parser = argparse.ArgumentParser(description="A2A 交互式客户端")
    parser.add_argument("--url", default="http://localhost:8000", help="Agent 根地址（自动读取 Agent Card）")
    parser.add_argument("--token", default=None, help="服务设置了 API_TOKEN 时传入")
    parser.add_argument("message", nargs="*", help="直接发送一条消息后退出")
    args = parser.parse_args()

    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    http = httpx.AsyncClient(timeout=180, headers=headers)
    async with await create_client(
        args.url, client_config=ClientConfig(httpx_client=http, streaming=True)
    ) as client:
        if args.message:
            await ask(client, " ".join(args.message), None)
            return
        print("已连接，输入消息开始对话（/new 新会话，/quit 退出）")
        context_id = None
        while True:
            try:
                text = (await asyncio.to_thread(input, "You> ")).strip()
            except (EOFError, KeyboardInterrupt):
                break
            if text in ("/quit", "/exit"):
                break
            if text == "/new":
                context_id = None
                continue
            if text:
                context_id = await ask(client, text, context_id)


if __name__ == "__main__":
    asyncio.run(main())
