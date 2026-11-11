"""设备工具调用策略：决定一次工具调用是直接执行、提交人工审批还是拒绝。

设备上报的数据（文本、语音转写、图片）是不可信输入，由它触发的自动任务不应直接执行有破坏性的操作。
默认策略来自 MCP 规范的工具注解：
- readOnlyHint=true，或 destructiveHint=false → 直接执行
- 其余情况（包括未声明注解：按 MCP 规范 destructiveHint 默认为 true）→ 需要人工审批
设备注册时可用 `tool_policies` 按工具名覆盖；`deny` 对任何来源都生效。
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from device_agent.models import Device, ToolPolicyAction


class Origin(StrEnum):
    CLIENT = "client"  # 外部 A2A 客户端 / 管理接口发起
    DEVICE = "device"  # 由设备数据自动触发（意图识别）


@dataclass(slots=True, frozen=True)
class PolicyDecision:
    action: ToolPolicyAction
    reason: str


def default_action(tool: dict[str, Any]) -> PolicyDecision:
    annotations = tool.get("annotations") or {}
    if annotations.get("readOnlyHint") is True:
        return PolicyDecision(ToolPolicyAction.ALLOW, "只读工具")
    if annotations.get("destructiveHint") is False:
        return PolicyDecision(ToolPolicyAction.ALLOW, "工具声明为非破坏性操作")
    return PolicyDecision(
        ToolPolicyAction.CONFIRM, "该工具可能产生破坏性或不可逆的影响（未声明 destructiveHint=false）"
    )


def evaluate(device: Device, tool_name: str, origin: Origin) -> PolicyDecision:
    override = (device.tool_policies or {}).get(tool_name)
    if override == ToolPolicyAction.DENY:
        return PolicyDecision(ToolPolicyAction.DENY, "设备策略禁止调用该工具")
    if origin == Origin.CLIENT:
        return PolicyDecision(ToolPolicyAction.ALLOW, "由客户端直接发起")
    if override:
        return PolicyDecision(ToolPolicyAction(override), "设备策略指定")
    tool = next((t for t in device.tools if t["name"] == tool_name), {})
    return default_action(tool)
