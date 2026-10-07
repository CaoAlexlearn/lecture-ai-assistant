from __future__ import annotations

import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import httpx
from fastapi.testclient import TestClient

from agent1_job_ledger import Agent1JobLedger
from agent1_models import Agent1Job
from agent1_pipeline import Agent1Pipeline
from classroom_app import create_app
from config import Settings
from dense_block_writer import DenseBlockWriter
from fake_agent1 import FakeAgent1Worker
from agent1_source_fixture import ClassroomTranscriptProcessor, _PendingTranscript
from session_runtime import DuplicateRuntimeConnectionError, SessionRuntimeOwner
from token_buffer import DeterministicTokenCounter, TokenBuffer
from transcript import SourceFinalTranscript
from workspace_catalog import WorkspaceCatalog, WorkspaceCatalogError


def settings_for_test(output_path: Path) -> Settings:
    return Settings(
        api_key="test-key",
        workspace_id=None,
        send_workspace_header=False,
        source_language="en",
        target_language="zh",
        debug=False,
        raw_event_debug=False,
        agent1_enabled=False,
        agent1_output_path=output_path,
    )


def create_catalog(root: Path) -> tuple[WorkspaceCatalog, str, str]:
    courses = root / "courses"
    courses.mkdir(parents=True)
    (courses / "index.json").write_text(
        json.dumps({"schema_version": 1, "course_ids": []}), encoding="utf-8"
    )
    catalog = WorkspaceCatalog(root)
    course = catalog.create_course(title="Distributed Systems")
    lecture = catalog.create_lecture(course["id"], number="01", title="Consensus")
    catalog.upload_material(
        course["id"], lecture["id"], filename="slides.pdf", content=b"%PDF-v1"
    )
    prepared = catalog.lecture_root(course["id"], lecture["id"]) / "prepared"
    prepared.mkdir()
    (prepared / "lecture_scaffold.json").write_text("# Consensus", encoding="utf-8")
    (prepared / "lecture_scaffold.md").write_text("# Slides", encoding="utf-8")
    (prepared / "asr_context.txt").write_text("Lecture context", encoding="utf-8")
    (prepared / "hotwords.json").write_text(
        json.dumps({"hotwords": [{"term": "Raft"}]}), encoding="utf-8"
    )
    catalog.mark_prepared(course["id"], lecture["id"])
    return catalog, course["id"], lecture["id"]


def start_session(
    catalog: WorkspaceCatalog, course_id: str, lecture_id: str
) -> dict[str, object]:
    return catalog.start_session(
        course_id,
        lecture_id,
        agent1_prompt_version_id="v1",
        instruction_snapshot={"fingerprint": "instructions-v1"},
        preparation_snapshot={
            "runId": "run-1",
            "manifestSha256": "a" * 64,
            "inputFingerprint": "b" * 64,
            "artifactHashes": {"prepared/hotwords.json": "c" * 64},
            "hotwordsSnapshot": ["Raft"],
        },
    )


class FakeRuntimeAgent1:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.counts = {"pending": 1, "running": 0, "written": 0, "failed": 0}

    async def start(self) -> None:
        self.calls.append("start")

    async def checkpoint(self) -> dict[str, int]:
        self.calls.append("checkpoint")
        return dict(self.counts)

    async def shutdown(self, *, timeout_seconds: float | None) -> bool:
        self.calls.append(f"shutdown:{timeout_seconds}")
        self.counts = {"pending": 0, "running": 0, "written": 1, "failed": 0}
        return True

    def job_status_counts(self) -> dict[str, int]:
        return dict(self.counts)


class FakeRealtimeWorker:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls
        self.stop_requested = asyncio.Event()

    async def stop_when_done(self) -> None:
        self.calls.append("provider_finish")
        self.stop_requested.set()


class BlockingShutdownAgent1(FakeRuntimeAgent1):
    def __init__(self) -> None:
        super().__init__()
        self.shutdown_started = asyncio.Event()
        self.release_shutdown = asyncio.Event()

    async def shutdown(self, *, timeout_seconds: float | None) -> bool:
        self.calls.append(f"shutdown:{timeout_seconds}")
        self.shutdown_started.set()
        await self.release_shutdown.wait()
        self.counts = {"pending": 0, "running": 0, "written": 1, "failed": 0}
        return True


class ClassSessionRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_pause_resume_uses_one_owner_and_new_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog, course_id, lecture_id = create_catalog(Path(directory))
            session = start_session(catalog, course_id, lecture_id)
            agent1 = FakeRuntimeAgent1()
            owner = SessionRuntimeOwner(
                catalog=catalog,
                course_id=course_id,
                lecture_id=lecture_id,
                session_id=str(session["id"]),
                agent1_pipeline=agent1,
            )
            first = await owner.attach()
            worker_calls: list[str] = []
            worker = FakeRealtimeWorker(worker_calls)
            self.assertTrue(
                await owner.bind_connection(
                    first.generation, worker=worker, event_sink=self._ignore_event
                )
            )

            pause_task = asyncio.create_task(owner.pause())
            await asyncio.wait_for(worker.stop_requested.wait(), timeout=1)
            self.assertEqual(
                catalog.get_session(course_id, lecture_id, str(session["id"]))[
                    "runtimeState"
                ],
                "pausing",
            )
            self.assertTrue(
                await owner.finish_connection(first.generation, unexpected=False)
            )
            paused = await asyncio.wait_for(pause_task, timeout=1)

            self.assertEqual(worker_calls, ["provider_finish"])
            self.assertEqual(paused["id"], session["id"])
            self.assertEqual(paused["status"], "paused")
            self.assertEqual(paused["preparationRunId"], "run-1")
            self.assertEqual(paused["hotwordsSnapshot"], ["Raft"])
            self.assertGreaterEqual(agent1.calls.count("checkpoint"), 1)
            self.assertFalse(any(call.startswith("shutdown:") for call in agent1.calls))

            await asyncio.sleep(0.01)
            during_break = catalog.get_session(
                course_id, lecture_id, str(session["id"])
            )
            self.assertEqual(during_break["status"], "paused")
            self.assertEqual(len(catalog.list_sessions(course_id, lecture_id)), 1)

            resumed = await owner.resume()
            second = await owner.attach()
            self.assertEqual(resumed["id"], session["id"])
            self.assertEqual(second.generation, first.generation + 1)
            self.assertFalse(
                await owner.finish_connection(first.generation, unexpected=True)
            )
            current = catalog.get_session(course_id, lecture_id, str(session["id"]))
            self.assertEqual(current["runtimeState"], "connected")
            self.assertEqual(current["connectionGeneration"], second.generation)

    async def test_duplicate_connection_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog, course_id, lecture_id = create_catalog(Path(directory))
            session = start_session(catalog, course_id, lecture_id)
            owner = SessionRuntimeOwner(
                catalog=catalog,
                course_id=course_id,
                lecture_id=lecture_id,
                session_id=str(session["id"]),
                agent1_pipeline=None,
            )
            await owner.attach()
            with self.assertRaises(DuplicateRuntimeConnectionError):
                await owner.attach()

    async def test_network_drop_is_interrupted_and_reconnectable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog, course_id, lecture_id = create_catalog(Path(directory))
            session = start_session(catalog, course_id, lecture_id)
            owner = SessionRuntimeOwner(
                catalog=catalog,
                course_id=course_id,
                lecture_id=lecture_id,
                session_id=str(session["id"]),
                agent1_pipeline=FakeRuntimeAgent1(),
            )
            first = await owner.attach()
            self.assertTrue(
                await owner.finish_connection(first.generation, unexpected=True)
            )
            interrupted = catalog.get_session(
                course_id, lecture_id, str(session["id"])
            )
            self.assertEqual(interrupted["status"], "live")
            self.assertEqual(interrupted["runtimeState"], "interrupted")
            self.assertEqual(interrupted["agent1JobStatus"]["pending"], 1)

            second = await owner.attach()
            self.assertGreater(second.generation, first.generation)
            self.assertEqual(
                catalog.get_session(course_id, lecture_id, str(session["id"]))[
                    "runtimeState"
                ],
                "connected",
            )

    async def test_stop_after_pause_waits_for_agent1_before_completed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog, course_id, lecture_id = create_catalog(Path(directory))
            session = start_session(catalog, course_id, lecture_id)
            agent1 = BlockingShutdownAgent1()
            owner = SessionRuntimeOwner(
                catalog=catalog,
                course_id=course_id,
                lecture_id=lecture_id,
                session_id=str(session["id"]),
                agent1_pipeline=agent1,
            )
            paused = await owner.pause()
            self.assertEqual(paused["status"], "paused")

            stop_task = asyncio.create_task(owner.stop())
            await asyncio.wait_for(agent1.shutdown_started.wait(), timeout=1)
            during_stop = catalog.get_session(
                course_id, lecture_id, str(session["id"])
            )
            self.assertEqual(during_stop["status"], "paused")
            self.assertEqual(during_stop["runtimeState"], "stopping")
            agent1.release_shutdown.set()
            completed = await asyncio.wait_for(stop_task, timeout=1)

            self.assertEqual(completed["status"], "completed")
            self.assertTrue(completed["notesDrained"])
            self.assertEqual(completed["agent1JobStatus"]["written"], 1)

    async def test_pause_returns_while_agent1_job_continues_durably(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, course_id, lecture_id = create_catalog(root)
            session = start_session(catalog, course_id, lecture_id)
            session_id = str(session["id"])
            writer = DenseBlockWriter(root / "dense.jsonl")
            ledger = Agent1JobLedger(
                root / "agent1",
                session_id=session_id,
                prompt_version_id="v1",
                instruction_snapshot={"fingerprint": "instructions-v1"},
            )
            pipeline = Agent1Pipeline(
                token_buffer=TokenBuffer(
                    course_id=course_id,
                    lecture_id=lecture_id,
                    session_id=session_id,
                    token_counter=DeterministicTokenCounter(),
                    token_target=1,
                ),
                worker=FakeAgent1Worker(writer=writer, sleep_seconds=0.08),
                writer=writer,
                job_ledger=ledger,
                queue_budget=1,
            )
            owner = SessionRuntimeOwner(
                catalog=catalog,
                course_id=course_id,
                lecture_id=lecture_id,
                session_id=session_id,
                agent1_pipeline=pipeline,
            )
            await owner.start()
            pipeline.publish_source_final(
                SourceFinalTranscript(
                    session_id=session_id,
                    sentence_id="stable-source",
                    source_text="Agent one keeps working.",
                    start_ms=1,
                    end_ms=2,
                    source_sequence=1,
                )
            )
            await self._wait_until(lambda: ledger.status_counts()["running"] == 1)

            paused = await owner.pause()

            self.assertEqual(paused["status"], "paused")
            self.assertEqual(paused["agent1JobStatus"]["running"], 1)
            await self._wait_until(lambda: ledger.status_counts()["written"] == 1)
            resumed = await owner.resume()
            self.assertEqual(resumed["id"], session_id)
            pipeline.publish_source_final(
                SourceFinalTranscript(
                    session_id=session_id,
                    sentence_id="stable-source-2",
                    source_text="Block sequence continues.",
                    start_ms=3,
                    end_ms=4,
                    source_sequence=2,
                )
            )
            await self._wait_until(lambda: ledger.status_counts()["written"] == 2)
            completed = await owner.stop()
            self.assertEqual(completed["status"], "completed")
            self.assertTrue(completed["notesDrained"])
            self.assertEqual(
                [path.name for path in sorted((root / "agent1/jobs").glob("J*.json"))],
                ["J000001.json", "J000002.json"],
            )

    @staticmethod
    async def _ignore_event(_payload: dict[str, object]) -> None:
        return None

    @staticmethod
    async def _wait_until(predicate, timeout: float = 1.0) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.001)


