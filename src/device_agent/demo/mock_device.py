"""模拟终端设备：一个用官方 MCP SDK 实现的智能摄像头 MCP Server（Streamable HTTP）。

工具都声明了 MCP 标准注解（readOnlyHint / destructiveHint），服务据此决定自动任务能否直接调用：
只读工具与 set_light（非破坏性）直接执行；reboot_device（破坏性）在自动任务中需要人工审批。

运行: uv run python -m device_agent.demo.mock_device --port 9001
MCP 端点: http://localhost:9001/mcp
"""

import argparse
import random
from datetime import datetime
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def build_server(location: str = "办公室") -> MCPServer:
    server = MCPServer(
        "mock-smart-camera",
        instructions=f"位于{location}的智能摄像头，带温湿度传感器与补光灯。",
        version="1.0.0",
    )
    state: dict[str, Any] = {"light_on": False, "brightness": 0, "battery": 86, "captures": 0, "reboots": 0}

    @server.tool(description="拍摄一张图像，返回图像文件信息", annotations=READ_ONLY)
    def capture_image(resolution: str = "1920x1080", image_format: Literal["jpg", "png"] = "jpg") -> dict:
        state["captures"] += 1
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        return {
            "filename": f"capture_{stamp}_{state['captures']}.{image_format}",
            "resolution": resolution,
            "location": location,
            "captured_at": datetime.now().isoformat(timespec="seconds"),
            "light_on": state["light_on"],
        }

    @server.tool(description="分析当前画面中的场景（人数、光线、安全状态等）", annotations=READ_ONLY)
    def analyze_scene(detail_level: Literal["basic", "detailed"] = "basic") -> dict:
        result: dict[str, Any] = {
            "environment": location,
            "people_count": 2,
            "lighting": "充足" if state["light_on"] or 8 <= datetime.now().hour < 18 else "偏暗",
            "safety_status": "正常",
        }
        if detail_level == "detailed":
            result |= {
                "objects": ["桌子", "椅子", "电脑", "文件柜"],
                "activities": ["使用电脑", "阅读文档"],
                "recommendations": ["保持当前照明", "注意定时休息"],
            }
        return result

    @server.tool(description="读取摄像头内置传感器的数据：温度(°C)、湿度(%)、电量(%)", annotations=READ_ONLY)
    def read_sensor_data(sensor_type: Literal["temperature", "humidity", "battery", "all"] = "all") -> dict:
        readings = {
            "temperature": {"value": round(24 + random.uniform(-1.5, 1.5), 1), "unit": "°C"},
            "humidity": {"value": round(55 + random.uniform(-5, 5), 1), "unit": "%"},
            "battery": {"value": state["battery"], "unit": "%"},
        }
        selected = readings if sensor_type == "all" else {sensor_type: readings[sensor_type]}
        return {
            "location": location,
            "readings": selected,
            "read_at": datetime.now().isoformat(timespec="seconds"),
        }

    @server.tool(
        description="控制补光灯：打开/关闭并设置亮度(0-100)",
        annotations=ToolAnnotations(
            readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
        ),
    )
    def set_light(on: bool, brightness: int = 80) -> dict:
        state["light_on"] = on
        state["brightness"] = max(0, min(100, brightness)) if on else 0
        return {"light_on": state["light_on"], "brightness": state["brightness"]}

    @server.tool(
        description="重启摄像头（拍摄与传感器会中断约 30 秒），用于设备异常时恢复",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
    )
    def reboot_device(reason: str = "") -> dict:
        state["reboots"] += 1
        return {"rebooted": True, "reason": reason, "reboot_count": state["reboots"]}

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="模拟智能摄像头 MCP Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9001)
    parser.add_argument("--location", default="办公室")
    args = parser.parse_args()
    print(f"MCP 端点: http://{args.host}:{args.port}/mcp")
    build_server(args.location).run(
        transport="streamable-http", host=args.host, port=args.port, streamable_http_path="/mcp"
    )


if __name__ == "__main__":
    main()
