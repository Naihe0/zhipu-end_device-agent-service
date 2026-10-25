"""Agent Card：静态基础信息来自配置，技能列表随已注册设备/外部 Agent 动态生成。"""

from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentProvider, AgentSkill
from a2a.utils.constants import PROTOCOL_VERSION_0_3, PROTOCOL_VERSION_1_0

from device_agent.config import Settings
from device_agent.models import Device, RemoteAgent

INPUT_MODES = [
    "text/plain",
    "application/json",
    "image/png",
    "image/jpeg",
    "image/webp",
    "audio/wav",
    "audio/mpeg",
]
OUTPUT_MODES = ["text/plain", "application/json"]

BASE_SKILLS = [
    AgentSkill(
        id="general_assistant",
        name="智能对话",
        description="通用问答与对话，由 Gemini 直接回答不需要设备或外部能力的问题",
        tags=["chat", "qa", "llm"],
        examples=["你好，你能做什么？", "解释一下 A2A 协议"],
    ),
    AgentSkill(
        id="device_control",
        name="终端设备控制",
        description="通过 MCP 调用已接入终端设备的工具（拍照、读取传感器、场景分析等），可组合多个设备完成任务",
        tags=["device", "mcp", "iot"],
        examples=["用办公室摄像头拍一张照片", "读取一下所有传感器的温湿度"],
    ),
    AgentSkill(
        id="multimodal_intent_recognition",
        name="多模态意图识别",
        description="持续分析设备上报的文本、传感器数据、图片、音频，识别需要处理的意图并自动发起任务",
        tags=["multimodal", "intent", "automation"],
    ),
]


def base_card(settings: Settings) -> AgentCard:
    card = AgentCard(
        name=settings.app_name,
        description=settings.agent_description,
        version=settings.app_version,
        documentation_url=settings.public_base_url.rstrip("/") + "/docs",
        # 同一端点同时支持 A2A 1.0 与 0.3（启用了 SDK 的 v0.3 兼容层），两者都声明以便旧客户端发现
        supported_interfaces=[
            AgentInterface(protocol_binding="JSONRPC", url=settings.a2a_rpc_url, protocol_version=version)
            for version in (PROTOCOL_VERSION_1_0, PROTOCOL_VERSION_0_3)
        ],
        capabilities=AgentCapabilities(streaming=True, push_notifications=True),
        default_input_modes=INPUT_MODES,
        default_output_modes=OUTPUT_MODES,
        skills=BASE_SKILLS,
    )
    if settings.agent_provider_organization:
        card.provider.CopyFrom(
            AgentProvider(
                organization=settings.agent_provider_organization, url=settings.agent_provider_url or ""
            )
        )
    return card


def device_skill(device: Device) -> AgentSkill:
    tools = "; ".join(f"{t['name']}: {t.get('description', '')}" for t in device.tools) or "无"
    where = f"（位置: {device.location}）" if device.location else ""
    return AgentSkill(
        id=f"device.{device.device_id}",
        name=f"设备: {device.name}",
        description=f"{device.description or device.device_type}{where}。可用工具: {tools}",
        tags=["device", device.device_type, *(t["name"] for t in device.tools)],
    )


def build_card(settings: Settings, devices: list[Device], agents: list[RemoteAgent]) -> AgentCard:
    card = base_card(settings)
    card.skills.extend(device_skill(d) for d in devices)
    if agents:
        names = "、".join(a.name or a.agent_id for a in agents)
        card.skills.append(
            AgentSkill(
                id="agent_delegation",
                name="外部 Agent 协作",
                description=f"将专业任务委派给已注册的外部 A2A Agent：{names}",
                tags=["a2a", "delegation", *(a.agent_id for a in agents)],
            )
        )
    return card
