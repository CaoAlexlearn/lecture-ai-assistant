"""Non-blocking queue controller for the live Agent 1 pipeline."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from agent1_boundary import (
    Agent1BoundaryController,
    Agent1BoundaryPolicy,
    BoundarySnapshot,
    LastSentenceBoundaryDetector,
)
from agent1_job_ledger import Agent1JobLedger
from agent1_models import Agent1Job, DenseBlock
from agent1_source_checkpoint import (
    Agent1SourceCheckpoint,
    Agent1SourceCheckpointError,
)
from dense_block_writer import DenseBlockWriter
from token_buffer import TokenBuffer
from transcript import FinalTranscript, SourceFinalTranscript

logger = logging.getLogger(__name__)
QueueMetricHook = Callable[[str, int], None]
Agent1EventSink = Callable[[dict[str, object]], Awaitable[None]]
_SOURCE_WAKE = object()
_SOURCE_STOP = object()


class Agent1Worker(Protocol):
    lifecycle_hook: Any

    async def run(self, queue: asyncio.Queue[Agent1Job | None]) -> None: ...


class Agent1Pipeline:
    def __init__(
        self,
        *,
        token_buffer: TokenBuffer,
        worker: Agent1Worker,
        writer: DenseBlockWriter,
        queue_metric_hook: QueueMetricHook | None = None,
        event_sink: Agent1EventSink | None = None,
        job_ledger: Agent1JobLedger | None = None,
        source_log_path: Path | None = None,
        queue_budget: int = 64,
        source_queue_budget: int = 1_024,
        boundary_controller: Agent1BoundaryController | None = None,
    ) -> None:
        if queue_budget <= 0 or source_queue_budget <= 0:
            raise ValueError("queue budgets must be positive")
        self.source_log_path = source_log_path
        self.source_checkpoint = (
            Agent1SourceCheckpoint(
                job_ledger.root / "source_checkpoint.json",
                session_id=token_buffer.session_id,
            )
            if job_ledger is not None and source_log_path is not None
            else None
        )
        # Production mode carries one coalesced wake-up, never source payloads.
        # The raw event log is the data plane and the durable checkpoint is its cursor.
        self.final_transcript_queue: asyncio.Queue[object] = asyncio.Queue(
            maxsize=1 if self.source_checkpoint is not None else source_queue_budget
        )
        self.agent1_job_queue: asyncio.Queue[Agent1Job | None] = asyncio.Queue(
            maxsize=queue_budget
        )
        self.token_buffer = token_buffer
        self.boundary_controller = boundary_controller or Agent1BoundaryController(
            detector=LastSentenceBoundaryDetector(),
            policy=Agent1BoundaryPolicy(
                check_start_tokens=min(
                    token_buffer.current_target_tokens, token_buffer.max_tokens
                ),
                min_block_tokens=1,
                lookahead_reserve_tokens=0,
                recheck_growth_tokens=1,
                max_tokens=token_buffer.max_tokens,
            ),
            token_counter=token_buffer.token_counter,
        )
        self.worker = worker
        self.writer = writer
        self.queue_metric_hook = queue_metric_hook
        self.event_sink = event_sink
        self.job_ledger = job_ledger
        self.errors: list[str] = []
        self.max_final_transcript_backlog = 0
        self.max_agent1_job_backlog = 0
        self.final_transcript_count = 0
        self.agent1_job_count = 0
        self.dense_block_count = 0
        self.tail_block_token_count: int | None = None
        self.final_to_job_latencies_seconds: list[float] = []
        self.final_to_dense_latencies_seconds: list[float] = []
        self._final_published_monotonic: dict[int | str, float] = {}
        self._job_created_monotonic: dict[str, float] = {}
        self._job_trigger_final_monotonic: dict[str, float] = {}
        self._source_cursor = 0
        self._raw_byte_offset = 0
        self._raw_source_head = 0

        self._accepting = False
        self._started = False
        self._shutdown_task: asyncio.Task[bool] | None = None
        self._scheduled_job_ids: set[str] = set()
        self._schedule_lock = asyncio.Lock()
        self._buffer_lock = asyncio.Lock()
        self._consumer_task: asyncio.Task[None] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._boundary_task: asyncio.Task[None] | None = None
        self.boundary_cut_count = 0
        self.boundary_wait_count = 0
        self.boundary_fallback_count = 0
        self.worker.lifecycle_hook = self._on_worker_lifecycle

    async def start(self) -> None:
        if self._started:
            return
        if self.job_ledger is not None:
            self.job_ledger.recover_interrupted()
        if self.source_checkpoint is not None:
            await self._restore_and_reconcile_sources()
        self._worker_task = asyncio.create_task(
            self.worker.run(self.agent1_job_queue), name="agent1-worker"
        )
        self._consumer_task = asyncio.create_task(
            self._consume_source_finals(), name="source-final-consumer"
        )
        if self.job_ledger is not None:
            await self._schedule_pending_jobs()
        self._started = True
        self._accepting = True
        await self._maybe_start_boundary_check()
        await self._emit_event(
            {"type": "agent1_status", "state": "collecting", "buffered_tokens": 0}
        )

    def publish_source_final(self, transcript: SourceFinalTranscript) -> None:
        """Synchronous, non-blocking, no-raise boundary for realtime output."""

        self._publish_source(transcript)

    def publish_final_transcript(self, transcript: FinalTranscript) -> None:
        """Backward-compatible entry point for replay and older tests."""

        self._publish_source(transcript)

    def _publish_source(
        self, transcript: SourceFinalTranscript | FinalTranscript
    ) -> None:
        try:
            if not self._accepting:
                logger.error(
                    "Agent 1 pipeline rejected source final after stop_accepting"
                )
                return
            if self.source_checkpoint is not None:
                try:
                    self.final_transcript_queue.put_nowait(_SOURCE_WAKE)
                except asyncio.QueueFull:
                    # A queued wake already guarantees another authoritative log scan.
                    pass
            else:
                self.final_transcript_queue.put_nowait(transcript)
            published_monotonic = time.perf_counter()
            self._final_published_monotonic[transcript.sentence_id] = (
                published_monotonic
            )
            if self.source_checkpoint is None:
                self.final_transcript_count += 1
            elif isinstance(transcript, SourceFinalTranscript) and isinstance(
                transcript.source_sequence, int
            ):
                self._raw_source_head = max(
                    self._raw_source_head, transcript.source_sequence
                )
            backlog = self.final_transcript_queue.qsize()
            self.max_final_transcript_backlog = max(
                self.max_final_transcript_backlog, backlog
            )
            logger.info(
                "Source final published at=%s sentence_id=%s SourceFinalQueue qsize=%s",
                datetime.now(UTC).isoformat(),
                transcript.sentence_id,
                backlog,
            )
            self._report_metric("final_transcript_queue", backlog)
        except asyncio.QueueFull as exc:
            self._record_error("source_notification_queue_full", exc)
        except Exception as exc:  # noqa: BLE001 - must not escape into realtime ASR
            self._record_error("publish_source_final", exc)

    def stop_accepting(self) -> None:
        self._accepting = False

    async def checkpoint(self) -> dict[str, int]:
        """Persist current rolling state without creating a semantic boundary."""

        if self.source_checkpoint is not None:
            await self._reconcile_raw_sources()
        else:
            await self.final_transcript_queue.join()
        return self.job_status_counts()

    async def shutdown(self, *, timeout_seconds: float | None) -> bool:
        """Stop ingestion and settle all durable jobs without cancelling on timeout."""

        if self._shutdown_task is None:
            self._shutdown_task = asyncio.create_task(
                self._settle_and_close(), name="agent1-settle-and-close"
            )
        try:
            if timeout_seconds is None:
                return await asyncio.shield(self._shutdown_task)
            async with asyncio.timeout(timeout_seconds):
                return await asyncio.shield(self._shutdown_task)
        except TimeoutError:
            self._record_error(
                "shutdown", TimeoutError(f"timed out after {timeout_seconds}s")
            )
            return False
        except Exception as exc:  # noqa: BLE001 - bounded shutdown is best-effort
            self._record_error("shutdown", exc)
            return False

    async def _settle_and_close(self) -> bool:
        self.stop_accepting()
        logger.info(
            "Agent 1 pipeline shutdown started at=%s", datetime.now(UTC).isoformat()
        )
        if not self._started:
            self.writer.close()
            return True

        await self.final_transcript_queue.put(_SOURCE_STOP)
        await self.final_transcript_queue.join()
        if self._consumer_task is not None:
            await self._consumer_task

        await self._await_boundary_checks()
        while await self._maybe_start_boundary_check():
            await self._await_boundary_checks()
        while (
            self.token_buffer.pending_token_count
            >= self.boundary_controller.policy.max_tokens
        ):
            started = await self._maybe_start_boundary_check(force=True)
            if not started:
                break
            await self._await_boundary_checks()
        await self._flush_final_tail()

        while True:
            await self._schedule_pending_jobs()
            await self.agent1_job_queue.join()
            if self.job_ledger is None or not self.job_ledger.has_unsettled():
                break
            await asyncio.sleep(0)

        await self.agent1_job_queue.put(None)
        await self.agent1_job_queue.join()
        if self._worker_task is not None:
            await self._worker_task
        if self.source_checkpoint is not None:
            await self._assert_completed_source_ownership()
        self.writer.close()
        logger.info(
            "Agent 1 durable jobs settled at=%s counts=%s",
            datetime.now(UTC).isoformat(),
            self.job_status_counts(),
        )
        return True

    async def _drain_pipeline(self) -> None:
        """Backward-compatible internal alias used by older callers/tests."""

        await self._settle_and_close()

    async def _schedule_pending_jobs(self) -> None:
        if self.job_ledger is None:
            return
        async with self._schedule_lock:
            for job in self.job_ledger.pending_jobs():
                if self.agent1_job_queue.full():
                    break
                if job.block_id in self._scheduled_job_ids:
                    continue
                self.agent1_job_queue.put_nowait(job)
                self._scheduled_job_ids.add(job.block_id)

    async def _consume_source_finals(self) -> None:
        while True:
            item = await self.final_transcript_queue.get()
            stopping = item is _SOURCE_STOP
            try:
                if stopping:
                    if self.source_checkpoint is not None:
                        await self._reconcile_raw_sources()
                    return
                if self.source_checkpoint is not None:
                    await self._reconcile_raw_sources()
                    continue
                transcript = item
                if not isinstance(transcript, (SourceFinalTranscript, FinalTranscript)):
                    raise TypeError("Invalid source queue item")
                async with self._buffer_lock:
                    self.token_buffer.append(transcript)
                buffered_tokens = self.token_buffer.pending_token_count
                await self._emit_event(
                    {
                        "type": "agent1_status",
                        "state": "collecting",
                        "buffered_tokens": buffered_tokens,
                    }
                )
                await self._maybe_start_boundary_check()
            except Exception as exc:
                self._record_error("source_final_consumer", exc)
                if stopping:
                    raise
            finally:
                self.final_transcript_queue.task_done()

    async def _restore_and_reconcile_sources(self) -> None:
        assert self.source_checkpoint is not None
        checkpoint = self.source_checkpoint.load()
        if checkpoint is None:
            baseline = (
                self.job_ledger.source_recovery_baseline()
                if self.job_ledger is not None
                else None
            )
            if baseline is not None:
                source_sequence = baseline["lastConsumedSourceSequence"]
                if not isinstance(source_sequence, int):
                    raise Agent1SourceCheckpointError(
                        "Durable Agent1 jobs lack source sequence recovery metadata"
                    )
                self.token_buffer.restore_job_baseline(
                    block_sequence=int(baseline["chunkSequence"]),
                    previous_chunk_text=str(baseline["currentTranscript"]),
                )
                self._source_cursor = source_sequence
            checkpoint = self.source_checkpoint.save(
                last_consumed_source_sequence=self._source_cursor,
                raw_byte_offset=0,
                token_buffer_state=self.token_buffer.export_state(),
                boundary_controller_state=self.boundary_controller.export_state(),
            )
        self._source_cursor = int(checkpoint["lastConsumedSourceSequence"])
        self._raw_source_head = self._source_cursor
        self._raw_byte_offset = int(checkpoint["rawByteOffset"])
        try:
            self.token_buffer.restore_state(dict(checkpoint["tokenBuffer"]))
            controller_state = checkpoint.get("boundaryController")
            if isinstance(controller_state, dict):
                self.boundary_controller.restore_state(controller_state)
        except (TypeError, ValueError) as exc:
            raise Agent1SourceCheckpointError(str(exc)) from exc
        async with self._buffer_lock:
            await self._apply_committed_jobs_locked()
            self._save_checkpoint_locked()
        await self._reconcile_raw_sources()

    async def _reconcile_raw_sources(self) -> None:
        if self.source_checkpoint is None or self.source_log_path is None:
            return
        async with self._buffer_lock:
            rows, end_offset = await asyncio.to_thread(
                self._read_raw_rows, self._raw_byte_offset
            )
            checkpoint_changed = False
            for record, row_end in rows:
                if record.get("event_type") != "source_final":
                    self._raw_byte_offset = row_end
                    checkpoint_changed = True
                    continue
                transcript = self._source_from_raw_record(record)
                assert transcript.source_sequence is not None
                self._raw_source_head = max(
                    self._raw_source_head, transcript.source_sequence
                )
                if transcript.source_sequence <= self._source_cursor:
                    self._raw_byte_offset = row_end
                    checkpoint_changed = True
                    continue
                if transcript.source_sequence != self._source_cursor + 1:
                    raise Agent1SourceCheckpointError(
                        "Raw source sequence is not contiguous with its durable checkpoint"
                    )
                before = self.token_buffer.export_state()
                try:
                    self.token_buffer.append(transcript)
                    await self._apply_committed_jobs_locked()
                    self.source_checkpoint.save(
                        last_consumed_source_sequence=transcript.source_sequence,
                        raw_byte_offset=row_end,
                        token_buffer_state=self.token_buffer.export_state(),
                        boundary_controller_state=(
                            self.boundary_controller.export_state()
                        ),
                    )
                except Exception:
                    self.token_buffer.restore_state(before)
                    raise
                self._source_cursor = transcript.source_sequence
                self._raw_byte_offset = row_end
                checkpoint_changed = False
                self.final_transcript_count += 1
                await self._emit_event(
                    {
                        "type": "agent1_status",
                        "state": "collecting",
                        "buffered_tokens": self.token_buffer.pending_token_count,
                    }
                )
            if checkpoint_changed and self._raw_byte_offset == end_offset:
                self.source_checkpoint.save(
                    last_consumed_source_sequence=self._source_cursor,
                    raw_byte_offset=self._raw_byte_offset,
                    token_buffer_state=self.token_buffer.export_state(),
                    boundary_controller_state=self.boundary_controller.export_state(),
                )

        await self._maybe_start_boundary_check()

    async def _apply_committed_jobs_locked(self) -> None:
        """Advance a stale checkpoint through immutable jobs after a crash."""

        if self.job_ledger is None:
            return
        while True:
            sequence = int(self.token_buffer.export_state()["blockSequence"]) + 1
            committed = self.job_ledger.job_for_sequence(sequence)
            if committed is None:
                return
            live_ids = tuple(
                source.sentence_id for source in self.token_buffer.snapshot_sentences()
            )
            expected = committed.source_sentence_ids
            if len(live_ids) < len(expected):
                if live_ids != expected[: len(live_ids)]:
                    raise Agent1SourceCheckpointError(
                        "Durable Agent1 job conflicts with recovered rolling buffer"
                    )
                return
            if live_ids[: len(expected)] != expected:
                raise Agent1SourceCheckpointError(
                    "Durable Agent1 job is not the next rolling-buffer prefix"
                )
            replayed = self.token_buffer.consume_prefix(
                expected[-1], boundary_metadata=committed.boundary_metadata
            )
            if replayed != committed:
                raise Agent1SourceCheckpointError(
                    "Recovered Agent1 job payload differs from durable segmentation"
                )
            self.boundary_controller.finish_cut()

    def _save_checkpoint_locked(self) -> None:
        if self.source_checkpoint is None:
            return
        self.source_checkpoint.save(
            last_consumed_source_sequence=self._source_cursor,
            raw_byte_offset=self._raw_byte_offset,
            token_buffer_state=self.token_buffer.export_state(),
            boundary_controller_state=self.boundary_controller.export_state(),
        )

    async def _maybe_start_boundary_check(self, *, force: bool = False) -> bool:
        if self._boundary_task is not None and not self._boundary_task.done():
            return False
        async with self._buffer_lock:
            snapshot = self.boundary_controller.snapshot(
                self.token_buffer.snapshot_sentences()
            )
            if not self.boundary_controller.should_check(snapshot, force=force):
                return False
            check_number = self.boundary_controller.begin_check()
            self._save_checkpoint_locked()
            logger.info(
                "Agent1 boundary check started check=%s tokens=%s sources=%s-%s",
                check_number,
                snapshot.token_count,
                snapshot.source_sequence_start,
                snapshot.source_sequence_end,
            )
            self._boundary_task = asyncio.create_task(
                self._run_boundary_check(snapshot), name="agent1-boundary-check"
            )
        return True

    async def _run_boundary_check(self, snapshot: BoundarySnapshot) -> None:
        decision = None
        detector_error: str | None = None
        commit_error: Exception | None = None
        try:
            decision = await self.boundary_controller.detector.detect(
                snapshot,
                hard_max_reached=(
                    snapshot.token_count >= self.boundary_controller.policy.max_tokens
                ),
            )
        except Exception as exc:  # noqa: BLE001 - hard max has deterministic fallback
            detector_error = f"{type(exc).__name__}: {exc}"

        try:
            async with self._buffer_lock:
                before_buffer = self.token_buffer.export_state()
                before_controller = self.boundary_controller.export_state()
                resolution = self.boundary_controller.resolve(
                    snapshot,
                    self.token_buffer.snapshot_sentences(),
                    decision,
                    detector_error=detector_error,
                )
                if resolution is None:
                    self.boundary_controller.finish_wait(snapshot)
                    self.boundary_wait_count += 1
                    self._save_checkpoint_locked()
                    log = (
                        logger.warning
                        if detector_error
                        or (decision is not None and decision.decision != "wait")
                        else logger.info
                    )
                    log(
                        "Agent1 boundary treated as WAIT tokens=%s "
                        "decision=%s detector_error=%s",
                        snapshot.token_count,
                        decision,
                        detector_error,
                    )
                    return
                try:
                    job = self.token_buffer.consume_prefix(
                        resolution.cut_after_source_id,
                        boundary_metadata=resolution.metadata,
                    )
                    # Durable segmentation commit precedes buffer checkpoint advance.
                    await self._enqueue_agent1_job(job)
                    self.boundary_controller.finish_cut()
                    self._save_checkpoint_locked()
                except Exception:
                    self.token_buffer.restore_state(before_buffer)
                    self.boundary_controller.restore_state(before_controller)
                    self.boundary_controller.abort_check()
                    raise
                self.boundary_cut_count += 1
                if resolution.metadata.boundary_fallback:
                    self.boundary_fallback_count += 1
                logger.info(
                    "Agent1 boundary CUT block_id=%s prefix_tokens=%s "
                    "suffix_tokens=%s fallback=%s warning=%s",
                    job.block_id,
                    resolution.metadata.prefix_tokens,
                    resolution.metadata.suffix_tokens,
                    resolution.metadata.boundary_fallback,
                    resolution.warning,
                )
        except Exception as exc:  # noqa: BLE001 - segmentation failure is isolated
            commit_error = exc
            async with self._buffer_lock:
                if self.boundary_controller.boundary_check_in_flight:
                    self.boundary_controller.finish_wait(snapshot)
                    try:
                        self._save_checkpoint_locked()
                    except Exception as checkpoint_exc:  # noqa: BLE001
                        self._record_error("boundary_checkpoint", checkpoint_exc)
            self._record_error("boundary_commit", exc)
        finally:
            current = asyncio.current_task()
            if self._boundary_task is current:
                self._boundary_task = None
            if self._accepting and commit_error is None:
                await self._maybe_start_boundary_check()

    async def _await_boundary_checks(self) -> None:
        while self._boundary_task is not None:
            task = self._boundary_task
            await asyncio.shield(task)
            if self._boundary_task is task:
                self._boundary_task = None

    async def _flush_final_tail(self) -> None:
        async with self._buffer_lock:
            await self._apply_committed_jobs_locked()
            self._save_checkpoint_locked()
            snapshot = self.boundary_controller.snapshot(
                self.token_buffer.snapshot_sentences()
            )
            if not snapshot.sentences:
                return
            before_buffer = self.token_buffer.export_state()
            before_controller = self.boundary_controller.export_state()
            metadata = self.boundary_controller.final_tail_metadata(snapshot)
            try:
                job = self.token_buffer.flush(boundary_metadata=metadata)
                assert job is not None
                await self._enqueue_agent1_job(job)
                self.boundary_controller.finish_cut()
                self._save_checkpoint_locked()
            except Exception:
                self.token_buffer.restore_state(before_buffer)
                self.boundary_controller.restore_state(before_controller)
                raise
            self.tail_block_token_count = job.current_chunk_token_count

    def _read_raw_rows(
        self, start_offset: int
    ) -> tuple[list[tuple[dict[str, Any], int]], int]:
        assert self.source_log_path is not None
        if not self.source_log_path.exists():
            if start_offset:
                raise Agent1SourceCheckpointError(
                    "Raw source log disappeared after checkpointing"
                )
            return [], 0
        size = self.source_log_path.stat().st_size
        if start_offset < 0 or start_offset > size:
            raise Agent1SourceCheckpointError("Raw source checkpoint offset is invalid")
        rows: list[tuple[dict[str, Any], int]] = []
        with self.source_log_path.open("rb") as handle:
            handle.seek(start_offset)
            while True:
                line = handle.readline()
                if not line:
                    break
                row_end = handle.tell()
                if not line.endswith(b"\n"):
                    raise Agent1SourceCheckpointError(
                        "Raw source log has an incomplete trailing record"
                    )
                try:
                    record = json.loads(line)
                except (UnicodeError, json.JSONDecodeError) as exc:
                    raise Agent1SourceCheckpointError(
                        "Raw source log contains an unreadable record"
                    ) from exc
                if not isinstance(record, dict):
                    raise Agent1SourceCheckpointError(
                        "Raw source log record must be an object"
                    )
                rows.append((record, row_end))
        return rows, size

    def _source_from_raw_record(self, record: dict[str, Any]) -> SourceFinalTranscript:
        if record.get("session_id") != self.token_buffer.session_id:
            raise Agent1SourceCheckpointError(
                "Raw source event belongs to another ClassSession"
            )
        sequence = record.get("source_sequence")
        identity = record.get("source_identity")
        text = record.get("source_text")
        if (
            not isinstance(sequence, int)
            or sequence <= 0
            or not isinstance(identity, (str, int))
            or not isinstance(text, str)
        ):
            raise Agent1SourceCheckpointError(
                "Raw source event is missing identity fields"
            )
        return SourceFinalTranscript(
            session_id=self.token_buffer.session_id,
            sentence_id=identity,
            source_text=text,
            start_ms=record.get("begin_time")
            if isinstance(record.get("begin_time"), int)
            else None,
            end_ms=record.get("end_time")
            if isinstance(record.get("end_time"), int)
            else None,
            source_sequence=sequence,
            provider_item_id=(
                record.get("provider_item_id")
                if isinstance(record.get("provider_item_id"), str)
                else None
            ),
            connection_generation=(
                record.get("connection_generation")
                if isinstance(record.get("connection_generation"), int)
                else 1
            ),
        )

    async def _assert_completed_source_ownership(self) -> None:
        await self._reconcile_raw_sources()
        if self.token_buffer.pending_sentence_count:
            raise Agent1SourceCheckpointError(
                "Completed Agent1 pipeline still owns an unflushed source buffer"
            )
        rows, _ = await asyncio.to_thread(self._read_raw_rows, 0)
        raw_source_ids = [
            record.get("source_identity")
            for record, _ in rows
            if record.get("event_type") == "source_final"
        ]
        report = (
            self.job_ledger.source_partition_report(raw_source_ids)
            if self.job_ledger
            else {"valid": not raw_source_ids}
        )
        if not report["valid"]:
            raise Agent1SourceCheckpointError(
                "Durable Agent1 source ownership is not an exact ordered partition: "
                f"{report}"
            )

    async def _enqueue_agent1_job(self, job: Agent1Job) -> None:
        created_monotonic = time.perf_counter()
        trigger_final_monotonic = max(
            (
                self._final_published_monotonic[sentence_id]
                for sentence_id in job.source_sentence_ids
                if sentence_id in self._final_published_monotonic
            ),
            default=created_monotonic,
        )
        self._job_created_monotonic[job.block_id] = created_monotonic
        self._job_trigger_final_monotonic[job.block_id] = trigger_final_monotonic
        final_to_job = created_monotonic - trigger_final_monotonic
        self.final_to_job_latencies_seconds.append(final_to_job)
        if self.job_ledger is not None:
            self.job_ledger.create_pending(job)
        self.agent1_job_count += 1
        if self.job_ledger is None:
            await self.agent1_job_queue.put(job)
        else:
            await self._schedule_pending_jobs()
        backlog = self.agent1_job_queue.qsize()
        self.max_agent1_job_backlog = max(self.max_agent1_job_backlog, backlog)
        logger.info(
            "Agent1Job created at=%s block_id=%s tokens=%s final_to_job_ms=%.3f "
            "Agent1JobQueue qsize=%s",
            datetime.now(UTC).isoformat(),
            job.block_id,
            job.current_chunk_token_count,
            final_to_job * 1_000,
            backlog,
        )
        self._report_metric("agent1_job_queue", backlog)

    async def _emit_event(self, payload: dict[str, object]) -> None:
        if self.event_sink is None:
            return
        try:
            await self.event_sink(payload)
        except Exception as exc:  # noqa: BLE001 - UI cannot affect Agent 1 or realtime
            self._record_error("event_sink", exc)

    def _report_metric(self, queue_name: str, backlog: int) -> None:
        if self.queue_metric_hook is None:
            return
        try:
            self.queue_metric_hook(queue_name, backlog)
        except Exception as exc:  # noqa: BLE001 - metrics cannot affect ingestion
            self._record_error("queue_metric_hook", exc)

    def _record_error(self, stage: str, exc: Exception) -> None:
        message = f"{stage}: {exc}"
        self.errors.append(message)
        logger.error("Agent 1 pipeline error at %s: %s", stage, exc)

    async def _on_worker_lifecycle(
        self,
        event: str,
        job: Agent1Job,
        block: DenseBlock | None,
        attempt: int,
    ) -> None:
        if event == "start" and self.job_ledger is not None:
            self.job_ledger.mark_running(job.block_id, attempt)
        if event == "start" and attempt == 1:
            await self._emit_event(
                {
                    "type": "agent1_status",
                    "state": "processing",
                    "block_id": job.block_id,
                }
            )
            return
        if event == "failed":
            if self.job_ledger is not None:
                self.job_ledger.mark_failed(
                    job.block_id, attempt, "Agent1 exhausted its configured attempts."
                )
            self._scheduled_job_ids.discard(job.block_id)
            await self._schedule_pending_jobs()
            await self._emit_event(
                {
                    "type": "agent1_status",
                    "state": "collecting",
                    "buffered_tokens": self.token_buffer.pending_token_count,
                }
            )
            return
        if event != "written" or block is None:
            if event == "end" and block is not None and self.job_ledger is not None:
                self.job_ledger.mark_written(job.block_id, attempt)
                self._scheduled_job_ids.discard(job.block_id)
                await self._schedule_pending_jobs()
            return
        if self.job_ledger is not None:
            self.job_ledger.mark_written(job.block_id, attempt)
        self._scheduled_job_ids.discard(job.block_id)
        await self._schedule_pending_jobs()
        completed_monotonic = time.perf_counter()
        trigger_final = self._job_trigger_final_monotonic.get(
            job.block_id, completed_monotonic
        )
        final_to_dense = completed_monotonic - trigger_final
        self.final_to_dense_latencies_seconds.append(final_to_dense)
        self.dense_block_count += 1
        logger.info(
            "DenseBlock completed block_id=%s final_to_dense_ms=%.3f",
            job.block_id,
            final_to_dense * 1_000,
        )
        await self._emit_event(
            {
                "type": "agent1_result",
                "block_id": block.block_id,
                "content": block.dense_text,
                "created_at": block.created_at,
                "source_token_count": block.source_token_count,
            }
        )
        await self._emit_event(
            {
                "type": "agent1_status",
                "state": "collecting",
                "buffered_tokens": self.token_buffer.pending_token_count,
            }
        )

    def runtime_metrics(self) -> dict[str, Any]:
        return {
            "final_transcript_count": self.final_transcript_count,
            "agent1_job_count": self.agent1_job_count,
            "dense_block_count": self.dense_block_count,
            "tail_block_token_count": self.tail_block_token_count,
            "final_transcript_queue_max_qsize": self.max_final_transcript_backlog,
            "agent1_job_queue_max_qsize": self.max_agent1_job_backlog,
            "final_to_job_latency_seconds": self.final_to_job_latencies_seconds,
            "final_to_dense_latency_seconds": self.final_to_dense_latencies_seconds,
            "errors": list(self.errors),
            "job_status_counts": self.job_status_counts(),
            "source_status": self.source_status(),
            "boundary": {
                "checks": self.boundary_controller.boundary_check_count,
                "cuts": self.boundary_cut_count,
                "waits": self.boundary_wait_count,
                "fallbacks": self.boundary_fallback_count,
                "inFlight": self.boundary_controller.boundary_check_in_flight,
                "lastCheckedTokenCount": (
                    self.boundary_controller.last_checked_token_count
                ),
            },
        }

    def source_status(self) -> dict[str, int]:
        counts = self.job_status_counts()
        return {
            "sourceCursor": self._source_cursor,
            "rawSourceHead": self._raw_source_head,
            "bufferedSourceCount": self.token_buffer.pending_sentence_count,
            "durableJobCount": sum(counts.values()),
            "pendingJobs": counts["pending"],
            "runningJobs": counts["running"],
            "failedJobs": counts["failed"],
        }

    def job_status_counts(self) -> dict[str, int]:
        if self.job_ledger is None:
            return {
                "pending": self.agent1_job_queue.qsize(),
                "running": 0,
                "written": self.dense_block_count,
                "failed": 0,
            }
        return self.job_ledger.status_counts()

    async def _cancel_tasks(self) -> None:
        tasks = [
            task
            for task in (self._consumer_task, self._worker_task, self._boundary_task)
            if task is not None and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
