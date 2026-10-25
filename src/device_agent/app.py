"""FastAPI 应用工厂。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from device_agent.api import agents, devices, intents, system
from device_agent.config import Settings, get_settings
from device_agent.container import Container
from device_agent.logging_setup import configure_logging
from device_agent.security import BearerTokenMiddleware
from device_agent.services.connections import IngestError
from device_agent.services.devices import DeviceNotFoundError, DeviceValidationError
from device_agent.services.remote_agents import (
    RemoteAgentConflictError,
    RemoteAgentError,
    RemoteAgentNotFoundError,
)

ERROR_STATUS: dict[type[Exception], int] = {
    DeviceNotFoundError: 404,
    RemoteAgentNotFoundError: 404,
    RemoteAgentConflictError: 409,
    DeviceValidationError: 422,
    IngestError: 422,
    RemoteAgentError: 502,
}


def create_app(settings: Settings | None = None, *, container: Container | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    container = container or Container.build(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        await container.start()
        try:
            yield
        finally:
            await container.stop()

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        description="终端设备接入 (MCP) · 多模态意图识别 (Gemini) · A2A 协议智能路由",
        lifespan=lifespan,
    )
    app.state.container = container

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    if settings.api_token is not None:
        app.add_middleware(BearerTokenMiddleware, token=settings.api_token.get_secret_value())

    for exc_type, code in ERROR_STATUS.items():

        async def handler(_: Request, exc: Exception, code: int = code) -> JSONResponse:
            name = exc.args[0] if isinstance(exc, LookupError) and exc.args else str(exc)
            detail = f"未找到: {name}" if code == 404 else str(exc)
            return JSONResponse({"detail": detail}, status_code=code)

        app.add_exception_handler(exc_type, handler)

    for module in (system, devices, agents, intents):
        app.include_router(module.router)

    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(container.card, card_modifier=container.dynamic_card),
        jsonrpc_routes=create_jsonrpc_routes(
            container.handler, rpc_url=settings.a2a_rpc_path, enable_v0_3_compat=True
        ),
    )
    return app
