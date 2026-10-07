"""Application-level transcript contract for realtime classroom consumers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FinalTranscript:
    session_id: str
    sentence_id: int | str
    source_text: str
    translated_text: str
    start_ms: int | None
    end_ms: int | None

    def to_raw_record(self) -> dict[str, object]:
        """Keep the established JSONL field names for offline consumers."""

        return {
            "session_id": self.session_id,
            "sentence_id": self.sentence_id,
            "begin_time": self.start_ms,
            "end_time": self.end_ms,
            "source_text": self.source_text,
            "translated_text": self.translated_text,
            "sentence_end": True,
        }


@dataclass(frozen=True, slots=True)
class SourceFinalTranscript:
    """A finalized source sentence that does not wait for translation."""

    session_id: str
    sentence_id: int | str
    source_text: str
    start_ms: int | None
    end_ms: int | None
    source_sequence: int | None = None
    provider_item_id: str | None = None
    connection_generation: int = 1


FinalTranscriptSink = Callable[[FinalTranscript], None]
SourceFinalTranscriptSink = Callable[[SourceFinalTranscript], None]