class Agent1DurabilityTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def job(session_id: str, sequence: int = 1) -> Agent1Job:
        return Agent1Job(
            course_id="course",
            lecture_id="lecture",
            session_id=session_id,
            block_id=f"{session_id}:B{sequence:06d}",
            source_sentence_ids=(f"source-{sequence}",),
            previous_overlap_text="",
            current_chunk_text="Durable source sentence.",
            start_time=100,
            end_time=900,
            current_chunk_token_count=4,
            source_sequence_start=sequence,
            source_sequence_end=sequence,
        )

    async def test_restart_recovers_running_job_and_writes_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_id = "recover-session"
            ledger = Agent1JobLedger(
                root / "agent1",
                session_id=session_id,
                prompt_version_id="v7",
                instruction_snapshot={"fingerprint": "instruction-v3"},
            )
            job = self.job(session_id)
            ledger.create_pending(job)
            ledger.mark_running(job.block_id, 1)

            dense_path = root / "dense.jsonl"
            writer = DenseBlockWriter(dense_path)
            pipeline = Agent1Pipeline(
                token_buffer=TokenBuffer(
                    course_id="course",
                    lecture_id="lecture",
                    session_id=session_id,
                    token_counter=DeterministicTokenCounter(),
                    token_target=10,
                ),
                worker=FakeAgent1Worker(writer=writer),
                writer=writer,
                job_ledger=ledger,
                queue_budget=1,
            )
            await pipeline.start()
            await self._wait_until(lambda: ledger.status_counts()["written"] == 1)
            self.assertTrue(await pipeline.shutdown(timeout_seconds=None))

            record = json.loads(next((root / "agent1/jobs").glob("J*.json")).read_text())
            self.assertEqual(record["status"], "written")
            self.assertEqual(record["sourceSequenceRange"], [1, 1])
            self.assertEqual(record["promptVersionId"], "v7")
            self.assertEqual(len(dense_path.read_text().splitlines()), 1)
            self.assertLessEqual(pipeline.max_agent1_job_backlog, 1)

    async def test_shutdown_timeout_reports_false_then_finishes_same_durable_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_id = "slow-session"
            ledger = Agent1JobLedger(
                root / "agent1",
                session_id=session_id,
                prompt_version_id="v1",
                instruction_snapshot={},
            )
            writer = DenseBlockWriter(root / "dense.jsonl")
            pipeline = Agent1Pipeline(
                token_buffer=TokenBuffer(
                    course_id="course",
                    lecture_id="lecture",
                    session_id=session_id,
                    token_counter=DeterministicTokenCounter(),
                    token_target=1,
                ),
                worker=FakeAgent1Worker(writer=writer, sleep_seconds=0.08),
                writer=writer,
                job_ledger=ledger,
                queue_budget=1,
            )
            await pipeline.start()
            pipeline.publish_source_final(
                SourceFinalTranscript(
                    session_id=session_id,
                    sentence_id="stable-source-1",
                    source_text="one sentence",
                    start_ms=1,
                    end_ms=2,
                    source_sequence=1,
                )
            )

            self.assertFalse(await pipeline.shutdown(timeout_seconds=0.01))
            self.assertTrue(ledger.has_unsettled())
            self.assertTrue(await pipeline.shutdown(timeout_seconds=None))
            self.assertEqual(
                ledger.status_counts(),
                {"pending": 0, "running": 0, "written": 1, "failed": 0},
            )

    async def test_source_final_is_fsynced_before_agent1_callback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "raw_sentences.jsonl"
            observed_records: list[dict[str, object]] = []

            def source_sink(_transcript: SourceFinalTranscript) -> None:
                observed_records.extend(
                    json.loads(line) for line in output.read_text().splitlines()
                )

            processor = ClassroomTranscriptProcessor(
                session_id="source-order",
                output_path=output,
                source_final_sink=source_sink,
                connection_generation=3,
            )
            processor._publish_source_final(
                _PendingTranscript(
                    sentence_id="provider-item-1",
                    provider_generation=4,
                    source_text="This source is authoritative.",
                    start_ms=1,
                    end_ms=2,
                )
            )
            processor.close()

            self.assertEqual(len(observed_records), 1)
            record = observed_records[0]
            self.assertEqual(record["event_type"], "source_final")
            self.assertEqual(record["connection_generation"], 3)
            self.assertEqual(record["provider_generation"], 4)
            self.assertIn(":G000003:P000004:", str(record["source_identity"]))

    @staticmethod
    async def _wait_until(predicate, timeout: float = 1.0) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.001)


class UploadResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_pptx_conversion_does_not_block_health_requests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, course_id, lecture_id = create_catalog(root)
            app = create_app(
                settings_for_test(root / "data"), workspace_catalog=catalog
            )

            def slow_conversion(source: Path, output_dir: Path) -> Path:
                del source
                time.sleep(0.2)
                converted = output_dir / "uploaded.pdf"
                converted.write_bytes(b"%PDF-converted")
                return converted

            transport = httpx.ASGITransport(app=app)
            with mock.patch.object(
                catalog, "_convert_pptx", side_effect=slow_conversion
            ):
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://test"
                ) as client:
                    upload = asyncio.create_task(
                        client.post(
                            f"/api/courses/{course_id}/lectures/{lecture_id}/material",
                            params={"filename": "new.pptx"},
                            content=b"pptx",
                        )
                    )
                    await asyncio.sleep(0.02)
                    started = time.perf_counter()
                    health = await client.get("/health")
                    heartbeat_elapsed = time.perf_counter() - started
                    uploaded = await upload

            self.assertEqual(health.status_code, 200)
            self.assertEqual(uploaded.status_code, 200)
            self.assertLess(heartbeat_elapsed, 0.1)


class PreparationSnapshotTests(unittest.TestCase):
    def test_active_capture_duration_excludes_break_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog, course_id, lecture_id = create_catalog(Path(directory))
            timestamps = iter(
                [
                    "2026-09-14T00:00:00+00:00",
                    "2026-09-14T00:50:00+00:00",
                    "2026-09-14T01:00:00+00:00",
                    "2026-09-14T01:50:00+00:00",
                ]
            )
            with mock.patch("workspace_catalog._now", side_effect=lambda: next(timestamps)):
                session = start_session(catalog, course_id, lecture_id)
                paused = catalog.pause_session(
                    course_id, lecture_id, str(session["id"])
                )
                catalog.resume_session(course_id, lecture_id, str(session["id"]))
                completed = catalog.complete_session(
                    course_id, lecture_id, str(session["id"])
                )

            self.assertEqual(paused["activeCaptureSeconds"], 3_000)
            self.assertEqual(completed["activeCaptureSeconds"], 6_000)
            self.assertEqual(len(completed["pauseIntervals"]), 1)
            self.assertEqual(
                completed["pauseIntervals"][0]["endedAt"],
                "2026-09-14T01:00:00+00:00",
            )

    def test_pdf_replacement_keeps_paused_snapshot_but_blocks_new_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            catalog, course_id, lecture_id = create_catalog(Path(directory))
            first = start_session(catalog, course_id, lecture_id)
            paused = catalog.pause_session(course_id, lecture_id, str(first["id"]))

            catalog.upload_material(
                course_id,
                lecture_id,
                filename="replacement.pdf",
                content=b"%PDF-v2",
            )
            resumed = catalog.resume_session(course_id, lecture_id, str(first["id"]))

            self.assertEqual(resumed["id"], paused["id"])
            self.assertEqual(
                resumed["preparationSnapshot"], first["preparationSnapshot"]
            )
            self.assertEqual(
                catalog.get_lecture(course_id, lecture_id)["preparationStatus"],
                "outdated",
            )
            catalog.complete_session(course_id, lecture_id, str(first["id"]))
            with self.assertRaises(WorkspaceCatalogError):
                start_session(catalog, course_id, lecture_id)

    def test_tampered_committed_preparation_is_rejected_by_session_api(self) -> None:
        from preagent_scaffold import PreAgentRun
        from tests.test_knowledge_scaffold import MockModel, workspace

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            catalog, course_id, lecture_id = create_catalog(root)
            lecture_root = catalog.lecture_root(course_id, lecture_id)
            workspace(lecture_root, 4)
            asyncio.run(PreAgentRun(lecture_root, lecture_id, "Consensus", MockModel()).run())
            catalog.mark_prepared(course_id, lecture_id)
            (lecture_root / "prepared/hotwords.json").write_text("{}", encoding="utf-8")
            client = TestClient(
                create_app(settings_for_test(root / "data"), workspace_catalog=catalog)
            )

            response = client.post(
                f"/api/courses/{course_id}/lectures/{lecture_id}/sessions"
            )

            self.assertEqual(response.status_code, 400)
            self.assertIn("invalid committed preparation", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
