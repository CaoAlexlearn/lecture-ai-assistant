"""Alibaba Qwen-Audio streaming ASR over the native DashScope WebSocket API.

This backend is intentionally independent from the ``qwen-api`` HTTP snapshot
adapter.  DashScope's ``result-generated`` event contains a replaceable full
snapshot of the *current sentence* while ``sentence_end`` is false.  It isn't a
delta.  Only a ``sentence_end=true`` event produces an append-only ASR token.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import json
import logging
import os
from queue import Empty, Full, Queue
import threading
from time import monotonic, sleep
from typing import Any, Callable, Deque, Iterable, Optional
import uuid

import numpy as np

from whisperlivekit.qwen_api_network import ensure_direct_connection
from whisperlivekit.timed_objects import ASRToken, Transcript


logger = logging.getLogger(__name__)

DEFAULT_MODEL = "qwen-audio-3.1-asr-flash-streaming"
DEFAULT_URL = "wss://dashscope.aliyuncs.com/api-ws/v1/inference"
SAMPLE_RATE = 16_000
PCM_BYTES_PER_SECOND = SAMPLE_RATE * 2


class ConnectionState(str, Enum):
    CONNECTING = "CONNECTING"
    RUNNING = "RUNNING"
    RECONNECTING = "RECONNECTING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


@dataclass
class StreamingASREvent:
    """Protocol-neutral view of one Qwen sentence snapshot."""

    text: str
    is_final: bool
    sentence_id: str | int | None
    revision: int = 0
    timestamp: float | None = None
    begin_timestamp: float | None = None
    raw_event: dict | None = None


@dataclass(frozen=True)
class StreamingPartialUpdate:
    committed_delta: str
    committed_source: str
    unstable_source: str
    display_source: str
    revision: int
    changed: bool
    is_final: bool
    sentence_id: str | int | None
    timestamp: float | None
    begin_timestamp: float | None
    reason: str = ""


def _append_text(prefix: str, suffix: str) -> tuple[str, str]:
    """Append one sentence while preserving natural inter-sentence spacing."""

    value = (suffix or "").strip()
    if not value:
        return prefix, ""
    delta = value
    if prefix and not prefix[-1].isspace() and value[0].isalnum():
        delta = " " + value
    return prefix + delta, delta


def _normalized_text(value: str) -> str:
    return " ".join((value or "").casefold().split())


class StreamingPartialAdapter:
    """Keep final sentences append-only and the current partial replaceable."""

    def __init__(self, *, debug_trace: bool = False) -> None:
        self.debug_trace = debug_trace
        self.committed_source = ""
        self.unstable_source = ""
        self.revision = 0
        self._current_sentence_id: str | int | None = None
        self._last_signature: tuple[Any, ...] | None = None
        self._seen_finals: Deque[tuple[str, str | int | None, str]] = deque(maxlen=64)
        self._recovering = False

    @property
    def display_source(self) -> str:
        return self.committed_source + self._unstable_with_boundary()

    def _unstable_with_boundary(self) -> str:
        if not self.unstable_source:
            return ""
        if (
            self.committed_source
            and not self.committed_source[-1].isspace()
            and self.unstable_source[0].isalnum()
        ):
            return " " + self.unstable_source
        return self.unstable_source

    def observe(self, event: StreamingASREvent) -> StreamingPartialUpdate:
        raw = event.raw_event or {}
        task_id = str((raw.get("header") or {}).get("task_id") or "")
        text = (event.text or "").strip()
        signature = (task_id, event.sentence_id, text, bool(event.is_final))
        if signature == self._last_signature:
            return self._update(event, changed=False, reason="duplicate-event")

        if not event.is_final:
            if not text:
                return self._update(event, changed=False, reason="empty-partial")
            if (
                self._current_sentence_id == event.sentence_id
                and self.unstable_source == text
            ):
                self._last_signature = signature
                return self._update(event, changed=False, reason="duplicate-partial")
            self._current_sentence_id = event.sentence_id
            self.unstable_source = text
            self._last_signature = signature
            self.revision += 1
            event.revision = self.revision
            update = self._update(event, changed=True, reason="partial-snapshot-replace")
            self._trace("PARTIAL", update)
            return update

        final_key = (task_id, event.sentence_id, _normalized_text(text))
        if final_key in self._seen_finals:
            self._last_signature = signature
            return self._update(event, changed=False, reason="duplicate-final")

        # A fresh task starts sentence ids at one again.  During reconnect only,
        # suppress an exact replay of the most recently committed sentence.  The
        # transport also replays only audio since the last server sentence_end,
        # so this is a final defensive guard rather than general text dedupe.
        if self._recovering and text:
            recent_text = self._seen_finals[-1][2] if self._seen_finals else ""
            if recent_text and recent_text == _normalized_text(text):
                self.unstable_source = ""
                self._current_sentence_id = None
                self._last_signature = signature
                self._recovering = False
                return self._update(event, changed=False, reason="reconnect-final-replay")

        authoritative = text or self.unstable_source
        self.committed_source, delta = _append_text(
            self.committed_source,
            authoritative,
        )
        self.unstable_source = ""
        self._current_sentence_id = None
        self._last_signature = signature
        self._seen_finals.append((task_id, event.sentence_id, _normalized_text(authoritative)))
        self._recovering = False
        if delta:
            self.revision += 1
        event.revision = self.revision
        update = self._update(
            event,
            changed=bool(delta),
            committed_delta=delta,
            reason="sentence-end-final",
        )
        self._trace("SENTENCE END", update)
        return update

    def reconnect(self) -> StreamingPartialUpdate:
        changed = bool(self.unstable_source)
        self.unstable_source = ""
        self._current_sentence_id = None
        self._last_signature = None
        self._recovering = True
        if changed:
            self.revision += 1
        event = StreamingASREvent("", False, None, revision=self.revision)
        update = self._update(event, changed=changed, reason="reconnect-clear-partial")
        self._trace("RECONNECT", update)
        return update

    def flush(self, *, timestamp: float | None = None) -> StreamingPartialUpdate:
        event = StreamingASREvent(
            text=self.unstable_source,
            is_final=True,
            sentence_id=self._current_sentence_id,
            timestamp=timestamp,
        )
        if not self.unstable_source:
            return self._update(event, changed=False, reason="empty-flush")
        self.committed_source, delta = _append_text(
            self.committed_source,
            self.unstable_source,
        )
        self.unstable_source = ""
        self._current_sentence_id = None
        self._last_signature = None
        self.revision += 1
        event.revision = self.revision
        update = self._update(
            event,
            changed=True,
            committed_delta=delta,
            reason="eof-flush",
        )
        self._trace("FINAL FLUSH", update)
        return update

    def _update(
        self,
        event: StreamingASREvent,
        *,
        changed: bool,
        committed_delta: str = "",
        reason: str,
    ) -> StreamingPartialUpdate:
        return StreamingPartialUpdate(
            committed_delta=committed_delta,
            committed_source=self.committed_source,
            unstable_source=self._unstable_with_boundary(),
            display_source=self.display_source,
            revision=self.revision,
            changed=changed,
            is_final=event.is_final,
            sentence_id=event.sentence_id,
            timestamp=event.timestamp,
            begin_timestamp=event.begin_timestamp,
            reason=reason,
        )

    def _trace(self, label: str, update: StreamingPartialUpdate) -> None:
        if not self.debug_trace:
            return
        logger.info(
            "[%s] text=%r text_state=%s [SOURCE REVISION]=%s "
            "[COMMITTED SOURCE]=%r [UNSTABLE SOURCE]=%r reason=%s",
            label,
            update.committed_delta or update.unstable_source,
            "final" if update.is_final else "partial",
            update.revision,
            update.committed_source,
            update.unstable_source,
            update.reason,
        )


@dataclass(frozen=True)
class _AudioFrame:
    data: bytes
    stream_end_s: float
    queued_at: float = field(default_factory=monotonic)

    @property
    def duration_s(self) -> float:
        return len(self.data) / PCM_BYTES_PER_SECOND


@dataclass(frozen=True)
class _ReconnectMarker:
    reason: str


_FINISH = object()
_CLOSE = object()


@dataclass
class QwenAudioStreamingMetrics:
    partial_latencies_ms: Deque[float] = field(default_factory=lambda: deque(maxlen=4096))
    sentence_end_latencies_ms: Deque[float] = field(default_factory=lambda: deque(maxlen=4096))
    audio_queue_latencies_ms: Deque[float] = field(default_factory=lambda: deque(maxlen=4096))
    reconnect_count: int = 0
    dropped_audio_frames: int = 0
    sent_audio_frames: int = 0
    received_partials: int = 0
    received_finals: int = 0
    partial_events_without_timestamp: int = 0
    sentence_end_events_without_timestamp: int = 0

    @staticmethod
    def _summary(values: Iterable[float]) -> dict[str, float | int]:
        items = sorted(values)
        if not items:
            return {"count": 0, "avg": 0.0, "p95": 0.0}
        index = min(len(items) - 1, int(len(items) * 0.95))
        return {
            "count": len(items),
            "avg": round(sum(items) / len(items), 2),
            "p95": round(items[index], 2),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "partial_latency_ms": self._summary(self.partial_latencies_ms),
            "sentence_end_latency_ms": self._summary(self.sentence_end_latencies_ms),
            "audio_queue_latency_ms": self._summary(self.audio_queue_latencies_ms),
            "reconnect_count": self.reconnect_count,
            "dropped_audio_frames": self.dropped_audio_frames,
            "sent_audio_frames": self.sent_audio_frames,
            "received_partials": self.received_partials,
            "received_finals": self.received_finals,
            "partial_events_without_timestamp": (
                self.partial_events_without_timestamp
            ),
            "sentence_end_events_without_timestamp": (
                self.sentence_end_events_without_timestamp
            ),
        }


def build_run_task(
    task_id: str,
    *,
    model: str,
    semantic_punctuation: bool,
    language: Optional[str],
    vocabulary: Optional[dict[str, int]] = None,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "sample_rate": SAMPLE_RATE,
        "format": "pcm",
        "semantic_punctuation_enabled": bool(semantic_punctuation),
        # Continuous microphone silence should not cause an idle disconnect.
        "heartbeat": True,
    }
    if language and language != "auto":
        parameters["language_hints"] = [language]
    if vocabulary:
        parameters["vocabulary"] = dict(vocabulary)
    return {
        "header": {
            "action": "run-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {
            "task_group": "audio",
            "task": "asr",
            "function": "recognition",
            "model": model,
            "parameters": parameters,
            "input": {},
        },
    }


def build_finish_task(task_id: str) -> dict[str, Any]:
    return {
        "header": {
            "action": "finish-task",
            "task_id": task_id,
            "streaming": "duplex",
        },
        "payload": {"input": {}},
    }


def parse_server_event(
    raw: dict[str, Any],
    *,
    task_audio_offset_s: float = 0.0,
) -> StreamingASREvent | None:
    """Parse a result-generated event; heartbeat/control events return None."""

    header = raw.get("header") or {}
    if header.get("event") != "result-generated":
        return None
    sentence = (((raw.get("payload") or {}).get("output") or {}).get("sentence") or {})
    if sentence.get("heartbeat"):
        return None
    begin_ms = sentence.get("begin_time")
    end_ms = sentence.get("end_time")
    return StreamingASREvent(
        text=str(sentence.get("text") or ""),
        is_final=bool(sentence.get("sentence_end")),
        sentence_id=sentence.get("sentence_id"),
        timestamp=(
            task_audio_offset_s + float(end_ms) / 1000.0
            if end_ms is not None
            else None
        ),
        begin_timestamp=(
            task_audio_offset_s + float(begin_ms) / 1000.0
            if begin_ms is not None
            else None
        ),
        raw_event=raw,
    )


class QwenAudioWebSocketSession:
    """Threaded WebSocket transport with an independent bounded audio queue."""

    def __init__(
        self,
        *,
        url: str,
        api_key: Optional[str],
        model: str,
        semantic_punctuation: bool,
        language: Optional[str],
        vocabulary: Optional[dict[str, int]] = None,
        chunk_ms: int,
        debug_trace: bool = False,
        connector: Optional[Callable[..., Any]] = None,
        max_reconnects: int = 3,
        queue_seconds: float = 30.0,
        replay_seconds: float = 2.0,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.model = model
        self.semantic_punctuation = semantic_punctuation
        self.language = language
        self.vocabulary = dict(vocabulary or {})
        self.chunk_ms = chunk_ms
        self.debug_trace = debug_trace
        self.max_reconnects = max_reconnects
        self.metrics = QwenAudioStreamingMetrics()
        self._connector = connector or self._default_connector
        max_frames = max(4, int(queue_seconds * 1000 / chunk_ms))
        self._audio_queue: Queue[Any] = Queue(maxsize=max_frames)
        self._events: Queue[Any] = Queue()
        self._replay: Deque[_AudioFrame] = deque()
        self._replay_bytes_limit = int(replay_seconds * PCM_BYTES_PER_SECOND)
        self._replay_bytes = 0
        self._thread: Optional[threading.Thread] = None
        self._active_websocket: Any = None
        self._force_close = threading.Event()
        self._finished = threading.Event()
        self._error: Optional[BaseException] = None
        self._state = ConnectionState.STOPPED
        self._state_lock = threading.Lock()
        self._wall_started = monotonic()
        ensure_direct_connection(url)

    @property
    def state(self) -> ConnectionState:
        with self._state_lock:
            return self._state

    @property
    def error(self) -> Optional[BaseException]:
        return self._error

    @property
    def queue_depth(self) -> int:
        return self._audio_queue.qsize()

    @property
    def queue_duration_ms(self) -> int:
        return self.queue_depth * self.chunk_ms

    def _set_state(self, state: ConnectionState) -> None:
        with self._state_lock:
            self._state = state
        if self.debug_trace:
            logger.info("[QWEN AUDIO CONNECTION] state=%s", state.value)

    def _default_connector(self, url: str, **kwargs: Any) -> Any:
        from websockets.sync.client import connect

        return connect(url, proxy=None, **kwargs)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if not self.api_key:
            raise ValueError(
                "DASHSCOPE_API_KEY is not configured for qwen-audio-streaming."
            )
        self._thread = threading.Thread(
            target=self._run,
            name="qwen-audio-streaming",
            daemon=True,
        )
        self._thread.start()

    def send_audio(self, data: bytes, stream_end_s: float) -> None:
        if not data:
            return
        if self._error:
            raise RuntimeError(str(self._error)) from self._error
        try:
            self._audio_queue.put_nowait(_AudioFrame(data, stream_end_s))
        except Full as exc:
            self.metrics.dropped_audio_frames += 1
            raise RuntimeError(
                "Qwen Audio streaming queue is full; refusing to silently drop audio."
            ) from exc

    def drain_events(self) -> list[StreamingASREvent | _ReconnectMarker]:
        events: list[StreamingASREvent | _ReconnectMarker] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except Empty:
                return events

    def finish(self, timeout: float = 15.0) -> None:
        if not self._thread:
            return
        try:
            self._audio_queue.put(_FINISH, timeout=1.0)
        except Full:
            self.metrics.dropped_audio_frames += 1
            self._force_close.set()
        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            self._force_close.set()
            self._close_active_websocket()
            self._thread.join(timeout=2.0)
        if self._error:
            raise RuntimeError(str(self._error)) from self._error

    def close(self) -> None:
        self._force_close.set()
        self._close_active_websocket()
        try:
            self._audio_queue.put_nowait(_CLOSE)
        except Full:
            pass
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def _close_active_websocket(self) -> None:
        websocket = self._active_websocket
        close = getattr(websocket, "close", None)
        if close is None:
            return
        try:
            close()
        except Exception:
            logger.debug("Failed to close Qwen Audio WebSocket", exc_info=True)

    def _remember_for_replay(self, frame: _AudioFrame) -> None:
        self._replay.append(frame)
        self._replay_bytes += len(frame.data)
        while self._replay and self._replay_bytes > self._replay_bytes_limit:
            removed = self._replay.popleft()
            self._replay_bytes -= len(removed.data)

    def _clear_replay(self) -> None:
        self._replay.clear()
        self._replay_bytes = 0

    def _run(self) -> None:
        reconnects = 0
        first_connection = True
        try:
            while not self._force_close.is_set():
                self._set_state(
                    ConnectionState.CONNECTING if first_connection
                    else ConnectionState.RECONNECTING
                )
                if not first_connection:
                    self.metrics.reconnect_count += 1
                    if self.debug_trace:
                        logger.info(
                            "[NETWORK] reconnect_count=%s replay_audio_ms=%.1f",
                            self.metrics.reconnect_count,
                            self._replay_bytes / PCM_BYTES_PER_SECOND * 1000.0,
                        )
                    self._events.put(_ReconnectMarker("websocket-reconnect"))
                try:
                    should_stop = self._run_one_connection(replay=not first_connection)
                    if should_stop:
                        self._set_state(ConnectionState.STOPPED)
                        return
                    raise RuntimeError("Qwen Audio task ended unexpectedly")
                except Exception as exc:
                    if self._force_close.is_set():
                        self._set_state(ConnectionState.STOPPED)
                        return
                    reconnects += 1
                    if reconnects > self.max_reconnects:
                        self._error = exc
                        self._set_state(ConnectionState.ERROR)
                        logger.error(
                            "[QWEN AUDIO CONNECTION] reconnect exhausted: %s",
                            exc,
                        )
                        return
                    logger.warning(
                        "[QWEN AUDIO CONNECTION] %s; reconnecting (%s/%s)",
                        exc,
                        reconnects,
                        self.max_reconnects,
                    )
                    sleep(min(2.0, 0.25 * (2 ** (reconnects - 1))))
                    first_connection = False
        finally:
            self._finished.set()

    def _run_one_connection(self, *, replay: bool) -> bool:
        task_id = uuid.uuid4().hex
        headers = {"Authorization": f"Bearer {self.api_key}"}
        with self._connector(
            self.url,
            additional_headers=headers,
            open_timeout=10,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=4 * 1024 * 1024,
        ) as websocket:
            self._active_websocket = websocket
            websocket.send(json.dumps(build_run_task(
                task_id,
                model=self.model,
                semantic_punctuation=self.semantic_punctuation,
                language=self.language,
                vocabulary=self.vocabulary,
            )))
            self._wait_for_task_started(websocket, task_id)
            self._set_state(ConnectionState.RUNNING)

            task_audio_offset_s: Optional[float] = None
            if replay and self._replay:
                replay_frames = list(self._replay)
                task_audio_offset_s = (
                    replay_frames[0].stream_end_s - replay_frames[0].duration_s
                )
                for frame in replay_frames:
                    websocket.send(frame.data)

            while not self._force_close.is_set():
                item: Any = None
                try:
                    item = self._audio_queue.get(timeout=0.02)
                except Empty:
                    pass

                if isinstance(item, _AudioFrame):
                    if task_audio_offset_s is None:
                        task_audio_offset_s = item.stream_end_s - item.duration_s
                    websocket.send(item.data)
                    self.metrics.sent_audio_frames += 1
                    self.metrics.audio_queue_latencies_ms.append(
                        (monotonic() - item.queued_at) * 1000.0
                    )
                    self._remember_for_replay(item)
                    if self.debug_trace:
                        logger.info(
                            "[AUDIO QUEUE] depth=%s duration_ms=%s "
                            "[NETWORK] send_latency_ms=%.1f",
                            self.queue_depth,
                            self.queue_duration_ms,
                            self.metrics.audio_queue_latencies_ms[-1],
                        )
                elif item is _FINISH:
                    self._set_state(ConnectionState.STOPPING)
                    websocket.send(json.dumps(build_finish_task(task_id)))
                    return self._wait_for_task_finished(
                        websocket,
                        task_audio_offset_s or 0.0,
                    )
                elif item is _CLOSE:
                    return True

                self._receive_once(websocket, task_audio_offset_s or 0.0)
            return True

    def _wait_for_task_started(self, websocket: Any, task_id: str) -> None:
        deadline = monotonic() + 10.0
        while monotonic() < deadline:
            raw = json.loads(websocket.recv(timeout=max(0.01, deadline - monotonic())))
            header = raw.get("header") or {}
            event = header.get("event")
            if self.debug_trace:
                logger.info("[QWEN AUDIO EVENT] event=%s", event)
            if event == "task-started":
                if str(header.get("task_id")) != task_id:
                    raise RuntimeError("DashScope task-started task_id mismatch")
                return
            if event == "task-failed":
                raise RuntimeError(self._task_failure_message(raw))
        raise TimeoutError("Timed out waiting for DashScope task-started")

    def _receive_once(self, websocket: Any, task_audio_offset_s: float) -> None:
        try:
            message = websocket.recv(timeout=0.001)
        except TimeoutError:
            return
        if message is None:
            raise ConnectionError("DashScope WebSocket closed")
        raw = json.loads(message)
        event_name = (raw.get("header") or {}).get("event")
        if self.debug_trace:
            logger.info("[QWEN AUDIO EVENT] event=%s", event_name)
        if event_name == "task-failed":
            raise RuntimeError(self._task_failure_message(raw))
        event = parse_server_event(raw, task_audio_offset_s=task_audio_offset_s)
        if event is None:
            return
        self._publish_event(event)

    def _publish_event(self, event: StreamingASREvent) -> None:
        latency: float | None = None
        if event.timestamp is not None:
            elapsed_ms = (monotonic() - self._wall_started) * 1000.0
            audio_end_ms = event.timestamp * 1000.0
            latency = max(0.0, elapsed_ms - audio_end_ms)
        if event.is_final:
            self.metrics.received_finals += 1
            if latency is None:
                self.metrics.sentence_end_events_without_timestamp += 1
            else:
                self.metrics.sentence_end_latencies_ms.append(latency)
            self._clear_replay()
        else:
            self.metrics.received_partials += 1
            if latency is None:
                self.metrics.partial_events_without_timestamp += 1
            else:
                self.metrics.partial_latencies_ms.append(latency)
        self._events.put(event)

    def _wait_for_task_finished(self, websocket: Any, task_audio_offset_s: float) -> bool:
        deadline = monotonic() + 12.0
        while monotonic() < deadline and not self._force_close.is_set():
            try:
                message = websocket.recv(timeout=max(0.01, deadline - monotonic()))
            except TimeoutError:
                break
            if message is None:
                break
            raw = json.loads(message)
            event_name = (raw.get("header") or {}).get("event")
            if self.debug_trace:
                logger.info("[QWEN AUDIO EVENT] event=%s", event_name)
            if event_name == "task-finished":
                return True
            if event_name == "task-failed":
                raise RuntimeError(self._task_failure_message(raw))
            event = parse_server_event(raw, task_audio_offset_s=task_audio_offset_s)
            if event is not None:
                self._publish_event(event)
        raise TimeoutError("Timed out waiting for DashScope task-finished")

    @staticmethod
    def _task_failure_message(raw: dict[str, Any]) -> str:
        header = raw.get("header") or {}
        return str(
            header.get("error_message")
            or header.get("error_code")
            or "DashScope Qwen Audio task failed"
        )


class QwenAudioStreamingASR:
    """Shared immutable configuration; every browser stream gets a new session."""

    sep = ""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        url: Optional[str] = None,
        semantic_punctuation: bool = True,
        chunk_ms: int = 100,
        api_key: Optional[str] = None,
        debug_trace: bool = False,
    ) -> None:
        self.model = model
        self.url = (
            url
            or os.getenv("DASHSCOPE_WEBSOCKET_URL")
            or DEFAULT_URL
        )
        self.semantic_punctuation = bool(semantic_punctuation)
        self.chunk_ms = int(chunk_ms)
        self.api_key = api_key or os.getenv("DASHSCOPE_API_KEY")
        self.debug_trace = debug_trace
        self.backend_choice = "qwen-audio-streaming"

    def new_session(
        self,
        language: Optional[str] = None,
        *,
        vocabulary: Optional[dict[str, int]] = None,
        transport_factory: Optional[Callable[..., Any]] = None,
    ) -> "QwenAudioStreamingOnlineProcessor":
        return QwenAudioStreamingOnlineProcessor(
            self,
            language=language,
            vocabulary=vocabulary,
            transport_factory=transport_factory,
        )


class QwenAudioStreamingOnlineProcessor:
    """WhisperLiveKit online-processor adapter for Qwen sentence snapshots."""

    SAMPLING_RATE = SAMPLE_RATE
    server_endpointing = True
    realtime_audio_streaming = True

    def __init__(
        self,
        asr: QwenAudioStreamingASR,
        *,
        language: Optional[str] = None,
        vocabulary: Optional[dict[str, int]] = None,
        transport_factory: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.asr = asr
        self.language = None if language in (None, "auto") else language
        self.vocabulary = dict(vocabulary or {})
        self.audio_buffer = np.array([], dtype=np.float32)
        self.adapter = StreamingPartialAdapter(debug_trace=asr.debug_trace)
        self._stream_end = 0.0
        self._pcm_pending = bytearray()
        self._last_commit_end = 0.0
        factory = transport_factory or QwenAudioWebSocketSession
        self.transport = factory(
            url=asr.url,
            api_key=asr.api_key,
            model=asr.model,
            semantic_punctuation=asr.semantic_punctuation,
            language=self.language,
            vocabulary=self.vocabulary,
            chunk_ms=asr.chunk_ms,
            debug_trace=asr.debug_trace,
        )
        self.transport.start()

    @property
    def frame_bytes(self) -> int:
        return int(PCM_BYTES_PER_SECOND * self.asr.chunk_ms / 1000)

    def insert_audio_chunk(
        self,
        audio: np.ndarray,
        audio_stream_end_time: Optional[float] = None,
    ) -> None:
        if audio is None or not len(audio):
            return
        samples = np.asarray(audio, dtype=np.float32)
        pcm = np.clip(samples, -1.0, 1.0)
        pcm = (pcm * 32767.0).astype("<i2", copy=False).tobytes()
        self._pcm_pending.extend(pcm)
        if audio_stream_end_time is None:
            self._stream_end += len(samples) / SAMPLE_RATE
        else:
            self._stream_end = float(audio_stream_end_time)

        pending_duration = len(self._pcm_pending) / PCM_BYTES_PER_SECOND
        pending_start = self._stream_end - pending_duration
        sent_bytes = 0
        while len(self._pcm_pending) >= self.frame_bytes:
            frame = bytes(self._pcm_pending[: self.frame_bytes])
            del self._pcm_pending[: self.frame_bytes]
            sent_bytes += len(frame)
            frame_end = pending_start + sent_bytes / PCM_BYTES_PER_SECOND
            self.transport.send_audio(frame, frame_end)

    def process_iter(self) -> tuple[list[ASRToken], float]:
        tokens: list[ASRToken] = []
        for event in self.transport.drain_events():
            if isinstance(event, _ReconnectMarker):
                self.adapter.reconnect()
                continue
            update = self.adapter.observe(event)
            if not update.changed or not update.committed_delta:
                continue
            start = (
                update.begin_timestamp
                if update.begin_timestamp is not None
                else self._last_commit_end
            )
            end = update.timestamp if update.timestamp is not None else self._stream_end
            start = max(self._last_commit_end, min(float(start), float(end)))
            end = max(start, float(end))
            tokens.append(ASRToken(
                start=start,
                end=end,
                text=update.committed_delta,
                detected_language=self.language,
                sentence_end=True,
                source_revision=update.revision,
                source_event_received_at=monotonic(),
            ))
            self._last_commit_end = end
        error = getattr(self.transport, "error", None)
        if error:
            raise RuntimeError(f"Qwen Audio streaming failed: {error}") from error
        return tokens, self._stream_end

    def get_buffer(self) -> Transcript:
        text = self.adapter.unstable_source
        if not text:
            return Transcript(start=None, end=None, text="")
        return Transcript(
            start=self._last_commit_end,
            end=self._stream_end,
            text=self.adapter._unstable_with_boundary(),
        )

    def start_silence(self) -> tuple[list[ASRToken], float]:
        # Local silence is intentionally not a hard endpoint for this backend.
        return self.process_iter()

    def end_silence(self, silence_duration: Optional[float], offset: float) -> None:
        return None

    def new_speaker(self, change_speaker: Any) -> tuple[list[ASRToken], float]:
        return self.process_iter()

    def finish(self) -> tuple[list[ASRToken], float]:
        if self._pcm_pending:
            duration = len(self._pcm_pending) / PCM_BYTES_PER_SECOND
            self.transport.send_audio(bytes(self._pcm_pending), self._stream_end)
            self._pcm_pending.clear()
            if duration <= 0:
                return [], self._stream_end
        self.transport.finish()
        tokens, _ = self.process_iter()
        if self.adapter.unstable_source:
            update = self.adapter.flush(timestamp=self._stream_end)
            if update.committed_delta:
                tokens.append(ASRToken(
                    start=self._last_commit_end,
                    end=self._stream_end,
                    text=update.committed_delta,
                    detected_language=self.language,
                    sentence_end=True,
                    source_revision=update.revision,
                    source_event_received_at=monotonic(),
                ))
                self._last_commit_end = self._stream_end
        logger.info("QWEN_AUDIO_STREAMING_METRICS %s", self.transport.metrics.to_dict())
        return tokens, self._stream_end

    def close(self) -> None:
        self.transport.close()
