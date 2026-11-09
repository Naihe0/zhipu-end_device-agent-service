"""多模态意图识别：定期读取各设备事件流中的新数据，交给 Gemini 判断是否需要采取行动。

- 文本、传感器 JSON 直接以文本给模型；图片/音频/视频以内联二进制交给模型原生理解（无需单独 ASR）。
- 使用结构化输出（Pydantic schema），不再依赖正则从自由文本中抠 JSON。
- 判定需要行动时，以 A2A 任务的形式提交（与外部请求同一链路，可通过 GetTask 查询），
  完成后把结果写回识别日志，并通过 WebSocket 推送给设备。
"""

import asyncio
import json
import logging
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from a2a.types import Message, Task, TaskState
from google.genai import types
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from device_agent.config import Settings
from device_agent.models import Device, IntentLog
from device_agent.services.devices import DeviceNotFoundError, DeviceService
from device_agent.services.event_store import MEDIA_KINDS, DeviceEvent, EventKind, EventStore
from device_agent.services.llm import LLMClient, LLMError
from device_agent.services.remote_agents import task_text

logger = logging.getLogger(__name__)

CONSUMER_GROUP = "intent-monitor"
INLINE_MIME_PREFIXES = ("image/", "audio/", "video/", "application/pdf")

SYSTEM_PROMPT = """你是多模态意图识别代理，负责分析终端设备上报的数据（文本、语音、图片、视频、传感器读数），
判断其中是否存在需要处理的用户意图或异常情况。

判断原则：
- 明确的用户请求或指令（包括语音中的指令）→ 需要行动
- 传感器读数异常、超出合理范围或出现危险信号 → 需要行动
- 画面/声音中出现需要关注的事件 → 视严重程度决定
- 例行、正常的数据 → 不需要行动，不要过度触发

若需要行动，instruction 必须是一条完整、自包含的任务指令（说明设备、现象和期望的处理），
它会被交给能调用设备工具和外部 Agent 的编排助手执行。"""

Submit = Callable[[str, dict[str, Any]], Awaitable[Task | Message]]
Notify = Callable[[str, dict[str, Any]], Awaitable[bool]]


class IntentAnalysis(BaseModel):
    intent_detected: bool = Field(description="是否检测到需要关注的意图或事件")
    intent_type: str = Field(description="意图类型的简短描述，如 user_command / anomaly / routine")
    confidence: float = Field(description="0 到 1 之间的置信度")
    reasoning: str = Field(description="判断依据")
    action_required: bool = Field(description="是否需要执行任务")
    instruction: str = Field(description="需要执行时交给编排助手的完整任务指令；否则为空字符串")
    priority: Literal["low", "medium", "high", "urgent"]


def summarize(events: list[DeviceEvent]) -> str:
    counts = Counter(e.kind.value for e in events)
    lines = [", ".join(f"{k}×{n}" for k, n in counts.items())]
    for e in events[-5:]:
        if e.kind == EventKind.TEXT:
            lines.append(f"文本: {e.text[:100] if e.text else ''}")
        elif e.kind == EventKind.DATA:
            lines.append(f"数据: {json.dumps(e.data, ensure_ascii=False)[:100]}")
        else:
            lines.append(f"{e.kind.value}: {e.filename or ''} {e.size}B")
    return "\n".join(lines)


