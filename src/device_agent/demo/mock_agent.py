"""模拟外部 A2A Agent：一个"天气查询"Agent，用于本地测试任务委派。

它以异步任务方式工作（先返回 working 状态，延迟后完成），可以验证委派时的轮询逻辑。
运行: uv run python -m device_agent.demo.mock_agent --port 9002
Agent Card: http://localhost:9002/.well-known/agent-card.json
"""

import argparse
import asyncio
import random
import re

from a2a.helpers import new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from a2a.utils.constants import PROTOCOL_VERSION_1_0
from fastapi import FastAPI

CITIES = ["北京", "上海", "广州", "深圳", "杭州", "成都", "武汉", "西安", "南京", "匹兹堡", "东京"]


class WeatherExecutor(AgentExecutor):
    def __init__(self, delay_seconds: float) -> None:
        self._delay = delay_seconds

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        task = context.current_task or new_task_from_user_message(context.message)
        if context.current_task is None:
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        await updater.start_work(updater.new_agent_message([new_text_part("正在查询天气…")]))
        await asyncio.sleep(self._delay)

        text = context.get_user_input()
        city = next((c for c in CITIES if c in text), None)
        if city is None and (m := re.search(r"([一-龥]{2,4})(?:市|的天气|天气)", text)):
            city = m.group(1)
        city = city or "北京"
        rng = random.Random(city)
        weather = rng.choice(["晴", "多云", "小雨", "阴"])
        report = (
            f"{city}今日天气（模拟数据）：{weather}，{rng.randint(12, 28)}°C，"
            f"湿度 {rng.randint(35, 80)}%，{rng.choice(['东南风', '北风', '西风'])} {rng.randint(1, 4)} 级。"
        )
        await updater.add_artifact([new_text_part(report)], name="weather")
        await updater.complete(updater.new_agent_message([new_text_part(report)]))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        await TaskUpdater(event_queue, context.task_id or "", context.context_id or "").cancel()


def build_app(base_url: str = "http://localhost:9002", delay_seconds: float = 1.0) -> FastAPI:
    card = AgentCard(
        name="Weather Agent",
        description="天气查询 Agent：查询指定城市的实时天气与出行建议",
        version="1.0.0",
        supported_interfaces=[
            AgentInterface(
                protocol_binding="JSONRPC", url=f"{base_url}/", protocol_version=PROTOCOL_VERSION_1_0
            )
        ],
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[
            AgentSkill(
                id="weather",
                name="天气查询",
                description="查询城市天气（气温、湿度、风力）",
                tags=["weather", "天气"],
                examples=["上海今天天气怎么样？"],
            )
        ],
    )
    handler = DefaultRequestHandler(
        agent_executor=WeatherExecutor(delay_seconds), task_store=InMemoryTaskStore(), agent_card=card
    )
    app = FastAPI(title="Mock Weather A2A Agent")
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/", enable_v0_3_compat=True),
    )
    return app


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description="模拟天气 A2A Agent")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9002)
    parser.add_argument("--delay", type=float, default=1.0, help="模拟处理耗时(秒)")
    parser.add_argument(
        "--public-url",
        default=None,
        help="写入 Agent Card 的对外地址（容器内运行时用服务名，如 http://mock-agent:9002）",
    )
    args = parser.parse_args()
    public_url = args.public_url or f"http://localhost:{args.port}"
    uvicorn.run(build_app(public_url, args.delay), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
