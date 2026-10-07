from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path

from agent1_pipeline import Agent1Pipeline
from dense_block_writer import DenseBlockWriter
from fake_agent1 import FakeAgent1Worker
from token_buffer import DeterministicTokenCounter, TokenBuffer
from transcript import FinalTranscript


def final_transcript(
    sentence_id: int, source: str = "English source"
) -> FinalTranscript:
    return FinalTranscript(
        session_id="session",
        sentence_id=sentence_id,
        source_text=source,
        translated_text="中文哨兵",
        start_ms=sentence_id * 100,
        end_ms=sentence_id * 100 + 90,
    )


def words(prefix: str, count: int) -> str:
    return " ".join(f"{prefix}{index}" for index in range(count))


class Agent1PipelineTests(unittest.IsolatedAsyncioTestCase):
    def build_pipeline(
        self,
        directory: str,
        *,
        target: int,
        sleep_seconds: float = 0.0,
        max_attempts: int = 2,
        fail_first: set[str] | None = None,
        first_target: int | None = None,
        normal_target: int | None = None,
        max_tokens: int = 1_800,
        first_wait: float = 40.0,
        normal_wait: float = 60.0,
        time_minimum: int = 100,
        event_sink=None,
    ) -> tuple[Agent1Pipeline, Path]:
        dense_path = Path(directory) / "dense.jsonl"
        writer = DenseBlockWriter(dense_path)
        token_buffer = TokenBuffer(
            course_id="course",
            lecture_id="lecture",
            session_id="session",
            token_counter=DeterministicTokenCounter(),
            first_target_tokens=first_target or target,
            target_tokens=normal_target or target,
            max_tokens=max_tokens,
            first_max_wait_seconds=first_wait,
            max_wait_seconds=normal_wait,
            time_trigger_min_tokens=time_minimum,
            overlap_tokens=2,
        )
        worker = FakeAgent1Worker(
            writer=writer,
            sleep_seconds=sleep_seconds,
            max_attempts=max_attempts,
            fail_first_block_ids=fail_first,
        )
        return (
            Agent1Pipeline(
                token_buffer=token_buffer,
                worker=worker,
                writer=writer,
                event_sink=event_sink,
            ),
            dense_path,
        )

    async def test_three_second_worker_does_not_slow_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline, _ = self.build_pipeline(
                directory, target=10_000, sleep_seconds=3.0
            )
            await pipeline.start()

            started = time.perf_counter()
            for sentence_id in range(100):
                pipeline.publish_final_transcript(final_transcript(sentence_id))
            publish_elapsed = time.perf_counter() - started

            self.assertLess(publish_elapsed, 0.1)
            self.assertGreater(pipeline.max_final_transcript_backlog, 0)
            self.assertTrue(await pipeline.shutdown(timeout_seconds=5))

    async def test_first_failure_retries_and_dense_is_written_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline, dense_path = self.build_pipeline(
                directory,
                target=1,
                max_attempts=2,
                fail_first={"B000001"},
            )
            await pipeline.start()
            with self.assertLogs("fake_agent1", level="WARNING"):
                pipeline.publish_final_transcript(final_transcript(1))
                self.assertTrue(await pipeline.shutdown(timeout_seconds=2))

            dense_records = [
                json.loads(line)
                for line in dense_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(dense_records), 1)
            self.assertEqual(dense_records[0]["attempt"], 2)
            self.assertNotIn("中文哨兵", dense_records[0]["dense_text"])

    async def test_shutdown_flushes_tail_and_drains_both_queues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline, dense_path = self.build_pipeline(directory, target=5)
            await pipeline.start()
            pipeline.publish_final_transcript(final_transcript(1, "one two three"))
            pipeline.publish_final_transcript(final_transcript(2, "four five six"))
            pipeline.publish_final_transcript(final_transcript(3, "tail only"))

            completed = await pipeline.shutdown(timeout_seconds=2)

            self.assertTrue(completed)
            self.assertEqual(pipeline.final_transcript_queue.qsize(), 0)
            self.assertEqual(pipeline.agent1_job_queue.qsize(), 0)
            records = [
                json.loads(line)
                for line in dense_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                [record["source_token_count"] for record in records], [6, 2]
            )
            self.assertEqual(
                [record["block_id"] for record in records],
                ["session:B000001", "session:B000002"],
            )

    async def test_metric_hook_failure_never_escapes_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline, _ = self.build_pipeline(directory, target=100)

            def broken_metric_hook(queue_name: str, backlog: int) -> None:
                del queue_name, backlog
                raise RuntimeError("metric failure")

            pipeline.queue_metric_hook = broken_metric_hook
            await pipeline.start()
            pipeline.publish_final_transcript(final_transcript(1))
            self.assertTrue(pipeline.errors)
            self.assertTrue(await pipeline.shutdown(timeout_seconds=2))

    async def test_token_target_reaches_async_boundary_detector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline, _ = self.build_pipeline(
                directory,
                target=1_800,
                first_target=300,
                normal_target=700,
                time_minimum=1,
            )
            await pipeline.start()

            pipeline.publish_final_transcript(final_transcript(1, words("first", 300)))
            await self._wait_until(lambda: pipeline.agent1_job_count == 1)
            pipeline.publish_final_transcript(final_transcript(2, words("normal", 300)))
            await self._wait_until(lambda: pipeline.agent1_job_count == 2)

            self.assertTrue(await pipeline.shutdown(timeout_seconds=2))

    async def test_first_time_setting_no_longer_flushes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline, _ = self.build_pipeline(
                directory,
                target=99,
                first_target=5,
                first_wait=0.02,
                time_minimum=2,
            )
            await pipeline.start()
            pipeline.publish_final_transcript(final_transcript(1, "one two"))

            await asyncio.sleep(0.05)
            self.assertEqual(pipeline.agent1_job_count, 0)

            self.assertTrue(await pipeline.shutdown(timeout_seconds=2))

    async def test_normal_time_setting_no_longer_flushes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline, _ = self.build_pipeline(
                directory,
                target=99,
                first_target=5,
                normal_target=5,
                normal_wait=0.02,
                time_minimum=2,
            )
            await pipeline.start()
            pipeline.publish_final_transcript(final_transcript(1, "one two"))
            await asyncio.sleep(0.05)
            self.assertEqual(pipeline.agent1_job_count, 0)

            self.assertTrue(await pipeline.shutdown(timeout_seconds=2))

    async def test_checkpoint_does_not_flush_rolling_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline, _ = self.build_pipeline(directory, target=100)
            await pipeline.start()
            for sentence_id in range(1, 9):
                pipeline.publish_final_transcript(
                    final_transcript(sentence_id, f"source {sentence_id}")
                )

            await pipeline.checkpoint()

            self.assertEqual(pipeline.agent1_job_count, 0)
            self.assertEqual(pipeline.token_buffer.pending_sentence_count, 8)
            self.assertTrue(await pipeline.shutdown(timeout_seconds=2))

    async def test_time_trigger_does_not_flush_tiny_buffer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pipeline, dense_path = self.build_pipeline(
                directory,
                target=5,
                first_wait=0.01,
                time_minimum=3,
            )
            await pipeline.start()
            pipeline.publish_final_transcript(final_transcript(1, "one two"))
            await asyncio.sleep(0.04)

            self.assertEqual(pipeline.agent1_job_count, 0)
            self.assertTrue(await pipeline.shutdown(timeout_seconds=2))
            record = json.loads(dense_path.read_text(encoding="utf-8"))
            self.assertEqual(record["source_token_count"], 2)

    async def test_frontend_event_sequence_includes_result_then_collecting(
        self,
    ) -> None:
        events: list[dict[str, object]] = []

        async def event_sink(payload: dict[str, object]) -> None:
            events.append(payload)

        with tempfile.TemporaryDirectory() as directory:
            pipeline, _ = self.build_pipeline(
                directory,
                target=2,
                time_minimum=1,
                event_sink=event_sink,
            )
            await pipeline.start()
            pipeline.publish_final_transcript(final_transcript(1, "one two"))
            await self._wait_until(lambda: pipeline.dense_block_count == 1)
            self.assertTrue(await pipeline.shutdown(timeout_seconds=2))

        lifecycle = [
            (event["type"], event.get("state"))
            for event in events
            if event["type"] in {"agent1_status", "agent1_result"}
        ]
        self.assertEqual(
            lifecycle[-4:],
            [
                ("agent1_status", "collecting"),
                ("agent1_status", "processing"),
                ("agent1_result", None),
                ("agent1_status", "collecting"),
            ],
        )

    @staticmethod
    async def _yield_to_consumer() -> None:
        await asyncio.sleep(0)

    @staticmethod
    async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.001)


if __name__ == "__main__":
    unittest.main()
