"""Real Qwen3.7-Flash worker for source-only Agent 1 jobs."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import AsyncOpenAI

from agent1_models import Agent1Job, DenseBlock, WorkerLifecycleHook
from agent_context import ContextBlock, build_agent1_context_blocks, model_messages
from dense_block_writer import DenseBlockWriter

logger = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_PROMPT_PATH = PROJECT_ROOT / "agents" / "agent1.md"
PROVIDER = "alibaba_cloud_model_studio"


class QwenAgent1Error(RuntimeError):
    """Raised when a Qwen job cannot produce a usable dense transcript."""


@dataclass(frozen=True, slots=True)
class QwenAgent1Config:
    api_key: str = field(repr=False)
    base_url: str
    provider: str = PROVIDER
    model: str = "qwen3.7-flash"
    prompt_path: Path = DEFAULT_PROMPT_PATH
    temperature: float = 0.1
    timeout_seconds: float = 90.0
    max_attempts: int = 2
    max_output_tokens: int | None = None


def load_qwen_config(
    *, api_key: str | None = None, max_attempts: int | None = None
) -> QwenAgent1Config:
    """Load the minimal Qwen configuration without loading realtime ASR settings."""

    load_dotenv()
    configured_api_key = api_key or os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not configured_api_key:
        raise QwenAgent1Error("DASHSCOPE_API_KEY is missing.")

    explicit_base_url = os.getenv("QWEN_BASE_URL", "").strip()
    base_url = (
        explicit_base_url.rstrip("/")
        if explicit_base_url
        else "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )

    try:
        timeout_seconds = float(os.getenv("QWEN_TIMEOUT_SECONDS", "90"))
        configured_max_attempts = (
            max_attempts
            if max_attempts is not None
            else int(os.getenv("QWEN_MAX_ATTEMPTS", "2"))
        )
        raw_max_output_tokens = os.getenv("QWEN_MAX_OUTPUT_TOKENS", "").strip()
        max_output_tokens = (
            int(raw_max_output_tokens) if raw_max_output_tokens else None
        )
    except ValueError as exc:
        raise QwenAgent1Error("Qwen numeric configuration is invalid.") from exc
    if timeout_seconds <= 0 or configured_max_attempts <= 0:
        raise QwenAgent1Error("Qwen timeout and attempts must be positive.")
    if max_output_tokens is not None and max_output_tokens <= 0:
        raise QwenAgent1Error(
            "Qwen output tokens must be positive when explicitly configured."
        )

    return QwenAgent1Config(
        api_key=configured_api_key,
        base_url=base_url,
        model=os.getenv("QWEN_MODEL", "qwen3.7-flash").strip()
        or "qwen3.7-flash",
        prompt_path=Path(
            os.getenv("AGENT1_PROMPT_PATH", str(DEFAULT_PROMPT_PATH)).strip()
            or DEFAULT_PROMPT_PATH
        ),
        timeout_seconds=timeout_seconds,
        max_attempts=configured_max_attempts,
        max_output_tokens=max_output_tokens,
    )


def load_agent1_prompt(path: Path) -> str:
    prompt = path.read_text(encoding="utf-8").strip()
    if not prompt:
        raise QwenAgent1Error(f"Agent 1 prompt is empty: {path}")
    return prompt


def build_agent1_messages(
    *,
    system_prompt: str,
    job: Agent1Job,
    instruction_blocks: Sequence[ContextBlock] = (),
) -> list[dict[str, str]]:
    """Project only the two English Agent1Job text fields into the request."""

    return model_messages(
        build_agent1_context_blocks(
            system_prompt=system_prompt,
            previous_context=job.previous_overlap_text,
            transcript_chunk=job.current_chunk_text,
            instruction_blocks=instruction_blocks,
        )
    )


class QwenAgent1Worker:
    """Queue worker with bounded retry and append-only DenseBlock output."""

    def __init__(
        self,
        *,
        writer: DenseBlockWriter,
        config: QwenAgent1Config,
        client: Any | None = None,
        lifecycle_hook: WorkerLifecycleHook | None = None,
        instruction_blocks: Sequence[ContextBlock] = (),
    ) -> None:
        self.writer = writer
        self.config = config
        self.system_prompt = load_agent1_prompt(config.prompt_path)
        self.prompt_path = _prompt_path_for_metadata(config.prompt_path)
        self.prompt_hash = hashlib.sha256(
            self.system_prompt.encode("utf-8")
        ).hexdigest()
        self.client = client or AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout_seconds,
            max_retries=0,
        )
        self.completed_blocks: list[DenseBlock] = []
        self.lifecycle_hook = lifecycle_hook
        self.instruction_blocks = tuple(instruction_blocks)
        self.succeeded = 0
        self.failed = 0

    async def run(self, queue: asyncio.Queue[Agent1Job | None]) -> None:
        while True:
            job = await queue.get()
            try:
                if job is None:
                    return
                await self.process_job(job)
            except Exception as exc:  # noqa: BLE001 - keep later jobs alive
                self.failed += 1
                logger.error(
                    "Qwen Agent 1 exhausted retries for block_id=%s: %s",
                    job.block_id,
                    self._safe_error(exc),
                )
                await self._notify("failed", job, None, self.config.max_attempts)
            finally:
                queue.task_done()

    async def process_job(self, job: Agent1Job) -> DenseBlock | None:
        started = time.perf_counter()
        last_error: Exception | None = None
        for attempt in range(1, self.config.max_attempts + 1):
            try:
                await self._notify("start", job, None, attempt)
                model_request: dict[str, Any] = {
                    "model": self.config.model,
                    "messages": build_agent1_messages(
                        system_prompt=self.system_prompt,
                        job=job,
                        instruction_blocks=self.instruction_blocks,
                    ),
                    "temperature": self.config.temperature,
                }
                if self.config.provider == PROVIDER:
                    model_request["extra_body"] = {"enable_thinking": False}
                if self.config.max_output_tokens is not None:
                    model_request["max_tokens"] = self.config.max_output_tokens
                response = await self.client.chat.completions.create(**model_request)
                dense_text = self._response_text(response)
                if not dense_text:
                    raise QwenAgent1Error("Qwen returned an empty response.")

                latency_ms = (time.perf_counter() - started) * 1_000
                usage = getattr(response, "usage", None)
                block = DenseBlock(
                    course_id=job.course_id,
                    lecture_id=job.lecture_id,
                    session_id=job.session_id,
                    block_id=job.block_id,
                    source_sentence_ids=job.source_sentence_ids,
                    start_time=job.start_time,
                    end_time=job.end_time,
                    source_token_count=job.current_chunk_token_count,
                    dense_text=dense_text,
                    created_at=datetime.now(UTC).isoformat(),
                    worker_type="qwen",
                    attempt=attempt,
                    provider=self.config.provider,
                    model=self.config.model,
                    prompt_path=self.prompt_path,
                    prompt_hash=self.prompt_hash,
                    latency_ms=round(latency_ms, 3),
                    input_tokens=self._usage_value(usage, "prompt_tokens"),
                    output_tokens=self._usage_value(usage, "completion_tokens"),
                    total_tokens=self._usage_value(usage, "total_tokens"),
                )
                if self.writer.write(block):
                    self.completed_blocks.append(block)
                    self.succeeded += 1
                    await self._notify("written", job, block, attempt)
                    logger.info(
                        "Qwen Agent 1 completed block_id=%s latency_ms=%.3f",
                        block.block_id,
                        block.latency_ms,
                    )
                    await self._notify("end", job, block, attempt)
                    return block
                logger.info("Qwen Agent 1 deduplicated block_id=%s", block.block_id)
                await self._notify("end", job, block, attempt)
                return None
            except Exception as exc:  # noqa: BLE001 - bounded retry
                last_error = exc
                logger.warning(
                    "Qwen Agent 1 attempt %s/%s failed for block_id=%s: %s",
                    attempt,
                    self.config.max_attempts,
                    job.block_id,
                    self._safe_error(exc),
                )
        assert last_error is not None
        raise QwenAgent1Error(self._safe_error(last_error)) from None

    async def _notify(
        self,
        event: str,
        job: Agent1Job,
        block: DenseBlock | None,
        attempt: int,
    ) -> None:
        if self.lifecycle_hook is None:
            return
        try:
            result = self.lifecycle_hook(event, job, block, attempt)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:  # noqa: BLE001 - UI hooks cannot stop the worker
            logger.error("Qwen Agent 1 lifecycle hook failed: %s", self._safe_error(exc))

    @staticmethod
    def _response_text(response: Any) -> str:
        choices = getattr(response, "choices", None)
        if not choices:
            return ""
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None)
        return content.strip() if isinstance(content, str) else ""

    @staticmethod
    def _usage_value(usage: Any, name: str) -> int | None:
        value = getattr(usage, name, None)
        return value if isinstance(value, int) else None

    def _safe_error(self, exc: Exception) -> str:
        message = str(exc).replace(self.config.api_key, "[REDACTED]")
        message = re.sub(r"Bearer\s+\S+", "Bearer [REDACTED]", message)
        return re.sub(r"sk-[A-Za-z0-9._-]+", "[REDACTED]", message)


def _prompt_path_for_metadata(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(resolved)
