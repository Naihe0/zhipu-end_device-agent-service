"""智能编排：用 Gemini 原生 function calling 统一完成"闲聊 / 调用设备 MCP 工具 / 委派外部 A2A Agent"。

每次请求动态构建工具集：
- 在线设备的每个 MCP 工具 → 一个函数声明（参数 schema 直接取自 MCP 的 inputSchema）
- 每个可用的外部 Agent → 一个 `agent__<id>` 函数，参数为自包含的任务描述
模型可在多轮中组合调用，直到给出最终回答或达到步数上限。
"""

import asyncio
import json
import logging
import re
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from a2a.client.card_resolver import parse_agent_card
from google.genai import types

from device_agent.database import utcnow
from device_agent.models import Device, RemoteAgent
from device_agent.services.devices import DeviceService
from device_agent.services.llm import LLMClient, text_of
from device_agent.services.remote_agents import RemoteAgentService, card_skills

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str], Awaitable[None]]
MAX_TOOL_OUTPUT_CHARS = 8000

SYSTEM_PROMPT = """你是「终端设备 A2A Agent 服务」的编排助手，负责理解用户需求并调度能力完成任务。
当前时间: {now}

你可以使用的能力：
1. 设备工具（函数名以 dev__ 开头）：直接操作已接入的终端设备（摄像头、传感器等），描述中注明了所属设备与位置。
2. 外部 Agent（函数名以 agent__ 开头）：把需要专业能力的任务委派给其他 A2A Agent，参数 message 必须是完整、自包含的任务描述。
3. 闲聊、常识问答、解释说明等不需要设备或外部能力的问题，直接回答。

规则：
- 需要设备数据或设备操作时必须调用工具，绝不编造设备结果；工具失败时如实说明原因。
- 一个请求可以组合多个工具；能并行的调用可以一次给出。
- 若没有合适的设备或 Agent，说明当前不具备该能力，并给出可行建议。
- 使用与用户相同的语言，回答简洁、突出关键信息。"""


@dataclass(slots=True)
class ToolCallRecord:
    function: str
    kind: str  # device | agent | unknown
    target: str
    arguments: dict[str, Any]
    ok: bool
    output: Any
    elapsed_ms: int = 0


@dataclass(slots=True)
class OrchestratorResult:
    text: str
    tool_calls: list[ToolCallRecord]

    def tool_calls_as_dicts(self) -> list[dict[str, Any]]:
        return [asdict(c) for c in self.tool_calls]


@dataclass(slots=True)
class _Binding:
    kind: str
    target: str
    tool: str = ""


def _ident(value: str, limit: int) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value)[:limit] or "x"


def _truncate(value: Any) -> Any:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return value if len(text) <= MAX_TOOL_OUTPUT_CHARS else text[:MAX_TOOL_OUTPUT_CHARS] + "...(已截断)"


class ConversationMemory:
    """按 contextId 保存最近的多轮对话（仅保留用户文本与最终回答，不含工具调用细节）。"""

    def __init__(self, max_turns: int, max_contexts: int = 500) -> None:
        self._max_turns = max_turns
        self._max_contexts = max_contexts
        self._store: OrderedDict[str, deque[types.Content]] = OrderedDict()

    def history(self, context_id: str | None) -> list[types.Content]:
        return list(self._store.get(context_id, ())) if context_id else []

    def append(self, context_id: str, user_text: str, answer: str) -> None:
        turns = self._store.setdefault(context_id, deque(maxlen=self._max_turns * 2))
        turns.append(types.Content(role="user", parts=[types.Part(text=user_text)]))
        turns.append(types.Content(role="model", parts=[types.Part(text=answer)]))
        self._store.move_to_end(context_id)
        while len(self._store) > self._max_contexts:
            self._store.popitem(last=False)


