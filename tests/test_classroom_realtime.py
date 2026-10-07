from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar
from unittest import mock

from classroom_realtime import (
    RealtimeTextState,
    RealtimeTranscriptStore,
    build_qwen_vocabulary,
    run_realtime_classroom,
)
from config import Settings
from whisperlivekit.qwen_audio_streaming import build_run_task
from whisperlivekit.timed_objects import FrontData, Segment, Translation


class QwenVocabularyTests(unittest.TestCase):
    def test_preagent_terms_become_request_scoped_instant_vocabulary(self) -> None:
        vocabulary = build_qwen_vocabulary(
            ["Fibonacci", " fibonacci ", "Chinese University of Hong Kong"]
        )
        self.assertEqual(
            vocabulary,
            {"Fibonacci": 2, "Chinese University of Hong Kong": 2},
        )
        task = build_run_task(
            "task",
            model="qwen-audio-3.1-asr-flash-streaming",
            semantic_punctuation=True,
            language="en",
            vocabulary=vocabulary,
        )
        parameters = task["payload"]["parameters"]
        self.assertTrue(parameters["semantic_punctuation_enabled"])
        self.assertEqual(parameters["language_hints"], ["en"])
        self.assertEqual(parameters["vocabulary"], vocabulary)

    def test_preagent_priority_weights_are_capped(self) -> None:
        vocabulary = build_qwen_vocabulary({"Fibonacci": 5, "golden ratio": 3})
        self.assertEqual(vocabulary, {"Fibonacci": 2, "golden ratio": 1})

    def test_legacy_session_short_acronyms_and_formulae_never_reach_asr(self) -> None:
        vocabulary = build_qwen_vocabulary({
            "US": 5, "HK": 5, "APR": 5, "PV": 5, "FV": 5,
            "ARM": 5, "CPI": 5, "HIBOR": 5, "HKMA": 5,
            "i_m": 5, "F/A": 5, "Present Worth ($P_0$)": 5,
            "Continuous Compounding": 3,
        })
        self.assertEqual(
            vocabulary,
            {"HIBOR": 2, "HKMA": 2, "Continuous Compounding": 1},
        )

    def test_parenthetical_acronyms_keep_speakable_expansions_once(self) -> None:
        vocabulary = build_qwen_vocabulary({
            "Domain Name System (DNS)": 3,
            "Domain Name System": 5,
            "NIST (National Institute of Standards and Technology)": 5,
            "value-added networks (VAN)": 3,
            "Present Worth ($P_0$)": 5,
            "PV": 5,
        })
        self.assertEqual(vocabulary, {
            "Domain Name System": 2,
            "National Institute of Standards and Technology": 2,
            "value-added networks": 1,
        })


