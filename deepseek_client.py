"""Minimal asynchronous DeepSeek adapter for future agent runtimes.

DeepSeek exposes an OpenAI-compatible API, so this module keeps the official
``openai`` SDK as the transport and only owns project configuration, request
defaults, and vision-message construction. It does not implement an agent loop.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, TypeAlias

from dotenv import load_dotenv
from openai import AsyncOpenAI

from workspace_tools import ImageReadResult

DEFAULT_DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"
DEFAULT_DEEPSEEK_PRO_MODEL = "deepseek-flash"
DEFAULT_DEEPSEEK_VISION_MODEL = "deepseek-flash"

Message: TypeAlias = Mapping[str, Any]
ToolDefinition: TypeAlias = Mapping[str, Any]
ToolChoice: TypeAlias = str | Mapping[str, Any]
ResponseFormat: TypeAlias = Mapping[str, Any]
ReasoningEffort: TypeAlias = Literal["low", "medium", "high"]
ThinkingMode: TypeAlias = Literal["enabled", "disabled"]
ImageDetail: TypeAlias = Literal["auto", "low", "high"]


class DeepSeekConfigurationError(ValueError):
    """DeepSeek environment or request configuration is invalid."""


class DeepSeekResponseError(RuntimeError):
    """A DeepSeek response does not contain usable assistant text."""


@dataclass(frozen=True, slots=True)
class DeepSeekConfig:
    """Transport defaults shared by future DeepSeek-backed agents."""

    api_key: str = field(repr=False)
    base_url: str = DEFAULT_DEEPSEEK_BASE_URL
    model: str = DEFAULT_DEEPSEEK_MODEL
    pro_model: str = DEFAULT_DEEPSEEK_PRO_MODEL
    vision_model: str = DEFAULT_DEEPSEEK_VISION_MODEL
    timeout_seconds: float = 90.0
    max_retries: int = 2


def resolve_logical_model(config: DeepSeekConfig, logical_model: str) -> str:
    """Resolve a registry-owned logical model role to a provider model name."""

    models = {"flash": config.model, "pro": config.pro_model}
    try:
        return models[logical_model]
    except (KeyError, TypeError) as exc:
        raise DeepSeekConfigurationError(
            f"Unsupported logical model role: {logical_model!r}"
        ) from exc


def _nonnegative_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise DeepSeekConfigurationError(f"{name} must be an integer.") from exc
    if value < 0:
        raise DeepSeekConfigurationError(f"{name} cannot be negative.")
    return value


def load_deepseek_config() -> DeepSeekConfig:
    """Load DeepSeek settings without coupling them to Qwen settings."""

    load_dotenv()
    api_key = os.getenv("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise DeepSeekConfigurationError("DEEPSEEK_API_KEY is missing.")

    base_url = os.getenv("DEEPSEEK_BASE_URL", DEFAULT_DEEPSEEK_BASE_URL).strip()
    model = os.getenv("DEEPSEEK_MODEL", DEFAULT_DEEPSEEK_MODEL).strip()
    pro_model = os.getenv("DEEPSEEK_MODEL_PRO", DEFAULT_DEEPSEEK_PRO_MODEL).strip()
    vision_model = os.getenv(
        "DEEPSEEK_VISION_MODEL", DEFAULT_DEEPSEEK_VISION_MODEL
    ).strip()
    if not base_url.startswith(("https://", "http://")):
        raise DeepSeekConfigurationError(
            "DEEPSEEK_BASE_URL must start with https:// or http://."
        )
    if not model or not pro_model or not vision_model:
        raise DeepSeekConfigurationError(
            "DEEPSEEK_MODEL, DEEPSEEK_MODEL_PRO, and DEEPSEEK_VISION_MODEL "
            "cannot be empty."
        )

    try:
        timeout_seconds = float(os.getenv("DEEPSEEK_TIMEOUT_SECONDS", "90"))
    except ValueError as exc:
        raise DeepSeekConfigurationError(
            "DEEPSEEK_TIMEOUT_SECONDS must be a number."
        ) from exc
    if timeout_seconds <= 0:
        raise DeepSeekConfigurationError(
            "DEEPSEEK_TIMEOUT_SECONDS must be positive."
        )

    return DeepSeekConfig(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        model=model,
        pro_model=pro_model,
        vision_model=vision_model,
        timeout_seconds=timeout_seconds,
        max_retries=_nonnegative_int("DEEPSEEK_MAX_RETRIES", 2),
    )


def image_data_url(image: ImageReadResult) -> str:
    """Encode a permission-checked image result for Chat Completions."""

    encoded = base64.b64encode(image.data).decode("ascii")
    return f"data:{image.mime_type};base64,{encoded}"


def build_vision_user_message(
    *, prompt: str, image: ImageReadResult, detail: ImageDetail = "auto"
) -> dict[str, Any]:
    """Build the OpenAI-compatible image message documented by DeepSeek."""

    if not prompt.strip():
        raise DeepSeekConfigurationError("Vision prompt cannot be empty.")
    if detail not in {"auto", "low", "high"}:
        raise DeepSeekConfigurationError(
            "Image detail must be one of: auto, low, high."
        )
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": image_data_url(image), "detail": detail},
            },
        ],
    }


class DeepSeekClient:
    """Thin async wrapper over DeepSeek's OpenAI-compatible endpoint."""

    def __init__(self, config: DeepSeekConfig, *, client: Any | None = None) -> None:
        self.config = config
        self.client = client or AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=config.max_retries,
        )

    async def create_chat_completion(
        self,
        *,
        messages: Sequence[Message],
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        tools: Sequence[ToolDefinition] | None = None,
        tool_choice: ToolChoice | None = None,
        response_format: ResponseFormat | None = None,
        thinking: ThinkingMode | None = None,
        reasoning_effort: ReasoningEffort | None = None,
    ) -> Any:
        """Create one Chat Completion while preserving tool-call response data."""

        if not messages:
            raise DeepSeekConfigurationError("messages cannot be empty.")
        selected_model = (model or self.config.model).strip()
        if not selected_model:
            raise DeepSeekConfigurationError("model cannot be empty.")
        if max_tokens is not None and max_tokens <= 0:
            raise DeepSeekConfigurationError("max_tokens must be positive.")

        request: dict[str, Any] = {
            "model": selected_model,
            "messages": list(messages),
        }
        if max_tokens is not None:
            request["max_tokens"] = max_tokens
        if temperature is not None:
            request["temperature"] = temperature
        if tools is not None:
            request["tools"] = list(tools)
        if tool_choice is not None:
            request["tool_choice"] = tool_choice
        if response_format is not None:
            request["response_format"] = dict(response_format)
        if reasoning_effort is not None:
            request["reasoning_effort"] = reasoning_effort
        if thinking is not None:
            request["extra_body"] = {"thinking": {"type": thinking}}
        return await self.client.chat.completions.create(**request)

    async def create_vision_completion(
        self,
        *,
        prompt: str,
        image: ImageReadResult,
        system_prompt: str | None = None,
        detail: ImageDetail = "auto",
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        thinking: ThinkingMode | None = None,
    ) -> Any:
        """Create a vision request from bytes loaded by ``WorkspaceTools``."""

        messages: list[Message] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append(
            build_vision_user_message(prompt=prompt, image=image, detail=detail)
        )
        return await self.create_chat_completion(
            messages=messages,
            model=model or self.config.vision_model,
            max_tokens=max_tokens,
            temperature=temperature,
            thinking=thinking,
        )

    @staticmethod
    def response_text(response: Any) -> str:
        """Return assistant text; callers retain the raw response for tool calls."""

        choices = getattr(response, "choices", None)
        if not choices:
            raise DeepSeekResponseError("DeepSeek response has no choices.")
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        if not isinstance(content, str) or not content.strip():
            raise DeepSeekResponseError(
                "DeepSeek response has no assistant text; inspect message.tool_calls."
            )
        return content.strip()
