"""REST API 的请求/响应模型。"""

from datetime import datetime
from typing import Any, Literal

from pydantic import AliasChoices, AnyHttpUrl, BaseModel, ConfigDict, Field

from device_agent.models import DeviceType

DEVICE_ID_PATTERN = r"^[A-Za-z0-9_.:-]{1,100}$"


# ---------- 设备 ----------


class DeviceRegister(BaseModel):
    device_id: str = Field(pattern=DEVICE_ID_PATTERN, examples=["camera_001"])
    name: str = Field(min_length=1, max_length=200, examples=["办公室摄像头"])
    device_type: DeviceType = DeviceType.OTHER
    description: str = ""
    mcp_url: AnyHttpUrl = Field(
        validation_alias=AliasChoices("mcp_url", "mcp_server_url"),
        examples=["http://localhost:9001/mcp"],
        description="设备 MCP Server 的 Streamable HTTP 地址",
    )
    location: str | None = None
    system_prompt: str | None = Field(default=None, description="设备特定的意图识别提示词")
    supported_data_types: list[str] = Field(default_factory=list)
    hardware_info: dict[str, Any] = Field(default_factory=dict)


class DeviceUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    device_type: DeviceType | None = None
    description: str | None = None
    mcp_url: AnyHttpUrl | None = Field(
        default=None, validation_alias=AliasChoices("mcp_url", "mcp_server_url")
    )
    location: str | None = None
    system_prompt: str | None = None
    supported_data_types: list[str] | None = None
    hardware_info: dict[str, Any] | None = None


class ToolOut(BaseModel):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)


class DeviceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    device_id: str
    name: str
    device_type: str
    description: str
    mcp_url: str
    location: str | None
    system_prompt: str | None
    supported_data_types: list[str]
    hardware_info: dict[str, Any]
    tools: list[ToolOut]
    is_online: bool
    ws_connected: bool = False
    last_seen: datetime | None
    tools_refreshed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ToolCallRequest(BaseModel):
    arguments: dict[str, Any] = Field(
        default_factory=dict, validation_alias=AliasChoices("arguments", "parameters")
    )


class ToolCallOut(BaseModel):
    ok: bool
    output: Any = None
    error: str | None = None
    elapsed_ms: int


# ---------- 设备数据 ----------


class IngestMessage(BaseModel):
    """设备上报的一条数据（HTTP 与 WebSocket 共用）。"""

    type: Literal["text", "data", "sensor_data", "json_data", "media"] = "text"
    text: str | None = None
    data: dict[str, Any] | list[Any] | None = None
    kind: Literal["image", "audio", "video", "binary"] | None = Field(
        default=None, description="type=media 时的媒体类别"
    )
    media_type: str | None = Field(default=None, examples=["image/jpeg"])
    filename: str | None = None
    data_b64: str | None = Field(default=None, description="type=media 时的 base64 数据")
    metadata: dict[str, Any] = Field(default_factory=dict)


class EventOut(BaseModel):
    id: str
    device_id: str
    kind: str
    text: str | None
    data: Any
    media_type: str | None
    filename: str | None
    size: int
    stored_as: Literal["none", "inline", "blob"]
    metadata: dict[str, Any]
    created_at: datetime


class StreamStatsOut(BaseModel):
    device_id: str
    backend: str
    length: int
    last_event_id: str | None
    last_event_at: datetime | None


# ---------- 外部 Agent ----------


class RemoteAgentCreate(BaseModel):
    card_url: AnyHttpUrl = Field(
        validation_alias=AliasChoices("card_url", "agent_card_url"),
        examples=["http://localhost:9002/.well-known/agent-card.json"],
        description="Agent Card 地址，也可以只给出 Agent 的根地址",
    )
    agent_id: str | None = Field(default=None, pattern=DEVICE_ID_PATTERN)


class RemoteAgentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    agent_id: str
    card_url: str
    name: str
    description: str
    enabled: bool
    available: bool
    last_error: str | None
    skills: list[dict[str, Any]] = Field(default_factory=list)
    refreshed_at: datetime | None
    created_at: datetime


# ---------- 意图识别 ----------


class IntentLogOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    device_id: str
    event_ids: list[str]
    data_summary: str
    intent_detected: bool
    intent_type: str
    confidence: float
    reasoning: str
    action_required: bool
    instruction: str
    priority: str
    task_id: str | None
    task_state: str | None
    result_text: str | None
    error: str | None
    created_at: datetime


# ---------- 便捷对话 ----------


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, examples=["帮我用办公室摄像头拍张照片"])
    context_id: str | None = Field(default=None, description="复用同一 contextId 以保持多轮上下文")


class ChatResponse(BaseModel):
    task_id: str
    context_id: str
    state: str
    text: str
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
