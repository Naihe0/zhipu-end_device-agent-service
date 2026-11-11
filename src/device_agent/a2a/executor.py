"""A2A AgentExecutor：把 A2A 任务交给编排器执行，并通过 TaskUpdater 发布状态与结果。

任务生命周期、持久化（DatabaseTaskStore）、流式推送（SSE）、取消与推送通知均由 a2a-sdk
的 DefaultRequestHandler 负责，这里只实现业务逻辑。

任务来源（origin）取自服务端的 ServerCallContext.state，而不是消息里的 metadata：
外部 JSON-RPC 请求一律是 client；只有进程内的 LocalA2AClient（意图识别）能标记为 device。
"""

import json
import logging
import uuid
from typing import Any

from a2a.helpers import new_data_part, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import RequestHandler
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, Part, Role, SendMessageRequest, Task, TaskState
from google.genai import types
from google.protobuf.json_format import MessageToDict

from device_agent.services.llm import LLMError
from device_agent.services.memory import RESPONSE_ARTIFACT, TOOL_CALLS_ARTIFACT, ConversationMemory
from device_agent.services.orchestrator import Orchestrator, RunContext
from device_agent.services.policy import Origin

logger = logging.getLogger(__name__)

INLINE_MIME_PREFIXES = ("image/", "audio/", "video/", "application/pdf", "text/")
ORIGIN_STATE_KEY = "origin"


def to_genai_parts(message: Message) -> list[types.Part]:
    parts: list[types.Part] = []
    for part in message.parts:
        content = part.WhichOneof("content")
        if content == "text":
            parts.append(types.Part(text=part.text))
        elif content == "raw":
            mime = part.media_type or "application/octet-stream"
            if mime.startswith(INLINE_MIME_PREFIXES):
                parts.append(types.Part.from_bytes(data=part.raw, mime_type=mime))
            else:
                parts.append(
                    types.Part(
                        text=f"[附件 {part.filename or ''} ({mime})，{len(part.raw)} 字节，无法直接解析]"
                    )
                )
        elif content == "data":
            data = json.dumps(MessageToDict(part.data), ensure_ascii=False)
            parts.append(types.Part(text=f"结构化数据: {data}"))
        elif content == "url":
            parts.append(types.Part(text=f"[附件链接 {part.filename or ''}: {part.url}]"))
    return parts or [types.Part(text="(空消息)")]


def _json_safe(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


class DeviceAgentExecutor(AgentExecutor):
    def __init__(self, orchestrator: Orchestrator, memory: ConversationMemory) -> None:
        self._orchestrator = orchestrator
        self._memory = memory

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        message = context.message
        assert message is not None
        task = context.current_task
        if task is None:
            task = new_task_from_user_message(message)
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)

        def say(text: str) -> Message:
            return updater.new_agent_message([new_text_part(text)])

        await updater.start_work(say("正在分析请求…"))

        async def progress(text: str) -> None:
            await updater.update_status(TaskState.TASK_STATE_WORKING, say(text))

        origin = Origin(context.call_context.state.get(ORIGIN_STATE_KEY, Origin.CLIENT))
        try:
            history = await self._memory.history(
                task.context_id, call_context=context.call_context, exclude_task_id=task.id
            )
            result = await self._orchestrator.run(
                to_genai_parts(message),
                history=history,
                run=RunContext(origin=origin, context_id=task.context_id, task_id=task.id),
                on_progress=progress,
            )
        except LLMError as exc:
            logger.warning("任务 %s 失败: %s", task.id, exc)
            await updater.failed(say(str(exc)))
            return

        await updater.add_artifact([new_text_part(result.text)], name=RESPONSE_ARTIFACT)
        if result.tool_calls:
            await updater.add_artifact(
                [new_data_part(_json_safe(result.tool_calls_as_dicts()))], name=TOOL_CALLS_ARTIFACT
            )
        await updater.complete(say(result.text))

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id or "", context.context_id or "")
        await updater.cancel(updater.new_agent_message([new_text_part("任务已取消")]))


class LocalA2AClient:
    """进程内提交 A2A 任务（与外部 JSON-RPC 请求走完全相同的处理链路，任务可通过 GetTask 查询）。"""

    def __init__(self, handler: RequestHandler) -> None:
        self._handler = handler

    async def send(
        self,
        text: str,
        *,
        context_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        wait: bool = True,
        origin: Origin = Origin.CLIENT,
    ) -> Task | Message:
        message = Message(
            message_id=str(uuid.uuid4()),
            role=Role.ROLE_USER,
            parts=[Part(text=text)],
            context_id=context_id or "",
        )
        if metadata:
            message.metadata.update(metadata)
        request = SendMessageRequest(message=message)
        request.configuration.return_immediately = not wait
        context = ServerCallContext(state={ORIGIN_STATE_KEY: origin.value})
        return await self._handler.on_message_send(request, context)
