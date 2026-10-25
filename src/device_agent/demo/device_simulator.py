"""终端设备模拟器：通过 WebSocket 向服务上报数据，并打印服务推送回来的消息（如自动任务结果）。

运行示例:
  uv run python -m device_agent.demo.device_simulator --device camera_001 --temperature 45
  uv run python -m device_agent.demo.device_simulator --text "帮我把灯打开" --image photo.jpg --audio cmd.wav
"""

import argparse
import asyncio
import base64
import json
import mimetypes
from pathlib import Path

import websockets


def media_message(path: Path, kind: str) -> dict:
    return {
        "type": "media",
        "kind": kind,
        "media_type": mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        "filename": path.name,
        "data_b64": base64.b64encode(path.read_bytes()).decode(),
    }


async def main() -> None:
    parser = argparse.ArgumentParser(description="WebSocket 终端设备模拟器")
    parser.add_argument("--url", default="ws://localhost:8000")
    parser.add_argument("--device", default="camera_001")
    parser.add_argument("--token", default=None)
    parser.add_argument("--text", default=None, help="上报一条文本（例如语音识别结果或用户输入）")
    parser.add_argument("--temperature", type=float, default=None, help="上报一条温度传感器读数")
    parser.add_argument("--image", type=Path, default=None)
    parser.add_argument("--audio", type=Path, default=None)
    parser.add_argument("--wait", type=float, default=90, help="发送后等待服务推送的秒数")
    args = parser.parse_args()

    messages: list[dict] = []
    if args.temperature is not None:
        messages.append({"type": "sensor_data", "data": {"temperature": args.temperature, "unit": "°C"}})
    if args.text:
        messages.append({"type": "text", "text": args.text})
    if args.image:
        messages.append(media_message(args.image, "image"))
    if args.audio:
        messages.append(media_message(args.audio, "audio"))
    if not messages:
        messages = [
            {"type": "sensor_data", "data": {"temperature": 45.2, "humidity": 30, "unit": "°C"}},
            {"type": "text", "text": "（语音转写）这里怎么这么热，帮我看看现场情况"},
        ]

    url = f"{args.url}/api/devices/{args.device}/ws" + (f"?token={args.token}" if args.token else "")
    async with websockets.connect(url, max_size=None) as ws:
        print("<", await ws.recv())
        for message in messages:
            await ws.send(json.dumps(message, ensure_ascii=False))
            print(">", message["type"], "|", "<", await ws.recv())
        print(f"已上报 {len(messages)} 条数据，等待服务推送（最多 {args.wait:.0f}s）…")
        try:
            async with asyncio.timeout(args.wait):
                async for raw in ws:
                    event = json.loads(raw)
                    if event.get("type") == "ping":
                        continue
                    print("<", json.dumps(event, ensure_ascii=False, indent=2))
                    if event.get("type") == "task_result":
                        break
        except TimeoutError:
            print("等待超时（未触发自动任务，或意图识别监控未启用）")


if __name__ == "__main__":
    asyncio.run(main())
