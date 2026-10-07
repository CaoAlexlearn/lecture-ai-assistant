"""Generic text-chat adapter for Alibaba Cloud Model Studio models."""

from __future__ import annotations

from typing import Any

from openai import AsyncOpenAI

from qwen_agent1 import QwenAgent1Config, load_qwen_config


class QwenChatClient:
    """Expose DashScope's OpenAI-compatible chat API as a ModelClient."""

    def __init__(self, config: QwenAgent1Config | None = None) -> None:
        self.config = config or load_qwen_config()
        self.client = AsyncOpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout=self.config.timeout_seconds,
            max_retries=0,
        )

    async def create_chat_completion(self, **kwargs: Any) -> Any:
        request = dict(kwargs)
        # ``thinking`` is a DeepSeek-specific request field in this codebase.
        request.pop("thinking", None)
        return await self.client.chat.completions.create(**request)
