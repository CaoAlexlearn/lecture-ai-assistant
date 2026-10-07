from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from agent1_boundary import (
    Agent1BoundaryController,
    Agent1BoundaryPolicy,
    BoundaryDecision,
)
from agent1_job_ledger import Agent1JobLedger
from agent1_pipeline import Agent1Pipeline
from dense_block_writer import DenseBlockWriter
from fake_agent1 import FakeAgent1Worker
from agent1_source_fixture import ClassroomTranscriptProcessor, _PendingTranscript
from replay_agent1 import ReplayConfig, build_replay_jobs
from token_buffer import DeterministicTokenCounter, TokenBuffer
from transcript import FinalTranscript


def source(source_id: int, token_count: int = 1) -> FinalTranscript:
    return FinalTranscript(
        session_id="session",
        sentence_id=source_id,
        source_text=" ".join(f"w{source_id}_{index}" for index in range(token_count)),
        translated_text="中文绝不能进入边界判断",
        start_ms=source_id * 100,
        end_ms=source_id * 100 + 90,
    )


class ScriptedDetector:
    provider = "fake"
    model = "deterministic-boundary"

    def __init__(self, decisions: list[BoundaryDecision | Exception]) -> None:
        self.decisions = list(decisions)
        self.calls = []

    async def detect(self, snapshot, *, hard_max_reached):
        self.calls.append((snapshot, hard_max_reached))
        value = self.decisions.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class GatedDetector:
    provider = "fake"
    model = "gated-boundary"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.second_started = asyncio.Event()
        self.second_release = asyncio.Event()
        self.snapshot = None
        self.call_count = 0

    async def detect(self, snapshot, *, hard_max_reached):
        del hard_max_reached
        self.call_count += 1
        if self.call_count > 1:
            self.second_started.set()
            await self.second_release.wait()
            return BoundaryDecision("wait", None, None)
        self.snapshot = snapshot
        self.started.set()
        await self.release.wait()
        return BoundaryDecision("cut", snapshot.sentences[0].source_id, "other")


class PenultimateDetector:
    provider = "fake"
    model = "penultimate-boundary"

    async def detect(self, snapshot, *, hard_max_reached):
        del hard_max_reached
        index = max(0, len(snapshot.sentences) - 2)
        return BoundaryDecision(
            "cut", snapshot.sentences[index].source_id, "completed_explanation"
        )


class WaitDetector:
    provider = "fake"
    model = "wait-boundary"

    def __init__(self) -> None:
        self.call_count = 0

    async def detect(self, snapshot, *, hard_max_reached):
        del snapshot, hard_max_reached
        self.call_count += 1
        return BoundaryDecision("wait", None, None)


class CutSecondDetector:
    provider = "fake"
    model = "cut-second-boundary"

    async def detect(self, snapshot, *, hard_max_reached):
        del hard_max_reached
        return BoundaryDecision(
            "cut", snapshot.sentences[1].source_id, "completed_explanation"
        )


class CutThenWaitDetector(CutSecondDetector):
    def __init__(self) -> None:
        self.call_count = 0

    async def detect(self, snapshot, *, hard_max_reached):
        self.call_count += 1
        if self.call_count > 1:
            return BoundaryDecision("wait", None, None)
        return await super().detect(snapshot, hard_max_reached=hard_max_reached)