class Orchestrator:
    def __init__(
        self,
        llm: LLMClient,
        devices: DeviceService,
        agents: RemoteAgentService,
        *,
        max_steps: int,
    ) -> None:
        self._llm = llm
        self._devices = devices
        self._agents = agents
        self._max_steps = max_steps

    # ---------- 工具集 ----------

    @staticmethod
    def _device_declarations(devices: list[Device]) -> tuple[list[types.FunctionDeclaration], dict]:
        decls: list[types.FunctionDeclaration] = []
        bindings: dict[str, _Binding] = {}
        for device in devices:
            where = f"，位置: {device.location}" if device.location else ""
            for tool in device.tools:
                name = f"dev__{_ident(device.device_id, 28)}__{_ident(tool['name'], 28)}"
                if name in bindings:
                    continue
                bindings[name] = _Binding("device", device.device_id, tool["name"])
                decls.append(
                    types.FunctionDeclaration(
                        name=name,
                        description=(
                            f"[设备 {device.name} (id={device.device_id}, 类型={device.device_type}{where})] "
                            f"{tool.get('description') or tool['name']}"
                        ),
                        parameters_json_schema=tool.get("input_schema")
                        or {"type": "object", "properties": {}},
                    )
                )
        return decls, bindings

    @staticmethod
    def _agent_declarations(agents: list[RemoteAgent]) -> tuple[list[types.FunctionDeclaration], dict]:
        decls: list[types.FunctionDeclaration] = []
        bindings: dict[str, _Binding] = {}
        for agent in agents:
            name = f"agent__{_ident(agent.agent_id, 50)}"
            try:
                skills = card_skills(parse_agent_card(agent.card))
            except Exception:
                skills = []
            skill_text = "; ".join(f"{s['name']}: {s['description']}" for s in skills)
            bindings[name] = _Binding("agent", agent.agent_id)
            decls.append(
                types.FunctionDeclaration(
                    name=name,
                    description=f"委派任务给外部 A2A Agent「{agent.name}」: {agent.description} 技能: {skill_text}",
                    parameters_json_schema={
                        "type": "object",
                        "properties": {
                            "message": {
                                "type": "string",
                                "description": "发给该 Agent 的完整、自包含的任务描述",
                            }
                        },
                        "required": ["message"],
                    },
                )
            )
        return decls, bindings

    async def _toolset(self) -> tuple[list[types.Tool], dict[str, _Binding]]:
        device_decls, device_bindings = self._device_declarations(
            await self._devices.list_devices(online_only=True)
        )
        agent_decls, agent_bindings = self._agent_declarations(
            await self._agents.list_agents(usable_only=True)
        )
        decls = device_decls + agent_decls
        return ([types.Tool(function_declarations=decls)] if decls else []), device_bindings | agent_bindings

    # ---------- 执行 ----------

    async def _execute(
        self, call: types.FunctionCall, bindings: dict[str, _Binding], context_id: str | None
    ) -> ToolCallRecord:
        name, args = call.name or "", dict(call.args or {})
        binding = bindings.get(name)
        if binding is None:
            return ToolCallRecord(name, "unknown", "", args, False, {"error": f"未知函数: {name}"})
        if binding.kind == "device":
            result = await self._devices.call_tool(binding.target, binding.tool, args)
            output = result.output if result.ok else {"error": result.error, "output": result.output}
            return ToolCallRecord(
                name, "device", f"{binding.target}.{binding.tool}", args, result.ok, output, result.elapsed_ms
            )
        started = utcnow()
        delegation = await self._agents.send(
            binding.target, str(args.get("message", "")), context_id=context_id
        )
        elapsed = int((utcnow() - started).total_seconds() * 1000)
        return ToolCallRecord(
            name, "agent", binding.target, args, delegation.ok, delegation.to_dict(), elapsed
        )

    @staticmethod
    def _describe(call: types.FunctionCall, bindings: dict[str, _Binding]) -> str:
        binding = bindings.get(call.name or "")
        if binding is None:
            return f"调用 {call.name}"
        if binding.kind == "device":
            return f"调用设备 {binding.target} 的工具 {binding.tool}"
        return f"委派给外部 Agent {binding.target}"

    async def run(
        self,
        parts: list[types.Part],
        *,
        history: list[types.Content] | None = None,
        context_id: str | None = None,
        on_progress: ProgressCallback | None = None,
        now: datetime | None = None,
    ) -> OrchestratorResult:
        tools, bindings = await self._toolset()
        system = SYSTEM_PROMPT.format(now=(now or utcnow()).astimezone().isoformat(timespec="seconds"))
        contents = [*(history or []), types.Content(role="user", parts=parts)]
        records: list[ToolCallRecord] = []

        for _ in range(self._max_steps):
            reply = await self._llm.generate(contents, system_instruction=system, tools=tools)
            contents.append(reply)
            calls = [p.function_call for p in reply.parts or [] if p.function_call]
            if not calls:
                return OrchestratorResult(text_of(reply) or "(模型未返回内容)", records)

            if on_progress:
                await on_progress("；".join(self._describe(c, bindings) for c in calls))
            step = await asyncio.gather(*(self._execute(c, bindings, context_id) for c in calls))
            records.extend(step)
            contents.append(
                types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=call.id,
                                name=call.name,
                                response={"output": _truncate(r.output)}
                                if r.ok
                                else {"error": _truncate(r.output)},
                            )
                        )
                        for call, r in zip(calls, step, strict=True)
                    ],
                )
            )

        logger.warning("编排达到最大步数 %d，要求模型直接总结", self._max_steps)
        contents.append(
            types.Content(role="user", parts=[types.Part(text="请根据以上工具结果直接给出最终回答。")])
        )
        reply = await self._llm.generate(contents, system_instruction=system, tools=tools, text_only=True)
        return OrchestratorResult(text_of(reply) or "(模型未返回内容)", records)
