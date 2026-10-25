"""MCP 网关：通过官方 MCP Python SDK 与终端设备的 MCP Server 通信（Streamable HTTP）。"""

import asyncio
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcp import Client

logger = logging.getLogger(__name__)

# 测试时可注入：url -> mcp.Client（例如连接进程内的 MCPServer）
ClientFactory = Callable[[str], Client]


class MCPConnectionError(RuntimeError):
    pass


@dataclass(slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


@dataclass(slots=True)
class ToolCallResult:
    ok: bool
    output: Any
    elapsed_ms: int
    error: str | None = None


def _content_to_json(block: Any) -> Any:
    data = block.model_dump(exclude_none=True, by_alias=False)
    kind = data.get("type")
    if kind == "text":
        text = data.get("text", "")
        try:
            return json.loads(text)
        except ValueError:
            return text
    if kind in ("image", "audio"):
        # 二进制内容不直接塞给 LLM，只给出摘要
        return {"type": kind, "mime_type": data.get("mime_type"), "size_b64": len(data.get("data", ""))}
    return data


class MCPGateway:
    def __init__(self, timeout_seconds: float, client_factory: ClientFactory | None = None) -> None:
        self._timeout = timeout_seconds
        self._factory: ClientFactory = client_factory or (lambda url: Client(url))

    async def list_tools(self, url: str) -> list[ToolSpec]:
        try:
            async with asyncio.timeout(self._timeout), self._factory(url) as client:
                result = await client.list_tools()
        except TimeoutError as exc:
            raise MCPConnectionError(f"连接 MCP Server 超时 ({self._timeout}s): {url}") from exc
        except Exception as exc:
            raise MCPConnectionError(f"无法连接 MCP Server {url}: {exc}") from exc
        return [
            ToolSpec(name=t.name, description=t.description or "", input_schema=t.input_schema or {})
            for t in result.tools
        ]

    async def call_tool(self, url: str, name: str, arguments: dict[str, Any]) -> ToolCallResult:
        start = time.perf_counter()

        def elapsed() -> int:
            return int((time.perf_counter() - start) * 1000)

        try:
            async with asyncio.timeout(self._timeout), self._factory(url) as client:
                result = await client.call_tool(name, arguments)
        except TimeoutError:
            return ToolCallResult(False, None, elapsed(), f"工具调用超时 ({self._timeout}s)")
        except Exception as exc:
            logger.warning("MCP 工具调用失败 %s %s: %s", url, name, exc)
            return ToolCallResult(False, None, elapsed(), f"MCP 调用失败: {exc}")

        if result.structured_content is not None:
            output: Any = result.structured_content
        else:
            blocks = [_content_to_json(b) for b in result.content]
            output = blocks[0] if len(blocks) == 1 else blocks
        if result.is_error:
            return ToolCallResult(False, output, elapsed(), str(output))
        return ToolCallResult(True, output, elapsed())
