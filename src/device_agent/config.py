"""集中配置：所有可调参数都来自环境变量或 `.env`，代码中不硬编码。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- 服务 ---
    app_name: str = "终端设备 A2A Agent 服务"
    app_version: str = "2.0.0"
    agent_description: str = (
        "智能终端设备代理服务：接入终端设备（MCP），多模态意图识别，并在设备与外部 A2A Agent 间智能路由任务"
    )
    agent_provider_organization: str | None = None
    agent_provider_url: str | None = None
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"
    public_base_url: str = Field(
        default="http://localhost:8000", description="对外可访问的地址，写入 Agent Card"
    )
    cors_origins: list[str] = Field(default_factory=lambda: ["*"])
    api_token: SecretStr | None = Field(
        default=None, description="设置后，除健康检查/Agent Card 外的所有接口都需要 Bearer Token"
    )

    # --- 存储 ---
    database_url: str = "sqlite+aiosqlite:///./data/device_agent.db"
    data_dir: Path = Path("./data")
    event_store_backend: Literal["redis", "memory"] = Field(
        default="redis",
        description="设备事件流后端：redis = Redis Streams；memory = 进程内（仅用于测试/临时调试）",
    )
    redis_url: str = "redis://localhost:6379/0"
    redis_stream_prefix: str = "device_agent:stream:"
    stream_max_len: int = Field(default=1000, description="每个设备事件流保留的最大条数")
    stream_retention_hours: int = 24
    inline_blob_max_bytes: int = Field(
        default=256 * 1024, description="超过该大小的二进制数据写入文件系统，事件里只保存引用"
    )
    max_upload_bytes: int = 20 * 1024 * 1024

    # --- LLM (Gemini) ---
    gemini_api_key: SecretStr | None = None
    gemini_model: str = "gemini-3.6-flash"
    gemini_thinking_level: str | None = Field(
        default="low", description="Gemini 3 思考深度 (minimal/low/medium/high)，留空使用模型默认"
    )
    llm_temperature: float | None = Field(
        default=None, description="留空使用模型默认值（Gemini 3 官方建议保持默认）"
    )
    llm_timeout_seconds: float = 60.0

    # --- 编排 ---
    orchestrator_max_steps: int = Field(default=6, description="一次请求内最多的工具调用轮数")
    conversation_max_turns: int = Field(
        default=20, description="同一 contextId 下带入模型的历史任务数上限（历史从任务表重建）"
    )
    conversation_window_hours: float = Field(
        default=24, description="只带入最近这段时间内更新过的历史任务；更早的不再进入模型上下文"
    )

    # --- 终端设备 / MCP ---
    mcp_timeout_seconds: float = 15.0
    device_offline_after_seconds: int = Field(
        default=120, description="超过该时长无心跳/无数据则标记设备离线"
    )
    maintenance_interval_seconds: int = 30

    # --- 外部 A2A Agent ---
    remote_agent_timeout_seconds: float = 30.0
    remote_task_poll_interval_seconds: float = 1.0
    remote_task_max_wait_seconds: float = 120.0

    # --- 多模态意图识别 ---
    intent_monitor_enabled: bool = True
    intent_scan_interval_seconds: float = 10.0
    intent_max_events_per_scan: int = 30
    intent_min_confidence: float = 0.6
    intent_max_media_bytes: int = Field(
        default=8 * 1024 * 1024, description="单次分析中内联给模型的媒体数据总上限"
    )
    incident_quiet_minutes: float = Field(
        default=30, description="事件超过这段时间没有新的触发即自动关闭；之后的触发会开启新事件"
    )
    intent_action_cooldown_seconds: float = Field(
        default=120,
        description="同一事件中同类意图两次自动处理的最小间隔；期间的触发被合并（用户指令类不受限）",
    )

    # --- 自动任务的工具审批 ---
    approval_ttl_minutes: float = Field(default=30, description="待审批的工具调用超过该时长未处理即过期")

    @property
    def blob_dir(self) -> Path:
        return self.data_dir / "blobs"

    @property
    def a2a_rpc_path(self) -> str:
        return "/api/a2a"

    @property
    def a2a_rpc_url(self) -> str:
        return self.public_base_url.rstrip("/") + self.a2a_rpc_path

    @property
    def use_redis(self) -> bool:
        return self.event_store_backend == "redis"


@lru_cache
def get_settings() -> Settings:
    return Settings()
