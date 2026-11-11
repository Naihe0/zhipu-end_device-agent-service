"""测试夹具：脚本化的假 LLM + 进程内 MCP 设备 + 进程内外部 A2A Agent；
数据库与事件流使用本机真实的 PostgreSQL / Redis（同时覆盖 SQLite 与内存实现）。"""

import asyncio
import os
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import redis
from dotenv import dotenv_values
from fastapi.testclient import TestClient
from google.genai import types
from mcp import Client

from device_agent.app import create_app
from device_agent.config import Settings
from device_agent.container import Container
from device_agent.demo.mock_agent import build_app as build_weather_agent
from device_agent.demo.mock_device import build_server as build_camera

CAMERA_URL = "http://camera.test/mcp"
WEATHER_CARD_URL = "http://weather.test/.well-known/agent-card.json"

Reply = types.Content | Callable[[list[types.Content], list[types.Tool] | None], types.Content]


def text_reply(text: str) -> types.Content:
    return types.Content(role="model", parts=[types.Part(text=text)])


def call_reply(name: str, args: dict[str, Any]) -> types.Content:
    return types.Content(
        role="model", parts=[types.Part(function_call=types.FunctionCall(name=name, args=args))]
    )


def call_tool_ending_with(suffix: str, args: dict[str, Any]) -> Reply:
    """按函数名后缀选择工具（函数名由服务动态生成，如 dev__camera_001__capture_image）。"""

    def pick(_: list[types.Content], tools: list[types.Tool] | None) -> types.Content:
        names = [d.name for t in tools or [] for d in t.function_declarations or []]
        matches = [n for n in names if n.endswith(suffix)]
        assert matches, f"没有以 {suffix} 结尾的工具: {names}"
        return call_reply(matches[0], args)

    return pick


@dataclass
class ScriptedLLM:
    replies: list[Reply] = field(default_factory=list)
    json_replies: list[Any] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def generate(self, contents, *, system_instruction=None, tools=None, text_only=False):
        self.calls.append(
            {"contents": list(contents), "tools": tools, "text_only": text_only, "system": system_instruction}
        )
        reply = self.replies.pop(0) if self.replies else text_reply("(默认回复)")
        return reply(contents, tools) if callable(reply) else reply

    async def generate_json(self, contents, schema, *, system_instruction=None):
        self.calls.append({"contents": list(contents), "schema": schema, "system": system_instruction})
        return self.json_replies.pop(0)

    @property
    def triage_calls(self) -> list[dict[str, Any]]:
        """意图识别（结构化输出）调用。"""
        return [c for c in self.calls if "schema" in c]

    @property
    def chat_calls(self) -> list[dict[str, Any]]:
        """编排器（工具调用循环）调用。"""
        return [c for c in self.calls if "schema" not in c]

    async def aclose(self) -> None:
        return None


# ---------- 真实的 Redis / PostgreSQL ----------

ENV = {**dotenv_values(Path(__file__).parents[1] / ".env"), **os.environ}
TEST_REDIS_URL = ENV.get("TEST_REDIS_URL") or "redis://localhost:6379/15"
HINT = "Windows 上先运行 scripts/dev-services.ps1；或用 -m 'not redis and not postgres' 排除这些用例"


def require_redis() -> None:
    try:
        redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=3).ping()
    except redis.RedisError as exc:
        pytest.fail(f"Redis 不可用 ({TEST_REDIS_URL}): {exc}。{HINT}", pytrace=False)


def postgres_admin_dsn() -> str:
    if dsn := ENV.get("TEST_POSTGRES_DSN"):
        return dsn
    if password := ENV.get("POSTGRES_APP_PASSWORD"):
        return f"postgresql://device_agent:{password}@localhost:5432/postgres"
    pytest.fail(f"未配置 TEST_POSTGRES_DSN 或 POSTGRES_APP_PASSWORD。{HINT}", pytrace=False)