class Agent1BoundaryControllerTests(unittest.TestCase):
    def make_controller(self, detector, **overrides):
        values = {
            "check_start_tokens": 900,
            "min_block_tokens": 450,
            "lookahead_reserve_tokens": 180,
            "recheck_growth_tokens": 200,
            "max_tokens": 1_800,
        }
        values.update(overrides)
        return Agent1BoundaryController(
            detector=detector,
            policy=Agent1BoundaryPolicy(**values),
            token_counter=DeterministicTokenCounter(),
        )

    def test_900_check_wait_and_200_token_recheck(self) -> None:
        detector = ScriptedDetector([])
        controller = self.make_controller(detector)
        buffer = TokenBuffer(
            course_id="course",
            lecture_id="lecture",
            session_id="session",
            token_counter=DeterministicTokenCounter(),
        )
        buffer.append(source(1, 899))
        self.assertFalse(
            controller.should_check(controller.snapshot(buffer.snapshot_sentences()))
        )
        buffer.append(source(2, 1))
        snapshot = controller.snapshot(buffer.snapshot_sentences())
        self.assertTrue(controller.should_check(snapshot))
        controller.begin_check()
        controller.finish_wait(snapshot)
        buffer.append(source(3, 199))
        self.assertFalse(
            controller.should_check(controller.snapshot(buffer.snapshot_sentences()))
        )
        buffer.append(source(4, 1))
        self.assertTrue(
            controller.should_check(controller.snapshot(buffer.snapshot_sentences()))
        )

    def test_invalid_stale_cut_is_treated_as_wait(self) -> None:
        detector = ScriptedDetector([])
        controller = self.make_controller(
            detector,
            check_start_tokens=4,
            min_block_tokens=2,
            lookahead_reserve_tokens=1,
            recheck_growth_tokens=2,
            max_tokens=10,
        )
        buffer = TokenBuffer(
            course_id="course",
            lecture_id="lecture",
            session_id="session",
            token_counter=DeterministicTokenCounter(),
        )
        for source_id in range(1, 5):
            buffer.append(source(source_id))
        snapshot = controller.snapshot(buffer.snapshot_sentences())
        controller.begin_check()
        buffer.consume_prefix(1)
        resolution = controller.resolve(
            snapshot,
            buffer.snapshot_sentences(),
            BoundaryDecision("cut", 2, "other"),
        )
        self.assertIsNone(resolution)

    def test_hard_max_wait_timeout_and_invalid_results_all_fallback(self) -> None:
        invalid_results = [
            BoundaryDecision("wait", None, None),
            BoundaryDecision("cut", "missing", "other"),
            RuntimeError("timeout"),
        ]
        for result in invalid_results:
            with self.subTest(result=result):
                detector = ScriptedDetector([result])
                controller = self.make_controller(detector)
                buffer = TokenBuffer(
                    course_id="course",
                    lecture_id="lecture",
                    session_id="session",
                    token_counter=DeterministicTokenCounter(),
                )
                for source_id in range(1, 19):
                    buffer.append(source(source_id, 100))
                snapshot = controller.snapshot(buffer.snapshot_sentences())
                controller.begin_check()
                decision = None
                error = None
                try:
                    decision = asyncio.run(
                        detector.detect(snapshot, hard_max_reached=True)
                    )
                except RuntimeError as exc:  # test provider failure path
                    error = str(exc)
                resolution = controller.resolve(
                    snapshot,
                    buffer.snapshot_sentences(),
                    decision,
                    detector_error=error,
                )
                self.assertIsNotNone(resolution)
                assert resolution is not None
                self.assertTrue(resolution.metadata.boundary_fallback)
                job = buffer.consume_prefix(
                    resolution.cut_after_source_id,
                    boundary_metadata=resolution.metadata,
                )
                self.assertGreater(job.current_chunk_token_count, 0)
                self.assertGreater(buffer.pending_token_count, 0)


