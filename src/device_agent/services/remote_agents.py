"""外部 A2A Agent 注册表与委派客户端（基于官方 a2a-sdk 客户端）。"""

import asyncio
import logging
import re
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx
from a2a.client import ClientConfig, create_client
from a2a.client.card_resolver import parse_agent_card
from a2a.helpers import get_artifact_text, get_message_text
from a2a.types import AgentCard, GetTaskRequest, Message, Part, Role, SendMessageRequest, Task, TaskState
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from device_agent.config import Settings
from device_agent.database import utcnow
from device_agent.models import RemoteAgent

logger = logging.getLogger(__name__)

TERMINAL_STATES = {
    TaskState.TASK_STATE_COMPLETED,
    TaskState.TASK_STATE_FAILED,
    TaskState.TASK_STATE_CANCELED,
    TaskState.TASK_STATE_REJECTED,
}
INTERRUPTED_STATES = {TaskState.TASK_STATE_INPUT_REQUIRED, TaskState.TASK_STATE_AUTH_REQUIRED}


class RemoteAgentError(RuntimeError):
    pass


class RemoteAgentNotFoundError(LookupError):
    pass


class RemoteAgentConflictError(ValueError):
    pass


@dataclass(slots=True)
class DelegationResult:
    ok: bool
    text: str
    state: str
    remote_task_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "text": self.text, "state": self.state, "remote_task_id": self.remote_task_id}


def card_skills(card: AgentCard) -> list[dict[str, Any]]:
    return [
        {"id": s.id, "name": s.name, "description": s.description, "tags": list(s.tags)} for s in card.skills
    ]


def task_text(task: Task) -> str:
    texts = [get_artifact_text(a) for a in task.artifacts]
    if not any(texts) and task.status.HasField("message"):
        texts = [get_message_text(task.status.message)]
    return "\n".join(t for t in texts if t).strip()


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", value.lower()).strip("_")


