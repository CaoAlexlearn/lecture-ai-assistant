"""Legacy Agent1 durability fixture, detached from the realtime runtime.

Agent1 is intentionally not connected in the current phase.  Its recovery
tests still need a deterministic source-final log producer; keeping that tiny
fixture here avoids retaining the removed Pipecat/Qwen realtime subsystem.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from transcript import SourceFinalTranscript, SourceFinalTranscriptSink


@dataclass(slots=True)
class PendingSourceFixture:
    sentence_id: str
    provider_generation: int = 1
    source_text: str = ""
    start_ms: int | None = None
    end_ms: int | None = None
    source_sequence: int | None = None
    source_identity: str = ""
    source_emitted: bool = False


class Agent1SourceFixtureWriter:
    def __init__(
        self,
        *,
        session_id: str,
        output_path: Path,
        source_final_sink: SourceFinalTranscriptSink | None = None,
        connection_generation: int = 1,
        **_unused: object,
    ) -> None:
        self.session_id = session_id
        self.output_path = output_path
        self.source_final_sink = source_final_sink
        self.connection_generation = connection_generation
        self._output_file = None
        self._source_sequence = self._load_last_source_sequence()

    def _publish_source_final(self, pending: PendingSourceFixture) -> None:
        if pending.source_emitted or not pending.source_text:
            return
        self._source_sequence += 1
        pending.source_sequence = self._source_sequence
        pending.source_identity = (
            f"{self.session_id}:G{self.connection_generation:06d}:"
            f"P{pending.provider_generation:06d}:S{pending.source_sequence:09d}"
        )
        self._append_record(
            {
                "schema_version": 2,
                "event_type": "source_final",
                "session_id": self.session_id,
                "source_identity": pending.source_identity,
                "source_sequence": pending.source_sequence,
                "provider_item_id": pending.sentence_id,
                "provider_generation": pending.provider_generation,
                "connection_generation": self.connection_generation,
                "sentence_id": pending.sentence_id,
                "begin_time": pending.start_ms,
                "end_time": pending.end_ms,
                "source_text": pending.source_text,
                "translated_text": "",
                "sentence_end": True,
            }
        )
        pending.source_emitted = True
        if self.source_final_sink is not None:
            self.source_final_sink(
                SourceFinalTranscript(
                    session_id=self.session_id,
                    sentence_id=pending.source_identity,
                    source_text=pending.source_text,
                    start_ms=pending.start_ms,
                    end_ms=pending.end_ms,
                    source_sequence=pending.source_sequence,
                    provider_item_id=pending.sentence_id,
                    connection_generation=self.connection_generation,
                )
            )

    def _append_record(self, record: dict[str, object]) -> None:
        if self._output_file is None:
            self.output_path.parent.mkdir(parents=True, exist_ok=True)
            self._output_file = self.output_path.open("a", encoding="utf-8")
        self._output_file.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._output_file.flush()
        os.fsync(self._output_file.fileno())

    def _load_last_source_sequence(self) -> int:
        if not self.output_path.is_file():
            return 0
        maximum = 0
        for line in self.output_path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            value = record.get("source_sequence") if isinstance(record, dict) else None
            if isinstance(value, int):
                maximum = max(maximum, value)
        return maximum

    def close(self) -> None:
        if self._output_file is not None:
            self._output_file.close()
            self._output_file = None


# Test-only compatibility aliases; no production runtime imports this module.
ClassroomTranscriptProcessor = Agent1SourceFixtureWriter
_PendingTranscript = PendingSourceFixture