class IntentMonitor:
    def __init__(
        self,
        llm: LLMClient,
        devices: DeviceService,
        store: EventStore,
        sessions: async_sessionmaker,
        settings: Settings,
        *,
        submit: Submit,
        notify: Notify,
    ) -> None:
        self._llm = llm
        self._devices = devices
        self._store = store
        self._sessions = sessions
        self._settings = settings
        self._submit = submit
        self._notify = notify
        self._dispatches: set[asyncio.Task] = set()
        self._lock = asyncio.Lock()
        self.scans = 0

    # ---------- 循环 ----------

    async def run_forever(self) -> None:
        interval = self._settings.intent_scan_interval_seconds
        logger.info("意图识别监控已启动，扫描间隔 %.0fs", interval)
        while True:
            try:
                await self.scan_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("意图识别扫描异常")
            await asyncio.sleep(interval)

    async def scan_once(self) -> list[IntentLog]:
        async with self._lock:
            self.scans += 1
            logs = []
            for device_id in await self._store.device_ids():
                events = await self._store.read_new(
                    device_id, CONSUMER_GROUP, self._settings.intent_max_events_per_scan
                )
                if not events:
                    continue
                try:
                    device = await self._devices.get(device_id)
                except DeviceNotFoundError:
                    continue
                logs.append(await self.analyze(device, events))
            return logs

    async def wait_for_dispatches(self) -> None:
        if self._dispatches:
            await asyncio.gather(*self._dispatches, return_exceptions=True)

    async def stop(self) -> None:
        for task in list(self._dispatches):
            task.cancel()
        await self.wait_for_dispatches()

    # ---------- 分析 ----------

    def _build_parts(self, device: Device, events: list[DeviceEvent]) -> list[types.Part]:
        header = (
            f"设备信息：\n- ID: {device.device_id}\n- 名称: {device.name}\n- 类型: {device.device_type}\n"
            f"- 位置: {device.location or '未知'}\n- 描述: {device.description or '无'}\n"
            f"- 设备特性/关注点: {device.system_prompt or '通用终端设备'}\n"
            f"- 可用工具: {', '.join(t['name'] for t in device.tools) or '无'}\n\n"
            f"以下是该设备最近上报的 {len(events)} 条数据（按时间顺序）："
        )
        parts = [types.Part(text=header)]
        budget = self._settings.intent_max_media_bytes
        for e in events:
            ts = e.created_at.astimezone().strftime("%Y-%m-%d %H:%M:%S")
            if e.kind == EventKind.TEXT:
                parts.append(types.Part(text=f"[{ts}] 文本: {e.text}"))
            elif e.kind == EventKind.DATA:
                parts.append(
                    types.Part(text=f"[{ts}] 结构化数据: {json.dumps(e.data, ensure_ascii=False)[:4000]}")
                )
            elif e.kind in MEDIA_KINDS:
                mime = e.media_type or ""
                payload = (
                    self._store.load_payload(e)
                    if mime.startswith(INLINE_MIME_PREFIXES) and e.size <= budget
                    else None
                )
                if payload:
                    budget -= len(payload)
                    parts.append(types.Part(text=f"[{ts}] {e.kind.value} ({e.filename or mime}):"))
                    parts.append(types.Part.from_bytes(data=payload, mime_type=mime))
                else:
                    parts.append(
                        types.Part(
                            text=f"[{ts}] {e.kind.value} {e.filename or ''} ({mime}, {e.size} 字节, 未内联)"
                        )
                    )
        return parts

    async def analyze(self, device: Device, events: list[DeviceEvent]) -> IntentLog:
        log = IntentLog(
            device_id=device.device_id,
            event_ids=[e.id for e in events],
            data_summary=summarize(events),
            intent_detected=False,
            intent_type="",
            confidence=0.0,
            reasoning="",
            action_required=False,
            instruction="",
            priority="low",
        )
        try:
            analysis = await self._llm.generate_json(
                [types.Content(role="user", parts=self._build_parts(device, events))],
                IntentAnalysis,
                system_instruction=SYSTEM_PROMPT,
            )
        except LLMError as exc:
            log.error = str(exc)
            analysis = None
        if analysis is not None:
            log.intent_detected = analysis.intent_detected
            log.intent_type = analysis.intent_type
            log.confidence = max(0.0, min(1.0, analysis.confidence))
            log.reasoning = analysis.reasoning
            log.action_required = analysis.action_required
            log.instruction = analysis.instruction
            log.priority = analysis.priority

        async with self._sessions() as session, session.begin():
            session.add(log)

        should_act = (
            log.intent_detected
            and log.action_required
            and log.instruction.strip()
            and log.confidence >= self._settings.intent_min_confidence
        )
        logger.info(
            "意图识别 %s: detected=%s type=%s conf=%.2f act=%s",
            device.device_id,
            log.intent_detected,
            log.intent_type,
            log.confidence,
            bool(should_act),
        )
        if should_act:
            task = asyncio.create_task(self._dispatch(log, device))
            self._dispatches.add(task)
            task.add_done_callback(self._dispatches.discard)
        return log

    async def _dispatch(self, log: IntentLog, device: Device) -> None:
        log_id = log.id
        text = (
            f"[自动任务] 由终端设备「{device.name}」(id={device.device_id}，位置: {device.location or '未知'}) "
            f"上报的数据触发。\n识别意图: {log.intent_type}（置信度 {log.confidence:.2f}，优先级 {log.priority}）\n"
            f"任务: {log.instruction}"
        )
        task_id, state, result, error = None, None, "", None
        try:
            outcome = await self._submit(
                text, {"source": "intent_monitor", "device_id": device.device_id, "intent_log_id": log_id}
            )
            if isinstance(outcome, Task):
                task_id = outcome.id
                state = TaskState.Name(outcome.status.state).removeprefix("TASK_STATE_").lower()
                result = task_text(outcome)
            else:
                state, result = "message", " ".join(p.text for p in outcome.parts if p.text)
        except Exception as exc:
            logger.exception("自动任务提交失败")
            error = str(exc)

        async with self._sessions() as session, session.begin():
            log = await session.get(IntentLog, log_id)
            if log is not None:
                log.task_id, log.task_state, log.result_text, log.error = task_id, state, result, error
        await self._notify(
            device.device_id,
            {
                "type": "task_result",
                "intent_log_id": log_id,
                "task_id": task_id,
                "state": state,
                "text": result or error,
            },
        )

    async def stats(self) -> dict[str, Any]:
        async with self._sessions() as session:
            total = await session.scalar(select(func.count()).select_from(IntentLog))
            detected = await session.scalar(
                select(func.count()).select_from(IntentLog).where(IntentLog.intent_detected.is_(True))
            )
            tasks = await session.scalar(
                select(func.count()).select_from(IntentLog).where(IntentLog.task_id.is_not(None))
            )
        return {
            "enabled": self._settings.intent_monitor_enabled,
            "scan_interval_seconds": self._settings.intent_scan_interval_seconds,
            "scans_since_start": self.scans,
            "total_analyses": total or 0,
            "intents_detected": detected or 0,
            "tasks_created": tasks or 0,
            "pending_dispatches": len(self._dispatches),
        }
