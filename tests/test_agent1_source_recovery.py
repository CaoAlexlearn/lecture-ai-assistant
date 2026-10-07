from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from agent1_job_ledger import Agent1JobLedger
from agent1_pipeline import Agent1Pipeline
from agent1_source_checkpoint import Agent1SourceCheckpointError
from dense_block_writer import DenseBlockWriter
from fake_agent1 import FakeAgent1Worker
from agent1_source_fixture import ClassroomTranscriptProcessor, _PendingTranscript
from session_runtime import SessionRuntimeOwner
from tests.test_p1_class_session_runtime import create_catalog, start_session
from token_buffer import DeterministicTokenCounter, TokenBuffer

CHILD_PROGRAM = textwrap.dedent(
    r"""
    import asyncio
    import os
    import sys
    from pathlib import Path
    from agent1_job_ledger import Agent1JobLedger
    from agent1_pipeline import Agent1Pipeline
    from dense_block_writer import DenseBlockWriter
    from fake_agent1 import FakeAgent1Worker
    from agent1_source_fixture import ClassroomTranscriptProcessor, _PendingTranscript
    from token_buffer import DeterministicTokenCounter, TokenBuffer

    root = Path(sys.argv[1])
    mode = sys.argv[2]
    session_id = "crash-session"
    raw = root / "raw_sentences.jsonl"
    ledger = Agent1JobLedger(
        root / "agent1",
        session_id=session_id,
        prompt_version_id="v1",
        instruction_snapshot={},
    )
    class CrashAfterDenseWriter(DenseBlockWriter):
        def write(self, block):
            written = super().write(block)
            if mode == "after_dense":
                os._exit(0)
            return written

    writer = CrashAfterDenseWriter(root / "dense.jsonl")
    pipeline = Agent1Pipeline(
        token_buffer=TokenBuffer(
            course_id="course",
            lecture_id="lecture",
            session_id=session_id,
            token_counter=DeterministicTokenCounter(),
            token_target=10 if mode == "buffered" else 1,
        ),
        worker=FakeAgent1Worker(writer=writer),
        writer=writer,
        job_ledger=ledger,
        source_log_path=raw,
        queue_budget=1,
    )

    async def main():
        await pipeline.start()
        processor = ClassroomTranscriptProcessor(
            session_id=session_id,
            output_path=raw,
            source_final_sink=(None if mode == "before_queue" else pipeline.publish_source_final),
            connection_generation=1,
        )
        if mode == "before_job":
            ledger.create_pending = lambda job: os._exit(0)
        elif mode == "after_job":
            original = ledger.create_pending
            def create_then_exit(job):
                original(job)
                os._exit(0)
            ledger.create_pending = create_then_exit
        processor._publish_source_final(
            _PendingTranscript(
                sentence_id="provider-1",
                provider_generation=1,
                source_text="one two three",
            )
        )
        if mode in {"before_queue", "before_consumer"}:
            os._exit(0)
        while True:
            status = pipeline.source_status()
            if mode == "buffered" and status["sourceCursor"] == 1:
                os._exit(0)
            await asyncio.sleep(0.001)

    asyncio.run(main())
    """
)


