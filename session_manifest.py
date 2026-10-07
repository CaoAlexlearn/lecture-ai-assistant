"""Minimal per-run provenance manifest for the realtime lecture session."""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


class SessionManifestWriter:
    """Write one small JSON manifest without changing raw sentence records."""

    def __init__(
        self,
        path: Path,
        *,
        course_id: str,
        lecture_id: str,
        session_id: str,
        raw_output_path: Path,
        dense_output_path: Path | None,
        agent1_mode: str,
        connection_generation: int = 1,
    ) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        previous = self._load_existing()
        self.connection_generation = connection_generation
        connections = list(previous.get("connections", []))
        connections.append(
            {
                "generation": connection_generation,
                "started_at": utc_now_iso(),
                "ended_at": None,
                "pipeline_metrics": None,
            }
        )
        self._record: dict[str, Any] = {
            "course_id": course_id,
            "lecture_id": lecture_id,
            "session_id": session_id,
            "started_at": previous.get("started_at", utc_now_iso()),
            "ended_at": None,
            "raw_output_path": str(raw_output_path.resolve()),
            "agent1_mode": agent1_mode,
            "connection_count": int(previous.get("connection_count", 0)) + 1,
            "connections": connections,
        }
        if dense_output_path is not None:
            self._record["dense_output_path"] = str(dense_output_path.resolve())
        self._write()

    def _load_existing(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def finish(self, *, pipeline_metrics: dict[str, Any] | None = None) -> None:
        self._record["ended_at"] = utc_now_iso()
        if pipeline_metrics is not None:
            self._record["pipeline_metrics"] = pipeline_metrics
        for connection in reversed(self._record["connections"]):
            if connection["generation"] == self.connection_generation:
                connection["ended_at"] = self._record["ended_at"]
                connection["pipeline_metrics"] = pipeline_metrics
                break
        self._write()
        logger.info("Session manifest finalized path=%s", self.path)

    def _write(self) -> None:
        temporary_path = self.path.with_name(
            f".{self.path.name}.{uuid.uuid4().hex}.tmp"
        )
        with temporary_path.open("xb") as handle:
            handle.write(
                (json.dumps(self._record, ensure_ascii=False, indent=2) + "\n").encode(
                    "utf-8"
                )
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