class RemoteAgentService:
    def __init__(
        self,
        sessions: async_sessionmaker,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._sessions = sessions
        self._settings = settings
        self._transport = transport  # 测试时可注入 ASGITransport
        self._contexts: OrderedDict[tuple[str, str], str] = OrderedDict()

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=self._settings.remote_agent_timeout_seconds, transport=self._transport
        )

    @staticmethod
    def _card_location(card_url: str) -> str:
        parts = urlsplit(card_url)
        if parts.path in ("", "/"):
            return card_url.rstrip("/") + AGENT_CARD_WELL_KNOWN_PATH
        return card_url

    async def fetch_card(self, card_url: str) -> dict[str, Any]:
        url = self._card_location(card_url)
        try:
            async with self._http() as http:
                response = await http.get(url)
                response.raise_for_status()
                raw = response.json()
            parse_agent_card(raw)  # 校验格式（兼容 v0.3 / v1.0 Agent Card）
        except Exception as exc:
            raise RemoteAgentError(f"获取 Agent Card 失败 ({url}): {exc}") from exc
        return raw

    async def add(self, card_url: str, agent_id: str | None = None) -> RemoteAgent:
        raw = await self.fetch_card(card_url)
        card = parse_agent_card(raw)
        async with self._sessions() as session, session.begin():
            existing = await session.scalar(select(RemoteAgent).where(RemoteAgent.card_url == card_url))
            if existing is not None:
                raise RemoteAgentConflictError(f"该 Agent Card 已注册为 {existing.agent_id}")
            base = agent_id or _slug(card.name) or _slug(urlsplit(card_url).netloc) or "agent"
            candidate, n = base, 1
            while await session.get(RemoteAgent, candidate) is not None:
                if agent_id:
                    raise RemoteAgentConflictError(f"agent_id 已存在: {agent_id}")
                n += 1
                candidate = f"{base}_{n}"
            agent = RemoteAgent(
                agent_id=candidate,
                card_url=card_url,
                name=card.name,
                description=card.description,
                card=raw,
                available=True,
                refreshed_at=utcnow(),
            )
            session.add(agent)
        logger.info("外部 Agent 已注册: %s (%s)", agent.agent_id, card.name)
        return agent

    async def refresh(self, agent_id: str) -> RemoteAgent:
        agent = await self.get(agent_id)
        try:
            raw, error = await self.fetch_card(agent.card_url), None
        except RemoteAgentError as exc:
            raw, error = None, str(exc)
        async with self._sessions() as session, session.begin():
            agent = await session.get(RemoteAgent, agent_id)
            if agent is None:
                raise RemoteAgentNotFoundError(agent_id)
            agent.available, agent.last_error, agent.refreshed_at = raw is not None, error, utcnow()
            if raw is not None:
                card = parse_agent_card(raw)
                agent.card, agent.name, agent.description = raw, card.name, card.description
        return agent

    async def get(self, agent_id: str) -> RemoteAgent:
        async with self._sessions() as session:
            agent = await session.get(RemoteAgent, agent_id)
        if agent is None:
            raise RemoteAgentNotFoundError(agent_id)
        return agent

    async def list_agents(self, *, usable_only: bool = False) -> list[RemoteAgent]:
        stmt = select(RemoteAgent).order_by(RemoteAgent.created_at)
        if usable_only:
            stmt = stmt.where(RemoteAgent.enabled.is_(True), RemoteAgent.available.is_(True))
        async with self._sessions() as session:
            return list((await session.scalars(stmt)).all())

    async def delete(self, agent_id: str) -> None:
        async with self._sessions() as session, session.begin():
            agent = await session.get(RemoteAgent, agent_id)
            if agent is None:
                raise RemoteAgentNotFoundError(agent_id)
            await session.delete(agent)

    async def set_enabled(self, agent_id: str, enabled: bool) -> RemoteAgent:
        async with self._sessions() as session, session.begin():
            agent = await session.get(RemoteAgent, agent_id)
            if agent is None:
                raise RemoteAgentNotFoundError(agent_id)
            agent.enabled = enabled
        return agent

    def _remember_context(self, key: tuple[str, str], remote_context_id: str) -> None:
        self._contexts[key] = remote_context_id
        self._contexts.move_to_end(key)
        while len(self._contexts) > 1000:
            self._contexts.popitem(last=False)

    async def send(self, agent_id: str, text: str, *, context_id: str | None = None) -> DelegationResult:
        """向外部 Agent 发送消息；若返回的是未完成的 Task，则轮询直到结束或超时。"""
        agent = await self.get(agent_id)
        card = parse_agent_card(agent.card)
        key = (context_id or "", agent_id)
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[Part(text=text)],
            context_id=self._contexts.get(key) if context_id else None,
        )
        request = SendMessageRequest(message=message)
        request.configuration.return_immediately = True
        settings = self._settings
        task: Task | None = None
        try:
            async with await create_client(
                card, client_config=ClientConfig(httpx_client=self._http(), streaming=False)
            ) as client:
                async for event in client.send_message(request):
                    if event.HasField("message"):
                        return DelegationResult(True, get_message_text(event.message), "message")
                    if event.HasField("task"):
                        task = event.task
                if task is None:
                    return DelegationResult(False, "外部 Agent 未返回结果", "unknown")
                if context_id and task.context_id:
                    self._remember_context(key, task.context_id)
                async with asyncio.timeout(settings.remote_task_max_wait_seconds):
                    while task.status.state not in TERMINAL_STATES | INTERRUPTED_STATES:
                        await asyncio.sleep(settings.remote_task_poll_interval_seconds)
                        task = await client.get_task(GetTaskRequest(id=task.id))
        except TimeoutError:
            return DelegationResult(False, "等待外部 Agent 结果超时", "timeout", task.id if task else None)
        except Exception as exc:
            logger.warning("委派外部 Agent %s 失败: %s", agent_id, exc)
            return DelegationResult(False, f"调用外部 Agent 失败: {exc}", "error")

        state = TaskState.Name(task.status.state).removeprefix("TASK_STATE_").lower()
        ok = task.status.state == TaskState.TASK_STATE_COMPLETED
        return DelegationResult(ok, task_text(task) or f"(外部任务状态: {state})", state, task.id)
