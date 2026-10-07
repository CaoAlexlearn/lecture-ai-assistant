"""Durable source-consumption state for the live Agent1 pipeline."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class Agent1SourceCheckpointError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _content_hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class Agent1SourceCheckpoint:
    """Atomically persist ownership of durable source-final events."""

    SCHEMA_VERSION = 2

    def __init__(self, path: Path, *, session_id: str) -> None:
        self.path = path
        self.session_id = session_id

    def load(self) -> dict[str, Any] | None:
        if not self.path.is_file():
            return None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise Agent1SourceCheckpointError(
                f"Agent1 source checkpoint is unreadable: {self.path}"
            ) from exc
        if not isinstance(value, dict):
            raise Agent1SourceCheckpointError(
                "Agent1 source checkpoint must be an object"
            )
        content_hash = value.pop("contentHash", None)
        if not isinstance(content_hash, str) or content_hash != _content_hash(value):
            raise Agent1SourceCheckpointError(
                "Agent1 source checkpoint integrity check failed"
            )
        schema_version = value.get("schemaVersion")
        if schema_version not in {1, self.SCHEMA_VERSION}:
            raise Agent1SourceCheckpointError(
                "Unsupported Agent1 source checkpoint schema"
            )
        if value.get("sessionId") != self.session_id:
            raise Agent1SourceCheckpointError(
                "Agent1 source checkpoint belongs to another ClassSession"
            )
        if not isinstance(value.get("lastConsumedSourceSequence"), int):
            raise Agent1SourceCheckpointError("Invalid source checkpoint cursor")
        if not isinstance(value.get("rawByteOffset"), int):
            raise Agent1SourceCheckpointError("Invalid source checkpoint offset")
        if not isinstance(value.get("tokenBuffer"), dict):
            raise Agent1SourceCheckpointError(
                "Invalid source checkpoint TokenBuffer state"
            )
        if schema_version == 1:
            # V1 checkpoints predate semantic-boundary state. They remain readable;
            # the pipeline supplies a fresh controller state and rewrites V2.
            value["boundaryController"] = None
        elif not isinstance(value.get("boundaryController"), dict):
            raise Agent1SourceCheckpointError(
                "Invalid source checkpoint boundary-controller state"
            )
        return value

    def save(
        self,
        *,
        last_consumed_source_sequence: int,
        raw_byte_offset: int,
        token_buffer_state: Mapping[str, Any],
        boundary_controller_state: Mapping[str, Any],
    ) -> dict[str, Any]:
        record = {
            "schemaVersion": self.SCHEMA_VERSION,
            "sessionId": self.session_id,
            "lastConsumedSourceSequence": last_consumed_source_sequence,
            "rawByteOffset": raw_byte_offset,
            "tokenBuffer": dict(token_buffer_state),
            "boundaryController": dict(boundary_controller_state),
            "updatedAt": _now(),
        }
        record["contentHash"] = _content_hash(record)
        self._write(record)
        return record

    def _write(self, record: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(
                    (
                        json.dumps(dict(record), ensure_ascii=False, indent=2) + "\n"
                    ).encode("utf-8")
                )
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