class RealtimeTranscriptStoreTests(unittest.TestCase):
    def make_store(self, root: Path, generation: int = 1) -> RealtimeTranscriptStore:
        return RealtimeTranscriptStore(
            root,
            course_id="course",
            lecture_id="lecture",
            session_id="session",
            connection_generation=generation,
        )

    def test_committed_prefix_and_replaceable_tail_are_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            first = RealtimeTextState(
                source_committed="Mathematics is",
                source_unstable="the science",
                translation_committed="数学是",
                translation_unstable="一门科学",
            )
            second = RealtimeTextState(
                source_committed="Mathematics is",
                source_unstable="the science of patterns",
                translation_committed="数学是",
                translation_unstable="研究模式的科学",
            )
            store.publish(first, status="active_transcription")
            store.publish(second, status="active_transcription")
            final = store.finalize()
            store.close()

            records = [
                json.loads(line)
                for line in (Path(directory) / "realtime_snapshots.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(len(records), 3)
            self.assertEqual(
                records[0]["source"]["committed"],
                records[1]["source"]["committed"],
            )
            self.assertEqual(
                records[1]["source"]["unstable"], "the science of patterns"
            )
            self.assertEqual(records[2]["status"], "final")
            self.assertEqual(records[2]["revision"], 3)
            self.assertEqual(
                final["source"]["display"], "Mathematics is the science of patterns"
            )
            self.assertEqual(final["translation"]["display"], "数学是研究模式的科学")

    def test_finalize_is_idempotent_and_terminal_revision_is_logged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = self.make_store(root)
            store.publish(
                RealtimeTextState(source_unstable="Hello"),
                status="active_transcription",
            )
            first = store.finalize(status="interrupted")
            second = store.finalize(status="final")
            store.close()

            records = [
                json.loads(line)
                for line in (root / "realtime_snapshots.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            self.assertEqual(first, second)
            self.assertEqual(len(records), 2)
            self.assertEqual(records[-1]["status"], "interrupted")
            self.assertEqual(records[-1]["source"]["display"], "Hello")

    def test_resume_uses_previous_display_as_immutable_base(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = self.make_store(root)
            first.publish(
                RealtimeTextState(source_committed="First", source_unstable="lecture"),
                status="active_transcription",
            )
            first.finalize()
            first.close()

            resumed = self.make_store(root, generation=2)
            self.assertEqual(resumed.base.source_committed, "First lecture")
            line = Segment(start=0, end=1, text="continues", speaker=-1)
            line.translation = "继续"
            state = resumed.state_from_front_data(
                FrontData(lines=[line], buffer_transcription="now")
            )
            self.assertEqual(state.source_committed, "First lecture continues")
            self.assertEqual(state.source_display, "First lecture continues now")
            resumed.close()

    def test_duplicate_state_does_not_create_another_revision(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = self.make_store(Path(directory))
            state = RealtimeTextState(source_unstable="Hello")
            self.assertIsNotNone(
                store.publish(state, status="active_transcription")
            )
            self.assertIsNone(store.publish(state, status="active_transcription"))
            store.close()


class _FakeWebSocket:
    def __init__(self, incoming: list[dict[str, object]]) -> None:
        self.incoming = list(incoming)
        self.sent: list[dict[str, object]] = []

    async def receive(self) -> dict[str, object]:
        await asyncio.sleep(0)
        return self.incoming.pop(0)

    async def send_json(self, payload: dict[str, object]) -> None:
        self.sent.append(payload)


class _FakeRuntimeOwner:
    def __init__(self, generation: int) -> None:
        self.current_generation: int | None = generation
        self.finished: list[tuple[int, bool]] = []

    async def bind_connection(self, generation: int, **_values: object) -> bool:
        return generation == self.current_generation

    async def finish_connection(self, generation: int, *, unexpected: bool) -> bool:
        self.finished.append((generation, unexpected))
        self.current_generation = None
        return True


class _FakeAudioProcessor:
    instances: ClassVar[list[_FakeAudioProcessor]] = []

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.queue: asyncio.Queue[FrontData | None] = asyncio.Queue()
        self.emitted_partial = False
        self.cleaned = False
        self.__class__.instances.append(self)

    async def create_tasks(self):
        async def results():
            while True:
                item = await self.queue.get()
                if item is None:
                    return
                yield item

        return results()

    async def process_audio(self, data: bytes) -> None:
        if data and not self.emitted_partial:
            self.emitted_partial = True
            await self.queue.put(
                FrontData(
                    status="active_transcription",
                    buffer_transcription="Hello",
                    buffer_translation="你好",
                )
            )
            return
        if not data:
            line = Segment(start=0, end=1, text="Hello world.", speaker=-1)
            line.translation = Translation(start=0, end=1, text="你好，世界。")
            await self.queue.put(FrontData(status="active_transcription", lines=[line]))
            await self.queue.put(None)

    async def cleanup(self) -> None:
        self.cleaned = True


class RealtimeProtocolIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, root: Path) -> Settings:
        return Settings(
            api_key="test-key",
            workspace_id=None,
            send_workspace_header=False,
            source_language="en",
            target_language="zh",
            debug=False,
            raw_event_debug=False,
            agent1_enabled=False,
            course_id="course",
            lecture_id="lecture",
            agent1_output_path=root,
        )

    async def test_pcm_to_snapshot_to_clean_eof_is_durable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            websocket = _FakeWebSocket(
                [
                    {"type": "websocket.receive", "bytes": b"\x01\x00"},
                    {"type": "websocket.receive", "bytes": b""},
                ]
            )
            owner = _FakeRuntimeOwner(7)
            _FakeAudioProcessor.instances.clear()
            with mock.patch("classroom_realtime.AudioProcessor", _FakeAudioProcessor):
                await run_realtime_classroom(
                    websocket,
                    self.settings(root),
                    engine=object(),
                    session_id="session",
                    connection_generation=7,
                    hotword_terms={
                        "Fibonacci": 5, "golden ratio": 3,
                        "US": 5, "HK": 5, "APR": 5, "PV": 5,
                    },
                    runtime_owner=owner,
                )

            processor = _FakeAudioProcessor.instances[-1]
            self.assertEqual(
                processor.kwargs["vocabulary"],
                {"Fibonacci": 2, "golden ratio": 1},
            )
            self.assertTrue(processor.cleaned)
            self.assertEqual(owner.finished, [(7, False)])
            self.assertEqual(websocket.sent[0]["type"], "session_started")
            self.assertEqual(websocket.sent[-1]["type"], "session_finished")
            final = json.loads(
                (root / "course" / "lecture" / "session" / "realtime_final.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(final["status"], "final")
            self.assertEqual(final["source"]["display"], "Hello world.")
            self.assertEqual(final["translation"]["display"], "你好，世界。")

    async def test_network_disconnect_is_saved_as_interrupted_and_reconnectable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            websocket = _FakeWebSocket([{"type": "websocket.disconnect"}])
            owner = _FakeRuntimeOwner(2)
            _FakeAudioProcessor.instances.clear()
            with mock.patch("classroom_realtime.AudioProcessor", _FakeAudioProcessor):
                await run_realtime_classroom(
                    websocket,
                    self.settings(root),
                    engine=object(),
                    session_id="session",
                    connection_generation=2,
                    hotword_terms=(),
                    runtime_owner=owner,
                )

            final = json.loads(
                (root / "course" / "lecture" / "session" / "realtime_final.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(final["status"], "interrupted")
            self.assertEqual(owner.finished, [(2, True)])
            self.assertTrue(_FakeAudioProcessor.instances[-1].cleaned)


if __name__ == "__main__":
    unittest.main()
