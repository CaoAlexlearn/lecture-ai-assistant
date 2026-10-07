"""Fake asynchronous Agent 1 worker used only to validate pipeline behavior."""

from __future__ import annotations

import asyncio
import inspect
import logging
from datetime import UTC, datetime

from agent1_models import Agent1Job, DenseBlock, WorkerLifecycleHook
from dense_block_writer import DenseBlockWriter

logger = logging.getLogger(__name__)


class FakeAgent1Worker:
    def __init__(
        self,
        *,
        writer: DenseBlockWriter,
        sleep_seconds: float = 0.0,
        max_attempts: int = 2,
        fail_first_block_ids: set[str] | None = None,
        lifecycle_hook: WorkerLifecycleHook | None = None,
    ) -> None:
        if sleep_seconds < 0:
            raise ValueError("sleep_seconds cannot be negative")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        self.writer = writer
        self.sleep_seconds = sleep_seconds
        self.max_attempts = max_attempts
        self.fail_first_block_ids = fail_first_block_ids or set()
        self.lifecycle_hook = lifecycle_hook
        self.succeeded = 0
        self.failed = 0

    async def run(self, queue: asyncio.Queue[Agent1Job | None]) -> None:
        """Process jobs until a None sentinel; never exit on a single job failure."""

        while True:
            job = await queue.get()
            try:
                if job is None:
                    return
                await self._process_with_retry(job)
            except Exception:
                self.failed += 1
                logger.exception("Fake Agent 1 exhausted retries for %s", job.block_id)
                await self._notify("failed", job, None, self.max_attempts)
            finally:
                queue.task_done()

    async def _process_with_retry(self, job: Agent1Job) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                logger.info(
                    "Fake worker start at=%s block_id=%s attempt=%s",
                    datetime.now(UTC).isoformat(),
                    job.block_id,
                    attempt,
                )
                await self._notify("start", job, None, attempt)
                if self.sleep_seconds:
                    await asyncio.sleep(self.sleep_seconds)
                block_suffix = job.block_id.rsplit(":", maxsplit=1)[-1]
                if (
                    job.block_id in self.fail_first_block_ids
                    or block_suffix in self.fail_first_block_ids
                ) and attempt == 1:
                    raise RuntimeError("injected first-attempt failure")

                block = self._create_dense_block(job, attempt)
                written = self.writer.write(block)
                self.succeeded += 1
                logger.info(
                    "Fake worker end at=%s block_id=%s attempt=%s written=%s",
                    datetime.now(UTC).isoformat(),
                    job.block_id,
                    attempt,
                    written,
                )
                if written:
                    await self._notify("written", job, block, attempt)
                await self._notify("end", job, block, attempt)
                return
            except Exception as exc:  # noqa: BLE001 - retry all job failures
                last_error = exc
                logger.warning(
                    "Fake Agent 1 attempt %s/%s failed for %s: %s",
                    attempt,
                    self.max_attempts,
                    job.block_id,
                    exc,
                )
        assert last_error is not None
        raise last_error

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
        except Exception as exc:  # noqa: BLE001 - observability cannot stop worker
            logger.error("Fake worker lifecycle hook failed: %s", exc)

    @staticmethod
    def _create_dense_block(job: Agent1Job, attempt: int) -> DenseBlock:
        dense_text = (
            "FAKE_AGENT1_OUTPUT\n"
            f"block_id={job.block_id}\n"
            f"sentence_count={len(job.source_sentence_ids)}\n"
            f"token_count={job.current_chunk_token_count}"
        )
        return DenseBlock(
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
            worker_type="fake",
            attempt=attempt,
            provider="local",
            model="fake-agent1",
            prompt_version="fake-v1",
        )