class Agent1RollingPipelineTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def durable_pipeline(
        root: Path, detector
    ) -> tuple[Agent1Pipeline, Agent1JobLedger]:
        counter = DeterministicTokenCounter()
        writer = DenseBlockWriter(root / "dense.jsonl")
        ledger = Agent1JobLedger(
            root / "agent1",
            session_id="session",
            prompt_version_id="v1",
            instruction_snapshot={},
        )
        pipeline = Agent1Pipeline(
            token_buffer=TokenBuffer(
                course_id="course",
                lecture_id="lecture",
                session_id="session",
                token_counter=counter,
            ),
            worker=FakeAgent1Worker(writer=writer),
            writer=writer,
            job_ledger=ledger,
            source_log_path=root / "raw_sentences.jsonl",
            boundary_controller=Agent1BoundaryController(
                detector=detector,
                policy=Agent1BoundaryPolicy(
                    check_start_tokens=4,
                    min_block_tokens=1,
                    lookahead_reserve_tokens=1,
                    recheck_growth_tokens=2,
                    max_tokens=10,
                ),
                token_counter=counter,
            ),
        )
        return pipeline, ledger

    async def test_wait_and_suffix_checkpoint_survive_crash_then_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wait_detector = WaitDetector()
            first, _ = self.durable_pipeline(root, wait_detector)
            await first.start()
            writer = ClassroomTranscriptProcessor(
                session_id="session",
                output_path=root / "raw_sentences.jsonl",
                source_final_sink=first.publish_source_final,
            )
            for index in range(1, 5):
                writer._publish_source_final(
                    _PendingTranscript(
                        sentence_id=f"provider-{index}",
                        provider_generation=1,
                        source_text=f"source{index}",
                    )
                )
            writer.close()
            async with asyncio.timeout(1):
                while wait_detector.call_count < 1:
                    await asyncio.sleep(0.001)
            await first.checkpoint()
            state = json.loads((root / "agent1/source_checkpoint.json").read_text())
            self.assertEqual(state["schemaVersion"], 2)
            self.assertEqual(state["boundaryController"]["lastCheckedTokenCount"], 4)
            self.assertEqual(len(state["tokenBuffer"]["sources"]), 4)
            self.assertEqual(list((root / "agent1/jobs").glob("J*.json")), [])
            await first._cancel_tasks()
            first.writer.close()

            second, ledger = self.durable_pipeline(root, CutThenWaitDetector())
            await second.start()
            writer = ClassroomTranscriptProcessor(
                session_id="session",
                output_path=root / "raw_sentences.jsonl",
                source_final_sink=second.publish_source_final,
                connection_generation=2,
            )
            for index in range(5, 7):
                writer._publish_source_final(
                    _PendingTranscript(
                        sentence_id=f"provider-{index}",
                        provider_generation=1,
                        source_text=f"source{index}",
                    )
                )
            writer.close()
            async with asyncio.timeout(1):
                while second.agent1_job_count < 1:
                    await asyncio.sleep(0.001)
            await second.checkpoint()
            self.assertEqual(second.token_buffer.pending_sentence_count, 4)
            await second._cancel_tasks()
            second.writer.close()

            third, ledger = self.durable_pipeline(root, CutSecondDetector())
            await third.start()
            writer = ClassroomTranscriptProcessor(
                session_id="session",
                output_path=root / "raw_sentences.jsonl",
                source_final_sink=third.publish_source_final,
                connection_generation=3,
            )
            writer._publish_source_final(
                _PendingTranscript(
                    sentence_id="provider-7",
                    provider_generation=1,
                    source_text="source7",
                )
            )
            writer.close()
            self.assertTrue(await third.shutdown(timeout_seconds=2))
            jobs = ledger.jobs()
            owned = [source_id for job in jobs for source_id in job.source_sentence_ids]
            self.assertEqual(len(owned), 7)
            self.assertEqual(len(set(owned)), 7)

    async def test_source_ingestion_continues_while_snapshot_is_in_flight(self) -> None:
        detector = GatedDetector()
        counter = DeterministicTokenCounter()
        controller = Agent1BoundaryController(
            detector=detector,
            policy=Agent1BoundaryPolicy(
                check_start_tokens=4,
                min_block_tokens=1,
                lookahead_reserve_tokens=1,
                recheck_growth_tokens=2,
                max_tokens=20,
            ),
            token_counter=counter,
        )
        with tempfile.TemporaryDirectory() as directory:
            writer = DenseBlockWriter(Path(directory) / "dense.jsonl")
            pipeline = Agent1Pipeline(
                token_buffer=TokenBuffer(
                    course_id="course",
                    lecture_id="lecture",
                    session_id="session",
                    token_counter=counter,
                ),
                worker=FakeAgent1Worker(writer=writer),
                writer=writer,
                boundary_controller=controller,
            )
            await pipeline.start()
            for source_id in range(1, 5):
                pipeline.publish_final_transcript(source(source_id))
            await asyncio.wait_for(detector.started.wait(), timeout=1)
            self.assertEqual(detector.snapshot.source_ids, (1, 2, 3, 4))
            for source_id in range(5, 9):
                pipeline.publish_final_transcript(source(source_id))
            async with asyncio.timeout(1):
                while pipeline.token_buffer.pending_sentence_count < 8:
                    await asyncio.sleep(0.001)
            detector.release.set()
            async with asyncio.timeout(1):
                while pipeline.agent1_job_count < 1:
                    await asyncio.sleep(0.001)
            self.assertEqual(
                tuple(
                    item.sentence_id
                    for item in pipeline.token_buffer.snapshot_sentences()
                ),
                tuple(range(2, 9)),
            )
            detector.second_release.set()
            await pipeline.shutdown(timeout_seconds=2)

    async def test_production_and_replay_use_same_boundaries_and_exact_partition(
        self,
    ) -> None:
        sentences = [source(index, 2) for index in range(1, 101)]
        policy = Agent1BoundaryPolicy(
            check_start_tokens=10,
            min_block_tokens=2,
            lookahead_reserve_tokens=2,
            recheck_growth_tokens=4,
            max_tokens=20,
        )
        counter = DeterministicTokenCounter()
        with tempfile.TemporaryDirectory() as directory:
            dense_path = Path(directory) / "dense.jsonl"
            writer = DenseBlockWriter(dense_path)
            pipeline = Agent1Pipeline(
                token_buffer=TokenBuffer(
                    course_id="course",
                    lecture_id="lecture",
                    session_id="session",
                    token_counter=counter,
                    overlap_tokens=2,
                ),
                worker=FakeAgent1Worker(writer=writer),
                writer=writer,
                boundary_controller=Agent1BoundaryController(
                    detector=PenultimateDetector(),
                    policy=policy,
                    token_counter=counter,
                ),
            )
            await pipeline.start()
            for item in sentences:
                pipeline.publish_final_transcript(item)
                await asyncio.sleep(0)
            self.assertTrue(await pipeline.shutdown(timeout_seconds=5))
            production = [
                json.loads(line)["source_sentence_ids"]
                for line in dense_path.read_text(encoding="utf-8").splitlines()
            ]

            replay = await build_replay_jobs(
                sentences,
                ReplayConfig(
                    input_path=Path("unused"),
                    output_dir=Path(directory) / "replay",
                    course_id="course",
                    lecture_id="lecture",
                    session_id="session",
                    boundary_check_start_tokens=policy.check_start_tokens,
                    min_block_tokens=policy.min_block_tokens,
                    lookahead_reserve_tokens=policy.lookahead_reserve_tokens,
                    recheck_growth_tokens=policy.recheck_growth_tokens,
                    max_tokens=policy.max_tokens,
                    overlap_tokens=2,
                ),
                boundary_detector=PenultimateDetector(),
            )
            replay_ranges = [list(job.source_sentence_ids) for job in replay]
            self.assertEqual(production, replay_ranges)
            self.assertEqual(
                [source_id for block in production for source_id in block],
                list(range(1, 101)),
            )


if __name__ == "__main__":
    unittest.main()
