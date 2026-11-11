"""会话记忆：从 A2A 任务表重建同一 contextId 的历史对话。

任务（含用户消息、最终回答、工具调用记录）已由 a2a-sdk 的 DatabaseTaskStore 持久化，
这里直接读取，不再单独维护一份记忆：服务重启后上下文依然完整，也不会出现
"任务还在、记忆没了"的不一致。每一轮带入模型的内容是：
  用户原话  →  最终回答 + 本轮调用过的工具摘要 + 相关审批的当前状态
只带入最近 `conversation_window_hours` 内、最多 `conversation_max_turns` 个已完成的任务。
"""

import json
from collections import defaultdict
from datetime import UTC, timedelta

from a2a.helpers import get_artifact_text, get_data_parts, get_message_text
from a2a.server.context import ServerCallContext
from a2a.server.tasks import TaskStore
from a2a.types import ListTasksRequest, Message, Role, Task, TaskState
from a2a.utils.constants import MAX_LIST_TASKS_PAGE_SIZE
from google.genai import types

from device_agent.database import utcnow
from device_agent.services.approvals import ApprovalService, describe

RESPONSE_ARTIFACT = "response"
TOOL_CALLS_ARTIFACT = "tool_calls"
MAX_CHARS_PER_TURN = 4000


def _clip(text: str) -> str:
    return text if len(text) <= MAX_CHARS_PER_TURN else text[:MAX_CHARS_PER_TURN] + "…(已截断)"


def describe_user_message(message: Message | None) -> str:
    if message is None:
        return ""
    text = get_message_text(message)
    attachments = [
        p.media_type or p.WhichOneof("content") for p in message.parts if p.WhichOneof("content") != "text"
    ]
    if attachments:
        text += f"\n[附件: {', '.join(attachments)}]"
    return text.strip()


def tool_calls_of(task: Task) -> list[dict]:
    for artifact in task.artifacts:
        if artifact.name == TOOL_CALLS_ARTIFACT:
            data = get_data_parts(artifact.parts)
            return data[0] if data and isinstance(data[0], list) else []
    return []


def summarize_tool_calls(calls: list[dict]) -> list[str]:
    lines = []
    for call in calls:
        args = json.dumps(call.get("arguments", {}), ensure_ascii=False)
        lines.append(f"- [{call.get('kind')}] {call.get('target')} {args} → {call.get('status', '')}")
    return lines


class ConversationMemory:
    def __init__(
        self,
        task_store: TaskStore,
        approvals: ApprovalService,
        *,
        max_turns: int,
        window_hours: float,
    ) -> None:
        self._store = task_store
        self._approvals = approvals
        self._max_turns = max_turns
        self._window = timedelta(hours=window_hours)

    async def _recent_tasks(
        self, context_id: str, call_context: ServerCallContext, exclude_task_id: str | None
    ) -> list[Task]:
        request = ListTasksRequest(
            context_id=context_id,
            status=TaskState.TASK_STATE_COMPLETED,
            page_size=min(self._max_turns + 1, MAX_LIST_TASKS_PAGE_SIZE),
        )
        response = await self._store.list(request, call_context)  # 按更新时间倒序
        cutoff = utcnow() - self._window
        tasks = [
            t
            for t in response.tasks
            if t.id != exclude_task_id and t.status.timestamp.ToDatetime(tzinfo=UTC) >= cutoff
        ]
        return list(reversed(tasks[: self._max_turns]))

    async def history(
        self,
        context_id: str | None,
        *,
        call_context: ServerCallContext,
        exclude_task_id: str | None = None,
    ) -> list[types.Content]:
        if not context_id or self._max_turns <= 0:
            return []
        tasks = await self._recent_tasks(context_id, call_context, exclude_task_id)
        if not tasks:
            return []
        approvals_by_task = defaultdict(list)
        for approval in await self._approvals.list_approvals(context_id=context_id, limit=200):
            approvals_by_task[approval.task_id].append(approval)

        contents: list[types.Content] = []
        for task in tasks:
            user = next((m for m in task.history if m.role == Role.ROLE_USER), None)
            answer = next((get_artifact_text(a) for a in task.artifacts if a.name == RESPONSE_ARTIFACT), "")
            extra = summarize_tool_calls(tool_calls_of(task))
            extra += [f"- {describe(a)}" for a in reversed(approvals_by_task.get(task.id, []))]
            if extra:
                answer += "\n\n[本轮调用记录]\n" + "\n".join(extra)
            contents.append(
                types.Content(role="user", parts=[types.Part(text=_clip(describe_user_message(user)))])
            )
            contents.append(types.Content(role="model", parts=[types.Part(text=_clip(answer))]))
        return contents
