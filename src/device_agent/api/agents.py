from typing import Annotated

from a2a.client.card_resolver import parse_agent_card
from fastapi import APIRouter, Depends, status

from device_agent.container import Container, get_container
from device_agent.models import RemoteAgent
from device_agent.schemas import RemoteAgentCreate, RemoteAgentOut
from device_agent.services.remote_agents import card_skills

router = APIRouter(prefix="/api/agents", tags=["外部 A2A Agent"])
Deps = Annotated[Container, Depends(get_container)]


def _out(agent: RemoteAgent) -> RemoteAgentOut:
    out = RemoteAgentOut.model_validate(agent)
    try:
        out.skills = card_skills(parse_agent_card(agent.card))
    except Exception:
        out.skills = []
    return out


@router.post("", status_code=status.HTTP_201_CREATED, summary="通过 Agent Card URL 注册外部 Agent")
async def add_agent(body: RemoteAgentCreate, c: Deps) -> RemoteAgentOut:
    return _out(await c.agents.add(str(body.card_url), body.agent_id))


@router.get("", summary="外部 Agent 列表")
async def list_agents(c: Deps) -> list[RemoteAgentOut]:
    return [_out(a) for a in await c.agents.list_agents()]


@router.get("/{agent_id}", summary="外部 Agent 详情")
async def get_agent(agent_id: str, c: Deps) -> RemoteAgentOut:
    return _out(await c.agents.get(agent_id))


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT, summary="移除外部 Agent")
async def delete_agent(agent_id: str, c: Deps) -> None:
    await c.agents.delete(agent_id)


@router.post("/{agent_id}/refresh", summary="重新获取 Agent Card 并更新可用状态")
async def refresh_agent(agent_id: str, c: Deps) -> RemoteAgentOut:
    return _out(await c.agents.refresh(agent_id))


@router.post("/{agent_id}/enable", summary="启用")
async def enable_agent(agent_id: str, c: Deps) -> RemoteAgentOut:
    return _out(await c.agents.set_enabled(agent_id, True))


@router.post("/{agent_id}/disable", summary="禁用（编排器不再委派给它）")
async def disable_agent(agent_id: str, c: Deps) -> RemoteAgentOut:
    return _out(await c.agents.set_enabled(agent_id, False))
