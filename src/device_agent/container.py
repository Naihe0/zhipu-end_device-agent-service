"""依赖容器：集中创建与管理所有服务实例及后台任务，替代原来散落各处的全局单例。"""

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import (
    BasePushNotificationSender,
    DatabasePushNotificationConfigStore,
    DatabaseTaskStore,
)
from a2a.types import AgentCard
from fastapi.requests import HTTPConnection
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from device_agent.a2a.card import base_card, build_card
from device_agent.a2a.executor import DeviceAgentExecutor, LocalA2AClient
from device_agent.config import Settings
from device_agent.database import create_engine, create_session_factory, init_db, utcnow
from device_agent.services.approvals import ApprovalService
from device_agent.services.connections import DeviceConnectionHub
from device_agent.services.devices import DeviceService
from device_agent.services.event_store import EventStore, create_event_store
from device_agent.services.incidents import IncidentService
from device_agent.services.intent_monitor import IntentMonitor
from device_agent.services.llm import LLMClient, create_llm
from device_agent.services.mcp_gateway import ClientFactory, MCPGateway
from device_agent.services.memory import ConversationMemory
from device_agent.services.orchestrator import Orchestrator
from device_agent.services.policy import Origin
from device_agent.services.remote_agents import RemoteAgentService

logger = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    engine: AsyncEngine
    sessions: async_sessionmaker
    http: httpx.AsyncClient
    llm: LLMClient
    mcp: MCPGateway
    store: EventStore
    devices: DeviceService
    agents: RemoteAgentService
    hub: DeviceConnectionHub
    incidents: IncidentService
    approvals: ApprovalService
    orchestrator: Orchestrator
    task_store: DatabaseTaskStore
    push_store: DatabasePushNotificationConfigStore
    handler: DefaultRequestHandler
    local_client: LocalA2AClient
    monitor: IntentMonitor
    card: AgentCard
    _background: list[asyncio.Task] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        settings: Settings,
        *,
        llm: LLMClient | None = None,
        mcp_client_factory: ClientFactory | None = None,
        agent_transport: httpx.AsyncBaseTransport | None = None,
        event_store: EventStore | None = None,
    ) -> "Container":
        engine = create_engine(settings.database_url)
        sessions = create_session_factory(engine)
        http = httpx.AsyncClient(timeout=10.0)
        llm = llm or create_llm(settings)
        mcp = MCPGateway(settings.mcp_timeout_seconds, mcp_client_factory)
        store = event_store or create_event_store(settings)
        devices = DeviceService(sessions, mcp, settings.device_offline_after_seconds)
        agents = RemoteAgentService(sessions, settings, transport=agent_transport)
        hub = DeviceConnectionHub(devices, store, settings.max_upload_bytes)
        incidents = IncidentService(sessions, settings.incident_quiet_minutes)
        approvals = ApprovalService(sessions, devices, settings.approval_ttl_minutes, notify=hub.send)
        orchestrator = Orchestrator(
            llm, devices, agents, approvals, max_steps=settings.orchestrator_max_steps
        )

        card = base_card(settings)
        task_store = DatabaseTaskStore(engine)
        push_store = DatabasePushNotificationConfigStore(engine)
        memory = ConversationMemory(
            task_store,
            approvals,
            max_turns=settings.conversation_max_turns,
            window_hours=settings.conversation_window_hours,
        )
        handler = DefaultRequestHandler(
            agent_executor=DeviceAgentExecutor(orchestrator, memory),
            task_store=task_store,
            agent_card=card,
            push_config_store=push_store,
            push_sender=BasePushNotificationSender(httpx_client=http, config_store=push_store),
        )
        local_client = LocalA2AClient(handler)
        monitor = IntentMonitor(
            llm,
            devices,
            store,
            sessions,
            settings,
            incidents=incidents,
            approvals=approvals,
            submit=lambda text, meta, context_id: local_client.send(
                text, metadata=meta, context_id=context_id, origin=Origin.DEVICE
            ),
            notify=hub.send,
        )
        return cls(
            settings=settings, engine=engine, sessions=sessions, http=http, llm=llm, mcp=mcp, store=store,
            devices=devices, agents=agents, hub=hub, incidents=incidents, approvals=approvals,
            orchestrator=orchestrator, task_store=task_store,
            push_store=push_store, handler=handler, local_client=local_client, monitor=monitor, card=card,
        )  # fmt: skip

    async def dynamic_card(self, _: AgentCard) -> AgentCard:
        return build_card(
            self.settings,
            await self.devices.list_devices(online_only=True),
            await self.agents.list_agents(usable_only=True),
        )

    async def maintenance_once(self) -> dict[str, Any]:
        cutoff = utcnow() - timedelta(hours=self.settings.stream_retention_hours)
        probed = await self.devices.sweep(self.hub.connected_ids())
        trimmed = await self.store.trim_older_than(cutoff)
        purged = self.store.blobs.purge_older_than(cutoff)
        closed = await self.incidents.close_stale()
        expired = await self.approvals.expire_stale()
        return {
            "probed": probed,
            "events_trimmed": trimmed,
            "blobs_purged": purged,
            "incidents_closed": closed,
            "approvals_expired": expired,
        }

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.maintenance_interval_seconds)
            try:
                await self.maintenance_once()
            except Exception:
                logger.exception("维护任务异常")

    async def start(self, *, background: bool = True) -> None:
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        await init_db(self.engine)
        await self.task_store.initialize()
        await self.push_store.initialize()
        await self.store.start()
        if background:
            self._background.append(asyncio.create_task(self._maintenance_loop(), name="maintenance"))
            if self.settings.intent_monitor_enabled:
                self._background.append(
                    asyncio.create_task(self.monitor.run_forever(), name="intent-monitor")
                )
        logger.info("服务已启动: A2A 端点 %s", self.settings.a2a_rpc_url)

    async def stop(self) -> None:
        for task in self._background:
            task.cancel()
        for task in self._background:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._background.clear()
        await self.monitor.stop()
        await self.hub.close_all()
        await self.store.close()
        await self.llm.aclose()
        await self.http.aclose()
        await self.engine.dispose()


def get_container(conn: HTTPConnection) -> Container:
    return conn.app.state.container
