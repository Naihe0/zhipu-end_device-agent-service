"""LLM 抽象与 Gemini 实现。

编排器与意图识别只依赖 `LLMClient` 协议，测试时可以替换为脚本化的假实现。
对话内容直接使用 google-genai 的 `types.Content`，以便原样保留模型返回的
function call 与 thought signature（Gemini 3 多轮工具调用要求回传）。
"""

import logging
from typing import Protocol, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from device_agent.config import Settings

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    pass


class LLMUnavailableError(LLMError):
    pass


class LLMClient(Protocol):
    async def generate(
        self,
        contents: list[types.Content],
        *,
        system_instruction: str | None = None,
        tools: list[types.Tool] | None = None,
        text_only: bool = False,
    ) -> types.Content:
        """返回模型的一条回复（可能包含文本和/或 function call）。text_only=True 时禁止再调用工具。"""
        ...

    async def generate_json(
        self,
        contents: list[types.Content],
        schema: type[T],
        *,
        system_instruction: str | None = None,
    ) -> T:
        """结构化输出：按 Pydantic schema 返回解析后的对象。"""
        ...

    async def aclose(self) -> None: ...


class GeminiLLM:
    def __init__(self, settings: Settings) -> None:
        assert settings.gemini_api_key is not None
        self._model = settings.gemini_model
        self._temperature = settings.llm_temperature
        self._thinking = (
            types.ThinkingConfig(thinking_level=settings.gemini_thinking_level)
            if settings.gemini_thinking_level
            else None
        )
        self._client = genai.Client(
            api_key=settings.gemini_api_key.get_secret_value(),
            http_options=types.HttpOptions(timeout=int(settings.llm_timeout_seconds * 1000)),
        )

    @property
    def model(self) -> str:
        return self._model

    def _config(self, **kwargs) -> types.GenerateContentConfig:
        return types.GenerateContentConfig(
            temperature=self._temperature, thinking_config=self._thinking, **kwargs
        )

    async def _call(self, contents: list[types.Content], config: types.GenerateContentConfig):
        try:
            return await self._client.aio.models.generate_content(
                model=self._model, contents=contents, config=config
            )
        except Exception as exc:
            raise LLMError(f"Gemini 调用失败: {exc}") from exc

    async def generate(
        self, contents, *, system_instruction=None, tools=None, text_only=False
    ) -> types.Content:
        config = self._config(
            system_instruction=system_instruction,
            tools=tools or None,
            tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="NONE"))
            if tools and text_only
            else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        response = await self._call(contents, config)
        if not response.candidates or response.candidates[0].content is None:
            raise LLMError(f"Gemini 返回空结果: {response.prompt_feedback}")
        content = response.candidates[0].content
        content.parts = content.parts or []
        return content

    async def generate_json(self, contents, schema, *, system_instruction=None):
        config = self._config(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=schema,
        )
        response = await self._call(contents, config)
        if isinstance(response.parsed, schema):
            return response.parsed
        if not response.text:
            raise LLMError(f"Gemini 未返回结构化结果: {response.prompt_feedback}")
        return schema.model_validate_json(response.text)

    async def aclose(self) -> None:
        await self._client.aio.aclose()


class UnconfiguredLLM:
    """未配置 API Key 时的占位实现：服务其余功能照常可用，调用 LLM 时给出明确错误。"""

    _MESSAGE = "未配置 GEMINI_API_KEY，LLM 功能不可用。请在 .env 中设置后重启服务。"

    async def generate(
        self, contents, *, system_instruction=None, tools=None, text_only=False
    ) -> types.Content:
        raise LLMUnavailableError(self._MESSAGE)

    async def generate_json(self, contents, schema, *, system_instruction=None):
        raise LLMUnavailableError(self._MESSAGE)

    async def aclose(self) -> None:
        return None


def create_llm(settings: Settings) -> LLMClient:
    if settings.gemini_api_key is None or not settings.gemini_api_key.get_secret_value():
        logger.warning("GEMINI_API_KEY 未设置，LLM 功能已禁用")
        return UnconfiguredLLM()
    logger.info("LLM: Gemini model=%s thinking=%s", settings.gemini_model, settings.gemini_thinking_level)
    return GeminiLLM(settings)


def text_of(content: types.Content) -> str:
    """提取模型回复中的可见文本（忽略思考过程与 function call）。"""
    return "".join(p.text for p in content.parts or [] if p.text and not p.thought).strip()
