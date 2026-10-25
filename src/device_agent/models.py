"""ORM 模型。A2A 任务本身由 a2a-sdk 的 DatabaseTaskStore 存在同一个库的 `tasks` 表中。"""

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, Boolean, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from device_agent.database import Base, UTCDateTime, utcnow


class DeviceType(StrEnum):
    IOT_SENSOR = "iot_sensor"
    SMART_CAMERA = "smart_camera"
    SMART_SPEAKER = "smart_speaker"
    MOBILE_APP = "mobile_app"
    DESKTOP_APP = "desktop_app"
    INDUSTRIAL_PLC = "industrial_plc"
    PRINTER = "printer"
    DISPLAY = "display"
    ROBOT = "robot"
    OTHER = "other"


class Device(Base):
    """终端设备。每个设备暴露一个 MCP Server（Streamable HTTP），其工具即设备能力。"""

    __tablename__ = "devices"

    device_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    device_type: Mapped[str] = mapped_column(String(50), default=DeviceType.OTHER)
    description: Mapped[str] = mapped_column(Text, default="")
    mcp_url: Mapped[str] = mapped_column(String(500))
    location: Mapped[str | None] = mapped_column(String(200))
    system_prompt: Mapped[str | None] = mapped_column(Text, doc="设备特定的意图识别提示词")
    supported_data_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    hardware_info: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    tools: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, default=list, doc="MCP tools/list 结果缓存：[{name, description, input_schema}]"
    )
    is_online: Mapped[bool] = mapped_column(Boolean, default=True)
    last_seen: Mapped[datetime | None] = mapped_column(UTCDateTime)
    tools_refreshed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class RemoteAgent(Base):
    """通过 Agent Card URL 注册的外部 A2A Agent，可被编排器委派任务。"""

    __tablename__ = "remote_agents"

    agent_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    card_url: Mapped[str] = mapped_column(String(500), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    card: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, doc="最近一次获取的 Agent Card")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    available: Mapped[bool] = mapped_column(Boolean, default=False)
    last_error: Mapped[str | None] = mapped_column(Text)
    refreshed_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class IntentLog(Base):
    """多模态意图识别记录：一次对某设备新数据的分析。"""

    __tablename__ = "intent_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(100), index=True)
    event_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    data_summary: Mapped[str] = mapped_column(Text, default="")
    intent_detected: Mapped[bool] = mapped_column(Boolean, default=False)
    intent_type: Mapped[str] = mapped_column(String(100), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    action_required: Mapped[bool] = mapped_column(Boolean, default=False)
    instruction: Mapped[str] = mapped_column(Text, default="")
    priority: Mapped[str] = mapped_column(String(20), default="low")
    task_id: Mapped[str | None] = mapped_column(String(100))
    task_state: Mapped[str | None] = mapped_column(String(50))
    result_text: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
