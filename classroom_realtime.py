"""WhisperLiveKit-backed realtime classroom transcription.

This is the canonical realtime path for the classroom application.  It keeps
the browser protocol and durable storage independent from Agent1 so later
agents can consume this state instead of constraining the ASR implementation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from config import Settings
from knowledge_scaffold import is_safe_asr_hotword, normalize_asr_hotword
from whisperlivekit.audio_processor import AudioProcessor
from whisperlivekit.config import WhisperLiveKitConfig
from whisperlivekit.core import TranscriptionEngine
from whisperlivekit.timed_objects import FrontData

logger = logging.getLogger(__name__)

ASR_MODEL = "qwen-audio-3.1-asr-flash-streaming"
TRANSLATION_MODEL = "qwen3.7-flash"
HOTWORD_WEIGHT = 2
MAX_HOTWORDS = 2_000


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def realtime_session_dir(
    output_root: Path, *, course_id: str, lecture_id: str, session_id: str
) -> Path:
    def safe(value: str) -> str:
        normalized = "".join(
            char if char.isalnum() or char in "._-" else "_" for char in value
        ).strip("._")
        return normalized or "unknown"

    return output_root / safe(course_id) / safe(lecture_id) / safe(session_id)


def build_qwen_vocabulary(
    terms: Iterable[str] | Mapping[str, int], *, weight: int = HOTWORD_WEIGHT
) -> dict[str, int]:
    """Build one request-scoped Qwen instant vocabulary from PreAgent terms."""

    if not 1 <= weight <= 5:
        raise ValueError("Qwen hotword weight must be between 1 and 5")
    vocabulary: dict[str, int] = {}
    canonical_terms: dict[str, str] = {}
    entries = terms.items() if isinstance(terms, Mapping) else ((term, weight) for term in terms)
    for term, configured_weight in entries:
        value = normalize_asr_hotword(str(term))
        key = value.casefold()
        if not is_safe_asr_hotword(value):
            continue
        normalized_weight = int(configured_weight)
        if not 1 <= normalized_weight <= 5:
            raise ValueError(
                f"Qwen hotword weight for {value!r} must be between 1 and 5"
            )
        # Existing sessions can carry a frozen 5/3 vocabulary from an older
        # preparation. Apply the current safety policy at the final API edge.
        capped_weight = 1 if normalized_weight == 3 else min(normalized_weight, 2)
        canonical = canonical_terms.get(key)
        if canonical is not None:
            vocabulary[canonical] = max(vocabulary[canonical], capped_weight)
            continue
        canonical_terms[key] = value
        vocabulary[value] = capped_weight
        if len(vocabulary) >= MAX_HOTWORDS:
            logger.warning("PreAgent hotwords truncated to Qwen's %s-term limit", MAX_HOTWORDS)
            break
    return vocabulary


def _append_source(prefix: str, suffix: str) -> str:
    value = (suffix or "").strip()
    if not value:
        return prefix
    if not prefix:
        return value
    if prefix[-1].isspace() or not value[0].isalnum():
        return prefix + value
    return prefix + " " + value


def _append_translation(prefix: str, suffix: str) -> str:
    value = (suffix or "").strip()
    if not value:
        return prefix
    if not prefix:
        return value
    # The configured target is Chinese. Preserve punctuation without injecting
    # the spaces that previously polluted character-token translations.
    return prefix + value


@dataclass(frozen=True, slots=True)
class RealtimeTextState:
    source_committed: str = ""
    source_unstable: str = ""
    translation_committed: str = ""
    translation_unstable: str = ""

    @property
    def source_display(self) -> str:
        return _append_source(self.source_committed, self.source_unstable)

    @property
    def translation_display(self) -> str:
        return _append_translation(
            self.translation_committed, self.translation_unstable
        )


class RealtimeTranscriptStore:
    """Append full state revisions and atomically publish the latest snapshot."""

    def __init__(
        self,
        root: Path,
        *,
        course_id: str,
        lecture_id: str,
        session_id: str,
        connection_generation: int,
    ) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.course_id = course_id
        self.lecture_id = lecture_id
        self.session_id = session_id
        self.connection_generation = connection_generation
        self.snapshots_path = root / "realtime_snapshots.jsonl"
        self.latest_path = root / "realtime_latest.json"
        self.final_path = root / "realtime_final.json"
        self.revision = 0
        self.base = self._load_previous_state()
        self.current = self.base
        self._last_signature: tuple[str, str, str, str] | None = None
        self._file = self.snapshots_path.open("a", encoding="utf-8")
        self._final_record: dict[str, Any] | None = None
        self._closed = False

    def _load_previous_state(self) -> RealtimeTextState:
        for path in (self.latest_path, self.final_path):
            if not path.is_file():
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                self.revision = int(value.get("revision", 0))
                source = value.get("source") or {}
                translation = value.get("translation") or {}
                # A previous generation is already closed, so its last display
                # becomes the immutable base for a resumed connection.
                return RealtimeTextState(
                    source_committed=str(source.get("display") or source.get("committed") or ""),
                    translation_committed=str(
                        translation.get("display") or translation.get("committed") or ""
                    ),
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                logger.warning("Ignoring unreadable realtime state %s", path, exc_info=True)
        return RealtimeTextState()

    def state_from_front_data(self, response: FrontData) -> RealtimeTextState:
        speech_lines = [line for line in response.lines if not line.is_silence()]
        local_source = "".join(str(line.text or "") for line in speech_lines).strip()
        local_translation = "".join(
            str(line.translation or "") for line in speech_lines
        ).strip()
        return RealtimeTextState(
            source_committed=_append_source(self.base.source_committed, local_source),
            source_unstable=str(response.buffer_transcription or "").strip(),
            translation_committed=_append_translation(
                self.base.translation_committed, local_translation
            ),
            translation_unstable=str(response.buffer_translation or "").strip(),
        )

    def publish(self, state: RealtimeTextState, *, status: str) -> dict[str, Any] | None:
        signature = (
            state.source_committed,
            state.source_unstable,
            state.translation_committed,
            state.translation_unstable,
        )
        if signature == self._last_signature:
            return None
        self._last_signature = signature
        self.current = state
        self.revision += 1
        record = self._record(state, status=status)
        self._file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())
        self._write_atomic(self.latest_path, record)
        return record

    def finalize(self, *, status: str = "final") -> dict[str, Any]:
        if self._final_record is not None:
            return self._final_record
        # A terminal transition is itself a durable revision, even when its
        # text is identical to the last live snapshot.  This lets recovery
        # distinguish a clean EOF from a process/network interruption by
        # reading only the append log.
        self.revision += 1
        record = self._record(self.current, status=status)
        self._file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._file.flush()
        os.fsync(self._file.fileno())
        self._write_atomic(self.latest_path, record)
        self._write_atomic(self.final_path, record)
        self._final_record = record
        return record

    def close(self) -> None:
        if not self._closed:
            self._file.close()
            self._closed = True

    def _record(self, state: RealtimeTextState, *, status: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "type": "realtime_snapshot",
            "session_id": self.session_id,
            "course_id": self.course_id,
            "lecture_id": self.lecture_id,
            "connection_generation": self.connection_generation,
            "revision": self.revision,
            "recorded_at": utc_now_iso(),
            "status": status,
            "source": {
                "committed": state.source_committed,
                "unstable": state.source_unstable,
                "display": state.source_display,
            },
            "translation": {
                "committed": state.translation_committed,
                "unstable": state.translation_unstable,
                "display": state.translation_display,
            },
            "models": {
                "asr": ASR_MODEL,
                "translation": TRANSLATION_MODEL,
                "semantic_punctuation": True,
            },
        }

    @staticmethod
    def _write_atomic(path: Path, value: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with temporary.open("xb") as handle:
            handle.write(
                (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        # fsync the directory as well as the file.  Without this, a sudden
        # power loss can theoretically lose the rename despite the file data
        # itself having reached disk.
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            # Some filesystems do not permit directory fsync. Atomic replace
            # still provides the strongest guarantee available there.
            logger.debug("Directory fsync is unavailable for %s", path.parent)


def create_realtime_engine(settings: Settings) -> TranscriptionEngine:
    """Create the process-wide immutable WhisperLiveKit model configuration."""

    config = WhisperLiveKitConfig(
        backend="qwen-audio-streaming",
        backend_policy="localagreement",
        lan=settings.source_language,
        target_language=settings.target_language,
        translation_backend="qwen-api",
        qwen_translation_model=TRANSLATION_MODEL,
        qwen_audio_streaming_model=ASR_MODEL,
        qwen_audio_semantic_punctuation=True,
        qwen_audio_streaming_debug=settings.raw_event_debug,
        qwen_translation_stabilizer_debug=settings.raw_event_debug,
        pcm_input=True,
        vac=False,
        vad=False,
        diarization=False,
        retention_seconds=0,
        log_level="DEBUG" if settings.debug else "INFO",
    )
    return TranscriptionEngine(config=config)


class RealtimeConnectionWorker:
    """Small SessionRuntimeOwner-compatible stop handle."""

    def __init__(self, processor: AudioProcessor) -> None:
        self.processor = processor
        self._stop_lock = asyncio.Lock()
        self._stopped = False

    async def stop_when_done(self) -> None:
        async with self._stop_lock:
            if self._stopped:
                return
            self._stopped = True
            await self.processor.process_audio(b"")


async def run_realtime_classroom(
    websocket: WebSocket,
    settings: Settings,
    *,
    engine: TranscriptionEngine,
    session_id: str,
    connection_generation: int,
    hotword_terms: Iterable[str] | Mapping[str, int],
    runtime_owner: Any,
) -> None:
    """Bridge raw 16 kHz PCM to WhisperLiveKit and emit full text states."""

    vocabulary = build_qwen_vocabulary(hotword_terms)
    output_dir = realtime_session_dir(
        settings.agent1_output_path,
        course_id=settings.course_id,
        lecture_id=settings.lecture_id,
        session_id=session_id,
    )
    processor = AudioProcessor(
        transcription_engine=engine,
        language=settings.source_language,
        target_language=settings.target_language,
        pcm_input=True,
        mode="full",
        vocabulary=vocabulary,
    )
    store = RealtimeTranscriptStore(
        output_dir,
        course_id=settings.course_id,
        lecture_id=settings.lecture_id,
        session_id=session_id,
        connection_generation=connection_generation,
    )
    worker = RealtimeConnectionWorker(processor)
    bound = await runtime_owner.bind_connection(
        connection_generation,
        worker=worker,
        event_sink=lambda _payload: asyncio.sleep(0),
    )
    if not bound:
        store.close()
        await processor.cleanup()
        raise RuntimeError("Realtime connection generation is no longer current")

    client_connected = True

    async def send(payload: dict[str, Any]) -> bool:
        nonlocal client_connected
        if not client_connected:
            return False
        try:
            await websocket.send_json(payload)
            return True
        except (RuntimeError, WebSocketDisconnect):
            client_connected = False
            return False

    await send(
        {
            "type": "session_started",
            "course_id": settings.course_id,
            "lecture_id": settings.lecture_id,
            "session_id": session_id,
            "started_at": utc_now_iso(),
            "connection_generation": connection_generation,
            "hotword_count": len(vocabulary),
            "models": {"asr": ASR_MODEL, "translation": TRANSLATION_MODEL},
        }
    )

    try:
        initial = store.publish(store.base, status="active_transcription")
        if initial is not None:
            await send(_browser_snapshot(initial))
        results = await processor.create_tasks()
    except BaseException:
        try:
            await processor.cleanup()
        finally:
            try:
                store.finalize(status="error")
            finally:
                store.close()
                await runtime_owner.finish_connection(
                    connection_generation, unexpected=True
                )
        raise
    unexpected = True
    processing_failed = False

    async def receive_audio() -> None:
        nonlocal client_connected, unexpected
        try:
            while True:
                message = await websocket.receive()
                message_type = message.get("type")
                if message_type == "websocket.disconnect":
                    client_connected = False
                    return
                data = message.get("bytes")
                if data is not None:
                    if data:
                        await processor.process_audio(data)
                    else:
                        unexpected = False
                        await worker.stop_when_done()
                        return
        except WebSocketDisconnect:
            return

    async def publish_results() -> None:
        nonlocal processing_failed
        async for response in results:
            if response.status == "error":
                await send(
                    {"type": "error", "message": response.error or "Realtime processing failed"}
                )
                processing_failed = True
                raise RuntimeError(response.error or "Realtime processing failed")
            state = store.state_from_front_data(response)
            record = store.publish(state, status=response.status)
            if record is not None:
                await send(_browser_snapshot(record))

    receiver = asyncio.create_task(receive_audio(), name=f"realtime-receive-{session_id}")
    publisher = asyncio.create_task(publish_results(), name=f"realtime-publish-{session_id}")
    raised: BaseException | None = None
    try:
        done, _pending = await asyncio.wait(
            {receiver, publisher}, return_when=asyncio.FIRST_COMPLETED
        )
        if receiver in done:
            await receiver
            if not worker._stopped:
                await worker.stop_when_done()
        elif publisher in done and not worker._stopped:
            # A formatter ending before EOF is a backend failure. Ensure all
            # producer queues are closed before propagating its exception.
            await worker.stop_when_done()
        await publisher
        if receiver not in done:
            # A server-side pause/stop requests EOF through the runtime owner;
            # the browser receiver may still be blocked waiting for a frame.
            unexpected = False
        unexpected = unexpected and runtime_owner.current_generation == connection_generation
        final = store.finalize(status="interrupted" if unexpected else "final")
        await send(_browser_snapshot(final))
        await send({"type": "session_finished", "session_id": session_id})
    except BaseException as exc:
        raised = exc
        processing_failed = processing_failed or not isinstance(exc, asyncio.CancelledError)
        raise
    finally:
        if not receiver.done():
            receiver.cancel()
        if not publisher.done():
            publisher.cancel()
        await asyncio.gather(receiver, publisher, return_exceptions=True)
        try:
            await processor.cleanup()
        finally:
            try:
                if store._final_record is None:
                    terminal_status = "error" if processing_failed else "interrupted"
                    store.finalize(status=terminal_status)
            finally:
                store.close()
                try:
                    await runtime_owner.finish_connection(
                        connection_generation,
                        unexpected=unexpected or raised is not None,
                    )
                except Exception:
                    if raised is None:
                        raise
                    logger.exception("Unable to finish failed realtime connection")


def _browser_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "realtime_snapshot",
        "revision": record["revision"],
        "status": record["status"],
        "source": record["source"],
        "translation": record["translation"],
    }