async def _pg_execute(dsn: str, sql: str) -> None:
    conn = await asyncpg.connect(dsn, timeout=5)
    try:
        await conn.execute(sql)
    finally:
        await conn.close()


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def database_url(request, tmp_path: Path) -> Iterator[str]:
    """每个用例一个全新数据库：SQLite 文件，或 PostgreSQL 上的临时库（用完删除）。"""
    if request.param == "sqlite":
        yield f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
        return
    admin = postgres_admin_dsn()
    name = f"device_agent_test_{uuid.uuid4().hex[:12]}"
    try:
        asyncio.run(_pg_execute(admin, f'CREATE DATABASE "{name}"'))
    except (OSError, asyncpg.PostgresError) as exc:
        pytest.fail(f"PostgreSQL 不可用: {exc}。{HINT}", pytrace=False)
    yield admin.replace("postgresql://", "postgresql+asyncpg://").rsplit("/", 1)[0] + f"/{name}"
    asyncio.run(_pg_execute(admin, f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
def event_backend() -> str:
    """默认用内存事件流；test_streams / test_intents 覆盖为 memory + redis 两种。"""
    return "memory"


@pytest.fixture
def redis_prefix(event_backend: str) -> Iterator[str]:
    prefix = f"test:{uuid.uuid4().hex[:12]}:"
    if event_backend == "redis":
        require_redis()
    yield prefix
    if event_backend == "redis":
        client = redis.Redis.from_url(TEST_REDIS_URL)
        for key in client.scan_iter(match=f"{prefix}*"):
            client.delete(key)


@pytest.fixture
def settings(tmp_path: Path, database_url: str, event_backend: str, redis_prefix: str) -> Settings:
    return Settings(
        _env_file=None,
        database_url=database_url,
        data_dir=tmp_path / "data",
        event_store_backend=event_backend,
        redis_url=TEST_REDIS_URL,
        redis_stream_prefix=redis_prefix,
        gemini_api_key=None,
        intent_monitor_enabled=False,
        remote_task_poll_interval_seconds=0.05,
        inline_blob_max_bytes=1024,
    )


@pytest.fixture
def llm() -> ScriptedLLM:
    return ScriptedLLM()


@pytest.fixture
def mcp_servers() -> dict[str, Any]:
    return {CAMERA_URL: build_camera()}


def build_container(settings: Settings, llm: ScriptedLLM, mcp_servers: dict[str, Any]) -> Container:
    def mcp_factory(url: str) -> Client:
        if url not in mcp_servers:
            raise ConnectionError(f"connection refused: {url}")
        return Client(mcp_servers[url])

    weather = build_weather_agent("http://weather.test", delay_seconds=0.1)
    return Container.build(
        settings,
        llm=llm,
        mcp_client_factory=mcp_factory,
        agent_transport=httpx.ASGITransport(app=weather),
    )


@pytest.fixture
def container(settings: Settings, llm: ScriptedLLM, mcp_servers: dict[str, Any]) -> Container:
    return build_container(settings, llm, mcp_servers)


@pytest.fixture
def client(settings: Settings, container: Container) -> Iterator[TestClient]:
    with TestClient(create_app(settings, container=container)) as test_client:
        yield test_client


CAMERA = {
    "device_id": "camera_001",
    "name": "办公室摄像头",
    "device_type": "smart_camera",
    "mcp_url": CAMERA_URL,
    "location": "办公室",
    "system_prompt": "关注温度异常（>35°C）与语音指令",
}


@pytest.fixture
def camera_body() -> dict[str, Any]:
    return dict(CAMERA)


@pytest.fixture
def camera(client: TestClient) -> dict[str, Any]:
    response = client.post("/api/devices", json=CAMERA)
    assert response.status_code == 201, response.text
    return response.json()


# ---------- 常用操作 ----------


def text_of_parts(content: types.Content) -> str:
    return "".join(p.text or "" for p in content.parts or [])


def send_message(client: TestClient, text: str, context_id: str | None = None) -> dict[str, Any]:
    """A2A 1.0 SendMessage，返回 Task。"""
    message: dict[str, Any] = {"messageId": uuid.uuid4().hex, "role": "ROLE_USER", "parts": [{"text": text}]}
    if context_id:
        message["contextId"] = context_id
    body = {"jsonrpc": "2.0", "id": 1, "method": "SendMessage", "params": {"message": message}}
    response = client.post("/api/a2a", json=body, headers={"A2A-Version": "1.0"})
    assert response.status_code == 200, response.text
    return response.json()["result"]["task"]


def get_task(client: TestClient, task_id: str) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": task_id}}
    return client.post("/api/a2a", json=body, headers={"A2A-Version": "1.0"}).json()["result"]


def report(client: TestClient, payload: dict[str, Any], device_id: str = "camera_001") -> None:
    response = client.post(f"/api/devices/{device_id}/events", json=payload)
    assert response.status_code == 201, response.text


def scan(client: TestClient) -> list[dict[str, Any]]:
    """触发一次意图识别扫描，并等待自动任务执行完毕。"""
    return client.post("/api/intents/scan", params={"wait_for_tasks": True}).json()


def intent_analysis(**overrides) -> Any:
    from device_agent.services.intent_monitor import IntentAnalysis

    base = {
        "intent_detected": True,
        "category": "anomaly",
        "intent_type": "overheating",
        "confidence": 0.9,
        "reasoning": "温度超过阈值 35°C",
        "action_required": True,
        "instruction": "办公室温度过高，请打开补光灯提示并读取传感器确认",
        "priority": "high",
    }
    return IntentAnalysis(**(base | overrides))