class Agent1SourceRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def build_pipeline(
        self,
        root: Path,
        *,
        session_id: str = "crash-session",
        target: int = 1,
        ledger: Agent1JobLedger | None = None,
    ) -> tuple[Agent1Pipeline, Agent1JobLedger]:
        ledger = ledger or Agent1JobLedger(
            root / "agent1",
            session_id=session_id,
            prompt_version_id="v1",
            instruction_snapshot={},
        )
        writer = DenseBlockWriter(root / "dense.jsonl")
        return (
            Agent1Pipeline(
                token_buffer=TokenBuffer(
                    course_id="course",
                    lecture_id="lecture",
                    session_id=session_id,
                    token_counter=DeterministicTokenCounter(),
                    token_target=target,
                ),
                worker=FakeAgent1Worker(writer=writer),
                writer=writer,
                job_ledger=ledger,
                source_log_path=root / "raw_sentences.jsonl",
                queue_budget=1,
            ),
            ledger,
        )

    def hard_exit(self, root: Path, mode: str) -> None:
        result = subprocess.run(
            [sys.executable, "-c", CHILD_PROGRAM, str(root), mode],
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0)

    async def recover_and_finish(
        self, root: Path, *, target: int = 1
    ) -> tuple[Agent1Pipeline, Agent1JobLedger]:
        pipeline, ledger = self.build_pipeline(root, target=target)
        await pipeline.start()
        await pipeline.shutdown(timeout_seconds=None)
        return pipeline, ledger

    async def test_window_a_source_fsync_before_queue_replays_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.hard_exit(root, "before_queue")
            self.assertEqual(list((root / "agent1/jobs").glob("J*.json")), [])
            _, ledger = await self.recover_and_finish(root)
            self.assertEqual(ledger.status_counts()["written"], 1)
            self.assertEqual(len((root / "dense.jsonl").read_text().splitlines()), 1)

    async def test_window_b_queue_before_consumer_replays_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.hard_exit(root, "before_consumer")
            self.assertEqual(list((root / "agent1/jobs").glob("J*.json")), [])
            _, ledger = await self.recover_and_finish(root)
            self.assertEqual(ledger.status_counts()["written"], 1)

    async def test_window_c_checkpointed_buffer_is_restored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.hard_exit(root, "buffered")
            pipeline, ledger = self.build_pipeline(root, target=10)
            await pipeline.start()
            self.assertEqual(pipeline.source_status()["bufferedSourceCount"], 1)
            self.assertEqual(ledger.status_counts()["pending"], 0)
            await pipeline.shutdown(timeout_seconds=None)
            self.assertEqual(ledger.status_counts()["written"], 1)

    async def test_window_d_flush_before_create_pending_replays_same_job(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.hard_exit(root, "before_job")
            self.assertEqual(list((root / "agent1/jobs").glob("J*.json")), [])
            _, ledger = await self.recover_and_finish(root)
            records = list((root / "agent1/jobs").glob("J*.json"))
            self.assertEqual(len(records), 1)
            self.assertEqual(ledger.status_counts()["written"], 1)

    async def test_window_e_job_before_checkpoint_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.hard_exit(root, "after_job")
            records = list((root / "agent1/jobs").glob("J*.json"))
            self.assertEqual(len(records), 1)
            _, ledger = await self.recover_and_finish(root)
            records = list((root / "agent1/jobs").glob("J*.json"))
            self.assertEqual(len(records), 1)
            self.assertEqual(ledger.status_counts()["written"], 1)
            self.assertEqual(len((root / "dense.jsonl").read_text().splitlines()), 1)

    async def test_window_f_dense_before_ledger_written_reconciles_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.hard_exit(root, "after_dense")
            self.assertEqual(len((root / "dense.jsonl").read_text().splitlines()), 1)
            record = json.loads(
                next((root / "agent1/jobs").glob("J*.json")).read_text()
            )
            self.assertEqual(record["status"], "running")

            _, ledger = await self.recover_and_finish(root)

            self.assertEqual(ledger.status_counts()["written"], 1)
            self.assertEqual(len((root / "dense.jsonl").read_text().splitlines()), 1)

    async def test_create_pending_oserror_keeps_recoverable_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            processor = ClassroomTranscriptProcessor(
                session_id="crash-session",
                output_path=root / "raw_sentences.jsonl",
                connection_generation=1,
            )
            processor._publish_source_final(
                _PendingTranscript(
                    sentence_id="provider-1",
                    provider_generation=1,
                    source_text="one two three",
                )
            )
            processor.close()
            pipeline, ledger = self.build_pipeline(root)
            with mock.patch.object(
                ledger, "create_pending", side_effect=OSError("injected disk error")
            ):
                await pipeline.start()
                await asyncio.sleep(0.02)
                self.assertTrue(
                    any("injected disk error" in error for error in pipeline.errors)
                )
            checkpoint = json.loads(
                (root / "agent1/source_checkpoint.json").read_text()
            )
            self.assertEqual(checkpoint["lastConsumedSourceSequence"], 1)
            self.assertEqual(len(checkpoint["tokenBuffer"]["sources"]), 1)
            pipeline.writer.close()

            _, recovered = await self.recover_and_finish(root)
            self.assertEqual(recovered.status_counts()["written"], 1)

    async def test_high_source_rate_uses_one_slot_notification_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline, ledger = self.build_pipeline(root, target=10_000)
            await pipeline.start()
            processor = ClassroomTranscriptProcessor(
                session_id="crash-session",
                output_path=root / "raw_sentences.jsonl",
                source_final_sink=pipeline.publish_source_final,
                connection_generation=1,
            )
            for index in range(300):
                processor._publish_source_final(
                    _PendingTranscript(
                        sentence_id=f"provider-{index}",
                        provider_generation=1,
                        source_text=f"source {index}",
                    )
                )
            processor.close()
            self.assertEqual(pipeline.final_transcript_queue.maxsize, 1)
            self.assertLessEqual(pipeline.final_transcript_queue.qsize(), 1)
            self.assertEqual(
                len((root / "raw_sentences.jsonl").read_text().splitlines()), 300
            )
            await pipeline.shutdown(timeout_seconds=None)
            self.assertEqual(ledger.status_counts()["written"], 1)
            self.assertEqual(pipeline.source_status()["sourceCursor"], 300)

    async def test_pause_and_stop_reconcile_raw_before_terminal_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, course_id, lecture_id = create_catalog(root)
            session = start_session(catalog, course_id, lecture_id)
            session_id = str(session["id"])
            pipeline, ledger = self.build_pipeline(
                root / "runtime", session_id=session_id, target=100
            )
            owner = SessionRuntimeOwner(
                catalog=catalog,
                course_id=course_id,
                lecture_id=lecture_id,
                session_id=session_id,
                agent1_pipeline=pipeline,
            )
            await owner.start()
            processor = ClassroomTranscriptProcessor(
                session_id=session_id,
                output_path=root / "runtime/raw_sentences.jsonl",
                source_final_sink=pipeline.publish_source_final,
            )
            processor._publish_source_final(
                _PendingTranscript(
                    sentence_id="provider-1",
                    provider_generation=1,
                    source_text="pause must reconcile this source",
                )
            )
            processor.close()
            paused = await owner.pause()
            self.assertEqual(paused["status"], "paused")
            self.assertEqual(paused["agent1SourceStatus"]["sourceCursor"], 1)
            self.assertEqual(paused["agent1SourceStatus"]["bufferedSourceCount"], 1)
            self.assertEqual(ledger.status_counts()["pending"], 0)
            completed = await owner.stop()
            self.assertEqual(completed["status"], "completed")
            self.assertTrue(completed["notesDrained"])
            self.assertEqual(ledger.status_counts()["written"], 1)

    async def test_recovery_finishes_before_new_source_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = ClassroomTranscriptProcessor(
                session_id="crash-session",
                output_path=root / "raw_sentences.jsonl",
                connection_generation=1,
            )
            first._publish_source_final(
                _PendingTranscript(
                    sentence_id="provider-1",
                    provider_generation=1,
                    source_text="old source",
                )
            )
            first.close()
            pipeline, ledger = self.build_pipeline(root)
            await pipeline.start()
            self.assertEqual(pipeline.source_status()["sourceCursor"], 1)
            second = ClassroomTranscriptProcessor(
                session_id="crash-session",
                output_path=root / "raw_sentences.jsonl",
                source_final_sink=pipeline.publish_source_final,
                connection_generation=2,
            )
            second._publish_source_final(
                _PendingTranscript(
                    sentence_id="provider-2",
                    provider_generation=1,
                    source_text="new source",
                )
            )
            second.close()
            await pipeline.shutdown(timeout_seconds=None)
            jobs = [
                json.loads(path.read_text())
                for path in sorted((root / "agent1/jobs").glob("J*.json"))
            ]
            self.assertEqual([item["chunkSequence"] for item in jobs], [1, 2])
            self.assertEqual(
                [item["sourceSequenceRange"] for item in jobs], [[1, 1], [2, 2]]
            )
            self.assertEqual(ledger.status_counts()["written"], 2)

    async def test_corrupt_checkpoint_fails_closed_before_accepting_source(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / "agent1/source_checkpoint.json"
            checkpoint.parent.mkdir(parents=True)
            checkpoint.write_text("{not-json\n", encoding="utf-8")
            pipeline, _ = self.build_pipeline(root)

            with self.assertRaises(Agent1SourceCheckpointError):
                await pipeline.start()

            self.assertFalse(pipeline._accepting)
            self.assertFalse(pipeline._started)
            self.assertIsNone(pipeline._consumer_task)
            self.assertIsNone(pipeline._worker_task)
            pipeline.writer.close()

    async def test_tampered_checkpoint_fails_integrity_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pipeline, _ = self.build_pipeline(root)
            await pipeline.start()
            await pipeline.shutdown(timeout_seconds=None)
            checkpoint_path = root / "agent1/source_checkpoint.json"
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            checkpoint["lastConsumedSourceSequence"] = 999
            checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")

            restarted, _ = self.build_pipeline(root)
            with self.assertRaises(Agent1SourceCheckpointError):
                await restarted.start()
            self.assertFalse(restarted._accepting)
            restarted.writer.close()

    async def test_stop_reconcile_error_does_not_complete_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, course_id, lecture_id = create_catalog(root)
            session = start_session(catalog, course_id, lecture_id)
            session_id = str(session["id"])
            pipeline, _ = self.build_pipeline(
                root / "runtime", session_id=session_id, target=100
            )
            owner = SessionRuntimeOwner(
                catalog=catalog,
                course_id=course_id,
                lecture_id=lecture_id,
                session_id=session_id,
                agent1_pipeline=pipeline,
            )
            await owner.start()
            (root / "runtime/raw_sentences.jsonl").write_bytes(b"{incomplete")

            with self.assertRaises(ValueError):
                await owner.stop()

            failed = catalog.get_session(course_id, lecture_id, session_id)
            self.assertEqual(failed["status"], "live")
            self.assertEqual(failed["runtimeState"], "drain_failed")
            self.assertFalse(failed["notesDrained"])


if __name__ == "__main__":
    unittest.main()
