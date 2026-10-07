"""Sentence-safe English-source buffering for Agent 1 jobs."""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any, Protocol

from agent1_models import Agent1BoundaryMetadata, Agent1Job, SentenceId
from transcript import FinalTranscript, SourceFinalTranscript

BufferedTranscript = FinalTranscript | SourceFinalTranscript


class TokenCounter(Protocol):
    """Replaceable tokenizer boundary for the future real Agent 1 model."""

    def count(self, text: str) -> int: ...

    def tail(self, text: str, max_tokens: int) -> str: ...


class DeterministicTokenCounter:
    """Dependency-free deterministic counter for Agent 1 buffering.

    This is deliberately not presented as any model's production tokenizer.
    """

    _token_pattern = re.compile(r"\w+(?:['’-]\w+)*|[^\w\s]", re.UNICODE)

    def count(self, text: str) -> int:
        return sum(1 for _ in self._token_pattern.finditer(text))

    def tail(self, text: str, max_tokens: int) -> str:
        if max_tokens <= 0 or not text:
            return ""
        matches = list(self._token_pattern.finditer(text))
        if len(matches) <= max_tokens:
            return text
        return text[matches[-max_tokens].start() :]


class TokenBuffer:
    """Accumulate complete final sentences and emit source-only Agent1Job blocks."""

    def __init__(
        self,
        *,
        course_id: str,
        lecture_id: str,
        session_id: str,
        token_counter: TokenCounter,
        first_target_tokens: int = 300,
        target_tokens: int = 700,
        max_tokens: int = 1_800,
        first_max_wait_seconds: float = 40.0,
        max_wait_seconds: float = 60.0,
        time_trigger_min_tokens: int = 100,
        overlap_tokens: int = 120,
        clock: Callable[[], float] = time.monotonic,
        token_target: int | None = None,
        initial_block_sequence: int = 0,
    ) -> None:
        if token_target is not None:
            first_target_tokens = token_target
            target_tokens = token_target
        if first_target_tokens <= 0 or target_tokens <= 0 or max_tokens <= 0:
            raise ValueError("token targets must be positive")
        if first_max_wait_seconds <= 0 or max_wait_seconds <= 0:
            raise ValueError("wait times must be positive")
        if time_trigger_min_tokens <= 0:
            raise ValueError("time_trigger_min_tokens must be positive")
        if time_trigger_min_tokens > max_tokens:
            raise ValueError("time_trigger_min_tokens cannot exceed max_tokens")
        if overlap_tokens < 0:
            raise ValueError("overlap_tokens cannot be negative")
        if initial_block_sequence < 0:
            raise ValueError("initial_block_sequence cannot be negative")

        self.course_id = course_id
        self.lecture_id = lecture_id
        self.session_id = session_id
        self.token_counter = token_counter
        self.first_target_tokens = first_target_tokens
        self.target_tokens = target_tokens
        self.max_tokens = max_tokens
        self.first_max_wait_seconds = first_max_wait_seconds
        self.max_wait_seconds = max_wait_seconds
        self.time_trigger_min_tokens = time_trigger_min_tokens
        self.overlap_tokens = overlap_tokens
        self.clock = clock

        self._sentences: list[BufferedTranscript] = []
        self._current_chunk_token_count = 0
        self._previous_overlap_text = ""
        self._block_sequence = initial_block_sequence
        self._buffer_started_at: float | None = None

    def append(self, sentence: BufferedTranscript) -> Agent1Job | None:
        """Append one whole final sentence without making a segmentation decision."""

        if not sentence.source_text:
            return None

        if not self._sentences:
            self._buffer_started_at = self.clock()
        self._sentences.append(sentence)
        current_text = self._current_text()
        # Only English source reaches the TokenCounter. Translation is never read.
        self._current_chunk_token_count = self.token_counter.count(current_text)
        return None

    def seconds_until_time_trigger(self) -> float | None:
        """Return the live timer delay once the buffer is large enough to flush."""

        if (
            self._buffer_started_at is None
            or self._current_chunk_token_count < self.time_trigger_min_tokens
        ):
            return None
        elapsed = self.clock() - self._buffer_started_at
        return max(0.0, self.current_max_wait_seconds - elapsed)

    def flush_for_time_trigger(self) -> Agent1Job | None:
        """Flush only when both the real timer and minimum-size guard are satisfied."""

        remaining = self.seconds_until_time_trigger()
        if remaining is None or remaining > 0:
            return None
        return self.flush()

    def flush(
        self, *, boundary_metadata: Agent1BoundaryMetadata | None = None
    ) -> Agent1Job | None:
        """Emit a non-empty tail block regardless of its token count."""

        if not self._sentences:
            return None
        return self._emit_prefix(
            len(self._sentences), boundary_metadata=boundary_metadata
        )

    def consume_prefix(
        self,
        cut_after_source_id: SentenceId,
        *,
        boundary_metadata: Agent1BoundaryMetadata | None = None,
    ) -> Agent1Job:
        """Emit through one complete source final and retain the ordered suffix."""

        matches = [
            index
            for index, sentence in enumerate(self._sentences)
            if sentence.sentence_id == cut_after_source_id
        ]
        if len(matches) != 1:
            raise ValueError(
                "cut_after_source_id must identify exactly one buffered source"
            )
        return self._emit_prefix(matches[0] + 1, boundary_metadata=boundary_metadata)

    def snapshot_sentences(self) -> tuple[BufferedTranscript, ...]:
        """Return an immutable view suitable for asynchronous boundary detection."""

        return tuple(self._sentences)

    def export_state(self) -> dict[str, Any]:
        """Return the complete state needed to preserve chunk boundaries."""

        age_seconds = (
            max(0.0, self.clock() - self._buffer_started_at)
            if self._buffer_started_at is not None
            else 0.0
        )
        return {
            "schemaVersion": 1,
            "blockSequence": self._block_sequence,
            "previousOverlapText": self._previous_overlap_text,
            "bufferAgeSeconds": age_seconds,
            "currentChunkTokenCount": self._current_chunk_token_count,
            "sources": [self._source_record(item) for item in self._sentences],
        }

    def restore_state(self, state: dict[str, Any]) -> None:
        """Restore a state produced by :meth:`export_state`, or fail closed."""

        if state.get("schemaVersion") != 1:
            raise ValueError("Unsupported TokenBuffer checkpoint schema")
        block_sequence = state.get("blockSequence")
        overlap = state.get("previousOverlapText")
        age_seconds = state.get("bufferAgeSeconds")
        source_records = state.get("sources")
        if (
            not isinstance(block_sequence, int)
            or block_sequence < 0
            or not isinstance(overlap, str)
            or not isinstance(age_seconds, (int, float))
            or age_seconds < 0
            or not isinstance(source_records, list)
        ):
            raise ValueError("Invalid TokenBuffer checkpoint state")
        sources = [self._source_from_record(item) for item in source_records]
        if any(item.session_id != self.session_id for item in sources):
            raise ValueError("TokenBuffer checkpoint belongs to another ClassSession")
        self._block_sequence = block_sequence
        self._previous_overlap_text = overlap
        self._sentences = sources
        current_text = self._current_text()
        self._current_chunk_token_count = self.token_counter.count(current_text)
        expected_count = state.get("currentChunkTokenCount")
        if expected_count != self._current_chunk_token_count:
            raise ValueError(
                "TokenBuffer checkpoint token count does not match its sources"
            )
        self._buffer_started_at = self.clock() - float(age_seconds) if sources else None

    def restore_job_baseline(
        self, *, block_sequence: int, previous_chunk_text: str
    ) -> None:
        """Bootstrap an empty buffer from durable jobs when no checkpoint exists."""

        if block_sequence < 0:
            raise ValueError("block_sequence cannot be negative")
        self.restore_state(
            {
                "schemaVersion": 1,
                "blockSequence": block_sequence,
                "previousOverlapText": self.token_counter.tail(
                    previous_chunk_text, self.overlap_tokens
                ),
                "bufferAgeSeconds": 0.0,
                "currentChunkTokenCount": 0,
                "sources": [],
            }
        )

    @property
    def pending_sentence_count(self) -> int:
        return len(self._sentences)

    @property
    def pending_token_count(self) -> int:
        return self._current_chunk_token_count

    @property
    def current_target_tokens(self) -> int:
        return (
            self.first_target_tokens
            if self._block_sequence == 0
            else self.target_tokens
        )

    @property
    def current_max_wait_seconds(self) -> float:
        return (
            self.first_max_wait_seconds
            if self._block_sequence == 0
            else self.max_wait_seconds
        )

    def _emit_prefix(
        self,
        prefix_count: int,
        *,
        boundary_metadata: Agent1BoundaryMetadata | None,
    ) -> Agent1Job:
        if prefix_count <= 0 or prefix_count > len(self._sentences):
            raise ValueError("prefix_count must select buffered source finals")
        self._block_sequence += 1
        block_id = f"{self.session_id}:B{self._block_sequence:06d}"
        sentences = tuple(self._sentences[:prefix_count])
        current_text = "\n".join(sentence.source_text for sentence in sentences)
        current_token_count = self.token_counter.count(current_text)

        job = Agent1Job(
            course_id=self.course_id,
            lecture_id=self.lecture_id,
            session_id=self.session_id,
            block_id=block_id,
            source_sentence_ids=tuple(sentence.sentence_id for sentence in sentences),
            previous_overlap_text=self._previous_overlap_text,
            current_chunk_text=current_text,
            start_time=self._first_time(sentences),
            end_time=self._last_time(sentences),
            current_chunk_token_count=current_token_count,
            source_sequence_start=self._first_sequence(sentences),
            source_sequence_end=self._last_sequence(sentences),
            boundary_metadata=boundary_metadata,
        )

        self._previous_overlap_text = self.token_counter.tail(
            current_text, self.overlap_tokens
        )
        del self._sentences[:prefix_count]
        remaining_text = self._current_text()
        self._current_chunk_token_count = self.token_counter.count(remaining_text)
        self._buffer_started_at = self.clock() if self._sentences else None
        return job

    @staticmethod
    def _source_record(sentence: BufferedTranscript) -> dict[str, Any]:
        return {
            "sessionId": sentence.session_id,
            "sentenceId": sentence.sentence_id,
            "sourceText": sentence.source_text,
            "startMs": sentence.start_ms,
            "endMs": sentence.end_ms,
            "sourceSequence": (
                sentence.source_sequence
                if isinstance(sentence, SourceFinalTranscript)
                else None
            ),
            "providerItemId": (
                sentence.provider_item_id
                if isinstance(sentence, SourceFinalTranscript)
                else None
            ),
            "connectionGeneration": (
                sentence.connection_generation
                if isinstance(sentence, SourceFinalTranscript)
                else 1
            ),
        }

    @staticmethod
    def _source_from_record(value: object) -> SourceFinalTranscript:
        if not isinstance(value, dict):
            raise TypeError("Invalid TokenBuffer source checkpoint")
        session_id = value.get("sessionId")
        sentence_id = value.get("sentenceId")
        source_text = value.get("sourceText")
        source_sequence = value.get("sourceSequence")
        connection_generation = value.get("connectionGeneration", 1)
        if (
            not isinstance(session_id, str)
            or not isinstance(sentence_id, (str, int))
            or not isinstance(source_text, str)
            or not isinstance(source_sequence, int)
            or source_sequence <= 0
            or not isinstance(connection_generation, int)
            or connection_generation <= 0
        ):
            raise ValueError("Invalid TokenBuffer source checkpoint fields")
        start_ms = value.get("startMs")
        end_ms = value.get("endMs")
        provider_item_id = value.get("providerItemId")
        if start_ms is not None and not isinstance(start_ms, int):
            raise ValueError("Invalid TokenBuffer source start time")
        if end_ms is not None and not isinstance(end_ms, int):
            raise ValueError("Invalid TokenBuffer source end time")
        if provider_item_id is not None and not isinstance(provider_item_id, str):
            raise ValueError("Invalid TokenBuffer provider item ID")
        return SourceFinalTranscript(
            session_id=session_id,
            sentence_id=sentence_id,
            source_text=source_text,
            start_ms=start_ms,
            end_ms=end_ms,
            source_sequence=source_sequence,
            provider_item_id=provider_item_id,
            connection_generation=connection_generation,
        )

    def _current_text(self) -> str:
        return "\n".join(sentence.source_text for sentence in self._sentences)

    @staticmethod
    def _first_time(sentences: tuple[BufferedTranscript, ...]) -> int | None:
        return next(
            (
                sentence.start_ms
                for sentence in sentences
                if sentence.start_ms is not None
            ),
            None,
        )

    @staticmethod
    def _last_time(sentences: tuple[BufferedTranscript, ...]) -> int | None:
        return next(
            (
                sentence.end_ms
                for sentence in reversed(sentences)
                if sentence.end_ms is not None
            ),
            None,
        )

    @staticmethod
    def _first_sequence(sentences: tuple[BufferedTranscript, ...]) -> int | None:
        return next(
            (
                sentence.source_sequence
                for sentence in sentences
                if isinstance(sentence, SourceFinalTranscript)
                and sentence.source_sequence is not None
            ),
            None,
        )

    @staticmethod
    def _last_sequence(sentences: tuple[BufferedTranscript, ...]) -> int | None:
        return next(
            (
                sentence.source_sequence
                for sentence in reversed(sentences)
                if isinstance(sentence, SourceFinalTranscript)
                and sentence.source_sequence is not None
            ),
            None,
        )
